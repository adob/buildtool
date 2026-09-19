"""Exercise the CMake/buildtool boundary with real GCC module compilations."""

import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import time
import unittest


@unittest.skipUnless(shutil.which('cmake') and shutil.which('g++'), 'requires CMake and GCC')
class CMakeBridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        """Skip real-compiler tests when the installed tools lack the tested module support."""
        cmake_version = subprocess.check_output(['cmake', '--version'], text=True).split()[2]
        if tuple(map(int, cmake_version.split('.')[:2])) < (3, 30):
            raise unittest.SkipTest('requires CMake 3.30+')
        compiler_version = subprocess.check_output(['g++', '-dumpversion'], text=True).strip()
        if int(compiler_version.split('.')[0]) < 14:
            raise unittest.SkipTest('requires GCC 14+ for the C++26 fixture')

    def test_shared_header_units(self) -> None:
        """Concurrent library builds share one nested header unit despite private macros."""
        adapter = pathlib.Path(__file__).resolve().parents[1] / 'cmake/Buildtool.cmake'
        with tempfile.TemporaryDirectory(prefix='bt-shared-headers-') as temporary:
            root = pathlib.Path(temporary)
            (root / 'common').mkdir()
            (root / 'common/inner.h').write_text('#pragma once\n#define NUMBER 41\n')
            (root / 'common/value.h').write_text(
                '#pragma once\nimport "inner.h";\n'
                '#ifdef PRIVATE_VALUE\n#error Consumer macro leaked into shared unit\n#endif\n'
                'inline int number() { return NUMBER; }\n')
            cmake = f'''cmake_minimum_required(VERSION 3.30)
project(shared_headers CXX)
include("{adapter}")
'''
            for name, value in [('left', 1), ('right', 2)]:
                (root / name).mkdir()
                (root / name / 'entry.cc').write_text(
                    f'import "value.h";\nint {name}() {{ return number() + PRIVATE_VALUE; }}\n')
                cmake += f'''
buildtool_register_project({name} SOURCE_ROOT {name})
target_include_directories({name} PRIVATE "${{CMAKE_CURRENT_SOURCE_DIR}}/common")
target_compile_features({name} PUBLIC cxx_std_23)
target_compile_definitions({name} PRIVATE PRIVATE_VALUE={value})
buildtool_add_library({name}_archive LIBRARY {name} SOURCES {name}/entry.cc)
'''
            (root / 'main.cc').write_text(
                'int left(); int right(); int main() { return left() == 42 && right() == 43 ? 0 : 1; }\n')
            cmake += '''
add_executable(app main.cc)
target_link_libraries(app PRIVATE left_archive right_archive)
'''
            (root / 'CMakeLists.txt').write_text(cmake)

            def run(*args: str) -> str:
                """Run args in root with bounded execution and captured diagnostics."""
                result = subprocess.run(args, cwd=root, text=True, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, timeout=120)
                self.assertEqual(result.returncode, 0, result.stdout)
                return result.stdout

            run('cmake', '-S', '.', '-B', 'build', '-G', 'Unix Makefiles', '-DCMAKE_CXX_COMPILER=g++')
            workers = [subprocess.Popen(['cmake', '--build', 'build', '--target', f'{name}_archive_build'],
                       cwd=root, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                       for name in ('left', 'right')]
            try:
                for worker in workers:
                    output, _ = worker.communicate(timeout=120)
                    self.assertEqual(worker.returncode, 0, output)
            finally:
                for worker in workers:
                    if worker.poll() is None:
                        worker.kill()
                    worker.wait()
            units = list((root / 'build/buildtool/libraries/buildtool_header_units').rglob('*.pcm'))
            self.assertEqual(len(units), 2, units)
            self.assertFalse(list((root / 'build/buildtool/libraries/left').rglob('*.pcm')))
            self.assertFalse(list((root / 'build/buildtool/libraries/right').rglob('*.pcm')))
            run('cmake', '--build', 'build', '--target', 'app', '-j2')
            run(str(root / 'build/app'))
            before = {p: p.stat().st_mtime_ns for p in units}
            run('cmake', '--build', 'build', '--target', 'app', '-j2')
            self.assertEqual(before, {p: p.stat().st_mtime_ns for p in units})

    def test_source_executable(self) -> None:
        """Build an application with discovered imports, generated input and a CMake library."""
        adapter = pathlib.Path(__file__).resolve().parents[1] / 'cmake/Buildtool.cmake'
        generators = ['Unix Makefiles']
        if shutil.which('ninja'):
            generators.extend(['Ninja', 'Ninja Multi-Config'])
        for generator in generators:
            with self.subTest(generator=generator), tempfile.TemporaryDirectory(prefix='bt-app-') as temporary:
                root = pathlib.Path(temporary)
                (root / 'library').mkdir()
                (root / 'app').mkdir()
                (root / 'library/number.cc').write_text(
                    'export module number;\nexport int number() { return 30; }\n')
                (root / 'external.h').write_text('int external();\n')
                (root / 'external.cc').write_text(
                    '#ifndef NATIVE_CMAKE\n#error Must be compiled by CMake\n#endif\n'
                    'int external() { return 12; }\n')
                (root / 'app/main.cc').write_text('''
import number;
import "value.h";
#include "generated.h"
static_assert(EXTERNAL_FLAG == 7);
#ifndef __OPTIMIZE__
#error Missing Release flags
#endif
#include "external.h"
int extra();
int main() { return number() + external() + extra() + VALUE + GENERATED_VALUE == 46 ? 0 : 1; }
''')
                (root / 'app/value.h').write_text('#define VALUE 1\n')
                (root / 'app/extra.cpp').write_text('int extra() { return 2; }\n')
                (root / 'app/extra_alias.cpp').symlink_to('extra.cpp')
                (root / 'CMakeLists.txt').write_text(f'''
cmake_minimum_required(VERSION 3.30)
project(app_test CXX)
include("{adapter}")
buildtool_register_project(library SOURCE_ROOT library)
target_compile_features(library PUBLIC cxx_std_23)
add_library(external STATIC external.cc)
target_compile_definitions(external PRIVATE NATIVE_CMAKE=1)
target_include_directories(external INTERFACE "${{CMAKE_CURRENT_SOURCE_DIR}}")
target_compile_definitions(external INTERFACE EXTERNAL_FLAG=7)
add_subdirectory(app)
''')
                (root / 'app/CMakeLists.txt').write_text('''
add_custom_command(OUTPUT "${CMAKE_CURRENT_BINARY_DIR}/generated.h"
  COMMAND ${CMAKE_COMMAND} -E echo "#define GENERATED_VALUE 1" > "${CMAKE_CURRENT_BINARY_DIR}/generated.h"
  VERBATIM)
add_custom_target(generate_header DEPENDS "${CMAKE_CURRENT_BINARY_DIR}/generated.h")
target_include_directories(external INTERFACE "${CMAKE_CURRENT_BINARY_DIR}")
buildtool_add_executable(app LIBRARY library SOURCES main.cc extra.cpp extra_alias.cpp
  PRIVATE_LIBRARIES external DEPENDS generate_header)
''')

                def run(*command: str) -> str:
                    """Run command in the fixture and report captured output on failure."""
                    result = subprocess.run(command, cwd=root, text=True, stdout=subprocess.PIPE,
                                            stderr=subprocess.STDOUT, timeout=180)
                    self.assertEqual(result.returncode, 0, result.stdout)
                    return result.stdout

                run('cmake', '-S', '.', '-B', 'build', '-G', generator,
                    '-DCMAKE_CXX_COMPILER=g++', '-DCMAKE_BUILD_TYPE=Release')
                build = ('cmake', '--build', 'build', '--config', 'Release', '--target', 'app', '-j2')
                run(*build)
                binary = root / 'build/app' / ('Release' if generator == 'Ninja Multi-Config' else '') / 'app'
                run(str(binary))
                before = binary.stat().st_mtime_ns
                run(*build)
                self.assertEqual(binary.stat().st_mtime_ns, before)
                self.assertFalse(list((root / 'build').rglob('consumer.rsp')))
                artifacts = root / 'build/buildtool/libraries/app_buildtool_settings/Release/artifacts'
                self.assertEqual(len(list(artifacts.rglob('extra.o'))), 1)
                self.assertFalse(list(artifacts.rglob('extra_alias.o')))

    def test_source_archive(self) -> None:
        """Discover imports from source roots, share archives, and track edits and cleaning."""
        adapter = pathlib.Path(__file__).resolve().parents[1] / 'cmake/Buildtool.cmake'
        generators = ['Unix Makefiles']
        if shutil.which('ninja'):
            generators.extend(['Ninja', 'Ninja Multi-Config'])
        for generator in generators:
            with self.subTest(generator=generator), tempfile.TemporaryDirectory(prefix='bt sources-') as temporary:
                root = pathlib.Path(temporary)
                (root / 'library/lib').mkdir(parents=True)
                module = root / 'library/lib/number.cc'
                module.write_text('export module lib.number;\nexport int number() { return 33; }\n')
                header = root / 'library/value.h'
                header.write_text('#pragma once\n#define HEADER_VALUE 10\n')
                entry = root / 'library/entry.cc'
                entry.write_text('import lib.number;\nimport "value.h";\n'
                                 '#include "helper.h"\nstatic_assert(LIBRARY_PRIVATE == 7);\n'
                                 '#ifndef __OPTIMIZE__\n#error Missing Release flags\n#endif\n'
                                 'int answer() { return number() + HEADER_VALUE + helper(); }\n')
                (root / 'library/helper.h').write_text('int helper();\n')
                (root / 'library/helper.cc').write_text('int helper() { return 1; }\n')
                (root / 'library/extra.cpp').write_text('int extra() { return 2; }\n')
                (root / 'main.cc').write_text('#include <cstdio>\nint answer(); int extra();\n'
                                            'int main() { std::printf("%d\\n", answer() + extra()); }\n')
                (root / 'CMakeLists.txt').write_text(f'''
cmake_minimum_required(VERSION 3.30)
project(sources CXX)
include("{adapter}")
buildtool_register_project(library SOURCE_ROOT library)
target_compile_features(library PUBLIC cxx_std_23)
target_compile_definitions(library PRIVATE LIBRARY_PRIVATE=7)
foreach(name first second)
  buildtool_add_library(${{name}} LIBRARY library SOURCES library/entry.cc library/extra.cpp)
  add_executable(${{name}}_app main.cc)
  target_link_libraries(${{name}}_app PRIVATE ${{name}})
endforeach()
''')

                def run(*command: str) -> str:
                    """Run command in the fixture and include captured diagnostics on failure."""
                    result = subprocess.run(command, cwd=root, text=True, stdout=subprocess.PIPE,
                                            stderr=subprocess.STDOUT, timeout=180)
                    self.assertEqual(result.returncode, 0, result.stdout)
                    return result.stdout

                run('cmake', '-S', '.', '-B', 'build', '-G', generator,
                    '-DCMAKE_CXX_COMPILER=g++', '-DCMAKE_BUILD_TYPE=Release')
                build = ('cmake', '--build', 'build', '--config', 'Release', '-j2')
                run(*build)
                binary_dir = root / 'build' / ('Release' if generator == 'Ninja Multi-Config' else '')
                self.assertEqual(run(str(binary_dir / 'first_app')), '46\n')
                self.assertEqual(run(str(binary_dir / 'second_app')), '46\n')
                first = root / 'build/buildtool/first_buildtool_sources/Release/libmodules.a'
                second = root / 'build/buildtool/second_buildtool_sources/Release/libmodules.a'
                self.assertEqual(first.resolve(), second.resolve())
                artifacts = root / 'build/buildtool/libraries/library/Release/artifacts'
                tracked = [*artifacts.rglob('*.o'), *artifacts.rglob('*.pcm'), first.resolve(),
                           binary_dir / 'first_app']
                self.assertTrue(list(artifacts.rglob('*.pcm')))
                before = {path: path.stat().st_mtime_ns for path in tracked}
                run(*build)
                self.assertEqual(before, {path: path.stat().st_mtime_ns for path in tracked})
                # Ordinary consumers receive neither a mapper nor the library's private flags.
                self.assertFalse(list((root / 'build/buildtool').rglob('consumer.rsp')))
                time.sleep(1.05)
                header.write_text('#pragma once\n#define HEADER_VALUE 11\n')
                run(*build)
                self.assertEqual(run(str(binary_dir / 'first_app')), '47\n')
                time.sleep(1.05)
                module.write_text('export module lib.number;\nexport int number() { return 34; }\n')
                run(*build)
                self.assertEqual(run(str(binary_dir / 'second_app')), '48\n')
                time.sleep(1.05)
                entry.write_text(entry.read_text().replace('return number()', 'return 1 + number()'))
                run(*build)
                self.assertEqual(run(str(binary_dir / 'first_app')), '49\n')
                run('cmake', '--build', 'build', '--config', 'Release', '--target', 'clean')
                self.assertFalse(artifacts.exists())
                run(*build)
                self.assertEqual(run(str(binary_dir / 'second_app')), '49\n')

    def test_c_companion(self) -> None:
        """Compile discovered C with its owner's C flags and rebuild after C header edits."""
        adapter = pathlib.Path(__file__).resolve().parents[1] / 'cmake/Buildtool.cmake'
        generators = ['Unix Makefiles']
        if shutil.which('ninja'):
            generators.append('Ninja')
        for generator in generators:
            with self.subTest(generator=generator), tempfile.TemporaryDirectory(prefix='bt-c-') as temporary:
                root = pathlib.Path(temporary)
                (root / 'provider/private').mkdir(parents=True)
                (root / 'library').mkdir()
                (root / 'provider/helper.h').write_text('int helper(void);\n')
                header = root / 'provider/private/value.h'
                header.write_text('#define VALUE 42\n')
                (root / 'provider/helper.c').write_text('''
#include "helper.h"
#include "value.h"
#ifdef __cplusplus
#error C compiled as C++
#endif
#ifdef CXX_ONLY
#error C++ settings leaked into C
#endif
_Static_assert(__STDC_VERSION__ == 201112L, "C standard not applied");
_Static_assert(OWNER == 9 && C_ONLY == 1 && BUILD_C == 1, "Wrong C settings");
int helper(void) { return VALUE; }
''')
                (root / 'library/entry.cc').write_text('''
extern "C" {
#include "../provider/helper.h"
}
static_assert(OWNER == 7 && CXX_ONLY == 1);
int answer() { return helper(); }
''')
                (root / 'main.cc').write_text('#include <cstdio>\nint answer();\n'
                                            'int main() { std::printf("%d\\n", answer()); }\n')
                cmake = root / 'CMakeLists.txt'
                cmake.write_text(f'''
cmake_minimum_required(VERSION 3.30)
project(c_companion LANGUAGES CXX)
include("{adapter}")
buildtool_register_project(provider SOURCE_ROOT provider)
buildtool_register_project(library SOURCE_ROOT library)
target_link_libraries(library PRIVATE provider)
target_compile_definitions(provider PRIVATE OWNER=9 "$<$<COMPILE_LANGUAGE:C>:C_ONLY=1>"
  "$<$<COMPILE_LANGUAGE:CXX>:CXX_ONLY=1>")
target_compile_definitions(library PRIVATE OWNER=7 "$<$<COMPILE_LANGUAGE:CXX>:CXX_ONLY=1>")
target_include_directories(provider PRIVATE "$<$<COMPILE_LANGUAGE:C>:${{CMAKE_CURRENT_SOURCE_DIR}}/provider/private>")
set_target_properties(provider PROPERTIES C_STANDARD 11 C_STANDARD_REQUIRED ON)
buildtool_add_library(archive LIBRARY library SOURCES library/entry.cc)
add_executable(app main.cc)
target_link_libraries(app PRIVATE archive)
''')

                def run(*command: str, success: bool = True) -> str:
                    """Run command in the fixture, asserting the requested exit status."""
                    result = subprocess.run(command, cwd=root, text=True, stdout=subprocess.PIPE,
                                            stderr=subprocess.STDOUT, timeout=120)
                    self.assertEqual(result.returncode == 0, success, result.stdout)
                    return result.stdout

                configure = ('cmake', '-S', '.', '-B', 'build', '-G', generator,
                             '-DCMAKE_CXX_COMPILER=g++')
                run(*configure)
                build = ('cmake', '--build', 'build', '-j2')
                self.assertIn('Enable C in project', run(*build, success=False))
                cmake.write_text(cmake.read_text().replace('LANGUAGES CXX', 'LANGUAGES C CXX'))
                run(*configure, '-DCMAKE_C_COMPILER=gcc', '-DCMAKE_C_FLAGS=-DBUILD_C=1')
                run(*build)
                self.assertEqual(run(str(root / 'build/app')), '42\n')
                artifacts = root / 'build/buildtool/libraries/provider/artifacts'
                objects = list(artifacts.rglob('helper.o'))
                self.assertEqual(len(objects), 1)
                before = objects[0].stat().st_mtime_ns
                run(*build)
                self.assertEqual(before, objects[0].stat().st_mtime_ns)
                time.sleep(1.05)
                header.write_text('#define VALUE 43\n')
                run(*build)
                self.assertGreater(objects[0].stat().st_mtime_ns, before)
                self.assertEqual(run(str(root / 'build/app')), '43\n')

    def test_source_archive_public_modules(self) -> None:
        """Publish discovered imports for headers without inheriting two GCC mappers."""
        adapter = pathlib.Path(__file__).resolve().parents[1] / 'cmake/Buildtool.cmake'
        for generator in ('Unix Makefiles', 'Ninja'):
            if generator == 'Ninja' and not shutil.which('ninja'):
                continue
            with self.subTest(generator=generator), tempfile.TemporaryDirectory(prefix='bt-public-') as temporary:
                root = pathlib.Path(temporary)
                (root / 'library').mkdir()
                module = root / 'library/value.cc'
                module.write_text('export module value;\nexport int value() { return 42; }\n')
                (root / 'library/left.h').write_text('#pragma once\nimport value;\nint left();\n')
                (root / 'library/left.cc').write_text('#include "left.h"\nint left() { return value(); }\n')
                (root / 'library/right.h').write_text('#include "left.h"\nint right();\n')
                (root / 'library/right.cc').write_text('#include "right.h"\nint right() { return left(); }\n')
                (root / 'generated.in').write_text('int generated() { return 0; }\n')
                (root / 'main.cc').write_text('#include "right.h"\n#include <cstdio>\n'
                                            'int main() { std::printf("%d\\n", right()); }\n')
                (root / 'CMakeLists.txt').write_text(f'''
cmake_minimum_required(VERSION 3.30)
project(public_sources CXX)
set(CMAKE_CXX_SCAN_FOR_MODULES OFF)
include("{adapter}")
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)
buildtool_register_project(library SOURCE_ROOT library)
buildtool_add_library(left LIBRARY library PUBLIC_MODULES
  SOURCES library/left.cc library/generated.cc)
add_custom_command(OUTPUT "${{CMAKE_CURRENT_SOURCE_DIR}}/library/generated.cc"
  COMMAND "${{CMAKE_COMMAND}}" -E copy "${{CMAKE_CURRENT_SOURCE_DIR}}/generated.in"
    "${{CMAKE_CURRENT_SOURCE_DIR}}/library/generated.cc"
  DEPENDS generated.in VERBATIM)
add_custom_target(generate DEPENDS "${{CMAKE_CURRENT_SOURCE_DIR}}/library/generated.cc")
add_dependencies(left_build generate)
buildtool_add_library(right LIBRARY library PUBLIC_MODULES SOURCES library/right.cc)
target_link_libraries(right INTERFACE "$<LINK_ONLY:left>")
add_executable(app main.cc)
target_link_libraries(app PRIVATE right)
''')

                def run(*command: str) -> str:
                    """Run command in the fixture, reporting captured output on failure."""
                    result = subprocess.run(command, cwd=root, text=True, stdout=subprocess.PIPE,
                                            stderr=subprocess.STDOUT, timeout=120)
                    self.assertEqual(result.returncode, 0, result.stdout)
                    return result.stdout

                run('cmake', '-S', '.', '-B', 'build', '-G', generator, '-DCMAKE_CXX_COMPILER=g++')
                run('cmake', '--build', 'build', '-j2')
                self.assertEqual(run(str(root / 'build/app')), '42\n')
                database = json.loads((root / 'build/compile_commands.json').read_text())
                command = next(entry['command'] for entry in database if entry['file'].endswith('/main.cc'))
                self.assertEqual(command.count('consumer.rsp'), 1, command)
                state = root / 'build/buildtool/right_buildtool_sources/state.h'
                before = state.stat().st_mtime_ns
                run('cmake', '--build', 'build', '-j2')
                self.assertEqual(before, state.stat().st_mtime_ns)
                time.sleep(1.05)
                module.write_text(module.read_text().replace('return 42', 'return 43'))
                run('cmake', '--build', 'build', '-j2')
                self.assertEqual(run(str(root / 'build/app')), '43\n')

    def test_conflicting_bundles_are_rejected(self) -> None:
        """Reject a native consumer that would otherwise silently use the last mapper."""
        adapter = pathlib.Path(__file__).resolve().parents[1] / 'cmake/Buildtool.cmake'
        with tempfile.TemporaryDirectory(prefix='buildtool-conflict-') as temporary:
            root = pathlib.Path(temporary)
            (root / 'main.cc').write_text('int main() { return 0; }\n')
            (root / 'CMakeLists.txt').write_text(f'''
cmake_minimum_required(VERSION 3.30)
project(conflict CXX)
include("{adapter}")
buildtool_register_project(library SOURCE_ROOT .)
foreach(name left right)
  add_library(${{name}} STATIC main.cc)
  buildtool_target_modules(${{name}} PUBLIC LIBRARY library MODULES lib.answer)
endforeach()
add_executable(app main.cc)
target_link_libraries(app PRIVATE left right)
''')
            result = subprocess.run(['cmake', '-S', '.', '-B', 'build', '-DCMAKE_CXX_COMPILER=g++'],
                                    cwd=root, text=True, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, timeout=120)
            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertIn('BUILDTOOL_BUNDLE', result.stdout)

    @unittest.skipUnless(shutil.which('ninja'), 'requires Ninja')
    def test_module_mode_selection(self) -> None:
        """Infer native integration from settings present when the helper is called."""
        adapter = pathlib.Path(__file__).resolve().parents[1] / 'cmake/Buildtool.cmake'
        cases = [
            ('unset', '', '', '', False),
            ('disabled', '', 'set_property(TARGET app PROPERTY CXX_SCAN_FOR_MODULES OFF)', '', False),
            ('enabled', '', 'set_property(TARGET app PROPERTY CXX_SCAN_FOR_MODULES ON)', '', True),
            ('global_enabled', 'set(CMAKE_CXX_SCAN_FOR_MODULES ON)', '', '', True),
            ('global_disabled', 'set(CMAKE_CXX_SCAN_FOR_MODULES OFF)', '', '', False),
            ('target_override', 'set(CMAKE_CXX_SCAN_FOR_MODULES ON)',
             'set_property(TARGET app PROPERTY CXX_SCAN_FOR_MODULES OFF)', '', False),
            ('explicit', '', '', 'NATIVE_MODULES', True),
            ('file_set', '', 'target_sources(app PRIVATE FILE_SET CXX_MODULES FILES native.cc)', '', True),
            ('file_set_disabled', 'set(CMAKE_CXX_SCAN_FOR_MODULES OFF)',
             'target_sources(app PRIVATE FILE_SET CXX_MODULES FILES native.cc)', '', True),
        ]
        with tempfile.TemporaryDirectory(prefix='buildtool-mode-') as temporary:
            root = pathlib.Path(temporary)
            (root / 'main.cc').write_text('int main() { return 0; }\n')
            (root / 'native.cc').write_text('export module native;\n')
            for name, before, after, option, expected in cases:
                with self.subTest(case=name):
                    (root / 'CMakeLists.txt').write_text(f'''
cmake_minimum_required(VERSION 3.30)
project(mode CXX)
include("{adapter}")
buildtool_register_project(library SOURCE_ROOT .)
{before}
add_executable(app main.cc)
{after}
get_property(standard_before TARGET app PROPERTY CXX_STANDARD)
get_property(extensions_before TARGET app PROPERTY CXX_EXTENSIONS)
buildtool_target_modules(app PRIVATE {option} LIBRARY library MODULES answer)
get_property(standard_after TARGET app PROPERTY CXX_STANDARD)
get_property(extensions_after TARGET app PROPERTY CXX_EXTENSIONS)
if(NOT "${{standard_before}}" STREQUAL "${{standard_after}}" OR
   NOT "${{extensions_before}}" STREQUAL "${{extensions_after}}")
  message(FATAL_ERROR "The helper overwrote consumer language properties")
endif()
get_target_property(scan app CXX_SCAN_FOR_MODULES)
file(WRITE "${{CMAKE_BINARY_DIR}}/scan" "${{scan}}")
''')
                    result = subprocess.run(
                        ['cmake', '-G', 'Ninja', '-S', '.', '-B', name, '-DCMAKE_CXX_COMPILER=g++'],
                        cwd=root, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120)
                    self.assertEqual(result.returncode, 0, result.stdout)
                    mode = root / name / 'buildtool/app_buildtool_modules/native_modules'
                    self.assertEqual(mode.read_text().strip(), 'TRUE' if expected else 'FALSE')
                    self.assertEqual((root / name / 'scan').read_text(), 'TRUE' if expected else 'FALSE')
            # An inferred native request must fail clearly on unsupported generators.
            result = subprocess.run(
                ['cmake', '-G', 'Unix Makefiles', '-S', '.', '-B', 'make',
                 '-DCMAKE_CXX_COMPILER=g++', '-DCMAKE_CXX_SCAN_FOR_MODULES=ON'],
                cwd=root, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120)
            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertIn('requires Ninja or Ninja Multi-Config', result.stdout)

    def test_shared_modules(self) -> None:
        """Share library builds regardless of consumer-private settings."""
        modes = [('Unix Makefiles', False)]
        if shutil.which('ninja'):
            modes.extend((generator, native)
                         for generator in ('Ninja', 'Ninja Multi-Config') for native in (False, True))
        for generator, native in modes:
            with self.subTest(generator=generator, native=native):
                self.shared_modules(generator, native)

    def shared_modules(self, generator: str, native: bool) -> None:
        """Build three consumers of one library using generator and native mapping mode."""
        adapter = pathlib.Path(__file__).resolve().parents[1] / 'cmake/Buildtool.cmake'
        with tempfile.TemporaryDirectory(prefix='buildtool shared-') as temporary:
            root = pathlib.Path(temporary)
            (root / 'library/lib').mkdir(parents=True)
            (root / 'library/lib/foo.cc').write_text(
                'export module lib.foo;\nimport lib.detail;\n'
                '#ifndef __OPTIMIZE__\n#error Build-wide optimization was lost\n#endif\n'
                'static_assert(LIBRARY_PRIVATE == 7);\n'
                '#ifndef EXTRA\n#define EXTRA 0\n#endif\n'
                'export int answer() { return detail() + EXTRA; }\n')
            (root / 'library/lib/detail.cc').write_text(
                'export module lib.detail;\nimport "number.h";\n'
                'export int detail() { return VALUE; }\n')
            header = root / 'library/lib/number.h'
            header.write_text('#pragma once\n#define VALUE 42\n')
            # A dependency loads the bridge; sibling consumers must still find Python.
            (root / 'library/CMakeLists.txt').write_text(f'''
include("{adapter}")
buildtool_register_project(library SOURCE_ROOT .)
''')
            (root / 'CMakeLists.txt').write_text(f'''
cmake_minimum_required(VERSION 3.30)
project(shared CXX)
add_subdirectory(library)
target_compile_definitions(library PRIVATE LIBRARY_PRIVATE=7)
add_subdirectory(a)
add_subdirectory(b)
add_subdirectory(different)
add_executable(app main.cc)
target_link_libraries(app PRIVATE a b)
add_executable(other other.cc)
target_link_libraries(other PRIVATE different)
''')
            (root / 'main.cc').write_text(
                'int a(); int b(); int main() { return a() == 42 && b() == 42 ? 0 : 1; }\n')
            (root / 'other.cc').write_text(
                'int different(); int main() { return different() == 52 ? 0 : 1; }\n')
            for name in ('a', 'b', 'different'):
                (root / name).mkdir()
                (root / name / 'source.cc').write_text(
                    '#ifdef LIBRARY_PRIVATE\n#error Library private setting leaked\n#endif\n'
                    '#ifndef EXTRA\n#define EXTRA 0\n#endif\n'
                    f'import lib.foo;\nint {name}() {{ return answer() + EXTRA; }}\n')
                definition = 'target_compile_definitions(different PRIVATE EXTRA=10)' if name == 'different' else ''
                (root / name / 'CMakeLists.txt').write_text(f'''
add_library({name} STATIC source.cc)
{definition}
target_compile_options({name} PRIVATE -O0)
buildtool_target_modules({name} PRIVATE {'NATIVE_MODULES' if native else ''}
  LIBRARY library MODULES lib.foo)
''')

            def run(*args: str) -> str:
                """Run args in root, including build output in any failure."""
                result = subprocess.run(args, cwd=root, text=True, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, timeout=120)
                self.assertEqual(result.returncode, 0, result.stdout)
                self.assertNotIn('CMake Error', result.stdout)
                return result.stdout

            def bundle(name: str) -> pathlib.Path:
                """Return name's generated per-target metadata directory."""
                return root / f'build/{name}/buildtool/{name}_buildtool_modules/Release'

            run('cmake', '-G', generator, '-S', '.', '-B', 'build',
                '-DCMAKE_CXX_COMPILER=g++', '-DCMAKE_BUILD_TYPE=Release',
                '-DCMAKE_CXX_FLAGS_RELEASE=-O2 -DNDEBUG')
            build = ['cmake', '--build', 'build', '--config', 'Release', '-j4']
            output = run(*build, '--target', 'app')
            # Both consumers trigger bridge jobs, but each module is compiled once.
            self.assertEqual(output.count('BUILT module lib/foo.cc'), 1, output)
            self.assertEqual(output.count('BUILT module lib/detail.cc'), 1, output)
            providers = [json.loads((bundle(name) / 'providers.json').read_text()) for name in ('a', 'b')]
            self.assertEqual(providers[0]['repository'], providers[1]['repository'])
            self.assertEqual(providers[0]['modules'], providers[1]['modules'])
            archive = (bundle('a') / 'libmodules.a').resolve()
            self.assertTrue((bundle('a') / 'libmodules.a').is_symlink())
            self.assertEqual(archive, (bundle('b') / 'libmodules.a').resolve())
            binary_dir = root / 'build' / ('Release' if generator == 'Ninja Multi-Config' else '')
            run(str(binary_dir / 'app'))
            tracked = [archive, binary_dir / 'app', *pathlib.Path(providers[0]['repository']).rglob('*.o')]
            before = {path: path.stat().st_mtime_ns for path in tracked}
            self.assertNotIn('BUILT module ', run(*build, '--target', 'app'))
            self.assertEqual(before, {path: path.stat().st_mtime_ns for path in tracked})

            # Different consumer definitions do not change the library compilation.
            self.assertNotIn('BUILT module ', run(*build, '--target', 'other'))
            run(str(binary_dir / 'other'))
            different = json.loads((bundle('different') / 'providers.json').read_text())
            self.assertEqual(providers[0]['repository'], different['repository'])
            self.assertEqual(archive, (bundle('different') / 'libmodules.a').resolve())
            self.assertEqual(before, {path: path.stat().st_mtime_ns for path in tracked})

            # Cleaning must invalidate the shared module cache, while preserving
            # configured manifests and the inode used for interprocess locking.
            library_directory = pathlib.Path(providers[0]['repository']).parent
            lock_inode = (library_directory / 'build.lock').stat().st_ino
            manifest = (library_directory / 'root').read_text()
            output = run(*build, '--clean-first', '--target', 'app')
            self.assertEqual(output.count('BUILT module lib/foo.cc'), 1, output)
            self.assertEqual(output.count('BUILT module lib/detail.cc'), 1, output)
            self.assertEqual((library_directory / 'build.lock').stat().st_ino, lock_inode)
            self.assertEqual((library_directory / 'root').read_text(), manifest)
            run(str(binary_dir / 'app'))
            self.assertNotIn('BUILT module ', run(*build, '--target', 'app'))

            time.sleep(0.02)
            header.write_text('#pragma once\n#define VALUE 43\n')
            output = run(*build, '--target', 'app')
            self.assertEqual(output.count('BUILT module lib/foo.cc'), 1, output)
            self.assertEqual(output.count('BUILT module lib/detail.cc'), 1, output)
            self.assertGreater(archive.stat().st_mtime_ns, before[archive])
            self.assertEqual(subprocess.run([str(binary_dir / 'app')]).returncode, 1)

    def test_native_consumer(self) -> None:
        """Exercise on-demand modules with native Make/Ninja compilation and depfiles."""
        generators = ['Unix Makefiles']
        if shutil.which('ninja'):
            generators.extend(['Ninja', 'Ninja Multi-Config'])
        for generator in generators:
            with self.subTest(generator=generator):
                self.native_consumer(generator)

    def test_ninja_jobserver(self) -> None:
        """Automatically join Ninja's own pool on both generators."""
        ninja = os.environ.get('BT_TEST_NINJA', shutil.which('ninja'))
        if not ninja or '--jobserver-pool' not in subprocess.run(
                [ninja, '--help'], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True).stdout:
            self.skipTest('requires Ninja with --jobserver-pool (or BT_TEST_NINJA)')
        for generator in ('Ninja', 'Ninja Multi-Config'):
            with self.subTest(generator=generator):
                self.native_consumer(generator, ninja)

    def native_consumer(self, generator: str, pool_ninja: str | None = None) -> None:
        """Build with generator; pool_ninja optionally selects a Ninja jobserver binary."""
        adapter = pathlib.Path(__file__).resolve().parents[1] / 'cmake/Buildtool.cmake'
        with tempfile.TemporaryDirectory(prefix='buildtool native-') as temporary:
            root = pathlib.Path(temporary)
            configuration = 'BridgeTest'
            multi_config = generator == 'Ninja Multi-Config'
            executable = root / 'build' / (configuration if multi_config else '') / 'app'

            def run(*args: str) -> str:
                """Run args in root and report compiler output on failure."""
                if pool_ninja and args[:2] == ('cmake', '--build'):
                    args = (*args, '--', '--jobserver-pool')
                result = subprocess.run(args, cwd=root, text=True, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, timeout=120,
                                        env={key: value for key, value in os.environ.items()
                                             if key != 'MAKEFLAGS'})
                self.assertEqual(result.returncode, 0, result.stdout)
                return result.stdout

            (root / 'library/lib').mkdir(parents=True)
            (root / 'dependency/dep').mkdir(parents=True)
            (root / 'include dir').mkdir()
            # The common working directory is not a registered source root.
            (root / 'lib').mkdir()
            (root / 'lib/answer.cc').write_text('unregistered module must not be selected\n')
            (root / 'CMakeLists.txt').write_text(f'''
cmake_minimum_required(VERSION 3.30)
project(native_consumer CXX)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)
include("{adapter}")
add_library(external STATIC factor.cc)
target_compile_definitions(external PUBLIC FACTOR=2)
target_include_directories(external SYSTEM PUBLIC "${{CMAKE_CURRENT_SOURCE_DIR}}/include dir")
buildtool_register_project(dependency SOURCE_ROOT dependency)
buildtool_register_project(library SOURCE_ROOT library)
add_library(example::library ALIAS library)
target_link_libraries(library PUBLIC dependency PRIVATE external)
target_compile_features(dependency PUBLIC cxx_std_26)
target_compile_definitions(dependency PRIVATE DEPENDENCY_PRIVATE=5)
target_compile_definitions(library PRIVATE LIBRARY_PRIVATE=7)
add_library(facade STATIC facade.cc)
set_target_properties(library PROPERTIES POSITION_INDEPENDENT_CODE ON)
target_compile_options(library PRIVATE -Wunused-function -Werror)
file(WRITE "${{CMAKE_CURRENT_BINARY_DIR}}/forced.h" "#define FORCED_VALUE 7\n")
target_compile_options(library PRIVATE -include forced.h)
# Transitive compile features may raise the standard above CXX_STANDARD.
target_compile_features(external PUBLIC cxx_std_26)
buildtool_target_modules(facade PUBLIC
  LIBRARY example::library MODULES lib.answer)
add_executable(app main.cc)
target_link_libraries(app PRIVATE facade)
set_target_properties(app PROPERTIES CXX_SCAN_FOR_MODULES OFF)
add_executable(dep_app dep_main.cc)
buildtool_target_modules(dep_app PRIVATE LIBRARY dependency MODULES dep.value)
''')
            (root / 'dep_main.cc').write_text(
                'import dep.value;\nint main() { return value() == 21 ? 0 : 1; }\n')
            (root / 'include dir/factor.h').write_text(
                'static int unused_function() { return 0; }\nint factor();\n')
            (root / 'factor.cc').write_text('int factor() { return FACTOR; }\n')
            # An invalid unrequested module proves registration does not build everything.
            (root / 'library/lib/unused.cc').write_text('this must not be compiled\n')
            (root / 'library/lib/answer.cc').write_text(
                'module;\n#include "factor.h"\nexport module lib.answer;\n'
                'import dep.value;\nstatic_assert(FACTOR == 2);\n'
                'static_assert(LIBRARY_PRIVATE == 7);\n'
                '#ifdef DEPENDENCY_PRIVATE\n#error Dependency private flags leaked\n#endif\n'
                'static_assert(BRIDGE_CONFIGURATION == 1);\n'
                'static_assert(FORCED_VALUE == 7);\nstatic_assert(__cplusplus > 202302L);\n'
                'export int answer() { return value() * factor(); }\n')
            (root / 'dependency/dep/value.cc').write_text(
                'module;\n#include "value_impl.h"\nexport module dep.value;\n'
                'import "unit.h";\n'
                'static_assert(DEPENDENCY_PRIVATE == 5);\n'
                '#ifdef LIBRARY_PRIVATE\n#error Importer private flags leaked\n#endif\n'
                'export int value() { return detail() + ANSWER; }\n')
            (root / 'dependency/dep/value_impl.h').write_text('int detail();\n')
            (root / 'dependency/dep/BUILD.py').write_text(
                'raise RuntimeError("CMake builds must not load BUILD.py")\n')
            implementation = root / 'dependency/dep/value_impl.cc'
            implementation.write_text('static_assert(DEPENDENCY_PRIVATE == 5);\nint detail() { return 1; }\n')
            unit = root / 'dependency/dep/unit.h'
            unit.write_text('#pragma once\n#ifndef ANSWER\n#define ANSWER 20\n#endif\n')
            (root / 'facade.cc').write_text('import lib.answer;\n'
                                          'int result() { return answer(); }\n')
            (root / 'main.cc').write_text(
                '#define ANSWER 100\n#include "dependency/dep/unit.h"\n'
                'static_assert(ANSWER == 100);\nimport lib.answer;\nint result();\n'
                'int main() { return answer() == 42 && result() == 42 ? 0 : 1; }\n')
            configure = ['cmake', '-G', generator, '-S', '.', '-B', 'build',
                         '-DCMAKE_CXX_COMPILER=g++', '-DCMAKE_BUILD_TYPE=BridgeTest',
                         '-DCMAKE_CXX_FLAGS_BRIDGETEST=-DBRIDGE_CONFIGURATION=1']
            if multi_config:
                configure.append('-DCMAKE_CONFIGURATION_TYPES=BridgeTest;Debug')
            if pool_ninja:
                configure.append(f'-DCMAKE_MAKE_PROGRAM={pool_ninja}')
            run(*configure)
            build = ['cmake', '--build', 'build', '--config', configuration, '-j4']
            if generator == 'Unix Makefiles':
                run(*build, '--target', 'facade_buildtool_modules_build', '--', '-n')
                self.assertFalse(list((root / 'build/buildtool/libraries').rglob('artifacts')))
            output = run(*build, '--target', 'app')
            self.assertEqual(output.count('buildtool concurrency:'), 1, output)
            self.assertIn('Building example::library modules', output)
            self.assertIn('\n       buildtool concurrency:', output)
            self.assertLess(output.index('buildtool concurrency:'), output.index('BUILT module '))
            self.assertIn('BUILT module ', output)
            if generator == 'Unix Makefiles' or pool_ninja:
                self.assertIn('buildtool concurrency: 4; requested 4;', output)
            else:
                self.assertIn('buildtool concurrency: 1; requested 1;', output)
            run(str(executable))
            # A direct consumer reuses the dependency built through the parent library.
            self.assertNotIn('BUILT module ', run(*build, '--target', 'dep_app'))
            run(str(executable.with_name('dep_app')))
            bundle = root / 'build/buildtool/facade_buildtool_modules' / configuration
            response = (bundle / 'consumer.rsp').read_text()
            self.assertNotIn('-fmodule-mapper=|', response)
            module_map = bundle / 'consumer.modmap'
            self.assertIn(str(module_map), response)
            content = module_map.read_text()
            self.assertIn('\nlib.answer ', content)
            self.assertIn('\ndep.value ', content)
            self.assertNotIn('unit.h', content)
            map_mtime = module_map.stat().st_mtime_ns
            # These objects are owned by CMake, not by buildtool's artifact tree.
            consumers = [root / f'build/CMakeFiles/{target}.dir' /
                         (configuration if multi_config else '') / f'{source}.cc.o'
                         for target, source in [('facade', 'facade'), ('app', 'main')]]
            self.assertTrue(all(path.exists() for path in consumers))
            objects = list((root / 'build').rglob('*.o'))
            before = {path: path.stat().st_mtime_ns for path in objects}
            output = run(*build)
            self.assertNotIn('BUILT module ', output)
            self.assertNotIn('buildtool concurrency:', output)
            self.assertEqual(before, {path: path.stat().st_mtime_ns for path in objects})
            self.assertEqual(map_mtime, module_map.stat().st_mtime_ns)
            time.sleep(0.02)
            unit.write_text('#pragma once\n#ifndef ANSWER\n#define ANSWER 21\n#endif\n')
            run(*build)
            self.assertTrue(all(path.stat().st_mtime_ns > before[path] for path in consumers))
            self.assertEqual(subprocess.run([str(executable)]).returncode, 1)
            # An implementation-only edit updates the archive and relinks consumers.
            time.sleep(0.02)
            implementation.write_text('int detail() { return 0; }\n')
            run(*build)
            run(str(executable))
            # Absolute external includes must invalidate a managed module too.
            time.sleep(0.02)
            (root / 'include dir/factor.h').write_text('inline int factor() { return 3; }\n')
            run(*build)
            self.assertEqual(subprocess.run([str(executable)]).returncode, 1)
            # Add an imported module after configuration: no source inventory or
            # manual CMake regeneration should be needed to find and build it.
            (root / 'dependency/dep/offset.cc').write_text(
                'export module dep.offset;\nexport int offset() { return -20; }\n')
            # Identical object basenames must both survive archive creation.
            (root / 'library/lib/offset.cc').write_text(
                'export module lib.offset;\nexport int lib_offset() { return -1; }\n')
            answer = root / 'library/lib/answer.cc'
            answer.write_text(answer.read_text().replace(
                'import dep.value;', 'import dep.value;\nimport dep.offset;\nimport lib.offset;').replace(
                    'value() * factor()', 'value() * factor() + offset() + lib_offset()'))
            run(*build)
            run(str(executable))


if __name__ == '__main__':
    unittest.main()
