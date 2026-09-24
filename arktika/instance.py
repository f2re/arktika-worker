"""Advisory process lock, Windows and POSIX. Taken before opening SQLite."""
import json
import os
from pathlib import Path

class InstanceLock:
    def __init__(self,root):
        self.root=Path(root).expanduser().resolve();self.root.mkdir(parents=True,exist_ok=True)
        self.handle=None;self.acquired=False
    def acquire(self):
        self.handle=(self.root/'instance.lock').open('a+b')
        self.handle.seek(0,os.SEEK_END)
        if self.handle.tell()==0:self.handle.write(b'0');self.handle.flush()
        self.handle.seek(0)
        try:
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(self.handle.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except (OSError,IOError):
            self.handle.close();self.handle=None;return False
        self.acquired=True;return True
    def publish(self,url):
        with (self.root/'server.json').open('w',encoding='utf-8') as f:
            json.dump({'pid':os.getpid(),'url':url},f)
        if os.name!='nt':os.chmod(self.root/'server.json',0o600)
    def read_url(self):
        try:return json.loads((self.root/'server.json').read_text(encoding='utf-8'))['url']
        except (OSError,ValueError,KeyError):return ''
    def close(self):
        if not self.handle:return
        if self.acquired:
            try:(self.root/'server.json').unlink()
            except OSError:pass
            self.handle.seek(0)
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(self.handle.fileno(),msvcrt.LK_UNLCK,1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(),fcntl.LOCK_UN)
        self.handle.close();self.handle=None;self.acquired=False
