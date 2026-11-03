"""Cross-process exclusion between model execution and temporary cookie-recovery Chrome."""
from contextlib import contextmanager
import fcntl
import os


@contextmanager
def slot(db_path):
    path=db_path.with_name(db_path.name+'.runtime.lock')
    fd=os.open(path,os.O_CREAT|os.O_RDWR,0o600)
    acquired=False
    try:
        try:
            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            acquired=True
        except BlockingIOError:
            pass
        yield acquired
    finally:
        if acquired:fcntl.flock(fd,fcntl.LOCK_UN)
        os.close(fd)
