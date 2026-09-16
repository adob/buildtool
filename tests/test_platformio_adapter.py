"""Regression coverage for dependency flags populated after library discovery."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import buildtool as bt
from platformio_adapter import configure, merge_include_flags
from platformio_modules import LibraryTarget


def library_environment(root: Path, includes: list[str], modules: str = 'lib.types') -> MagicMock:
    """Mock a PlatformIO library environment whose include flags are read lazily."""
    library = MagicMock()
    library.GetProjectOption.side_effect = lambda name, default=None: modules if name == 'buildtool_modules' else default
    library.get.return_value = ['arduino']
    variables = {'$BUILD_DIR': str(root / 'build'), '$PROJECT_DIR': str(root),
                 '$CC': '/usr/bin/gcc', '$CXX': '/usr/bin/g++', '$AR': '/usr/bin/ar'}
    library.subst.side_effect = lambda expression: variables.get(expression, ' '.join(includes))
    library.WhereIs.side_effect = lambda name: name
    return library


def project_environment() -> MagicMock:
    """Mock the application environment, storing registrations like a SCons environment."""
    project = MagicMock()
    variables: dict[str, object] = {}
    project.get.side_effect = lambda key, default=None: variables.get(key, default)
    project.__setitem__.side_effect = variables.__setitem__
    return project


class PlatformIOAdapterTests(unittest.TestCase):
    def test_module_prefix_maps_to_project_root(self) -> None:
        """A package prefix strips from submodules whose files live directly in its root."""
        fs = bt.MemoryFileSystem()
        fs.makedirs('deps/serialrpc')
        fs.write_text('deps/serialrpc/serialrpc.cc', 'export module serialrpc;')
        fs.write_text('deps/serialrpc/server.cc', 'export module serialrpc.server;')
        target = LibraryTarget(
            bt.BuildConfig(vfs=fs, SRCDIR='.'), [], {'serialrpc': 'deps/serialrpc'})
        self.assertEqual(target.mod2src('serialrpc', bt.SourceType.MODULE),
                         bt.Path('deps/serialrpc/serialrpc.cc'))
        self.assertEqual(target.mod2src('serialrpc.server', bt.SourceType.MODULE),
                         bt.Path('deps/serialrpc/server.cc'))

    def test_dependency_flags_are_read_at_action_time(self) -> None:
        """Includes added after registration must reach both module compiler commands."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            includes: list[str] = []
            library = library_environment(root, includes)
            project = project_environment()
            with patch('platformio_adapter.subprocess.check_output', return_value='/usr/bin/ranlib\n'):
                configure(library, project, root)
            action = project.Command.call_args.args[2]
            includes.append('-I' + str(root / 'dependency/include'))
            with patch('platformio_adapter.subprocess.call', return_value=0):
                self.assertEqual(action([], [], project), 0)
            request = json.loads((root / 'build/buildtool-modules/request.json').read_text())
            for field in ('cflags', 'cxxflags'):
                self.assertIn(includes[0], request[field])
            self.assertEqual(request['roots'], [str(root)])
            self.assertEqual(request['sources'], [])

    def test_libraries_share_one_action(self) -> None:
        """A second library adds its sources, roots, and includes to the first registration."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base, dependent = root / 'base', root / 'dependent'
            first = library_environment(root, ['-I' + str(base / 'deps/fmt/include')])
            second = library_environment(root, ['-I' + str(root / 'build/prefix')])
            project = project_environment()
            with patch('platformio_adapter.subprocess.check_output', return_value='/usr/bin/ranlib\n'):
                configure(first, project, base)
                configure(second, project, dependent, sources=[dependent / 'rpc.cc'],
                          search_roots=[base], module_roots={'rpc': dependent})
            project.Command.assert_called_once()
            first.Replace.assert_called_once_with(SRC_FILTER=['-<*>'])
            second.Replace.assert_called_once_with(SRC_FILTER=['-<*>'])
            action = project.Command.call_args.args[2]
            with patch('platformio_adapter.subprocess.call', return_value=0):
                self.assertEqual(action([], [], project), 0)
            request = json.loads((root / 'build/buildtool-modules/request.json').read_text())
            self.assertEqual(request['roots'], [str(base), str(dependent)])
            self.assertEqual(request['sources'], [str(dependent / 'rpc.cc')])
            self.assertEqual(request['module_roots'], {'rpc': str(dependent)})
            for flag in ('-I' + str(base / 'deps/fmt/include'), '-I' + str(root / 'build/prefix'),
                         '-I' + str(base), '-I' + str(dependent)):
                self.assertIn(flag, request['cxxflags'])
            with self.assertRaises(ValueError):
                configure(second, project, dependent, sources=[dependent / 'rpc.cc'])

    def test_merge_include_flags_keeps_other_flags_once(self) -> None:
        """Only missing include options are appended; repeated toolchain flags are ignored."""
        base = ['-Os', '-I', '/a', '-isystem/b', '-include', 'x.h']
        merged = merge_include_flags(base, ['-Os', '-I', '/a', '-I/c', '-include', 'y.h', '-isystem/b'])
        self.assertEqual(merged, [*base, '-I/c'])
