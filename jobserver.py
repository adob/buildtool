"""Nonblocking GNU Make jobserver client for FIFO and Linux pipe transports."""

from collections.abc import Mapping
import os
import shlex
import signal
import stat
import warnings


def make_suppresses_execution(environment: Mapping[str, str] | None = None) -> bool:
    """Check Make's dry-run/touch/question modes before executing a '+' recipe."""
    environment = os.environ if environment is None else environment
    flags = shlex.split(environment.get('MAKEFLAGS', ''))
    # Make emits no-argument short options as the initial word without a dash.
    if flags and not flags[0].startswith('-') and '=' not in flags[0]:
        if any(option in flags[0] for option in 'ntq'):
            return True
    return any(flag in ('-n', '-t', '-q', '--just-print', '--dry-run', '--recon', '--touch', '--question')
               for flag in flags)


def advertised_jobs(flags: list[str]) -> int | None:
    """Read the last -j/--jobs setting from parsed MAKEFLAGS; None means unspecified/unlimited."""
    jobs = None
    for index, flag in enumerate(flags):
        if flag in ('-j', '--jobs'):
            value = flags[index + 1] if index + 1 < len(flags) else ''
        elif flag.startswith('--jobs='):
            value = flag.removeprefix('--jobs=')
        elif flag.startswith('-j'):
            value = flag[2:]
        else:
            continue
        jobs = int(value) if value.isascii() and value.isdecimal() and int(value) > 0 else None
    return jobs


class JobServer:
    def __init__(self, reader: int, writer: int, jobs: int | None = None) -> None:
        """Own reader/writer; jobs is Make's advertised limit, with tokens authoritative."""
        self.reader = reader
        self.writer = writer
        self.jobs = jobs
        self.active = 0
        self.tokens: list[bytes] = []

    @classmethod
    def from_environment(cls, environment: Mapping[str, str] | None = None) -> 'JobServer | None':
        """Connect to the last MAKEFLAGS authorization in environment, or return None."""
        environment = os.environ if environment is None else environment
        reader = writer = None
        try:
            flags = shlex.split(environment.get('MAKEFLAGS', ''))
            authorizations = [flag.split('=', 1)[1] for flag in flags
                              if flag.startswith(('--jobserver-auth=', '--jobserver-fds='))]
            if not authorizations:
                return None
            auth = authorizations[-1]
            if auth.startswith('fifo:'):
                reader = os.open(auth[5:], os.O_RDONLY | os.O_NONBLOCK)
                writer = os.open(auth[5:], os.O_WRONLY | os.O_NONBLOCK)
            else:
                read_fd, write_fd = map(int, auth.split(','))
                if read_fd < 0 or write_fd < 0:
                    return None
                if not stat.S_ISFIFO(os.fstat(read_fd).st_mode):
                    raise ValueError('jobserver read descriptor is not a pipe')
                # dup() shares status flags with Make. Reopening via procfs gives
                # an independent description, so O_NONBLOCK cannot affect Make.
                reader = os.open(f'/proc/self/fd/{read_fd}', os.O_RDONLY | os.O_NONBLOCK)
                writer = os.dup(write_fd)
            if not all(stat.S_ISFIFO(os.fstat(fd).st_mode) for fd in (reader, writer)):
                raise ValueError('jobserver descriptors must refer to pipes')
            return cls(reader, writer, advertised_jobs(flags))
        except (OSError, ValueError) as error:
            for fd in (reader, writer):
                if fd is not None:
                    os.close(fd)
            warnings.warn(f'Jobserver unavailable; using one compiler: {error}', stacklevel=2)
            return None

    def try_acquire(self) -> bool:
        """Reserve the implicit slot or borrow one exact token without blocking."""
        # A terminating signal must not land between reading a token and recording
        # ownership; cleanup would otherwise have no record of the borrowed byte.
        previous = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT, signal.SIGTERM})
        try:
            if self.active:
                try:
                    token = os.read(self.reader, 1)
                except BlockingIOError:
                    return False
                if not token:
                    return False
                self.tokens.append(token)
            self.active += 1
            return True
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous)

    def release(self) -> None:
        """Release an active slot, returning borrowed tokens but never the implicit slot."""
        previous = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT, signal.SIGTERM})
        try:
            if self.tokens:
                os.write(self.writer, self.tokens[-1])
                self.tokens.pop()
            self.active -= 1
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous)

    def close(self) -> None:
        """Return outstanding tokens and close owned descriptors after compiler cleanup."""
        if self.reader < 0:
            return
        while self.active:
            self.release()
        os.close(self.reader)
        os.close(self.writer)
        self.reader = self.writer = -1
