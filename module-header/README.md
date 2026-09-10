# Module-to-header prototype

`module-to-header` uses Clang's parsed AST to extract the exported API from one
module interface. It then parses the result as an ordinary C++ header before
atomically replacing the output. Failed extraction or validation leaves an
existing output untouched. The input file is never modified.

This is an experimental extractor, available through the
`bt generate-module-headers` subcommand. Ordinary builds do not generate headers.

## Generate a directory tree

```sh
bt generate-module-headers deps/baselib/lib
```

Buildtool recursively scans `.cc` files for apparent `export module` declarations.
For this example, `lib/math/math.cc` produces
`deps/baselib/generated-headers/math/math.h`. The output directory is a sibling
of the input directory, with paths beneath that input preserved. Existing source
headers are untouched. Hidden directories, directory/file symlinks, and nested
`generated-headers` directories are skipped.

This command uses a source-text heuristic only to select candidates; Clang parses
and validates each selected file. Comments, strings, and preprocessor directive
lines do not count as module declarations, but conditional compilation is not
evaluated by discovery. The extractor can therefore reject a candidate whose
declaration is inactive for the selected flags. Ordinary compilation still uses
filename-based module lookup without source inspection.

The command uses project include/C++ flags and the source directory's `BUILD.py`
settings, and queries `clang++ -print-resource-dir`. It looks for the prebuilt
extractor at `build/module-header/module-to-header` in the buildtool repository;
build it with CMake as below if missing. Set `BT_MODULE_HEADER` to override the
executable path and `BT_MODULE_HEADER_CLANG` to select its matching Clang compiler.
Use `--verbose` before the input path to print every command at launch.

Generation runs sequentially in sorted traversal order and stops on the first
failure. Successfully generated earlier headers remain. Files are regenerated on
each invocation; stale headers for deleted sources are not removed automatically.

## Build and run

Use a matching Clang/LLVM development installation (tested with the local Clang
23 build). This tool uses standard Clang tooling APIs, not the wrapper's custom
header-unit loader.

```sh
cmake -S module-header -B build/module-header -G Ninja \
  -DClang_DIR=/path/to/clang/lib/cmake/clang \
  -DLLVM_DIR=/path/to/llvm/lib/cmake/llvm
cmake --build build/module-header -j2

build/module-header/module-to-header /path/to/math.cc -o /path/to/generated/math.h -- \
  -std=c++26 -I/path/to/baselib -resource-dir=/path/to/clang/lib/clang/23
```

Obtain the resource directory with `clang++ -print-resource-dir`. Arguments after
`--` are compiler flags for parsing the module and validating the header. A
compilation database can also be selected with `-p`. Supply the same include
paths and configuration defines as the module build. Exactly one input is allowed.

In this workspace, the patched Clang CMake package is at
`/home/alex/llvm-project/build-header-imports/lib/cmake/clang` and its LLVM SDK is
at `/home/alex/Downloads/LLVM-23.1.0-Linux-X64/lib/cmake/llvm`. The local build also
uses these settings to avoid mixing the system compiler runtime with Nix libraries:

```sh
-DCMAKE_CXX_COMPILER=/home/alex/Downloads/LLVM-23.1.0-Linux-X64/bin/clang++
-DCMAKE_EXE_LINKER_FLAGS=--ld-path=/usr/bin/ld.lld
-Dzstd_LIBRARY=/usr/lib/x86_64-linux-gnu/libzstd.a
-Dzstd_INCLUDE_DIR=/usr/include
-DZLIB_LIBRARY_RELEASE=/usr/lib/x86_64-linux-gnu/libz.a
-DZLIB_INCLUDE_DIR=/usr/include
```

## What it emits

- Active, direct `#include` directives, preserving their header spelling.
- Exported namespaces, linkage blocks, types, aliases, and using declarations.
- Ordinary function declarations, omitting their implementation bodies.
- Ordinary namespace variable declarations with `extern`, omitting initializers.
- Definitions of templates, classes, inline/`constexpr` functions and variables.
  Non-template functions with deduced return types must already be inline or
  `constexpr`; otherwise extraction reports the required source change.

For example:

```cpp
export module example;
export extern "C++" {
    int answer() { return 42; }
    constexpr int twice(int n) { return n * 2; }
}
```

produces a header containing:

```cpp
extern "C++" {
    int answer();
    constexpr int twice(int n) { return n * 2; }
}
```

Source ranges preserve template syntax and retained definitions; Clang's
declaration printer supplies ordinary function/variable declarations. Formatting
and documentation comments are not preserved consistently in this prototype.

## Boundaries

- Generating a header **does not change the module's ABI or declaration
  attachment**. Use `extern "C++"` in the module for declarations that must match
  header consumers. Native module attachment produces a diagnostic note, not a
  claim of binary compatibility.
- Imports and re-exports are rejected. Supporting them needs explicit mappings
  from imported modules/partitions to generated headers, plus suitable BMIs for
  parsing. The tool does not build dependencies automatically.
- Source-local `#define`, `#undef`, pragmas, and declarations whose boundaries
  come from macro expansion are rejected. Shared macros may come from included
  headers. Put includes in the global module fragment.
- Output reflects the active preprocessing configuration; it does not recreate
  `#if` branches for all possible configurations. Consumers must use compatible
  defines and include settings.
- Private supporting declarations are not extracted. If a retained body or type
  needs one, standalone validation usually reports the missing dependency. This
  check does not instantiate every possible template specialization or prove ODR,
  ABI, or semantic equivalence.
- Source ranges are not a complete C++ serializer; complicated grouped
  declarations and out-of-line member definitions may fail validation. They need
  dedicated handling before this is a general library publishing tool.
- Validation uses the input file's directory for quoted includes. If the generated
  header is moved, preserve those include paths (for example with `-iquote`) or
  keep generated headers alongside the corresponding sources.

## Regression tests

From the buildtool repository:

```sh
BT_TEST_MODULE_HEADER="$PWD/build/module-header/module-to-header" \
BT_TEST_MODULE_HEADER_CLANG=/path/to/matching/clang++ \
BT_TEST_CLANG_LDFLAGS=--ld-path=/usr/bin/ld.lld \
python3 -m unittest discover -s tests -p test_module_header.py -v
```

Tests compile two header consumers and link them against the module object, check
templates and `constexpr` expressions, and exercise rejection/overwrite behavior.
