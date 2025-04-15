import json
import threading

import cachetools

from config.constants import NUM_CACHE_CHUNKS


source_json = json.load(open("tests/fixtures/pub_sources.json"))
# Global mapping of local filenames to HTTP URLs.
source_files = {}
for source_file in source_json["categories"][0]["videos"]:
    source_url: str = source_file["sources"][0]
    filename = source_url.split("/")[-1]
    print(f"{filename} : {source_url}")

    source_files[filename] = source_url

FILES_LOCK = threading.Lock()
file_attributes_cache = {}

# LRU cache to store chunks
file_chunk_cache = cachetools.LRUCache(maxsize=NUM_CACHE_CHUNKS)
CACHE_LOCK = threading.Lock()

# TODO: INODE_MAP should be a mapping of inode -> file
inode_map = {}  # filename -> inode
