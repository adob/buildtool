"""GCC's export replies use the same artifact paths as buildtool's imports."""

import asyncio
import unittest

import buildtool as bt


class GccMapperTests(unittest.TestCase):
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
