package main

import (
	"flag"
	"log"
	"os"
	"os/signal"
	"sync"
	"syscall"

	"github.com/brettbedarf/httpfs/util"
	"github.com/hanwen/go-fuse/v2/fuse"
)

var (
	nextInode   uint64       = 2                       // starting inode (root is 1)
	sourceFiles              = make(map[string]string) // filename -> url mapping
	inodeMap                 = make(map[string]uint64) // filename -> inode mapping
	filesLock   sync.RWMutex                           // lock for accessing the above maps
)

// HttpFS implements the FUSE filesystem
type HttpFS struct {
	fuse.RawFileSystem
}

func NewHttpFS() *HttpFS {
	return &HttpFS{
		RawFileSystem: fuse.NewDefaultRawFileSystem(),
	}
}

// TODO: see if the higher-lvl v2/fs lib has default method implementations for boilerplate
// methods

func (fs *HttpFS) String() string {
	return "httpfs"
}

func (fs *HttpFS) ReadDir(cancel <-chan struct{}, input *fuse.ReadIn, out *fuse.DirEntryList) fuse.Status {
	logger := util.GetLogger("readdir")
	logger.Debug().
		Uint64("fh", input.Fh).
		Int("offset", int(input.Offset)).
		Msg("ReadDir called")

	// Only allow reading the root directory for simplicity
	if input.NodeId != fuse.FUSE_ROOT_ID {
		logger.Error().
			Uint64("inode", input.NodeId).
			Msg("ReadDir: inode is not root")
		return fuse.ENOTDIR
	}

	// Start at the provided offset
	startIdx := int(input.Offset)

	// First, add "." and ".." entries if we're at the beginning
	if startIdx == 0 {
		rootAttr := getRootAttr()
		if !out.AddDirEntry(fuse.DirEntry{
			Name: ".",
			Mode: uint32(syscall.S_IFDIR | 0755),
			Ino:  fuse.FUSE_ROOT_ID,
		}) {
			// Buffer is full
			return fuse.OK
		}
		startIdx++
	}

	if startIdx == 1 {
		if !out.AddDirEntry(fuse.DirEntry{
			Name: "..",
			Mode: uint32(syscall.S_IFDIR | 0755),
			Ino:  fuse.FUSE_ROOT_ID, // Parent of root is root
		}) {
			// Buffer is full
			return fuse.OK
		}
		startIdx++
	}

	// Now add file entries
	filesLock.RLock()
	defer filesLock.RUnlock()

	idx := 2 // We've already processed . and ..
	for filename := range sourceFiles {
		// Skip entries before the requested offset
		if idx < startIdx {
			idx++
			continue
		}

		// Get inode for this file
		inode, exists := inodeMap[filename]
		if !exists {
			logger.Warn().
				Str("filename", filename).
				Msg("ReadDir: no inode found for file")
			idx++
			continue
		}

		// Get file attributes
		attr := getFileAttr(filename)

		// Add this entry to the result
		if !out.AddDirEntry(fuse.DirEntry{
			Name: filename,
			Mode: uint32(syscall.S_IFREG | 0444), // Regular file, read-only
			Ino:  inode,
		}) {
			// The buffer is full, but we were able to add some entries.
			// We'll return OK and let the kernel call us again with a new offset.
			return fuse.OK
		}

		logger.Debug().
			Str("filename", filename).
			Uint64("inode", inode).
			Msg("ReadDir: added entry")

		idx++
	}

	return fuse.OK
}

func main() {
	// Parse command line arguments
	// configPath := flag.String("config", "", "Path to config file")
	debug := flag.Bool("debug", false, "Enable debug logging")
	flag.Parse()
	mountPoint := flag.Arg(0)

	// Initialize logger
	logLevel := util.InfoLevel
	if *debug {
		logLevel = util.DebugLevel
	}
	util.InitializeLogger(logLevel)
	logger := util.GetLogger("main")

	// Check if mount point is provided
	if mountPoint == "" {
		logger.Fatal().Msg("Mount point not specified; it must be passed as the argument")
	}

	// Create filesystem
	fs := NewHttpFS()
	opts := &fuse.MountOptions{
		FsName: "httpfs",
		Name:   "httpfs",
		Debug:  *debug,
		Logger: log.Default(),
	}

	// Create server
	server, err := fuse.NewServer(fs, mountPoint, opts)
	if err != nil {
		logger.Fatal().Err(err).Msg("Failed to create FUSE server")
	}

	// Setup signal handling for graceful shutdown
	signalChan := make(chan os.Signal, 1)
	signal.Notify(signalChan, syscall.SIGINT, syscall.SIGTERM)

	// Start server in the background
	go server.Serve()

	// Wait until the filesystem is mounted
	if err := server.WaitMount(); err != nil {
		logger.Fatal().Err(err).Msg("Failed to mount filesystem")
	}

	logger.Info().Str("mountpoint", mountPoint).Msg("Filesystem mounted successfully")

	// Wait for termination signal
	sig := <-signalChan
	logger.Info().Str("signal", sig.String()).Msg("Received signal, unmounting filesystem")

	// Unmount the filesystem
	if err := server.Unmount(); err != nil {
		logger.Error().Err(err).Msg("Failed to unmount filesystem")
	}

	logger.Info().Msg("Filesystem unmounted successfully")
}
