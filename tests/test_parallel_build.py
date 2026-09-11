"""Parallel graph integration, using a VFS and an asynchronous fake compiler."""

import asyncio
import contextlib
import io
import json
import unittest
from unittest import mock

import buildtool as bt


class ParallelBuildTests(unittest.TestCase):
    def setUp(self):
        """Create companion sources that import one common module."""
        self.fs = bt.MemoryFileSystem()
        self.cfg = bt.BuildConfig(vfs=self.fs, JOBS=2, OBJDIR='build',
                                 LDFLAGS=[], CXXFLAGS=[])
        self.calls = []
        self.barrier = None
        self.active = 0
        self.maximum_active = 0
        self.fail_source = None
        self.write('main.cc', {'headers': ['a/a.h', 'b/b.h']})
        for name in ('a', 'b'):
            self.write(f'{name}/{name}.h', {})
            self.write(f'{name}/{name}.cc', {'imports': ['value']})
            self.fs.write_text(f'{name}/BUILD.py', f'LDFLAGS = ["-l{name}"]')
        self.write('value.cc', {'value': 42})
        self.enterContext(mock.patch.object(bt.SourceFile, 'compile_gcc',
                                           autospec=True, side_effect=self.compile))

    def write(self, path, contents):
        """Write JSON contents at path in the test filesystem."""
        self.fs.makedirs(bt.Path(path).parent, exist_ok=True)
        self.fs.write_text(path, json.dumps(contents))

    async def compile(self, source, target, cfg):
        """Compile source JSON into cfg's VFS, resolving imports through target."""
        name = str(source.path)
        self.calls.append(name)
        contents = json.loads(self.fs.read_text(source.path))
        source.deps = {}
        async with source.job.compiler_slot():
            self.active += 1
            self.maximum_active = max(self.maximum_active, self.active)
            try:
                if name in ('a/a.cc', 'b/b.cc') and self.barrier is not None:
                    if self.active == 2:
                        self.barrier.set()
                    await asyncio.wait_for(self.barrier.wait(), 2)
                source.job.message(name + ': start')
                for imported in contents.get('imports', []):
                    module = bt.CompiledModule.get(imported, cfg)
                    self.active -= 1
                    digest = await module.build(target, source.dircfg(), source.job)
                    self.active += 1
                    source.deps[bt.ModuleDep(imported, digest)] = None
                if name == self.fail_source:
                    raise RuntimeError(name + ': deliberate failure')
                for path in contents.get('headers', []):
                    header = bt.HeaderDep.get(bt.Path(path), cfg)
                    source.deps[header] = None
                    source.header_deps[header] = None
                self.fs.write_text(source.objpath, str(contents))
                if source.type == bt.SourceType.MODULE:
                    self.fs.write_text(source.cmpath, str(contents.get('value', 0)))
                source.job.message(name + ': done')
            finally:
                self.active -= 1

    def build(self):
        """Run a fresh session over retained files and return target plus output."""
        self.cfg.reset_build_state()
        self.calls = []
        self.active = self.maximum_active = 0
        output = io.StringIO()
        target = bt.Target(bt.Path('main'), self.cfg)
        with contextlib.redirect_stdout(output):
            target.compile(bt.Path('main.cc'))
        return target, output.getvalue()

    def test_parallel_companions_share_module_and_keep_link_order(self):
        """Both companions must run together but build their common module once."""
        self.barrier = asyncio.Event()
        target, output = self.build()
        self.assertEqual(self.maximum_active, 2)
        self.assertEqual(self.calls.count('value.cc'), 1)
        self.assertEqual(target.get_linkflags(), ['-la', '-lb'])
        self.assertEqual(output,
                         'main.cc: start\nmain.cc: done\n'
                         'a/a.cc: start\na/a.cc: done\n'
                         'b/b.cc: start\nb/b.cc: done\n'
                         'value.cc: start\nvalue.cc: done\n')

    def test_incremental_parallel_build_rechecks_shared_module_hash(self):
        """A module edit rebuilds its importers, while a no-op rebuild is silent."""
        target, _ = self.build()
        original_objects = list(target.objs)
        target, output = self.build()
        self.assertEqual(self.calls, [])
        self.assertEqual(output, '')
        self.assertEqual(target.objs, original_objects)
        self.assertEqual(target.get_linkflags(), ['-la', '-lb'])
        self.write('value.cc', {'value': 43})
        target, _ = self.build()
        self.assertCountEqual(self.calls, ['value.cc', 'a/a.cc', 'b/b.cc'])
        self.assertEqual(target.objs, original_objects)
        self.assertEqual(self.build()[1], '')

    def test_single_job_limit_can_build_nested_modules(self):
        """One active compiler still permits nested on-demand module builds."""
        self.cfg.JOBS = 1
        self.write('value.cc', {'imports': ['leaf'], 'value': 42})
        self.write('leaf.cc', {'value': 7})
        self.build()
        self.assertEqual(self.maximum_active, 1)
        self.assertEqual(self.calls.count('leaf.cc'), 1)

    def test_directories_share_jobs_but_keep_separate_link_inputs(self) -> None:
        """Concurrent packages share transitive dependencies without mixing objects or flags."""
        self.barrier = asyncio.Event()
        self.write('value.cc', {'imports': ['leaf'], 'value': 42})
        self.write('leaf.cc', {'headers': ['common/common.h'], 'value': 7})
        self.write('common/common.h', {})
        self.write('common/common.cc', {})
        self.fs.write_text('common/BUILD.py', 'LDFLAGS = ["-lcommon"]')
        plans = [bt.BuildPlan(bt.Target(bt.Path(name), self.cfg), [bt.Path(f'{name}/{name}.cc')],
                              check_main=False, publish=False) for name in ('a', 'b')]
        linked = []

        async def link(target: bt.Target, job: bt.Job, artifact: bt.Path | None) -> bt.Path:
            """Verify target's full graph is ready before recording its simulated link."""
            self.assertTrue(self.cfg.source_files[bt.Path('common/common.cc')].processed)
            linked.append(target)
            job.message(f'LINKING {target.path}')
            return target.binary_paths(artifact)[0]

        with mock.patch.object(bt.Target, 'link_async', autospec=True, side_effect=link):
            for repeat in range(2):
                self.cfg.reset_build_state()
                self.calls = []
                for plan in plans:
                    plan.target = bt.Target(plan.target.path, self.cfg)
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    bt.build_plans(plans, self.cfg)
                if repeat == 0:
                    self.assertEqual(self.maximum_active, 2)
                    self.assertEqual(self.calls.count('value.cc'), 1)
                    self.assertEqual(self.calls.count('leaf.cc'), 1)
                    self.assertEqual(self.calls.count('common/common.cc'), 1)
                    self.assertLess(output.getvalue().index('common/common.cc: done'),
                                    output.getvalue().index('LINKING'))
                else:
                    self.assertEqual(self.calls, [])
                for name, plan in zip(('a', 'b'), plans):
                    self.assertEqual(list(map(str, plan.target.objs)),
                                     ['build/common/common.o', 'build/leaf.o', 'build/value.o',
                                      f'build/{name}/{name}.o'])
                    self.assertEqual(plan.target.get_linkflags(), [f'-l{name}', '-lcommon'])

    def test_ready_target_links_while_another_directory_compiles(self) -> None:
        """An unrelated slow compilation must not delay a ready target's linker."""
        self.write('fast/fast.cc', {})
        self.write('slow/slow.cc', {})
        slow_started = asyncio.Event()
        fast_linked = asyncio.Event()
        original_compile = self.compile

        async def compile(source: bt.SourceFile, target: bt.Target, cfg: bt.BuildConfig) -> None:
            """Keep slow's compiler active until fast's link has run in another slot."""
            if str(source.path) == 'slow/slow.cc':
                async with source.job.compiler_slot():
                    slow_started.set()
                    await asyncio.wait_for(fast_linked.wait(), 2)
            await original_compile(source, target, cfg)

        async def link(target: bt.Target, job: bt.Job, artifact: bt.Path | None) -> bt.Path:
            """Acquire the shared limit and release the slow compiler after fast links."""
            async with job.compiler_slot(compilation=False):
                if str(target.path) == 'fast':
                    await asyncio.wait_for(slow_started.wait(), 2)
                    fast_linked.set()
            return target.binary_paths(artifact)[0]

        plans = [bt.BuildPlan(bt.Target(bt.Path(name), self.cfg), [bt.Path(f'{name}/{name}.cc')],
                              check_main=False, publish=False) for name in ('fast', 'slow')]
        with mock.patch.object(bt.SourceFile.compile_gcc, 'side_effect', compile), \
             mock.patch.object(bt.Target, 'link_async', autospec=True, side_effect=link), \
             contextlib.redirect_stdout(io.StringIO()):
            bt.build_plans(plans, self.cfg)
        self.assertTrue(fast_linked.is_set())

    def test_all_directories_obey_one_job_limit(self) -> None:
        """Four packages must overlap while sharing two slots, including linker work."""
        two_running = asyncio.Event()
        plans = []
        for index in range(4):
            name = f'pkg{index}'
            self.write(f'{name}/code.cc', {})
            plans.append(bt.BuildPlan(bt.Target(bt.Path(name), self.cfg),
                                       [bt.Path(f'{name}/code.cc')], check_main=False, publish=False))

        async def work(job: bt.Job, *, compilation: bool) -> None:
            """Measure activity inside job's shared slot and let other jobs contend."""
            async with job.compiler_slot(compilation=compilation):
                self.active += 1
                self.maximum_active = max(self.maximum_active, self.active)
                if self.active == 2:
                    two_running.set()
                await asyncio.wait_for(two_running.wait(), 2)
                await asyncio.sleep(0)
                self.active -= 1

        async def compile(source: bt.SourceFile, target: bt.Target, cfg: bt.BuildConfig) -> None:
            """Simulate source compilation under the shared limit."""
            await work(source.job, compilation=True)
            self.fs.write_text(source.objpath, '')

        async def link(target: bt.Target, job: bt.Job, artifact: bt.Path | None) -> bt.Path:
            """Simulate linking with the same capacity counter as compiler jobs."""
            await work(job, compilation=False)
            return target.binary_paths(artifact)[0]

        with mock.patch.object(bt.SourceFile.compile_gcc, 'side_effect', compile), \
             mock.patch.object(bt.Target, 'link_async', autospec=True, side_effect=link), \
             contextlib.redirect_stdout(io.StringIO()):
            bt.build_plans(plans, self.cfg)
        self.assertEqual(self.maximum_active, 2)
        self.assertEqual(self.active, 0)

    def test_failed_test_package_does_not_link_or_block_other_packages(self) -> None:
        """Keep-going batches retain per-package failure while sharing successful dependencies."""
        self.fail_source = 'a/a.cc'
        plans = [bt.BuildPlan(bt.Target(bt.Path(name), self.cfg), [bt.Path(f'{name}/{name}.cc')],
                              check_main=False, publish=False) for name in ('a', 'b')]
        linked = []

        async def link(target: bt.Target, job: bt.Job, artifact: bt.Path | None) -> bt.Path:
            """Record only successfully built targets reaching the linker."""
            linked.append(str(target.path))
            return target.binary_paths(artifact)[0]

        with mock.patch.object(bt.Target, 'link_async', autospec=True, side_effect=link), \
             contextlib.redirect_stdout(io.StringIO()):
            bt.build_plans(plans, self.cfg, keep_going=True)
        self.assertIsInstance(plans[0].error, RuntimeError)
        self.assertIsNone(plans[0].binary)
        self.assertIsNone(plans[1].error)
        self.assertIsNotNone(plans[1].binary)
        self.assertEqual(linked, ['b'])
        self.assertEqual(self.calls.count('value.cc'), 1)

    def test_forced_rebuild_recompiles_shared_and_transitive_dependencies(self) -> None:
        """Force unchanged companions and nested modules once each, then return to no-op."""
        self.write('value.cc', {'imports': ['leaf'], 'value': 42})
        self.write('leaf.cc', {'value': 7})
        self.write('unrelated.cc', {})
        self.build()
        self.assertEqual(self.build()[1], '')
        self.cfg.REBUILD = True
        self.build()
        self.assertCountEqual(self.calls, ['main.cc', 'a/a.cc', 'b/b.cc', 'value.cc', 'leaf.cc'])
        self.cfg.REBUILD = False
        self.assertEqual(self.build()[1], '')

    def test_failure_does_not_publish_metadata_or_link(self):
        """A failed companion cannot publish success or update the public binary."""
        self.fail_source = 'a/a.cc'
        with mock.patch.object(bt.Target, 'link') as link:
            with self.assertRaisesRegex(RuntimeError, 'deliberate failure'):
                bt.build(bt.Path('main.cc'), self.cfg)
        link.assert_not_called()
        self.assertFalse(self.fs.is_file('build/a/a.info'))
