#!/usr/bin/env python3

import os
import json
import hashlib
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
from functools import cache
from dataclasses import dataclass

ROOT = os.path.dirname(os.path.realpath(sys.argv[0]))

DEBUG_LOG = False

VCPKG_INCLUDE_RE = r"^vcpkg\/installed\/[a-z0-9-]+\/include\/([^\/]+)\/"


COMPILE_FLAGS = ["-pthread", "-fnon-call-exceptions", "-g",
            "-Wall", "-Wextra", "-Wconversion", 
            "-Wno-sign-compare", "-Wno-deprecated", "-Wno-sign-conversion",
            "-Wno-missing-field-initializers",
            "-Werror=shift-count-overflow",
            "-Werror=return-type",
]
CFLAGS = COMPILE_FLAGS
CLANG_CFLAGS = ["-Wno-logical-op-parentheses"]
CXXFLAGS = COMPILE_FLAGS + ["-std=c++26"]
LDFLAGS = ["-lrt"]
OBJDIR = "obj"
DEPDIR = "obj"
SUFFIX = ""

SRCDIR = "."
SRC_ROOTS = [SRCDIR]
BINDIR = "bin"
INCFLAGS = []
USECLANG = False

CXX = "clang++" if USECLANG else "g++"
CC = "clang" if USECLANG else "gcc"

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
                 OUTFILE=None):
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


    #    paths = [str(p) for p in paths]
    #    normalized = os.path.normpath('/'.join(paths))

    #    #print("__INIT__", paths)
    #    super().__init__(normalized)

    def with_extra_suffix(self, suffix: str) -> 'Path':
        return self.with_name(self.name + suffix)
    
    @cache
    def try_stat(self):
        try:
            return self.path.stat()
        except FileNotFoundError:
            return None
        
    def mtime(self):
        stat = self.try_stat()
        if stat is None:
            return 0
        return stat.st_mtime
    
    def exists(self):
        return self.try_stat() is not None
    
    def __str__(self):
        return  str(self.path)
    
    def __truediv__(self, other):
        if isinstance(other, Path):
            return Path(self.path / other.path)
            
        return Path(self.path / other)
    
    def __rtruediv__(self, other):
        if isinstance(other, Path):
            return Path(other.path / self.path)
            
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
    
    def read_text(self):
        return self.path.read_text()
    
    def is_dir(self):
        return self.path.is_dir()
    
    def is_file(self):
        return self.path.is_file()
    
    def __fspath__(self) -> str:
        return self.path.__fspath__()
    
    def __eq__(self, other):
        if isinstance(other, Path):
            return self.path == other.path
        
        return self.path == other
    
    def __hash__(self):
        return self.path.__hash__()

class CompiledModule:
    modules = {}

    @staticmethod
    def get(name: str, type=None):
        mod = CompiledModule.modules.get(name)
        if mod:
            return mod
        mod = CompiledModule(name, type)
        CompiledModule.modules[name] = mod
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

    def build(self, target):
        debug_log("CompiledModule.build()")
        if self.cmhash:
            return self.cmhash
        
        self.srcpath = target.mod2src(self.name, self.type)
        
        self.srcfile = target.compile(self.srcpath, type=self.type, modname=self.name)

        self.cmpath = self.srcfile.cmpath
        self.cmhash = sha256_file(self.cmpath)
        return self.cmhash

class Target:
    def __init__(self, path: Path, cfg: BuildConfig):
        self.path = path
        self.srcfiles = set()
        self.objs = []
        self.processed_files = set()
        self.configs = set()
        self.most_recent_output_mtime = 0
        self.extra_linkflags = {}
        self.cfg = cfg

    def compile(self, path: Path, type=None, modname: str=None):
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

        file = SourceFile.get(path, self.cfg, type=type, modname=modname)
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
            ofile = self.cfg.BINDIR / (self.path.name + suffix)
        else:
            ofile = self.cfg.BINDIR / self.cfg.OUTFILE

        ofile_mtime = ofile.mtime()
        if self.most_recent_output_mtime >= ofile_mtime or THIS_MTIME > ofile_mtime:
            lflags = self.get_linkflags()
            print("LINKING", ofile)
            shell(self.cfg.CXX, *extra_flags, *self.objs, *lflags, f"-o{ofile}")
        return ofile

    def add_config(self, config):
        if config in self.configs:
            return
        self.configs.add(config)

        if config.linkflags:
            self.extra_linkflags.update(dict.fromkeys(config.linkflags))


    def get_linkflags(self):
        lflags = dict.fromkeys(self.cfg.LDFLAGS)
        lflags.update(self.extra_linkflags)

        extra = []

        for flag in lflags:
            if flag.startswith('-L'):
                rpath_flag = '-Wl,-rpath,' + flag[2:]
                extra.append(rpath_flag)

        lflags.update(dict.fromkeys(extra))
        return list(lflags)
    

    def mod2src(self, modname: str, type: SourceType):
        debug_log("mod2src", modname, type)
        path = mod2path(modname, type)
        debug_log("TRYING TO FIND module source file", path)
        failed = []

        for base_path in [SRCDIR, *INCFLAGS]:
            if isinstance(base_path, str):
                base_path = base_path.removeprefix("-I").removeprefix("-iquote")
                base_path = Path(base_path)

            full_path = base_path / path
            debug_log("TRYING", full_path)
            if full_path.exists():
                return full_path
            
            failed.append(str(full_path))

            srcfile2 = full_path.parent / full_path.stem / full_path.name
            if srcfile2.exists():
                    return srcfile2
            failed.append(str(srcfile2))

        warn(f"FATAL: Unable to locate module {modname}: the following files do not exist: %s" % ', '.join(failed))
        exit(1)

class SourceFile:
    files = {}

    @staticmethod
    def get(path: Path, cfg: BuildConfig, type: SourceType=None, modname: str=None):
        file = SourceFile.files.get(path)
        if file:
            if type and file.type and type != file.type:
                raise Exception(f"type mismatch: new type {type}; old type {file.type}")
            if modname and file.modname and modname != file.modname:
                raise Exception("modname mismatch")
            return file
        file = SourceFile(path, type=type, modname=modname, cfg=cfg)
        SourceFile.files[path] = file
        return file

    def __init__(self, path: Path, type: SourceType, modname: str, cfg: BuildConfig):
        self.path         = path
        self.dirname      = path.parent
        self.type         = type
        self.modname      = modname
        self.processed    = False
        self.output_mtime = 0

        file_parts        = list(path.relative_to(SRCDIR).parts)
        for i, part in enumerate(file_parts):
            if part == "..":
                file_parts[i] = "__PARENT__"
        file = Path(*file_parts)
        
        self.objpath     = cfg.OBJDIR / file.with_suffix('.o')

        if modname:
            self.cmpath  = cfg.OBJDIR / mod2cm(modname)
        else:
            self.cmpath  = cfg.OBJDIR / file.with_suffix(".pcm")

        self.output_path = self.cmpath if self.type in [SourceType.USER_HEADER, SourceType.SYSTEM_HEADER, SourceType.GENERATED_HEADER] else self.objpath
        self.infofile    = cfg.OBJDIR / file.with_suffix(".info")
        self.makefile    = cfg.OBJDIR / file.with_suffix(".make")
        self.mtime       = self.path.mtime()
        self.deps        = set()
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
        
        infofile_mtime = self.infofile.mtime()
        if self.mtime >= infofile_mtime:
            self.up_to_date = False
            self.need_recompile = True
            debug_log(f"#{self.path} NEED RECOMPILE BECAUSE MTIME={self.mtime} > INFOFILE_MTIME={infofile_mtime}")
            return
        
        self.output_mtime = infofile_mtime
        
        try:
            with open(self.infofile, 'r') as f:
                data = json.load(f)
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
                dep = depname[5:]

                if SourceFile.get(dep, cfg).mtime >= infofile_mtime:
                    self.up_to_date     = False
                    self.need_recompile = True

            elif depname.startswith('module:'):
                m = re.match(r'module:(.*)@(.*)', depname)
                name, sha256 = m.groups()
                self.deps.add(ModuleDep(name, sha256))
                self.up_to_date = False

            elif depname.startswith('include:'):
                dep = depname[8:]
                hfile = HeaderDep.get(Path(dep))
                self.up_to_date = False
                if hfile.mtime() >= infofile_mtime:
                    self.need_recompile = True
                self.deps.add(hfile)

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
        
        if self.need_recompile:
            objdir = self.objpath.parent
            os.makedirs(objdir, exist_ok=True)
            self.compile(target, cfg)
            self.update(target.cfg)
            self.output_mtime = time.time()

            for header_dep in self.header_deps:
                header_dep.build(target)

        else:
            self.build_deps(target, cfg)

    def build_deps(self, target, cfg: BuildConfig):
        for dep in self.deps:
            if isinstance(dep, ModuleDep):
                mod = CompiledModule.get(dep.name)
                new_hash = mod.build(target, cfg)

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
        #print(out)
        atomic_write(self.infofile, json.dumps(out, indent=2) + '\n')

    @cache
    def dircfg(self):
        return DirectoryConfig.get(self.dirname)

    @cache
    def compiler_cmd(self, cfg: BuildConfig):
        if USECLANG:
            cmd = self.compiler_cmd_clang(cfg)
        else:
            cmd = self.compiler_cmd_gcc(cfg)

        return cmd
    
    IFLAG_RE = re.compile('^-I')
    def compiler_extra_args(self):
        flags = set()

        buildvars = self.dircfg().buildvars
        if 'CFLAGS' in buildvars:
            cflags = buildvars['CFLAGS']

            cflags = map(lambda flag: re.sub(self.IFLAG_RE, '-idirafter', flag, 1), cflags)
            flags.update(cflags)

        dirparts = list(self.dirname.parts)
        self.add_include(dirparts, flags)

        if self.type == SourceType.C:
            flags.add("-xc")
        elif self.type == SourceType.ASM:
            flags.add("-xassembler-with-cpp")

        return flags
    
    def add_include(self, dirparts, flags):
        index = -1

        try: index = dirparts.index('src')
        except ValueError: pass
        if index >= 0:
            flags.add("-I"+str(Path(*dirparts[:index], 'include')))
            flags.add("-iquote"+str(Path(*dirparts[:index], 'src')))
            return
        
        try: index = dirparts.index('Src')
        except ValueError: pass
        if index >= 0:
            flags.add("-iquote"+str(Path(*dirparts[:index], 'Inc')))
            return
        
        try: index = dirparts.index('deps')
        except ValueError: pass
        if index >= 0:
            f = "-I"+str(Path(*dirparts[:index+2]))
            flags.add(f)
            return

        

    def compiler_cmd_clang(self, cfg: BuildConfig, extra_args=[]):
        extra_args1 = self.compiler_extra_args()
        header_units = []

        if self.type == SourceType.USER_HEADER:
            return [cfg.CXX, "-xc++-header", "-fmodule-header=user", f"-fprebuilt-module-path={cfg.OBJDIR}", *cfg.CFLAGS, *cfg.CXXFLAGS, *cfg.INCFLAGS, "-o"+str(self.cmpath), "-c", str(self.path)]
        
        if self.type == SourceType.SYSTEM_HEADER:
            raise NotImplementedError
        
        if self.type == SourceType.MODULE:
            extra_args2 = [f"-fmodule-file={f}" for f in header_units] + [
                "-xc++-module", 
                f"-fmodule-output={self.cmpath}", 
                "-MD", 
                f"-MF{self.makefile}"
            ]
            return [cfg.CXX, f"-fprebuilt-module-path={cfg.OBJDIR}", *extra_args1, *extra_args2, *cfg.CLANG_CFLAGS, *cfg.CXXFLAGS, *cfg.INCFLAGS, "-o"+str(self.objpath), "-c", str(self.path)]
        
        
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
            args += ["-fmodules-ts", "-fmodule-header=system", "-I.", *cfg.CXXFLAGS]

        elif self.type == SourceType.USER_HEADER:
            args += ["-fmodules-ts", "-fmodule-header=user", "-iquote.", *cfg.CXXFLAGS]

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

        args += ["-c", str(self.path)]

        return args

    def compile(self, target, cfg: BuildConfig):
        self.header_deps = set()

        if USECLANG:
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
        print(f"BUILDING {self.type} {self.path}")
        if ".." in str(self.path):
            print("TYPE", type(self.path), self.path, id(self.path))
            raise "x"

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

        self.deps = set()
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
                            header_dep = HeaderDep.get(path)

                            self.deps.add(header_dep)
                            self.header_deps.add(header_dep)

                        out.append("BOOL TRUE")

                    elif cmd == "MODULE-REPO":
                        debug_log(f"MODULE-REPO => PATHNAME {cfg.OBJDIR}")
                        out.append(f"PATHNAME {cfg.OBJDIR}")

                    elif cmd == "MODULE-IMPORT":
                        modname = args[0].replace("'", '')
                        mod = CompiledModule.get(modname)
                        cmhash = mod.build(target, cfg)
                        self.deps.add(ModuleDep(modname, cmhash))
                        
                        path = mod.cmpath.relative_to(cfg.OBJDIR)
                        debug_log(f"MODULE-IMPORT {self.path}: {args} => PATHNAME {path}")
                        out.append(f"PATHNAME {path}")

                    elif cmd == "MODULE-EXPORT":
                        modname = args[0]
                        #debug_log(f"MODULE-EXPORT {modname}")
                        file = modname.replace("'", '')
                        cmfile = mod2cm(file)
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

    def compile_gcc_c(self, cfg: BuildConfig):
        print(f"BUILDING {self.type} {self.path}")
        
        shell(*self.compiler_cmd(cfg))
        self.process_makefile_deps()

    def compile_clang(self, target):
        deps, header_units = self.clang_get_deps(target)
        
        print(f"BUILDING {self.type} {self.path}")
        cmdline = self.compiler_cmd()
        print(*cmdline)
        
        result = subprocess.run(cmdline, check=False)
        if result.returncode != 0:
            exit(result.returncode)

        self.process_makefile_deps()
        return deps

    def clang_get_deps(self, target, cfg: BuildConfig):
        self.deps = set()
        self.vcpkgs = set()

        if self.type in [SourceType.USER_HEADER, SourceType.SYSTEM_HEADER]:
            extra_args = ["-xc++-header"]
        else:
            extra_args = ["-xc++"]
        args = ["clang-scan-deps", "-format=p1689", "--", cfg.CXX, *extra_args, f"-fprebuilt-module-path={cfg.OBJDIR}", *CXXFLAGS, *INCFLAGS, "-o"+str(self.objpath), "-c", self.path]

        #print("running", *args)
        result = subprocess.run(args, capture_output=True)

        header_units = []
        #line_match = re.compile('^[a-zA-Z0-9\-_.\/]+:\d+:\d+: error: header file (["<])([a-zA-Z0-9\-_.\/]+)[">] \(aka \'([a-zA-Z0-9\-_.\/]+)\'\) cannot be imported because it is not known to be a header unit\n$')
        if result.returncode != 0:
            for line in result.stderr.decode().splitlines():
                m = re.match(r'^.*:\d+:\d+: error: header file (["<])([a-zA-Z0-9\-_.\/]+)[">] \(aka \'([a-zA-Z0-9\-_.\/]+)\'\) cannot be imported because it is not known to be a header unit$', line)
                #print("GOT", m, line)
                if m:
                    type = SourceType.SYSTEM_HEADER if m.group(1) == '<' else SourceType.USER_HEADER
                    header_path = m.group(3)

                    print("GOT HEADER PATH", header_path)
                    mod = CompiledModule.get(header_path, type)
                    cmhash = mod.build(target, cfg)
                    dep = ModuleDep(header_path, cmhash)
                    self.deps.add(dep)
                    header_units.append(mod.cmpath)
                    #exit(0)

                    #srcfile = SourceFile.get(header_path, cfg, type)
                    #srcfile.build(target)
                    #if type == SourceType.USER_HEADER:
                        #self.deps.add(srcfile)
                    #self.vcpkgs.update(srcfile.vcpkgs)
                    #header_units.append(srcfile.cmpath)

            extra_args += [f"-fmodule-file={f}" for f in header_units]
            clang_args = self.compiler_cmd_clang(extra_args=extra_args)
            #args = ["clang-scan-deps", "-format=p1689", "--", cfg.CXX, *extra_args,  *CCFLAGS, *CXXFLAGS, *INCFLAGS, "-o"+str(self.objpath), "-c", self.path]
            args = ["clang-scan-deps", "-format=p1689", "--", *clang_args]
            result = subprocess.run(args, capture_output=True)

            if result.returncode != 0:
                warn("SCANDEPS failed with cmd line:", *args)
                warn(result.stderr.decode())
                exit(1)

        # print(result.stdout.decode())
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
                    mod = CompiledModule.get(modname)
                    cmhash = mod.build(target, cfg)
                    self.deps.add(ModuleDep(modname, cmhash))
            return self.deps, header_units

    def process_makefile_deps(self):
        if self.type in [SourceType.USER_HEADER, SourceType.SYSTEM_HEADER]:
            return
        text = self.makefile.read_text()
        rules = parse_makefile_rules(text)
        for rule in rules:
            if not rule.startswith('/') and rule != self.path:
                headerdep = HeaderDep.get(Path(rule))
                self.deps.add(headerdep)
                self.header_deps.add(headerdep)
                
            elif re.match(VCPKG_INCLUDE_RE, rule):
                pkg = re.match(VCPKG_INCLUDE_RE, rule).group(1)
                self.vcpkgs.add(pkg)


class ModuleDep:
    def __init__(self, name, sha256):
        self.name = name
        self.sha256 = sha256

class DirectoryConfig:
    @classmethod
    @cache
    def get(cls, path: Path):
        cfg = DirectoryConfig(path)
        cfg.process()
        return cfg

    def __init__(self, path: Path):
        self.dir = path.relative_to(SRCDIR)

    def process(self):
        buildpy_file = self.dir / 'BUILD.py'
        if not buildpy_file.exists():
            self.buildvars = {}
            self.linkflags = []
            return
        
        json_file = DEPDIR / self.dir / 'buildvars.json'
        json_mtime = json_file.mtime()
        buildrb_mtime = buildpy_file.mtime()

        if buildrb_mtime > json_mtime or THIS_MTIME > json_mtime:
            text = try_read(buildpy_file)
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
            os.makedirs(json_file.parent, exist_ok=True)
            with open(json_file, 'w') as f:
                json.dump(self.buildvars, f, indent=2)
        else:
            try:
                with open(json_file, 'r') as f:
                    self.buildvars = json.load(f)
            except Exception as ex:
                warn("error reading JSON %s: %s" % (json_file, str(ex)))
                exit(1)

        if 'LDFLAGS' in self.buildvars:
            self.linkflags = self.buildvars['LDFLAGS']
        else:
            self.linkflags = []

    def handle_pkgconfig(self, buildvars):
        if 'PKGCONFIG' not in buildvars:
            return
        
        linkflags = set()
        if 'LDFLAGS' in buildvars:
            linkflags.update(buildvars['LDFLAGS'])

        cflags = set()
        if 'CFLAGS' in buildvars:
            cflags.update(buildvars['CFLAGS'])
        
        for pkg in buildvars['PKGCONFIG']:
            libs_flags = shlex.split(shell("pkg-config", "--libs", pkg))
            cflags_cur = self.filter_cflags(shlex.split(shell("pkg-config", "--cflags", pkg)))
            linkflags.update(libs_flags)
            cflags.update(cflags_cur)

        if linkflags:
            buildvars['LDFLAGS'] = list(linkflags)

        if cflags:
            buildvars['CFLAGS'] = list(cflags)
            # buildvars['CXXFLAGS'] = list(cflags)
            
    def filter_cflags(self, flags):
        out = []
        
        for flag in flags:
            if flag.startswith('-std='):
                continue
            
            out.append(flag)
            
        return out

class HeaderDep:
    files = {}

    @classmethod
    @cache
    def get(cls, path: Path):
        #path = str(Path(path).resolve())
        #file = cls.files.get(path)
        #if file:
        #    return file
        #debug_log("HeaderDep", path)
        return HeaderDep(path)
        #cls.files[path] = file
        #return file

    def __init__(self, path):
        #print("PATH", path, type(path))
        self.path = path
        self.built = False

    def build(self, target):
        if self.built:
            return
        self.built = True
        #debug_log("HeaderDep.build", self.path)
        
        dirname = self.path.parent
        dircfg = DirectoryConfig.get(dirname)

        target.add_config(dircfg)
        cppfile = self.find_cpp(self.path)
        debug_log('find_cpp', self.path, '-->', cppfile)
        if cppfile:
            self.cpp_path = cppfile
            target.compile(self.cpp_path)
            return

    @cache
    def mtime(self):
        return self.path.mtime()

    def find_cpp(self, hfile: Path):
        if hfile.suffix not in HFILE_SUFFIXES:
            return None
        
        basename = hfile.with_suffix('')
        for ext in [".cc", ".cpp", ".c"]:
            cppfile = basename.with_extra_suffix(ext)
            if cppfile.exists():
                return cppfile
            
        #print("!!!!", list(hfile.parts), 'include' in hfile.parts)
        if "include" in hfile.parts:
            parts = list(hfile.parts)
            include_index = parts.index('include')
            parts[include_index] = 'src'
            newpath = Path(*parts)

            if newpath.parent.is_dir():
                return self.find_cpp(newpath)
            
            # project/include/project/file.h -> project/src/file.h
            if include_index > 0 and include_index < len(parts) - 2 and parts[include_index-1] == parts[include_index+1]:
                parts.pop(include_index+1)
                return self.find_cpp(Path(*parts))
        
        if "Inc" in hfile.parts:
            parts = list(hfile.parts)
            include_index = parts.index('Inc')
            parts[include_index] = 'Src'
            newpath = Path(*parts)

            if newpath.parent.is_dir():
                return self.find_cpp(newpath)

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
        for path in find_files(self.paths, suffixes=[".cc", ".cpp", ".c"]):
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
            "directory": os.getcwd(),
            "arguments": compilation_cmd,
        })

def find_files(paths: list[Path], suffixes: tuple[str], prefixes: tuple[str] = None):
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
        if path.is_file():
            if not path.name.endswith(suffixes):
                continue

            if prefixes is not None and not path.name.startswith(prefixes):
                continue

            yield path
            continue

        with os.scandir(path) as entries:
            for entry in entries:
                # print("entry", entry)
                if entry.is_file() and entry.name.endswith(suffixes):
                    if prefixes is not None and not entry.name.startswith(prefixes):
                        continue

                    yield Path(entry.path)
                    
                elif entry.is_dir() and not entry.is_symlink() and not entry.name.startswith("."):  # Recurse into subdirectories
                    yield from find_files([Path(entry.path)], suffixes=suffixes, prefixes=prefixes)

def atomic_write(path: Path, data: str):
    tmpfile = path.with_extra_suffix(".tmp")
    with open(tmpfile, 'w') as f:
        f.write(data)
    os.rename(tmpfile, path)

def try_read(path: Path):
    try:
        with open(path, 'r') as f:
            return f.read()
    except FileNotFoundError:
        return None
    
def shell(*args):
    cmd = " ".join(shlex.quote(str(arg)) for arg in args)
    print(cmd)
    result = subprocess.run(args, shell=False, text=True, stdin=0, stdout=subprocess.PIPE, stderr=2)
    if result.returncode != 0:
        exit(1)
    return result.stdout

def mod2cm(modname):
    debug_log(f"mod2cm {modname}")
    if modname.startswith('/'):
        path = modname[1:]
    elif modname.startswith('./'):
        path = Path(modname + '.pcm')
        path = str(path.relative_to(SRCDIR))
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
        
    return path + '.cc'
    
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
    rules = text.replace(':', '').replace('\\\n', '').split()
    return rules[1:]

def warn(*s: str):
    print(*s, file=sys.stderr)

def debug_log(*text):
    if DEBUG_LOG:
        warn(*text)

def build(path: Path, cfg: BuildConfig):
    name = path.with_suffix('')
    target = Target(name, cfg)
    target.compile(path)
    
    os.makedirs(cfg.BINDIR, exist_ok=True)

    return target.link()

def make_compilation_database(paths: list[Path], cfg: BuildConfig):
    db = CompilationDatabase(paths)
    return db.build(cfg)

def build_compilation_database(out: Path, paths: list[Path], cfg: BuildConfig):
    data = make_compilation_database(paths, cfg)
    
    atomic_write(out, data)
    print("wrote %s" % out)


def mkpath(path: str) -> Path:
    return Path(os.path.relpath(os.path.abspath(path), os.path.abspath(ROOT)))

def run_tool(tool_path: str, dirs: list[str], cfg: BuildConfig):
    dirs = [Path(os.path.abspath(dir)) for dir in dirs]

    # change directory to root
    oldwd = None
    if ROOT != ".":
        oldwd = os.getcwd()
        os.chdir(ROOT)

    
    main_path = mkpath(tool_path)
    main_name = main_path.with_suffix('')
    target = Target(main_name, cfg)
    target.compile(main_path, SourceType.CPP)
    
    for filename in find_files(dirs, suffixes = ('_test.cc', '_test.cpp')):
        #print("building %s..." % filename)
        path = mkpath(filename)
        target.compile(path, SourceType.CPP)

    bin = target.link()
    bin = os.path.abspath(bin)
    if oldwd:
        os.chdir(oldwd)
    os.execv(bin, [bin])


def run_tests(dirs: list[str], cfg: BuildConfig):
    run_tool(TESTMAIN, dirs, cfg)

def run_benchmarks(dirs: list[str], cfg: BuildConfig):
    run_tool(BENCHMAIN, dirs, cfg)
        
def sha256_file(path: Path):
    with open(path, 'rb', buffering=0) as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()

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
        SRC_ROOTS=SRC_ROOTS
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
            g['USECLANG'] = True
            g['CXX'] = 'clang++'
    
    if args.debug_log:
        g['DEBUG_LOG'] = True

    for key, val in buildtype.__dict__.items():
        if key.startswith('__'):
            continue

        globals()[key] = val

    cfg = BuildConfig(
        CC=CC,
        CXX=CXX, 
        CFLAGS=CFLAGS,
        CXXFLAGS=CXXFLAGS,
        LDFLAGS=LDFLAGS,
        OBJDIR=Path(OBJDIR),
        DEPDIR=Path(DEPDIR),
        SRCDIR=Path(SRCDIR),
        BINDIR=Path(BINDIR),
        INCFLAGS=INCFLAGS,
        SUFFIX=SUFFIX
    )

    # g['OBJDIR'] = Path(OBJDIR)
    # g['DEPDIR'] = Path(DEPDIR)
    # g['SRCDIR'] = Path(SRCDIR)
    # g['BINDIR'] = Path(BINDIR)
    # g['INCFLAGS'] = INCFLAGS

    if args.cmd == 'build':
        file = args.path
        target = Path(os.path.relpath(os.path.abspath(file), os.path.abspath(ROOT)))
        if ROOT != ".":
            os.chdir(ROOT)

        if args.library:
            cfg.SUFFIX = '.so'
            cfg.LDFLAGS += ["-shared"]
        
        build(target, cfg)
    
    elif args.cmd == 'run':
        file = args.path
        target = Path(os.path.relpath(os.path.abspath(file), os.path.abspath(ROOT)))
        oldwd = None
        if ROOT != ".":
            oldwd = os.getcwd()
            os.chdir(ROOT)
        bin = os.path.abspath(build(target, cfg))
        if oldwd:
            os.chdir(oldwd)
        os.execv(bin, [bin] + args.args)

    elif args.cmd == 'ide':
        paths = []
        os.chdir(ROOT)

        if len(args.paths) == 0:
            for root in SRC_ROOTS:
                paths.append(Path(root))

        else:
            for arg in args.paths:
                file = arg
                path = Path(os.path.relpath(os.path.abspath(file), os.path.abspath(ROOT)))
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
