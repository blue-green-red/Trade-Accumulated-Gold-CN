"""Cross-process exclusion between model execution and temporary cookie-recovery Chrome."""
from contextlib import contextmanager
import fcntl
import os
import json
import socket
from datetime import datetime,timezone


def _take(fd):
    try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB);return True
    except BlockingIOError:return False


def _legacy_interactive(db_path):
    """Also recognize a viewer started before interactive locking was deployed."""
    try:
        value=json.loads((db_path.parent/'jd-remote-browser/status.json').read_text())
        age=(datetime.now(timezone.utc)-datetime.fromisoformat(value['checked_at'])).total_seconds()
        if value.get('status') not in ('starting','ready') or not 0<=age<90:return False
        with socket.create_connection(('127.0.0.1',9225),timeout=.15):return True
    except (OSError,ValueError,KeyError,TypeError):return False


@contextmanager
def slot(db_path,*,model=False):
    path=db_path.with_name(db_path.name+'.runtime.lock')
    fd=os.open(path,os.O_CREAT|os.O_RDWR,0o600)
    acquired=False
    try:
        acquired=_take(fd)
        if acquired and model:
            guard=os.open(str(path)+'.interactive',os.O_CREAT|os.O_RDWR,0o600)
            try:
                if not _take(guard) or _legacy_interactive(db_path):
                    fcntl.flock(fd,fcntl.LOCK_UN);acquired=False
            finally:os.close(guard)
        yield acquired
    finally:
        if acquired:fcntl.flock(fd,fcntl.LOCK_UN)
        os.close(fd)


@contextmanager
def interactive_slot(db_path,*,hold_startup=False):
    """Exclude models for the viewer lifetime; allow lightweight cookie attachment."""
    guard=None;acquired=False;startup=slot(db_path);startup_open=False
    def release_startup():
        nonlocal startup_open
        if startup_open:
            startup.__exit__(None,None,None);startup_open=False
    try:
        free=startup.__enter__();startup_open=True
        if free:
            path=db_path.with_name(db_path.name+'.runtime.lock.interactive')
            guard=os.open(path,os.O_CREAT|os.O_RDWR,0o600)
            acquired=_take(guard)
        if not hold_startup or not acquired:release_startup()
        yield release_startup if hold_startup and acquired else acquired
    finally:
        release_startup()
        if guard is not None:
            if acquired:fcntl.flock(guard,fcntl.LOCK_UN)
            os.close(guard)
