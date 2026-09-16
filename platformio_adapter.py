"""SCons integration for explicitly requested application modules using GCC."""

import json
from pathlib import Path
import shlex
import subprocess
import sys
from typing import Any


def configure(env: Any, projenv: Any, root: Path) -> None:
    """Register module builds for projenv, using library env and source root."""
    modules = env.GetProjectOption('custom_buildtool_modules', '').split()
    if not modules:
        return
    if projenv.get('BUILDTOOL_MODULE_ROOT'):
        raise ValueError('Only one buildtool library root is supported per PlatformIO environment')
    projenv['BUILDTOOL_MODULE_ROOT'] = str(root)
    directory = Path(env.subst('$BUILD_DIR')).resolve() / 'buildtool-modules'
    directory.mkdir(parents=True, exist_ok=True)

    def tool(variable: str) -> str:
        """Resolve a tool variable through PlatformIO's executable search path."""
        name = env.subst('$' + variable)
        path = env.WhereIs(name)
        if not path:
            raise ValueError(f'Cannot locate PlatformIO tool {name}')
        return str(Path(path).absolute())

    def flags(language: str) -> list[str]:
        """Expand compiler flags and definitions with absolute include paths."""
        return shlex.split(env.subst('$CCFLAGS $' + language + 'FLAGS $_CPPDEFFLAGS $_CPPINCFLAGS'))

    settings = dict(root=str(root), modules=modules, cc=tool('CC'), cxx=tool('CXX'),
                    archiver=tool('AR'), tags=list(env.get('PIOFRAMEWORK', [])))
    # PlatformIO may implement RANLIB as AR with flags; use GCC's actual ranlib.
    settings['ranlib'] = subprocess.check_output(
        [settings['cxx'], '-print-prog-name=ranlib'], text=True).strip()
    settings['ranlib'] = str(Path(env.WhereIs(settings['ranlib']) or settings['ranlib']).resolve())
    # Flag paths may be relative to the application, not to the baselib checkout.
    from cmake_modules import absolute_file_flags
    project = Path(env.subst('$PROJECT_DIR'))
    manifest = directory / 'request.json'

    def compile_modules(target: Any, source: Any, env: Any) -> int:
        """Capture resolved library flags and build modules before application compilation."""
        # Library scripts run before PlatformIO propagates dependency include paths.
        # Read the library environment only after SCons has constructed the graph.
        settings['cflags'] = flags('C')
        settings['cxxflags'] = [flag for flag in flags('CXX')
                                if not flag.startswith('-std=')] + ['-std=gnu++23']
        for field in ('cflags', 'cxxflags'):
            settings[field] = absolute_file_flags(settings[field], project)
            settings[field].append('-I' + str(root))
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
    # The archive replaces PlatformIO's ordinary compilation of baselib sources.
    env.Replace(SRC_FILTER=['-<*>'])
