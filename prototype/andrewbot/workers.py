"""One bounded wake signal per worker; durable work stays in SQLite."""
from datetime import datetime, timezone
import logging
import threading
import time


class ModelWorker:
    def __init__(self, action, should_stop):
        self.action = action
        self.should_stop = should_stop
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread = None
        self._active_since = None
        self._last_completion_at = None
        self._failures = 0

    def start(self):
        with self._lock:
            if self._thread is not None:
                raise RuntimeError('Worker already started')
            self._thread = threading.Thread(target=self._run, name='andrew-work', daemon=True)
            self._thread.start()

    def wake(self):
        if not self._stop.is_set():
            self._ready.set()

    def stop(self, timeout=2):
        self._stop.set()
        self._ready.set()
        if self._thread is not None:
            self._thread.join(timeout=max(0, timeout))
        return self._thread is None or not self._thread.is_alive()

    def snapshot(self):
        with self._lock:
            alive = self._thread is not None and self._thread.is_alive()
            state = ('stopping' if alive else 'stopped') if self._stop.is_set() else (
                'running' if self._active_since is not None else 'idle')
            return {'state': state,
                    'active_seconds': None if self._active_since is None else max(0, time.monotonic() - self._active_since),
                    'last_completion_at': self._last_completion_at, 'failures': self._failures}

    def _run(self):
        while not self._stop.is_set() and not self.should_stop():
            if not self._ready.wait(1):
                continue
            self._ready.clear()
            if self._stop.is_set() or self.should_stop():
                break
            with self._lock:
                self._active_since = time.monotonic()
            succeeded = False
            try:
                self.action()
                succeeded = True
            except Exception:
                with self._lock:
                    self._failures += 1
                logging.warning('Background worker action failed')
            finally:
                with self._lock:
                    self._active_since = None
                    if succeeded:
                        self._last_completion_at = datetime.now(timezone.utc).isoformat()
