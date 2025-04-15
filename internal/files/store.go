package files

import "sync"

type FileStore struct {
	sourceFiles map[string]string // filename -> url mapping
	inodeMap    map[string]uint64 // filename -> inode mapping
	nextInode   uint64            // Next inode number to assign
	lock        sync.RWMutex      // Protects the above fields
}

func (fs *FileStore) GetURL(filename string) (string, bool) {
	fs.lock.RLock()
	defer fs.lock.RUnlock()
	url, exists := fs.sourceFiles[filename]
	return url, exists
}

func (fs *FileStore) AssignInode(filename string) uint64 {
	fs.lock.Lock()
	defer fs.lock.Unlock()

	if inode, exists := fs.inodeMap[filename]; exists {
		return inode
	}

	inode := fs.nextInode
	fs.inodeMap[filename] = inode
	fs.nextInode++
	return inode
}
