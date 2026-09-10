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
