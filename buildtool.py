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

ROOT = os.path.dirname(os.path.realpath(sys.argv[0]))

DEBUG_LOG = False

VCPKG_INCLUDE_RE = r"^vcpkg\/installed\/[a-z0-9-]+\/include\/([^\/]+)\/"

CCFLAGS = ["-pthread", "-fnon-call-exceptions", "-g",
            "-Wall", "-Wextra", "-Wconversion", "-Wno-sign-compare", "-Wno-deprecated"]
CXXFLAGS = ["-std=c++23"]
LFLAGS = ["-lrt"]
OBJDIR = "obj"
DEPDIR = "obj"
SUFFIX = ""

SRCDIR = "."
BINDIR = "bin"
INCPATH = []
USECLANG = False

CXX = "clang++" if USECLANG else "g++"
CC = "clang" if USECLANG else "gcc"

class Release:
    CCFLAGS = CCFLAGS + ["-O2", "-mtune=native"]
    LFLAGS  = LFLAGS + ["-fwhole-program", "-O2", "-mtune=native"]
    OBJDIR  = OBJDIR + "/release"
    DEPDIR  = DEPDIR + "/release"


class Debug:
    CCFLAGS = CCFLAGS + ["-fsanitize=address"]
    OBJDIR  = OBJDIR + "/debug"
    DEPDIR  = DEPDIR + "/debug"
    SUFFIX  = "+debug"

## =========================================================== ##

CCFILE_SUFFIXES = ('.cc', '.cpp')
HFILE_SUFFIXES  = ('.h', '.hpp', '.hh')

THIS_MTIME = 0

class TargetType(Enum):
    EXECUTABLE = 1
    LIBRARY    = 2

class SourceType(StrEnum):
    CPP              = 'c++'
    C                = 'c'
    SYSTEM_HEADER    = 'system header'
    USER_HEADER      = 'user header'
    GENERATED_HEADER = 'generated header'
    MODULE           = 'module'

# https://stackoverflow.com/q/29850801/
BasePath = type(pathlib.Path())
class Path(BasePath):
    def __new__(cls, *paths: str):
        normalized = os.path.normpath('/'.join(paths))
        return super(Path, cls).__new__(cls, normalized)

    def with_extra_suffix(self, suffix: str) -> 'Path':
        return self.with_name(self.name + suffix)
    
    @cache
    def stat(self):
        try:
            return super().stat()
        except FileNotFoundError:
            return None
        
    def mtime(self):
        stat = self.stat()
        if stat is None:
            return 0
        return stat.st_mtime
    
    def exists(self):
        return self.stat() is not None


class Target:
    def __init__(self, path: Path, targettype: TargetType):
        self.path = path
        self.targettype = targettype
        self.srcfiles = set()
        self.objs = []
        self.processed_files = set()
        self.configs = set()
        self.most_recent_output_mtime = 0
        self.extra_linkflags = set()

    def compile(self, path: Path, modname: str=None):
        if path.suffix in CCFILE_SUFFIXES:
            type = SourceType.CPP
        elif path.suffix in ('.c'):
            type = SourceType.C
        else:
            warn("uncrecognized file type: %s" % path)
            exit(1)
        
        file = SourceFile.get(path, type, modname)
        if file in self.processed_files:
            return
        self.processed_files.add(file)

        debug_log(f"processing {path} type={type}")
        file.build(self)

        #if type not in [SourceType.SYSTEM_HEADER, SourceType.USER_HEADER]:
        self.objs.append(file.objpath)

        if file.output_mtime > self.most_recent_output_mtime:
            self.most_recent_output_mtime = file.output_mtime
        
        return file

    def link(self):
        dirname = self.path.parent
        buildvars = DirectoryConfig.get(dirname).buildvars

        suffix = SUFFIX
        extra_flags = []
        if self.targettype == TargetType.LIBRARY:
            suffix += ".so"
            extra_flags = ['-shared']
        
        ofile = BINDIR / (self.path.name + suffix)

        ofile_mtime = ofile.mtime()
        if self.most_recent_output_mtime >= ofile_mtime or THIS_MTIME > ofile_mtime:
            lflags = self.get_linkflags()
            print("LINKING", self.path)
            shell(CXX, *CCFLAGS, *extra_flags, *self.objs, *lflags, f"-o{ofile}")
        return ofile

    def add_config(self, config):
        if config in self.configs:
            return
        self.configs.add(config)

        if config.linkflags:
            self.extra_linkflags.update(config.linkflags)


    def get_linkflags(self):
        lflags = set(LFLAGS)
        lflags.update(self.extra_linkflags)

        extra = []

        for flag in lflags:
            if flag.startswith('-L'):
                rpath_flag = '-Wl,-rpath,' + flag[2:]
                extra.append(rpath_flag)

        lflags.update(extra)
        return lflags
        

class SourceFile:
    files = {}

    @staticmethod
    def get(path: Path, type: SourceType=None, modname: str=None):
        file = SourceFile.files.get(path)
        if file:
            if type and file.type and type != file.type:
                raise Exception(f"type mismatch: new type {type}; old type {file.type}")
            if modname and file.modname and modname != file.modname:
                raise Exception("modname mismatch")
            return file
        file = SourceFile(path, type, modname)
        SourceFile.files[path] = file
        return file

    def __init__(self, path: Path, type: SourceType, modname: str):
        self.path         = path
        self.dirname      = path.parent
        self.type         = type
        self.modname      = modname
        self.processed    = False
        self.output_mtime = 0

        file             = path.relative_to(SRCDIR)
        self.objpath     = OBJDIR / file.with_suffix('.o')

        if modname:
            self.cmpath  = OBJDIR / mod2cm(modname)
        else:
            self.cmpath  = OBJDIR / file.with_suffix(".pcm")

        self.output_path = self.cmpath if self.type in [SourceType.USER_HEADER, SourceType.SYSTEM_HEADER, SourceType.GENERATED_HEADER] else self.objpath
        self.infofile    = OBJDIR / file.with_suffix(".info")
        self.makefile    = OBJDIR / file.with_suffix(".make")
        self.mtime       = self.path.mtime()
        self.deps        = set()
        self.up_to_date  = None

    def check_up_to_date(self):
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
        
        if data['command'] != self.compiler_cmd():
            self.up_to_date = False
            self.need_recompile = True
            debug_log("compiler command changed %s != %s" % (data['command'], self.compiler_cmd()))
            return
        
        self.need_recompile = False
        for depname in data['deps']:
            if depname.startswith('file:'):
                dep = depname[5:]

                if SourceFile.get(dep).mtime >= infofile_mtime:
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

    def build(self, target):
        if self.processed:
            return
        self.processed = True

        target.add_config(self.dircfg())

        self.check_up_to_date()
        if self.up_to_date:
            return
        
        if self.need_recompile:
            objdir = self.objpath.parent
            os.makedirs(objdir, exist_ok=True)
            self.compile(target)
            self.update()
            self.output_mtime = time.time()

            for header_dep in self.header_deps:
                header_dep.build(target)

        else:
             self.build_deps(target)

    def build_deps(self, target):
        for dep in self.deps:
            if isinstance(dep, ModuleDep):
                mod = CompiledModule.get(dep.name)
                new_hash = mod.build(target)

                if new_hash != dep.sha256:
                    self.need_recompile = True

            elif isinstance(dep, HeaderDep):
                dep.build(target)

            else:
                raise Exception(f"unrecognized dep {dep}")

    def update(self):
        deps = []
        for dep in self.deps:

            if isinstance(dep, ModuleDep):
                deps.append(f"module:{dep.name}@{dep.sha256}")

            if isinstance(dep, HeaderDep):
                deps.append(f"include:{dep.path}")

            else:
                raise Exception(f"unhandled dep type #{dep} of type #{type(dep)}")

        out = {
            'command': self.compiler_cmd(),
            'deps': deps
        }
        atomic_write(self.infofile, json.dumps(out, indent=2) + '\n')

    @cache
    def dircfg(self):
        return DirectoryConfig.get(self.dirname)

    @cache
    def compiler_cmd(self):
        if USECLANG:
            cmd = self.compiler_cmd_clang()
        else:
            cmd = self.compiler_cmd_gcc()

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
        try:
            src_index = dirparts.index('src')
            dirparts[src_index] = 'include'
            flags.add("-I"+str(Path(*dirparts)))
        except ValueError:
            pass

        if self.type == SourceType.C:
            flags.add("-xc")

        return flags

    def compiler_cmd_clang(self):
        extra_args1 = self.compiler_extra_args()
        header_units = []

        if self.type == SourceType.USER_HEADER:
            return [C, "-xc++-header", "-fmodule-header=user", f"-fprebuilt-module-path={OBJDIR}", *CCFLAGS, *CXXFLAGS, *INCPATH, "-o"+self.cmpath, "-c", self.path]
        
        elif self.type == SourceType.SYSTEM_HEADER:
            raise NotImplementedError
        
        elif self.type == SourceType.MODULE:
            extra_args2 = [f"-fmodule-file={f}" for f in header_units] + [
                "-xc++-module", 
                f"-fmodule-output={self.cmpath}", 
                "-MD", 
                f"-MF{self.makefile}"
            ]
            return [CXX, f"-fprebuilt-module-path={OBJDIR}", *extra_args1, *extra_args2, *CCFLAGS, *INCPATH, "-o"+str(self.objpath), "-c", self.path]
        
        else:
            if self.type == SourceType.C:
                cmd = CC
                
            extra_args2 = [f"-fmodule-file={f}" for f in header_units] + ["-MD", f"-MF{self.makefile}"]
            return [CXX, f"-fprebuilt-module-path={OBJDIR}", *extra_args1, *extra_args2, *CCFLAGS, *CXXFLAGS, *INCPATH, "-o"+str(self.objpath), "-c", self.path]

    def compiler_cmd_gcc(self):
        cmd = CXX
        if self.type == SourceType.C:
            cmd = CC
            
        args = [cmd]
        if self.type == SourceType.SYSTEM_HEADER:
            args += ["-fmodules-ts", "-fmodule-header=system", "-I.", *CXXFLAGS]

        elif self.type == SourceType.USER_HEADER:
            args += ["-fmodules-ts", "-fmodule-header=user", "-iquote.", *CXXFLAGS]

        elif self.type == SourceType.CPP:
            args += [
                "-fmodules-ts", 
                *CXXFLAGS
            ]

        elif self.type == SourceType.C:
            args += ["-MD", f"-MF{self.makefile}"]

        args += [*self.compiler_extra_args(), *CCFLAGS, *INCPATH]

        if self.type not in [SourceType.USER_HEADER, SourceType.SYSTEM_HEADER, SourceType.GENERATED_HEADER]:
            args += ["-o"+str(self.objpath)]

        args += ["-c", str(self.path)]

        return args

    def compile(self, target):
        self.header_deps = set()

        if USECLANG:
            self.compile_clang(target)
        else:
            self.compile_gcc(target)

    MODULE_MAPPER_LINE_RE = re.compile(r'^([A-Z-]+)\b(.*)')
    def compile_gcc(self, target):
        if self.type == SourceType.C:
            self.compile_gcc_c(target)
            return
        
        # https://splichal.eu/scripts/sphinx/gcc/_build/html/gcc-command-options/c%2B%2B-modules.html
        # https://github.com/urnathan/libcody
        # https://www.open-std.org/jtc1/sc22/wg21/docs/papers/2020/p1184r2.pdf
        print(f"BUILDING {self.type} {self.path}")

        mapper_read, compiler_write = os.pipe()
        compiler_read, mapper_write = os.pipe()

        cmd = self.compiler_cmd() + [f"-fmodule-mapper=<{compiler_read}>{compiler_write}"]

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
                        break;
                    
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
                        out.append("HELLO 1 compile.rb")
                        
                    elif cmd == "INCLUDE-TRANSLATE":
                        file = args[0]
                        if not file.startswith('/'):
                            path = Path(file)
                            #debug_log(f"INCLUDE-TRANSLATE {path}")
                            header_dep = HeaderDep.get(path)

                            self.deps.add(header_dep)
                            self.header_deps.add(header_dep)

                        out.append("BOOL TRUE")

                    elif cmd == "MODULE-REPO":
                        out.append("PATHNAME obj/release")

                    elif cmd == "MODULE-IMPORT":
                        debug_log("MODULE-IMPORT #{path}: #{args}")

                        modname = args[0].replace("'", '')
                        mod = CompiledModule.get(modname)
                        cmhash = mod.build(target)
                        self.deps.add(ModuleDep(modname, cmhash))

                    elif cmd == "MODULE-EXPORT":
                        modname = args[0]
                        debug_log(f"MODULE-EXPORT {modname}")
                        file = modname.replace("'", '').replace(':', '-')
                        if file.startswith('/'):
                            file = "system" + file + ".pcm"
                        elif file.startswith("./"):
                            file = file[2:] + ".pcm"
                        else:
                            file = file.replace('.', '/') + ".pcm"
                        out.append(f"PATHNAME {file}")

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

    def compile_gcc_c(self, target):
        print(f"BUILDING {self.type} {self.path}")
        
        shell(*self.compiler_cmd())
        self.process_makefile_deps()

    def compile_clang(self, target):
        deps, header_units = self.clang_get_deps(target)
        
        print(f"COMPILE {self.path}: ")
        
        subprocess.run(self.compiler_cmd(), check=True)
        self.process_makefile_deps()
        return deps

    def clang_get_deps(self, target):
        self.deps = set()
        self.vcpkgs = set()

        if self.type in [SourceType.USER_HEADER, SourceType.SYSTEM_HEADER]:
            extra_args = ["-xc++-header"]
        else:
            extra_args = ["-xc++"]
        args = ["clang-scan-deps", "-format=p1689", "--", CXX, *extra_args, f"-fprebuilt-module-path={OBJDIR}", *CCFLAGS, *INCPATH, "-o"+self.objpath, "-c", self.path]

        stdout, stderr, status = subprocess.run(args, capture_output=True)

        header_units = []
        #line_match = re.compile('^[a-zA-Z0-9\-_.\/]+:\d+:\d+: error: header file (["<])([a-zA-Z0-9\-_.\/]+)[">] \(aka \'([a-zA-Z0-9\-_.\/]+)\'\) cannot be imported because it is not known to be a header unit\n$')
        if status.returncode != 0:
            for line in stderr.decode().splitlines():
                m = re.match(r'^.*:\d+:\d+: error: header file (["<])([a-zA-Z0-9\-_.\/]+)[">] \(aka \'([a-zA-Z0-9\-_.\/]+)\'\) cannot be imported because it is not known to be a header unit\n$', line)
                if m:
                    type = 'system_header' if m.group(1) == '<' else 'user_header'
                    header_path = m.group(3)
                    srcfile = SourceFile.get(header_path, type)
                    srcfile.build(target)
                    if type == 'user_header':
                        self.deps.add(srcfile)
                    self.vcpkgs.update(srcfile.vcpkgs)
                    header_units.append(srcfile.cmpath)

            extra_args += [f"-fmodule-file={f}" for f in header_units]
            args = ["clang-scan-deps", "-format=p1689", "--", CXX, *extra_args, f"-fprebuilt-module-path={OBJDIR}", *CCFLAGS, *INCPATH, "-o"+self.objpath, "-c", self.path]
            stdout, stderr, status = subprocess.run(args, capture_output=True)

            if status.returncode != 0:
                warn("SCANDEPS failed")
                warn(stderr.decode())
                exit(1)

        p1689 = json.loads(stdout.decode())
        provides = p1689["rules"][0]["requires"]
        if self.type == 'module':
            provides = p1689["rules"][0]["provides"]
            if not provides or len(provides) != 1:
                warn(f"wanted module with name {self.modname} in file {self.path} but got something else")
                exit(1)

            name = provides[0]["logical-name"]
            if name != self.modname:
                warn(f"wanted module with name {self.modname} in file {self.path} but got {name}")
                exit(1)

        reqs = p1689["rules"][0]["requires"]
        if reqs:
            for req in reqs:
                modname = req["logical-name"]
                print(f"about to build dep module {modname}")
                mod = CompiledModule.get(modname)
                mod.build(target)
                self.deps.add(mod)
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
        buildrb_file = self.dir / 'BUILD.py'
        if not buildrb_file.exists():
            self.buildvars = {}
            self.linkflags = []
            return
        
        json_file = DEPDIR / self.dir / 'buildvars.json'
        json_mtime = json_file.mtime()
        buildrb_mtime = buildrb_file.mtime()

        if buildrb_mtime > json_mtime or THIS_MTIME > json_mtime:
            text = try_read(buildrb_file)
            code = compile(text, buildrb_file, 'exec')
            env = {}
            exec(code, env)

            out = {}
            ALLOWED = ('LINKFLAGS', 'CFLAGS', 'PKGCONFIG')
            for key, val in env.items():
                if key in ALLOWED:
                    out[key] = val

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

        if 'LINKFLAGS' in self.buildvars:
            self.linkflags = self.buildvars['LINKFLAGS']
        else:
            self.linkflags = []

    def handle_pkgconfig(self, buildvars):
        if 'PKGCONFIG' not in buildvars:
            return
        
        linkflags = set()
        if 'LINKFLAGS' in buildvars:
            linkflags.update(buildvars['LINKFLAGS'])

        cflags = set()
        if 'CFLAGS' in buildvars:
            cflags.update(buildvars['CFLAGS'])
        
        for pkg in buildvars['PKGCONFIG']:
            libs_flags = shlex.split(shell("pkg-config", "--libs", pkg))
            cflags_cur = self.filter_cflags(shlex.split(shell("pkg-config", "--cflags", pkg)))
            linkflags.update(libs_flags)
            cflags.update(cflags_cur)

        if linkflags:
            buildvars['LINKFLAGS'] = list(linkflags)

        if cflags:
            buildvars['CFLAGS'] = list(cflags)
            
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
            return
        
        basename = hfile.with_suffix('')
        for ext in [".cc", ".cpp", ".c"]:
            cppfile = basename.with_extra_suffix(ext)
            if cppfile.exists():
                return cppfile
            
        #print("!!!!", list(hfile.parts), 'include' in hfile.parts)
        if "include" in hfile.parts:
            parts = list(hfile.parts)
            parts[parts.index('include')] = 'src'
            newpath = Path(*parts)
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

    def build(self):
        for path in find_files_with_suffixes(self.paths, [".cc", ".cpp", ".c"]):
            self.process_file(path)

        return json.dumps(self.entries, indent=2)

    def process_file(self, path):
        # path = os.path.normpath(os.path.join(basepath, filepath))
        file = SourceFile.get(path)
        if file in self.processed_files:
            return
        
        self.processed_files.add(file)

        # dirpath = os.path.dirname(filepath)
        # filename = os.path.basename(filepath)
        compilation_cmd = [str(cmd) for cmd in file.compiler_cmd_clang()]

        self.entries.append({
            "file": str(path),
            "directory": os.getcwd(),
            "arguments": compilation_cmd,
        })

def find_files_with_suffixes(paths: Path, suffixes: list[str]):
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

    for directory in paths:
        with os.scandir(directory) as entries:
            for entry in entries:
                # print("entry", entry)
                if entry.is_file() and entry.name.endswith(suffixes):
                    yield Path(entry.path)
                    
                elif entry.is_dir() and not entry.is_symlink() and not entry.name.startswith("."):  # Recurse into subdirectories
                    yield from find_files_with_suffixes([entry.path], suffixes)

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
    if modname.startswith('/'):
        path = modname[1:]
    elif modname.startswith('./'):
        path = modname[2:].removeprefix((SRCDIR + '/', ''))
    else:
        path = modname.replace(':', '-')
    return path + ".pcm"

def parse_makefile_rules(text):
    rules = text.replace(':', '').replace('\\\n', '').split()
    return rules[1:]

def warn(*s: str):
    print(*s, file=sys.stderr)

def debug_log(*text):
    if DEBUG_LOG:
        warn(*text)

def build(path: Path, buildtype: str):
    name = path.with_suffix('')
    target = Target(name, buildtype)
    target.compile(path, SourceType.CPP)
    
    os.makedirs(BINDIR, exist_ok=True)

    return target.link()

def vscode(paths: list[Path]):
    db = CompilationDatabase(paths)
    return db.build()


## MAIN ##
def main():
    global OBJDIR, DEPDIR, SRCDIR, BINDIR

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
    build_parser.add_argument('args', nargs='*')

    run_parser = subparsers.add_parser('run', help='run the specified binary')
    run_parser.add_argument('path', help="path/to/file.cc")
    run_parser.add_argument('--release', '-r', action='store_const', dest='buildtype', const='release', help='build in release mode')
    run_parser.add_argument('--debug', '-d', action='store_const', dest='buildtype', const='debug', help='build in debug mode')
    run_parser.add_argument('args', nargs='*')

    ide_parser = subparsers.add_parser('ide', help='generate a compile_commands.json compilation database')
    ide_parser.add_argument('paths', nargs='*')

    args = parser.parse_args()
    if args.cmd in ['build', 'run']:
        if args.buildtype == 'debug':
            buildcfg = Debug
        else:
            buildcfg = Release
    
    if args.debug_log:
        DEBUG_LOG = True

    for key, val in buildcfg.__dict__.items():
        if key.startswith('__'):
            continue

        globals()[key] = val

    OBJDIR = Path(OBJDIR)
    DEPDIR = Path(DEPDIR)
    SRCDIR = Path(SRCDIR)
    BINDIR = Path(BINDIR)

    if args.cmd == 'build':
        file = args.path
        target = Path(os.path.relpath(os.path.abspath(file), os.path.abspath(ROOT)))
        if ROOT != ".":
            os.chdir(ROOT)
        
        if args.library:
            build(target, TargetType.LIBRARY)
        else:
            build(target, TargetType.EXECUTABLE)
    
    elif args.cmd == 'run':
        file = args.path
        target = Path(os.path.relpath(os.path.abspath(file), os.path.abspath(ROOT)))
        oldwd = None
        if ROOT != ".":
            oldwd = os.getcwd()
            os.chdir(ROOT)
        bin = os.path.abspath(build(target, TargetType.EXECUTABLE))
        if oldwd:
            os.chdir(oldwd)
        os.execv(bin, [bin] + args.args)

    elif args.cmd == 'ide':
        paths = []
        os.chdir(ROOT)

        if len(args.paths) == 0:
            paths.append(SRCDIR)

        else:
            for arg in args.paths:
                file = arg
                path = Path(os.path.relpath(os.path.abspath(file), os.path.abspath(ROOT)))
                paths.append(path)

        data = vscode(paths)
        atomic_write(Path("compile_commands.json"), data)
        print("wrote compile_commands.json")
    else:
        parser.print_help()
        exit(1)

if __name__ == '__main__':
    main()
