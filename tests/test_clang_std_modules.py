"""Clang standard-module commands, IDE discovery, and optional real builds."""

import contextlib
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest
from unittest import mock

import buildtool as bt
import gcc_std


class StandardModuleIdeTests(unittest.TestCase):
    def test_project_module_uses_query_driver_language(self) -> None:
        """Use GCC's supported probe language for project modules as well as std."""
        for clang in (False, True):
            with self.subTest(clang=clang):
                fs = bt.MemoryFileSystem()
                fs.write_text('library.cc', 'export module library;\n')
                cfg = bt.BuildConfig(vfs=fs, USECLANG=clang)
                bt.SourceFile.get(bt.Path('library.cc'), cfg,
                                  type=bt.SourceType.MODULE, modname='library')
                database = bt.CompilationDatabase([])
                database.process_file(bt.Path('library.cc'), cfg)
                args = database.entries[0]['arguments']
                self.assertIn('-xc++-module' if clang else '-xc++', args)
                if not clang:
                    self.assertNotIn('-xc++-module', args)

    def test_standalone_standard_module_tracks_sdk_headers(self) -> None:
        """SDK headers in standalone Clang depfiles participate in incremental checks."""
        fs = bt.MemoryFileSystem()
        fs.makedirs('/sdk')
        fs.write_text('/sdk/std.cc', '')
        fs.write_text('/sdk/header.h', '')
        cfg = bt.BuildConfig(vfs=fs, USECLANG=True)
        source = bt.SourceFile(bt.Path('/sdk/std.cc'), bt.SourceType.MODULE, 'std', cfg)
        source.std_module_variant = True
        fs.makedirs(source.makefile.parent, exist_ok=True)
        fs.write_text(source.makefile, 'std.pcm: /sdk/std.cc /sdk/header.h\n')
        source.process_makefile_deps()
        self.assertEqual([str(dep.path) for dep in source.deps], ['/sdk/header.h'])

    def test_manifest_sources_and_local_arguments_enter_ide_database(self) -> None:
        """Expose SDK interfaces to clangd without reusing buildtool's PCMs."""
        for clang in (False, True):
            with self.subTest(clang=clang):
                fs = bt.MemoryFileSystem()
                fs.makedirs('/sdk/include')
                fs.write_text('/sdk/compiler', '')
                fs.write_text('main.cc', 'import std;\n')
                for name in ('std', 'std.compat'):
                    fs.write_text('/sdk/' + name + '.cppm', 'export module ' + name + ';\n')
                fs.write_text('/sdk/modules.json', json.dumps({'version': 1, 'modules': [
                    {'logical-name': name, 'source-path': name + '.cppm',
                     'local-arguments': {'system-include-directories': ['include']}}
                    for name in ('std', 'std.compat')]}))
                cfg = bt.BuildConfig(vfs=fs, USECLANG=clang, CXX='/sdk/compiler',
                                    CXXFLAGS=['-std=c++23'], INCFLAGS=[])
                with mock.patch.object(gcc_std, 'run_compiler', new=mock.AsyncMock(
                        return_value=subprocess.CompletedProcess([], 0, b'/sdk/modules.json', b''))) as probe, \
                     contextlib.redirect_stdout(io.StringIO()):
                    entries = json.loads(bt.make_compilation_database([bt.Path('main.cc')], cfg))
                    self.assertEqual(len(entries), 3)
                    query = probe.call_args.args[1]
                    self.assertIn('--print-library-module-manifest-path' if clang else
                                  '-print-file-name=libstdc++.modules.json', query)
                    cfg.reset_build_state()
                    bt.make_compilation_database([bt.Path('main.cc')], cfg)
                    probe.assert_awaited_once()
                for entry in entries[1:]:
                    self.assertIn('-isystem/sdk/include', entry['arguments'])
                    self.assertIn('-xc++-module' if clang else '-xc++', entry['arguments'])
                    self.assertIn('--precompile', entry['arguments'])
                    self.assertFalse(any(arg.startswith('-fmodule-file=') or
                                         arg.startswith('-fprebuilt-module-path=')
                                         for arg in entry['arguments']))

    def test_missing_manifest_does_not_break_ide_generation(self) -> None:
        """An older toolchain can still provide ordinary IDE commands."""
        fs = bt.MemoryFileSystem()
        fs.makedirs('/sdk')
        fs.write_text('/sdk/clang++', '')
        fs.write_text('main.cc', '')
        cfg = bt.BuildConfig(vfs=fs, CXX='/sdk/clang++', USECLANG=True)
        with mock.patch.object(gcc_std, 'run_compiler', new=mock.AsyncMock(
                return_value=subprocess.CompletedProcess([], 0, b'<NOT PRESENT>\n', b''))), \
             contextlib.redirect_stdout(io.StringIO()):
            entries = json.loads(bt.make_compilation_database([bt.Path('main.cc')], cfg))
        self.assertEqual(len(entries), 1)


@unittest.skipUnless(os.environ.get('BT_TEST_CLANG_STD_COMPILER'),
                     'set BT_TEST_CLANG_STD_COMPILER for real standard-module tests')
class RealClangStandardModuleTests(unittest.TestCase):
    def test_build_link_reuse_and_forced_rebuild(self) -> None:
        """Build both modules with Clang, verify execution, and reuse all artifacts."""
        with tempfile.TemporaryDirectory(prefix='buildtool-clang-std-') as directory:
            previous = os.getcwd()
            os.chdir(directory)
            try:
                Path('main.cc').write_text('import std.compat;\n'
                    'int helper();\nint main() { ::printf("%d\\n", helper()); }\n')
                Path('helper.cc').write_text('import std;\n'
                    'int helper() { return std::vector<int>{1, 2, 3}.size(); }\n')

                def build(rebuild: bool = False) -> str:
                    """Build from fresh caches with optional forced reconstruction."""
                    cfg = bt.BuildConfig(CXX=os.environ['BT_TEST_CLANG_STD_COMPILER'], USECLANG=True,
                        CLANG_WRAPPER=os.environ.get('BT_TEST_CLANG_STD_WRAPPER'),
                        CXXFLAGS=['-std=c++23', *shlex.split(os.environ.get('BT_TEST_CLANG_STD_FLAGS', ''))],
                        LDFLAGS=shlex.split(os.environ.get('BT_TEST_CLANG_STD_LDFLAGS', '')),
                        CFLAGS=[], INCFLAGS=[], JOBS=1, REBUILD=rebuild)
                    self.cfg = cfg
                    output = io.StringIO()
                    try:
                        with contextlib.redirect_stdout(output):
                            target = bt.Target(bt.Path('main'), cfg)
                            target.compile_many([bt.Path('main.cc'), bt.Path('helper.cc')])
                            target.link(publish=False)
                    except Exception as error:
                        raise AssertionError(output.getvalue()) from error
                    for source in cfg.std_module_sources.values():
                        self.assertTrue(Path(str(source.objpath)).is_file())
                        self.assertTrue(Path(str(source.cmpath)).is_file())
                    return output.getvalue()

                self.assertEqual(build().count('BUILDING module'), 2)
                self.assertEqual(subprocess.check_output(['build/release/bin/main'], text=True), '3\n')
                if clangd := os.environ.get('BT_TEST_CLANGD'):
                    with contextlib.redirect_stdout(io.StringIO()):
                        database = bt.make_compilation_database([bt.Path('main.cc'), bt.Path('helper.cc')], self.cfg)
                    Path('compile_commands.json').write_text(database)
                    result = subprocess.run([clangd, '--check=' + str(Path('main.cc').resolve()),
                        '--compile-commands-dir=' + directory, '--experimental-modules-support',
                        '--query-driver=' + self.cfg.CXX, '--log=error'], capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                with mock.patch('asyncio.create_subprocess_exec', side_effect=AssertionError('unexpected compiler')), \
                     mock.patch.object(bt, 'shell', side_effect=AssertionError('unexpected linker')):
                    self.assertEqual(build(), '')
                with Path('helper.cc').open('a') as source:
                    source.write('// edited importer\n')
                with mock.patch.object(gcc_std, 'run_compiler', side_effect=AssertionError('unexpected discovery')):
                    output = build()
                self.assertEqual(output.count('BUILDING c++'), 1)
                self.assertNotIn('BUILDING module', output)
                self.assertEqual(build(rebuild=True).count('BUILDING module'), 2)
            finally:
                os.chdir(previous)
