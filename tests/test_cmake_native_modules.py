"""Exercise buildtool providers alongside native CMake modules with real Ninja builds."""

import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


@unittest.skipUnless(all(shutil.which(tool) for tool in ('cmake', 'ninja', 'g++')),
                     'requires CMake, Ninja and GCC')
class NativeModuleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        """Require tool versions with native GCC module scanning and the file API."""
        version = subprocess.check_output(['cmake', '--version'], text=True).split()[2]
        if tuple(map(int, version.split('.')[:2])) < (3, 30):
            raise unittest.SkipTest('requires CMake 3.30+')
        if int(subprocess.check_output(['g++', '-dumpversion'], text=True).split('.')[0]) < 14:
            raise unittest.SkipTest('requires GCC 14+')

    def test_mixed_modules(self) -> None:
        """Check native providers, transitive users, header units, and incremental metadata."""
        for generator in ('Ninja', 'Ninja Multi-Config'):
            with self.subTest(generator=generator):
                self.mixed_modules(generator)

    def mixed_modules(self, generator: str) -> None:
        """Build an external module plus a native module with generator in a path with spaces."""
        helper = Path(__file__).resolve().parents[1] / 'cmake/Buildtool.cmake'
        with tempfile.TemporaryDirectory(prefix='buildtool mixed modules ') as temporary:
            root = Path(temporary)
            external = root / 'external'
            external.mkdir()
            build = root / 'build'
            config = 'Debug'

            def run(*args: str) -> str:
                """Run args in the fixture and include combined diagnostics on failure."""
                result = subprocess.run(args, cwd=root, text=True, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, timeout=120)
                self.assertEqual(result.returncode, 0, result.stdout)
                # CMake's collator can print an error without returning failure.
                self.assertNotIn('CMake Error', result.stdout)
                return result.stdout

            (root / 'CMakeLists.txt').write_text(f'''
cmake_minimum_required(VERSION 3.30)
project(mixed CXX)
include("{helper}")
buildtool_register_project(external SOURCE_ROOT external)
target_compile_features(external PUBLIC cxx_std_23)
add_library(native STATIC)
target_sources(native PUBLIC FILE_SET CXX_MODULES FILES native.cc)
target_compile_features(native PUBLIC cxx_std_23)
buildtool_target_modules(native PUBLIC LIBRARY external MODULES answer)
add_executable(app main.cc)
target_link_libraries(app PRIVATE native)
''')
            header = external / 'unit.h'
            header.write_text('#pragma once\n#ifndef VALUE\n#define VALUE 40\n#endif\n')
            answer = external / 'answer.cc'
            answer.write_text('export module answer;\nimport "unit.h";\n'
                              'export int answer() { return VALUE + 1; }\n')
            native = root / 'native.cc'
            native.write_text('export module native;\nimport answer;\n'
                              'export int result() { return answer(); }\n')
            (root / 'main.cc').write_text('#define VALUE 777\n#include "unit.h"\n'
                                         'import native;\nimport answer;\n'
                                         'static_assert(VALUE == 777);\n'
                                         'int main() { return answer() + result(); }\n')
            configure = ['cmake', '-G', generator, '-S', '.', '-B', 'build',
                         '-DCMAKE_CXX_COMPILER=g++', '-DCMAKE_BUILD_TYPE=Debug']
            run(*configure)
            command = ['cmake', '--build', 'build', '--config', config, '-j4']
            executable = build / ('Debug/app' if generator == 'Ninja Multi-Config' else 'app')

            def check(expected: int) -> str:
                """Build once, then verify the executable returns expected in this invocation."""
                output = run(*command)
                self.assertEqual(subprocess.run([str(executable)]).returncode, expected, output)
                return output

            check(82)
            directory = build / 'buildtool/native_buildtool_modules/Debug'
            self.assertNotIn('-fmodule-mapper', (directory / 'consumer.rsp').read_text())
            maps = list(build.rglob('main.cc.o.modmap'))
            self.assertEqual(len(maps), 1)
            self.assertIn('\nnative ', maps[0].read_text())
            self.assertIn('\nanswer ', maps[0].read_text())
            tracked = [*build.rglob('*.o'), *build.rglob('*.gcm'), maps[0], executable]
            before = {path: path.stat().st_mtime_ns for path in tracked}
            check(82)
            self.assertEqual(before, {path: path.stat().st_mtime_ns for path in tracked})

            # A header unit is consumed transitively via the buildtool-produced BMI.
            header.write_text(header.read_text().replace('VALUE 40', 'VALUE 41'))
            check(84)
            # Discover new providers after configuration and update native module maps.
            extra = external / 'extra.cc'
            extra.write_text('export module extra;\nexport int extra() { return 2; }\n')
            answer.write_text(answer.read_text().replace('import "unit.h";',
                              'import "unit.h";\nimport extra;').replace('VALUE + 1', 'VALUE + extra()'))
            check(86)
            metadata = json.loads((directory / 'CXXModules.json').read_text())
            self.assertEqual(metadata['usages']['answer'], ['extra'])
            # Native changes continue to be compiled and scheduled by CMake.
            native.write_text(native.read_text().replace('return answer();', 'return answer() + 1;'))
            check(87)
            # Regeneration overwrites the injected inputs; the bridge must restore them.
            run(*configure)
            check(87)
            answer.write_text(answer.read_text().replace('\nimport extra;', '').replace('VALUE + extra()', 'VALUE + 1'))
            extra.unlink()
            check(85)
            self.assertNotIn('extra', json.loads((directory / 'CXXModules.json').read_text())['modules'])
            artifacts = Path(json.loads((directory / 'providers.json').read_text())['repository'])
            archives = artifacts.parent / 'archives'
            run('cmake', '--build', 'build', '--config', config, '--target', 'clean')
            self.assertFalse(artifacts.exists())
            self.assertFalse(archives.exists())
            self.assertTrue((artifacts.parent / 'root').is_file())
            self.assertTrue((artifacts.parent / 'build.lock').is_file())
            check(85)


if __name__ == '__main__':
    unittest.main()
