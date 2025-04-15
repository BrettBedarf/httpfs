import threading

# Global dict for per-chunk events
ongoing_requests = {}
ONGOING_LOCK = threading.Lock()

# Global dict to track active prefetch threads per URL
prefetch_threads = {}
PREFETCH_LOCK = threading.Lock()


# Global mapping: file URL -> persistent requests.Session
sessions = {}
SESSION_LOCK = threading.Lock()
