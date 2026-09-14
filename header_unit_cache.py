"""Per-header leases for the CMake bridge's shared header-unit cache."""

from contextlib import ExitStack
import fcntl
import hashlib
import random
import time
from pathlib import Path


class HeaderUnitBusy(Exception):
    """Request a fresh build attempt after releasing all compiler and file leases."""

    def __init__(self, path: Path) -> None:
        """Record the contended lock path for waiting after the attempt unwinds."""
        self.path = path
        super().__init__(f'Waiting for shared header unit: {path.name}')

    def wait(self) -> None:
        """Wait outside all build locks and compiler slots, without reserving the unit."""
        with self.path.open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
        # Desynchronize opposing retry chains so they do not repeatedly collide.
        time.sleep(random.uniform(0.005, 0.05))


class HeaderUnitLocks:
    def __init__(self, directory: Path, stack: ExitStack) -> None:
        """Keep leases in stack until all compilers in this build attempt have stopped."""
        self.directory = directory
        self.stack = stack
        self.held: set[Path] = set()

    def acquire(self, header: Path) -> None:
        """Lease canonical header without blocking; contention unwinds the whole attempt."""
        header = header.resolve()
        if header in self.held:
            return
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / (hashlib.sha256(str(header).encode()).hexdigest() + '.lock')
        lock = path.open('a')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close()
            raise HeaderUnitBusy(path) from None
        except BaseException:
            lock.close()
            raise
        self.stack.enter_context(lock)
        self.held.add(header)
