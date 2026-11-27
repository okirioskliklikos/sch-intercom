"""Ένα αντίγραφο ανά χρήστη/εγκατάσταση (Φάση 5A): κλείδωμα αρχείου.

Το λειτουργικό απελευθερώνει το κλείδωμα όταν τερματιστεί η διεργασία, ακόμη και
μετά από kill, άρα δεν μένει «κολλημένο» κλείδωμα. Στα Windows η απελευθέρωση
γίνεται λίγο μετά τον τερματισμό (ασύγχρονα).
"""
from __future__ import annotations

import os
from pathlib import Path


class AlreadyRunning(Exception):
    pass


class InstanceLock:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._fd: int | None = None

    def acquire(self) -> "InstanceLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            if os.name == "nt":
                import msvcrt
                if os.fstat(fd).st_size == 0:
                    os.write(fd, b"0")
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            raise AlreadyRunning(str(self.path)) from exc
        self._fd = fd
        return self

    def release(self) -> None:
        if self._fd is None:
            return
        if os.name == "nt":
            try:
                import msvcrt
                os.lseek(self._fd, 0, os.SEEK_SET)
                msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        os.close(self._fd)
        self._fd = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc):
        self.release()


def is_locked(path: Path) -> bool:
    """True αν κάποια άλλη διεργασία κρατά το κλείδωμα."""
    if not Path(path).exists():
        return False
    try:
        InstanceLock(path).acquire().release()
    except AlreadyRunning:
        return True
    return False
