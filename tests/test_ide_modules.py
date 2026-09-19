"""IDE commands must not expose normal compiler module caches to clangd."""

import contextlib
import io
import json
import unittest
from unittest import mock

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

    def test_generated_sources_are_added_to_compilation_database(self) -> None:
        """Materialized generated translation units use physical paths in the IDE database."""
        fs = bt.MemoryFileSystem()
        fs.makedirs('proto')
        fs.write_text(
            'proto/BUILD.py',
            '''GENERATED = [{
    "outputs": ["controller/client.cc", "controller/server.cc", "controller/msg.cc"],
    "command": ["/tools/gen", "{outdir}"],
}]\n''',
        )
        cfg = bt.BuildConfig(vfs=fs)
        bt.DirectoryConfig.get(bt.Path('proto'), cfg)

        for name in ('client.cc', 'server.cc', 'msg.cc'):
            path = bt.Path(f'build/release/generated/proto/controller/{name}')
            fs.makedirs(path.parent, exist_ok=True)
            fs.write_text(path, f'export module proto.controller.{name[:-3]};\n')

        with mock.patch.object(
            bt.CompilationDatabase, 'add_standard_modules', new=mock.AsyncMock()
        ):
            entries = json.loads(bt.make_compilation_database([bt.Path('proto')], cfg))

        by_file = {entry['file']: entry for entry in entries}
        expected = {
            'build/release/generated/proto/controller/client.cc',
            'build/release/generated/proto/controller/server.cc',
            'build/release/generated/proto/controller/msg.cc',
        }
        self.assertEqual(set(by_file), expected)
        for path in expected:
            self.assertIn(path, by_file[path]['arguments'])
            self.assertIn('-iquoteproto/controller', by_file[path]['arguments'])
