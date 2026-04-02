"""Cross-platform single-instance process lock."""

from __future__ import annotations

from pathlib import Path


class SingleInstanceLock:
    """Best-effort cross-platform file lock for single bot instance."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._handle = None
        self._locked = False

    def _ensure_handle(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self._handle:
            self._handle = open(self.path, "a+")
            self._handle.seek(0)
            if not self._handle.read(1):
                self._handle.seek(0)
                self._handle.write("0")
                self._handle.flush()
                self._handle.seek(0)

    def acquire(self) -> bool:
        if self._locked:
            return True

        self._ensure_handle()
        try:
            try:
                import msvcrt

                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
            except ImportError:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False

        self._locked = True
        return True

    def release(self):
        if not self._handle or not self._locked:
            return
        try:
            try:
                import msvcrt

                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            except ImportError:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            self._locked = False
            self._handle.close()
            self._handle = None
