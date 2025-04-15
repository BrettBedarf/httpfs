import os
import stat
import threading
import time
from typing import cast

import requests
from pyfuse3 import ROOT_INODE, EntryAttributes, ModeT, FileHandleT

from shared.files import (
    CACHE_LOCK,
    file_attributes_cache,
    file_chunk_cache,
    inode_map,
    source_files,
)
from shared.requests import ONGOING_LOCK, ongoing_requests
from utils.fetch_utils import fetch_chunks_sync
from config.constants import MAX_FH

from .logger import log_time, logger


@log_time
def get_file_attr(filename: str) -> EntryAttributes:
    logger.debug("Getting file attributes for '%s'", filename)
    if filename in file_attributes_cache:
        logger.debug("Returning cached file attributes for '%s'", filename)
        return file_attributes_cache[filename]

    inode = inode_map.get(filename)
    url = source_files.get(filename)
    if inode is None or url is None:
        logger.error("File not found: '%s'", filename)
        raise FileNotFoundError

    try:
        logger.info("Fetching HEAD from remote")
        r = requests.head(url, allow_redirects=True)
        size = int(r.headers.get("Content-Length", 0))
        logger.debug("Size of '%s': %d bytes", filename, size)
    except Exception as e:
        logger.error("Error fetching HEAD for '%s': %s", filename, e)
        size = 0

    now_ns = int(time.time() * 1e9)
    attr = EntryAttributes()
    attr.st_ino = inode
    attr.st_mode = cast(ModeT, stat.S_IFREG | 0o444)
    attr.st_size = size
    attr.st_uid = os.getuid()
    attr.st_gid = os.getgid()
    attr.st_atime_ns = now_ns
    attr.st_mtime_ns = now_ns
    attr.st_ctime_ns = now_ns
    attr.st_nlink = 1

    file_attributes_cache[filename] = attr
    return attr


@log_time
def get_root_attr() -> EntryAttributes:
    logger.debug("Getting root attributes")
    now_ns = int(time.time() * 1e9)
    attr = EntryAttributes()
    attr.st_ino = ROOT_INODE
    # Mark directory mode with 755 perms
    attr.st_mode = cast(ModeT, stat.S_IFDIR | 0o755)
    attr.st_uid = os.getuid()
    attr.st_gid = os.getgid()
    attr.st_size = 0
    attr.st_atime_ns = now_ns
    attr.st_mtime_ns = now_ns
    attr.st_ctime_ns = now_ns
    attr.st_nlink = 2
    return attr


@log_time
def get_file_chunk(file_url, chunk_start, chunk_size, total_size):
    cache_key = (file_url, chunk_start)
    with CACHE_LOCK:
        if cache_key in file_chunk_cache:
            return file_chunk_cache[cache_key]

    is_ongoing = False
    with ONGOING_LOCK:
        event = ongoing_requests.get(cache_key)
        if event:
            is_ongoing = True
        else:
            event = threading.Event()
            ongoing_requests[cache_key] = event
    if is_ongoing:
        # Wait for the fetching thread to complete
        event.wait()
        with CACHE_LOCK:
            return file_chunk_cache.get(cache_key, b"")
    else:
        chunk = fetch_chunks_sync(file_url, chunk_start, chunk_size, total_size)
        with CACHE_LOCK:
            file_chunk_cache[cache_key] = chunk
        with ONGOING_LOCK:
            ongoing_requests[cache_key].set()  # important to signal waiting threads
            del ongoing_requests[cache_key]
        return chunk


_next_fh = 1
FH_LOCK = threading.Lock()
# Mapping: file handle -> metadata dictionary (e.g. inode and allocation timestamp)
open_handles = {}


def get_next_fh(inode):
    global _next_fh
    with FH_LOCK:
        candidate = _next_fh
        _next_fh = (_next_fh + 1) % MAX_FH
        while candidate in open_handles:
            candidate = _next_fh
            _next_fh = (_next_fh + 1) % MAX_FH
        open_handles[candidate] = {"inode": inode, "allocated_at": time.time()}
        return FileHandleT(candidate)
