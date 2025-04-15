package config

// TODO: use as defaults but load overrides from config
const (
	// MAX_FH is the maximum file handle value
	// 32-bits ensures compat with libfuse and more than enough open handles
	MAX_FH = (1 << 31) - 1

	// Read cache configuration
	DEFAULT_CHUNK_SIZE = 1 * 1024 * 1024   // 1MB per chunk
	CACHE_MAX_SIZE     = 200 * 1024 * 1024 // 200MB total
	NUM_CACHE_CHUNKS   = CACHE_MAX_SIZE / DEFAULT_CHUNK_SIZE

	MAX_PREFETCH_AHEAD = 100 * 1024 * 1024 // e.g., 100MB ahead
	// How many chunks to fetch concurrently each batch
	PREFETCH_BATCH_SIZE = 3
)
