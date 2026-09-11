"""GCC's export replies use the same artifact paths as buildtool's imports."""

import asyncio
import unittest

import buildtool as bt


class GccMapperTests(unittest.IsolatedAsyncioTestCase):
    def test_header_path_aliases_reuse_source_and_artifact(self) -> None:
        """Redundant dot components in header names must not create another source."""
        for kind, name in ((bt.SourceType.USER_HEADER, './lib/errors/errors.h'),
                           (bt.SourceType.SYSTEM_HEADER, '/sdk/errors/errors.h')):
            with self.subTest(kind=kind):
                cfg = bt.BuildConfig(vfs=bt.MemoryFileSystem())
                path = bt.Path(name)
                source = bt.SourceFile.get(path, cfg, type=kind, modname=name)
                alias = name.replace('/errors/errors.h', '/errors/./errors.h')
                reused = bt.SourceFile.get(bt.Path(alias), cfg, type=kind, modname=alias)
                self.assertIs(reused, source)
                self.assertEqual(reused.cmpath, source.cmpath)
                with self.assertRaisesRegex(Exception, 'modname mismatch'):
                    bt.SourceFile.get(path, cfg, type=kind, modname=name + '.other')

    async def test_import_refines_directory_source_without_duplicate_job(self) -> None:
        """A module import reuses a .cc root already scheduled by directory discovery."""
        cfg = bt.BuildConfig(vfs=bt.MemoryFileSystem())
        target = bt.Target(bt.Path('main'), cfg)
        graph = bt.CompilationGraph(cfg)
        self.addAsyncCleanup(graph.session.close)
        target.schedule_sources([], graph)
        path = bt.Path('lib/math/math.cc')
        root = target.schedule_compilation_job(path)
        source = cfg.source_files[path]
        imported = target.schedule_compilation_job(
            path, type=bt.SourceType.MODULE, modname='lib.math')
        self.assertIs(root, imported)
        self.assertIs(source, cfg.source_files[path])
        self.assertEqual(source.type, bt.SourceType.MODULE)
        self.assertEqual(source.cmpath, cfg.OBJDIR / 'lib.math.pcm')
        self.assertIs(bt.SourceFile.get(path, cfg, type=bt.SourceType.CPP), source)

    def test_export_records_module_identity_for_later_import(self) -> None:
        """An export discovered by GCC supplies the module name and actual CMI path."""
        cfg = bt.BuildConfig(vfs=bt.MemoryFileSystem())
        path = bt.Path('lib/math/bits.cc')
        source = bt.SourceFile.get(path, cfg)
        target = bt.Target(bt.Path('main'), cfg)
        reply = asyncio.run(source.gcc_mapper_request(
            'MODULE-EXPORT', ['lib.math:bits'], target, cfg))
        self.assertEqual(source.type, bt.SourceType.MODULE)
        self.assertEqual(source.modname, 'lib.math:bits')
        self.assertEqual(reply, 'PATHNAME ' + str(source.cmpath.relative_to(cfg.OBJDIR)))
        self.assertIs(bt.SourceFile.get(path, cfg, type=bt.SourceType.MODULE,
                                       modname='lib.math:bits'), source)
        with self.assertRaisesRegex(Exception, 'modname mismatch'):
            bt.SourceFile.get(path, cfg, type=bt.SourceType.MODULE, modname='other')

    def test_exports_are_relative_to_the_build_directory(self) -> None:
        """Check named modules, relative headers, and absolute system headers."""
        fs = bt.MemoryFileSystem()
        cfg = bt.BuildConfig(vfs=fs, OBJDIR='build/release')
        for name, kind, source_path, expected in (
            ('math', bt.SourceType.MODULE, 'math.cc', 'math.pcm'),
            ('./local.h', bt.SourceType.USER_HEADER, 'local.h', 'local.h.pcm'),
            ('/sdk/api.hpp', bt.SourceType.SYSTEM_HEADER, '/sdk/api.hpp',
             'SYSTEM/sdk/api.hpp.pcm'),
        ):
            with self.subTest(name=name):
                source = bt.SourceFile.get(bt.Path(source_path), cfg, type=kind, modname=name)
                target = bt.Target(bt.Path('main'), cfg)
                reply = asyncio.run(source.gcc_mapper_request('MODULE-EXPORT', [name], target, cfg))
                self.assertEqual(reply, 'PATHNAME ' + expected)
                self.assertEqual(cfg.OBJDIR / expected, source.cmpath)
