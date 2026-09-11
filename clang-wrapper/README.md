# On-demand Clang modules

`buildtool-clang` embeds the Clang driver and frontend. When Clang resolves an
`import <header>;` or `import "header";`, the wrapper asks buildtool to build or
reuse that header's PCM, loads it, and resumes preprocessing. Named modules are
requested through `ModuleLoader::loadModule`. No dependency scan or import
rewriting is involved. Imported macros therefore affect subsequent `#if`s and
imports normally.

For `-fmodule-header=system`, the wrapper also marks the frontend input as a
system header. Clang's lookup mode alone does not set this diagnostic property.
Warnings in those inputs are suppressed as for ordinary system includes;
user header units retain warnings, and `-Wsystem-headers` enables system warnings.

This requires our patched Clang branch `feat/clangd-implicit-header-units`
(commit `92b7ae1e9` or a compatible revision). In particular, it uses
`ModuleLoader::loadHeaderUnit` and `PreprocessorOptions::ImplicitHeaderUnits`.
It cannot be linked against stock Clang. Keep the Clang headers, libraries,
resource directory, and driver from the same build. There is no new LLVM patch
in this change.

## Build with buildtool

From the workspace root (`/home/alex`), GCC can bootstrap the wrapper directly:

```sh
bt build deps/buildtool/clang-wrapper/main.cpp
```

This uses `BUILD.py` in this directory; CMake and an existing wrapper are not
needed. Buildtool places the executable at `build/release/bin/main` and creates
the public `bin/main` symlink, following its source-filename convention. To use
that executable for subsequent Clang builds:

```sh
export BT_CLANG_WRAPPER=/home/alex/bin/main
```

The recipe defaults to this workspace's patched Clang build and downloaded
LLVM package. Override `BT_CLANG_BUILD`, `BT_CLANG_SOURCE`, and `BT_LLVM_CONFIG`
for different locations. The Clang library list matches our patched Clang 23;
`llvm-config` supplies LLVM's compiler flags, component libraries, and system
dependencies. Static archives are linked as a group to resolve their mutual
references.

In the current Nix environment, LLVM's reported `-lxml2` is not on GCC's library
search path. The verified bootstrap command supplies the installed static
archive explicitly:

```sh
export BT_LLVM_SYSTEM_LIBS='-lrt -ldl -lm -lz /usr/lib/x86_64-linux-gnu/libzstd.a /usr/lib/x86_64-linux-gnu/libxml2.a'
bt build deps/buildtool/clang-wrapper/main.cpp
```

Buildtool caches the evaluated `BUILD.py` flags. After changing these environment
variables or rebuilding LLVM's libraries, remove
`build/release/deps/buildtool/clang-wrapper/` before rebuilding the wrapper to
refresh its configuration and artifacts. An unchanged build is a no-op.

## Build with CMake

From the buildtool repository:

```sh
cmake -S clang-wrapper -B build/clang-wrapper -G Ninja \
  -DClang_DIR=/path/to/patched-clang-build/lib/cmake/clang \
  -DCMAKE_CXX_COMPILER=/path/to/matching/clang++ \
  -DCMAKE_BUILD_TYPE=Release
cmake --build build/clang-wrapper
```

`ClangConfig.cmake` locates the matching LLVM package. For this workspace,
`Clang_DIR` is `/home/alex/llvm-project/build-header-imports/lib/cmake/clang`
and the matching compiler is
`/home/alex/Downloads/LLVM-23.1.0-Linux-X64/bin/clang++`.

The local Nix environment currently mixes runtime versions when its linker is
used with that downloaded compiler. This build was verified with these extra
CMake settings to use the host linker and static compression libraries:

```sh
cmake -S clang-wrapper -B build/clang-wrapper \
  -DCMAKE_EXE_LINKER_FLAGS=--ld-path=/usr/bin/ld.lld \
  -DZLIB_LIBRARY_RELEASE=/usr/lib/x86_64-linux-gnu/libz.a \
  -DZLIB_INCLUDE_DIR=/usr/include \
  -Dzstd_LIBRARY=/usr/lib/x86_64-linux-gnu/libzstd.a \
  -Dzstd_INCLUDE_DIR=/usr/include
cmake --build build/clang-wrapper
```

## Use with buildtool

Put the patched compiler first on `PATH`, and select the wrapper explicitly:

```sh
export PATH=/home/alex/llvm-project/build-header-imports/bin:$PATH
export BT_CLANG_WRAPPER=/home/alex/deps/buildtool/build/clang-wrapper/buildtool-clang
bt build --clang cmd/hello.cc
```

Or pass `CLANG_WRAPPER="/path/to/buildtool-clang"`, `USECLANG=True`, and
`CXX="/path/to/patched/clang++"` to `BuildConfig`. Without `BT_CLANG_WRAPPER`,
the CLI retains the existing Clang backend. GCC does not use the wrapper.
Linking still uses `cfg.CXX`; only compilation is wrapped.

Each header unit gets its own depfile and `.info`. Header-unit dependencies
are recorded by PCM hash; textual includes, including absolute paths, are
recorded by timestamp. Header search classification (user/system) survives
metadata reloads. Switching the wrapper setting changes the stored compiler
command and triggers recompilation. Existing buildtool caches still identify
one source/header by filename per `BuildConfig`; different per-importer flag
variants of the same header are not independently cached.

The driver API performs toolchain discovery using the compiler's path and
arguments. It does **not** execute a compiler shell script, so flags normally
injected by an external wrapper (for example Nix) must be supplied explicitly
through the build configuration.

## Discovering module interfaces during ordinary compilation

The wrapper compiles C++ source files in ordinary C++ mode. It attaches a
conditional reduced-BMI writer alongside object generation, using the parsed
AST to distinguish interfaces and partitions from ordinary translation units
and primary implementation units. No source-text scan or additional Clang patch
is required.

Interfaces and partitions produce both an object and a source-scoped PCM (for
example, `build/release+clang/pkg/math.o` and `pkg/math.pcm` beneath that same
configuration directory). Ordinary sources and primary implementation units
produce only objects. Header-unit, explicit precompile, and PCM-to-object actions
retain their existing execution paths.

Recursive target discovery and import requests share the same source job and
output paths, even if the job has already started. Successful `.info` metadata
records the exported module name. Imports verify that name before using a PCM,
so a leftover PCM cannot satisfy an import after the source stops exporting the
module. Metadata from older wrappers is rebuilt once to acquire this information.

## Protocol

The executable accepts one compile job:

```text
buildtool-clang --mapper-fds READ WRITE -- /path/to/clang++ <compile arguments>
```

`WRITE` sends one JSON request per line; `READ` receives one JSON reply per
line. These are dedicated inherited descriptors, separate from stdout/stderr:

```json
{"kind":"header","path":"/absolute/path/header.h","system":false}
{"pcm":"/absolute/path/header.h.pcm"}
```

```json
{"kind":"module","name":"math:detail"}
{"pcm":"/absolute/path/math-detail.pcm"}
```

Replies may also supply a transitive module mapping:

```json
{"pcm":"/cache/std.compat.pcm","modules":{"std":"/cache/std.pcm","std.compat":"/cache/std.compat.pcm"}}
```

The wrapper registers those paths before loading the requested PCM. Clang's
AST reader can load transitive dependencies without calling the module-loader
callback again, so returning only the outer module's path is insufficient.

For an interface or partition discovered while compiling a source, the wrapper
announces the export before serializing it:

```json
{"kind":"export","name":"pkg.math","path":"build/release+clang/pkg/math.pcm"}
```

Buildtool validates the source identity and output path and acknowledges with
`{"pcm":"/absolute/output/path.pcm"}`. Unlike an import reply, this acknowledges
an output that is about to be written; consumers still wait for the producer job
to finish successfully. A rejected export fails compilation.

The alternative reply is `{"error":"reason"}`. For import requests, a returned PCM must already be
complete and compatible with the importing invocation. Requests are synchronous;
buildtool starts a nested wrapper when that dependency itself imports modules.
Each child has separate pipes. Cycles fail explicitly; resolver exceptions,
compiler failures, and disconnects fail compilation without writing success
metadata. Python cleans up waiting children on failure.

The wrapper currently initializes the native target and accepts exactly one
Clang frontend job. Linking, offloading, and multiple input files remain the
responsibility of the caller. There is no FUSE or custom VFS requirement; the
frontend initializes Clang's ordinary VFS, including requested overlays.

Standard-module builds use one invocation to precompile the interface and a
second invocation with `-x pcm` to produce its object. Both run through the same
wrapper frontend. Build against the current patched Clang headers: its
`loadModule` override includes the `SourceRange ModuleNameRange` argument used
by the module-import diagnostic fix.

## Tests

The normal Python suite includes protocol, request validation, command, and
depfile tests without needing Clang. Enable real compiler tests with:

```sh
BT_TEST_CLANG_WRAPPER="$PWD/build/clang-wrapper/buildtool-clang" \
BT_TEST_CLANG=/home/alex/llvm-project/build-header-imports/bin/clang++ \
BT_TEST_CLANG_LDFLAGS=--ld-path=/usr/bin/ld.lld \
python3 -m unittest discover -s tests -v
```

`BT_TEST_CLANG_LDFLAGS` is optional; the setting above avoids the local Nix
linker/runtime mismatch for test executables. The integration tests cover
angle/quoted imports, imported macros controlling further imports, nested
header units, named modules sharing a header unit, paths with spaces, system
header lookup, no-op rebuilds, transitive textual-header edits, new imports,
cycles, and failed compilation recovery.

The workspace's actual `cmd/hello.cc` was also compiled through the wrapper,
including its Pinocchio `rnea.hpp` header unit, `lib/hdr1.h`, and `mod1`. That
smoke test used a temporary output directory, an explicit Pinocchio include
directory (its installed pkg-config entry has a duplicated prefix), and
`-Wno-enum-enum-conversion` for Pinocchio 3.8's mixed-enum operations under
C++26. These application-specific settings were not added to buildtool.
