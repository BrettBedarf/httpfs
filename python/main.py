#!/usr/bin/env python3
import errno
import json
import logging
import os
import socket
import sys
import threading

import debugpy
import pyfuse3
import trio
from pyfuse3 import FileHandleT, FileNameT, Operations, RequestContext

from config.constants import (
    DEFAULT_CHUNK_SIZE,
)
from shared.files import (
    FILES_LOCK,
    inode_map,
    source_files,
)
from utils.fetch_utils import fetch_chunks_sync, maybe_prefetch
from utils.file_utils import (
    get_file_attr,
    get_root_attr,
    get_next_fh,
    FH_LOCK,
    open_handles,
)
from utils.logger import logger

# This should not block execution
debugpy.listen(("0.0.0.0", 5678))
print("Debugpy is listening on port 5678; wait_for_client")
# debugpy.wait_for_client()


next_inode = 2  # starting inode (root is 1)


class HTTPFS(Operations):
    async def lookup(self, parent_inode, name, ctx):
        if parent_inode != pyfuse3.ROOT_INODE:
            logger.error("lookup: non-root parent inode %d", parent_inode)
            raise pyfuse3.FUSEError(errno.ENOENT)
        filename = name.decode("utf-8") if isinstance(name, bytes) else name
        # if filename[0] != ".":
        #     logger.debug("lookup: parent_inode=%d, name=%s", parent_inode, name)
        with FILES_LOCK:
            if filename not in source_files:
                logging.error("lookup: '%s' not found", filename)
                raise pyfuse3.FUSEError(errno.ENOENT)
            inode = inode_map.get(filename)
            if inode is None:
                global next_inode
                inode = next_inode
                inode_map[filename] = inode
                next_inode += 1
                logger.debug("lookup: assigned new inode %d to '%s'", inode, filename)
        return get_file_attr(filename)

    async def getattr(self, inode, ctx):
        logger.debug("getattr: inode=%d", inode)
        if inode == pyfuse3.ROOT_INODE:
            return get_root_attr()
        with FILES_LOCK:
            filename = next((fn for fn, ino in inode_map.items() if ino == inode), None)
        if filename is None:
            logger.error("getattr: inode %d not found", inode)
            raise pyfuse3.FUSEError(errno.ENOENT)
        return get_file_attr(filename)

    async def opendir(self, inode: int, ctx: RequestContext) -> FileHandleT:
        logger.debug("opendir: inode=%d", inode)
        if inode != pyfuse3.ROOT_INODE:
            logger.error("opendir: inode %d is not root", inode)
            raise pyfuse3.FUSEError(errno.ENOTDIR)
        fh = get_next_fh(inode)
        return fh

    async def readdir(
        self, fh: FileHandleT, start_id: int, token: pyfuse3.ReaddirToken
    ) -> None:
        logger.debug("readdir: fh=%d, start_id=%d", fh, start_id)
        # if fh != pyfuse3.ROOT_INODE:
        #     logger.error("readdir: file handle %d is not root", fh)
        #     return

        # Build directory entry list including '.' and '..'
        entries = [
            (FileNameT(b"."), get_root_attr()),
            (FileNameT(b".."), get_root_attr()),
        ]
        with FILES_LOCK:
            for filename in source_files.keys():
                try:
                    attr = get_file_attr(filename)
                    entries.append((FileNameT(filename.encode("utf-8")), attr))
                    logger.debug("readdir: adding entry '%s'", filename)
                except FileNotFoundError:
                    logger.error("readdir: file '%s' not found", filename)
                    continue

        # Use the pyfuse3.readdir_reply method to add each entry.
        for idx, (name, attr) in enumerate(entries):
            if idx < start_id:
                continue
            next_id = idx + 1
            if not pyfuse3.readdir_reply(token, name, attr, next_id):
                break

    async def open(
        self, inode: int, flags: int, ctx: RequestContext
    ) -> pyfuse3.FileInfo:
        logger.debug("open: inode=%d, flags=%d", inode, flags)
        if flags & (os.O_WRONLY | os.O_RDWR):
            logger.error("open: write access denied for inode=%d", inode)
            raise pyfuse3.FUSEError(errno.EACCES)

        fi = pyfuse3.FileInfo(
            fh=get_next_fh(inode),
            direct_io=True,  # take over responsibilty for caching, buffering, etc from kernel
        )

        return fi

    async def read(self, fh: FileHandleT, off: int, size: int) -> bytes:
        logger.debug("read: fh=%d, off=%d, size=%d", fh, off, size)
        with FH_LOCK:
            handle_details = open_handles.get(fh)
            ino = handle_details.get("inode") if handle_details else None
            if not ino:
                logger.error(f"no inode found for handle {fh}")
                raise pyfuse3.FUSEError(errno.ENOENT)
        with FILES_LOCK:
            # TODO: refactor INODE_MAP to be inode -> filename
            filename = next((fn for fn, _ino in inode_map.items() if _ino == fh), None)
            if filename is None:
                raise pyfuse3.FUSEError(errno.ENOENT)
            url = source_files.get(filename)
        if not url:
            raise pyfuse3.FUSEError(errno.ENOENT)
        attr = get_file_attr(filename)
        total_size = attr.st_size
        # Determine chunk boundaries covering the requested range.
        start_offset = off - (off % DEFAULT_CHUNK_SIZE)
        end_offset = off + size
        # Offsets at which each chunk starts
        offsets = list(range(start_offset, end_offset, DEFAULT_CHUNK_SIZE))
        # If range() is empty, force at least one offset
        if not offsets:
            offsets = [start_offset]

        # Fetch all needed chunks concurrently.
        # fetch_chunks_sync expects a list of offsets.
        chunks = fetch_chunks_sync(url, offsets, DEFAULT_CHUNK_SIZE, total_size)

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
        maybe_prefetch(url, off + size, total_size)

        logger.debug("read: returning %d bytes", len(result))
        return result


def listen_for_updates(port=9000):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("localhost", port))
    sock.listen(5)
    logger.info("Update server listening on port %d", port)
    while True:
        conn, _ = sock.accept()
        logger.debug("Update server: connection accepted")
        data = conn.recv(1024).decode("utf-8")
        logger.debug("Update server: received data: %s", data)
        try:
            update = json.loads(data)
            filename = update.get("filename")
            url = update.get("url")
            if filename and url:
                with FILES_LOCK:
                    source_files[filename] = url
                    if filename not in inode_map:
                        global next_inode
                        inode_map[filename] = next_inode
                        next_inode += 1
                logger.info("Added mapping: '%s' -> '%s'", filename, url)
                conn.send(b"OK")
            else:
                logger.error("Update server: invalid data received")
                conn.send(b"ERROR: Invalid data")
        except Exception as e:
            logger.exception("Update server: error processing update")
            conn.send(f"ERROR: {str(e)}".encode("utf-8"))
        conn.close()
        logger.debug("Update server: connection closed")


async def main(mountpoint):
    fs = HTTPFS()
    fuse_opts = set(pyfuse3.default_options)
    # fuse_opts.add(
    #     "allow_other"
    # )  # Allow all users (not just the mounter) to access the FS.
    fuse_opts.add("ro")  # Mount as read-only.
    fuse_opts.add("fsname=httpls")  # Set a custom filesystem name ("httpls").
    # fuse_opts.add("max_read=65536")  # Limit each read request to 64KB.
    fuse_opts.add(
        "auto_unmount"
    )  # Automatically unmount the FS if the process terminates.
    fuse_opts.add(
        "nodiratime"
    )  # Do not update directory access times (reduces overhead).

    pyfuse3.init(fs, mountpoint, fuse_opts)
    logger.info("FUSE filesystem mounted on '%s'", mountpoint)
    try:
        await pyfuse3.main()
    finally:
        logger.info("Unmounting filesystem")
        pyfuse3.close()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <mountpoint>")
        sys.exit(1)
    mountpoint = sys.argv[1]

    # Pre-populate the inode map.
    with FILES_LOCK:
        for filename in list(source_files.keys()):
            if filename not in inode_map:
                inode_map[filename] = next_inode
                next_inode += 1

    threading.Thread(target=listen_for_updates, daemon=True).start()

    # Allow clean exit with Ctrl+C.
    def sigint_handler(signum, frame):
        logger.info("SIGINT received, exiting...")
        pyfuse3.close()
        sys.exit(0)

    # signal.signal(signal.SIGINT, sigint_handler)

    trio.run(main, mountpoint)
