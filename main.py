#!/usr/bin/env python3
import llfuse
import stat
import time
import threading
import socket
import json
import requests
import os
import sys
import errno
import signal
import logging
from llfuse import EntryAttributes

# Global mapping of local filenames to HTTP URLs.
FILES = {
    "Running Point S01E01 Pilot 1080p NF WEB-DL DDP5 1 Atmos H 264-FLUX.mkv": "https://easydebrid.com/cdndownload/R2uzVaKTAPmsJpMq7YdEiat6BBHEd9C0/Xq9Kqi5OhWan6824yODmOyIckdZ3b7mkIloMCmpaEDT2dxQNw8Wmckr_jMbMvtZCF-QoGeAJJeZsbNn5B2RK3V4X9TdhGios_cR_fuvOG3FMETBPInYzlZFw01X095vuLHpuTgvDYl5mxzCLGFXht1lGJ73Fy80_PGqYGf619kj9Q6Dk0qOxYQpLlU0MRtBFBIT-PTRju9TJzjZn8t_--Hjkqtu_VsMv5qLAawC-4EAihf6WxBI9t6PFz0-8sUhebImeVggyfTQs1TDBHn_PnHjI5noA5hJjj-1GZxTTS8r94Y7Ydo8m77xBeUUOdWkJXCYfk4F8_4_MW4g7K0mBfEpgwxUcYFdSNRuxZWEH5JtZweBGiujPX6NrfRcBTg/Running%2520Point%2520S01E01%2520Pilot%25201080p%2520NF%2520WEB-DL%2520DDP5%25201%2520Atmos%2520H%2520264-STC%255BEZTVx.to%255D.mkv",
}

FILES_LOCK = threading.Lock()
INODE_MAP = {}  # filename -> inode
NEXT_INODE = 2  # starting inode (root is 1)

# Setup debug logging.
logging.basicConfig(
    level=logging.DEBUG, format="%(asctime)s [%(levelname)s] %(message)s"
)


def get_file_attr(filename: str) -> EntryAttributes:
    logging.debug("Getting file attributes for '%s'", filename)
    attr = EntryAttributes()
    with FILES_LOCK:
        inode = INODE_MAP.get(filename)
        url = FILES.get(filename)
    if inode is None or url is None:
        logging.error("File not found: '%s'", filename)
        raise FileNotFoundError

    try:
        r = requests.head(url)
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


class HTTPFS(llfuse.Operations):
    def lookup(self, parent_inode, name, ctx):
        logging.debug("lookup: parent_inode=%d, name=%s", parent_inode, name)
        if parent_inode != llfuse.ROOT_INODE:
            logging.error("lookup: non-root parent inode %d", parent_inode)
            raise llfuse.FUSEError(errno.ENOENT)
        filename = name.decode("utf-8") if isinstance(name, bytes) else name
        with FILES_LOCK:
            if filename not in FILES:
                logging.error("lookup: '%s' not found", filename)
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
        return inode

    def read(self, fh, off, size):
        logging.debug("read: fh=%d, off=%d, size=%d", fh, off, size)
        with FILES_LOCK:
            filename = next((fn for fn, ino in INODE_MAP.items() if ino == fh), None)
            if filename is None:
                logging.error("read: inode %d not found", fh)
                raise llfuse.FUSEError(errno.ENOENT)
            url = FILES.get(filename)
        if not url:
            logging.error("read: URL for '%s' not found", filename)
            raise llfuse.FUSEError(errno.ENOENT)
        headers = {"Range": f"bytes={off}-{off + size - 1}"}
        r = requests.get(url, headers=headers)
        if r.status_code in (200, 206):
            logging.debug("read: read %d bytes from '%s'", len(r.content), url)
            return r.content
        logging.error("read: HTTP error %d for '%s'", r.status_code, url)
        return b""


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
