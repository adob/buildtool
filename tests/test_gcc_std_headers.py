"""On-demand GCC standard header discovery and incremental module scheduling."""

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


HEADER = '/sdk/include/bits/stdc++.h'
ALGORITHM = '/sdk/include/algorithm'
SEARCH = b'#include <...> search starts here:\n /sdk/include\nEnd of search list.\n'


class HeaderConfigurationTests(unittest.TestCase):
    def test_project_flags_are_local_but_sdk_settings_survive(self) -> None:
        """Keep global settings and directory ABI macros in their original order."""
        flags = gcc_std.header_unit_flags(['-std=c++23', '-DGLOBAL=1'], [
            '-I', '/pkg', '-isystem/pkg2', '-Wextra', '-Wno-error',
            '-DPROJECT=1', '-U', 'PROJECT', '-D', '_GLIBCXX_DEBUG',
            '-U_GLIBCXX_USE_CXX11_ABI', '-DNDEBUG', '-m32', '-fno-exceptions',
            '-std=c++26'])
        self.assertEqual(flags, ('-std=c++23', '-DGLOBAL=1', '-D_GLIBCXX_DEBUG',
            '-U_GLIBCXX_USE_CXX11_ABI', '-DNDEBUG', '-m32', '-fno-exceptions',
            '-std=c++26'))

    def test_forced_preprocessing_preserves_directory_configuration(self) -> None:
        """Opaque preprocessing inputs retain paths and macros needed to interpret them."""
        for special in (['-include', 'config.h'], ['-imacrosconfig.h'],
                        ['-Wp,-DABI=1'], ['-Xpreprocessor', '-DABI=1']):
            with self.subTest(special=special):
                directory = ['-I/pkg', '-DPROJECT=1', *special]
                self.assertEqual(gcc_std.header_unit_flags(['-std=c++23'], directory),
                                 ('-std=c++23', *directory))


class DiscoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        """Provide a fake SDK and capture compiler include-search probes."""
        self.fs = bt.MemoryFileSystem()
        self.fs.makedirs('/sdk/include/bits')
        self.fs.write_text(HEADER, '')
        self.discovery = gcc_std.GccStdHeaders(self.fs)
        self.calls = []

        async def probe(job: Job, command: tuple[str, ...], **kwargs: object) -> subprocess.CompletedProcess:
            """Emulate a compiler probe using job's slot and record its command."""
            async with job.compiler_slot():
                self.calls.append(command)
                return subprocess.CompletedProcess(command, 0, b'', SEARCH)

        self.probe = self.enterContext(mock.patch.object(gcc_std, 'run_compiler', side_effect=probe))

    async def matches(self, paths: list[str], flags: list[str] | None = None) -> list[bool]:
        """Query paths with flags from a parent occupying the sole compiler slot."""
        results = []
        session = BuildSession(1, output=io.StringIO())

        async def work(job: Job) -> None:
            """Probe each requested path while lending job's slot to discovery."""
            async with job.compiler_slot():
                for path in paths:
                    results.append(await self.discovery.matches(path, 'g++', flags or [], job))

        session.schedule('parent', work)
        await asyncio.wait_for(session.finish(), 3)
        return results

    async def test_identity_symlinks_and_project_shadow(self) -> None:
        """Only the SDK header or a symlink to it qualifies; probe once."""
        self.fs.symlink('/sdk/include', '/alias')
        self.fs.makedirs('/project/bits')
        self.fs.write_text('/project/bits/stdc++.h', '')
        self.assertEqual(await self.matches(
            [ALGORITHM, HEADER, '/alias/bits/stdc++.h', '/project/bits/stdc++.h']),
            [False, True, True, False])
        self.assertEqual(len(self.calls), 1)

    async def test_ordinary_header_does_not_launch_probe(self) -> None:
        """Projects without an aggregate-header request pay no discovery cost."""
        self.assertEqual(await self.matches([ALGORITHM, './local.h']), [False, False])
        self.probe.assert_not_called()

    async def test_no_installed_aggregate_is_cached(self) -> None:
        """An SDK lacking the aggregate remains textual without repeated probes."""
        self.fs.unlink(HEADER)
        self.assertEqual(await self.matches([HEADER, HEADER]), [False, False])
        self.assertEqual(len(self.calls), 1)

    async def test_probe_preserves_target_but_excludes_project_paths(self) -> None:
        """Project include roots cannot impersonate the standard-library header."""
        flags = ['--sysroot=/target', '-m32', '-nostdinc++', '-std=c++23',
                 '-I/project', '-isystem', '/another', '-iquote/local',
                 '-idirafter/late', '-include', 'forced.h', '-fmodules-ts']
        with mock.patch.dict(os.environ, {'CPATH': '/project', 'CPLUS_INCLUDE_PATH': '/another'}):
            await self.matches([HEADER], flags)
        self.assertEqual(self.calls[0], ('g++', '--sysroot=/target', '-m32',
            '-nostdinc++', '-std=c++23', '-E', '-v', '-xc++', '-'))
        self.assertNotIn('CPATH', self.probe.call_args.kwargs['env'])
        self.assertNotIn('CPLUS_INCLUDE_PATH', self.probe.call_args.kwargs['env'])

    async def test_failed_probe_is_not_cached_as_missing(self) -> None:
        """Report compiler failure and permit discovery to succeed next session."""
        self.probe.return_value = subprocess.CompletedProcess(['g++'], 1, b'', b'bad target\n')
        self.probe.side_effect = None
        with self.assertRaises(subprocess.CalledProcessError):
            await self.matches([HEADER])
        self.assertEqual(self.discovery.paths, {})


class StandardHeaderBuildTests(unittest.TestCase):
    def setUp(self) -> None:
        """Create two importers and an SDK, replacing only compiler execution."""
        # These scheduling tests use simulated compilers without executable files.
        self.enterContext(mock.patch.object(bt.BuildConfig, "compiler_identity",
                                            return_value=["/fake/compiler", 1]))
        self.fs = bt.MemoryFileSystem()
        self.cfg = bt.BuildConfig(vfs=self.fs, CXXFLAGS=['-std=c++23'],
                                  CFLAGS=[], INCFLAGS=[], LDFLAGS=[], JOBS=2)
        self.calls = []
        self.fail_header = False
        for file, data in ((HEADER, ''), (ALGORITHM, 'version 1'),
                           ('a/main.cc', ''), ('b/main.cc', '')):
            self.fs.makedirs(bt.Path(file).parent, exist_ok=True)
            self.fs.write_text(file, data)
        self.enterContext(mock.patch.object(gcc_std, 'run_compiler',
            new=mock.AsyncMock(return_value=subprocess.CompletedProcess([], 0, b'', SEARCH))))
        self.enterContext(mock.patch.object(bt.SourceFile, 'compile_gcc',
                                            autospec=True, side_effect=self.compile))

    async def compile(self, source: bt.SourceFile, target: bt.Target, cfg: bt.BuildConfig) -> None:
        """Resolve real mapper requests, then write fake compiler outputs to cfg."""
        self.calls.append(str(source.path))
        source.deps = {}
        async with source.job.compiler_slot():
            if source.std_header_variant:
                if self.fail_header:
                    raise RuntimeError('standard header compilation failed')
                for path in (ALGORITHM, HEADER):
                    reply = await source.gcc_mapper_request('INCLUDE-TRANSLATE', [path], target, cfg)
                    self.assertEqual(reply, 'BOOL TRUE')
                reply = await source.gcc_mapper_request('MODULE-EXPORT', [HEADER], target, cfg)
                self.assertEqual(reply, 'PATHNAME ' + str(source.cmpath.relative_to(cfg.OBJDIR)))
                contents = self.fs.read_text(ALGORITHM) + str(source.compiler_cmd(cfg))
                self.fs.makedirs(source.cmpath.parent, exist_ok=True)
                self.fs.write_text(source.cmpath, contents)
            else:
                reply = await source.gcc_mapper_request('INCLUDE-TRANSLATE', [ALGORITHM], target, cfg)
                if not cfg.STD_HEADER_UNIT:
                    self.assertEqual(reply, 'BOOL TRUE')
                    self.fs.write_text(source.objpath, self.fs.read_text(ALGORITHM))
                    return
                self.assertEqual(reply, 'BOOL FALSE')
                reply = await source.gcc_mapper_request('INCLUDE-TRANSLATE', [HEADER], target, cfg)
                self.assertTrue(reply.startswith('PATHNAME '))
                self.fs.write_text(source.objpath, self.fs.read_text(cfg.OBJDIR / reply[9:]))

    def build(self) -> str:
        """Build both roots with fresh caches but retained filesystem artifacts."""
        self.cfg.reset_build_state()
        self.calls.clear()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            bt.Target(bt.Path('main'), self.cfg).compile_many(
                [bt.Path('a/main.cc'), bt.Path('b/main.cc')])
        return output.getvalue()

    def test_shared_unit_noop_and_changed_system_header(self) -> None:
        """Share one unit, stay silent on no-op, rebuild importers on SDK edits."""
        self.build()
        self.assertEqual(self.calls.count(HEADER), 1)
        self.assertEqual(self.build(), '')
        self.assertEqual(self.calls, [])
        self.fs.write_text(ALGORITHM, 'version 2')
        self.build()
        self.assertCountEqual(self.calls, [HEADER, 'a/main.cc', 'b/main.cc'])
        self.assertEqual(self.build(), '')

    def test_one_slot_and_forced_rebuild(self) -> None:
        """Nested compilation makes progress at -j1 and --rebuild shares work."""
        self.cfg.JOBS = 1
        self.build()
        self.cfg.REBUILD = True
        self.build()
        self.assertCountEqual(self.calls, [HEADER, 'a/main.cc', 'b/main.cc'])

    def test_disabling_and_reenabling_invalidates_importers(self) -> None:
        """Changing mapper policy rebuilds C++ sources before loading old deps."""
        self.build()
        self.cfg.STD_HEADER_UNIT = False
        with mock.patch.object(self.cfg.gcc_std_headers, 'matches') as matches:
            self.build()
            matches.assert_not_called()
        self.assertCountEqual(self.calls, ['a/main.cc', 'b/main.cc'])
        info = json.loads(self.fs.read_text('build/release/a/main.info'))
        self.assertEqual(info['deps'], [])
        self.assertFalse(info['std_header_unit'])
        self.assertEqual(self.build(), '')
        self.assertEqual(self.calls, [])
        self.cfg.STD_HEADER_UNIT = True
        self.build()
        self.assertCountEqual(self.calls, ['a/main.cc', 'b/main.cc'])
        self.assertEqual(self.build(), '')

    def test_directory_flags_use_distinct_cached_units(self) -> None:
        """Different standard-library macro configurations cannot share a CMI."""
        self.fs.write_text('a/BUILD.py', 'CFLAGS = ["-D_GLIBCXX_USE_CXX11_ABI=0"]\n')
        self.fs.write_text('b/BUILD.py', 'CFLAGS = ["-D_GLIBCXX_USE_CXX11_ABI=1"]\n')
        self.build()
        self.assertEqual(self.calls.count(HEADER), 2)
        units = list(self.cfg.std_header_sources.values())
        self.assertNotEqual(units[0].cmpath, units[1].cmpath)
        self.assertNotEqual(units[0].infofile, units[1].infofile)
        self.assertNotEqual(units[0].cmhash, units[1].cmhash)
        self.assertEqual(self.build(), '')

    def test_package_flags_share_unit_and_remain_on_importers(self) -> None:
        """Package paths, warnings, and project macros must not multiply SDK units."""
        self.fs.write_text('a/BUILD.py', 'CFLAGS = ["-I/pkg-a", "-DPROJECT_A=1", "-Wextra"]\n')
        self.fs.write_text('b/BUILD.py', 'CFLAGS = ["-I/pkg-b", "-DPROJECT_B=1"]\n')
        self.build()
        self.assertEqual(self.calls.count(HEADER), 1)
        unit = next(iter(self.cfg.std_header_sources.values()))
        command = unit.compiler_cmd(self.cfg)
        self.assertFalse(any('/pkg-' in flag or 'PROJECT_' in flag or flag == '-Wextra'
                             for flag in command))
        for name in ('a', 'b'):
            source = self.cfg.source_files[bt.Path(name + '/main.cc')]
            self.assertIn('-DPROJECT_' + name.upper() + '=1', source.compiler_cmd(self.cfg))
        self.assertEqual(self.build(), '')
        self.fs.write_text('a/BUILD.py', 'CFLAGS = ["-I/pkg-new", "-DPROJECT_A=2"]\n')
        self.build()
        self.assertEqual(self.calls, ['a/main.cc'])
        self.assertEqual(next(iter(self.cfg.std_header_sources.values())).cmpath, unit.cmpath)
        self.assertEqual(self.build(), '')

    def test_failed_header_does_not_publish_success(self) -> None:
        """A failed dependency must fail its importers and be retried next build."""
        self.fail_header = True
        with self.assertRaisesRegex(RuntimeError, 'standard header compilation failed'):
            self.build()
        self.assertFalse(self.fs.is_file('build/release/a/main.info'))
        self.assertFalse(self.fs.is_file('build/release/b/main.info'))
        for source in self.cfg.std_header_sources.values():
            self.assertFalse(self.fs.is_file(source.infofile))
        self.fail_header = False
        self.build()
        self.assertEqual(self.calls.count(HEADER), 1)

    def test_compiler_flags_select_a_new_artifact(self) -> None:
        """Changing the configuration compiler flags must preserve the old unit."""
        self.build()
        old = next(iter(self.cfg.std_header_sources.values())).cmpath
        self.cfg.CXXFLAGS = ['-std=c++26']
        self.build()
        new = next(iter(self.cfg.std_header_sources.values())).cmpath
        self.assertNotEqual(old, new)
        self.assertTrue(self.fs.is_file(old))
        self.assertTrue(self.fs.is_file(new))
        self.assertEqual(self.calls.count(HEADER), 1)


@unittest.skipUnless(os.environ.get('BT_TEST_GCC'), 'set BT_TEST_GCC for GCC tests')
class RealStandardHeaderTests(unittest.TestCase):
    def test_directory_sharing_and_abi_variants(self) -> None:
        """Real GCC shares package configurations but preserves string ABI differences."""
        compiler = os.environ['BT_TEST_GCC']
        major = int(subprocess.check_output([compiler, '-dumpversion'], text=True).split('.')[0])
        if major < 16:
            self.skipTest('automatic aggregate-header translation requires GCC 16')
        with tempfile.TemporaryDirectory(prefix='buildtool-std-config-') as directory:
            previous = os.getcwd()
            os.chdir(directory)
            try:
                for name in ('a', 'b'):
                    Path(name).mkdir()
                    Path(name, 'BUILD.py').write_text(
                        f'CFLAGS = ["-DPROJECT_{name}=1", "-I/{name}", "-Wextra"]\n')
                    Path(name, 'main.cc').write_text(
                        '#include <string>\n'
                        f'#ifndef PROJECT_{name}\n#error missing project macro\n#endif\n'
                        'static_assert(sizeof(std::string) == '
                        '(_GLIBCXX_USE_CXX11_ABI ? 32 : 8));\n')
                cfg = bt.BuildConfig(CXX=compiler, CXXFLAGS=['-std=c++23'],
                                     CFLAGS=[], INCFLAGS=[], LDFLAGS=[], JOBS=2)

                def build() -> str:
                    """Compile both directory configurations with fresh in-memory caches."""
                    cfg.reset_build_state()
                    output = io.StringIO()
                    with contextlib.redirect_stdout(output):
                        bt.Target(bt.Path('main'), cfg).compile_many(
                            [bt.Path('a/main.cc'), bt.Path('b/main.cc')])
                    return output.getvalue()

                self.assertEqual(build().count('BUILDING system header'), 1)
                self.assertEqual(build(), '')
                with Path('b/BUILD.py').open('a') as config:
                    config.write('CFLAGS += ["-D_GLIBCXX_USE_CXX11_ABI=0"]\n')
                self.assertEqual(build().count('BUILDING system header'), 1)
                self.assertEqual(len(cfg.std_header_sources), 2)
                self.assertEqual(build(), '')
            finally:
                os.chdir(previous)

    def test_on_demand_translation_and_noop(self) -> None:
        """GCC 16 must build one unit for two importers at -j1 and reuse it."""
        compiler = os.environ['BT_TEST_GCC']
        major = int(subprocess.check_output([compiler, '-dumpversion'], text=True).split('.')[0])
        if major < 16:
            self.skipTest('automatic aggregate-header translation requires GCC 16')
        with tempfile.TemporaryDirectory(prefix='buildtool-std-header-') as directory:
            previous = os.getcwd()
            os.chdir(directory)
            try:
                for name in ('a', 'b'):
                    include = 'import "wrapper.h";' if name == 'a' else '#include <algorithm>'
                    Path(name + '.cc').write_text(include + '\nint ' + name + '() { return std::max(1, 2); }\n')
                Path('wrapper.h').write_text('#include <algorithm>\n')
                cfg = bt.BuildConfig(CXX=compiler, CXXFLAGS=['-std=c++23'],
                                     CFLAGS=[], INCFLAGS=[], LDFLAGS=[], JOBS=1)
                def build() -> str:
                    """Build both sources, collecting diagnostics for assertions."""
                    cfg.reset_build_state()
                    output = io.StringIO()
                    with contextlib.redirect_stdout(output):
                        bt.Target(bt.Path('main'), cfg).compile_many([bt.Path('a.cc'), bt.Path('b.cc')])
                    return output.getvalue()
                output = build()
                self.assertEqual(output.count('BUILDING system header'), 1)
                for path in ('build/release/b.info', 'build/release/wrapper.info'):
                    info = json.loads(Path(path).read_text())
                    self.assertTrue(any(key.startswith('module:') and 'bits/stdc++.h@' in key for key in info['deps']))
                self.assertEqual(build(), '')
                cfg.JOBS = 2
                cfg.REBUILD = True
                self.assertEqual(build().count('BUILDING system header'), 1)
                cfg.REBUILD = False
                cfg.STD_HEADER_UNIT = False
                output = build()
                self.assertIn('BUILDING c++', output)
                self.assertNotIn('BUILDING system header', output)
                self.assertEqual(build(), '')
                cfg.STD_HEADER_UNIT = True
                self.assertIn('BUILDING c++', build())
                self.assertEqual(build(), '')
            finally:
                os.chdir(previous)
