"""Track pipeline subprocesses so deadlines and helper shutdown reap their children."""
import os
import signal
import subprocess
import threading

_ACTIVE = set()
_LOCK = threading.Lock()
_STOPPING = False


def spawn(cmd, **kwargs):
    with _LOCK:
        if _STOPPING:
            raise RuntimeError('helper is shutting down')
        proc = subprocess.Popen(cmd, start_new_session=True, **kwargs)
        _ACTIVE.add(proc)
        return proc


def kill(proc):
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def release(proc):
    with _LOCK:
        _ACTIVE.discard(proc)


def stop_all():
    global _STOPPING
    with _LOCK:
        _STOPPING = True
        for proc in _ACTIVE:
            kill(proc)


def run(cmd, *, input=None, timeout=None, **kwargs):
    if input is not None:
        kwargs['stdin'] = subprocess.PIPE
    proc = spawn(cmd, **kwargs)
    try:
        try:
            stdout, stderr = proc.communicate(input, timeout=timeout)
        except BaseException:
            kill(proc)
            proc.communicate()
            raise
        return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)
    finally:
        release(proc)
