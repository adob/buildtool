#!/usr/bin/env python3

from __future__ import annotations
import os
import json
import subprocess
import shlex
import argparse
import sys
import re
from datetime import datetime
import time
from typing import Dict, Set
import pathlib
from enum import Enum, StrEnum
from dataclasses import dataclass
from uuid import uuid4

if __package__:
    from .vfs import FileSystem, RealFileSystem, MemoryFileSystem
    from .clang_mapper import compile_with_mapper
else:
    from vfs import FileSystem, RealFileSystem, MemoryFileSystem
    from clang_mapper import compile_with_mapper

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

def auto_str(cls):
    def __str__(self):
        clsname = cls.__name__
        attrs = ", ".join(f"{k}={v!r}" for k, v in self.__dict__.items())
        return f"{clsname}({attrs})"
    cls.__str__ = __str__
    return cls

@auto_str
class BuildConfig:
    def __init__(self,
                 CC=CC,
                 CXX=CXX,
                 COMPILE_FLAGS=[],
                 CFLAGS=Release.CFLAGS,
                 CXXFLAGS=CXXFLAGS,
                 LDFLAGS=LDFLAGS,
                 OBJDIR=Release.OBJDIR, 
                 DEPDIR=Release.DEPDIR, 
                 SRCDIR=SRCDIR, 
                 BINDIR=BINDIR, 
                 INCFLAGS=INCFLAGS,
                 SUFFIX="",
                 OUTFILE=None,
                 USECLANG=False,
                 CLANG_WRAPPER=None,
                 vfs: FileSystem = _DEFAULT_VFS):
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
        self.active_modules = set()
        self.source_files: dict[Path, SourceFile] = {}
        self.compiled_modules: dict[str, CompiledModule] = {}
        self.directory_configs: dict[Path, DirectoryConfig] = {}
        self.header_deps: dict[Path, HeaderDep] = {}
        self.compiler_commands: dict[SourceFile, list[str]] = {}

    def reset_build_state(self):
        """Clear this configuration's caches, retaining filesystem contents.

        Create new targets and sources before starting the next build.
        """
        self.source_files.clear()
        self.compiled_modules.clear()
        self.directory_configs.clear()
        self.header_deps.clear()
        self.compiler_commands.clear()
        self.active_modules.clear()

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
        
    
    def __init__(self, *paths: str):
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
    def parent(self):
        parent = self.path.parent
        if parent is None:
            return None
        
        return Path(parent)
    
    @property
    def stem(self):
        return self.path.stem

    @property
    def anchor(self):
        return self.path.anchor

    #    paths = [str(p) for p in paths]
    #    normalized = os.path.normpath('/'.join(paths))

    #    #print("__INIT__", paths)
    #    super().__init__(normalized)

    def with_extra_suffix(self, suffix: str) -> 'Path':
        return self.with_name(self.name + suffix)
    
    def try_stat(self, vfs: FileSystem):
        try:
            return vfs.stat(self.path)
        except FileNotFoundError:
            return None
        
    def mtime(self, vfs: FileSystem):
        stat = self.try_stat(vfs)
        if stat is None:
            return 0
        return stat.st_mtime
    
    def exists(self, vfs: FileSystem):
        return self.try_stat(vfs) is not None
    
    def __str__(self):
        return  str(self.path)
    
    def __truediv__(self, other):
        if not isinstance(other, Path):
            other = Path(other)

        p = other.path
        if p.is_absolute():
            p = pathlib.Path("SYSTEM") / p.relative_to(p.anchor)

        return Path(self.path / p)
    
    def __rtruediv__(self, other):
        if isinstance(other, Path):
            return other / self
            
        return Path(other / self.path)
    
    def relative_to(self, other):
        if isinstance(other, Path):
            #print("relative_to", self.path, other, Path(self.path.relative_to(other.path)))
            return Path(self.path.relative_to(other.path))
        
        #print("relative_to", self.path, other, Path(self.path.relative_to(other)))
        return Path(self.path.relative_to(other))
    
    def with_suffix(self, suffix):
        return Path(self.path.with_suffix(suffix))
    
    def with_name(self, name):
        return Path(self.path.with_name(name))
    
    def read_text(self, vfs: FileSystem):
        return vfs.read_text(self.path)
    
    def is_dir(self, vfs: FileSystem):
        return vfs.is_dir(self.path)
    
    def is_file(self, vfs: FileSystem):
        return vfs.is_file(self.path)
    
    def is_absolute(self):
        return self.path.is_absolute()
    
    def __fspath__(self) -> str:
        return self.path.__fspath__()
    
    def __eq__(self, other):
        if isinstance(other, Path):
            return self.path == other.path
        
        return NotImplemented
    
    def __hash__(self):
        return hash(self.path)

class CompiledModule:
    @staticmethod
    def get(name: str, cfg: BuildConfig, type=None):
        mod = cfg.compiled_modules.get(name)
        if mod:
            return mod
        mod = CompiledModule(name, type)
        cfg.compiled_modules[name] = mod
        return mod
    
    def __init__(self, name: str, type:SourceType = None):
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

    def build(self, target, inherited_dircfg: DirectoryConfig):
        debug_log("CompiledModule.build()")
        if self.cmhash:
            return self.cmhash
        
        if self.name in target.cfg.active_modules:
            raise RuntimeError(f"Cyclic module import: {self.name}")
        target.cfg.active_modules.add(self.name)
        try:
            self.srcpath = target.mod2src(self.name, self.type)
            self.srcfile = target.compile(self.srcpath, type=self.type, modname=self.name, inherited_dircfg=inherited_dircfg)
        finally:
            target.cfg.active_modules.remove(self.name)

        self.cmpath = self.srcfile.cmpath
        self.cmhash = sha256_file(self.cmpath, target.cfg.vfs)
        return self.cmhash

class Target:
    def __init__(self, path: Path, cfg: BuildConfig):
        self.path = path
        self.srcfiles = set()
        self.objs = []
        self.processed_files = set()
        self.configs = set()
        self.most_recent_output_mtime = 0
        self.extra_linkflags = []
        self.cfg = cfg

    def compile(self, path: Path, type=None, modname: str=None, inherited_dircfg: DirectoryConfig=None):
        if type is not None:
            pass
        elif path.suffix in CCFILE_SUFFIXES:
            type = SourceType.CPP
        elif path.suffix in ('.c'):
            type = SourceType.C
        elif path.suffix in ('.S', '.s'):
            type = SourceType.ASM
        else:
            warn("unrecognized file type: %s" % path)
            exit(1)

        file = SourceFile.get(path, self.cfg, type=type, modname=modname, inherited_dircfg=inherited_dircfg)
        if file in self.processed_files:
            return
        self.processed_files.add(file)

        debug_log(f"processing {path} type={type}")
        file.build(self, self.cfg)

        if type not in [SourceType.SYSTEM_HEADER, SourceType.USER_HEADER]:
            self.objs.append(file.objpath)

        if file.output_mtime > self.most_recent_output_mtime:
            self.most_recent_output_mtime = file.output_mtime
        
        return file

    def link(self):
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

    def add_config(self, config):
        if config in self.configs:
            return
        self.configs.add(config)

        if config.linkflags:
            self.extra_linkflags.extend(config.linkflags)


    def get_linkflags(self):
        lflags = list(self.cfg.LDFLAGS) + self.extra_linkflags

        extra = []

        for flag in lflags:
            if flag.startswith('-L'):
                rpath_flag = '-Wl,-rpath,' + flag[2:]
                extra.append(rpath_flag)

        return lflags + extra
    

    def mod2src(self, modname: str, type: SourceType):
        debug_log("mod2src", modname, type)
        path = mod2path(modname, type)
        debug_log("TRYING TO FIND module source file", path)
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
                debug_log("TRYING", full_path)
                if full_path.exists(self.cfg.vfs):
                    return full_path
                
                failed.append(str(full_path))

                srcfile2 = full_path.parent / full_path.stem / full_path.name
                if srcfile2.exists(self.cfg.vfs):
                        return srcfile2
                failed.append(str(srcfile2))

        warn(f"FATAL: Unable to locate module {modname}: the following files do not exist: %s" % ', '.join(failed))
        exit(1)

class SourceFile:
    @staticmethod
    def get(path: Path, cfg: BuildConfig, type: SourceType=None, modname: str=None, inherited_dircfg: DirectoryConfig=None):
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

    def __init__(self, path: Path, type: SourceType, modname: str, cfg: BuildConfig, inherited_dircfg: DirectoryConfig=None):
        self.cfg = cfg
        self.path         = path
        self.dirname      = path.parent
        self.type         = type
        self.modname      = modname
        self.processed    = False
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

    def check_up_to_date(self, cfg: BuildConfig):
        if self.up_to_date is not None:
            return
        
        infofile_mtime = self.infofile.mtime(cfg.vfs)
        if self.mtime >= infofile_mtime:
            self.up_to_date = False
            self.need_recompile = True
            debug_log(f"#{self.path} NEED RECOMPILE BECAUSE MTIME={self.mtime} > INFOFILE_MTIME={infofile_mtime}")
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
            debug_log("compiler command changed %s != %s" % (data['command'], self.compiler_cmd(cfg)))
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

    def build(self, target, cfg: BuildConfig):
        if self.processed:
            return
        self.processed = True

        target.add_config(self.dircfg())

        self.check_up_to_date(target.cfg)
        if self.up_to_date:
            return

        # A dependency build can discover a changed module interface. Check it
        # before deciding whether the importing source needs recompilation.
        if not self.need_recompile:
            self.build_deps(target, cfg)
        
        if self.need_recompile:
            objdir = self.objpath.parent
            cfg.vfs.makedirs(objdir, exist_ok=True)
            self.compile(target, cfg)
            self.update(target.cfg)
            self.output_mtime = self.output_path.mtime(cfg.vfs)

            for header_dep in self.header_deps:
                header_dep.build(target)

    def build_deps(self, target, cfg: BuildConfig):
        for dep in self.deps:
            if isinstance(dep, ModuleDep):
                mod = CompiledModule.get(dep.name, cfg, dep.type)
                new_hash = mod.build(target, inherited_dircfg=self.dircfg())

                if new_hash != dep.sha256:
                    self.need_recompile = True

            elif isinstance(dep, HeaderDep):
                dep.build(target)

            else:
                raise Exception(f"unrecognized dep {dep}")

    def update(self, cfg: BuildConfig):
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

    def dircfg(self):
        if self.dirname.is_absolute():
            return self.inherited_dircfg
        
        return DirectoryConfig.get(self.dirname, self.cfg)

    def compiler_cmd(self, cfg: BuildConfig):
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
    def compiler_extra_args(self):
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
    
    def add_include(self, dirparts, flags):
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
        

    def compiler_cmd_clang(self, cfg: BuildConfig, extra_args=[]):
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
                    

    def compiler_cmd_gcc(self, cfg: BuildConfig):
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

    def compile(self, target, cfg: BuildConfig):
        self.header_deps = {}

        if cfg.USECLANG:
            self.compile_clang(target, cfg)
        else:
            self.compile_gcc(target, cfg)

    MODULE_MAPPER_LINE_RE = re.compile(r'^([A-Z-]+)\b(.*)')
    def compile_gcc(self, target, cfg: BuildConfig):
        if self.type in (SourceType.C, SourceType.ASM):
            self.compile_gcc_c(cfg)
            return
        
        # https://splichal.eu/scripts/sphinx/gcc/_build/html/gcc-command-options/c%2B%2B-modules.html
        # https://github.com/urnathan/libcody
        # https://www.open-std.org/jtc1/sc22/wg21/docs/papers/2020/p1184r2.pdf
        print(f"BUILDING {self.type} {self.path}...")
        start = time.perf_counter()
        if ".." in str(self.path):
            print("TYPE", type(self.path), self.path, id(self.path))
            raise "dots in path"

        mapper_read, compiler_write = os.pipe()
        compiler_read, mapper_write = os.pipe()

        cmd = self.compiler_cmd(target.cfg) + [f"-fmodule-mapper=<{compiler_read}>{compiler_write}"]

        cmdline = " ".join(shlex.quote(item) for item in cmd)
        print(cmdline)

        env = dict(os.environ)
        env['SOURCE_DATE_EPOCH'] = '0'

        process = subprocess.Popen(
            cmd,
            env=env,
            pass_fds=(compiler_read, compiler_write),
            stdin=0, stdout=1, stderr=2)
        
        #pid = os.spawn({ 'SOURCE_DATE_EPOCH': '0' }, cmdline, {3: compiler_read, 4: compiler_write})

        os.close(compiler_read)
        os.close(compiler_write)
        mapper_read = os.fdopen(mapper_read, 'r')
        mapper_write = os.fdopen(mapper_write, 'w')

        self.deps = {}
        self.vcpkgs = set()

        try:
            eof = False
            while not eof:
                lines = []
                while True:
                    line = mapper_read.readline()
                    if line == "":
                        eof = True
                        break
                    
                    line = line.strip()
                    # debug_log("GOT LINE <%s>" % line)

                    m = re.match(self.MODULE_MAPPER_LINE_RE, line)
                    cmd, args = m.groups()
                    args = args.strip().split()
                    # debug_log("ARGS", args)
                    lines.append((cmd, args))
                    
                    if len(args) == 0 or args[-1] != ';':
                        break

                out = []

                for line in lines:
                    cmd, args = line
                    # debug_log("CMD", cmd, args)

                    if cmd == "HELLO":
                        out.append("HELLO 1 buildtool.py")
                        
                    elif cmd == "INCLUDE-TRANSLATE":
                        file = args[0]
                        if not file.startswith('/'):
                            debug_log(f"INCLUDE-TRANSLATE {file}")
                            path = Path(file)
                            header_dep = HeaderDep.get(path, cfg)

                            self.deps[header_dep] = None
                            self.header_deps[header_dep] = None

                        out.append("BOOL TRUE")

                    elif cmd == "MODULE-REPO":
                        debug_log(f"MODULE-REPO => PATHNAME {cfg.OBJDIR}")
                        out.append(f"PATHNAME {cfg.OBJDIR}")

                    elif cmd == "MODULE-IMPORT":
                        modname = args[0].replace("'", '')
                        mod = CompiledModule.get(modname, cfg)
                        cmhash = mod.build(target, inherited_dircfg=self.dircfg())
                        self.deps[ModuleDep(modname, cmhash)] = None
                        
                        path = mod.cmpath.relative_to(cfg.OBJDIR)
                        debug_log(f"MODULE-IMPORT {self.path}: {args} => PATHNAME {path}")
                        out.append(f"PATHNAME {path}")

                    elif cmd == "MODULE-EXPORT":
                        modname = args[0]
                        #debug_log(f"MODULE-EXPORT {modname}")
                        file = modname.replace("'", '')
                        cmfile = Path(".") / mod2cm(file, cfg.SRCDIR)
                        # .replace(':', '-')
                        # if file.startswith('/'):
                        #     file = "system" + file + ".pcm"
                        # elif file.startswith("./"):
                        #     file = file[2:] + ".pcm"
                        # else:
                        #     file = file.replace('.', '/') + ".pcm"
                        debug_log(f"MODULE-EXPORT {modname} => {cmfile}")
                        out.append(f"PATHNAME {cmfile}")

                    elif cmd == "MODULE-COMPILED":
                        out.append("OK")

                    else:
                        warn(f"unknown command: {cmd}")

                if len(out) == 0:
                    continue

                s = " ;\n".join(out) + '\n'
                # debug_log("WRITING <%s>" % s)
                mapper_write.write(s)
                mapper_write.flush()

        except EOFError as ex:
            debug_log("got exception", ex)
            pass

        mapper_read.close()
        mapper_write.close()
        
        exitcode = process.wait()
        if exitcode != 0:
            exit(exitcode)

        end = time.perf_counter()
        print(f"{self.path} done in {end - start:.2f} seconds")

    def compile_gcc_c(self, cfg: BuildConfig):
        print(f"BUILDING {self.type} {self.path}...")
        
        start = time.perf_counter()
        shell(*self.compiler_cmd(cfg))
        self.process_makefile_deps()
        end = time.perf_counter()
        #print(f"done in {end - start:.2f} seconds")

    def compile_clang(self, target, cfg: BuildConfig):
        if cfg.CLANG_WRAPPER:
            self.deps = {}
            self.vcpkgs = set()
            print(f"BUILDING {self.type} {self.path}...")
            cfg.vfs.makedirs(self.cmpath.parent, exist_ok=True)
            compile_with_mapper(
                cfg.CLANG_WRAPPER, self.compiler_cmd_clang(cfg),
                lambda request: self.resolve_clang_module(request, target, cfg))
            self.process_makefile_deps()
            return self.deps
        deps, header_units = self.clang_get_deps(target, cfg)
        
        print(f"BUILDING {self.type} {self.path}...")
        start = time.perf_counter()
        cmdline = self.compiler_cmd(cfg)
        print(*cmdline)
        
        result = subprocess.run(cmdline, check=False)
        if result.returncode != 0:
            exit(result.returncode)

        end = time.perf_counter()
        #print(f"done in {end - start:.2f} seconds")

        self.process_makefile_deps()
        return deps

    def resolve_clang_module(self, request, target, cfg):
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
        cmhash = module.build(target, inherited_dircfg=self.dircfg())
        self.deps[ModuleDep(name, cmhash, source_type)] = None
        return cfg.vfs.abspath(module.cmpath)

    def clang_get_deps(self, target, cfg: BuildConfig):
        print("clang_get_deps", self.path)
        self.deps = {}
        self.vcpkgs = set()

        if self.type in [SourceType.USER_HEADER, SourceType.SYSTEM_HEADER]:
            extra_args = ["-xc++-header"]
        else:
            extra_args = ["-xc++"]
        args = [CLANG_PATH + CLANG_SCAND_DEPS, "-format=p1689", "--", cfg.CXX, *extra_args, f"-fprebuilt-module-path={cfg.OBJDIR}", *CXXFLAGS, *INCFLAGS, "-o"+str(self.objpath), "-c", self.path]

        #print("running", *args)
        result = subprocess.run(args, capture_output=True)
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
                    cmhash = mod.build(target, inherited_dircfg=self.dircfg())
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
            result = subprocess.run(args, capture_output=True)

            if result.returncode != 0:
                warn("SCANDEPS failed with cmd line:", *args)
                warn(result.stderr.decode())
                exit(1)


        p1689 = json.loads(result.stdout.decode())
        for rule in p1689["rules"]:
            
            # provides = p1689["rules"][0]["requires"]
            if self.type == 'module':
                provides = rule["provides"]
                if not provides or len(provides) != 1:
                    warn(f"wanted module with name {self.modname} in file {self.path} but got something else")
                    exit(1)

                name = provides[0]["logical-name"]
                if name != self.modname:
                    warn(f"wanted module with name {self.modname} in file {self.path} but got {name}")
                    exit(1)

            if "requires" in rule:
                reqs = rule["requires"]
                for req in reqs:
                    modname = req["logical-name"]
                    print(f"about to build dep module {modname}")
                    mod = CompiledModule.get(modname, cfg)
                    cmhash = mod.build(target, inherited_dircfg=self.dircfg())
                    self.deps[ModuleDep(modname, cmhash)] = None
            return self.deps, header_units

    def process_makefile_deps(self):
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
    def __init__(self, name, sha256, type=None):
        self.name = name
        self.sha256 = sha256
        self.type = type

class DirectoryConfig:
    CACHE_VERSION = 1

    @classmethod
    def get(cls, path: Path, cfg: BuildConfig):
        if path in cfg.directory_configs:
            return cfg.directory_configs[path]
        directory = cls(path, cfg)
        directory.process()
        cfg.directory_configs[path] = directory
        return directory

    def __init__(self, path: Path, cfg: BuildConfig):
        self.cfg = cfg
        if path.is_absolute():
            self.dir = None
            return
        
        self.dir = path.relative_to(cfg.SRCDIR)

    def process(self):
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
                warn("error reading JSON %s: %s" % (json_file, str(ex)))
                exit(1)

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

            self.handle_pkgconfig(self.buildvars)
            self.cfg.vfs.makedirs(json_file.parent, exist_ok=True)
            cached = {'version': self.CACHE_VERSION, 'buildvars': self.buildvars}
            atomic_write(json_file, json.dumps(cached, indent=2), self.cfg.vfs)
        else:
            self.buildvars = cached['buildvars']

        if 'LDFLAGS' in self.buildvars:
            self.linkflags = self.buildvars['LDFLAGS']
        else:
            self.linkflags = []

    def handle_pkgconfig(self, buildvars):
        if 'PKGCONFIG' not in buildvars:
            return
        
        linkflags = list(buildvars.get('LDFLAGS', []))
        cflags = list(buildvars.get('CFLAGS', []))
        
        for pkg in buildvars['PKGCONFIG']:
            libs_flags = shlex.split(shell("pkg-config", "--libs", pkg))
            cflags_cur = self.filter_cflags(shlex.split(shell("pkg-config", "--cflags", pkg)))
            linkflags.extend(libs_flags)
            cflags.extend(cflags_cur)

        if linkflags:
            buildvars['LDFLAGS'] = linkflags

        if cflags:
            buildvars['CFLAGS'] = cflags
            # buildvars['CXXFLAGS'] = list(cflags)
            
    def filter_cflags(self, flags):
        out = []
        
        for flag in flags:
            if flag.startswith('-std='):
                continue
            
            out.append(flag)
            
        return out

class HeaderDep:
    @classmethod
    def get(cls, path: Path, cfg: BuildConfig):
        if path not in cfg.header_deps:
            cfg.header_deps[path] = cls(path)
        return cfg.header_deps[path]

    def __init__(self, path):
        #print("PATH", path, type(path))
        self.path = path
        self.built = False
        self._mtimes: dict[FileSystem, float] = {}

    def build(self, target):
        if self.built:
            return
        self.built = True
        if self.path.is_absolute():
            # External headers participate in timestamp checks but do not
            # discover project BUILD.py files or companion source files.
            return
        #debug_log("HeaderDep.build", self.path)
        
        dirname = self.path.parent
        dircfg = DirectoryConfig.get(dirname, target.cfg)

        target.add_config(dircfg)
        cppfile = self.find_cpp(self.path, target.cfg.vfs)
        debug_log('find_cpp', self.path, '-->', cppfile)
        if cppfile:
            self.cpp_path = cppfile
            target.compile(self.cpp_path)
            return

    def mtime(self, vfs: FileSystem):
        # Input headers are stable during a build. Keep their timestamps for
        # this dependency's lifetime, which ends when its BuildConfig resets.
        if vfs not in self._mtimes:
            self._mtimes[vfs] = self.path.mtime(vfs)
        return self._mtimes[vfs]

    def find_cpp(self, hfile: Path, vfs: FileSystem):
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
    def __init__(self, paths: list[Path]):
        self.paths = paths
        self.processed_files = set()
        self.entries = []

    def build(self, cfg: BuildConfig):
        for path in find_files(self.paths, suffixes=[".cc", ".cpp", ".c"], vfs=cfg.vfs):
            self.process_file(path, cfg)

        return json.dumps(self.entries, indent=2)

    def process_file(self, path, cfg: BuildConfig):
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

def find_files(paths: list[Path], suffixes: tuple[str], prefixes: tuple[str] = None, *, vfs: FileSystem):
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

        for entry in vfs.scandir(path):
            if entry.is_file and entry.name.endswith(suffixes):
                if prefixes is not None and not entry.name.startswith(prefixes):
                    continue
                yield Path(entry.path)
            elif entry.is_dir and not entry.is_symlink and not entry.name.startswith("."):
                yield from find_files([Path(entry.path)], suffixes=suffixes, prefixes=prefixes, vfs=vfs)

def atomic_write(path: Path, data: str, vfs: FileSystem):
    tmpfile = path.with_extra_suffix(".tmp")
    vfs.write_text(tmpfile, data)
    vfs.replace(tmpfile, path)

def atomic_symlink(path: Path, target: str, vfs: FileSystem):
    tmpfile = path.with_extra_suffix(f".{uuid4().hex}.tmp")
    vfs.symlink(target, tmpfile)
    try:
        vfs.replace(tmpfile, path)
    finally:
        try:
            vfs.unlink(tmpfile)
        except FileNotFoundError:
            pass

def try_read(path: Path, vfs: FileSystem):
    try:
        return path.read_text(vfs)
    except FileNotFoundError:
        return None
    
def shell(*args):
    cmd = " ".join(shlex.quote(str(arg)) for arg in args)
    print(cmd)
    result = subprocess.run(args, shell=False, text=True, stdin=0, stdout=subprocess.PIPE, stderr=2)
    if result.returncode != 0:
        exit(1)
    return result.stdout

def mod2cm(modname, srcdir=SRCDIR):
    debug_log(f"mod2cm {modname}")
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

def mod2path(modname: str, type:SourceType):
    debug_log("mod2path", modname, type)
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

def parse_makefile_rules(text):
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

def warn(*s: str):
    print(*s, file=sys.stderr)

def debug_log(*text):
    if DEBUG_LOG:
        warn(*text)

def build(path: Path, cfg: BuildConfig):
    name = path.with_suffix('')
    target = Target(name, cfg)
    target.compile(path)
    
    return target.link()

def make_compilation_database(paths: list[Path], cfg: BuildConfig):
    db = CompilationDatabase(paths)
    return db.build(cfg)

def build_compilation_database(out: Path, paths: list[Path], cfg: BuildConfig):
    data = make_compilation_database(paths, cfg)
    
    atomic_write(out, data, cfg.vfs)
    print("wrote %s" % out)


def mkpath(path: str, *, vfs: FileSystem) -> Path:
    return Path(os.path.relpath(vfs.abspath(path), vfs.abspath(ROOT)))

def run_tool(tool_path: str, dirs: list[str], cfg: BuildConfig):
    dirs = [Path(cfg.vfs.abspath(dir)) for dir in dirs]

    # change directory to root
    oldwd = None
    if ROOT != ".":
        oldwd = cfg.vfs.getcwd()
        cfg.vfs.chdir(ROOT)

    
    main_path = mkpath(tool_path, vfs=cfg.vfs)
    main_name = main_path.with_suffix('')
    target = Target(main_name, cfg)
    target.compile(main_path, SourceType.CPP)
    
    for filename in find_files(dirs, suffixes = ('_test.cc', '_test.cpp'), vfs=cfg.vfs):
        #print("building %s..." % filename)
        path = mkpath(filename, vfs=cfg.vfs)
        target.compile(path, SourceType.CPP)

    bin = target.link()
    bin = cfg.vfs.abspath(bin)
    if oldwd:
        cfg.vfs.chdir(oldwd)
    os.execv(bin, [bin])


def run_tests(dirs: list[str], cfg: BuildConfig):
    run_tool(TESTMAIN, dirs, cfg)

def run_benchmarks(dirs: list[str], cfg: BuildConfig):
    run_tool(BENCHMAIN, dirs, cfg)
        
def sha256_file(path: Path, vfs: FileSystem):
    return vfs.sha256(path)


def reset_build_state(cfg: BuildConfig):
    """Start a fresh build invocation for the specified configuration."""
    cfg.reset_build_state()

## MAIN ##
def main(
        CC=CC,
        CXX=CXX, 
        CFLAGS=CFLAGS,
        CXXFLAGS=CXXFLAGS,
        LDFLAGS=LDFLAGS,
        OBJDIR=OBJDIR,
        DEPDIR=DEPDIR, 
        SRCDIR=SRCDIR, 
        BINDIR=BINDIR,
        INCFLAGS=INCFLAGS,
        USECLANG=USECLANG, 
        SRC_ROOTS=SRC_ROOTS,
        vfs: FileSystem = _DEFAULT_VFS,
):

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

    args = parser.parse_args()

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

if __name__ == '__main__':
    main()
