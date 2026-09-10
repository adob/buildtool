#!/usr/bin/env python3

from __future__ import annotations
import os
import asyncio
import json
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
    from .scheduler import BuildSession, Job
    from .memory import MemoryBudget
else:
    from vfs import FileSystem, RealFileSystem, MemoryFileSystem
    from clang_mapper import compile_with_mapper_async
    from compiler import mapper_pipe, run_compiler
    from scheduler import BuildSession, Job
    from memory import MemoryBudget

_DEFAULT_VFS = RealFileSystem()


ROOT = os.path.dirname(os.path.realpath(sys.argv[0]))

DEBUG_LOG = False

VCPKG_INCLUDE_RE = r"^vcpkg\/installed\/[a-z0-9-]+\/include\/([^\/]+)\/"


COMPILE_FLAGS = ["-pthread", "-fnon-call-exceptions", "-g",
            "-Wall", "-Wextra", "-Wconversion", 
            "-Wno-sign-compare", "-Wno-deprecated", "-Wno-sign-conversion",
            "-Wno-missing-field-initializers",
            "-Werror=shift-count-overflow",
            "-Werror=return-type", "-Wno-unused-parameter"
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
    memory: MemoryBudget | None
    source_files: dict[Path, SourceFile]
    compiled_modules: dict[str, CompiledModule]
    directory_configs: dict[Path, DirectoryConfig]
    header_deps: dict[Path, HeaderDep]
    compiler_commands: dict[SourceFile, list[str]]

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
        memory: MemoryBudget | None = None,
        vfs: FileSystem = _DEFAULT_VFS,
    ) -> None:
        self.vfs = vfs
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
        self.memory = memory
        self.source_files = {}
        self.compiled_modules = {}
        self.directory_configs = {}
        self.header_deps = {}
        self.compiler_commands = {}

    def reset_build_state(self) -> None:
        """Clear this configuration's caches, retaining filesystem contents.

        Create new targets and sources before starting the next build.
        """
        self.source_files.clear()
        self.compiled_modules.clear()
        self.directory_configs.clear()
        self.header_deps.clear()
        self.compiler_commands.clear()

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
        self.srcpath = target.mod2src(self.name, self.type)
        module_job = target.schedule_compilation_job(
            self.srcpath, type=self.type, modname=self.name,
            inherited_dircfg=inherited_dircfg, parent=parent)
        await parent.wait_for_dependency(module_job)
        self.srcfile = target.job_sources[module_job]
        self.cmpath = self.srcfile.cmpath
        if self.cmhash is None:
            self.cmhash = sha256_file(self.cmpath, target.cfg.vfs)
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
            self.session = BuildSession(self.cfg.JOBS, memory=self.cfg.memory)
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
            if path.suffix in CCFILE_SUFFIXES:
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

    def link(self) -> Path:
        dirname = self.path.parent
        #buildvars = DirectoryConfig.get(dirname).buildvars

        suffix = self.cfg.SUFFIX
        extra_flags = []
        
        if self.cfg.OUTFILE is None:
            name = self.path.name + suffix
        else:
            name = self.cfg.OUTFILE
        ofile = self.cfg.OBJDIR / "bin" / name
        public_file = self.cfg.BINDIR / name

        ofile_mtime = ofile.mtime(self.cfg.vfs)
        if self.most_recent_output_mtime >= ofile_mtime or THIS_MTIME > ofile_mtime:
            lflags = self.get_linkflags()
            self.cfg.vfs.makedirs(ofile.parent, exist_ok=True)
            print("LINKING", ofile)
            shell(self.cfg.CXX, *extra_flags, *self.objs, *lflags, f"-o{ofile}")
        self.cfg.vfs.makedirs(public_file.parent, exist_ok=True)
        link_target = os.path.relpath(self.cfg.vfs.abspath(ofile),
                                      self.cfg.vfs.abspath(public_file.parent))
        atomic_symlink(public_file, link_target, self.cfg.vfs)
        return public_file

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
    

    def mod2src(self, modname: str | None, type: SourceType) -> Path:
        path = mod2path(modname, type)
        failed = []

        if path.is_absolute():
            if path.exists(self.cfg.vfs):
                return path
            failed.append(str(path))
        else:
            for base_path in [self.cfg.SRCDIR, *self.cfg.INCFLAGS]:
                if isinstance(base_path, str):
                    base_path = base_path.removeprefix("-I").removeprefix("-iquote")
                    base_path = Path(base_path)

                full_path = base_path / path
                if full_path.exists(self.cfg.vfs):
                    return full_path
                
                failed.append(str(full_path))

                srcfile2 = full_path.parent / full_path.stem / full_path.name
                if srcfile2.exists(self.cfg.vfs):
                        return srcfile2
                failed.append(str(srcfile2))

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
        
        if data['command'] != self.compiler_cmd(cfg):
            self.up_to_date = False
            self.need_recompile = True
            debug_log("compiler command changed %s != %s" % (data['command'], self.compiler_cmd(cfg)), log=self.job)
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
        modules = []
        for dep in self.deps:
            if isinstance(dep, ModuleDep):
                mod = CompiledModule.get(dep.name, cfg, dep.type)
                path = target.mod2src(mod.name, mod.type)
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
            'deps': deps
        }
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
        self.job.message(f"{self.path} done in {time.perf_counter() - start:.2f} seconds")

    async def gcc_mapper_request(self, verb: str, args: list[str], target: Target, cfg: BuildConfig) -> str:
        """Answer one GCC verb/args request, scheduling imports under target/cfg."""
        if verb == 'HELLO':
            return 'HELLO 1 buildtool.py'
        if verb == 'INCLUDE-TRANSLATE':
            if not args[0].startswith('/'):
                header = HeaderDep.get(Path(args[0]), cfg)
                self.deps[header] = None
                self.header_deps[header] = None
            return 'BOOL TRUE'
        if verb == 'MODULE-REPO':
            return f'PATHNAME {cfg.OBJDIR}'
        if verb == 'MODULE-IMPORT':
            name = args[0]
            module = CompiledModule.get(name, cfg)
            digest = await module.build(target, inherited_dircfg=self.dircfg(), parent=self.job)
            self.deps[ModuleDep(name, digest)] = None
            return f'PATHNAME {module.cmpath.relative_to(cfg.OBJDIR)}'
        if verb == 'MODULE-EXPORT':
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
        if cfg.CLANG_WRAPPER:
            self.deps = {}
            self.vcpkgs = set()
            cfg.vfs.makedirs(self.cmpath.parent, exist_ok=True)
            await compile_with_mapper_async(
                self.job, cfg.CLANG_WRAPPER, self.compiler_cmd_clang(cfg),
                lambda request: self.resolve_clang_module(request, target, cfg))
        else:
            await self.clang_get_deps(target, cfg)
            await run_compiler(self.job, self.compiler_cmd(cfg), color_diagnostics=True)
        self.process_makefile_deps()
        return self.deps

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
        args = [CLANG_PATH + CLANG_SCAND_DEPS, "-format=p1689", "--", cfg.CXX, *extra_args, f"-fprebuilt-module-path={cfg.OBJDIR}", *CXXFLAGS, *INCFLAGS, "-o"+str(self.objpath), "-c", self.path]

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
            return self.deps, header_units

    def process_makefile_deps(self) -> None:
        if not self.cfg.CLANG_WRAPPER and self.type in [SourceType.USER_HEADER, SourceType.SYSTEM_HEADER]:
            return
        text = self.makefile.read_text(self.cfg.vfs)
        rules = parse_makefile_rules(text)
        for rule in rules:
            if self.cfg.CLANG_WRAPPER:
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
        cppfile = self.find_cpp(self.path, target.cfg.vfs)
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

    def find_cpp(self, hfile: Path, vfs: FileSystem) -> Path | None:
        if hfile.suffix not in HFILE_SUFFIXES:
            return None
        
        basename = hfile.with_suffix('')
        for ext in [".cc", ".cpp", ".c"]:
            cppfile = basename.with_extra_suffix(ext)
            if cppfile.exists(vfs):
                return cppfile
            
        #print("!!!!", list(hfile.parts), 'include' in hfile.parts)
        if "include" in hfile.parts:
            parts = list(hfile.parts)
            include_index = parts.index('include')
            parts[include_index] = 'src'
            newpath = Path(*parts)

            if newpath.parent.is_dir(vfs):
                return self.find_cpp(newpath, vfs)
            
            # project/include/project/file.h -> project/src/file.h
            if include_index > 0 and include_index < len(parts) - 2 and parts[include_index-1] == parts[include_index+1]:
                parts.pop(include_index+1)
                return self.find_cpp(Path(*parts), vfs)
        
        if "Inc" in hfile.parts:
            parts = list(hfile.parts)
            include_index = parts.index('Inc')
            parts[include_index] = 'Src'
            newpath = Path(*parts)

            if newpath.parent.is_dir(vfs):
                return self.find_cpp(newpath, vfs)

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
            self.process_file(path, cfg)

        return json.dumps(self.entries, indent=2)

    def process_file(self, path: Path, cfg: BuildConfig) -> None:
        # path = os.path.normpath(os.path.join(basepath, filepath))
        file = SourceFile.get(path, cfg)
        if file in self.processed_files:
            return
        
        self.processed_files.add(file)

        # dirpath = os.path.dirname(filepath)
        # filename = os.path.basename(filepath)
        compilation_cmd = [str(cmd) for cmd in file.compiler_cmd_clang(cfg)]

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
    
def shell(*args: str | os.PathLike[str], log: Job | None = None) -> str:
    """Run args synchronously, returning stdout and sending diagnostics to log."""
    if log is not None:
        log.message(shlex.join(list(map(str, args))))
        result = subprocess.run(args, capture_output=True, text=True)
        log.write(result.stderr)
        result.check_returncode()
        return result.stdout
    cmd = " ".join(shlex.quote(str(arg)) for arg in args)
    print(cmd)
    result = subprocess.run(args, shell=False, text=True, stdin=0, stdout=subprocess.PIPE, stderr=2)
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

def build(path: Path, cfg: BuildConfig) -> Path:
    name = path.with_suffix('')
    target = Target(name, cfg)
    target.compile(path)
    
    return target.link()

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
    
    for filename in find_files(dirs, suffixes = ('_test.cc', '_test.cpp'), vfs=cfg.vfs):
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
    run_tool(TESTMAIN, dirs, cfg)

def run_benchmarks(dirs: list[str], cfg: BuildConfig) -> None:
    run_tool(BENCHMAIN, dirs, cfg)
        
def sha256_file(path: Path, vfs: FileSystem) -> str:
    return vfs.sha256(path)


def reset_build_state(cfg: BuildConfig) -> None:
    """Start a fresh build invocation for the specified configuration."""
    cfg.reset_build_state()

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
) -> None:
    """Parse CLI arguments using the supplied compiler, path, flag and VFS defaults."""

    buildcfg = Release
    parser = argparse.ArgumentParser(
        prog        = 'buildtool',
        description = 'Utility for compiling and running C++ programs'
    )
    parser.add_argument('--debug-log', action='store_true', help='enable debug logging')
    subparsers = parser.add_subparsers(dest='cmd')
    
    build_parser = subparsers.add_parser('build', help='build the specified binary or library')
    build_parser.add_argument('path', help="path/to/file.cc")
    build_parser.add_argument('--release', '-r', action='store_const', dest='buildtype', const='release', help='build in release mode')
    build_parser.add_argument('--debug', '-d', action='store_const', dest='buildtype', const='debug', help='build in debug mode')
    build_parser.add_argument('--library', action='store_true', help='build in library mode')
    build_parser.add_argument('--clang', action='store_true', help='build with clang')
    build_parser.add_argument('args', nargs='*')

    run_parser = subparsers.add_parser('run', help='run the specified binary')
    run_parser.add_argument('path', help="path/to/file.cc")
    run_parser.add_argument('--release', '-r', action='store_const', dest='buildtype', const='release', help='build in release mode')
    run_parser.add_argument('--debug', '-d', action='store_const', dest='buildtype', const='debug', help='build in debug mode')
    run_parser.add_argument('--clang', action='store_true', help='build with clang')
    run_parser.add_argument('args', nargs='*')

    ide_parser = subparsers.add_parser('ide', help='generate a compile_commands.json compilation database')
    ide_parser.add_argument('paths', nargs='*')

    test_parser = subparsers.add_parser('test', help='run tests in the specified directories or files')
    test_parser.add_argument('dirs', nargs='+')
    test_parser.add_argument('--release', '-r', action='store_const', dest='buildtype', const='release', help='build in release mode')
    test_parser.add_argument('--debug', '-d', action='store_const', dest='buildtype', const='debug', help='build in debug mode')
    test_parser.add_argument('--clang', action='store_true', help='build with clang')

    bench_parser = subparsers.add_parser('bench', help='run benchmarks in the specified directories or files')
    bench_parser.add_argument('dirs', nargs='+')
    bench_parser.add_argument('--release', '-r', action='store_const', dest='buildtype', const='release', help='build in release mode')
    bench_parser.add_argument('--debug', '-d', action='store_const', dest='buildtype', const='debug', help='build in debug mode')
    bench_parser.add_argument('--clang', action='store_true', help='build with clang')

    for command_parser in (build_parser, run_parser, test_parser, bench_parser):
        command_parser.add_argument("-j", "--jobs", type=int, default=os.cpu_count() or 1,
                                    help="maximum active compiler processes (also capped by memory)")

    args = parser.parse_args()
    if getattr(args, "jobs", 1) < 1:
        parser.error("--jobs must be at least 1")

    g = globals()
    buildtype = Release
    if args.cmd in ['build', 'run', 'test', 'bench']:
        if args.buildtype == 'debug':
            buildtype = Debug
        else:
            buildtype = Release

        if args.clang:
            USECLANG = True
            CXX = CLANG_PATH + CLANGXX
            CC = CLANG_PATH + CLANG
    
    if args.debug_log:
        g['DEBUG_LOG'] = True

    for key, val in buildtype.__dict__.items():
        if key.startswith('__'):
            continue

        globals()[key] = val

    build_dir = "release"
    if buildtype == Debug:
        build_dir = "debug"
    if USECLANG:
        build_dir += "+clang"

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
        memory=MemoryBudget() if args.cmd in ('build', 'run', 'test', 'bench') else None,
        vfs=vfs,
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
        
        build(target, cfg)
    
    elif args.cmd == 'run':
        file = args.path
        target = mkpath(file, vfs=cfg.vfs)
        oldwd = None
        if ROOT != ".":
            oldwd = cfg.vfs.getcwd()
            cfg.vfs.chdir(ROOT)
        bin = cfg.vfs.abspath(build(target, cfg))
        if oldwd:
            cfg.vfs.chdir(oldwd)
        os.execv(bin, [bin] + args.args)

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
