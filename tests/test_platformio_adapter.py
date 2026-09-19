"""Regression coverage for dependency flags populated after library discovery."""

import asyncio
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import buildtool as bt
from platformio_adapter import configure, merge_include_flags
from platformio_modules import LibraryTarget, module_namespace_search_root


def library_environment(root: Path, includes: list[str], modules: str = 'lib.types') -> MagicMock:
    """Mock a PlatformIO library environment whose include flags are read lazily."""
    library = MagicMock()
    library.GetProjectOption.side_effect = lambda name, default=None: modules if name == 'buildtool_modules' else default
    library.get.return_value = ['arduino']
    variables = {'$BUILD_DIR': str(root / 'build'), '$PROJECT_DIR': str(root),
                 '$PROJECT_SRC_DIR': str(root / 'src'),
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
    project.GetOption.side_effect = lambda name: 6 if name == 'num_jobs' else None
    return project


class PlatformIOAdapterTests(unittest.TestCase):
    def test_module_namespace_search_root_uses_prefix_parent(self) -> None:
        """Mapped host imports can resolve a prefix whose directory has the same suffix."""
        self.assertEqual(
            module_namespace_search_root(
                'serialrpc', Path('/workspace/third_party/serialrpc')),
            Path('/workspace/third_party'))
        self.assertEqual(
            module_namespace_search_root(
                'foo.bar', Path('/workspace/vendor/foo/bar')),
            Path('/workspace/vendor'))
        self.assertIsNone(
            module_namespace_search_root(
                'rpc', Path('/workspace/dependent')))

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

    def test_mapped_module_falls_back_to_library_generated_action(self) -> None:
        """A mapped library may materialize a missing module through its own BUILD.py."""
        fs = bt.MemoryFileSystem()
        fs.makedirs('deps/serialrpc/generated')
        fs.makedirs('/tools')
        fs.write_text('/tools/gen', 'generator')
        fs.write_text(
            'deps/serialrpc/generated/BUILD.py',
            '''GENERATED = [{
    "outputs": ["serialrpc_protocol/msg.cc"],
    "command": ["/tools/gen", "{outdir}/serialrpc_protocol/msg.cc"],
}]\n''',
        )
        cfg = bt.BuildConfig(
            vfs=fs, SRCDIR='.', OBJDIR='build/target', DEPDIR='build/target/deps',
            USE_DIRECTORY_CONFIG=False, STD_HEADER_UNIT=False,
        )
        generated_cfg = bt.BuildConfig(
            vfs=fs, SRCDIR='deps/serialrpc', OBJDIR='build/generated',
            DEPDIR='build/generated/deps', STD_HEADER_UNIT=False,
        )
        target = LibraryTarget(
            cfg, [], {'serialrpc': 'deps/serialrpc'}, {'serialrpc': generated_cfg})

        async def generate(job: bt.Job, command: list[str], **kwargs: object):
            output = bt.Path(command[-1])
            fs.makedirs(output.parent, exist_ok=True)
            fs.write_text(output, 'export module serialrpc.generated.serialrpc_protocol.msg;\n')
            return subprocess.CompletedProcess(command, 0, b'', b'')

        async def run() -> bt.Path:
            graph = bt.CompilationGraph(cfg)
            target.session = graph.session
            target.job_sources = graph.job_sources
            target.link_events = graph.link_events
            result: bt.Path | None = None

            async def resolve(job: bt.Job) -> None:
                nonlocal result
                result = await target.resolve_module_source(
                    'serialrpc.generated.serialrpc_protocol.msg',
                    bt.SourceType.MODULE, None, job)

            try:
                root = graph.session.schedule('resolve', resolve)
                await graph.session.finish()
                if root.error:
                    raise root.error
                assert result is not None
                return result
            finally:
                await graph.session.close()
                target.session = None

        with patch.object(bt, 'run_compiler', side_effect=generate):
            path = asyncio.run(run())
        self.assertEqual(
            path, bt.Path('build/generated/generated/generated/serialrpc_protocol/msg.cc'))
        self.assertEqual(
            cfg.generated_logical_paths[path],
            bt.Path('__generated__/serialrpc/generated/serialrpc_protocol/msg.cc'))

    def test_mapped_generated_module_can_depend_outside_mapped_root(self) -> None:
        """Generation uses the shared source root while lookup stays below its mapped root."""
        fs = bt.MemoryFileSystem()
        fs.makedirs('proto')
        fs.makedirs('third_party/serialrpc')
        fs.makedirs('/tools')
        fs.write_text('/tools/gen', 'generator')
        fs.write_text('third_party/serialrpc/serialrpc.proto', 'syntax = "proto3";\n')
        fs.write_text(
            'proto/BUILD.py',
            '''GENERATED = [{
    "inputs": ["../third_party/serialrpc/serialrpc.proto"],
    "outputs": ["controller/server.cc"],
    "command": ["/tools/gen", "{outdir}/controller/server.cc"],
}]\n''',
        )
        cfg = bt.BuildConfig(
            vfs=fs, SRCDIR='.', OBJDIR='build/target', DEPDIR='build/target/deps',
            USE_DIRECTORY_CONFIG=False, STD_HEADER_UNIT=False,
        )
        generated_cfg = bt.BuildConfig(
            vfs=fs, SRCDIR='.', OBJDIR='build/generated',
            DEPDIR='build/generated/deps', STD_HEADER_UNIT=False,
        )
        target = LibraryTarget(
            cfg, [], {'proto': 'proto'}, {'proto': generated_cfg})

        async def generate(job: bt.Job, command: list[str], **kwargs: object):
            output = bt.Path(command[-1])
            fs.makedirs(output.parent, exist_ok=True)
            fs.write_text(output, 'export module proto.controller.server;\n')
            return subprocess.CompletedProcess(command, 0, b'', b'')

        async def run() -> bt.Path:
            graph = bt.CompilationGraph(cfg)
            target.session = graph.session
            target.job_sources = graph.job_sources
            target.link_events = graph.link_events
            result: bt.Path | None = None

            async def resolve(job: bt.Job) -> None:
                nonlocal result
                result = await target.resolve_module_source(
                    'proto.controller.server', bt.SourceType.MODULE, None, job)

            try:
                root = graph.session.schedule('resolve', resolve)
                await graph.session.finish()
                if root.error:
                    raise root.error
                assert result is not None
                return result
            finally:
                await graph.session.close()
                target.session = None

        with patch.object(bt, 'run_compiler', side_effect=generate):
            path = asyncio.run(run())
        self.assertEqual(
            path, bt.Path('build/generated/generated/proto/controller/server.cc'))
        self.assertEqual(
            cfg.generated_logical_paths[path],
            bt.Path('__generated__/proto/controller/server.cc'))

    def test_explicit_requested_module_materializes_generated_source(self) -> None:
        """PlatformIO's top-level module list uses generated-aware resolution."""
        fs = bt.MemoryFileSystem()
        fs.makedirs('proto')
        fs.makedirs('/tools')
        fs.write_text('/tools/gen', 'generator')
        fs.write_text(
            'proto/BUILD.py',
            '''GENERATED = [{
    "outputs": ["controller/server.cc"],
    "command": ["/tools/gen", "{outdir}/controller/server.cc"],
}]\n''',
        )
        cfg = bt.BuildConfig(
            vfs=fs, SRCDIR='.', OBJDIR='build/target', DEPDIR='build/target/deps',
            USE_DIRECTORY_CONFIG=False, STD_HEADER_UNIT=False,
        )
        generated_cfg = bt.BuildConfig(
            vfs=fs, SRCDIR='.', OBJDIR='build/generated',
            DEPDIR='build/generated/deps', STD_HEADER_UNIT=False,
        )
        target = LibraryTarget(
            cfg, [], {'proto': 'proto'}, {'proto': generated_cfg})

        async def generate(job: bt.Job, command: list[str], **kwargs: object):
            output = bt.Path(command[-1])
            fs.makedirs(output.parent, exist_ok=True)
            fs.write_text(output, 'export module proto.controller.server;\n')
            return subprocess.CompletedProcess(command, 0, b'', b'')

        with patch.object(bt, 'run_compiler', side_effect=generate):
            paths = target.resolve_requested_modules(['proto.controller.server'])

        self.assertEqual(
            paths, [bt.Path('build/generated/generated/proto/controller/server.cc')])

    def test_mapped_module_error_keeps_requested_name(self) -> None:
        """Mapped filesystem lookup must not expose its stripped internal module name."""
        fs = bt.MemoryFileSystem()
        fs.makedirs('proto')
        target = LibraryTarget(
            bt.BuildConfig(vfs=fs, SRCDIR='.'), [], {'proto': 'proto'})
        with self.assertRaisesRegex(
                RuntimeError, r'^Unable to locate module proto\.controller\.server:'):
            target.mod2src('proto.controller.server', bt.SourceType.MODULE)

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
            self.assertEqual(request['jobs'], 6)

    def test_consumer_flags_only_apply_to_application_objects(self) -> None:
        """Framework/library compilations must not race buildtool's consumer response file."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            library = library_environment(root, [])
            project = project_environment()
            project.File.side_effect = lambda path: path
            with patch('platformio_adapter.subprocess.check_output',
                       return_value='/usr/bin/ranlib\n'):
                configure(library, project, root)

            project.Replace.assert_not_called()
            self.assertEqual(project.Append.call_args_list[-1].kwargs,
                             {'LIBS': [str(root / 'build/buildtool-modules/libmodules.a')]})

            middleware = project.AddBuildMiddleware.call_args.args[0]
            buildenv = MagicMock()
            buildenv.get.side_effect = lambda key, default=None: (
                ['-O2', '-std=gnu++17'] if key == 'CXXFLAGS' else default)
            appenv = MagicMock()
            buildenv.Clone.return_value = appenv
            obj = MagicMock()
            appenv.Object.return_value = obj

            application = MagicMock()
            application.srcnode.return_value.abspath = str(root / 'src/main.cpp')
            self.assertIs(middleware(buildenv, application), obj)
            appenv.Replace.assert_called_once_with(CXXFLAGS=['-O2'])
            appenv.Append.assert_called_once_with(CXXFLAGS=[
                '-std=gnu++23',
                '@' + str(root / 'build/buildtool-modules/consumer.rsp'),
            ])
            appenv.Depends.assert_called_once()

            framework = MagicMock()
            framework.srcnode.return_value.abspath = '/framework/TeensyThreads.cpp'
            self.assertIs(middleware(buildenv, framework), framework)
            self.assertEqual(buildenv.Clone.call_count, 1)

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
