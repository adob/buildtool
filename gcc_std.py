"""Discover GCC's aggregate standard header without trusting a filename suffix."""

from __future__ import annotations

import os
import hashlib
import json
import subprocess
from uuid import uuid4
from collections.abc import Sequence

if __package__:
    from .compiler import run_compiler
    from .scheduler import Job
    from .vfs import FileSystem
else:
    from compiler import run_compiler
    from scheduler import Job
    from vfs import FileSystem


def header_unit_flags(global_flags: Sequence[str], directory_flags: Sequence[str]) -> tuple[str, ...]:
    """Use global_flags plus directory settings relevant to the standard library.

    Directory package paths, warnings, and nonreserved project macros are local
    to the importer. Preserve reserved macros and NDEBUG, plus all other compiler
    options (including target, ABI, and language flags). Unusual macros intended
    to configure the SDK belong in global_flags. Forced includes and preprocessor
    passthrough retain the entire directory configuration conservatively.
    """
    if any(flag.startswith(('-include', '-imacros', '-Wp,')) or flag == '-Xpreprocessor'
           for flag in directory_flags):
        return (*global_flags, *directory_flags)
    paths = ('-I', '-iquote', '-isystem', '-idirafter', '-iprefix',
             '-iwithprefixbefore', '-iwithprefix')
    result = list(global_flags)
    flags = iter(directory_flags)
    for flag in flags:
        if flag in paths:
            next(flags, None)
        elif any(flag.startswith(prefix) for prefix in paths):
            continue
        elif flag.startswith('-W') and not flag.startswith(('-Wa,', '-Wl,')):
            continue
        elif flag.startswith(('-D', '-U')):
            definition = next(flags, '') if flag in ('-D', '-U') else flag[2:]
            name = definition.split('=', 1)[0]
            if name.startswith('_') or name == 'NDEBUG':
                result.append(flag[:2] + definition)
        else:
            result.append(flag)
    return tuple(result)


def toolchain_flags(flags: Sequence[str]) -> tuple[str, ...]:
    """Keep flags affecting the toolchain, excluding project headers and modules.

    Preserve target/sysroot and standard-library selection while removing search
    paths and forced includes that could substitute a project's bits/stdc++.h.
    """
    paths = ('-I', '-iquote', '-isystem', '-idirafter', '-include', '-imacros',
             '-iprefix', '-iwithprefixbefore', '-iwithprefix')
    result = []
    skip = False
    for flag in flags:
        if skip:
            skip = False
        elif flag in paths:
            skip = True
        elif any(flag.startswith(prefix) for prefix in paths):
            continue
        elif flag.startswith('-fmodule'):
            continue
        else:
            result.append(flag)
    return tuple(result)


class GccStdHeaders:
    def __init__(self, vfs: FileSystem) -> None:
        """Cache discovered toolchain header paths using vfs for file identity."""
        self.vfs = vfs
        self.paths: dict[tuple[str, ...], str | None] = {}

    async def matches(self, path: str, compiler: str, flags: Sequence[str],
                      parent: Job) -> bool:
        """Check path against compiler/flags; parent lends its slot to discovery."""
        if not os.path.isabs(path) or not path.endswith('/bits/stdc++.h'):
            return False
        command = (compiler, *toolchain_flags(flags), '-E', '-v', '-xc++', '-')
        if command not in self.paths:
            async def discover(job: Job) -> None:
                """Probe the compiler's default include search using job's slot."""
                env = dict(os.environ, LC_ALL='C')
                for name in ('CPATH', 'CPLUS_INCLUDE_PATH', 'C_INCLUDE_PATH'):
                    env.pop(name, None)
                result = await run_compiler(job, command, capture=True,
                                            env=env)
                if result.returncode:
                    job.write(result.stderr)
                    raise subprocess.CalledProcessError(result.returncode, command)
                header = None
                searching = False
                for line in result.stderr.decode(errors='replace').splitlines():
                    if line == '#include <...> search starts here:':
                        searching = True
                    elif line == 'End of search list.':
                        break
                    elif searching:
                        candidate = os.path.join(line.strip(), 'bits/stdc++.h')
                        if self.vfs.is_file(candidate):
                            header = self.vfs.realpath(candidate)
                            break
                self.paths[command] = header

            job = parent.session.schedule(('gcc-std-search', command), discover,
                                          parent=parent)
            await parent.wait_for_dependency(job)
        expected = self.paths[command]
        return expected is not None and self.vfs.realpath(path) == expected


class GccStdModules:
    def __init__(self, vfs: FileSystem) -> None:
        """Cache GCC/Clang standard-module metadata using vfs for discovery records."""
        self.vfs = vfs
        self.sources: dict[str, dict[str, str]] = {}
        self.fingerprints: dict[tuple[str, tuple[str, ...]], str] = {}
        self.local_flags: dict[str, dict[str, tuple[str, ...]]] = {}

    async def resolve(self, name: str, compiler: str, flags: Sequence[str],
                      cache_dir: str, parent: Job, *, clang: bool = False,
                      optional: bool = False) -> str:
        """Find name with compiler/flags; cache discovery in cache_dir via parent.

        clang selects Clang's library-manifest query (libstdc++ or libc++).
        optional lets IDE generation proceed when no module SDK is installed.
        Metadata is reread each build. The persistent probe result is keyed by
        compiler identity, flags, working directory, and driver search environment.
        """
        query = '--print-library-module-manifest-path' if clang else '-print-file-name=libstdc++.modules.json'
        command = (compiler, *toolchain_flags(flags), query)
        candidates = ([compiler] if os.path.dirname(compiler) else
                      [os.path.join(path, compiler) for path in os.get_exec_path()])
        executable = next((path for path in candidates if self.vfs.is_file(path)), None)
        if executable is None:
            if optional:
                return ''
            raise RuntimeError(f'Cannot discover module {name}: compiler {compiler!r} not found')
        stat = self.vfs.stat(executable)
        identity = (command, self.vfs.realpath(executable), stat.st_mtime_ns, stat.st_size,
                    self.vfs.getcwd(), [(key, os.environ.get(key)) for key in
                    ('PATH', 'COMPILER_PATH', 'GCC_EXEC_PREFIX', 'LIBRARY_PATH')])
        key = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
        self.fingerprints[(compiler, tuple(flags))] = key
        if key not in self.sources:
            async def discover(job: Job) -> None:
                """Load persisted metadata location or query GCC using job's slot."""
                cache = os.path.join(cache_dir, key + '.json')
                metadata = None
                try:
                    saved = json.loads(self.vfs.read_text(cache))
                    candidate = saved.get('metadata') if isinstance(saved, dict) else None
                    if isinstance(candidate, str) and self.vfs.is_file(candidate):
                        metadata = candidate
                except (FileNotFoundError, ValueError):
                    pass
                if metadata is None:
                    result = await run_compiler(job, command, capture=True)
                    if result.returncode:
                        if optional:
                            self.sources[key] = {}
                            return
                        job.write(result.stderr)
                        raise subprocess.CalledProcessError(result.returncode, command)
                    metadata = self.vfs.abspath(result.stdout.decode().strip())
                    if not self.vfs.is_file(metadata):
                        if optional:
                            self.sources[key] = {}
                            return
                        if clang:
                            raise RuntimeError(f'{compiler} did not locate a standard-library module manifest; '
                                               'select a module-capable C++ library/toolchain')
                        raise RuntimeError(f'{compiler} does not provide libstdc++.modules.json; '
                                           f'cannot build import {name}')
                try:
                    document = json.loads(self.vfs.read_text(metadata))
                    if document['version'] != 1:
                        raise ValueError('unsupported metadata version')
                    sources = {}
                    local_flags = {}
                    for module in document['modules']:
                        logical_name = module['logical-name']
                        if logical_name in ('std', 'std.compat'):
                            path = os.path.normpath(os.path.join(
                                os.path.dirname(metadata), module['source-path']))
                            if not self.vfs.is_file(path):
                                raise ValueError(f'module source does not exist: {path}')
                            sources[logical_name] = path
                            local_flags[logical_name] = tuple(
                                '-isystem' + os.path.normpath(os.path.join(os.path.dirname(metadata), directory))
                                for directory in module.get('local-arguments', {}).get('system-include-directories', []))
                except (KeyError, TypeError, ValueError) as error:
                    raise RuntimeError(f'Invalid GCC module metadata {metadata}: {error}') from error
                self.vfs.makedirs(cache_dir, exist_ok=True)
                temporary = cache + '.' + uuid4().hex + '.tmp'
                self.vfs.write_text(temporary, json.dumps({'metadata': metadata}) + '\n')
                self.vfs.replace(temporary, cache)
                self.sources[key] = sources
                self.local_flags[key] = local_flags

            job = parent.session.schedule(('gcc-std-modules', key), discover, parent=parent)
            await parent.wait_for_dependency(job)
        if name not in self.sources[key]:
            if optional:
                return ''
            raise RuntimeError(f'GCC module metadata does not provide {name}')
        return self.sources[key][name]
