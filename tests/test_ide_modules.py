"""IDE commands must not expose normal compiler module caches to clangd."""

import contextlib
import io
import json
import unittest

import buildtool as bt


class IdeModuleTests(unittest.TestCase):
    def test_ide_omits_build_cache_without_changing_build_commands(self) -> None:
        """Omit automatic BMI search paths for both backends, retaining explicit paths."""
        for clang in (False, True):
            with self.subTest(clang=clang):
                fs = bt.MemoryFileSystem()
                fs.makedirs('lib/math')
                fs.write_text('lib/math/math.cc', 'export module lib.math;\nexport import :bits;\n')
                fs.write_text('lib/math/bits.cc', 'export module lib.math:bits;\n')
                fs.write_text('lib/math/helper.c', 'int helper(void);\n')
                cfg = bt.BuildConfig(vfs=fs, USECLANG=clang,
                                     OBJDIR='build/custom+clang' if clang else 'build/custom',
                                     CXXFLAGS=['-std=c++26', '-fprebuilt-module-path=/vendor/modules'])
                with contextlib.redirect_stdout(io.StringIO()):
                    database = json.loads(bt.make_compilation_database([bt.Path('lib')], cfg))
                self.assertEqual({row['file'] for row in database},
                                 {'lib/math/math.cc', 'lib/math/bits.cc', 'lib/math/helper.c'})
                automatic = f'-fprebuilt-module-path={cfg.OBJDIR}'
                for row in database:
                    self.assertNotIn(automatic, row['arguments'])
                    if row['file'].endswith('.cc'):
                        self.assertIn('-fprebuilt-module-path=/vendor/modules', row['arguments'])
                source = bt.SourceFile.get(bt.Path('lib/math/math.cc'), cfg)
                self.assertIn(automatic, source.compiler_cmd_clang(cfg))
