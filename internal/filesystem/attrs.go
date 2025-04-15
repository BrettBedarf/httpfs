package filesystem

import (
	"os"
	"syscall"
	"time"

	"github.com/hanwen/go-fuse/v2/fuse"
)

func getRootAttr() *fuse.Attr {
	now := time.Now()
	return &fuse.Attr{
		Ino:       fuse.FUSE_ROOT_ID,
		Size:      0,
		Blocks:    0,
		Atime:     uint64(now.Unix()),
		Mtime:     uint64(now.Unix()),
		Ctime:     uint64(now.Unix()),
		Atimensec: uint32(now.Nanosecond()),
		Mtimensec: uint32(now.Nanosecond()),
		Ctimensec: uint32(now.Nanosecond()),
		Mode:      uint32(syscall.S_IFDIR | 0755), // directory with rwxr-xr-x permissions
		Nlink:     2,
		Owner: fuse.Owner{
			Uid: uint32(os.Getuid()),
			Gid: uint32(os.Getgid()),
		},
		Rdev:    0,
		Blksize: 4096, // preferred size for fs ops
		Padding: 0,    // TODO: what is this?
	}
}

func getFileAttr(filename string) *fuse.Attr {
	now := time.Now()
	inode := inodeMap[filename]

	return &fuse.Attr{
		Ino:       inode,
		Size:      0, // You'll need to get this from metadata/HTTP HEAD
		Blocks:    0,
		Atime:     uint64(now.Unix()),
		Mtime:     uint64(now.Unix()),
		Ctime:     uint64(now.Unix()),
		Atimensec: uint32(now.Nanosecond()),
		Mtimensec: uint32(now.Nanosecond()),
		Ctimensec: uint32(now.Nanosecond()),
		Mode:      uint32(syscall.S_IFREG | 0444), // regular file with r--r--r-- permissions
		Nlink:     1,
		Uid:       uint32(os.Getuid()),
		Gid:       uint32(os.Getgid()),
		Rdev:      0,
		Blksize:   4096,
	}
}
