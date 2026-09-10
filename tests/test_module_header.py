"""Opt-in extraction tests using the module-to-header executable and matching Clang."""

import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest


@unittest.skipUnless(os.environ.get('BT_TEST_MODULE_HEADER') and
                     os.environ.get('BT_TEST_MODULE_HEADER_CLANG'),
                     'set BT_TEST_MODULE_HEADER and BT_TEST_MODULE_HEADER_CLANG')
class ModuleHeaderTests(unittest.TestCase):
    def setUp(self) -> None:
        """Use a fresh directory and the resource headers from the selected compiler."""
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory(prefix='module-header-test-')))
        self.compiler = os.environ['BT_TEST_MODULE_HEADER_CLANG']
        self.tool = os.environ['BT_TEST_MODULE_HEADER']
        resource = subprocess.check_output([self.compiler, '-print-resource-dir'], text=True).strip()
        self.flags = ['-std=c++26', '-resource-dir=' + resource]
        self.ldflags = shlex.split(os.environ.get('BT_TEST_CLANG_LDFLAGS', ''))
        self.source = self.directory / 'api.cc'
        self.header = self.directory / 'api.h'

    def extract(self, source: str, extra: tuple[str, ...] = ()) -> subprocess.CompletedProcess[str]:
        """Extract source with extra compiler flags, returning captured tool diagnostics."""
        self.source.write_text(source)
        return subprocess.run([self.tool, str(self.source), '-o', str(self.header), '--',
                               *self.flags, *extra], text=True, capture_output=True)

    def compile(self, *args: str) -> None:
        """Run the matching compiler with args and fail with its complete diagnostics."""
        result = subprocess.run([self.compiler, *self.flags, *args], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_header_consumers_link_with_module_object(self) -> None:
        """Preserve public types/templates/inline bodies and link declarations to the module."""
        result = self.extract('''module;
#include <cstdint>
export module fixture;
int implementation_only(int value) { return value + 3; }
export extern "C++" {
namespace api {
inline constexpr int base = 2;
inline int shared = 4;
int counter = 7;
int add(int value, int offset = 2) { return implementation_only(value) + offset; }
constexpr int square(int value) { return value * value; }
inline auto inferred(int value) { return value + 1; }
template<class T> constexpr T twice(T value) { return value + value; }
template<class T> struct Box { T value; constexpr T get() const { return value; } };
using Number = std::int32_t;
enum class Kind { first, second };
}
}
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        header = self.header.read_text()
        self.assertNotIn('export ', header)
        self.assertNotIn('implementation_only', header)
        self.assertIn('extern int counter', header)
        self.assertIn('#include <cstdint>', header)
        self.assertNotIn('attached to a named module', result.stderr)
        main = self.directory / 'main.cc'
        main.write_text('''#include "api.h"
static_assert(api::square(3) == 9);
static_assert(api::twice(4) == 8);
static_assert(api::Box<int>{5}.get() == 5);
int other();
int main() { return api::add(1) + api::counter + api::inferred(2) + other() != 19; }
''')
        other = self.directory / 'other.cc'
        other.write_text('#include "api.h"\nint other() { return api::inferred(2); }\n')
        obj = self.directory / 'api.o'
        self.compile('-x', 'c++-module', '-c', str(self.source),
                     '-fmodule-output=' + str(self.directory / 'api.pcm'), '-o', str(obj))
        binary = self.directory / 'consumer'
        self.compile(str(main), str(other), str(obj), *self.ldflags, '-o', str(binary))
        self.assertEqual(subprocess.run([str(binary)]).returncode, 0)

    def test_native_module_reports_abi_limitation(self) -> None:
        """Header generation must not imply that native module symbol names were changed."""
        result = self.extract('export module fixture;\nexport int answer() { return 42; }\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('does not fix their ABI', result.stderr)
        self.assertIn('int answer()', self.header.read_text())

    def test_private_dependency_is_rejected_without_overwriting_header(self) -> None:
        """Validation catches a retained body referencing an unexported declaration."""
        self.header.write_text('existing header\n')
        result = self.extract('''export module fixture;
constexpr int secret = 9;
export constexpr int answer() { return secret; }
''')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('generated header validation failed', result.stderr)
        self.assertEqual(self.header.read_text(), 'existing header\n')

    def test_local_macros_are_rejected(self) -> None:
        """Do not silently lose configuration macros referenced by retained source text."""
        result = self.extract('export module fixture;\n#define VALUE 4\nexport constexpr int value = VALUE;\n')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('local #define is unsupported', result.stderr)
        self.assertFalse(self.header.exists())

    def test_non_inline_deduced_return_requires_source_change(self) -> None:
        """Do not change a function's inline contract just to retain its required body."""
        result = self.extract('export module fixture;\nexport auto answer() { return 42; }\n')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('needs explicit inline', result.stderr)
        self.assertFalse(self.header.exists())

    def test_reexport_needs_header_mapping(self) -> None:
        """Even a successfully parsed import must not disappear from the generated API."""
        dependency = self.directory / 'dep.cc'
        dependency.write_text('export module dependency;\nexport int answer();\n')
        pcm = self.directory / 'dependency.pcm'
        self.compile('-x', 'c++-module', '--precompile', str(dependency), '-o', str(pcm))
        result = self.extract('export module fixture;\nexport import dependency;\n',
                              ('-fmodule-file=dependency=' + str(pcm),))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('imports need a module-to-header mapping', result.stderr)
        self.assertFalse(self.header.exists())

    def test_output_cannot_overwrite_source(self) -> None:
        """Reject identical source/output paths before extraction or file replacement."""
        self.source.write_text('export module fixture;\nexport int answer();\n')
        before = self.source.read_text()
        result = subprocess.run([self.tool, str(self.source), '-o', str(self.source), '--',
                                 *self.flags], text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('must not overwrite', result.stderr)
        self.assertEqual(self.source.read_text(), before)
