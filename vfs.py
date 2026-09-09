"""Filesystem operations used by buildtool, with real and in-memory backends."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
import errno
import hashlib
import os
from pathlib import Path
import stat


@dataclass(frozen=True)
class DirectoryEntry:
    path: str
    name: str
    is_file: bool
    is_dir: bool
    is_symlink: bool = False


class FileSystem(ABC):
    @abstractmethod
    def stat(self, path) -> os.stat_result:
        pass

    @abstractmethod
    def read_bytes(self, path) -> bytes:
        pass

    @abstractmethod
    def write_bytes(self, path, data: bytes):
        pass

    @abstractmethod
    def makedirs(self, path, exist_ok=False):
        pass

    @abstractmethod
    def replace(self, source, destination):
        """Atomically replace a file with another file on the same filesystem."""
        pass

    @abstractmethod
    def unlink(self, path):
        pass

    @abstractmethod
    def symlink(self, target, path):
        """Create a symbolic link, preserving the target's relative spelling."""
        pass

    @abstractmethod
    def readlink(self, path) -> str:
        pass

    @abstractmethod
    def scandir(self, path) -> list[DirectoryEntry]:
        pass

    @abstractmethod
    def getcwd(self) -> str:
        pass

    @abstractmethod
    def chdir(self, path):
        pass

    def abspath(self, path) -> str:
        return os.path.normpath(os.path.join(self.getcwd(), os.fspath(path)))

    def read_text(self, path) -> str:
        return self.read_bytes(path).decode("utf-8")

    def write_text(self, path, text: str):
        self.write_bytes(path, text.encode("utf-8"))

    def is_file(self, path) -> bool:
        try:
            return stat.S_ISREG(self.stat(path).st_mode)
        except (FileNotFoundError, NotADirectoryError):
            return False

    def is_dir(self, path) -> bool:
        try:
            return stat.S_ISDIR(self.stat(path).st_mode)
        except (FileNotFoundError, NotADirectoryError):
            return False

    def sha256(self, path) -> str:
        return hashlib.sha256(self.read_bytes(path)).hexdigest()


class RealFileSystem(FileSystem):
    def stat(self, path):
        return os.stat(path)

    def read_bytes(self, path):
        return Path(path).read_bytes()

    def write_bytes(self, path, data):
        Path(path).write_bytes(data)

    def makedirs(self, path, exist_ok=False):
        os.makedirs(path, exist_ok=exist_ok)

    def replace(self, source, destination):
        os.replace(source, destination)

    def unlink(self, path):
        os.unlink(path)

    def symlink(self, target, path):
        os.symlink(target, path)

    def readlink(self, path):
        return os.readlink(path)

    def scandir(self, path):
        with os.scandir(path) as entries:
            return [DirectoryEntry(
                entry.path, entry.name, entry.is_file(), entry.is_dir(),
                entry.is_symlink(),
            ) for entry in entries]

    def getcwd(self):
        return os.getcwd()

    def chdir(self, path):
        os.chdir(path)

    def sha256(self, path):
        # Header-unit PCMs can be hundreds of MB: stream rather than copy them.
        with open(path, "rb", buffering=0) as source:
            return hashlib.file_digest(source, "sha256").hexdigest()


@dataclass
class _MemoryEntry:
    data: bytes | None  # None denotes a directory when target is also None.
    mtime: float
    target: str | None = None


class MemoryFileSystem(FileSystem):
    """Files and directories with a virtual cwd and deterministic timestamps.

    Each mutation advances the clock; advance() can also simulate elapsed time.
    This backend does not emulate permissions or external processes.
    """

    def __init__(self, cwd="/workspace"):
        self._clock = 1000.0
        self._cwd = os.sep
        self._entries = {self._cwd: _MemoryEntry(None, self._clock)}
        self.makedirs(cwd, exist_ok=True)
        self.chdir(cwd)

    def advance(self, seconds=1):
        if seconds < 0:
            raise ValueError("The filesystem clock cannot move backwards")
        self._clock += seconds
        return self._clock

    def _resolve(self, path, *, follow_final=True, missing_ok=False, links=0):
        parts = self.abspath(path).split(os.sep)[1:]
        current = os.sep
        for index, part in enumerate(parts):
            if not part:
                continue
            current = os.path.join(current, part)
            entry = self._entries.get(current)
            if entry is None:
                if missing_ok:
                    return os.path.join(current, *parts[index + 1:])
                raise FileNotFoundError(current)
            if entry.target is not None and (follow_final or index < len(parts) - 1):
                if links >= 40:
                    raise OSError(errno.ELOOP, "Too many symbolic links", current)
                target = os.path.join(os.path.dirname(current), entry.target, *parts[index + 1:])
                return self._resolve(target, follow_final=follow_final,
                                     missing_ok=missing_ok, links=links + 1)
            if index < len(parts) - 1 and entry.data is not None:
                raise NotADirectoryError(current)
        return current

    def _entry(self, path):
        return self._entries[self._resolve(path)]

    def _require_directory(self, path):
        if self._entry(path).data is not None:
            raise NotADirectoryError(os.fspath(path))

    def stat(self, path):
        entry = self._entry(path)
        mode = stat.S_IFDIR | 0o755 if entry.data is None else stat.S_IFREG | 0o644
        size = 0 if entry.data is None else len(entry.data)
        return os.stat_result((mode, 0, 0, 1, 0, 0, size,
                               entry.mtime, entry.mtime, entry.mtime))

    def read_bytes(self, path):
        entry = self._entry(path)
        if entry.data is None:
            raise IsADirectoryError(os.fspath(path))
        return entry.data

    def write_bytes(self, path, data):
        path = self._resolve(path, missing_ok=True)
        self._require_directory(os.path.dirname(path))
        if self.is_dir(path):
            raise IsADirectoryError(path)
        self._entries[path] = _MemoryEntry(bytes(data), self.advance())

    def makedirs(self, path, exist_ok=False):
        path = self._resolve(path, missing_ok=True)
        if path in self._entries:
            if not exist_ok or self._entries[path].data is not None:
                raise FileExistsError(path)
            return
        parent = os.path.dirname(path)
        if self.is_file(parent):
            raise NotADirectoryError(parent)
        self.makedirs(parent, exist_ok=True)
        self._entries[path] = _MemoryEntry(None, self.advance())

    def replace(self, source, destination):
        source = self._resolve(source, follow_final=False)
        destination = self._resolve(destination, follow_final=False, missing_ok=True)
        entry = self._entries[source]
        self._require_directory(os.path.dirname(destination))
        dest_entry = self._entries.get(destination)
        if (entry.data is None and entry.target is None) or (
            dest_entry is not None and dest_entry.data is None and dest_entry.target is None
        ):
            raise IsADirectoryError("replace() supports files and symbolic links only")
        if source != destination:
            self._entries[destination] = entry
            del self._entries[source]
            self.advance()

    def unlink(self, path):
        path = self._resolve(path, follow_final=False)
        entry = self._entries[path]
        if entry.data is None and entry.target is None:
            raise IsADirectoryError(path)
        del self._entries[path]
        self.advance()

    def symlink(self, target, path):
        path = self._resolve(path, follow_final=False, missing_ok=True)
        self._require_directory(os.path.dirname(path))
        if path in self._entries:
            raise FileExistsError(path)
        self._entries[path] = _MemoryEntry(None, self.advance(), os.fspath(target))

    def readlink(self, path):
        path = self._resolve(path, follow_final=False)
        target = self._entries[path].target
        if target is None:
            raise OSError(errno.EINVAL, "Not a symbolic link", path)
        return target

    def scandir(self, path):
        self._require_directory(path)
        absolute = self._resolve(path)
        return [DirectoryEntry(
            os.path.join(os.fspath(path), os.path.basename(name)),
            os.path.basename(name), self.is_file(name), self.is_dir(name),
            entry.target is not None,
        ) for name, entry in sorted(self._entries.items())
                if name != absolute and os.path.dirname(name) == absolute]

    def getcwd(self):
        return self._cwd

    def chdir(self, path):
        self._require_directory(path)
        self._cwd = self._resolve(path)
