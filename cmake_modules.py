"""Build requested module closures for consumers compiled and linked by CMake."""

from __future__ import annotations

import argparse
from collections.abc import Iterable
from contextlib import ExitStack
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import uuid
from typing import Any

import buildtool as bt
from jobserver import JobServer, make_suppresses_execution
from header_unit_cache import HeaderUnitBusy, HeaderUnitLocks


def lock_files(stack: ExitStack, paths: Iterable[Path]) -> None:
    """Acquire paths in canonical order; stack holds and releases their file locks."""
    for path in sorted({path.resolve() for path in paths}):
        lock = stack.enter_context(path.open('a'))
        fcntl.flock(lock, fcntl.LOCK_EX)


def lines(directory: Path, name: str) -> list[str]:
    """Read manifest field name in directory, omitting empty list entries."""
    return [line for line in (directory / name).read_text().splitlines() if line]


def native_modules(directory: Path) -> bool:
    """Return whether directory's manifest requests native CMake module integration."""
    return (directory / 'native_modules').read_text().strip() == 'TRUE'


def write_changed(path: Path, text: str) -> None:
    """Atomically write text to path only when its contents changed."""
    if path.exists() and path.read_text() == text:
        return
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(text)
    temporary.replace(path)


def cmake_targets(directory: Path) -> tuple[Path, list[dict[str, Any]]]:
    """Read directory's configured CMake targets and the top-level binary directory."""
    reply = Path(lines(directory, 'reply_directory')[0])
    indexes = sorted(reply.glob('index-*.json'))
    if not indexes:
        raise RuntimeError('CMake file API reply is missing; rerun CMake configuration')
    index = json.loads(indexes[-1].read_text())
    model_file = next(item['jsonFile'] for item in index['objects'] if item['kind'] == 'codemodel')
    model = json.loads((reply / model_file).read_text())
    config_name = (directory / 'configuration').read_text().strip()
    config = next(item for item in model['configurations'] if item['name'] == config_name)
    targets = [json.loads((reply / entry['jsonFile']).read_text()) for entry in config['targets']]
    for target in targets:
        for source in target.get('sources', []):
            source['path'] = str((Path(model['paths']['source']) / source['path']).resolve())
    return Path(model['paths']['build']), targets


def compilation_group(directory: Path, language: str = 'CXX') -> tuple[dict[str, Any], Path]:
    """Load directory's library settings for language (C/CXX) and compiler working directory."""
    build_directory, targets = cmake_targets(directory)
    library = lines(directory, 'name')[0]
    target = next(item for item in targets if item['name'] == library)
    groups = [group for group in target.get('compileGroups', []) if group['language'] == language]
    if len(groups) != 1:
        raise RuntimeError(f'{library} must have one {language} settings group; '
                           'do not add source files to a registered buildtool library')
    group = groups[0]
    if group.get('precompileHeaders'):
        raise RuntimeError('Precompiled headers on registered libraries are not supported')
    working_directory = build_directory / target['paths']['build']
    return group, working_directory


def compiler_flags(directory: Path, language: str = 'CXX') -> list[str]:
    """Read directory's C/CXX flags for language, rebasing paths to the compiler's cwd."""
    group, working_directory = compilation_group(directory, language)
    argument_field = 'c_compiler_arg1' if language == 'C' else 'compiler_arg1'
    flags = shlex.split((directory / argument_field).read_text())
    flags.extend('-D' + item['define'] for item in group.get('defines', []))
    for include in group.get('includes', []):
        flags.extend(['-isystem' if include.get('isSystem') else '-I', include['path']])
    for fragment in group.get('compileCommandFragments', []):
        flags.extend(shlex.split(fragment['fragment']))
    if any(flag == '-flto' or flag.startswith('-flto=') for flag in flags):
        raise RuntimeError('IPO/LTO is not supported by the module bridge')
    return absolute_file_flags(flags, working_directory)


def absolute_file_flags(flags: list[str], directory: Path) -> list[str]:
    """Resolve common compiler file/directory options against CMake's build directory."""
    options = ('-include', '-imacros', '-I', '-isystem', '-iquote', '-idirafter',
               '-isysroot', '--sysroot', '-B', '-F')
    result = []
    expects_path = False
    for flag in flags:
        if expects_path:
            result.append(str(directory / flag))
            expects_path = False
        elif flag in options:
            result.append(flag)
            expects_path = True
        elif flag.startswith('@'):
            raise RuntimeError('Compiler response files on registered libraries are not supported')
        else:
            for prefix in ('--sysroot=', '-isystem', '-iquote', '-idirafter', '-I', '-B', '-F'):
                if flag.startswith(prefix) and len(flag) > len(prefix):
                    result.append(prefix + str(directory / flag[len(prefix):]))
                    break
            else:
                result.append(flag)
    if expects_path:
        raise RuntimeError('Compiler path option is missing its argument')
    return result


class ModuleTarget(bt.Target):
    """Discover modules in registered source roots, retaining relative project paths."""

    def __init__(self, cfg: bt.BuildConfig, roots: list[str],
                 projects: dict[Path, ModuleTarget],
                 cmake_sources: frozenset[Path] = frozenset()) -> None:
        """Route sources through projects; cmake_sources are compiled by native CMake targets."""
        super().__init__(bt.Path('cmake-modules'), cfg)
        self.source_roots = roots
        self.projects = projects
        self.cmake_sources = cmake_sources
        self.header_target: ModuleTarget | None = None

    def should_build_companion(self, path: bt.Path) -> bool:
        """Leave companion path to CMake when its file API lists a native compilation."""
        return Path(str(path)).resolve() not in self.cmake_sources

    def schedule_sources(
        self, sources: Iterable[bt.Path | tuple[bt.Path, bt.SourceType | None, str | None,
                                               bt.DirectoryConfig | None]],
        graph: bt.CompilationGraph,
    ) -> None:
        """Attach all library configurations to graph before scheduling root sources."""
        for project in self.projects.values():
            project.session = graph.session
            project.job_sources = graph.job_sources
            project.link_events = graph.link_events
        if self.header_target is not None:
            self.header_target.session = graph.session
            self.header_target.job_sources = graph.job_sources
            self.header_target.link_events = graph.link_events
        super().schedule_sources(sources, graph)

    def mod2src(self, modname: str | None, type: bt.SourceType) -> bt.Path:
        """Resolve modname/type normally; relative sources enable companion discovery."""
        path = super().mod2src(modname, type,
                              search_roots=self.source_roots if type == bt.SourceType.MODULE else None)
        if type == bt.SourceType.MODULE:
            return bt.Path(os.path.relpath(str(path)))
        return path

    def schedule_compilation_job(self, path: bt.Path, type: bt.SourceType | None = None,
                                 modname: str | None = None,
                                 inherited_dircfg: bt.DirectoryConfig | None = None,
                                 parent: bt.Job | None = None) -> bt.Job:
        """Route C/C++ sources to their owner; assembly requires a native CMake target."""
        if type in (bt.SourceType.USER_HEADER, bt.SourceType.SYSTEM_HEADER) and self.header_target is not None:
            absolute = Path(str(path)).resolve()
            return self.header_target.schedule_compilation_job(
                bt.Path(str(absolute)), type, str(absolute), parent=parent)
        if path.suffix in ('.s', '.S'):
            raise RuntimeError(f'Build {path} as a native CMake dependency; '
                               'the module bridge does not compile assembly')
        if (type == bt.SourceType.MODULE or
                (type not in (bt.SourceType.USER_HEADER, bt.SourceType.SYSTEM_HEADER)
                 and path.suffix in (*bt.CCFILE_SUFFIXES, '.c'))):
            absolute = Path(str(path)).resolve()
            # Include-prefix symlinks must not schedule a second object for the same source.
            path = bt.Path(os.path.relpath(absolute))
            # The most specific registered root owns a named module or companion.
            # Header units retain their importing library's compilation settings.
            for root, project in self.projects.items():
                if absolute.is_relative_to(root):
                    if project is not self:
                        job = project.schedule_compilation_job(path, type, modname, inherited_dircfg, parent)
                        if parent is None and job not in self.roots:
                            self.roots.append(job)
                        return job
                    break
        if path.suffix == '.c' and not self.cfg.CC:
            raise RuntimeError(f'Enable C in project(... LANGUAGES C CXX) before registering '
                               f'the library that owns {path}')
        return super().schedule_compilation_job(path, type, modname, inherited_dircfg, parent)


class HeaderUnitTarget(ModuleTarget):
    """Compile all header units with one shared configuration and per-file leases."""

    def __init__(self, cfg: bt.BuildConfig, roots: list[str], projects: dict[Path, ModuleTarget],
                 cmake_sources: frozenset[Path], leases: HeaderUnitLocks,
                 header_roots: tuple[Path, ...]) -> None:
        """Use cfg/leases for shared units, projects for companions, and all header_roots for classification."""
        super().__init__(cfg, roots, projects, cmake_sources)
        self.leases = leases
        self.header_roots = header_roots

    def schedule_compilation_job(self, path: bt.Path, type: bt.SourceType | None = None,
                                 modname: str | None = None,
                                 inherited_dircfg: bt.DirectoryConfig | None = None,
                                 parent: bt.Job | None = None) -> bt.Job:
        """Lease a header before inspecting metadata; other sources use normal routing."""
        if type in (bt.SourceType.USER_HEADER, bt.SourceType.SYSTEM_HEADER):
            absolute = Path(str(path)).resolve()
            self.leases.acquire(absolute)
            path, modname = bt.Path(str(absolute)), str(absolute)
            type = (bt.SourceType.USER_HEADER if any(absolute.is_relative_to(root) for root in self.header_roots)
                    else bt.SourceType.SYSTEM_HEADER)
        return super().schedule_compilation_job(path, type, modname, inherited_dircfg, parent)

    async def compile_source(self, source: bt.SourceFile, cfg: bt.BuildConfig) -> None:
        """Publish a completed CMI atomically; retain the previous CMI on cancellation."""
        if source.type not in (bt.SourceType.USER_HEADER, bt.SourceType.SYSTEM_HEADER):
            await super().compile_source(source, cfg)
            return
        final = source.cmpath
        temporary = Path(str(final) + '.' + uuid.uuid4().hex + '.tmp')
        source.cmpath = bt.Path(str(temporary))
        try:
            await source.compile(self, cfg)
            temporary.replace(str(final))
        finally:
            source.cmpath = final
            temporary.unlink(missing_ok=True)


def configuration(directory: Path) -> bt.BuildConfig:
    """Build this library with its own CMake settings and per-configuration artifacts."""
    compiler = lines(directory, 'compiler')[0]
    c_compiler = (directory / 'c_compiler').read_text().strip()
    artifacts = directory / 'artifacts'
    # Absolute BMI dependencies work with both standalone and native CMake maps,
    # allowing both consumer modes to reuse the same library compilation.
    # CMake's evaluated flags already contain the library's include directories.
    return bt.BuildConfig(CXX=compiler, CC=c_compiler, CXXFLAGS=compiler_flags(directory),
                          CFLAGS=compiler_flags(directory, 'C') if c_compiler else [],
                          LDFLAGS=[], INCFLAGS=[], SRCDIR='.',
                          OBJDIR=str(artifacts), DEPDIR=str(artifacts / 'deps'), JOBS=1,
                          memory=bt.MemoryBudget(), progress=True,
                          STD_HEADER_UNIT=False, USE_DIRECTORY_CONFIG=False, ABSOLUTE_MODULE_PATHS=True)


def artifact_manifest(configs: list[bt.BuildConfig]) -> dict[str, Any]:
    """Describe built modules across configs, with the requested library first."""
    modules: dict[str, dict[str, Any]] = {}
    headers: dict[str, dict[str, str]] = {}
    sources = [source for cfg in configs for source in (
        *cfg.source_files.values(), *cfg.std_module_sources.values(), *cfg.std_header_sources.values())]
    for source in sources:
        if not source.processed:
            continue
        if source.type == bt.SourceType.MODULE:
            modules[source.modname] = dict(bmi=str(source.cmpath),
                                           source=str(Path(str(source.path)).resolve()),
                                           imports=sorted(dep.name for dep in source.deps
                                                          if isinstance(dep, bt.ModuleDep)))
        elif source.type in (bt.SourceType.USER_HEADER, bt.SourceType.SYSTEM_HEADER):
            headers[str(Path(str(source.path)).resolve())] = dict(bmi=str(source.cmpath))
    cfg = configs[0]
    return dict(repository=str(cfg.OBJDIR), modules=modules, headers=headers,
                compiler=cfg.compiler_identity(cfg.CXX))


def file_hash(path: Path) -> str:
    """Hash path in bounded chunks; module artifacts can be hundreds of megabytes."""
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def archive_objects(directory: Path, objects: list[bt.Path], library: Path) -> None:
    """Archive objects with library's tools and publish a shared link in directory."""
    # Discovery order can differ between cold and cached builds. Sort the object
    # set so both builds, and different consumers, select the same archive.
    paths = sorted({Path(str(path)) for path in objects})
    tools = [lines(library, name)[0] for name in ('archiver', 'ranlib')]
    key = hashlib.sha256(json.dumps([list(map(str, paths)), tools]).encode()).hexdigest()
    shared = library / 'archives' / key
    shared.mkdir(parents=True, exist_ok=True)
    signature = [(str(path), file_hash(path)) for path in paths]
    signature.extend((tool, str(Path(tool).stat().st_mtime_ns)) for tool in tools)
    contents = json.dumps(signature) + '\n'
    stamp = shared / 'archive.json'
    archive = shared / 'libmodules.a'
    if not archive.is_file() or not stamp.exists() or stamp.read_text() != contents:
        temporary = shared / 'libmodules.a.tmp'
        temporary.unlink(missing_ok=True)
        response = shared / 'objects.rsp'
        write_changed(response, '\n'.join(shlex.quote(str(path)) for path in paths) + '\n')
        # Quick append preserves objects with identical basenames from different
        # directories; replacing archive members by basename would lose definitions.
        subprocess.run([tools[0], 'qc', str(temporary), '@' + str(response)], check=True)
        subprocess.run([tools[1], str(temporary)], check=True)
        temporary.replace(archive)
        write_changed(stamp, contents)
    link_archive(directory / 'libmodules.a', archive)


def link_archive(path: Path, archive: Path) -> None:
    """Atomically point path at archive without changing an already correct link."""
    target = os.path.relpath(archive, path.parent)
    if path.is_symlink() and os.readlink(path) == target:
        return
    temporary = path.with_suffix('.a.tmp')
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(target)
    temporary.replace(path)


def write_module_map(directory: Path, manifest: dict[str, Any]) -> Path:
    """Write named-module mappings from manifest; omit headers to preserve textual includes."""
    # GCC's tuple format uses the remainder of each line as the filename, not
    # shell quoting. Paths with spaces work; literal newlines cannot be represented.
    entries = [('$root', manifest['repository'])]
    entries.extend((name, record['bmi']) for name, record in sorted(manifest['modules'].items()))
    if any('\n' in name or '\r' in name or '\n' in path or '\r' in path
           for name, path in entries):
        raise ValueError('GCC module maps cannot represent paths containing newlines')
    path = directory / 'consumer.modmap'
    write_changed(path, ''.join(f'{name} {filename}\n' for name, filename in entries))
    return path


def publish_consumer_files(directory: Path, manifest: dict[str, Any]) -> None:
    """Publish consumer flags, module mappings, and a header tracking interface changes."""
    metadata = json.dumps(manifest, sort_keys=True, indent=2) + '\n'
    write_changed(directory / 'providers.json', metadata)
    digest = hashlib.sha256(metadata.encode())
    for category in ('modules', 'headers'):
        for name, record in sorted(manifest[category].items()):
            digest.update(name.encode())
            digest.update(file_hash(Path(record['bmi'])).encode())
    # CMake's normal compiler depfiles track this forced include. Its timestamp
    # changes only when the module interface artifacts change, including on Make.
    fingerprint = digest.hexdigest()
    flags = ['-fmodules-ts']
    if native_modules(directory):
        # Native compilations get their sole mapper from CMake's collator.
        # Metadata publication never acquires library locks: all are held already.
        metadata_lock = Path(lines(directory, 'registry')[0]).parent / 'metadata.lock'
        with ExitStack() as locks:
            lock_files(locks, [metadata_lock])
            fingerprint = publish_cmake_metadata(directory, manifest, fingerprint)
    else:
        module_map = write_module_map(directory, manifest)
        flags.extend(['-Mno-modules', '-fmodule-mapper=' + str(module_map)])
    write_changed(directory / 'state.h', '#pragma once\n// Module artifacts: ' + fingerprint + '\n')
    flags.extend(['-include', str(directory / 'state.h')])
    write_changed(directory / 'consumer.rsp', '\n'.join(map(shlex.quote, flags)) + '\n')


def module_usages(modules: dict[str, Any]) -> dict[str, list[str]]:
    """Return each named module's transitive imports, excluding header units."""
    usages = {}
    for name, record in modules.items():
        pending = list(record['imports'])
        visited = {name}
        while pending:
            dependency = pending.pop()
            if dependency in visited or dependency not in modules:
                continue
            visited.add(dependency)
            pending.extend(modules[dependency]['imports'])
        usages[name] = sorted(visited - {name})
    return usages


def publish_cmake_metadata(directory: Path, manifest: dict[str, Any], fingerprint: str) -> str:
    """Publish manifest's providers and update inheriting targets' collation inputs.

    This uses Ninja generator internals. The caller holds the metadata lock;
    target dependencies order publication before consumer scans/collation.
    Return a fingerprint covering both artifacts and provider metadata. Its forced
    header invalidates consumer scans in the same build, so collation runs again.
    """
    modules = manifest['modules']
    build_directory, targets = cmake_targets(directory)
    data = {
        'modules': {name: {'bmi': record['bmi'], 'is-private': False}
                    for name, record in modules.items()},
        # CMake stores references using Ninja paths relative to the top-level build.
        'references': {name: {'path': os.path.relpath(record['bmi'], build_directory),
                              'lookup-method': 'by-name'}
                       for name, record in modules.items()},
        'usages': module_usages(modules),
    }
    metadata = json.dumps(data, sort_keys=True, indent=2) + '\n'
    write_changed(directory / 'CXXModules.json', metadata)
    fingerprint = hashlib.sha256((fingerprint + metadata).encode()).hexdigest()
    configuration = (directory / 'configuration').read_text().strip()
    consumer = lines(directory, 'consumer')[0]
    found_consumer = False
    for target in targets:
        cwd = build_directory / target['paths']['build']
        arguments = [argument for group in target.get('compileGroups', [])
                     if group['language'] == 'CXX'
                     for fragment in group.get('compileCommandFragments', [])
                     for argument in shlex.split(fragment['fragment'])]
        if not any(arg.startswith('@') and (cwd / arg[1:]).resolve() == directory / 'consumer.rsp'
                   for arg in arguments):
            continue
        support = cwd / 'CMakeFiles' / (target['name'] + '.dir')
        candidates = [support / configuration / 'CXXDependInfo.json', support / 'CXXDependInfo.json']
        info = next((path for path in candidates if path.exists()), None)
        if info is None:
            raise RuntimeError(f"Enable CXX_SCAN_FOR_MODULES on {target['name']} to use NATIVE_MODULES")
        content = json.loads(info.read_text())
        if (content.get('config') != configuration or content.get('language') != 'CXX'
                or not isinstance(content.get('linked-target-dirs'), list)):
            raise RuntimeError(f'Unsupported CMake collation metadata in {info}')
        if str(directory) not in content['linked-target-dirs']:
            content['linked-target-dirs'].append(str(directory))
        content['buildtool-fingerprint'] = fingerprint
        write_changed(info, json.dumps(content, sort_keys=True, indent=2) + '\n')
        found_consumer |= target['name'] == consumer
    if not found_consumer:
        raise RuntimeError('CMake file API did not expose the module consumer response file')
    return fingerprint


def build_modules(directory: Path) -> None:
    """Retry directory's build after contention, releasing all leases before waiting."""
    while True:
        try:
            build_modules_attempt(directory)
            return
        except HeaderUnitBusy as busy:
            busy.wait()


def build_modules_attempt(directory: Path) -> None:
    """Build directory's requested modules or source files and publish CMake artifacts."""
    library = Path(lines(directory, 'library')[0])
    registry = Path(lines(directory, 'registry')[0])
    config = (directory / 'configuration').read_text().strip()
    _, cmake_projects = cmake_targets(directory)
    names = {target['name'] for target in cmake_projects}
    cmake_sources = frozenset(Path(source['path']) for target in cmake_projects
                             for source in target.get('sources', [])
                             if 'compileGroupIndex' in source)
    # All registered roots give a stable working directory across consumer jobs,
    # including a dependency built both on its own and through another library.
    registrations: dict[Path, Path] = {}
    for entry in registry.iterdir():
        if entry.name == 'buildtool_header_units':
            continue
        if entry.name not in names or not (entry / config / 'root').exists():
            continue
        root = Path(lines(entry / config, 'root')[0])
        if root in registrations:
            raise RuntimeError(f'Multiple registered libraries own source root {root}')
        registrations[root] = entry / config
    projects: dict[Path, ModuleTarget] = {}
    active: set[Path] = set()
    pending = [Path(lines(library, 'root')[0])]
    while pending:
        root = pending.pop()
        if root in active:
            continue
        active.add(root)
        pending.extend(Path(path) for path in lines(registrations[root], 'roots'))
    with ExitStack() as locks:
        # Acquire the entire registered dependency set before reading cached build
        # state. A waiting invocation then observes its predecessor's new outputs.
        lock_files(locks, (registrations[root] / 'build.lock' for root in active))
        jobserver = JobServer.from_environment()
        if jobserver is not None:
            locks.callback(jobserver.close)
        locks.callback(os.chdir, Path.cwd())
        # Keep all registered project paths relative without leading '..', so
        # GCC header-unit filenames remain inside the artifact repository.
        os.chdir(os.path.commonpath(list(map(str, registrations))))
        for root in sorted(active, key=lambda path: (-len(path.parts), str(path))):
            manifest = registrations[root]
            roots = list(dict.fromkeys(lines(manifest, 'roots')))
            projects[root] = ModuleTarget(configuration(manifest), roots, projects, cmake_sources)
            if jobserver is not None:
                cfg = projects[root].cfg
                cfg.jobserver = jobserver
                cfg.JOBS = os.cpu_count() or 1
        header_manifest = registry / 'buildtool_header_units' / config
        headers = HeaderUnitTarget(configuration(header_manifest),
            list(dict.fromkeys(path for project in projects.values() for path in project.source_roots)),
            projects, cmake_sources, HeaderUnitLocks(header_manifest / 'locks', locks), tuple(registrations))
        for project in projects.values():
            project.header_target = headers
        target = projects[Path(lines(library, 'root')[0])]
        source_build = (directory / 'sources').exists()
        if source_build:
            sources = [Path(path).resolve() for path in lines(directory, 'sources')]
            for source in sources:
                if not any(source.is_relative_to(root) for root in projects):
                    raise RuntimeError(f'Source {source} is outside the registered library roots')
            target.compile_many([bt.Path(os.path.relpath(source)) for source in sources])
        else:
            target.compile_many([(target.mod2src(name, bt.SourceType.MODULE),
                                  bt.SourceType.MODULE, name, None)
                                 for name in lines(directory, 'modules')])
        archive_objects(directory, target.objs, library)
        if not source_build or (directory / 'public_modules').read_text().strip() == 'TRUE':
            configs = [target.cfg, *(project.cfg for project in projects.values() if project is not target),
                       headers.cfg]
            publish_consumer_files(directory, artifact_manifest(configs))


def main() -> None:
    """Build modules from a CMake manifest, locking their registered libraries."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', type=Path)
    args = parser.parse_args()
    directory = args.manifest.resolve()
    if make_suppresses_execution():
        return

    def interrupt(signum: int, frame: object) -> None:
        """Turn termination into cancellation so compilers stop before tokens return."""
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGTERM, interrupt)
    try:
        build_modules(directory)
    except subprocess.CalledProcessError as error:
        # Compiler diagnostics have already been streamed; preserve its exit status.
        raise SystemExit(error.returncode if error.returncode > 0 else 128 - error.returncode) from None
    finally:
        signal.signal(signal.SIGTERM, previous)


if __name__ == '__main__':
    main()
