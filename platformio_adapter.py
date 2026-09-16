"""SCons integration for buildtool-compiled library sources and application modules using GCC."""

import json
from pathlib import Path
import shlex
import subprocess
import sys
from typing import Any

INCLUDE_OPTIONS = ('-I', '-isystem', '-iquote', '-idirafter')


def merge_include_flags(base: list[str], extra: list[str]) -> list[str]:
    """Return base plus the include-directory options from extra that base lacks."""
    result = list(base)
    index = 0
    while index < len(extra):
        flag = extra[index]
        if flag in INCLUDE_OPTIONS and index + 1 < len(extra):
            candidate = [flag, extra[index + 1]]
            index += 2
        elif flag.startswith(INCLUDE_OPTIONS):
            candidate = [flag]
            index += 1
        else:
            index += 1
            continue
        if not any(result[position:position + len(candidate)] == candidate
                   for position in range(len(result))):
            result.extend(candidate)
    return result


def configure(env: Any, projenv: Any, root: Path, sources: list[Path] | tuple[Path, ...] = (),
              search_roots: list[Path] | tuple[Path, ...] = (),
              module_roots: dict[str, Path] | None = None) -> None:
    """Register a buildtool library: root, its ordinary sources, and dependency module roots.

    Libraries call this from their PlatformIO extra scripts. Sources are C/C++ files
    that buildtool compiles so their module imports resolve. Named modules requested
    by the project (buildtool_modules) and imports discovered while compiling
    are looked up in every registered root and search root. All registrations in a
    PlatformIO environment share one SCons action, archive, and module map.
    """
    modules = env.GetProjectOption('buildtool_modules', '').split()
    if not modules and not sources:
        return
    registration = dict(root=Path(root).resolve(), env=env,
                        sources=[Path(source).resolve() for source in sources],
                        search_roots=[Path(item).resolve() for item in search_roots],
                        module_roots={name: Path(path).resolve()
                                      for name, path in (module_roots or {}).items()})
    # The archive replaces PlatformIO's ordinary compilation of this library's sources.
    env.Replace(SRC_FILTER=['-<*>'])
    libraries = projenv.get('BUILDTOOL_LIBRARIES')
    if libraries is not None:
        if any(item['root'] == registration['root'] for item in libraries):
            raise ValueError(f'buildtool library {root} is registered twice in one PlatformIO environment')
        libraries.append(registration)
        return
    libraries = [registration]
    projenv['BUILDTOOL_LIBRARIES'] = libraries
    directory = Path(env.subst('$BUILD_DIR')).resolve() / 'buildtool-modules'
    directory.mkdir(parents=True, exist_ok=True)
    # Flag paths may be relative to the application, not to the library checkouts.
    from cmake_modules import absolute_file_flags
    project = Path(env.subst('$PROJECT_DIR'))

    def tool(variable: str) -> str:
        """Resolve a tool variable through PlatformIO's executable search path."""
        name = env.subst('$' + variable)
        path = env.WhereIs(name)
        if not path:
            raise ValueError(f'Cannot locate PlatformIO tool {name}')
        return str(Path(path).absolute())

    def flags(libenv: Any, language: str) -> list[str]:
        """Expand a library environment's flags and definitions with absolute include paths."""
        return absolute_file_flags(
            shlex.split(libenv.subst('$CCFLAGS $' + language + 'FLAGS $_CPPDEFFLAGS $_CPPINCFLAGS')),
            project)

    settings = dict(modules=modules, cc=tool('CC'), cxx=tool('CXX'),
                    archiver=tool('AR'), tags=list(env.get('PIOFRAMEWORK', [])))
    # PlatformIO may implement RANLIB as AR with flags; use GCC's actual ranlib.
    settings['ranlib'] = subprocess.check_output(
        [settings['cxx'], '-print-prog-name=ranlib'], text=True).strip()
    settings['ranlib'] = str(Path(env.WhereIs(settings['ranlib']) or settings['ranlib']).resolve())
    manifest = directory / 'request.json'

    def compile_modules(target: Any, source: Any, env: Any) -> int:
        """Capture resolved library flags and build modules before application compilation."""
        # Library scripts run before PlatformIO propagates dependency include paths.
        # Read the library environments only after SCons has constructed the graph.
        roots: list[str] = []
        for item in libraries:
            for candidate in (item['root'], *item['search_roots']):
                if str(candidate) not in roots:
                    roots.append(str(candidate))
        settings['roots'] = roots
        settings['sources'] = sorted({str(path) for item in libraries for path in item['sources']})
        prefixes: dict[str, str] = {}
        for item in libraries:
            for name, path in item['module_roots'].items():
                value = str(path)
                if name in prefixes and prefixes[name] != value:
                    raise ValueError(f'Module prefix {name} maps to both {prefixes[name]} and {value}')
                prefixes[name] = value
        settings['module_roots'] = prefixes
        for field, language in (('cflags', 'C'), ('cxxflags', 'CXX')):
            # Libraries share the toolchain flags; only their dependency includes differ.
            merged = flags(libraries[0]['env'], language)
            for item in libraries[1:]:
                merged = merge_include_flags(merged, flags(item['env'], language))
            if language == 'CXX':
                merged = [flag for flag in merged if not flag.startswith('-std=')] + ['-std=gnu++23']
            merged.extend('-I' + item['root'].as_posix() for item in libraries)
            settings[field] = merged
        content = json.dumps(settings, sort_keys=True, indent=2)
        if not manifest.exists() or manifest.read_text() != content:
            manifest.write_text(content)
        return subprocess.call([sys.executable, str(Path(__file__).with_name('platformio_modules.py')),
                                str(manifest)])

    outputs = [str(directory / name) for name in ('libmodules.a', 'consumer.rsp', 'state.h', 'consumer.modmap')]
    job = projenv.Command(outputs, [], compile_modules)
    projenv.AlwaysBuild(job)
    projenv.Clean(job, str(directory))
    projenv.Replace(CXXFLAGS=[flag for flag in projenv.get('CXXFLAGS', [])
                             if not str(flag).startswith('-std=')])
    projenv.Append(CXXFLAGS=['-std=gnu++23', '@' + str(directory / 'consumer.rsp')],
                   LIBS=[projenv.File(outputs[0])])

    def application_object(buildenv: Any, node: Any) -> Any:
        """Order application objects after module publication, leaving framework sources alone."""
        path = Path(node.srcnode().abspath)
        if path.is_relative_to(Path(env.subst('$PROJECT_SRC_DIR'))):
            obj = buildenv.Object(node)
            buildenv.Depends(obj, job)
            return obj
        return node

    projenv.AddBuildMiddleware(application_object)
