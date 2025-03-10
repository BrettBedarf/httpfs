#!/usr/bin/env python3
import errno
import json
import logging
import os
import signal
import socket
import stat
import sys
import threading
import time
import llfuse
import requests
from llfuse import EntryAttributes
import cachetools
import asyncio
import aiohttp

# This should not block execution
# debugpy.listen(("0.0.0.0", 5678))
# print("Debugpy is listening on port 5678; wait_for_client")
# debugpy.wait_for_client()

# Global mapping of local filenames to HTTP URLs.
FILES = {
    "Running Point S01E01 Pilot 1080p NF WEB-DL DDP5 1 Atmos H 264-FLUX.mkv": "https://easydebrid.com/cdndownload/WwSCAwMuo_qMAU1Q7k2SnBOGBVQELdd0/l5JjZHC_kkDieUaIX3YYG6xSu99bC1iyDLYxGEs3dsG054HfYM5Hf4X1h64K8zFXIK6n8nJuakPwhe9OrcXpcNpknZvf3cix4qwILbOvByv-RUpXltwdy9a3XmAQxAJiOemLJikMh7toFAKhqqm8DLhZroHRslqAlnocgOBUDE65PvkcaX_0nW4JRBsdS1Y4SYSDSU8GdTIwYhKaAEA2-NCc8JlRzKZIjQl9FB02dpwNOcFAWU7MPk9QfRpcvpEsI0afpzK4Uz2FAyNOwEqIL4FPkEepTXm-xuvyKxrzJMQSGQBM7bIIVtDugR2JjBWakqOzzBXk7E5PMw4fswPWZvUi9YhLqYWKtB2nOZrfFuiL/Running%2520Point%2520S01E01%2520Pilot%25201080p%2520NF%2520WEB-DL%2520DDP5%25201%2520Atmos%2520H%2520264-FLUX.mkv"
}

FILES_LOCK = threading.Lock()
INODE_MAP = {}  # filename -> inode
NEXT_INODE = 2  # starting inode (root is 1)

# Read cache configuration
DEFAULT_CHUNK_SIZE = 2 * 1024 * 1024  # 4MB per chunk
CACHE_MAX_SIZE = 200 * 1024 * 1024  # 100MB total
NUM_CACHE_CHUNKS = CACHE_MAX_SIZE // DEFAULT_CHUNK_SIZE
MAX_PREFETCH_AHEAD = 100 * 1024 * 1024  # e.g., 100MB ahead
# How many chunks to fetch concurrently each batch.
PREFETCH_BATCH_SIZE = 8

# Setup debug logging.
logging.basicConfig(
    level=logging.DEBUG, format="%(asctime)s [%(levelname)s] %(message)s"
)

file_attributes_cache = {}


def get_file_attr(filename: str) -> EntryAttributes:
    logging.debug("Getting file attributes for '%s'", filename)
    if filename in file_attributes_cache:
        logging.debug("Returning cached file attributes for '%s'", filename)
        return file_attributes_cache[filename]
    attr = EntryAttributes()
    inode = INODE_MAP.get(filename)
    url = FILES.get(filename)
    if inode is None or url is None:
        logging.error("File not found: '%s'", filename)
        raise FileNotFoundError

    try:
        logging.info("Fetching HEAD from remote")
        r = requests.head(url, allow_redirects=True)
        size = int(r.headers.get("Content-Length", 0))
        logging.debug("Size of '%s': %d bytes", filename, size)
    except Exception as e:
        logging.error("Error fetching HEAD for '%s': %s", filename, e)
        size = 0

    now_ns = int(time.time() * 1e9)
    attr.st_ino = inode
    attr.st_mode = stat.S_IFREG | 0o444
    attr.st_size = size
    attr.st_uid = os.getuid()
    attr.st_gid = os.getgid()
    attr.st_atime_ns = now_ns
    attr.st_mtime_ns = now_ns
    attr.st_ctime_ns = now_ns
    attr.st_nlink = 1
    # cache so we don't keep firing http requests every read
    file_attributes_cache[filename] = attr

    return attr


def get_root_attr() -> EntryAttributes:
    logging.debug("Getting root attributes")
    attr = EntryAttributes()
    attr.st_ino = llfuse.ROOT_INODE
    attr.st_mode = stat.S_IFDIR | 0o755
    attr.st_size = 0
    attr.st_uid = os.getuid()
    attr.st_gid = os.getgid()
    now_ns = int(time.time() * 1e9)
    attr.st_atime_ns = now_ns
    attr.st_mtime_ns = now_ns
    attr.st_ctime_ns = now_ns
    attr.st_nlink = 2
    return attr


# LRU cache to store chunks
file_chunk_cache = cachetools.LRUCache(maxsize=NUM_CACHE_CHUNKS)
cache_lock = threading.Lock()

# Global dict for per-chunk events
ongoing_requests = {}
ongoing_lock = threading.Lock()

# Global dict to track active prefetch threads per URL
prefetch_threads = {}
prefetch_lock = threading.Lock()


# Global mapping: file URL -> persistent requests.Session
sessions = {}
session_lock = threading.Lock()
IDLE_TIMEOUT = 300  # seconds


def get_session_for_url(url):
    now = time.time()
    with session_lock:
        if url in sessions:
            session, _ = sessions[url]
            sessions[url] = (session, now)
            return session
        else:
            s = requests.Session()
            sessions[url] = (s, now)
            return s


def cleanup_sessions():
    while True:
        time.sleep(60)  # check every minute
        now = time.time()
        with session_lock:
            for url in list(sessions.keys()):
                session, last_used = sessions[url]
                if now - last_used > IDLE_TIMEOUT:
                    session.close()
                    del sessions[url]
                    # also clear the redirect cache
                    del redirect_cache[url]


# Start cleanup thread
threading.Thread(target=cleanup_sessions, daemon=True).start()

# Global cache for resolved URLs
redirect_cache = {}
redirect_cache_lock = threading.Lock()


def resolve_redirect(url):
    with redirect_cache_lock:
        if url in redirect_cache:
            return redirect_cache[url]
    session = get_session_for_url(url)
    r = session.head(url, allow_redirects=True)
    final_url = r.url
    with redirect_cache_lock:
        redirect_cache[url] = final_url
    return final_url


def maybe_prefetch(url, current_read_offset):
    # Determine how far we've cached for this URL.
    with cache_lock:
        cached_offsets = [off for (u, off) in file_chunk_cache if u == url]
    highest_cached = max(cached_offsets) if cached_offsets else current_read_offset
    target = current_read_offset + MAX_PREFETCH_AHEAD
    if highest_cached < target:
        # Spawn a thread to prefetch continuously from the current highest offset up to the target.
        threading.Thread(
            target=prefetch,
            args=(url, highest_cached, DEFAULT_CHUNK_SIZE, target - highest_cached),
            daemon=True,
        ).start()


async def fetch_chunk(session, url, offset, chunk_size):
    headers = {"Range": f"bytes={offset}-{offset + chunk_size - 1}"}
    async with session.get(url, headers=headers) as response:
        if response.status in (200, 206):
            return await response.read()
        else:
            raise Exception(f"HTTP error {response.status}")


# Async function to fetch multiple chunks concurrently.
async def fetch_chunks_async(url, offsets, chunk_size):
    async with aiohttp.ClientSession() as session:
        tasks = [fetch_chunk(session, url, offset, chunk_size) for offset in offsets]
        return await asyncio.gather(*tasks)


# Synchronous wrapper around the async function.
def fetch_chunks_sync(url, offsets, chunk_size):
    return asyncio.run(fetch_chunks_async(url, offsets, chunk_size))


def get_file_chunk(file_url, chunk_start, chunk_size):
    cache_key = (file_url, chunk_start)
    with cache_lock:
        if cache_key in file_chunk_cache:
            return file_chunk_cache[cache_key]

    is_ongoing = False
    with ongoing_lock:
        event = ongoing_requests.get(cache_key)
        if event:
            is_ongoing = True
        else:
            event = threading.Event()
            ongoing_requests[cache_key] = event
    if is_ongoing:
        # Wait for the fetching thread to complete
        event.wait()
        with cache_lock:
            return file_chunk_cache.get(cache_key, b"")
    else:
        chunk = fetch_chunks_sync(file_url, chunk_start, chunk_size)
        with cache_lock:
            file_chunk_cache[cache_key] = chunk
        with ongoing_lock:
            ongoing_requests[cache_key].set()  # important to signal waiting threads
            del ongoing_requests[cache_key]
        return chunk


def prefetch(url, start_offset, chunk_size, max_prefetch_bytes):
    """
    Prefetch multiple chunks at once until we fill up max_prefetch_bytes.
    """
    current = start_offset
    end_offset = start_offset + max_prefetch_bytes

    while current < end_offset:
        # Accumulate a list of offsets we still need (and aren't cached yet).
        offsets_to_fetch = []
        for _ in range(PREFETCH_BATCH_SIZE):
            if current >= end_offset:
                break
            cache_key = (url, current)
            with cache_lock:
                # If it's already cached, skip it
                if cache_key in file_chunk_cache:
                    current += chunk_size
                    continue
            offsets_to_fetch.append(current)
            current += chunk_size

        # If we didn't find any offsets to fetch this round, break out
        if not offsets_to_fetch:
            break

        # Now fetch them in one async batch
        chunks = fetch_chunks_sync(url, offsets_to_fetch, chunk_size)
        # Store them in the cache
        with cache_lock:
            for i, offset in enumerate(offsets_to_fetch):
                file_chunk_cache[(url, offset)] = chunks[i]

    # Remove thread marker when done
    with prefetch_lock:
        prefetch_threads.pop(url, None)


class HTTPFS(llfuse.Operations):
    def lookup(self, parent_inode, name, ctx):
        if parent_inode != llfuse.ROOT_INODE:
            logging.error("lookup: non-root parent inode %d", parent_inode)
            raise llfuse.FUSEError(errno.ENOENT)
        filename = name.decode("utf-8") if isinstance(name, bytes) else name
        # if filename[0] != ".":
        #     logging.debug("lookup: parent_inode=%d, name=%s", parent_inode, name)
        with FILES_LOCK:
            if filename not in FILES:
                # if filename[0] != ".":
                # logging.error("lookup: '%s' not found", filename)
                raise llfuse.FUSEError(errno.ENOENT)
            inode = INODE_MAP.get(filename)
            if inode is None:
                global NEXT_INODE
                inode = NEXT_INODE
                INODE_MAP[filename] = inode
                NEXT_INODE += 1
                logging.debug("lookup: assigned new inode %d to '%s'", inode, filename)
        return get_file_attr(filename)

    def getattr(self, inode, ctx):
        logging.debug("getattr: inode=%d", inode)
        if inode == llfuse.ROOT_INODE:
            return get_root_attr()
        with FILES_LOCK:
            filename = next((fn for fn, ino in INODE_MAP.items() if ino == inode), None)
        if filename is None:
            logging.error("getattr: inode %d not found", inode)
            raise llfuse.FUSEError(errno.ENOENT)
        return get_file_attr(filename)

    def opendir(self, inode, ctx):
        logging.debug("opendir: inode=%d", inode)
        if inode != llfuse.ROOT_INODE:
            logging.error("opendir: inode %d is not root", inode)
            raise llfuse.FUSEError(errno.ENOTDIR)
        return inode

    def readdir(self, fh, off):
        logging.debug("readdir: fh=%d, off=%d", fh, off)
        if fh != llfuse.ROOT_INODE:
            logging.error("readdir: file handle %d is not root", fh)
            return

        entries = [(b".", get_root_attr()), (b"..", get_root_attr())]
        with FILES_LOCK:
            for filename in FILES.keys():
                try:
                    attr = get_file_attr(filename)
                    entries.append((filename.encode("utf-8"), attr))
                    logging.debug("readdir: adding entry '%s'", filename)
                except FileNotFoundError:
                    logging.error("readdir: file '%s' not found", filename)
                    continue
        for i, (name, attr) in enumerate(entries):
            if i < off:
                continue
            logging.debug("readdir: yielding '%s' at offset %d", name, i + 1)
            yield (name, attr, i + 1)

    def open(self, inode, flags, ctx):
        logging.debug("open: inode=%d, flags=%d", inode, flags)
        if flags & (os.O_WRONLY | os.O_RDWR):
            logging.error("open: write access denied for inode=%d", inode)
            raise llfuse.FUSEError(errno.EACCES)
        with FILES_LOCK:
            filename = next((fn for fn, ino in INODE_MAP.items() if ino == inode), None)
            url = FILES.get(filename) if filename else None
        if filename and url:
            cache_key = (url, 0)
            with cache_lock:
                # Only start prefetch if the first chunk isn't already cached or fetching
                if cache_key not in file_chunk_cache:
                    with prefetch_lock:
                        if url not in prefetch_threads:
                            file_size = get_file_attr(filename).st_size
                            t = threading.Thread(
                                target=prefetch,
                                args=(
                                    url,
                                    0,
                                    DEFAULT_CHUNK_SIZE,
                                    min(CACHE_MAX_SIZE, file_size, 10 * 1024 * 1024),
                                ),
                                daemon=True,
                            )
                            prefetch_threads[url] = t
                            t.start()
        return inode

    def read(self, fh, off, size):
        logging.debug("read: fh=%d, off=%d, size=%d", fh, off, size)
        with FILES_LOCK:
            filename = next((fn for fn, ino in INODE_MAP.items() if ino == fh), None)
            if filename is None:
                raise llfuse.FUSEError(errno.ENOENT)
            url = FILES.get(filename)
        if not url:
            raise llfuse.FUSEError(errno.ENOENT)

        # Determine chunk boundaries covering the requested range.
        start_offset = off - (off % DEFAULT_CHUNK_SIZE)
        end_offset = off + size  # Offsets at which each chunk starts
        offsets = list(range(start_offset, end_offset, DEFAULT_CHUNK_SIZE))
        # If range() is empty, force at least one offset
        if not offsets:
            offsets = [start_offset]

        # Fetch all needed chunks concurrently.
        # fetch_chunks_sync expects a list of offsets.
        chunks = fetch_chunks_sync(url, offsets, DEFAULT_CHUNK_SIZE)

        # Assemble the requested data:
        data = bytearray()
        for i, chunk in enumerate(chunks):
            # For the first chunk, start at the proper offset.
            if i == 0:
                start_in_chunk = off - start_offset
                data.extend(chunk[start_in_chunk:])
            else:
                data.extend(chunk)
        # Trim data to exactly 'size' bytes.
        result = bytes(data[:size])

        # Trigger prefetch for data beyond what was just read.
        maybe_prefetch(url, off + size)

        logging.debug("read: returning %d bytes", len(result))
        return result


def listen_for_updates(port=9000):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("localhost", port))
    sock.listen(5)
    logging.info("Update server listening on port %d", port)
    while True:
        conn, _ = sock.accept()
        logging.debug("Update server: connection accepted")
        data = conn.recv(1024).decode("utf-8")
        logging.debug("Update server: received data: %s", data)
        try:
            update = json.loads(data)
            filename = update.get("filename")
            url = update.get("url")
            if filename and url:
                with FILES_LOCK:
                    FILES[filename] = url
                    if filename not in INODE_MAP:
                        global NEXT_INODE
                        INODE_MAP[filename] = NEXT_INODE
                        NEXT_INODE += 1
                logging.info("Added mapping: '%s' -> '%s'", filename, url)
                conn.send(b"OK")
            else:
                logging.error("Update server: invalid data received")
                conn.send(b"ERROR: Invalid data")
        except Exception as e:
            logging.exception("Update server: error processing update")
            conn.send(f"ERROR: {str(e)}".encode("utf-8"))
        conn.close()
        logging.debug("Update server: connection closed")


def main(mountpoint):
    fs = HTTPFS()
    llfuse.init(fs, mountpoint, ["fsname=httpls", "ro"])
    logging.info("FUSE filesystem mounted on '%s'", mountpoint)
    try:
        llfuse.main()
    finally:
        logging.info("Unmounting filesystem")
        llfuse.close()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <mountpoint>")
        sys.exit(1)
    mountpoint = sys.argv[1]

    # Pre-populate the inode map.
    with FILES_LOCK:
        for filename in list(FILES.keys()):
            if filename not in INODE_MAP:
                INODE_MAP[filename] = NEXT_INODE
                NEXT_INODE += 1

    threading.Thread(target=listen_for_updates, daemon=True).start()

    # Allow clean exit with Ctrl+C.
    def sigint_handler(signum, frame):
        logging.info("SIGINT received, exiting...")
        llfuse.close()
        sys.exit(0)

    signal.signal(signal.SIGINT, sigint_handler)

    main(mountpoint)
