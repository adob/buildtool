#!/usr/bin/env python3

from __future__ import annotations
import os
import asyncio
import json
import hashlib
import subprocess
import shlex
import argparse
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
    from .gcc_std import GccStdHeaders, GccStdModules, header_unit_flags
else:
    from vfs import FileSystem, RealFileSystem, MemoryFileSystem
    from clang_mapper import compile_with_mapper_async
    from compiler import mapper_pipe, run_compiler
    from scheduler import BuildSession, ConcurrencyReporter, Job
    from memory import MemoryBudget
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

TESTMAIN = "deps/baselib/lib/testing/testmain.cc"
BENCHMAIN = "deps/baselib/lib/testing/benchmain.cc"

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

THIS_MTIME = 0

# Generate the representation while retaining custom initialization and identity.
@dataclass(init=False, eq=False)
class BuildConfig:
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
    gcc_std_headers: GccStdHeaders
    gcc_std_modules: GccStdModules
    std_header_sources: dict[tuple[Path, tuple[str, ...]], SourceFile]
    std_module_sources: dict[tuple[Path, tuple[str, ...]], SourceFile]

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
    ) -> None:
        self.vfs = vfs
        self.TAGS = validate_tags(TAGS) if TAGS is not None else native_tags()
        self.KNOWN_TAGS = validate_tags(KNOWN_TAGS) if KNOWN_TAGS is not None else None
        if self.KNOWN_TAGS is not None and (unknown := self.TAGS - self.KNOWN_TAGS):
            raise ValueError(f'Unknown active build tags: {", ".join(sorted(unknown))}')
        self.CC = CC
        self.CXX = CXX
        self.CFLAGS = COMPILE_FLAGS + CFLAGS
        self.CXXFLAGS = COMPILE_FLAGS + CXXFLAGS
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
        self.gcc_std_headers = GccStdHeaders(vfs)
        self.gcc_std_modules = GccStdModules(vfs)
        self.std_header_sources = {}
        self.std_module_sources = {}

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
        self.gcc_std_headers.paths.clear()
        self.gcc_std_modules.sources.clear()
        self.gcc_std_modules.fingerprints.clear()
        self.gcc_std_modules.local_flags.clear()
        self.std_header_sources.clear()
        self.std_module_sources.clear()

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
        self.cmpath = self.srcfile.cmpath
        if self.srcfile.cmhash is None:
            self.srcfile.cmhash = sha256_file(self.cmpath, target.cfg.vfs)
        self.cmhash = self.srcfile.cmhash
        return self.cmhash

class Target:
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
            self.session = BuildSession(self.cfg.JOBS, memory=self.cfg.memory,
                                        verbose=self.cfg.VERBOSE,
                                        concurrency_reporter=self.cfg.concurrency_reporter)
            self.job_sources = {}
            self.link_events = {}
            try:
                for source in sources:
                    if isinstance(source, tuple):
                        path, kind, name, directory = source
                        self.schedule_compilation_job(path, kind, name, directory)
                    else:
                        self.schedule_compilation_job(source)
                await self.session.finish()
                self.collect_link_inputs()
            finally:
                # Scheduling can fail before finish() takes ownership of cleanup.
                await self.session.close()
                self.session = None

        asyncio.run(run())

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
            await source.build(self, self.cfg)

        job = self.session.schedule(str(source.output_path), work, parent=parent)
        self.job_sources[job] = source
        self.link_events.setdefault(job, [])
        if parent is not None and job not in self.link_events[parent]:
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

        for root in self.session.roots:
            visit(root)

    def link(self, *, publish: bool = True, artifact: Path | None = None) -> Path:
        """Link to optional artifact; publish exposes the executable through bin's symlink."""
        dirname = self.path.parent
        #buildvars = DirectoryConfig.get(dirname).buildvars

        suffix = self.cfg.SUFFIX
        extra_flags = []
        
        if self.cfg.OUTFILE is None:
            name = self.path.name + suffix
        else:
            name = self.cfg.OUTFILE
        ofile = artifact if artifact is not None else self.cfg.OBJDIR / "bin" / name
        public_file = self.cfg.BINDIR / name

        ofile_mtime = ofile.mtime(self.cfg.vfs)
        if self.cfg.REBUILD or self.most_recent_output_mtime >= ofile_mtime or THIS_MTIME > ofile_mtime:
            lflags = self.get_linkflags()
            self.cfg.vfs.makedirs(ofile.parent, exist_ok=True)
            print("LINKING", ofile)
            shell(self.cfg.CXX, *extra_flags, *self.objs, *lflags, f"-o{ofile}",
                  verbose=self.cfg.VERBOSE)
        if not publish:
            return ofile
        self.cfg.vfs.makedirs(public_file.parent, exist_ok=True)
        link_target = os.path.relpath(self.cfg.vfs.abspath(ofile),
                                      self.cfg.vfs.abspath(public_file.parent))
        atomic_symlink(public_file, link_target, self.cfg.vfs)
        return public_file

    def defines_main(self) -> bool:
        """Check this target's compiled objects for an externally defined main function."""
        if not self.objs:
            return False
        # Let the compiler select its toolchain's nm, including cross-toolchains.
        nm = shell(self.cfg.CXX, '-print-prog-name=nm', verbose=self.cfg.VERBOSE).strip()
        symbols = shell(nm, '--defined-only', '--extern-only', '--format=posix',
                        *self.objs, verbose=self.cfg.VERBOSE)
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
        return self.mod2src(name, type)

    def mod2src(self, modname: str | None, type: SourceType) -> Path:
        """Find modname's interface or header of type in the configured search roots."""
        path = mod2path(modname, type)
        failed = []

        if path.is_absolute():
            if path.exists(self.cfg.vfs) and source_matches_target(path, self.cfg):
                return path
            failed.append(str(path))
        else:
            for base_path in [self.cfg.SRCDIR, *self.cfg.INCFLAGS]:
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
                    if candidate.is_file(self.cfg.vfs) and source_matches_target(candidate, self.cfg):
                        return candidate
                    failed.append(str(candidate))

        raise RuntimeError(f"Unable to locate module {modname}: " + ", ".join(failed))

class SourceFile:
    @staticmethod
    def get(
        path: Path,
        cfg: BuildConfig,
        type: SourceType | None = None,
        modname: str | None = None,
        inherited_dircfg: DirectoryConfig | None = None,
    ) -> SourceFile:
        if not source_matches_target(path, cfg):
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
            if type and file.type and type != file.type:
                raise Exception(f"type mismatch: new type {type}; old type {file.type}")
            if modname and file.modname and modname != file.modname:
                raise Exception("modname mismatch")
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
        self.path         = path
        self.dirname      = path.parent
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
        self.cmhash = None

        if path.is_absolute():
            file_parts = list(path.parts)
        else:
            file_parts = list(path.relative_to(cfg.SRCDIR).parts)

        for i, part in enumerate(file_parts):
            if part == "..":
                file_parts[i] = "__PARENT__"
        file = Path(*file_parts)
        
        self.objpath     = cfg.OBJDIR / file.with_suffix('.o')

        if modname:
            self.cmpath  = cfg.OBJDIR / mod2cm(modname, cfg.SRCDIR)
        else:
            self.cmpath  = cfg.OBJDIR / file.with_suffix(".pcm")
        
        self.output_path = self.cmpath if self.type in [SourceType.USER_HEADER, SourceType.SYSTEM_HEADER, SourceType.GENERATED_HEADER] else self.objpath
        self.infofile    = cfg.OBJDIR / file.with_suffix(".info")
        self.makefile    = cfg.OBJDIR / file.with_suffix(".make")
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
        
        self.need_recompile = False
        for depname in data['deps']:
            if depname.startswith('file:'):
                dep = Path(depname[5:])

                if SourceFile.get(dep, cfg).mtime >= infofile_mtime:
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
                if hfile.mtime(cfg.vfs) >= infofile_mtime:
                    self.need_recompile = True
                self.deps[hfile] = None

            else:
                raise Exception(f"unrecognized dep type: {depname}")
            
        if self.up_to_date is None:
            self.up_to_date = True

    async def build(self, target: Target, cfg: BuildConfig) -> None:
        """Build this source for target/cfg; publish metadata only after success."""
        target.add_config(self.dircfg(), parent=self.job)
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
                await self.compile(target, cfg)
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
            'tags': sorted(cfg.TAGS),
            'deps': deps
        }
        if not cfg.USECLANG and self.type not in (SourceType.C, SourceType.ASM):
            out['std_header_unit'] = cfg.STD_HEADER_UNIT
        module_types = {dep.name: dep.type.value for dep in self.deps
                        if isinstance(dep, ModuleDep) and dep.type is not None}
        if module_types:
            out['module_types'] = module_types
        #print(out)
        atomic_write(self.infofile, json.dumps(out, indent=2) + '\n', cfg.vfs)

    def dircfg(self) -> DirectoryConfig | None:
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
        flags = []

        buildvars = self.dircfg().buildvars
        if 'CFLAGS' in buildvars:
            cflags = buildvars['CFLAGS']

            cflags = map(lambda flag: re.sub(self.IFLAG_RE, '-idirafter', flag, count=1), cflags)
            flags.extend(cflags)

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
        cmd = cfg.CXX
        args = [cmd]
        if self.type in (SourceType.C, SourceType.ASM):
            cmd = cfg.CC
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
        # Let the reporter emit its banner before this backend's BUILDING line.
        self.job.session.compilation_started = True
        self.header_deps = {}

        if cfg.USECLANG:
            await self.compile_clang(target, cfg)
        else:
            await self.compile_gcc(target, cfg)

    MODULE_MAPPER_LINE_RE = re.compile(r'^([A-Z-]+)\b(.*)')
    async def compile_gcc(self, target: Target, cfg: BuildConfig) -> None:
        """Compile with GCC, resolving mapper dependencies through target's jobs."""
        if self.type in (SourceType.C, SourceType.ASM):
            await self.compile_gcc_c(cfg)
            return
        self.job.message(f"BUILDING {self.type} {self.path}...")
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
                               color_diagnostics=True)
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
            if not args[0].startswith('/') or self.std_header_variant or self.std_module_variant:
                header = HeaderDep.get(Path(args[0]), cfg)
                self.deps[header] = None
                self.header_deps[header] = None
            return reply
        if verb == 'MODULE-REPO':
            return f'PATHNAME {cfg.OBJDIR}'
        if verb == 'MODULE-IMPORT':
            name = args[0]
            module = CompiledModule.get(name, cfg)
            digest = await module.build(target, inherited_dircfg=self.dircfg(), parent=self.job)
            self.deps[ModuleDep(name, digest)] = None
            return f'PATHNAME {module.cmpath.relative_to(cfg.OBJDIR)}'
        if verb == 'MODULE-EXPORT':
            if self.std_header_variant or self.std_module_variant:
                return 'PATHNAME ' + shlex.quote(str(self.cmpath.relative_to(cfg.OBJDIR)))
            # Path joining maps absolute header names under SYSTEM/, matching
            # SourceFile.cmpath. GCC expects a path relative to MODULE-REPO.
            module_path = cfg.OBJDIR / mod2cm(args[0], cfg.SRCDIR)
            return f'PATHNAME {module_path.relative_to(cfg.OBJDIR)}'
        if verb == 'MODULE-COMPILED':
            return 'OK'
        raise RuntimeError(f'Unknown GCC mapper request: {verb}')

    async def compile_gcc_c(self, cfg: BuildConfig) -> None:
        """Compile a C/assembly input with cfg and load its header depfile."""
        self.job.message(f"BUILDING {self.type} {self.path}...")
        await run_compiler(self.job, self.compiler_cmd(cfg), color_diagnostics=True)
        self.process_makefile_deps()

    async def compile_clang(self, target: Target, cfg: BuildConfig) -> dict[ModuleDep | HeaderDep, None]:
        """Compile using cfg's wrapper or legacy scanner, scheduling under target."""
        self.job.message(f"BUILDING {self.type} {self.path}...")
        self.clang_module_files = {}
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
                               color_diagnostics=True)
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
                await run_compiler(self.job, object_command, color_diagnostics=True)
        self.process_makefile_deps()
        return self.deps

    async def resolve_clang_request(self, request: object, target: Target, cfg: BuildConfig) -> dict[str, object]:
        """Resolve request for target/cfg and include transitive named-module paths."""
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
            if self.cfg.CLANG_WRAPPER or self.std_module_variant:
                # PCMs are tracked by ModuleDep hashes, not as input headers.
                if rule.endswith('.pcm'):
                    continue
                if self.cfg.vfs.abspath(rule) == self.cfg.vfs.abspath(self.path):
                    continue
                path = Path(rule)
                dep = HeaderDep.get(path, self.cfg)
                self.deps[dep] = None
                if not path.is_absolute():
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

class DirectoryConfig:
    CACHE_VERSION = 1

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
        
        buildpy_file = self.dir / 'BUILD.py'
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
            ALLOWED = ('LDFLAGS', 'CFLAGS', 'PKGCONFIG')
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
        dircfg = DirectoryConfig.get(dirname, target.cfg, log=parent)

        target.add_config(dircfg, parent=parent)
        cppfile = self.find_cpp(self.path, target.cfg)
        debug_log('find_cpp', self.path, '-->', cppfile, log=parent)
        if cppfile:
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
        for path in find_files(self.paths, suffixes=[".cc", ".cpp", ".c"], vfs=cfg.vfs):
            if source_matches_target(path, cfg):
                self.process_file(path, cfg)

        asyncio.run(self.add_standard_modules(cfg))
        return json.dumps(self.entries, indent=2)

    async def add_standard_modules(self, cfg: BuildConfig) -> None:
        """Add installed SDK sources to cfg's IDE database so clangd builds its own PCMs."""
        directory = DirectoryConfig.get(Path('.'), cfg)
        flags = header_unit_flags([*cfg.CXXFLAGS, *cfg.INCFLAGS], directory.buildvars.get('CFLAGS', []))
        session = BuildSession(cfg.JOBS, verbose=cfg.VERBOSE)

        async def discover(job: Job) -> None:
            """Discover optional SDK modules through job without compiling them."""
            for name in ('std', 'std.compat'):
                source = await cfg.gcc_std_modules.resolve(name, cfg.CXX, flags,
                    str(cfg.DEPDIR / 'gcc-std-modules'), job, clang=cfg.USECLANG, optional=True)
                if source:
                    self.process_file(Path(source), cfg, modname=name)

        session.schedule('ide-standard-modules', discover)
        await session.finish()

    def process_file(self, path: Path, cfg: BuildConfig, *, modname: str | None = None) -> None:
        """Record path's IDE command; modname identifies an external SDK module."""
        # path = os.path.normpath(os.path.join(basepath, filepath))
        file = SourceFile.get(path, cfg, type=SourceType.MODULE if modname else None,
                              modname=modname,
                              inherited_dircfg=DirectoryConfig.get(Path('.'), cfg) if modname else None)
        if file in self.processed_files:
            return
        
        self.processed_files.add(file)

        # dirpath = os.path.dirname(filepath)
        # filename = os.path.basename(filepath)
        compilation_cmd = [str(cmd) for cmd in file.compiler_cmd_clang(cfg)]
        if modname and not cfg.USECLANG:
            # GCC's query-driver probe understands c++, but not c++-module.
            # clangd's module builder selects the module-interface action itself.
            compilation_cmd = ['-xc++' if arg == '-xc++-module' else arg for arg in compilation_cmd]
        # clangd builds its own BMIs. The normal build cache may contain GCC CMIs
        # or Clang BMIs made with a different compiler/configuration.
        compilation_cmd = [arg for arg in compilation_cmd
                           if arg != f'-fprebuilt-module-path={cfg.OBJDIR}']

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
        print("file", path)
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

    # puts "modname #{modname.inspect}"
    path = modname.replace('.', '/')

    if ':' in modname:
        path = path.replace(':', '/')
        
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
    return [Path(entry.path) for entry in sorted(cfg.vfs.scandir(path), key=lambda entry: entry.name)
            if entry.is_file and entry.name.endswith(suffixes)
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


def build_targets(path: Path, cfg: BuildConfig) -> None:
    """Build path or each directory selected by path/... independently using cfg."""
    for selected in expand_target_pattern(path, cfg):
        if path.name == '...':
            build(selected, cfg, artifact=package_artifact(selected, cfg))
        else:
            build(selected, cfg)


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
        for directory, files in groups.items():
            label = mkpath(directory, vfs=cfg.vfs)
            if not files:
                print(f'? {label} [no test files]', flush=True)
                continue
            sources = [mkpath(file, vfs=cfg.vfs) for file in sorted(files, key=str)]
            target = Target(directory, cfg)
            try:
                target.compile_many([main_path, *sources])
                binary = target.link(publish=False,
                    artifact=package_artifact(directory, cfg, sources=sources))
                # A subprocess lets later packages run after a test failure; tests run in their own directory.
                sys.stdout.flush()
                result = subprocess.run([cfg.vfs.abspath(binary)], cwd=str(directory), check=False)
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
        JOBS=getattr(args, "jobs", 1),
        REBUILD=getattr(args, 'rebuild', False),
        VERBOSE=args.verbose,
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
        
        build_targets(target, cfg)
    
    elif args.cmd == 'run':
        file = args.path
        target = mkpath(file, vfs=cfg.vfs)
        oldwd = None
        if ROOT != ".":
            oldwd = cfg.vfs.getcwd()
            cfg.vfs.chdir(ROOT)
        executable = build(target, cfg, publish=False)
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
        run_tests(dirs, cfg)

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
            warn(error)
        raise SystemExit(max(1, getattr(error, 'returncode', 1))) from None
    except KeyboardInterrupt:
        raise SystemExit(130) from None


if __name__ == '__main__':
    main()
