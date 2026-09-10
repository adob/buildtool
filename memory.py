"""Available-memory estimates used to throttle compiler launches."""

from collections.abc import Callable
from pathlib import Path, PurePosixPath


GIB = 1024 ** 3
DEFAULT_JOB_MEMORY = 2 * GIB


def read_system_file(path: str) -> str:
    """Read kernel accounting text from path; callers handle unavailable files."""
    return Path(path).read_text()


def available_memory_bytes(read_text: Callable[[str], str] = read_system_file) -> int | None:
    """Read Linux available RAM and readable cgroup-v2 limits using read_text.

    Inspect ancestors under the standard cgroup mount as well as the current
    group. Return None if no usable accounting is available.
    """
    limits = []
    try:
        for line in read_text('/proc/meminfo').splitlines():
            fields = line.split()
            if fields and fields[0] == 'MemAvailable:':
                limits.append(max(0, int(fields[1]) * 1024))
                break
    except (OSError, ValueError, IndexError):
        pass

    try:
        groups = read_text('/proc/self/cgroup').splitlines()
    except OSError:
        groups = []
    for group in groups:
        if not group.startswith('0::/'):
            continue
        relative = PurePosixPath(group[4:])
        if '..' in relative.parts:
            continue
        # The mount root also covers cgroup namespaces where membership is '/'.
        for directory in (relative, *relative.parents):
            base = PurePosixPath('/sys/fs/cgroup') / directory
            try:
                maximum = read_text(str(base / 'memory.max')).strip()
                if maximum == 'max':
                    continue
                current = int(read_text(str(base / 'memory.current')))
                limits.append(max(0, int(maximum) - current))
            except (OSError, ValueError):
                continue
    return min(limits) if limits else None


class MemoryBudget:
    def __init__(
        self,
        available: Callable[[], int | None] = available_memory_bytes,
        bytes_per_job: int = DEFAULT_JOB_MEMORY,
    ) -> None:
        """Estimate each job at bytes_per_job; available supplies current free capacity."""
        if bytes_per_job <= 0:
            raise ValueError('bytes_per_job must be positive')
        self.available = available
        self.bytes_per_job = bytes_per_job

    def job_limit(self, requested: int, available: int | None) -> int:
        """Cap requested jobs by a memory snapshot, keeping at least one runnable."""
        if available is None:
            return requested
        return min(requested, max(1, available // self.bytes_per_job))

    def permits(self, active: int) -> bool:
        """Allow one more compiler after active, conservatively budgeting peak growth."""
        if active == 0:
            # Blocked importers retain RAM. Permit one compiler to make progress
            # rather than waiting forever for memory only that build can release.
            return True
        available = self.available()
        return available is None or available >= (active + 1) * self.bytes_per_job
