import asyncio
import threading
import time

import aiohttp
import requests

from config.constants import DEFAULT_CHUNK_SIZE, MAX_PREFETCH_AHEAD, PREFETCH_BATCH_SIZE
from shared.files import CACHE_LOCK, file_chunk_cache
from shared.requests import PREFETCH_LOCK, SESSION_LOCK, prefetch_threads, sessions

from .logger import log_time, logger

IDLE_TIMEOUT = 300  # seconds


@log_time
def get_session_for_url(url):
    now = time.time()
    with SESSION_LOCK:
        if url in sessions:
            session, _ = sessions[url]
            sessions[url] = (session, now)
            return session
        else:
            s = requests.Session()
            sessions[url] = (s, now)
            return s


@log_time
def cleanup_sessions():
    while True:
        time.sleep(60)  # check every minute
        now = time.time()
        with SESSION_LOCK:
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


@log_time
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


async def fetch_chunk(session, url, offset, chunk_size):
    headers = {"Range": f"bytes={offset}-{offset + chunk_size - 1}"}
    start = time.perf_counter()
    async with session.get(url, headers=headers) as response:
        if response.status in (200, 206):
            ret = await response.read()
            end = time.perf_counter() - start
            logger.debug(
                f"fetch_chunk: offset={offset}, chunk_size={chunk_size}, elapsed={end:.4f} seconds"
            )
            return ret
        else:
            end = time.perf_counter() - start
            logger.debug(
                f"fetch_chunk FAIL: offset={offset}, chunk_size={chunk_size}, elapsed={end:.4f} seconds"
            )
            raise Exception(f"HTTP error {response.status}")


# Async function to fetch multiple chunks concurrently.
async def fetch_chunks_async(url, offsets, chunk_size):
    async with aiohttp.ClientSession() as session:
        tasks = [fetch_chunk(session, url, offset, chunk_size) for offset in offsets]
        return await asyncio.gather(*tasks)


@log_time
# Synchronous wrapper around the async function for fuse compat
def fetch_chunks_sync(url, offsets, chunk_size, total_size):
    # Only fetch offsets less than the file's total size.
    valid_offsets = [offset for offset in offsets if offset < total_size]
    if not valid_offsets:
        return []
    result = asyncio.run(fetch_chunks_async(url, valid_offsets, chunk_size))
    # Log the current cache size in MB and active prefetch threads.
    with CACHE_LOCK:
        total_bytes = sum(len(chunk) for chunk in file_chunk_cache.values())
    cache_mb = total_bytes / (1024 * 1024)
    with PREFETCH_LOCK:
        active_prefetch = len(prefetch_threads)
    logger.debug(
        f"fetch_chunks_sync: current file_chunk_cache size: {cache_mb:.2f} MB; active prefetch threads: {active_prefetch}"
    )
    return result


@log_time
def prefetch(url, start_offset, chunk_size, max_prefetch_bytes, total_size):
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
            with CACHE_LOCK:
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
        chunks = fetch_chunks_sync(url, offsets_to_fetch, chunk_size, total_size)
        # Store them in the cache
        with CACHE_LOCK:
            for i, offset in enumerate(offsets_to_fetch):
                file_chunk_cache[(url, offset)] = chunks[i]

    # Remove thread marker when done
    with PREFETCH_LOCK:
        prefetch_threads.pop(url, None)


@log_time
def maybe_prefetch(url, current_read_offset, total_size):
    # Determine how far we've cached for this URL.
    with CACHE_LOCK:
        cached_offsets = [off for (u, off) in file_chunk_cache if u == url]
    highest_cached = max(cached_offsets) if cached_offsets else current_read_offset
    target = current_read_offset + MAX_PREFETCH_AHEAD
    if highest_cached < target:
        # Spawn a thread to prefetch continuously from the current highest offset up to the target.
        threading.Thread(
            target=prefetch,
            args=(
                url,
                highest_cached,
                DEFAULT_CHUNK_SIZE,
                target - highest_cached,
                total_size,
            ),
            daemon=True,
        ).start()
