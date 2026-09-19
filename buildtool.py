#!/usr/bin/env python3

from __future__ import annotations
import os
import asyncio
import json
import hashlib
import subprocess
import shlex
import argparse
import copy
import sys
import re
from datetime import datetime
import time
from collections.abc import Iterable, Iterator, Sequence
from types import NotImplementedType
from typing import Any
import pathlib
from enum import Enum, StrEnum
from dataclasses import dataclass
from uuid import uuid4

if __package__:
    from .vfs import FileSystem, RealFileSystem, MemoryFileSystem
    from .clang_mapper import compile_with_mapper_async
    from .compiler import mapper_pipe, run_compiler
    from .scheduler import BuildSession, ConcurrencyReporter, Job
    from .memory import MemoryBudget
    from .jobserver import JobServer
    from .gcc_std import GccStdHeaders, GccStdModules, header_unit_flags
else:
    from vfs import FileSystem, RealFileSystem, MemoryFileSystem
    from clang_mapper import compile_with_mapper_async
    from compiler import mapper_pipe, run_compiler
    from scheduler import BuildSession, ConcurrencyReporter, Job
    from memory import MemoryBudget
    from jobserver import JobServer
    from gcc_std import GccStdHeaders, GccStdModules, header_unit_flags

_DEFAULT_VFS = RealFileSystem()


ROOT = os.path.dirname(os.path.realpath(sys.argv[0]))

DEBUG_LOG = False

VCPKG_INCLUDE_RE = r"^vcpkg\/installed\/[a-z0-9-]+\/include\/([^\/]+)\/"


COMPILE_FLAGS = ["-pthread", "-fnon-call-exceptions", "-g",
            "-Wall", "-Wextra", "-Wconversion", 
            "-Wno-sign-compare", "-Wno-deprecated", "-Wno-sign-conversion",
            "-Wno-missing-field-initializers",
            "-Werror=shift-count-overflow",
            "-Wno-unused-parameter",
            "-Wno-parentheses",
            "-Werror=return-type",
]
CFLAGS = COMPILE_FLAGS
CLANG_CFLAGS = ["-Wno-logical-op-parentheses"]
CXXFLAGS = COMPILE_FLAGS + ["-std=c++26"]
LDFLAGS = ["-lrt"]
OBJDIR = "build"
DEPDIR = "build"
SUFFIX = ""

SRCDIR = "."
SRC_ROOTS = [SRCDIR]
BINDIR = "bin"
INCFLAGS = []
USECLANG = False

CLANG_PATH = ""
CLANG   = "clang"
CLANGXX = "clang++"
CLANG_SCAND_DEPS = "clang-scan-deps"

CXX = "g++"
CC = "gcc"

# TODO: Fix the build system to avoid needing to hardcode this path.
TESTMAIN = "third_party/baselib/lib/testing/testmain.cc"
BENCHMAIN = "third_party/baselib/lib/testing/benchmain.cc"

class Release:
    CFLAGS = CFLAGS + ["-O2", "-mtune=native", 
                         #"-march=native", 
                         "-mcx16"]
    LFLAGS  = LDFLAGS + ["-fwhole-program", "-O2", "-mtune=native"]
    OBJDIR  = OBJDIR + "/release"
    DEPDIR  = DEPDIR + "/release"
    LDFLAGS = LDFLAGS + ["-O2"]


class Debug:
    CFLAGS = CFLAGS + [
        "-fsanitize=address", 
        #"-fsanitize=thread", 
        "-fsanitize=undefined",
        "-mcx16"]
    OBJDIR  = OBJDIR + "/debug"
    DEPDIR  = DEPDIR + "/debug"
    SUFFIX  = "+debug"

## =========================================================== ##

CCFILE_SUFFIXES = ('.cc', '.cpp')
HFILE_SUFFIXES  = ('.h', '.hpp', '.hh')

def native_tags() -> frozenset[str]:
    """Return default native platform tags; explicit configurations replace this set."""
    name = re.sub(r'\d+$', '', sys.platform)
    name = {'win': 'windows', 'sunos': 'solaris'}.get(name, name)
    tags = {name}
    if name in ('aix', 'android', 'cygwin', 'darwin', 'dragonfly', 'freebsd',
                'illumos', 'ios', 'linux', 'netbsd', 'openbsd', 'solaris'):
        tags.add('posix')
    return frozenset(tags)


def validate_tags(tags: Iterable[str]) -> frozenset[str]:
    """Normalize tag names and reject empty or filename-unsafe identifiers."""
    if isinstance(tags, str):
        raise ValueError('Tags must be a collection of names, not a string')
    result = frozenset(tags)
    for tag in sorted(result):
        if not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]*', tag):
            raise ValueError(f'Invalid build tag: {tag!r}')
    return result


def is_test_source(path: Path) -> bool:
    """Recognize name_test+tag.cc/cpp as well as unqualified test filenames."""
    return path.suffix in CCFILE_SUFFIXES and path.stem.split('+', 1)[0].endswith('_test')


def source_matches_target(path: Path, cfg: BuildConfig) -> bool:
    """Require every +tag in a source filename to be active in cfg; underscores are ordinary text."""
    if path.suffix not in (*CCFILE_SUFFIXES, '.c', '.S', '.s'):
        return True
    tags = validate_tags(path.stem.split('+')[1:])
    if cfg.KNOWN_TAGS is not None and (unknown := tags - cfg.KNOWN_TAGS):
        raise ValueError(f'Unknown build tags in {path}: {", ".join(sorted(unknown))}')
    return tags <= cfg.TAGS


def tagged_partition_path(modname: str | None) -> Path | None:
    """Map a module partition to a +tag variant of its primary source.

    For example, ``lib.sync.cond:teensy`` maps to
    ``lib/sync/cond+teensy.cc`` and ``lib.sync.cond:teensy.debug`` maps to
    ``lib/sync/cond+teensy+debug.cc``.  This is an alternate lookup layout;
    the conventional partition layout remains preferred.
    """
    if not modname or ':' not in modname:
        return None
    module, partition = modname.split(':', 1)
    if not partition:
        return None
    return Path(module.replace('.', '/') + '+' + partition.replace('.', '+') + '.cc')


def module_partition_selects_variant(path: Path, modname: str | None) -> bool:
    """Return whether path is explicitly selected by modname's partition.

    A dotted module partition is mapped to filename tags, for example
    ``lib.sync:mutex.linux`` resolves to ``lib/sync/mutex+linux.cc``.  Such an
    explicit import is authoritative even when ``linux`` is not an active
    Buildtool source tag; source tags continue to filter automatic/root source
    selection only.  A tagged-primary fallback such as
    ``lib.sync.cond:teensy`` -> ``lib/sync/cond+teensy.cc`` is also explicit.
    """
    if not modname or ':' not in modname:
        return False
    _, partition = modname.split(':', 1)
    candidates = []
    if '.' in partition:
        candidates.append(mod2path(modname, SourceType.MODULE))
    tagged = tagged_partition_path(modname)
    if tagged is not None:
        candidates.append(tagged)
    return any(len(path.parts) >= len(candidate.parts)
               and path.parts[-len(candidate.parts):] == candidate.parts
               for candidate in candidates)

THIS_MTIME = 0

# Generate the representation while retaining custom initialization and identity.
@dataclass(init=False, eq=False)
class BuildConfig:
    USE_DIRECTORY_CONFIG: bool
    ABSOLUTE_MODULE_PATHS: bool
    jobserver: JobServer | None
    vfs: FileSystem
    CC: str
    CXX: str
    CFLAGS: list[str]
    CXXFLAGS: list[str]
    LDFLAGS: list[str]
    OBJDIR: Path
    DEPDIR: Path
    SRCDIR: Path
    BINDIR: Path
    INCFLAGS: list[str]
    SUFFIX: str
    OUTFILE: str | None
    USECLANG: bool
    CLANG_WRAPPER: str | None
    JOBS: int
    REBUILD: bool
    VERBOSE: bool
    STD_HEADER_UNIT: bool
    TAGS: frozenset[str]
    KNOWN_TAGS: frozenset[str] | None
    memory: MemoryBudget | None
    concurrency_reporter: ConcurrencyReporter
    source_files: dict[Path, SourceFile]
    compiled_modules: dict[str, CompiledModule]
    directory_configs: dict[Path, DirectoryConfig]
    header_deps: dict[Path, HeaderDep]
    compiler_commands: dict[SourceFile, list[str]]
    nm_paths: dict[str, str]
    nm_locks: dict[str, asyncio.Lock]
    gcc_std_headers: GccStdHeaders
    gcc_std_modules: GccStdModules
    std_header_sources: dict[tuple[Path, tuple[str, ...]], SourceFile]
    std_module_sources: dict[tuple[Path, tuple[str, ...]], SourceFile]
    generated_outputs: dict[Path, GeneratedAction]
    generated_logical_paths: dict[Path, Path]
    IDE_CXX: str | None
    IDE_CC: str | None
    IDE_CXXFLAGS: list[str] | None
    EXEC_CONFIG: BuildConfig | None

    def __init__(
        self,
        CC: str = CC,
        CXX: str = CXX,
        COMPILE_FLAGS: list[str] = [],
        CFLAGS: list[str] = Release.CFLAGS,
        CXXFLAGS: list[str] = CXXFLAGS,
        LDFLAGS: list[str] = LDFLAGS,
        OBJDIR: str | os.PathLike[str] = Release.OBJDIR,
        DEPDIR: str | os.PathLike[str] = Release.DEPDIR,
        SRCDIR: str | os.PathLike[str] = SRCDIR,
        BINDIR: str | os.PathLike[str] = BINDIR,
        INCFLAGS: list[str] = INCFLAGS,
        SUFFIX: str = '',
        OUTFILE: str | None = None,
        USECLANG: bool = False,
        CLANG_WRAPPER: str | None = None,
        JOBS: int = 1,
        REBUILD: bool = False,
        VERBOSE: bool = False,
        STD_HEADER_UNIT: bool = True,
        memory: MemoryBudget | None = None,
        vfs: FileSystem = _DEFAULT_VFS,
        TAGS: Iterable[str] | None = None,
        KNOWN_TAGS: Iterable[str] | None = None,
        USE_DIRECTORY_CONFIG: bool = True,
        ABSOLUTE_MODULE_PATHS: bool = False,
        jobserver: JobServer | None = None,
        progress: bool = False,
        IDE_CXX: str | None = None,
        IDE_CC: str | None = None,
        IDE_CXXFLAGS: list[str] | None = None,
        EXEC_CONFIG: BuildConfig | None = None,
    ) -> None:
        self.vfs = vfs
        self.jobserver = jobserver
        self.progress = progress
        self.USE_DIRECTORY_CONFIG = USE_DIRECTORY_CONFIG
        self.ABSOLUTE_MODULE_PATHS = ABSOLUTE_MODULE_PATHS
        self.TAGS = validate_tags(TAGS) if TAGS is not None else native_tags()
        self.KNOWN_TAGS = validate_tags(KNOWN_TAGS) if KNOWN_TAGS is not None else None
        if self.KNOWN_TAGS is not None and (unknown := self.TAGS - self.KNOWN_TAGS):
            raise ValueError(f'Unknown active build tags: {", ".join(sorted(unknown))}')
        self.CC = CC
        self.CXX = CXX
        self.CFLAGS = COMPILE_FLAGS + CFLAGS
        self.CXXFLAGS = (COMPILE_FLAGS
                         + (["-Wno-experimental-header-units"] if USECLANG else [])
                         + CXXFLAGS)
        self.LDFLAGS = LDFLAGS
        self.OBJDIR = Path(OBJDIR)
        self.DEPDIR = Path(DEPDIR)
        self.SRCDIR = Path(SRCDIR)
        self.BINDIR = Path(BINDIR)
        self.INCFLAGS = INCFLAGS
        self.SUFFIX = SUFFIX
        self.OUTFILE = OUTFILE
        self.USECLANG = USECLANG
        self.CLANG_WRAPPER = CLANG_WRAPPER
        self.IDE_CXX = IDE_CXX
        self.IDE_CC = IDE_CC
        self.IDE_CXXFLAGS = list(IDE_CXXFLAGS) if IDE_CXXFLAGS is not None else None
        self.EXEC_CONFIG = EXEC_CONFIG
        if JOBS < 1:
            raise ValueError("JOBS must be at least 1")
        self.JOBS = JOBS
        self.REBUILD = REBUILD
        self.VERBOSE = VERBOSE
        self.STD_HEADER_UNIT = STD_HEADER_UNIT
        self.memory = memory
        self.concurrency_reporter = ConcurrencyReporter()
        self.source_files = {}
        self.compiled_modules = {}
        self.directory_configs = {}
        self.header_deps = {}
        self.compiler_commands = {}
        self.compiler_identities: dict[str, list[str | int]] = {}
        self.nm_paths = {}
        self.nm_locks = {}
        self.gcc_std_headers = GccStdHeaders(vfs)
        self.gcc_std_modules = GccStdModules(vfs)
        self.std_header_sources = {}
        self.std_module_sources = {}
        self.generated_outputs = {}
        self.generated_logical_paths = {}
        self.compilation_database_mtime: float | None = None
        self.compilation_database_dirty = False

    def check_database_timestamp(self, path: Path, *, build_config: bool = False) -> None:
        """Flag a database refresh for path's ctime, or mtime for a BUILD.py file."""
        if self.compilation_database_mtime is None or self.compilation_database_dirty:
            return
        try:
            status = self.vfs.stat(path)
        except FileNotFoundError:
            return
        timestamp = status.st_mtime if build_config else status.st_ctime
        if timestamp > self.compilation_database_mtime:
            self.compilation_database_dirty = True

    def compiler_identity(self, compiler: str) -> list[str | int]:
        """Return compiler's canonical path and mtime, cached for this build."""
        if compiler in self.compiler_identities:
            return self.compiler_identities[compiler]

        executable = self.vfs.which(compiler)
        if executable is None:
            raise FileNotFoundError(f'Compiler {compiler!r} not found')

        path = self.vfs.realpath(executable)
        status = self.vfs.stat(path)
        identity = [path, status.st_mtime_ns]
        self.compiler_identities[compiler] = identity
        return identity

    def get_nm(self) -> str:
        """Return this configuration's cached nm path for the selected C++ compiler."""
        compiler = self.CXX
        if compiler not in self.nm_paths:
            self.nm_paths[compiler] = shell(
                compiler, '-print-prog-name=nm', verbose=self.VERBOSE).strip()
        return self.nm_paths[compiler]

    async def get_nm_async(self, job: Job) -> str:
        """Share nm discovery across targets; job supplies the lookup's slot and log."""
        compiler = self.CXX
        if compiler not in self.nm_paths:
            lock = self.nm_locks.setdefault(compiler, asyncio.Lock())
            async with lock:
                # Another target may have completed discovery while we waited.
                if compiler not in self.nm_paths:
                    self.nm_paths[compiler] = (await shell_async(
                        job, compiler, '-print-prog-name=nm')).strip()
        return self.nm_paths[compiler]

    def reset_build_state(self) -> None:
        """Clear this configuration's caches, retaining filesystem contents.

        Create new targets and sources before starting the next build.
        """
        self.concurrency_reporter = ConcurrencyReporter()
        self.source_files.clear()
        self.compiled_modules.clear()
        self.directory_configs.clear()
        self.header_deps.clear()
        self.compiler_commands.clear()
        self.compiler_identities.clear()
        self.nm_paths.clear()
        self.nm_locks.clear()
        self.gcc_std_headers.paths.clear()
        self.gcc_std_modules.sources.clear()
        self.gcc_std_modules.fingerprints.clear()
        self.gcc_std_modules.local_flags.clear()
        self.std_header_sources.clear()
        self.std_module_sources.clear()
        self.generated_outputs.clear()
        self.generated_logical_paths.clear()

class TargetType(Enum):
    EXECUTABLE = 1
    LIBRARY    = 2

class SourceType(StrEnum):
    CPP              = 'c++'
    C                = 'c'
    ASM              = 'asm'
    SYSTEM_HEADER    = 'system header'
    USER_HEADER      = 'user header'
    GENERATED_HEADER = 'generated header'
    MODULE           = 'module'

# https://stackoverflow.com/q/29850801/
BasePath = type(pathlib.Path())
class Path():
    # def __new__(cls, *paths: str):
    #     paths = [str(p) for p in paths]
        
    #     normalized = os.path.normpath('/'.join(paths))
    #     p = super(Path, cls).__new__(cls, normalized)
        
    #     if '..' in paths:
    #         print("normalized", normalized, '/'.join(paths), id(p))

    #     print("NEW", paths, normalized)
    #     return p
    
    #def __init__(self, *paths: str):
    #    print("INIT")
        
    
    def __init__(self, *paths: str | os.PathLike[str]) -> None:
        #paths = [str(p) for p in paths]
        #normalized = os.path.normpath('/'.join(paths))

        #super().__init__(*normalized) 

        # print("INIT", paths, [str(p) for p in paths])
        paths = [str(p) for p in paths]
        normalized = os.path.normpath('/'.join(paths))
        self.path = pathlib.Path(normalized)
        
        self.suffix = self.path.suffix
        self.parts = self.path.parts
        self.name = self.path.name

    @property
    def parent(self) -> Path:
        parent = self.path.parent
        if parent is None:
            return None
        
        return Path(parent)
    
    @property
    def stem(self) -> str:
        return self.path.stem

    @property
    def anchor(self) -> str:
        return self.path.anchor

    #    paths = [str(p) for p in paths]
    #    normalized = os.path.normpath('/'.join(paths))

    #    #print("__INIT__", paths)
    #    super().__init__(normalized)

    def with_extra_suffix(self, suffix: str) -> 'Path':
        return self.with_name(self.name + suffix)
    
    def try_stat(self, vfs: FileSystem) -> os.stat_result | None:
        try:
            return vfs.stat(self.path)
        except FileNotFoundError:
            return None
        
    def mtime(self, vfs: FileSystem) -> float:
        stat = self.try_stat(vfs)
        if stat is None:
            return 0
        return stat.st_mtime
    
    def exists(self, vfs: FileSystem) -> bool:
        return self.try_stat(vfs) is not None
    
    def __str__(self) -> str:
        return  str(self.path)
    
    def __truediv__(self, other: str | os.PathLike[str]) -> Path:
        if not isinstance(other, Path):
            other = Path(other)

        p = other.path
        if p.is_absolute():
            p = pathlib.Path("SYSTEM") / p.relative_to(p.anchor)

        return Path(self.path / p)
    
    def __rtruediv__(self, other: str | os.PathLike[str]) -> Path:
        if isinstance(other, Path):
            return other / self
            
        return Path(other / self.path)
    
    def relative_to(self, other: str | os.PathLike[str]) -> Path:
        if isinstance(other, Path):
            #print("relative_to", self.path, other, Path(self.path.relative_to(other.path)))
            return Path(self.path.relative_to(other.path))
        
        #print("relative_to", self.path, other, Path(self.path.relative_to(other)))
        return Path(self.path.relative_to(other))
    
    def with_suffix(self, suffix: str) -> Path:
        return Path(self.path.with_suffix(suffix))
    
    def with_name(self, name: str) -> Path:
        return Path(self.path.with_name(name))
    
    def read_text(self, vfs: FileSystem) -> str:
        return vfs.read_text(self.path)
    
    def is_dir(self, vfs: FileSystem) -> bool:
        return vfs.is_dir(self.path)
    
    def is_file(self, vfs: FileSystem) -> bool:
        return vfs.is_file(self.path)
    
    def is_absolute(self) -> bool:
        return self.path.is_absolute()
    
    def __fspath__(self) -> str:
        return self.path.__fspath__()
    
    def __eq__(self, other: object) -> bool | NotImplementedType:
        if isinstance(other, Path):
            return self.path == other.path
        
        return NotImplemented
    
    def __hash__(self) -> int:
        return hash(self.path)

class CompiledModule:
    @staticmethod
    def get(name: str, cfg: BuildConfig, type: SourceType | None = None) -> CompiledModule:
        mod = cfg.compiled_modules.get(name)
        if mod:
            return mod
        if type is None and name.startswith('/'):
            path = Path(name)
            logical = cfg.generated_logical_paths.get(path)
            if logical is not None and logical.suffix in HFILE_SUFFIXES:
                type = SourceType.USER_HEADER
        mod = CompiledModule(name, type)
        cfg.compiled_modules[name] = mod
        return mod
    
    def __init__(self, name: str, type: SourceType | None = None) -> None:
        self.name = name
        if type is not None:
            self.type = type
        elif name.startswith('/'):
            self.type = SourceType.SYSTEM_HEADER
        elif name.startswith('./'):
            self.type = SourceType.USER_HEADER
        else:
            self.type = SourceType.MODULE
        self.cmhash = None

        # self.cmfile = mod2cm(name)
        # self.cmfile_path = OBJDIR / self.cmfile

    async def build(self, target: Target, inherited_dircfg: DirectoryConfig | None, parent: Job) -> str:
        """Build this module for target using inherited_dircfg's flags.

        Runs in the importing parent job, which waits for the module job.
        """
        self.srcpath = await target.resolve_module_source(self.name, self.type, inherited_dircfg, parent)
        module_job = target.schedule_compilation_job(
            self.srcpath, type=self.type, modname=self.name,
            inherited_dircfg=inherited_dircfg, parent=parent)
        await parent.wait_for_dependency(module_job)
        self.srcfile = target.job_sources[module_job]
        if (target.cfg.USECLANG and target.cfg.CLANG_WRAPPER
                and self.type == SourceType.MODULE and not self.srcfile.std_module_variant
                and self.srcfile.clang_exported_module != self.name):
            raise RuntimeError(f'{self.srcpath} does not export module {self.name}')
        self.cmpath = self.srcfile.cmpath
        if self.srcfile.cmhash is None:
            self.srcfile.cmhash = sha256_file(self.cmpath, target.cfg.vfs)
        self.cmhash = self.srcfile.cmhash
        return self.cmhash

class CompilationGraph:
    def __init__(self, cfg: BuildConfig) -> None:
        """Own one scheduler and shared dependency records for targets using cfg."""
        self.session = BuildSession(cfg.JOBS, memory=cfg.memory, verbose=cfg.VERBOSE,
                                    concurrency_reporter=cfg.concurrency_reporter,
                                    jobserver=cfg.jobserver, progress=cfg.progress)
        self.job_sources: dict[Job, SourceFile] = {}
        self.link_events: dict[Job, list[Job | DirectoryConfig]] = {}


class Target:
    async def compile_source(self, source: SourceFile, cfg: BuildConfig) -> None:
        """Compile source using cfg; adapters may wrap compilation for publication."""
        await source.compile(self, cfg)

    def should_build_companion(self, path: Path) -> bool:
        """Return whether this target should compile the discovered companion path."""
        return True

    def __init__(self, path: Path, cfg: BuildConfig) -> None:
        self.path = path
        self.srcfiles = set()
        self.objs = []
        self.processed_files = set()
        self.configs = set()
        self.most_recent_output_mtime = 0
        self.extra_linkflags = []
        self.cfg = cfg
        self.session = None
        self.roots: list[Job] = []

    def compile(
        self,
        path: Path,
        type: SourceType | None = None,
        modname: str | None = None,
        inherited_dircfg: DirectoryConfig | None = None,
    ) -> SourceFile:
        """Build path synchronously; optional type/name/flags describe a module."""
        self.compile_many([(path, type, modname, inherited_dircfg)])
        return SourceFile.get(path, self.cfg)

    def compile_many(
        self,
        sources: Iterable[Path | tuple[Path, SourceType | None, str | None, DirectoryConfig | None]],
    ) -> None:
        """Build source paths or (path, type, module name, directory config) tuples."""
        async def run() -> None:
            """Schedule this target's roots and consume their ordered output."""
            graph = CompilationGraph(self.cfg)
            try:
                self.schedule_sources(sources, graph)
                await graph.session.finish()
                self.collect_link_inputs()
            finally:
                # Scheduling can fail before finish() takes ownership of cleanup.
                await graph.session.close()
                self.session = None

        asyncio.run(run())

    def schedule_sources(
        self,
        sources: Iterable[Path | tuple[Path, SourceType | None, str | None, DirectoryConfig | None]],
        graph: CompilationGraph,
    ) -> None:
        """Attach to graph and schedule this target's source paths or module tuples."""
        self.session = graph.session
        self.job_sources = graph.job_sources
        self.link_events = graph.link_events
        self.roots = []
        for source in sources:
            if isinstance(source, tuple):
                path, kind, name, directory = source
                self.schedule_compilation_job(path, kind, name, directory)
            else:
                self.schedule_compilation_job(source)

    async def wait_for_compilations(self, parent: Job | None = None) -> None:
        """Wait for this target's roots and dynamically discovered dependencies.

        When parent is supplied, dependency failures are routed through it so
        buffered compiler diagnostics are preserved in the parent's log.
        """
        seen: set[Job] = set()

        async def wait(job: Job) -> None:
            """Await job before visiting its completed list of discovered children."""
            if job in seen:
                return
            seen.add(job)
            if parent is None:
                await asyncio.shield(job.task)
                if job.error:
                    raise job.error
            else:
                await parent.wait_for_dependency(job)
            for child in job.children:
                await wait(child)

        for root in self.roots:
            await wait(root)

    def schedule_compilation_job(
        self,
        path: Path,
        type: SourceType | None = None,
        modname: str | None = None,
        inherited_dircfg: DirectoryConfig | None = None,
        parent: Job | None = None,
    ) -> Job:
        """Schedule path once, recording parent order and optional module settings."""
        if type is None:
            existing = self.cfg.source_files.get(path)
            if existing is not None:
                # A header companion may already have been identified by an import.
                type, modname = existing.type, existing.modname
            elif path.suffix in CCFILE_SUFFIXES:
                type = SourceType.CPP
            elif path.suffix == '.c':
                type = SourceType.C
            elif path.suffix in ('.S', '.s'):
                type = SourceType.ASM
            else:
                raise ValueError(f'unrecognized file type: {path}')
        source = SourceFile.get(path, self.cfg, type=type, modname=modname,
                                inherited_dircfg=inherited_dircfg)

        async def work(job: Job) -> None:
            """Associate source with job before building through the shared caches."""
            source.job = job
            job.diagnostic_source = str(source.path)
            await source.build(self, self.cfg)

        job = self.session.schedule(str(source.output_path), work, parent=parent)
        self.job_sources[job] = source
        self.link_events.setdefault(job, [])
        if parent is None and job not in self.roots:
            self.roots.append(job)
        elif parent is not None and job not in self.link_events[parent]:
            self.link_events[parent].append(job)
        return job

    def collect_link_inputs(self) -> None:
        """Collect objects and flags in dependency encounter order, not finish order."""
        visited = set()

        def visit(job: Job) -> None:
            """Replay job's configuration/dependency events and append its object."""
            if job in visited:
                return
            visited.add(job)
            for event in self.link_events[job]:
                if isinstance(event, DirectoryConfig):
                    self.add_config(event)
                else:
                    visit(event)
            source = self.job_sources[job]
            if source.type not in (SourceType.SYSTEM_HEADER, SourceType.USER_HEADER):
                if source.objpath not in self.objs:
                    self.objs.append(source.objpath)
            self.most_recent_output_mtime = max(
                self.most_recent_output_mtime, source.output_mtime)

        for root in self.roots:
            visit(root)

    def binary_paths(self, artifact: Path | None = None) -> tuple[Path, Path]:
        """Return internal and public binary paths, with an optional artifact override."""
        if self.cfg.OUTFILE is None:
            name = self.path.name + self.cfg.SUFFIX
        else:
            name = self.cfg.OUTFILE
        ofile = artifact if artifact is not None else self.cfg.OBJDIR / "bin" / name
        public_file = self.cfg.BINDIR / name
        return ofile, public_file

    def needs_link(self, ofile: Path) -> bool:
        """Check whether ofile predates this target's inputs or a rebuild was requested."""
        ofile_mtime = ofile.mtime(self.cfg.vfs)
        return self.cfg.REBUILD or self.most_recent_output_mtime >= ofile_mtime or THIS_MTIME > ofile_mtime

    def publish_binary(self, ofile: Path, public_file: Path) -> Path:
        """Point public_file at the completed ofile artifact and return the public path."""
        self.cfg.vfs.makedirs(public_file.parent, exist_ok=True)
        link_target = os.path.relpath(self.cfg.vfs.abspath(ofile),
                                      self.cfg.vfs.abspath(public_file.parent))
        atomic_symlink(public_file, link_target, self.cfg.vfs)
        return public_file

    def link(self, *, publish: bool = True, artifact: Path | None = None) -> Path:
        """Link to optional artifact; publish exposes the executable through bin's symlink."""
        ofile, public_file = self.binary_paths(artifact)
        if self.needs_link(ofile):
            self.cfg.vfs.makedirs(ofile.parent, exist_ok=True)
            print("LINKING", ofile)
            shell(self.cfg.CXX, *self.objs, *self.get_linkflags(), f"-o{ofile}",
                  verbose=self.cfg.VERBOSE)
        return self.publish_binary(ofile, public_file) if publish else ofile

    async def link_async(self, job: Job, artifact: Path | None = None) -> Path:
        """Link through job's slot and log into artifact; publication is handled in target order."""
        ofile, _ = self.binary_paths(artifact)
        if self.needs_link(ofile):
            self.cfg.vfs.makedirs(ofile.parent, exist_ok=True)
            job.message('LINKING', ofile)
            await run_compiler(job, [self.cfg.CXX, *self.objs, *self.get_linkflags(), f'-o{ofile}'],
                               color_diagnostics=True, compilation=False)
        return ofile

    async def defines_main_async(self, job: Job) -> bool:
        """Inspect this target's objects through job without blocking other compilations."""
        if not self.objs:
            return False
        nm = await self.cfg.get_nm_async(job)
        symbols = await shell_async(job, nm, '--defined-only', '--extern-only', '--format=posix',
                                    *self.objs)
        return self.symbols_define_main(symbols)

    def defines_main(self) -> bool:
        """Check this target's compiled objects for an externally defined main function."""
        if not self.objs:
            return False
        # Let the compiler select its toolchain's nm, including cross-toolchains.
        nm = self.cfg.get_nm()
        symbols = shell(nm, '--defined-only', '--extern-only', '--format=posix',
                        *self.objs, verbose=self.cfg.VERBOSE)
        return self.symbols_define_main(symbols)

    @staticmethod
    def symbols_define_main(symbols: str) -> bool:
        """Recognize an externally defined main in nm's POSIX-format symbols."""
        return any(len(fields := line.split()) >= 2 and
                   fields[0] == 'main' and fields[1] in ('T', 'W')
                   for line in symbols.splitlines())

    def add_config(self, config: DirectoryConfig, parent: Job | None = None) -> None:
        """Record config at parent's discovery position, or add its linker flags."""
        if parent is not None:
            self.link_events[parent].append(config)
            return
        if config in self.configs:
            return
        self.configs.add(config)

        if config.linkflags:
            self.extra_linkflags.extend(config.linkflags)


    def get_linkflags(self) -> list[str]:
        lflags = list(self.cfg.LDFLAGS) + self.extra_linkflags

        extra = []

        for flag in lflags:
            if flag.startswith('-L'):
                rpath_flag = '-Wl,-rpath,' + flag[2:]
                extra.append(rpath_flag)

        return lflags + extra
    

    async def resolve_module_source(self, name: str, type: SourceType,
                                    directory: DirectoryConfig | None, parent: Job) -> Path:
        """Resolve name/type, discovering GCC SDK modules with directory flags via parent."""
        if type == SourceType.MODULE and name in ('std', 'std.compat'):
            flags = header_unit_flags([*self.cfg.CXXFLAGS, *self.cfg.INCFLAGS],
                                      directory.buildvars.get('CFLAGS', []) if directory else [])
            return Path(await self.cfg.gcc_std_modules.resolve(
                name, self.cfg.CXX, flags, str(self.cfg.DEPDIR / 'gcc-std-modules'), parent,
                clang=self.cfg.USECLANG))
        try:
            return self.mod2src(name, type)
        except RuntimeError as error:
            if (type != SourceType.MODULE or not self.cfg.USE_DIRECTORY_CONFIG
                    or not str(error).startswith(f'Unable to locate module {name}:')):
                raise
            generated = await self.resolve_generated_module_source(name, parent)
            if generated is not None:
                return generated
            raise

    def module_lookup_candidates(
        self, modname: str, *, search_roots: Iterable[Path | str] | None = None
    ) -> list[Path]:
        """Return explicit module layouts in lookup order, without filesystem probing."""
        path = mod2path(modname, SourceType.MODULE)
        if path.is_absolute():
            return [path]
        roots = search_roots if search_roots is not None else [self.cfg.SRCDIR, *self.cfg.INCFLAGS]
        tagged_partition = tagged_partition_path(modname)
        result = []
        for base_path in roots:
            if isinstance(base_path, str):
                base_path = base_path.removeprefix('-I').removeprefix('-iquote')
                base_path = Path(base_path)
            full_path = base_path / path
            directory = full_path.with_suffix('')
            candidates = [full_path, directory / 'module.cc', directory / full_path.name]
            if tagged_partition is not None:
                candidates.append(base_path / tagged_partition)
            for candidate in candidates:
                if candidate not in result:
                    result.append(candidate)
        return result

    async def resolve_generated_module_source(
        self, modname: str, parent: Job, *, search_roots: Iterable[Path | str] | None = None
    ) -> Path | None:
        """Materialize a declared generated module after all source-tree layouts fail."""
        cfg = self.cfg
        for candidate in self.module_lookup_candidates(modname, search_roots=search_roots):
            logical = source_relative_path(candidate, cfg)
            if logical is None:
                continue
            directory = logical.parent
            while True:
                DirectoryConfig.get(cfg.SRCDIR / directory, cfg, log=parent)
                action = cfg.generated_outputs.get(logical)
                if action is not None:
                    await action.materialize(self, parent)
                    physical = action.physical_path(logical)
                    if not physical.is_file(cfg.vfs):
                        raise RuntimeError(f'Generated module output does not exist: {physical}')
                    return physical
                if str(directory) == '.':
                    break
                directory = directory.parent
        return None

    def mod2src(self, modname: str | None, type: SourceType,
                *, search_roots: Iterable[Path | str] | None = None,
                display_name: str | None = None) -> Path:
        """Find modname/type in search_roots, defaulting to source/include directories."""
        diagnostic_name = modname if display_name is None else display_name
        path = mod2path(modname, type)
        dotted_partition_variant = (type == SourceType.MODULE and modname is not None
                                    and ':' in modname and '.' in modname.split(':', 1)[1])
        tagged_partition = tagged_partition_path(modname) if type == SourceType.MODULE else None
        failed = []

        if path.is_absolute():
            if path.exists(self.cfg.vfs) and source_matches_target(path, self.cfg):
                return path
            failed.append(str(path))
        else:
            roots = search_roots if search_roots is not None else [self.cfg.SRCDIR, *self.cfg.INCFLAGS]
            for base_path in roots:
                if isinstance(base_path, str):
                    base_path = base_path.removeprefix("-I").removeprefix("-iquote")
                    base_path = Path(base_path)

                full_path = base_path / path
                directory = full_path.with_suffix('')
                candidates = [full_path]
                if type == SourceType.MODULE:
                    candidates.append(directory / 'module.cc')
                candidates.append(directory / full_path.name)
                for candidate in candidates:
                    if (candidate.is_file(self.cfg.vfs)
                            and (dotted_partition_variant or source_matches_target(candidate, self.cfg))):
                        return candidate
                    failed.append(str(candidate))
                for candidate in candidates:
                    if (type == SourceType.MODULE and not dotted_partition_variant
                            and candidate.parent.is_dir(self.cfg.vfs)):
                        # Tagged interfaces retain the logical module name. Only
                        # scan after all unqualified layouts in this root fail.
                        variants = sorted(
                            (Path(entry.path) for entry in self.cfg.vfs.scandir(candidate.parent)
                             if entry.is_file and entry.name.startswith(candidate.stem + '+')
                             and entry.name.endswith(candidate.suffix)
                             and source_matches_target(Path(entry.path), self.cfg)),
                            key=str)
                        if len(variants) > 1:
                            raise RuntimeError(f"Ambiguous module {diagnostic_name}: "
                                               + ", ".join(map(str, variants)))
                        if variants:
                            return variants[0]
                if tagged_partition is not None:
                    tagged_candidate = base_path / tagged_partition
                    if tagged_candidate.is_file(self.cfg.vfs):
                        return tagged_candidate
                    failed.append(str(tagged_candidate))

        raise RuntimeError(f"Unable to locate module {diagnostic_name}: " + ", ".join(failed))

class SourceFile:
    @staticmethod
    def get(
        path: Path,
        cfg: BuildConfig,
        type: SourceType | None = None,
        modname: str | None = None,
        inherited_dircfg: DirectoryConfig | None = None,
    ) -> SourceFile:
        if not (type == SourceType.MODULE and module_partition_selects_variant(path, modname)) \
                and not source_matches_target(path, cfg):
            raise RuntimeError(f'Source {path} requires inactive build tags (active: {", ".join(sorted(cfg.TAGS))})')
        std_header = type == SourceType.SYSTEM_HEADER and str(path).endswith('/bits/stdc++.h')
        std_module = type == SourceType.MODULE and modname in ('std', 'std.compat') and path.is_absolute()
        if (not cfg.USECLANG and std_header) or std_module:
            # The aggregate is requested by many directories. Keep incompatible
            # command-line configurations in separate source and artifact caches.
            directory_flags = (inherited_dircfg.buildvars.get('CFLAGS', [])
                               if inherited_dircfg is not None else [])
            flags = header_unit_flags([*cfg.CXXFLAGS, *cfg.INCFLAGS], directory_flags)
            key = (path, (cfg.CXX, *flags))
            if std_module:
                # A replaced compiler must not reuse an older module's CMI/object.
                fingerprint = cfg.gcc_std_modules.fingerprints[(cfg.CXX, flags)]
                local_flags = cfg.gcc_std_modules.local_flags[fingerprint].get(modname, ())
                flags = (*flags, *local_flags)
                key = (path, (*key[1], fingerprint, *local_flags))
                if cfg.USECLANG and cfg.CLANG_WRAPPER:
                    backend = cfg.vfs.stat(cfg.CLANG_WRAPPER)
                    key = (path, (*key[1], cfg.vfs.realpath(cfg.CLANG_WRAPPER),
                                  str(backend.st_mtime_ns), str(backend.st_size)))
            cache = cfg.std_header_sources if std_header else cfg.std_module_sources
            if key not in cache:
                source = SourceFile(path, type, modname, cfg, inherited_dircfg)
                source.std_header_flags = flags
                variant = hashlib.sha256(json.dumps(key[1]).encode()).hexdigest()[:16]
                for attribute in ('objpath', 'cmpath', 'infofile', 'makefile'):
                    original = getattr(source, attribute)
                    setattr(source, attribute, original.with_extra_suffix('.' + variant))
                source.output_path = source.cmpath if std_header else source.objpath
                source.std_header_variant = std_header
                source.std_module_variant = std_module
                cache[key] = source
            return cache[key]
        file = cfg.source_files.get(path)
        if file:
            if not cfg.USECLANG or cfg.CLANG_WRAPPER:
                # Both mappers discover modules while compiling ordinary .cc inputs.
                # Preserve the existing job and Clang's source-scoped PCM path.
                if file.type == SourceType.CPP and type == SourceType.MODULE and modname:
                    file.type = SourceType.MODULE
                    file.modname = modname
                    if not cfg.USECLANG:
                        file.cmpath = cfg.OBJDIR / mod2cm(modname, cfg.SRCDIR)
                elif file.type == SourceType.MODULE and type == SourceType.CPP:
                    type = SourceType.MODULE
            if type and file.type and type != file.type:
                raise Exception(f"type mismatch: new type {type}; old type {file.type}")
            if modname and file.modname and modname != file.modname:
                # Header-unit names are paths: GCC can retain redundant './' components.
                same_header = (file.type in (SourceType.USER_HEADER, SourceType.SYSTEM_HEADER)
                               and pathlib.PurePath(modname) == pathlib.PurePath(file.modname))
                if not same_header:
                    raise Exception(f"modname mismatch for {path}: requested {modname!r}; cached {file.modname!r}")
            return file
        file = SourceFile(path, type=type, modname=modname, cfg=cfg, inherited_dircfg=inherited_dircfg)
        cfg.source_files[path] = file
        return file

    def __init__(
        self,
        path: Path,
        type: SourceType | None,
        modname: str | None,
        cfg: BuildConfig,
        inherited_dircfg: DirectoryConfig | None = None,
    ) -> None:
        self.cfg = cfg
        logical_path = cfg.generated_logical_paths.get(path, path)
        cfg.check_database_timestamp(logical_path)
        self.path         = path
        self.logical_path = logical_path
        self.dirname      = logical_path.parent
        self.type         = type
        self.modname      = modname
        self.processed    = False
        self.job = None
        self.output_mtime = 0
        self.inherited_dircfg = inherited_dircfg
        self.std_header_variant = False
        self.std_module_variant = False
        self.std_header_flags: tuple[str, ...] = ()
        self.clang_module_files: dict[str, Path] = {}
        self.clang_exported_module: str | None = None
        self.cmhash = None

        if logical_path.is_absolute():
            file_parts = list(logical_path.parts)
        else:
            file_parts = list(logical_path.relative_to(cfg.SRCDIR).parts)

        for i, part in enumerate(file_parts):
            if part == "..":
                file_parts[i] = "__PARENT__"
        file = Path(*file_parts)
        
        self.objpath     = cfg.OBJDIR / file.with_suffix('.o')

        if modname and not (cfg.USECLANG and cfg.CLANG_WRAPPER and type == SourceType.MODULE):
            cmname = modname
            if path != logical_path and type in (SourceType.USER_HEADER, SourceType.SYSTEM_HEADER):
                cmname = './' + str(logical_path)
            self.cmpath  = cfg.OBJDIR / mod2cm(cmname, cfg.SRCDIR)
        else:
            self.cmpath  = cfg.OBJDIR / file.with_suffix(".pcm")
        
        self.output_path = self.cmpath if self.type in [SourceType.USER_HEADER, SourceType.SYSTEM_HEADER, SourceType.GENERATED_HEADER] else self.objpath
        if path != logical_path:
            # Generated header/source siblings (for example foo.pb.h/foo.pb.cc)
            # must not overwrite each other's dependency metadata.
            self.infofile = cfg.OBJDIR / file.with_extra_suffix(".info")
            self.makefile = cfg.OBJDIR / file.with_extra_suffix(".make")
        else:
            self.infofile = cfg.OBJDIR / file.with_suffix(".info")
            self.makefile = cfg.OBJDIR / file.with_suffix(".make")
        self.mtime       = self.path.mtime(cfg.vfs)
        # Preserve dependency encounter order while deduplicating headers.
        self.deps        = {}
        self.up_to_date  = None

        if type is None:
            if path.suffix in CCFILE_SUFFIXES:
                self.type = SourceType.CPP
            elif path.suffix == '.c':
                self.type = SourceType.C
            else:
                raise Exception('Unrecognized file type: %s' % str(path))

    def check_up_to_date(self, cfg: BuildConfig) -> None:
        if self.up_to_date is not None:
            return
        if cfg.REBUILD:
            # Rediscover dependencies through the compiler, including header units.
            self.up_to_date = False
            self.need_recompile = True
            return
        
        infofile_mtime = self.infofile.mtime(cfg.vfs)
        if self.mtime >= infofile_mtime:
            self.up_to_date = False
            self.need_recompile = True
            debug_log(f"#{self.path} NEED RECOMPILE BECAUSE MTIME={self.mtime} > INFOFILE_MTIME={infofile_mtime}", log=self.job)
            return
        
        self.output_mtime = infofile_mtime
        
        try:
            data = json.loads(self.infofile.read_text(cfg.vfs))
        except FileNotFoundError:
            self.up_to_date = False
            self.need_recompile = True
            return
        
        if data.get('tags') != sorted(cfg.TAGS):
            self.up_to_date = False
            self.need_recompile = True
            return

        if (cfg.USECLANG and cfg.CLANG_WRAPPER and not self.std_module_variant
                and self.type in (SourceType.CPP, SourceType.MODULE)
                and 'clang_exported_module' not in data):
            # Older wrapper builds did not record whether this source exported a PCM.
            self.up_to_date = False
            self.need_recompile = True
            return

        if data.get('absolute_module_paths', False) != cfg.ABSOLUTE_MODULE_PATHS:
            self.up_to_date = False
            self.need_recompile = True
            return

        if data.get('compiler_identity') != cfg.compiler_identity(self.compiler_cmd(cfg)[0]):
            self.up_to_date = False
            self.need_recompile = True
            debug_log(f"#{self.path} NEED RECOMPILE BECAUSE COMPILER IDENTITY CHANGED", log=self.job)
            return

        if data['command'] != self.compiler_cmd(cfg):
            self.up_to_date = False
            self.need_recompile = True
            debug_log("compiler command changed %s != %s" % (data['command'], self.compiler_cmd(cfg)), log=self.job)
            return

        if (not cfg.USECLANG and self.type not in (SourceType.C, SourceType.ASM)
                and data.get('std_header_unit', True) != cfg.STD_HEADER_UNIT):
            # Mapper policy changes are not reflected in the compiler command.
            # Recompile before loading dependencies recorded under the old mode.
            self.up_to_date = False
            self.need_recompile = True
            return
        
        self.clang_exported_module = data.get('clang_exported_module')
        self.need_recompile = False
        for depname in data['deps']:
            if depname.startswith('file:'):
                dep = Path(depname[5:])

                dep_mtime = SourceFile.get(dep, cfg).mtime
                if dep_mtime == 0 or dep_mtime >= infofile_mtime:
                    self.up_to_date     = False
                    self.need_recompile = True

            elif depname.startswith('module:'):
                m = re.match(r'module:(.*)@(.*)', depname)
                name, sha256 = m.groups()
                source_type = data.get('module_types', {}).get(name)
                self.deps[ModuleDep(name, sha256, SourceType(source_type) if source_type else None)] = None
                self.up_to_date = False

            elif depname.startswith('include:'):
                dep = depname[8:]
                hfile = HeaderDep.get(Path(dep), cfg)
                self.up_to_date = False
                header_mtime = hfile.mtime(cfg.vfs)
                if header_mtime == 0 or header_mtime >= infofile_mtime:
                    self.need_recompile = True
                self.deps[hfile] = None

            else:
                raise Exception(f"unrecognized dep type: {depname}")
            
        if self.up_to_date is None:
            self.up_to_date = True

    async def build(self, target: Target, cfg: BuildConfig) -> None:
        """Build this source for target/cfg; publish metadata only after success."""
        target.add_config(self.dircfg(), parent=self.job)
        if self.type == SourceType.USER_HEADER:
            # Importing a project header needs its companion object just like including it.
            HeaderDep.get(self.path, cfg).build(target, parent=self.job)
        if self.processed:
            await self.build_deps(target, cfg)
            return

        self.check_up_to_date(cfg)
        if not self.up_to_date:
            # Schedule cached dependencies together before checking their hashes.
            if not self.need_recompile:
                await self.build_deps(target, cfg)
            if self.need_recompile:
                cfg.vfs.makedirs(self.objpath.parent, exist_ok=True)
                await target.compile_source(self, cfg)
                self.update(cfg)
                self.output_mtime = self.output_path.mtime(cfg.vfs)
                for header_dep in self.header_deps:
                    header_dep.build(target, parent=self.job)
        self.processed = True

    async def build_deps(self, target: Target, cfg: BuildConfig) -> None:
        """Schedule recorded dependencies for target/cfg, then compare module hashes."""
        # Restore module identities before a preceding header schedules the same
        # path as its companion. Keep job scheduling in dependency encounter order.
        for dep in self.deps:
            if isinstance(dep, ModuleDep):
                mod = CompiledModule.get(dep.name, cfg, dep.type)
                path = await target.resolve_module_source(mod.name, mod.type, self.dircfg(), self.job)
                SourceFile.get(path, cfg, type=mod.type, modname=mod.name,
                               inherited_dircfg=self.dircfg())
        modules = []
        for dep in self.deps:
            if isinstance(dep, ModuleDep):
                mod = CompiledModule.get(dep.name, cfg, dep.type)
                path = await target.resolve_module_source(mod.name, mod.type, self.dircfg(), self.job)
                target.schedule_compilation_job(
                    path, type=mod.type, modname=mod.name,
                    inherited_dircfg=self.dircfg(), parent=self.job)
                modules.append((dep, mod))
            elif isinstance(dep, HeaderDep):
                dep.build(target, parent=self.job)
            else:
                raise ValueError(f'unrecognized dep {dep}')
        for dep, mod in modules:
            new_hash = await mod.build(target, inherited_dircfg=self.dircfg(),
                                       parent=self.job)
            if cfg.USECLANG:
                self.clang_module_files.update(mod.srcfile.clang_module_files)
                if mod.type == SourceType.MODULE:
                    self.clang_module_files[mod.name] = mod.cmpath
            if new_hash != dep.sha256:
                self.need_recompile = True

    def update(self, cfg: BuildConfig) -> None:
        deps = []
        for dep in self.deps:

            if isinstance(dep, ModuleDep):
                deps.append(f"module:{dep.name}@{dep.sha256}")
            elif isinstance(dep, HeaderDep):
                deps.append(f"include:{dep.path}")
            else:
                raise Exception(f"unhandled dep type #{dep} of type #{type(dep)}")

        out = {
            'command': self.compiler_cmd(cfg),
            'compiler_identity': cfg.compiler_identity(self.compiler_cmd(cfg)[0]),
            'absolute_module_paths': cfg.ABSOLUTE_MODULE_PATHS,
            'tags': sorted(cfg.TAGS),
            'deps': deps
        }
        if not cfg.USECLANG and self.type not in (SourceType.C, SourceType.ASM):
            out['std_header_unit'] = cfg.STD_HEADER_UNIT
        if cfg.USECLANG and cfg.CLANG_WRAPPER:
            out['clang_exported_module'] = self.clang_exported_module
        module_types = {dep.name: dep.type.value for dep in self.deps
                        if isinstance(dep, ModuleDep) and dep.type is not None}
        if module_types:
            out['module_types'] = module_types
        #print(out)
        atomic_write(self.infofile, json.dumps(out, indent=2) + '\n', cfg.vfs)

    def dircfg(self) -> DirectoryConfig | None:
        if not self.cfg.USE_DIRECTORY_CONFIG:
            return DirectoryConfig.get(Path('/'), self.cfg)
        if self.dirname.is_absolute():
            return self.inherited_dircfg
        
        return DirectoryConfig.get(self.dirname, self.cfg, log=self.job)

    def compiler_cmd(self, cfg: BuildConfig) -> list[str]:
        if self in cfg.compiler_commands:
            return cfg.compiler_commands[self]
        if cfg.USECLANG:
            cmd = self.compiler_cmd_clang(cfg)
            if cfg.CLANG_WRAPPER:
                # Include the selected backend in incremental command identity.
                # Runtime pipe descriptors are intentionally excluded.
                cmd = [cfg.CLANG_WRAPPER, "--", *cmd]
        else:
            cmd = self.compiler_cmd_gcc(cfg)

        cfg.compiler_commands[self] = cmd
        return cmd
    
    IFLAG_RE = re.compile('^-I')
    def compiler_extra_args(self) -> list[str]:
        """Return this source's directory flags and include paths."""
        if not self.cfg.USE_DIRECTORY_CONFIG:
            return []
        flags = []

        buildvars = self.dircfg().buildvars
        if 'CFLAGS' in buildvars:
            cflags = buildvars['CFLAGS']

            cflags = map(lambda flag: re.sub(self.IFLAG_RE, '-idirafter', flag, count=1), cflags)
            flags.extend(cflags)

        if self.path != self.logical_path:
            # A generated source physically lives below OBJDIR, but quoted
            # source-tree includes retain the semantics of its logical package.
            flags.append('-iquote' + str(self.dirname))

        dirparts = list(self.dirname.parts)
        self.add_include(dirparts, flags)

        if self.type == SourceType.C:
            flags.append("-xc")
        elif self.type == SourceType.ASM:
            flags.append("-xassembler-with-cpp")

        return flags
    
    def add_include(self, dirparts: list[str], flags: list[str]) -> None:
        index = -1

        try: index = dirparts.index('src')
        except ValueError: pass
        if index >= 0:
            flags.append("-I"+str(Path(*dirparts[:index], 'include')))
            flags.append("-iquote"+str(Path(*dirparts[:index], 'src')))
            return
        
        try: index = dirparts.index('Src')
        except ValueError: pass
        if index >= 0:
            flags.append("-iquote"+str(Path(*dirparts[:index], 'Inc')))
            return
        
        try: index = dirparts.index('deps')
        except ValueError: pass
        if index >= 0:
            f = "-I"+str(Path(*dirparts[:index+2]))
            flags.append(f)
            return
        

    def compiler_cmd_clang(self, cfg: BuildConfig, extra_args: list[str] = []) -> list[str]:
        if self.std_module_variant:
            # Emit the PCM first; the wrapper executes one frontend job at a time.
            return [cfg.CXX, *self.std_header_flags, *extra_args,
                    '-Wno-reserved-module-identifier', '-xc++-module', '--precompile',
                    '-MD', f'-MF{self.makefile}', '-o' + str(self.cmpath), str(self.path)]
        extra_args1 = self.compiler_extra_args()
        header_units = []

        if cfg.CLANG_WRAPPER and self.type in (SourceType.USER_HEADER, SourceType.SYSTEM_HEADER):
            # The mapper supplies a resolved filename, so no second header
            # search is needed. Keep inherited package flags and emit a depfile
            # for the headers textually included by this header unit.
            flavor = "system" if self.type == SourceType.SYSTEM_HEADER else "user"
            return [cfg.CXX, "-xc++-header", f"-fmodule-header={flavor}",
                    *extra_args, *extra_args1, *cfg.CXXFLAGS, *cfg.INCFLAGS,
                    "-MD", f"-MF{self.makefile}", "-o" + str(self.cmpath), str(self.path)]

        if self.type == SourceType.USER_HEADER:
            return [cfg.CXX, "-xc++-header", "-fmodule-header=user", f"-fprebuilt-module-path={cfg.OBJDIR}", *cfg.CXXFLAGS, *cfg.INCFLAGS, "-o"+str(self.cmpath), *extra_args, str(self.path)]
        
        if self.type == SourceType.SYSTEM_HEADER:
            raise NotImplementedError
        
        if cfg.CLANG_WRAPPER and self.type in (SourceType.CPP, SourceType.MODULE):
            # The wrapper emits a source-scoped PCM only if it discovers an interface.
            return [cfg.CXX, *extra_args, *extra_args1, *CLANG_CFLAGS,
                    *cfg.CXXFLAGS, *cfg.INCFLAGS, "-MD", f"-MF{self.makefile}",
                    "-o" + str(self.objpath), "-c", str(self.path)]

        if self.type == SourceType.MODULE:
            extra_args2 = [f"-fmodule-file={f}" for f in header_units] + [
                "-xc++-module", 
                f"-fmodule-output={self.cmpath}", 
                "-MD", 
                f"-MF{self.makefile}"
            ]
            return [cfg.CXX, f"-fprebuilt-module-path={cfg.OBJDIR}", *extra_args, *extra_args1, *extra_args2, *CLANG_CFLAGS, *cfg.CXXFLAGS, *cfg.INCFLAGS, "-o"+str(self.objpath), "-c", str(self.path)]
        
        
        if self.type == SourceType.CPP:
            args = [f"-fmodule-file={f}" for f in header_units] + ["-MD", f"-MF{self.makefile}"]
            return [cfg.CXX, *args, f"-fprebuilt-module-path={cfg.OBJDIR}", *extra_args, *extra_args1, *CLANG_CFLAGS, *cfg.CXXFLAGS, *cfg.INCFLAGS, "-o"+str(self.objpath), "-c", str(self.path)]    
            
        elif self.type in (SourceType.C, SourceType.ASM):
            args = ["-MD", f"-MF{self.makefile}"]
            return [cfg.CXX, *args, *extra_args, *extra_args, *extra_args1, *cfg.CFLAGS, *CLANG_CFLAGS, *cfg.INCFLAGS, "-o"+str(self.objpath), "-c", str(self.path)]
        
        raise Exception("unrecognized type: %s" % self.type)
                    

    def compiler_cmd_gcc(self, cfg: BuildConfig) -> list[str]:
        if self.std_module_variant:
            return [cfg.CXX, '-fmodules-ts', *self.std_header_flags,
                    '-o' + str(self.objpath), '-c', str(self.path)]
        if self.std_header_variant:
            # Build with exactly the configuration used for this unit's cache key,
            # independent of which importing directory requested it first.
            return [cfg.CXX, '-fmodules-ts', '-fmodule-header=system',
                    '-xc++-system-header', *self.std_header_flags, '-c', str(self.path)]
        args = [cfg.CC if self.type in (SourceType.C, SourceType.ASM) else cfg.CXX]
        if self.type in (SourceType.C, SourceType.ASM):
            args += [*cfg.CFLAGS]
            
        if self.type == SourceType.SYSTEM_HEADER:
            args += ["-fmodules-ts", "-fmodule-header=system", "-xc++-system-header", "-I.", *cfg.CXXFLAGS]

        elif self.type == SourceType.USER_HEADER:
            args += ["-fmodules-ts", "-fmodule-header=user", "-xc++-user-header", "-iquote.", *cfg.CXXFLAGS]

        elif self.type in [SourceType.CPP, SourceType.MODULE]:
            args += [
                "-fmodules-ts", 
                *cfg.CXXFLAGS
            ]

        elif self.type in (SourceType.C, SourceType.ASM):
            args += ["-MD", f"-MF{self.makefile}"]

        args += [*self.compiler_extra_args(), *cfg.INCFLAGS]

        if self.type not in [SourceType.USER_HEADER, SourceType.SYSTEM_HEADER, SourceType.GENERATED_HEADER]:
            args += ["-o"+str(self.objpath)]
        #args += ["-o"+str(self.output_path)]

        args += ["-c", str(self.path)]

        return args

    async def compile(self, target: Target, cfg: BuildConfig) -> None:
        """Compile for target/cfg using its selected asynchronous backend."""
        self.job.start_compilation(f'{self.type} {self.path}')
        self.header_deps = {}

        if cfg.USECLANG:
            await self.compile_clang(target, cfg)
        else:
            await self.compile_gcc(target, cfg)
        self.job.complete_compilation()

    MODULE_MAPPER_LINE_RE = re.compile(r'^([A-Z-]+)\b(.*)')
    async def compile_gcc(self, target: Target, cfg: BuildConfig) -> None:
        """Compile with GCC, resolving mapper dependencies through target's jobs."""
        if self.type in (SourceType.C, SourceType.ASM):
            await self.compile_gcc_c(cfg)
            return
        start = time.perf_counter()
        self.deps = {}
        self.vcpkgs = set()
        async with mapper_pipe() as (requests, replies, child_input, child_output):
            read_fd, write_fd = child_input.fileno(), child_output.fileno()
            command = self.compiler_cmd(cfg) + [f"-fmodule-mapper=<{read_fd}>{write_fd}"]

            async def protocol(process: asyncio.subprocess.Process) -> None:
                """Serve GCC process's batched mapper messages without blocking I/O."""
                child_input.close()
                child_output.close()
                while True:
                    responses = []
                    while True:
                        line = await requests.readline()
                        if not line:
                            return
                        match = self.MODULE_MAPPER_LINE_RE.match(line.decode().strip())
                        if match is None:
                            raise RuntimeError(f'Invalid GCC mapper request: {line!r}')
                        verb, args = match.groups()
                        args = shlex.split(args)
                        continuation = bool(args and args[-1] == ';')
                        if continuation:
                            args.pop()
                        responses.append(await self.gcc_mapper_request(verb, args, target, cfg))
                        if not continuation:
                            break
                    replies.write(' ;\n'.join(responses) + '\n')
                    replies.flush()

            await run_compiler(self.job, command, protocol=protocol,
                               pass_fds=(read_fd, write_fd),
                               env=dict(os.environ, SOURCE_DATE_EPOCH='0'),
                               color_diagnostics=True,
                               announce_command=self.type != SourceType.MODULE,
                               announce_on_failure=self.type == SourceType.MODULE)
        if cfg.VERBOSE:
            self.job.message(f"{self.path} done in {time.perf_counter() - start:.2f} seconds")

    async def gcc_mapper_request(self, verb: str, args: list[str], target: Target, cfg: BuildConfig) -> str:
        """Answer one GCC verb/args request, scheduling imports under target/cfg."""
        #if cfg.VERBOSE:
        #    print("GCC MAPPER REQUEST:", verb, args)
        if verb == 'HELLO':
            return 'HELLO 1 buildtool.py'
        if verb == 'INCLUDE-TRANSLATE':
            # Building the aggregate must use textual includes so its own
            # standard headers cannot request this same unit recursively.
            if not cfg.STD_HEADER_UNIT or self.std_header_variant:
                reply = 'BOOL TRUE'
            elif await cfg.gcc_std_headers.matches(
                    args[0], cfg.CXX,
                    [*cfg.CXXFLAGS, *self.compiler_extra_args(), *cfg.INCFLAGS], self.job):
                module = CompiledModule.get(args[0], cfg, SourceType.SYSTEM_HEADER)
                digest = await module.build(target, self.dircfg(), self.job)
                self.deps[ModuleDep(args[0], digest, SourceType.SYSTEM_HEADER)] = None
                return 'PATHNAME ' + shlex.quote(str(module.cmpath.relative_to(cfg.OBJDIR)))
            else:
                # FALSE means unknown, allowing GCC to ask about bits/stdc++.h.
                # TRUE would mark the original header as explicitly textual.
                reply = 'BOOL FALSE'
            # CMake supplies absolute include directories. Without directory
            # config, those headers must still invalidate the managed sources.
            if (not args[0].startswith('/') or self.std_header_variant or self.std_module_variant
                    or not cfg.USE_DIRECTORY_CONFIG):
                header = HeaderDep.get(Path(args[0]), cfg)
                self.deps[header] = None
                self.header_deps[header] = None
            return reply
        if verb == 'MODULE-REPO':
            return 'PATHNAME ' + shlex.quote(str(cfg.OBJDIR))
        if verb == 'MODULE-IMPORT':
            name = args[0]
            module = CompiledModule.get(name, cfg)
            digest = await module.build(target, inherited_dircfg=self.dircfg(), parent=self.job)
            self.deps[ModuleDep(name, digest)] = None
            return self.module_mapper_path(module.cmpath)
        if verb == 'MODULE-EXPORT':
            if (self.std_header_variant or self.std_module_variant
                    or self.type in (SourceType.USER_HEADER, SourceType.SYSTEM_HEADER)):
                return self.module_mapper_path(self.cmpath)
            if self.type in (SourceType.CPP, SourceType.MODULE):
                SourceFile.get(self.path, cfg, type=SourceType.MODULE, modname=args[0])
            # Path joining maps absolute header names under SYSTEM/, matching
            # SourceFile.cmpath. GCC expects a path relative to MODULE-REPO.
            module_path = cfg.OBJDIR / mod2cm(args[0], cfg.SRCDIR)
            return self.module_mapper_path(module_path)
        if verb == 'MODULE-COMPILED':
            return 'OK'
        raise RuntimeError(f'Unknown GCC mapper request: {verb}')

    def module_mapper_path(self, path: Path) -> str:
        """Format path for GCC; absolute paths allow reuse under another mapper root."""
        cfg = self.cfg
        filename = cfg.vfs.abspath(path) if cfg.ABSOLUTE_MODULE_PATHS else str(path.relative_to(cfg.OBJDIR))
        return 'PATHNAME ' + shlex.quote(filename)

    async def compile_gcc_c(self, cfg: BuildConfig) -> None:
        """Compile a C/assembly input with cfg and load its header depfile."""
        await run_compiler(self.job, self.compiler_cmd(cfg), color_diagnostics=True)
        self.process_makefile_deps()

    async def compile_clang(self, target: Target, cfg: BuildConfig) -> dict[ModuleDep | HeaderDep, None]:
        """Compile using cfg's wrapper or legacy scanner, scheduling under target."""
        self.clang_module_files = {}
        self.clang_exported_module = None
        if cfg.CLANG_WRAPPER:
            self.deps = {}
            self.vcpkgs = set()
            cfg.vfs.makedirs(self.cmpath.parent, exist_ok=True)
            await compile_with_mapper_async(
                self.job, cfg.CLANG_WRAPPER, self.compiler_cmd_clang(cfg),
                lambda request: self.resolve_clang_request(request, target, cfg))
        else:
            await self.clang_get_deps(target, cfg)
            module_args = [f'-fmodule-file={name}={path}' for name, path in self.clang_module_files.items()]
            await run_compiler(self.job, self.compiler_cmd_clang(cfg, extra_args=module_args),
                               color_diagnostics=True,
                               announce_command=self.type != SourceType.MODULE,
                               announce_on_failure=self.type == SourceType.MODULE)
        if self.std_module_variant:
            object_command = [cfg.CXX, *self.std_header_flags,
                *[f'-fmodule-file={name}={path}' for name, path in self.clang_module_files.items()],
                '-Wno-unused-command-line-argument', '-x', 'pcm', '-c', str(self.cmpath),
                '-o' + str(self.objpath)]
            if cfg.CLANG_WRAPPER:
                # The wrapper and standalone Clang may use different Clang library
                # revisions. Read the PCM with the same frontend that produced it.
                await compile_with_mapper_async(self.job, cfg.CLANG_WRAPPER, object_command,
                    lambda request: self.resolve_clang_request(request, target, cfg))
            else:
                await run_compiler(self.job, object_command, color_diagnostics=True,
                                   announce_command=self.type != SourceType.MODULE,
                                   announce_on_failure=self.type == SourceType.MODULE)
        self.process_makefile_deps()
        return self.deps

    async def resolve_clang_request(self, request: object, target: Target, cfg: BuildConfig) -> dict[str, object]:
        """Resolve request for target/cfg and include transitive named-module paths."""
        if isinstance(request, dict) and request.get("kind") == "export":
            name, pcm = request.get("name"), request.get("path")
            if not isinstance(name, str) or not re.fullmatch(r"[\w.]+(?::[\w.]+)?", name):
                raise ValueError("Invalid Clang exported module name")
            if not isinstance(pcm, str) or cfg.vfs.abspath(pcm) != cfg.vfs.abspath(self.cmpath):
                raise ValueError("Clang exported an unexpected PCM path")
            SourceFile.get(self.path, cfg, type=SourceType.MODULE, modname=name)
            self.clang_exported_module = name
            return {'pcm': cfg.vfs.abspath(self.cmpath)}
        path = await self.resolve_clang_module(request, target, cfg)
        return {'pcm': path, 'modules': {name: cfg.vfs.abspath(pcm)
                                        for name, pcm in self.clang_module_files.items()}}

    async def resolve_clang_module(self, request: object, target: Target, cfg: BuildConfig) -> str:
        """Resolve a wrapper request to a built PCM path using target/cfg."""
        source_type = None
        if not isinstance(request, dict):
            raise ValueError("Invalid Clang mapper request")
        if request.get("kind") == "header":
            name = request.get("path")
            if not isinstance(name, str) or not os.path.isabs(name):
                raise ValueError("Clang mapper requires an absolute header path")
            # Absolute header identities survive .info reloads and refer to
            # the same PCM from direct imports and named-module dependencies.
            name = os.path.normpath(name)
            source_type = SourceType.SYSTEM_HEADER if request.get("system", False) else SourceType.USER_HEADER
        elif request.get("kind") == "module":
            name = request.get("name")
            if not isinstance(name, str) or not re.fullmatch(r"[\w.]+(?::[\w.]+)?", name):
                raise ValueError("Invalid Clang module name")
        else:
            raise ValueError("Unknown Clang mapper request kind")
        module = CompiledModule.get(name, cfg, source_type)
        cmhash = await module.build(target, inherited_dircfg=self.dircfg(), parent=self.job)
        self.deps[ModuleDep(name, cmhash, source_type)] = None
        if module.srcfile is not None:
            self.clang_module_files.update(getattr(module.srcfile, 'clang_module_files', {}))
        if request.get('kind') == 'module':
            self.clang_module_files[name] = module.cmpath
        return cfg.vfs.abspath(module.cmpath)

    async def clang_get_deps(
        self,
        target: Target,
        cfg: BuildConfig,
    ) -> tuple[dict[ModuleDep | HeaderDep, None], list[Path]] | None:
        """Run the legacy dependency scanner for this source under target/cfg."""
        self.job.message("clang_get_deps", self.path)
        self.deps = {}
        self.vcpkgs = set()

        if self.type in [SourceType.USER_HEADER, SourceType.SYSTEM_HEADER]:
            extra_args = ["-xc++-header"]
        else:
            extra_args = ["-xc++"]
        args = [CLANG_PATH + CLANG_SCAND_DEPS, "-format=p1689", "--", *self.compiler_cmd_clang(cfg)]

        #print("running", *args)
        result = await run_compiler(self.job, args, capture=True)
        #print(result.stdout.decode(), result.stderr.decode())
        header_units = []
        #line_match = re.compile('^[a-zA-Z0-9\-_.\/]+:\d+:\d+: error: header file (["<])([a-zA-Z0-9\-_.\/]+)[">] \(aka \'([a-zA-Z0-9\-_.\/]+)\'\) cannot be imported because it is not known to be a header unit\n$')
        if result.returncode != 0:
            for line in result.stderr.decode().splitlines():
                m = re.match(r'^.*:\d+:\d+: error: header file (["<])([a-zA-Z0-9\-_.\/]+)[">] \(aka \'([a-zA-Z0-9\-_.\/]+)\'\) cannot be imported because it is not known to be a header unit$', line)
                #print("GOT", m, line)
                if m:
                    type = SourceType.SYSTEM_HEADER if m.group(1) == '<' else SourceType.USER_HEADER
                    header_path = m.group(3)

                    #print("GOT HEADER PATH", header_path)
                    mod = CompiledModule.get(header_path, cfg, type)
                    cmhash = await mod.build(target, inherited_dircfg=self.dircfg(), parent=self.job)
                    dep = ModuleDep(header_path, cmhash)
                    self.deps[dep] = None
                    header_units.append(mod.cmpath)
                    #exit(0)

                    #srcfile = SourceFile.get(header_path, cfg, type)
                    #srcfile.build(target)
                    #if type == SourceType.USER_HEADER:
                        #self.deps.add(srcfile)
                    #self.vcpkgs.update(srcfile.vcpkgs)
                    #header_units.append(srcfile.cmpath)

            extra_args += [f"-fmodule-file={f}" for f in header_units]
            clang_args = self.compiler_cmd_clang(cfg, extra_args=extra_args)
            #args = ["clang-scan-deps", "-format=p1689", "--", cfg.CXX, *extra_args,  *CCFLAGS, *CXXFLAGS, *INCFLAGS, "-o"+str(self.objpath), "-c", self.path]
            args = ["clang-scan-deps", "-format=p1689", "--", *clang_args]
            result = await run_compiler(self.job, args, capture=True)

            if result.returncode != 0:
                if result.stderr and not cfg.VERBOSE:
                    self.job.message(shlex.join(list(map(str, args))))
                self.job.write(result.stderr)
                raise subprocess.CalledProcessError(result.returncode, args)


        p1689 = json.loads(result.stdout.decode())
        for rule in p1689["rules"]:
            
            # provides = p1689["rules"][0]["requires"]
            if self.type == 'module':
                provides = rule["provides"]
                if not provides or len(provides) != 1:
                    raise RuntimeError(f"wanted module {self.modname} in {self.path}")

                name = provides[0]["logical-name"]
                if name != self.modname:
                    raise RuntimeError(f"wanted module {self.modname} in {self.path} but got {name}")

            if "requires" in rule:
                reqs = rule["requires"]
                for req in reqs:
                    modname = req["logical-name"]
                    self.job.message(f"about to build dep module {modname}")
                    mod = CompiledModule.get(modname, cfg)
                    cmhash = await mod.build(target, inherited_dircfg=self.dircfg(), parent=self.job)
                    self.deps[ModuleDep(modname, cmhash)] = None
                    self.clang_module_files.update(mod.srcfile.clang_module_files)
                    self.clang_module_files[modname] = mod.cmpath
            return self.deps, header_units

    def process_makefile_deps(self) -> None:
        if not self.cfg.CLANG_WRAPPER and self.type in [SourceType.USER_HEADER, SourceType.SYSTEM_HEADER]:
            return
        text = self.makefile.read_text(self.cfg.vfs)
        rules = parse_makefile_rules(text)
        for rule in rules:
            if self.cfg.CLANG_WRAPPER or self.std_module_variant or not self.cfg.USE_DIRECTORY_CONFIG:
                # PCMs are tracked by ModuleDep hashes, not as input headers.
                if rule.endswith('.pcm'):
                    continue
                if self.cfg.vfs.abspath(rule) == self.cfg.vfs.abspath(self.path):
                    continue
                path = Path(rule)
                dep = HeaderDep.get(path, self.cfg)
                self.deps[dep] = None
                if not dep.path.is_absolute():
                    self.header_deps[dep] = None
                continue
            if not rule.startswith('/') and rule != self.path:
                headerdep = HeaderDep.get(Path(rule), self.cfg)
                self.deps[headerdep] = None
                self.header_deps[headerdep] = None
                
            elif re.match(VCPKG_INCLUDE_RE, rule):
                pkg = re.match(VCPKG_INCLUDE_RE, rule).group(1)
                self.vcpkgs.add(pkg)


class ModuleDep:
    def __init__(self, name: str, sha256: str, type: SourceType | None = None) -> None:
        self.name = name
        self.sha256 = sha256
        self.type = type


def source_relative_path(path: Path, cfg: BuildConfig) -> Path | None:
    """Return path relative to SRCDIR, or None when it lies outside the source tree."""
    absolute = pathlib.Path(cfg.vfs.abspath(path))
    root = pathlib.Path(cfg.vfs.abspath(cfg.SRCDIR))
    try:
        return Path(absolute.relative_to(root))
    except ValueError:
        return None


class GeneratedAction:
    """One declared command producing fixed logical source-tree outputs."""

    def __init__(self, owner: Path, spec: object, cfg: BuildConfig) -> None:
        if not isinstance(spec, dict):
            raise ValueError(f'GENERATED entries in {owner}/BUILD.py must be dictionaries')
        unknown = set(spec) - {'inputs', 'outputs', 'command', 'tools', 'build_tools'}
        if unknown:
            raise ValueError(f'Unknown GENERATED keys in {owner}/BUILD.py: {", ".join(sorted(unknown))}')
        inputs, outputs, command, tools, build_tools = (
            spec.get('inputs', []), spec.get('outputs'), spec.get('command'), spec.get('tools', []),
            spec.get('build_tools', {}))
        if not isinstance(inputs, list) or not all(isinstance(value, str) for value in inputs):
            raise ValueError(f'GENERATED inputs in {owner}/BUILD.py must be a list of paths')
        if not isinstance(outputs, list) or not outputs or not all(isinstance(value, str) for value in outputs):
            raise ValueError(f'GENERATED outputs in {owner}/BUILD.py must be a non-empty list of paths')
        if not isinstance(command, list) or not command or not all(isinstance(value, str) for value in command):
            raise ValueError(f'GENERATED command in {owner}/BUILD.py must be a non-empty argument list')
        if not isinstance(tools, list) or not all(isinstance(value, str) for value in tools):
            raise ValueError(f'GENERATED tools in {owner}/BUILD.py must be a list of executables')
        if (not isinstance(build_tools, dict)
                or not all(isinstance(name, str) and name and isinstance(value, str)
                           for name, value in build_tools.items())):
            raise ValueError(f'GENERATED build_tools in {owner}/BUILD.py must map names to target paths')
        self.owner = owner
        self.inputs = tuple(self._input_path(value) for value in inputs)
        self.outputs = tuple(self._output_path(value) for value in outputs)
        self.command = tuple(command)
        self.tools = tuple(tools)
        self.cfg = cfg
        self.build_tools = {
            name: self._source_target_path(value) for name, value in build_tools.items()
        }

        for output in self.outputs:
            previous = cfg.generated_outputs.get(output)
            if previous is not None and previous is not self:
                raise ValueError(f'Generated output {output} has multiple producers')
            cfg.generated_outputs[output] = self
            cfg.generated_logical_paths[self.physical_path(output)] = cfg.SRCDIR / output

    def _input_path(self, value: str) -> Path:
        path = Path(value)
        if path.is_absolute():
            raise ValueError(f'Generated input must be relative to {self.owner}: {value}')
        logical = self.owner / path
        if logical.parts and logical.parts[0] == '..':
            raise ValueError(f'Generated input escapes the source tree: {value}')
        return logical

    def _output_path(self, value: str) -> Path:
        raw = pathlib.PurePath(value)
        if raw.is_absolute() or not raw.parts or '..' in raw.parts:
            raise ValueError(f'Generated output must stay below {self.owner}: {value}')
        return self.owner / Path(raw)

    def _source_target_path(self, value: str) -> Path:
        """Resolve one BUILD.py-relative target and keep it inside the source tree."""
        raw = pathlib.PurePath(value)
        if raw.is_absolute() or not raw.parts:
            raise ValueError(f'Generated build tool must be relative to {self.owner}: {value}')
        candidate = self.cfg.SRCDIR / self.owner / Path(raw)
        logical = source_relative_path(candidate, self.cfg)
        if logical is None:
            raise ValueError(f'Generated build tool escapes the source tree: {value}')
        return logical

    def physical_path(self, logical: Path) -> Path:
        """Return this configuration's materialized path for logical."""
        return self.cfg.OBJDIR / 'generated' / logical

    def state_path(self) -> Path:
        identity = hashlib.sha256('\0'.join(map(str, self.outputs)).encode()).hexdigest()[:20]
        return self.cfg.DEPDIR / 'generated-actions' / (identity + '.json')

    def expanded_command(self, built_tools: dict[str, Path] | None = None) -> list[str]:
        """Expand stable directory placeholders without shell interpretation."""
        cfg = self.cfg
        substitutions = {
            '{outdir}': cfg.vfs.abspath(cfg.OBJDIR / 'generated' / self.owner),
            '{srcdir}': cfg.vfs.abspath(cfg.SRCDIR / self.owner),
            '{root}': cfg.vfs.abspath(cfg.SRCDIR),
        }
        command = []
        for argument in self.command:
            for key, value in substitutions.items():
                argument = argument.replace(key, value)
            for name, path in (built_tools or {}).items():
                argument = argument.replace('{tool:' + name + '}', cfg.vfs.abspath(path))
            unresolved = re.search(r'\{tool:([^}]+)\}', argument)
            if unresolved:
                raise ValueError(f'Generated command references undeclared build tool {unresolved.group(1)!r}')
            command.append(argument)
        return command

    def executable_identity(self, tool: str) -> list[str | int]:
        """Identify one generator executable so tool upgrades invalidate outputs."""
        cfg = self.cfg
        if os.path.dirname(tool):
            candidate = tool if os.path.isabs(tool) else os.path.join(
                cfg.vfs.abspath(cfg.SRCDIR / self.owner), tool)
            executable = candidate if cfg.vfs.is_file(candidate) else None
        else:
            executable = cfg.vfs.which(tool)
        if executable is None:
            raise FileNotFoundError(f'Generator tool {tool!r} not found')
        path = cfg.vfs.realpath(executable)
        status = cfg.vfs.stat(path)
        return [path, status.st_mtime_ns, status.st_size]

    def tool_identities(self, command: Sequence[str]) -> list[list[str | int]]:
        """Identify the command executable and any additionally declared tools."""
        return [self.executable_identity(tool) for tool in (command[0], *self.tools)]

    def identity(self, command: Sequence[str], built_tools: dict[str, Path] | None = None) -> dict[str, object]:
        """Hash declared inputs plus command/tool identity for incremental generation."""
        cfg = self.cfg
        inputs = {}
        for logical in self.inputs:
            path = cfg.SRCDIR / logical
            if not path.is_file(cfg.vfs):
                raise FileNotFoundError(f'Generated input not found: {path}')
            inputs[str(logical)] = sha256_file(path, cfg.vfs)
        return {
            'command': list(command),
            'tools': self.tool_identities(command),
            'build_tools': {
                name: [cfg.vfs.realpath(path), sha256_file(path, cfg.vfs)]
                for name, path in sorted((built_tools or {}).items())
            },
            'inputs': inputs,
        }

    def current(self, identity: dict[str, object]) -> bool:
        """Return whether all outputs exist and their recorded action identity matches."""
        cfg = self.cfg
        if cfg.REBUILD or any(not self.physical_path(output).is_file(cfg.vfs) for output in self.outputs):
            return False
        try:
            return json.loads(self.state_path().read_text(cfg.vfs)) == identity
        except FileNotFoundError:
            return False

    def build_tool_artifact(self, logical: Path) -> Path:
        """Give source-built generator executables unique deterministic artifact paths."""
        cfg = self.cfg.EXEC_CONFIG or self.cfg
        return cfg.OBJDIR / 'tools' / self.execution_tool_path(logical)

    def execution_tool_path(self, logical: Path) -> Path:
        """Map a generator target from the action source tree into the execution tree."""
        cfg = self.cfg.EXEC_CONFIG
        if cfg is None:
            return logical
        source = self.cfg.SRCDIR / logical
        mapped = source_relative_path(source, cfg)
        if mapped is None:
            raise RuntimeError(f'Generated build tool lies outside execution source tree: {source}')
        return mapped

    async def build_tool(self, logical: Path, target: Target, parent: Job) -> Path:
        """Recursively build one executable target in the current scheduler."""
        cfg = self.cfg.EXEC_CONFIG or self.cfg
        execution_logical = self.execution_tool_path(logical)
        source = cfg.SRCDIR / execution_logical
        if not source.is_dir(cfg.vfs):
            raise RuntimeError(f'Generated build tool target is not a directory: {source}')
        artifact = self.build_tool_artifact(logical)
        key = ('build-tool', str(execution_logical), str(artifact))

        async def work(job: Job) -> None:
            job.diagnostic_source = str(source)
            tool = Target(Path(cfg.vfs.abspath(source)), cfg)
            tool.session = target.session
            tool.job_sources = target.job_sources
            tool.link_events = target.link_events
            tool.roots = []
            tool.link_events.setdefault(job, [])
            sources = directory_sources(source, cfg)
            if not sources:
                raise RuntimeError(f'No source files in generated build tool target {source}')

            for path in sources:
                dependency = tool.schedule_compilation_job(path, parent=job)
                if dependency not in tool.roots:
                    tool.roots.append(dependency)
            await tool.wait_for_compilations(job)

            tool.collect_link_inputs()
            if not await tool.defines_main_async(job):
                raise RuntimeError(f'Generated build tool target has no main function: {source}')
            await tool.link_async(job, artifact)

        dependency = target.session.schedule(key, work, parent=parent)
        await parent.wait_for_dependency(dependency)
        if not artifact.is_file(cfg.vfs):
            raise RuntimeError(f'Generated build tool did not produce executable: {artifact}')
        return artifact

    async def build(self, target: Target, job: Job) -> None:
        """Materialize all outputs once when this action's identity is stale."""
        cfg = self.cfg
        built_tools = {
            name: await self.build_tool(logical, target, job)
            for name, logical in self.build_tools.items()
        }
        command = self.expanded_command(built_tools)
        identity = self.identity(command, built_tools)
        if self.current(identity):
            return
        for output in self.outputs:
            cfg.vfs.makedirs(self.physical_path(output).parent, exist_ok=True)
        job.message('GENERATING', ', '.join(map(str, self.outputs)))
        await run_compiler(
            job, command, cwd=cfg.vfs.abspath(cfg.SRCDIR / self.owner), compilation=False,
            announce_command=cfg.VERBOSE, announce_on_failure=True)
        missing = [output for output in self.outputs if not self.physical_path(output).is_file(cfg.vfs)]
        if missing:
            raise RuntimeError('Generator did not produce declared outputs: ' + ', '.join(map(str, missing)))
        for output in self.outputs:
            physical = self.physical_path(output)
            if output.name.endswith((".cc", ".cpp", ".c")):
                cfg.check_database_timestamp(physical)
        cfg.vfs.makedirs(self.state_path().parent, exist_ok=True)
        atomic_write(self.state_path(), json.dumps(identity, indent=2, sort_keys=True) + '\n', cfg.vfs)

    async def materialize(self, target: Target, parent: Job) -> None:
        """Share this generation action in target's current scheduler and await it."""
        key = ('generate', tuple(map(str, self.outputs)))

        async def work(job: Job) -> None:
            job.diagnostic_source = str(self.owner / 'BUILD.py')
            await self.build(target, job)

        dependency = target.session.schedule(key, work, parent=parent)
        await parent.wait_for_dependency(dependency)


class DirectoryConfig:
    CACHE_VERSION = 4

    @classmethod
    def get(cls, path: Path, cfg: BuildConfig, log: Job | None = None) -> DirectoryConfig:
        """Load/cache path's build settings; send setup diagnostics to optional log."""
        if path in cfg.directory_configs:
            return cfg.directory_configs[path]
        directory = cls(path, cfg)
        directory.process(log=log)
        cfg.directory_configs[path] = directory
        return directory

    def __init__(self, path: Path, cfg: BuildConfig) -> None:
        self.cfg = cfg
        if path.is_absolute():
            self.dir = None
            return
        
        self.dir = path.relative_to(cfg.SRCDIR)

    def process(self, log: Job | None = None) -> None:
        """Read this directory's settings, writing setup diagnostics to optional log."""
        if self.dir is None:
            self.buildvars = {}
            self.linkflags = []
            return
        
        buildpy_file = self.cfg.SRCDIR / self.dir / 'BUILD.py'
        self.cfg.check_database_timestamp(buildpy_file, build_config=True)
        if not buildpy_file.exists(self.cfg.vfs):
            self.buildvars = {}
            self.linkflags = []
            return
        
        json_file = self.cfg.DEPDIR / self.dir / 'buildvars.json'
        json_mtime = json_file.mtime(self.cfg.vfs)
        buildrb_mtime = buildpy_file.mtime(self.cfg.vfs)

        cached = None
        if buildrb_mtime <= json_mtime and THIS_MTIME <= json_mtime:
            try:
                cached = json.loads(json_file.read_text(self.cfg.vfs))
            except Exception as ex:
                raise RuntimeError(f"error reading JSON {json_file}: {ex}") from ex

        if cached is None or cached.get('version') != self.CACHE_VERSION:
            text = try_read(buildpy_file, self.cfg.vfs)
            code = compile(text, buildpy_file, 'exec')
            env = {}
            exec(code, env)

            out = {}
            ALLOWED = ('LDFLAGS', 'CFLAGS', 'PKGCONFIG', 'EXPLICIT_SOURCES', 'GENERATED')
            for key, val in env.items():
                if key in ALLOWED:
                    out[key] = val
                elif key.startswith('__'):
                    continue
                else:
                    #raise(Exception(f"unrecognized key {key} in {buildpy_file}"))
                    pass

            self.buildvars = out

            self.handle_pkgconfig(self.buildvars, log=log)
            self.cfg.vfs.makedirs(json_file.parent, exist_ok=True)
            cached = {'version': self.CACHE_VERSION, 'buildvars': self.buildvars}
            atomic_write(json_file, json.dumps(cached, indent=2), self.cfg.vfs)
        else:
            self.buildvars = cached['buildvars']

        generated = self.buildvars.get('GENERATED', [])
        if not isinstance(generated, list):
            raise ValueError(f'GENERATED in {buildpy_file} must be a list')
        self.generators = [GeneratedAction(self.dir, spec, self.cfg) for spec in generated]

        if 'LDFLAGS' in self.buildvars:
            self.linkflags = self.buildvars['LDFLAGS']
        else:
            self.linkflags = []

    def handle_pkgconfig(self, buildvars: dict[str, list[str]], log: Job | None = None) -> None:
        """Append package flags to buildvars; route tool diagnostics to optional log."""
        if 'PKGCONFIG' not in buildvars:
            return
        
        linkflags = list(buildvars.get('LDFLAGS', []))
        cflags = list(buildvars.get('CFLAGS', []))
        
        output_options = {"log": log} if log is not None else {}
        if self.cfg.VERBOSE:
            output_options['verbose'] = True
        for pkg in buildvars['PKGCONFIG']:
            libs_flags = shlex.split(shell("pkg-config", "--libs", pkg, **output_options))
            cflags_cur = self.filter_cflags(shlex.split(shell("pkg-config", "--cflags", pkg, **output_options)))
            linkflags.extend(libs_flags)
            cflags.extend(cflags_cur)

        if linkflags:
            buildvars['LDFLAGS'] = linkflags

        if cflags:
            buildvars['CFLAGS'] = cflags
            # buildvars['CXXFLAGS'] = list(cflags)
            
    def filter_cflags(self, flags: list[str]) -> list[str]:
        out = []
        
        for flag in flags:
            if flag.startswith('-std='):
                continue
            
            out.append(flag)
            
        return out

class HeaderDep:
    @classmethod
    def get(cls, path: Path, cfg: BuildConfig) -> HeaderDep:
        """Cache path in cfg, normalizing workspace headers for companion discovery."""
        path = cfg.generated_logical_paths.get(path, path)
        if path.is_absolute():
            try:
                path = path.relative_to(cfg.vfs.getcwd())
            except ValueError:
                pass  # Headers outside the workspace remain external dependencies.
        if path not in cfg.header_deps:
            cfg.header_deps[path] = cls(path)
        return cfg.header_deps[path]

    def __init__(self, path: Path) -> None:
        #print("PATH", path, type(path))
        self.path = path
        self.built = False
        self._mtimes: dict[FileSystem, float] = {}

    def build(self, target: Target, parent: Job | None = None) -> None:
        """Discover this header's companion source, recording it under parent job."""
        self.built = True
        if self.path.is_absolute():
            # External headers participate in timestamp checks but do not
            # discover project BUILD.py files or companion source files.
            return
        #debug_log("HeaderDep.build", self.path)
        
        dirname = self.path.parent
        dircfg = DirectoryConfig.get(dirname if target.cfg.USE_DIRECTORY_CONFIG else Path('/'),
                                     target.cfg, log=parent)

        target.add_config(dircfg, parent=parent)
        cppfile = self.find_cpp(self.path, target.cfg)
        debug_log('find_cpp', self.path, '-->', cppfile, log=parent)
        if cppfile and target.should_build_companion(cppfile):
            self.cpp_path = cppfile
            if parent is None:
                target.compile(self.cpp_path)
            else:
                target.schedule_compilation_job(self.cpp_path, parent=parent)
            return

    def mtime(self, vfs: FileSystem) -> float:
        # Input headers are stable during a build. Keep their timestamps for
        # this dependency's lifetime, which ends when its BuildConfig resets.
        if vfs not in self._mtimes:
            self._mtimes[vfs] = self.path.mtime(vfs)
        return self._mtimes[vfs]

    def find_cpp(self, hfile: Path, cfg: BuildConfig) -> Path | None:
        """Find hfile's companion source using cfg's filesystem and active tags."""
        vfs = cfg.vfs
        if hfile.suffix not in HFILE_SUFFIXES:
            return None
        
        basename = hfile.with_suffix('')
        for ext in [".cc", ".cpp", ".c"]:
            cppfile = basename.with_extra_suffix(ext)
            if cppfile.exists(vfs) and source_matches_target(cppfile, cfg):
                return cppfile
            for physical, logical in cfg.generated_logical_paths.items():
                if logical == cppfile and physical.exists(vfs):
                    return physical
            
        #print("!!!!", list(hfile.parts), 'include' in hfile.parts)
        if "include" in hfile.parts:
            parts = list(hfile.parts)
            include_index = parts.index('include')
            parts[include_index] = 'src'
            newpath = Path(*parts)

            if newpath.parent.is_dir(vfs):
                return self.find_cpp(newpath, cfg)
            
            # project/include/project/file.h -> project/src/file.h
            if include_index > 0 and include_index < len(parts) - 2 and parts[include_index-1] == parts[include_index+1]:
                parts.pop(include_index+1)
                return self.find_cpp(Path(*parts), cfg)
        
        if "Inc" in hfile.parts:
            parts = list(hfile.parts)
            include_index = parts.index('Inc')
            parts[include_index] = 'Src'
            newpath = Path(*parts)

            if newpath.parent.is_dir(vfs):
                return self.find_cpp(newpath, cfg)

        return None
    
      
# def setup_vscode(path):
#     os.makedirs(os.path.join(path, '.vscode'), exist_ok=True)
#     name = os.path.basename(path)

#     props = {
#         'name': name,
#         'includePath': ['${workspaceFolder}/../lib/**'],
#         "defines": [],
#         "compilerPath": "/usr/bin/g++-12",
#         "cStandard": "c11",
#         "cppStandard": "c++23",
#         # "intelliSenseMode": "clang-x64"
#     }

#     json_content = json.dumps(props, indent=4)
#     json_path = os.path.join(path, '.vscode', 'c_cpp_properties.json')
#     print(json_content)
#     with open(json_path, 'w') as f:
#         f.write(json_content)

class CompilationDatabase:
    def __init__(self, paths: list[Path]) -> None:
        self.paths = paths
        self.processed_files = set()
        self.entries = []

    def build(self, cfg: BuildConfig) -> str:
        command_cfg = cfg
        if cfg.IDE_CXX is not None:
            command_cfg = copy.copy(cfg)
            command_cfg.CXX = cfg.IDE_CXX
            command_cfg.CC = cfg.IDE_CC or cfg.IDE_CXX
            if cfg.IDE_CXXFLAGS is not None:
                command_cfg.CXXFLAGS = cfg.IDE_CXXFLAGS
            command_cfg.USECLANG = True
            command_cfg.CLANG_WRAPPER = None

        for path in find_files(self.paths, suffixes=[".cc", ".cpp", ".c"], vfs=cfg.vfs):
            if source_matches_target(path, cfg):
                self.process_file(path, cfg, command_cfg=command_cfg)

        # Generated translation units live below OBJDIR rather than the source
        # roots scanned above. A normal build has already registered their
        # physical-to-logical paths. A standalone `ide` invocation has not, so
        # discover materialized outputs under OBJDIR/generated and infer the same
        # logical source path from their relative path.
        generated_root = cfg.OBJDIR / 'generated'
        if generated_root.is_dir(cfg.vfs):
            for path in find_files([generated_root], suffixes=[".cc", ".cpp", ".c"], vfs=cfg.vfs):
                if path not in cfg.generated_logical_paths:
                    cfg.generated_logical_paths[path] = cfg.SRCDIR / path.relative_to(generated_root)

        for path in sorted(cfg.generated_logical_paths, key=str):
            if path.name.endswith((".cc", ".cpp", ".c")) and path.is_file(cfg.vfs):
                self.process_file(path, cfg, command_cfg=command_cfg)

        asyncio.run(self.add_standard_modules(cfg, command_cfg))
        return json.dumps(self.entries, indent=2)

    async def add_standard_modules(self, cfg: BuildConfig, command_cfg: BuildConfig) -> None:
        """Add installed SDK sources to cfg's IDE database so clangd builds its own PCMs."""
        directory = DirectoryConfig.get(Path('.'), cfg)
        flags = header_unit_flags([*command_cfg.CXXFLAGS, *command_cfg.INCFLAGS],
                                  directory.buildvars.get('CFLAGS', []))
        session = BuildSession(cfg.JOBS, verbose=cfg.VERBOSE, jobserver=cfg.jobserver)

        async def discover(job: Job) -> None:
            """Discover optional SDK modules through job without compiling them."""
            for name in ('std', 'std.compat'):
                source = await cfg.gcc_std_modules.resolve(name, command_cfg.CXX, flags,
                    str(cfg.DEPDIR / 'gcc-std-modules'), job,
                    clang=command_cfg.USECLANG, optional=True)
                if source:
                    self.process_file(Path(source), cfg, modname=name, command_cfg=command_cfg)

        session.schedule('ide-standard-modules', discover)
        await session.finish()

    def process_file(self, path: Path, cfg: BuildConfig, *, modname: str | None = None,
                     command_cfg: BuildConfig | None = None) -> None:
        """Record path's IDE command; modname identifies an external SDK module."""
        # path = os.path.normpath(os.path.join(basepath, filepath))
        command_cfg = command_cfg or cfg
        source_cfg = command_cfg if modname in ('std', 'std.compat') else cfg
        file = SourceFile.get(path, source_cfg, type=SourceType.MODULE if modname else None,
                              modname=modname,
                              inherited_dircfg=DirectoryConfig.get(Path('.'), source_cfg) if modname else None)
        if file in self.processed_files:
            return
        
        self.processed_files.add(file)

        # dirpath = os.path.dirname(filepath)
        # filename = os.path.basename(filepath)
        compilation_cmd = [str(cmd) for cmd in file.compiler_cmd_clang(command_cfg)]
        if not command_cfg.USECLANG:
            # GCC's query-driver probe understands c++, but not c++-module.
            # clangd's module builder selects the module-interface action itself.
            compilation_cmd = ['-xc++' if arg == '-xc++-module' else arg for arg in compilation_cmd]
        # clangd builds its own BMIs. The normal build cache may contain GCC CMIs
        # or Clang BMIs made with a different compiler/configuration.
        compilation_cmd = [arg for arg in compilation_cmd
                           if arg != f'-fprebuilt-module-path={command_cfg.OBJDIR}']

        self.entries.append({
            "file": str(path),
            "directory": cfg.vfs.getcwd(),
            "arguments": compilation_cmd,
        })

def find_files(
    paths: list[Path],
    suffixes: Sequence[str],
    prefixes: Sequence[str] | None = None,
    *,
    vfs: FileSystem,
) -> Iterator[Path]:
    """
    Generator function to yield all files in the given directory
    that end with any of the specified suffixes.

    Args:
    directory (str): The directory path to search for files.
    suffixes (list of str): A list of file suffixes to match.

    Yields:
    str: Full path to a file matching one of the suffixes.
    """
    # Normalize the suffixes to ensure consistent comparison
    # print("file", paths)
    suffixes = tuple(suffixes)  # Convert to tuple for faster checks
    for path in paths:
        if path.is_file(vfs):
            if not path.name.endswith(suffixes):
                continue

            if prefixes is not None and not path.name.startswith(prefixes):
                continue

            yield path
            continue

        for entry in sorted(vfs.scandir(path), key=lambda entry: entry.name):
            if entry.is_file and entry.name.endswith(suffixes):
                if prefixes is not None and not entry.name.startswith(prefixes):
                    continue
                yield Path(entry.path)
            elif entry.is_dir and not entry.is_symlink and not entry.name.startswith("."):
                yield from find_files([Path(entry.path)], suffixes=suffixes, prefixes=prefixes, vfs=vfs)

def atomic_write(path: Path, data: str, vfs: FileSystem) -> None:
    tmpfile = path.with_extra_suffix(".tmp")
    vfs.write_text(tmpfile, data)
    vfs.replace(tmpfile, path)

def atomic_symlink(path: Path, target: str, vfs: FileSystem) -> None:
    tmpfile = path.with_extra_suffix(f".{uuid4().hex}.tmp")
    vfs.symlink(target, tmpfile)
    try:
        vfs.replace(tmpfile, path)
    finally:
        try:
            vfs.unlink(tmpfile)
        except FileNotFoundError:
            pass

def try_read(path: Path, vfs: FileSystem) -> str | None:
    try:
        return path.read_text(vfs)
    except FileNotFoundError:
        return None
    
def shell(*args: str | os.PathLike[str], log: Job | None = None, verbose: bool = False) -> str:
    """Run args, returning stdout; send diagnostics to log and echo launches if verbose."""
    cmd = shlex.join(list(map(str, args)))
    if verbose:
        if log is not None:
            log.session.report_launch(cmd)
        else:
            print(f'launching {cmd}', flush=True)
    if log is not None:
        result = subprocess.run(args, capture_output=True, text=True)
        if result.stderr:
            if not verbose:
                log.message(cmd)
            log.write(result.stderr)
        result.check_returncode()
        return result.stdout
    result = subprocess.run(args, shell=False, text=True, stdin=0, capture_output=True)
    if result.stderr:
        if not verbose:
            print(cmd, flush=True)
        print(result.stderr, end='', file=sys.stderr, flush=True)
    if result.returncode != 0:
        exit(1)
    return result.stdout

async def shell_async(job: Job, *args: str | os.PathLike[str]) -> str:
    """Run inspection args within job's limit; return stdout and report stderr."""
    result = await run_compiler(job, args, capture=True, compilation=False)
    if result.stderr:
        if not job.session.verbose:
            job.message(shlex.join(list(map(str, args))))
        job.write(result.stderr)
    result.check_returncode()
    return result.stdout.decode(errors='replace')


def mod2cm(modname: str, srcdir: str | os.PathLike[str] = SRCDIR) -> str:
    if modname.startswith('/'):
        path = modname
    elif modname.startswith('./'):
        path = pathlib.PurePath(modname + '.pcm')
        path = str(path.relative_to(srcdir))
        return path
        #path = modname[2:].removeprefix((SRCDIR + '/', ''))
    else:
        path = modname.replace(':', '-')

    #path = path.replace('.', '/')
    return path + ".pcm"

def mod2path(modname: str | None, type: SourceType) -> Path:
    if type == SourceType.USER_HEADER:
        return Path(modname)
    
    if modname.startswith('/'):
        return Path(modname)
        
    if modname.startswith('./'):
        return Path(modname)

    if ':' in modname:
        module, partition = modname.split(':', 1)
        path = module.replace('.', '/') + '/' + partition.replace('.', '+')
    else:
        path = modname.replace('.', '/')

    return Path(path + '.cc')
    
    # srcfile =  SRCDIR / path + ".cc"

    #     if not srcfile.exists():
    #         warn(f"FATAL: Unable to locate module fragment {modname}: the following files does not exist: {srcfile}")
    #         exit(1)
    #     return srcfile


    # srcfile1 = SRCDIR / (path + ".cc")
    # if srcfile1.exists():
    #     return srcfile1
    
    # basename = srcfile1.name
    # srcfile2 = SRCDIR / path / basename
    
    # if srcfile2.exists():
    #     return srcfile2
    
    # warn(f"FATAL: Unable to locate module {modname}: the following files do not exist: {srcfile1}, {srcfile2}")
    # exit(1)

def parse_makefile_rules(text: str) -> list[str]:
    # Compiler depfiles escape whitespace, #, backslash and colon with a
    # backslash, and escape dollar signs as $$. Only the first unescaped
    # colon separates the target from dependencies (we don't request -MP).
    text = text.replace('\\\n', '')
    words, word = [], []
    dependencies = False
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == '\\' and i + 1 < len(text):
            i += 1
            word.append(text[i])
        elif ch == ':' and not dependencies:
            dependencies = True
            word = []
        elif ch.isspace():
            if word and dependencies:
                words.append(''.join(word))
            word = []
        elif ch == '$' and i + 1 < len(text) and text[i + 1] == '$':
            word.append('$')
            i += 1
        else:
            word.append(ch)
        i += 1
    if word and dependencies:
        words.append(''.join(word))
    return words

def warn(*s: object) -> None:
    print(*s, file=sys.stderr)

def debug_log(*text: object, log: Job | None = None) -> None:
    """Emit text when debugging is enabled, using log if a build job owns it."""
    if DEBUG_LOG:
        if log is not None:
            log.message(*text)
        else:
            warn(*text)

def directory_sources(path: Path, cfg: BuildConfig, *, tests: bool = False) -> list[Path]:
    """List immediate sources in path using cfg's VFS; tests selects only test sources."""
    suffixes = (*CCFILE_SUFFIXES, '.c', '.S', '.s')
    # Explicit targets and header companions remain usable; only automatic
    # directory discovery omits these alternative entry points/implementations.
    explicit_only = DirectoryConfig.get(path, cfg).buildvars.get('EXPLICIT_SOURCES', [])
    return [Path(entry.path) for entry in sorted(cfg.vfs.scandir(path), key=lambda entry: entry.name)
            if entry.is_file and entry.name.endswith(suffixes)
            and entry.name not in explicit_only
            and source_matches_target(Path(entry.path), cfg)
            and is_test_source(Path(entry.path)) == tests]


def expand_target_pattern(path: Path, cfg: BuildConfig, *, tests: bool = False) -> list[Path]:
    """Expand a trailing /... into source directories; tests selects test-bearing directories."""
    if path.name != '...':
        return [path]
    root = path.parent
    if not root.is_dir(cfg.vfs):
        raise RuntimeError(f'Not a directory: {root}')
    excluded = {cfg.vfs.abspath(directory) for directory in (cfg.OBJDIR, cfg.DEPDIR, cfg.BINDIR)}
    # Skip sibling build configurations too, unless their parent is the search root.
    for directory in (cfg.OBJDIR.parent, cfg.DEPDIR.parent):
        if cfg.vfs.abspath(directory) != cfg.vfs.abspath(root):
            excluded.add(cfg.vfs.abspath(directory))

    def visit(directory: Path) -> Iterator[Path]:
        """Visit directory and eligible children in lexical order, without following symlinks."""
        if directory_sources(directory, cfg, tests=tests):
            yield directory
        for entry in sorted(cfg.vfs.scandir(directory), key=lambda entry: entry.name):
            if (entry.is_dir and not entry.is_symlink and not entry.name.startswith(('.', '_'))
                    and entry.name not in ('testdata', 'vendor')
                    and cfg.vfs.abspath(entry.path) not in excluded):
                yield from visit(Path(entry.path))

    targets = list(visit(root))
    if not targets:
        warn(f'Pattern {path} matched no {"test" if tests else "source"} directories')
    return targets


def package_artifact(path: Path, cfg: BuildConfig, *, sources: Sequence[Path] = ()) -> Path:
    """Name a package binary by absolute path and optional test selection to avoid collisions."""
    identity = [cfg.vfs.abspath(path), *sorted(cfg.vfs.abspath(source) for source in sources)]
    key = hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:16]
    name = Path(cfg.vfs.abspath(path)).name
    return cfg.OBJDIR / ('tests' if sources else 'packages') / key / (name + cfg.SUFFIX)


@dataclass
class BuildPlan:
    """A target's roots, output policy, and resulting internal binary, if it has main."""
    target: Target
    sources: list[Path]
    artifact: Path | None = None
    check_main: bool = True
    publish: bool = True
    binary: Path | None = None
    error: Exception | None = None

    async def finish(self, job: Job) -> None:
        """Wait for this target's graph, then inspect/link using job's slot and log."""
        await self.target.wait_for_compilations(job)
        self.target.collect_link_inputs()
        if self.check_main and not await self.target.defines_main_async(job):
            return
        self.binary = await self.target.link_async(job, self.artifact)


def build_plans(plans: Sequence[BuildPlan], cfg: BuildConfig, *, keep_going: bool = False) -> None:
    """Build plans with one limit; keep_going records per-plan errors for test packages."""
    if not plans:
        return

    async def run() -> None:
        """Schedule all roots before running finalizers alongside pending compilations."""
        graph = CompilationGraph(cfg)
        try:
            for plan in plans:
                plan.binary = None
                plan.error = None
                plan.target.schedule_sources(plan.sources, graph)
            final_jobs = []
            for index, plan in enumerate(plans):
                # Finalizers execute immediately but their logs follow the compilation queue.
                final_jobs.append(graph.session.schedule(('link', index), plan.finish, final=True))
            await graph.session.finish(keep_going=keep_going)
            for plan, job in zip(plans, final_jobs):
                plan.error = job.error
        finally:
            await graph.session.close()
            for plan in plans:
                plan.target.session = None

    asyncio.run(run())
    for plan in plans:
        if plan.binary is not None and plan.publish:
            _, public_file = plan.target.binary_paths(plan.artifact)
            plan.target.publish_binary(plan.binary, public_file)


def build_targets(path: Path, cfg: BuildConfig) -> None:
    """Build path or schedule every directory selected by path/... in one shared graph."""
    if path.name != '...':
        build(path, cfg)
        return
    plans = [BuildPlan(Target(Path(cfg.vfs.abspath(selected)), cfg),
                       directory_sources(selected, cfg), artifact=package_artifact(selected, cfg),
                       check_main=cfg.SUFFIX != '.so')
             for selected in expand_target_pattern(path, cfg)]
    build_plans(plans, cfg)


def build(path: Path, cfg: BuildConfig, *, publish: bool = True,
          artifact: Path | None = None) -> Path | None:
    """Build file/directory path with cfg; publish exposes executables in bin.

    Directory builds compile immediate source files and return None if no main
    is defined. artifact overrides the internal binary path for recursive targets.
    Explicit shared-library builds still link without main.
    """
    directory = path.is_dir(cfg.vfs)
    name = Path(cfg.vfs.abspath(path)) if directory else path.with_suffix('')
    target = Target(name, cfg)
    if directory:
        sources = directory_sources(path, cfg)
        if not sources:
            raise RuntimeError(f'No source files in {path}')
        target.compile_many(sources)
        if cfg.SUFFIX != '.so' and not target.defines_main():
            return None
    else:
        target.compile(path)
    
    return target.link(publish=publish, artifact=artifact)

def make_compilation_database(paths: list[Path], cfg: BuildConfig) -> str:
    db = CompilationDatabase(paths)
    return db.build(cfg)

def build_compilation_database(out: Path, paths: list[Path], cfg: BuildConfig) -> None:
    data = make_compilation_database(paths, cfg)
    
    atomic_write(out, data, cfg.vfs)
    print("wrote %s" % out)


def track_compilation_database(cfg: BuildConfig) -> Path:
    """Enable timestamp tracking in cfg and return the absolute database path."""
    database = Path(cfg.vfs.abspath(ROOT)) / 'compile_commands.json'
    cfg.compilation_database_mtime = database.mtime(cfg.vfs)
    cfg.compilation_database_dirty = not database.exists(cfg.vfs)
    return database


def refresh_compilation_database(cfg: BuildConfig, database: Path, source_roots: list[str]) -> None:
    """Stop tracking in cfg and refresh database from source_roots if flagged."""
    cfg.compilation_database_mtime = None
    if not cfg.compilation_database_dirty:
        return
    previous = cfg.vfs.getcwd()
    try:
        cfg.vfs.chdir(database.parent)
        build_compilation_database(database, [Path(p) for p in source_roots], cfg)
        cfg.compilation_database_dirty = False
    finally:
        cfg.vfs.chdir(previous)


def mkpath(path: str | os.PathLike[str], *, vfs: FileSystem) -> Path:
    return Path(os.path.relpath(vfs.abspath(path), vfs.abspath(ROOT)))

def run_tool(tool_path: str, dirs: list[str], cfg: BuildConfig) -> None:
    dirs = [Path(cfg.vfs.abspath(dir)) for dir in dirs]

    # change directory to root
    oldwd = None
    if ROOT != ".":
        oldwd = cfg.vfs.getcwd()
        cfg.vfs.chdir(ROOT)

    
    main_path = mkpath(tool_path, vfs=cfg.vfs)
    main_name = main_path.with_suffix('')
    target = Target(main_name, cfg)
    sources = [main_path]
    
    for filename in find_files(dirs, suffixes=CCFILE_SUFFIXES, vfs=cfg.vfs):
        if not is_test_source(filename) or not source_matches_target(filename, cfg):
            continue
        #print("building %s..." % filename)
        path = mkpath(filename, vfs=cfg.vfs)
        sources.append(path)

    target.compile_many(sources)
    bin = target.link()
    bin = cfg.vfs.abspath(bin)
    if oldwd:
        cfg.vfs.chdir(oldwd)
    os.execv(bin, [bin])


def run_tests(dirs: list[str], cfg: BuildConfig) -> None:
    """Build and run a separate test binary per directory; only /... searches recursively."""
    patterns = [Path(cfg.vfs.abspath(argument)) for argument in dirs]
    previous = cfg.vfs.getcwd()
    failed = False
    try:
        cfg.vfs.chdir(ROOT)
        groups: dict[Path, set[Path]] = {}
        for pattern in patterns:
            for selected in expand_target_pattern(pattern, cfg, tests=True):
                if selected.is_dir(cfg.vfs):
                    groups.setdefault(selected, set()).update(directory_sources(selected, cfg, tests=True))
                elif selected.is_file(cfg.vfs) and is_test_source(selected):
                    if not source_matches_target(selected, cfg):
                        raise RuntimeError(f'Source {selected} requires inactive build tags')
                    groups.setdefault(selected.parent, set()).add(selected)
                else:
                    raise RuntimeError(f'Expected a directory or test source: {selected}')
        main_path = mkpath(TESTMAIN, vfs=cfg.vfs)
        plans = []
        for directory, files in groups.items():
            label = mkpath(directory, vfs=cfg.vfs)
            if not files:
                print(f'? {label} [no test files]', flush=True)
                continue
            sources = [mkpath(file, vfs=cfg.vfs) for file in sorted(files, key=str)]
            plans.append(BuildPlan(Target(directory, cfg), [main_path, *sources],
                                   artifact=package_artifact(directory, cfg, sources=sources),
                                   check_main=False, publish=False))
        build_plans(plans, cfg, keep_going=True)
        for plan in plans:
            directory = plan.target.path
            label = mkpath(directory, vfs=cfg.vfs)
            success = False
            try:
                # A subprocess lets later packages run after a test failure; tests run in their own directory.
                if plan.error is None and plan.binary is not None:
                    sys.stdout.flush()
                    result = subprocess.run([cfg.vfs.abspath(plan.binary)], cwd=str(directory), check=False)
                    success = result.returncode == 0
            except (RuntimeError, ValueError, OSError, subprocess.CalledProcessError) as error:
                if not getattr(error, 'buildtool_reported', False):
                    warn(error)
                success = False
            print(f'{"ok" if success else "FAIL"} {label}', flush=True)
            failed |= not success
    finally:
        cfg.vfs.chdir(previous)
    if failed:
        raise SystemExit(1)

def run_benchmarks(dirs: list[str], cfg: BuildConfig) -> None:
    run_tool(BENCHMAIN, dirs, cfg)
        
def sha256_file(path: Path, vfs: FileSystem) -> str:
    return vfs.sha256(path)


def reset_build_state(cfg: BuildConfig) -> None:
    """Start a fresh build invocation for the specified configuration."""
    cfg.reset_build_state()


def looks_like_module_interface(path: Path, vfs: FileSystem) -> bool:
    """Heuristically recognize an export-module declaration in path using vfs.

    This filter is only for header generation; Clang validates the selected
    inputs. It ignores comments, literals, and preprocessor directive lines,
    but does not evaluate conditional compilation or expand macros.
    """
    source = path.read_text(vfs).replace('\\\n', '')
    ignored = (r'\b(?:u8|u|U|L)?R"(?P<delimiter>[^ ()\\\t\r\n]{0,16})\(.*?\)(?P=delimiter)"'
               r'|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\''
               r'|//[^\n]*|/\*.*?\*/')
    source = re.sub(ignored, ' ', source, flags=re.DOTALL)
    source = re.sub(r'^[ \t]*#[^\n]*', '', source, flags=re.MULTILINE)
    return re.search(r'\bexport\s+module\s+[A-Za-z_]\w*(?:\s*[.:]\s*[A-Za-z_]\w*)*\s*[;\[]',
                     source) is not None


def module_header_sources(directory: Path, vfs: FileSystem) -> Iterator[Path]:
    """Yield candidate .cc interfaces below directory in sorted order using vfs."""
    for entry in sorted(vfs.scandir(directory), key=lambda entry: entry.name):
        if entry.is_symlink:
            continue
        path = Path(entry.path)
        if entry.is_dir and not entry.name.startswith('.') and entry.name != 'generated-headers':
            yield from module_header_sources(path, vfs)
        elif entry.is_file and path.suffix == '.cc' and looks_like_module_interface(path, vfs):
            yield path


def generate_module_headers(path: Path, cfg: BuildConfig) -> list[Path]:
    """Extract interfaces below path with cfg's Clang flags into sibling generated-headers."""
    # Use project-relative inputs so their BUILD.py settings are loaded even when
    # the user supplies an absolute scan path.
    root = Path(os.path.relpath(cfg.vfs.abspath(path), cfg.vfs.getcwd()))
    if not root.is_dir(cfg.vfs):
        raise RuntimeError(f'Expected a directory for generate-module-headers: {path}')
    output_root = Path(cfg.vfs.abspath(root)).parent / 'generated-headers'
    sources = [source for source in module_header_sources(root, cfg.vfs)
               if source_matches_target(source, cfg)]
    if not sources:
        print(f'No module interfaces found in {path}')
        return []
    tool = Path(os.environ.get('BT_MODULE_HEADER',
        str(pathlib.Path(__file__).resolve().parent / 'build/module-header/module-to-header')))
    if not tool.is_file(cfg.vfs):
        raise RuntimeError(f'Module header extractor not found: {tool}. Build module-header with CMake '
                           '(see module-header/README.md), or set BT_MODULE_HEADER.')
    resource_dir = shell(cfg.CXX, '-print-resource-dir', verbose=cfg.VERBOSE).strip()
    if not resource_dir:
        raise RuntimeError(f'{cfg.CXX} returned an empty Clang resource directory')
    outputs = []
    for source in sources:
        output = output_root / source.relative_to(root).with_suffix('.h')
        file = SourceFile.get(source, cfg, type=SourceType.MODULE)
        cfg.vfs.makedirs(output.parent, exist_ok=True)
        print(f'GENERATING {source} -> {output}', flush=True)
        shell(tool, source, '-o', output, '--', *file.compiler_extra_args(),
              *CLANG_CFLAGS, *cfg.CXXFLAGS, *cfg.INCFLAGS,
              '-resource-dir=' + resource_dir, verbose=cfg.VERBOSE)
        outputs.append(output)
    return outputs

## MAIN ##
def _main(
    CC: str = CC,
    CXX: str = CXX,
    CFLAGS: list[str] = CFLAGS,
    CXXFLAGS: list[str] = CXXFLAGS,
    LDFLAGS: list[str] = LDFLAGS,
    OBJDIR: str | os.PathLike[str] = OBJDIR,
    DEPDIR: str | os.PathLike[str] = DEPDIR,
    SRCDIR: str | os.PathLike[str] = SRCDIR,
    BINDIR: str | os.PathLike[str] = BINDIR,
    INCFLAGS: list[str] = INCFLAGS,
    USECLANG: bool = USECLANG,
    SRC_ROOTS: list[str] = SRC_ROOTS,
    vfs: FileSystem = _DEFAULT_VFS,
    CLANG_CXXFLAGS: list[str] = [],
    CLANG_LDFLAGS: list[str] = [],
    IDE_CXX: str | None = None,
    IDE_CC: str | None = None,
    IDE_CXXFLAGS: list[str] | None = None,
    TAGS: Iterable[str] | None = None,
    KNOWN_TAGS: Iterable[str] | None = None,
) -> None:
    """Parse CLI arguments with compiler/path/VFS defaults; CLANG_* flags apply only to Clang."""

    buildcfg = Release
    parser = argparse.ArgumentParser(
        prog        = 'buildtool',
        description = 'Utility for compiling and running C++ programs',
        allow_abbrev=False,
    )
    parser.add_argument('--debug-log', action='store_true', help='enable debug logging')
    parser.add_argument('--verbose', '-v', action='store_true', help='print every command and compilation timings')
    subparsers = parser.add_subparsers(dest='cmd')
    
    build_parser = subparsers.add_parser('build', help='build the specified binary or library')
    build_parser.add_argument('path', help='source file, directory, or directory/... for recursive builds')
    build_parser.add_argument('--release', '-r', action='store_const', dest='buildtype', const='release', help='build in release mode')
    build_parser.add_argument('--library', action='store_true', help='build in library mode')
    build_parser.add_argument('--clang', action='store_true', help='build with clang')
    build_parser.add_argument('args', nargs=argparse.REMAINDER)

    run_parser = subparsers.add_parser('run', help='run the specified binary')
    run_parser.add_argument('path', help='source file or directory defining main')
    run_parser.add_argument('--release', '-r', action='store_const', dest='buildtype', const='release', help='build in release mode')
    run_parser.add_argument('--clang', action='store_true', help='build with clang')
    run_parser.add_argument('args', nargs=argparse.REMAINDER,
                            help='arguments passed to the program')

    ide_parser = subparsers.add_parser('ide', help='generate a compile_commands.json compilation database')
    ide_parser.add_argument('paths', nargs='*')
    ide_parser.add_argument('--clang', action='store_true', help='use the Clang toolchain for IDE commands')

    headers_parser = subparsers.add_parser('generate-module-headers',
        help='recursively generate headers for .cc module interfaces')
    headers_parser.add_argument('path', help='directory to scan; output goes to sibling generated-headers')
    headers_parser.add_argument('args', nargs=argparse.REMAINDER, help=argparse.SUPPRESS)

    test_parser = subparsers.add_parser('test', help='run tests in the specified directories or files')
    test_parser.add_argument('dirs', nargs='+', help='test files, directories, or directory/... for recursive tests')
    test_parser.add_argument('--release', '-r', action='store_const', dest='buildtype', const='release', help='build in release mode')
    test_parser.add_argument('--clang', action='store_true', help='build with clang')

    bench_parser = subparsers.add_parser('bench', help='run benchmarks in the specified directories or files')
    bench_parser.add_argument('dirs', nargs='+')
    bench_parser.add_argument('--release', '-r', action='store_const', dest='buildtype', const='release', help='build in release mode')
    bench_parser.add_argument('--clang', action='store_true', help='build with clang')

    for command_parser in (ide_parser, test_parser, bench_parser):
        # Capture option-looking filenames after the first path as positional args.
        command_parser.add_argument('remaining_paths', nargs=argparse.REMAINDER,
                                    help=argparse.SUPPRESS)

    for command_parser in (build_parser, run_parser, test_parser, bench_parser, ide_parser, headers_parser):
        command_parser.add_argument('--tags', metavar='TAG,TAG',
                                    help='replace active source tags (default: configured/native tags)')
        command_parser.add_argument('--verbose', '-v', action='store_true', default=argparse.SUPPRESS,
                                    help='print every command and compilation timings')

    for command_parser in (build_parser, run_parser, test_parser, bench_parser):
        command_parser.add_argument('--debug', '-d', action='store_const',
                                    dest='buildtype', const='debug', help='build in debug mode')
        command_parser.add_argument('--rebuild', action='store_true',
                                    help='recompile the target and all its dependencies, then relink')
        command_parser.add_argument('--no-std-header-unit', action='store_true',
                                    help='disable automatic GCC bits/stdc++.h header-unit imports')
        command_parser.add_argument("-j", "--jobs", type=int, default=os.cpu_count() or 1,
                                    help="maximum active compiler processes (also capped by memory)")

    args = parser.parse_args()
    if args.cmd == 'generate-module-headers' and args.args:
        parser.error('generate-module-headers accepts exactly one directory; options must precede it')
    if args.cmd in ('ide', 'test', 'bench'):
        paths = args.paths if args.cmd == 'ide' else args.dirs
        paths.extend(args.remaining_paths)
    if getattr(args, "jobs", 1) < 1:
        parser.error("--jobs must be at least 1")

    g = globals()
    buildtype = Release
    if args.cmd in ['build', 'run', 'test', 'bench']:
        if args.buildtype == 'debug':
            buildtype = Debug
        if args.clang:
            USECLANG = True
            CXX = CLANG_PATH + CLANGXX
            CC = CLANG_PATH + CLANG
    if args.cmd == 'ide' and args.clang:
        USECLANG = True
        CXX = CLANG_PATH + CLANGXX
        CC = CLANG_PATH + CLANG
    if USECLANG:
        CXXFLAGS = [*CXXFLAGS, *CLANG_CXXFLAGS]
        LDFLAGS = [*LDFLAGS, *CLANG_LDFLAGS]
    if args.cmd == 'generate-module-headers':
        USECLANG = True
        CXX = os.environ.get('BT_MODULE_HEADER_CLANG', CLANG_PATH + CLANGXX)
        CC = CLANG_PATH + CLANG
    
    if args.debug_log:
        g['DEBUG_LOG'] = True

    for key, val in buildtype.__dict__.items():
        if key.startswith('__'):
            continue

        globals()[key] = val

    build_dir = "debug" if buildtype == Debug else "release"
    if USECLANG:
        build_dir += "+clang"
    tags = TAGS if TAGS is not None else native_tags()
    if getattr(args, 'tags', None) is not None:
        tags = [tag.strip() for tag in args.tags.split(',')] if args.tags else []
    tags = validate_tags(tags)
    if tags != native_tags():
        key = hashlib.sha256(json.dumps(sorted(tags)).encode()).hexdigest()[:12]
        build_dir += '+tags-' + key

    cfg = BuildConfig(
        CC=CC,
        CXX=CXX, 
        CFLAGS=CFLAGS,
        CXXFLAGS=CXXFLAGS,
        LDFLAGS=LDFLAGS,
        OBJDIR=Path(OBJDIR) / build_dir,
        DEPDIR=Path(DEPDIR) / build_dir,
        SRCDIR=SRCDIR,
        BINDIR=BINDIR,
        INCFLAGS=INCFLAGS,
        SUFFIX=SUFFIX,
        USECLANG=USECLANG,
        CLANG_WRAPPER=os.environ.get("BT_CLANG_WRAPPER") if USECLANG else None,
        IDE_CXX=IDE_CXX,
        IDE_CC=IDE_CC,
        IDE_CXXFLAGS=IDE_CXXFLAGS,
        JOBS=getattr(args, "jobs", 1),
        REBUILD=getattr(args, 'rebuild', False),
        VERBOSE=args.verbose,
        progress=True,
        STD_HEADER_UNIT=not getattr(args, 'no_std_header_unit', False),
        memory=MemoryBudget() if args.cmd in ('build', 'run', 'test', 'bench') else None,
        vfs=vfs,
        TAGS=tags,
        KNOWN_TAGS=KNOWN_TAGS,
    )

    # g['OBJDIR'] = Path(OBJDIR)
    # g['DEPDIR'] = Path(DEPDIR)
    # g['SRCDIR'] = Path(SRCDIR)
    # g['BINDIR'] = Path(BINDIR)
    # g['INCFLAGS'] = INCFLAGS

    if args.cmd == 'build':
        file = args.path
        target = mkpath(file, vfs=cfg.vfs)
        if ROOT != ".":
            cfg.vfs.chdir(ROOT)

        if args.library:
            cfg.SUFFIX = '.so'
            cfg.LDFLAGS += ["-shared"]
        
        database = track_compilation_database(cfg)
        try:
            build_targets(target, cfg)
        finally:
            refresh_compilation_database(cfg, database, SRC_ROOTS)
    
    elif args.cmd == 'run':
        file = args.path
        target = mkpath(file, vfs=cfg.vfs)
        oldwd = None
        if ROOT != ".":
            oldwd = cfg.vfs.getcwd()
            cfg.vfs.chdir(ROOT)
        database = track_compilation_database(cfg)
        try:
            executable = build(target, cfg, publish=False)
        finally:
            refresh_compilation_database(cfg, database, SRC_ROOTS)
        if executable is None:
            raise RuntimeError(f'No main function defined in {target}')
        bin = cfg.vfs.abspath(executable)
        if oldwd:
            cfg.vfs.chdir(oldwd)
        os.execv(bin, [bin] + args.args)

    elif args.cmd == 'generate-module-headers':
        target = mkpath(args.path, vfs=cfg.vfs)
        if ROOT != '.':
            cfg.vfs.chdir(ROOT)
        generate_module_headers(target, cfg)

    elif args.cmd == 'ide':
        paths = []
        cfg.vfs.chdir(ROOT)

        if len(args.paths) == 0:
            for root in SRC_ROOTS:
                paths.append(Path(root))

        else:
            for arg in args.paths:
                file = arg
                path = mkpath(file, vfs=cfg.vfs)
                paths.append(path)

        build_compilation_database(Path("compile_commands.json"), paths, cfg)

    elif args.cmd == "test":
        dirs = args.dirs
        database = track_compilation_database(cfg)
        try:
            run_tests(dirs, cfg)
        finally:
            refresh_compilation_database(cfg, database, SRC_ROOTS)

    elif args.cmd == "bench":
        dirs = args.dirs
        run_benchmarks(dirs, cfg)
    
        

    else:
        parser.print_help()
        exit(1)

def main(*args: Any, **kwargs: Any) -> None:
    """Run the CLI with configuration args/kwargs; report build errors without traces."""
    try:
        return _main(*args, **kwargs)
    except (RuntimeError, ValueError, OSError, subprocess.CalledProcessError) as error:
        if not getattr(error, 'buildtool_reported', False):
            warn(f'buildtool: error: {error}')
        raise SystemExit(max(1, getattr(error, 'returncode', 1))) from None
    except KeyboardInterrupt:
        raise SystemExit(130) from None


if __name__ == '__main__':
    main()
