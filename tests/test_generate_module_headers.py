"""Recursive module-header generation uses project flags and a sibling output tree."""

import contextlib
import io
import os
import subprocess
import sys
import unittest
from unittest import mock

import buildtool as bt


class GenerateModuleHeadersTests(unittest.TestCase):
    def setUp(self) -> None:
        """Create a virtual library and a fake extractor subprocess boundary."""
        self.fs = bt.MemoryFileSystem()
        self.fs.makedirs('deps/base/lib/math')
        self.fs.makedirs('tools')
        self.fs.write_text('tools/extractor', '')
        self.cfg = bt.BuildConfig(vfs=self.fs, USECLANG=True, CXX='test-clang++',
                                 CXXFLAGS=['-std=c++26', '-DPROJECT'], INCFLAGS=['-Ideps/base'])
        self.enterContext(mock.patch.dict(os.environ, {'BT_MODULE_HEADER': 'tools/extractor'}))
        self.commands = []
        self.enterContext(mock.patch.object(bt, 'shell', side_effect=self.command))

    def command(self, *args: str | os.PathLike[str], verbose: bool = False) -> str:
        """Record args and simulate resource discovery or an extractor writing its output."""
        command = list(map(str, args))
        self.commands.append((command, verbose))
        if command[1] == '-print-resource-dir':
            return '/toolchain/lib/clang/23\n'
        output = command[command.index('-o') + 1]
        self.fs.write_text(output, '// extracted\n')
        return ''

    def generate(self, path: str = 'deps/base/lib') -> list[bt.Path]:
        """Run generation for path while capturing progress output."""
        with contextlib.redirect_stdout(io.StringIO()):
            return bt.generate_module_headers(bt.Path(path), self.cfg)

    def test_recursion_output_layout_and_directory_flags(self) -> None:
        """Only .cc interfaces are extracted, in stable order, using source-local flags."""
        self.fs.write_text('deps/base/lib/z.cc', 'export module z;')
        self.fs.write_text('deps/base/lib/math/math.cc', 'export module lib.math;')
        self.fs.write_text('deps/base/lib/math/BUILD.py', "CFLAGS = ['-DLOCAL', '-Iextra']\n")
        self.fs.write_text('deps/base/lib/math/math.h', 'existing header')
        self.fs.write_text('deps/base/lib/ordinary.cc', 'int ordinary();')
        self.fs.write_text('deps/base/lib/ignored.cpp', 'export module ignored;')
        outputs = self.generate()
        self.assertEqual(list(map(str, outputs)), [
            '/workspace/deps/base/generated-headers/math/math.h',
            '/workspace/deps/base/generated-headers/z.h'])
        self.assertEqual(self.commands[0][0], ['test-clang++', '-print-resource-dir'])
        flags = self.commands[1][0]
        for flag in ('-DLOCAL', '-idirafterextra', '-DPROJECT', '-Ideps/base',
                     '-std=c++26', '-resource-dir=/toolchain/lib/clang/23'):
            self.assertIn(flag, flags)
        self.assertNotIn('-DLOCAL', self.commands[2][0])
        self.assertEqual(self.fs.read_text('deps/base/lib/math/math.h'), 'existing header')

    def test_comments_strings_and_implementation_units_are_skipped(self) -> None:
        """The heuristic ignores textual examples and non-interface module declarations."""
        self.fs.write_text('deps/base/lib/not-module.cc', '''// export module comment;
/* export module block; */
const char *s = "export module string;";
const char *raw = R"tag(" export module raw; ")tag";
#define DECL export module macro;
module lib.math;
''')
        self.assertEqual(self.generate(), [])
        self.assertEqual(self.commands, [])
        self.assertFalse(self.fs.is_dir('deps/base/generated-headers'))

    def test_partition_interface_and_comments_between_tokens(self) -> None:
        """Interface partitions also qualify for extraction by the Clang tool."""
        self.fs.write_text('deps/base/lib/math/bits.cc', 'export /* comment */ module lib.math:bits;')
        self.assertEqual(self.generate(), [bt.Path('/workspace/deps/base/generated-headers/math/bits.h')])

    def test_absolute_input_uses_same_layout_and_flags(self) -> None:
        """Absolute input paths preserve project-relative BUILD.py lookup."""
        self.fs.write_text('deps/base/lib/math/math.cc', 'export module lib.math;')
        self.fs.write_text('deps/base/lib/math/BUILD.py', "CFLAGS = ['-DLOCAL']\n")
        self.generate('/workspace/deps/base/lib/')
        self.assertIn('-DLOCAL', self.commands[1][0])
        self.assertIn('/workspace/deps/base/generated-headers/math/math.h', self.commands[1][0])

    def test_hidden_and_symlink_directories_are_not_followed(self) -> None:
        """Avoid hidden build metadata and symlink cycles during recursive discovery."""
        self.fs.makedirs('deps/base/lib/.hidden')
        self.fs.write_text('deps/base/lib/.hidden/hidden.cc', 'export module hidden;')
        self.fs.symlink('.', 'deps/base/lib/cycle')
        self.assertEqual(self.generate(), [])

    def test_missing_extractor_reports_setup_instructions(self) -> None:
        """Discover modules before requiring the optional extractor executable."""
        self.fs.write_text('deps/base/lib/math/math.cc', 'export module lib.math;')
        with mock.patch.dict(os.environ, {'BT_MODULE_HEADER': 'missing'}):
            with self.assertRaisesRegex(RuntimeError, 'Build module-header with CMake'):
                self.generate()
        self.assertEqual(self.commands, [])

    def test_requires_directory(self) -> None:
        """Reject missing paths and regular files as scan roots."""
        self.fs.write_text('deps/base/lib/file.cc', '')
        for path in ('missing', 'deps/base/lib/file.cc'):
            with self.subTest(path=path), self.assertRaisesRegex(RuntimeError, 'Expected a directory'):
                self.generate(path)

    def test_extractor_failure_stops_generation(self) -> None:
        """Propagate extractor failures without reporting later files as generated."""
        self.fs.write_text('deps/base/lib/a.cc', 'export module a;')
        self.fs.write_text('deps/base/lib/z.cc', 'export module z;')
        with mock.patch.object(bt, 'shell', side_effect=[
                '/resource\n', subprocess.CalledProcessError(1, ['extractor'])]) as command:
            with self.assertRaises(subprocess.CalledProcessError):
                self.generate()
        self.assertEqual(command.call_count, 2)

    def test_cli_selects_clang_and_respects_verbose(self) -> None:
        """The subcommand uses the extractor's compiler even when normal builds use GCC."""
        with mock.patch.dict(vars(bt)), mock.patch.object(bt, 'ROOT', '.'), \
             mock.patch.dict(os.environ, {'BT_MODULE_HEADER_CLANG': 'matching-clang++'}), \
             mock.patch.object(sys, 'argv', ['bt', 'generate-module-headers', '--verbose', 'deps/base/lib']), \
             mock.patch.object(bt, 'generate_module_headers') as generate:
            bt.main(vfs=self.fs)
        path, cfg = generate.call_args.args
        self.assertEqual(str(path), 'deps/base/lib')
        self.assertEqual(cfg.CXX, 'matching-clang++')
        self.assertTrue(cfg.USECLANG)
        self.assertTrue(cfg.VERBOSE)
