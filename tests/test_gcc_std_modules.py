"""Persistent GCC standard-module discovery and real import/link regressions."""

import asyncio
import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import buildtool as bt
import gcc_std
from scheduler import BuildSession, Job


class ModuleDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        """Provide an SDK in memory and replace only the compiler metadata probe."""
        self.fs = bt.MemoryFileSystem()
        self.compiler = '/sdk/bin/g++'
        self.metadata = '/sdk/lib/libstdc++.modules.json'
        for path in (self.compiler, '/sdk/include/std.cc', '/sdk/include/std.compat.cc'):
            self.fs.makedirs(Path(path).parent, exist_ok=True)
            self.fs.write_text(path, 'initial contents')
        self.fs.makedirs('/sdk/lib')
        self.document = {'version': 1, 'modules': [
            {'logical-name': name, 'source-path': '../include/' + name + '.cc'}
            for name in ('std', 'std.compat')]}
        self.fs.write_text(self.metadata, json.dumps(self.document))
        self.probe = self.enterContext(mock.patch.object(gcc_std, 'run_compiler',
            new=mock.AsyncMock(return_value=subprocess.CompletedProcess(
                [], 0, (self.metadata + '\n').encode(), b''))))

    async def resolve(self, names: tuple[str, ...] = ('std',),
                      flags: tuple[str, ...] = ('-std=c++23',)) -> list[str]:
        """Resolve names with flags in a fresh cache, retaining the fake filesystem."""
        discovery = gcc_std.GccStdModules(self.fs)
        session = BuildSession(1, output=io.StringIO())
        results = []

        async def work(job: Job) -> None:
            """Resolve every module while lending the sole compiler slot."""
            async with job.compiler_slot():
                for name in names:
                    results.append(await discovery.resolve(
                        name, self.compiler, flags, '/cache', job))

        session.schedule('importer', work)
        await asyncio.wait_for(session.finish(), 3)
        return results

    async def test_shared_probe_and_persistent_reuse(self) -> None:
        """Both names share a probe; a fresh build reads metadata without GCC."""
        self.assertEqual(await self.resolve(('std', 'std.compat')),
                         ['/sdk/include/std.cc', '/sdk/include/std.compat.cc'])
        self.probe.assert_awaited_once()
        self.probe.reset_mock()
        await self.resolve(('std.compat', 'std'))
        self.probe.assert_not_awaited()

    async def test_metadata_edits_are_read_without_reprobing(self) -> None:
        """A cached metadata path must not hide a changed module source location."""
        await self.resolve()
        self.fs.write_text('/sdk/include/new.cc', '')
        self.document['modules'][0]['source-path'] = '../include/new.cc'
        self.fs.write_text(self.metadata, json.dumps(self.document))
        self.assertEqual(await self.resolve(), ['/sdk/include/new.cc'])
        self.probe.assert_awaited_once()

    async def test_compiler_and_flag_changes_invalidate_discovery(self) -> None:
        """Compiler replacement and target changes each require a fresh probe."""
        await self.resolve()
        self.fs.write_text(self.compiler, 'replacement compiler')
        await self.resolve()
        await self.resolve(flags=('-std=c++23', '-m32'))
        self.assertEqual(self.probe.await_count, 3)

    async def test_failed_probe_can_be_retried(self) -> None:
        """Compiler failure publishes no successful discovery record."""
        self.probe.return_value = subprocess.CompletedProcess([], 1, b'', b'bad target\n')
        with self.assertRaises(subprocess.CalledProcessError):
            await self.resolve()
        self.probe.return_value = subprocess.CompletedProcess([], 0, self.metadata.encode(), b'')
        self.assertEqual(await self.resolve(), ['/sdk/include/std.cc'])
        self.assertEqual(self.probe.await_count, 2)

    async def test_missing_or_invalid_metadata_has_actionable_error(self) -> None:
        """Unsupported compilers and broken installations report the actual problem."""
        self.probe.return_value = subprocess.CompletedProcess([], 0, b'libstdc++.modules.json\n', b'')
        with self.assertRaisesRegex(RuntimeError, r'does not provide libstdc\+\+\.modules.json'):
            await self.resolve()
        self.probe.return_value = subprocess.CompletedProcess([], 0, self.metadata.encode(), b'')
        self.fs.write_text(self.metadata, '{}')
        with self.assertRaisesRegex(RuntimeError, 'Invalid GCC module metadata'):
            await self.resolve()
        self.fs.write_text(self.metadata, json.dumps(self.document))
        self.fs.unlink('/sdk/include/std.cc')
        with self.assertRaisesRegex(RuntimeError, 'module source does not exist'):
            await self.resolve()


class StandardModuleBuildTests(unittest.TestCase):
    def setUp(self) -> None:
        """Use fake compiler outputs with the real dependency graph and discovery cache."""
        self.fs = bt.MemoryFileSystem()
        self.cfg = bt.BuildConfig(vfs=self.fs, CXX='/sdk/g++', CXXFLAGS=['-std=c++23'],
            CFLAGS=[], INCFLAGS=[], LDFLAGS=[], STD_HEADER_UNIT=False, JOBS=2)
        self.calls = []
        self.fail_module = False
        for path in ('/sdk/g++', '/sdk/std.cc', '/sdk/std.compat.cc', '/sdk/header.h',
                     'a/main.cc', 'b/main.cc'):
            self.fs.makedirs(Path(path).parent, exist_ok=True)
            self.fs.write_text(path, 'initial')
        self.fs.write_text('/sdk/modules.json', json.dumps({'version': 1, 'modules': [
            {'logical-name': name, 'source-path': name + '.cc'} for name in ('std', 'std.compat')]}))
        self.probe = self.enterContext(mock.patch.object(gcc_std, 'run_compiler',
            new=mock.AsyncMock(return_value=subprocess.CompletedProcess([], 0, b'/sdk/modules.json', b''))))
        self.enterContext(mock.patch.object(bt.SourceFile, 'compile_gcc', autospec=True, side_effect=self.compile))

    async def compile(self, source: bt.SourceFile, target: bt.Target, cfg: bt.BuildConfig) -> None:
        """Serve actual mapper requests for source, then write fake CMI/object outputs."""
        self.calls.append(str(source.path))
        source.deps = {}
        async with source.job.compiler_slot():
            contents = self.fs.read_text(source.path) + str(source.compiler_cmd(cfg))
            if source.modname == 'std':
                if self.fail_module:
                    raise RuntimeError('std compilation failed')
                await source.gcc_mapper_request('INCLUDE-TRANSLATE', ['/sdk/header.h'], target, cfg)
                contents += self.fs.read_text('/sdk/header.h')
            else:
                imported = 'std.compat' if str(source.path) == 'a/main.cc' else 'std'
                reply = await source.gcc_mapper_request('MODULE-IMPORT', [imported], target, cfg)
                contents += self.fs.read_text(cfg.OBJDIR / reply.removeprefix('PATHNAME '))
            if source.std_module_variant:
                reply = await source.gcc_mapper_request('MODULE-EXPORT', [source.modname], target, cfg)
                self.assertEqual(reply, 'PATHNAME ' + str(source.cmpath.relative_to(cfg.OBJDIR)))
                self.fs.write_text(source.cmpath, contents)
            self.fs.write_text(source.objpath, contents)

    def build(self) -> bt.Target:
        """Build both importers with fresh memory caches and preserved disk state."""
        self.cfg.reset_build_state()
        self.calls.clear()
        target = bt.Target(bt.Path('main'), self.cfg)
        with contextlib.redirect_stdout(io.StringIO()):
            target.compile_many([bt.Path('a/main.cc'), bt.Path('b/main.cc')])
        return target

    def test_reuse_link_inputs_and_dependency_changes(self) -> None:
        """Reuse CMIs/objects, retain them in link inputs, propagate SDK edits."""
        for name in ('a', 'b'):
            self.fs.write_text(name + '/BUILD.py', f'CFLAGS = ["-DPACKAGE_{name}=1"]\n')
        target = self.build()
        self.assertEqual(len(self.calls), 4)
        units = list(self.cfg.std_module_sources.values())
        self.assertEqual(len(units), 2)
        for unit in units:
            self.assertIn(unit.objpath, target.objs)
        self.build()
        self.assertEqual(self.calls, [])
        self.probe.assert_awaited_once()
        self.fs.write_text('/sdk/header.h', 'new standard declaration')
        self.build()
        self.assertCountEqual(self.calls, ['/sdk/std.cc', '/sdk/std.compat.cc', 'a/main.cc', 'b/main.cc'])
        self.fs.write_text('/sdk/std.cc', 'changed module exports')
        self.build()
        self.assertEqual(len(self.calls), 4)

    def test_compiler_and_abi_changes_select_new_artifacts(self) -> None:
        """A replaced compiler or changed ABI cannot reuse the old named-module CMI."""
        self.build()
        old = {source.cmpath for source in self.cfg.std_module_sources.values()}
        self.fs.write_text('/sdk/g++', 'new compiler executable')
        self.build()
        new = {source.cmpath for source in self.cfg.std_module_sources.values()}
        self.assertTrue(old.isdisjoint(new))
        self.assertEqual(len(self.calls), 4)
        self.fs.write_text('b/BUILD.py', 'CFLAGS = ["-D_GLIBCXX_USE_CXX11_ABI=0"]\n')
        self.build()
        self.assertCountEqual(self.calls, ['b/main.cc', '/sdk/std.cc'])
        self.assertEqual(len(self.cfg.std_module_sources), 3)
        self.build()
        self.assertEqual(self.calls, [])

    def test_failed_module_does_not_publish_success(self) -> None:
        """Compiler failure is retried without leaving successful module metadata."""
        self.fail_module = True
        with self.assertRaisesRegex(RuntimeError, 'std compilation failed'):
            self.build()
        for source in self.cfg.std_module_sources.values():
            self.assertFalse(self.fs.is_file(source.infofile))
        self.fail_module = False
        self.build()
        self.assertEqual(len(self.calls), 4)


@unittest.skipUnless(os.environ.get('BT_TEST_GCC'), 'set BT_TEST_GCC for GCC tests')
class RealStandardModuleTests(unittest.TestCase):
    def test_import_link_reuse_and_rebuild(self) -> None:
        """Compile/link std.compat and std, reuse across processes, rebuild on request."""
        compiler = os.environ['BT_TEST_GCC']
        if int(subprocess.check_output([compiler, '-dumpversion'], text=True).split('.')[0]) < 16:
            self.skipTest('standard-module metadata requires GCC 16')
        with tempfile.TemporaryDirectory(prefix='buildtool-std-module-') as directory:
            previous = os.getcwd()
            os.chdir(directory)
            try:
                Path('main.cc').write_text('import std.compat;\n'
                    'int helper();\nint main() { ::printf("%d\\n", helper()); }\n')
                Path('helper').mkdir()
                Path('helper/BUILD.py').write_text('CFLAGS = ["-DPROJECT=1", "-Wextra"]\n')
                Path('helper/helper.cc').write_text('import std;\n'
                    'int helper() { return std::vector<int>{1, 2, 3}.size(); }\n')

                def build(rebuild: bool = False, header_unit: bool = True) -> tuple[bt.BuildConfig, str]:
                    """Build and link from a fresh config with requested rebuild/header policy."""
                    cfg = bt.BuildConfig(CXX=compiler, CXXFLAGS=['-std=c++23'],
                        CFLAGS=[], INCFLAGS=[], LDFLAGS=[], JOBS=1, REBUILD=rebuild,
                        STD_HEADER_UNIT=header_unit)
                    output = io.StringIO()
                    with contextlib.redirect_stdout(output):
                        target = bt.Target(bt.Path('main'), cfg)
                        target.compile_many([bt.Path('main.cc'), bt.Path('helper/helper.cc')])
                        target.link(publish=False)
                    return cfg, output.getvalue()

                cfg, output = build()
                self.assertEqual(output.count('BUILT module'), 2)
                self.assertEqual(output.count('BUILT system header'), 1)
                self.assertEqual(len(cfg.std_module_sources), 2)
                for source in cfg.std_module_sources.values():
                    self.assertTrue(Path(str(source.objpath)).is_file())
                    self.assertTrue(Path(str(source.cmpath)).is_file())
                self.assertEqual(subprocess.check_output(['build/release/bin/main'], text=True), '3\n')

                # No compiler, metadata probe, or linker may run on a no-op build.
                with mock.patch('asyncio.create_subprocess_exec', side_effect=AssertionError('unexpected compiler')), \
                     mock.patch.object(bt, 'shell', side_effect=AssertionError('unexpected link')):
                    self.assertEqual(build()[1], '')

                with Path('helper/helper.cc').open('a') as source:
                    source.write('// edit only the importer\n')
                with mock.patch.object(gcc_std, 'run_compiler', side_effect=AssertionError('unexpected discovery')):
                    output = build()[1]
                self.assertEqual(output.count('BUILT c++'), 1)
                self.assertNotIn('BUILT module', output)
                self.assertNotIn('BUILT system header', output)
                output = build(rebuild=True)[1]
                self.assertEqual(output.count('BUILT module'), 2)
                self.assertEqual(output.count('BUILT system header'), 1)
                output = build(header_unit=False)[1]
                self.assertEqual(output.count('BUILT module'), 2)
                self.assertNotIn('BUILT system header', output)
                self.assertEqual(subprocess.check_output(['build/release/bin/main'], text=True), '3\n')
                self.assertEqual(build(header_unit=False)[1], '')
            finally:
                os.chdir(previous)
