# Buildtool regression tests

Run from the buildtool repository directory:

```sh
python3 -m unittest discover -s tests -v
```

The tests use the Python standard library. No GCC, Clang, pkg-config, or
third-party Python packages are required.

The optional `test_module_header.py` tests exercise the
[Clang module-to-header prototype](../module-header/README.md), including linking
header consumers to a module object. Its executable and matching compiler are
selected with `BT_TEST_MODULE_HEADER` and `BT_TEST_MODULE_HEADER_CLANG`.
`test_generate_module_headers.py` covers the `generate-module-headers` subcommand
using an injected filesystem and mock extractor, including recursive discovery,
output paths, compiler flags, and failure handling.

## GCC standard-library header units

On GCC 16 and later, a standard include such as `#include <algorithm>` can
trigger a request for `bits/stdc++.h`. Buildtool builds that aggregate header
unit on demand and returns its compiled path through the module mapper. This
also works when a user header unit includes a standard header. Projects that
never request the aggregate incur no discovery or compilation cost for it.

Use `bt build --no-std-header-unit cmd/hello.cc` to disable automatic translation
and discovery. The flag also works with `run`, `test`, and `bench` and must appear
before the first path. Python callers can use `BuildConfig(STD_HEADER_UNIT=False)`.
Switching this setting recompiles affected GCC C++ sources automatically; no
`--rebuild` is needed. Explicit header-unit imports remain supported.

`gcc_std.py` identifies the header using the selected compiler's default include
search, preserving target/sysroot flags but excluding project include paths and
include-path environment variables. A matching filename suffix alone is not
enough; the requested file must resolve to the discovered SDK file. The probe
and compilation run as dependency jobs, releasing a waiting importer's slot.
Concurrent importers share the work, including with `-j1`.

The aggregate's own includes remain textual to prevent recursive construction.
Its compiled unit and metadata are stored under the configuration's build
directory with a compiler/flags hash suffix. Its controlled configuration uses
global `CXXFLAGS` and `INCFLAGS`, plus directory options affecting the standard
library. Directory package include paths, warning flags, and ordinary project
macros do not create separate units or enter their compiler commands. Reserved
macros (names starting with `_`), `NDEBUG`, and other compiler options remain in
the configuration, so ABI, target, and language differences still select distinct
units. Both `-D` and `-U`, joined or separate, are supported. Put any unusual
nonreserved macro intended to configure the SDK in global `CXXFLAGS`; directory
project macros are intentionally local to the importing code. Forced includes,
`-imacros`, and preprocessor passthrough preserve the full directory configuration
conservatively. Importing source files retain all their own flags.

Header dependencies and content hashes participate in ordinary
incremental checks; `--rebuild` forces reconstruction.

`test_gcc_std_headers.py` covers SDK identity, symlinks, shadow headers, lazy
discovery, flag filtering, failures, shared jobs, configuration isolation, and
incremental invalidation. Run its optional real GCC 16 tests with:

```sh
BT_TEST_GCC=g++ python3 -m unittest discover -s tests -p test_gcc_std_headers.py -v
```

## GCC named standard-library modules

`import std;` and `import std.compat;` are discovered on demand using GCC's
`-print-file-name=libstdc++.modules.json`. Buildtool reads the manifest and resolves
each `source-path` relative to the manifest directory. This requires a GCC/libstdc++
installation providing that metadata (tested with GCC 16). Clang's discovery is
unchanged.

The metadata location is persisted in
`build/<configuration>/gcc-std-modules/<hash>.json`. The key includes the compiler
path and file identity, flags, working directory, and driver search environment.
Fresh builds reread the manifest, but reuse the stored location without launching
GCC. Missing metadata or sources produce an explicit error.

Named modules use the same controlled flags as the standard header unit, with
separate CMI, object, and incremental metadata artifacts. For example, the CMI
is `build/<configuration>/std.pcm.<hash>`; the object and `.info` file live under
the corresponding `SYSTEM/.../bits/std` path. Compatible importers share the
artifacts. The mapper returns the CMI and the linker receives the object;
`std.compat` also builds and links its `std` dependency.

An up-to-date build requires no compiler, discovery, or linker invocation. Editing
an importer reuses the modules. Module sources, tracked SDK headers, compiler
identity, and relevant flags invalidate cached modules as appropriate.
`--rebuild` recompiles them. `--no-std-header-unit` disables automatic header units
while leaving named standard modules available; those modules use textual
includes in that mode.

`test_gcc_std_modules.py` covers persistent discovery, configuration isolation,
dependency invalidation, and failure recovery using a fake filesystem/compiler.
Its optional real GCC test compiles and links both modules, verifies execution,
checks no-op and importer-only rebuilds, and tests both header-unit modes:

```sh
BT_TEST_GCC=g++ python3 -m unittest discover -s tests -p test_gcc_std_modules.py -v
```

## Building a directory

`bt build cmd/foo` compiles every immediate `.cc`, `.cpp`, `.c`, `.S`, and `.s`
source in that directory, in sorted order. Subdirectories are separate targets;
normal dependency discovery can still add sources outside the directory.
The toolchain's `nm` checks the compiled objects for a defined `main` function.
If present, buildtool links `build/<config>/bin/foo` and publishes `bin/foo`.
Debug builds retain the `+debug` suffix, and an explicit `OUTFILE` overrides the
directory name. Explicit file builds retain their existing filename-based names.

A directory without `main` is compiled successfully without linking or publishing
an executable. An empty source directory reports an error. `--library` continues
to link a shared library without requiring `main`. All matching source files,
including files named `*_test.cc`, are included.

`bt run cmd/foo --option` uses the same directory build but executes the artifact
directly without publishing it. A directory without `main` cannot be run.

## Named module lookup

`bt ide` refreshes the compilation database, including newly added interfaces and
partitions. Its commands omit buildtool's automatic `-fprebuilt-module-path` so
clangd can build its own BMIs with its compiler and flags. GCC CMIs and Clang BMIs
are not interchangeable even when both have a `.pcm` filename. Explicit custom
module search paths in user flags remain. Normal compiler commands are unchanged.
`test_ide_modules.py` checks this separation for both backend configurations.

For `import lib.math;`, buildtool searches each configured source/include root
in this order: `lib/math.cc`, `lib/math/module.cc`, `lib/math/math.cc`.
The first existing file wins; all three candidates in an earlier root precede
those in later roots. Lookup checks file metadata without reading source contents
or scanning directories. Other filenames are not discovered automatically.

The import identifies the selected file as a module, including for Clang's
module compilation flags. The compiler validates its declaration. Explicit
`.cc` file and directory targets do not infer module declarations from source
contents. A header companion already identified by an import reuses that module
job, including on incremental builds.

For a library offering headers and modules, `module.cc` avoids the conventional
header/companion filename collision: without an import identifying it as a module,
`math.cc` beside `math.h` is still treated as the header's implementation source.
`test_module_lookup.py` covers metadata-only lookup and optional GCC/Clang builds
for all three layouts, including imports alongside legacy headers and no-op builds.

## Parallel compilation and output

CLI builds use the CPU count as their requested concurrency. Available memory
caps this at an estimated **2 GiB per active compiler**, with a minimum of one.
The first compilation prints the selected limit, requested limit, and available
memory above the first `BUILDING` line. Displayed memory values are rounded to
whole 1024-based units labeled GB (1 GB here means 1024³ bytes).
Builds requiring no compilation do not print a concurrency message.
Set an explicit upper limit with:

```sh
bt build -j4 cmd/hello.cc
bt build --clang -j4 cmd/hello.cc
```

`bt build --rebuild cmd/hello.cc` forces recompilation of the target and every
reachable source, named module, and header unit, followed by relinking. Shared
dependencies compile once per invocation. It rebuilds the selected compiler and
configuration; unrelated targets and installed libraries are outside its scope.
It does not delete build directories or reset configuration caches. The flag
also works with `run`, `test`, and `bench`; the Python API uses
`BuildConfig(REBUILD=True)`.

`run`, `test`, and `bench` accept the same `-j` / `--jobs` option. The Python
API accepts `BuildConfig(JOBS=4)`; its default remains one active compiler.
Pass `memory=MemoryBudget()` to enable the same memory policy and startup report
in the Python API. Tests can inject `MemoryBudget(available=callable)` returning
available bytes (or `None` for unknown capacity).

Before granting another compiler slot, the scheduler checks available memory
again, reserving 2 GiB for each active compiler plus the new one. This
conservatively allows for growth in running compilers. It retries every half
second while memory blocks queued work. Running compilers are not killed.
Linux accounting uses `/proc/meminfo`'s `MemAvailable`, further constrained by
readable cgroup-v2 limits under `/sys/fs/cgroup`. If accounting is unavailable,
the requested concurrency applies. The startup cap stays fixed for that build;
later checks can temporarily reduce concurrency further.

This is a soft estimate: a compiler may need more than 2 GiB, and blocked
importers retain memory. One active compiler is always allowed so a build can
make progress, including nested module imports, even below the estimate.

`Target.compile_many(paths)` and package-level `build([paths...], cfg)` schedule
multiple roots together. `Target.compile(path)` remains a synchronous entry
point for one root and its dynamically discovered dependencies.

Compilation order and output order are independent. The reporter starts with
the target's root job and streams its output immediately. When that job
finishes successfully, its discovered jobs enter the back of the reporting
queue. Each job's complete stream is printed before the next job's stream.
Shared dependencies appear once, in breadth-first discovery order, even if
another importer happened to start building them first. Compiler stdout and
stderr share a stream; other jobs' output is buffered, spilling to temporary
files above 1 MiB per job. Warning output follows the same rules as other output.
Compiler diagnostics retain colors when the reporter writes to a terminal.
Compiler commands are printed once, immediately before their first stdout or
stderr bytes. Silent compilations retain progress messages without command
lines. Captured tool output used internally (such as pkg-config stdout) does
not cause a command line to be printed.
Use `--verbose` (`-v`) to log every command at launch, including silent commands
and internal tools, and show the `done in ...` timing lines. Each launch prints
`launching <COMMAND>` directly and flushes immediately, bypassing the ordered
output queue. These lines follow actual launch order and can appear while an
earlier job's diagnostics are streaming. Compiler diagnostics retain their
ordered output queue. The option
can appear before or after the subcommand, but before the first path; the Python API uses
`BuildConfig(VERBOSE=True)`. Verbosity does not affect incremental build metadata.
Use `--debug` (`-d`) to select debug builds independently of verbosity.
All buildtool options must precede the first source/directory argument.
For example, `bt run --debug src/foo.cc --option value` builds in debug mode and
passes `--option value` to the program. After the target, even `--verbose`,
`--debug`, and `--help` are program arguments. Commands taking multiple paths
(`ide`, `test`, and `bench`) treat subsequent arguments as paths.
Redirected output, `TERM=dumb`, and nonempty `NO_COLOR` disable automatic color
forcing; explicit compiler color flags are preserved. The automatic color flag
is applied only at execution time, so terminal detection does not affect build
metadata or trigger recompilation.

When the job being printed fails, reporting stops and remaining processes are
terminated and reaped. A failure in a later queued job does not interrupt the
current stream. If a module failure causes its importer to fail, the underlying
module diagnostic is included in the importer's stream so stopping there does
not hide the cause. Linking and the public symlink update require every job to
succeed. Keyboard interruption also cancels outstanding work.

The scheduler runs on one asyncio event loop, keeping cache mutations on one
thread. Jobs that wait for modules release their compiler slots and reacquire
them before their compiler resumes. Dependencies and resumptions have priority
over unrelated queued sources. Blocked compiler processes still occupy memory:
`-j` limits active compilation, not the total number of resident processes.
Compiler subprocesses run in separate process groups so cancellation can also
stop children launched by a compiler driver.

Output order is stable for the same root/dependency encounter order, rather
than sorted by completion time. Individual compilers can discover dependencies
in different orders. Commands include actual runtime pipe descriptors, and
elapsed times naturally vary. Directory linker flags and object inputs are
collected by deterministic traversal, independently of completion order.

For review, the implementation is divided into:

1. `scheduler.py`: shared jobs, execution slots and the streaming output queue.
2. `compiler.py` and `clang_mapper.py`: subprocess lifetime, output capture and
   mapper transport.
3. `buildtool.py`: async dependency traversal, backend integration, deterministic
   link inputs and CLI wiring.

`test_clang_wrapper.py` also has opt-in real compiler tests for the patched
Clang wrapper. See [build and test instructions](../clang-wrapper/README.md).
`test_parallel_compilers.py` exercises concurrent sources importing a shared
named module and header unit, including no-op and changed-header rebuilds.
Set `BT_TEST_GCC` to a GCC executable to enable its GCC case; its Clang case
uses the same environment variables as the wrapper tests. Optional
`BT_TEST_GCC_LDFLAGS` and `BT_TEST_CLANG_LDFLAGS` supply linker flags.

CLI builds use `build/release` or `build/debug` for GCC, and
`build/release+clang` or `build/debug+clang` for Clang.
Custom object and dependency roots receive the
same build-directory suffix, keeping object files, module files, and metadata
separate by compiler.

Real executables live under each build directory's `bin/`, for example
`build/release/bin/hello` and `build/release+clang/bin/hello`. The public `bin/hello`
is a relative symlink to the selected build. `bt build` replaces it atomically
after a successful build, including when switching back to an existing build that
requires no compilation or linking. Debug builds retain the `+debug` filename
suffix. A failed link leaves the public symlink unchanged.

`bt run` executes the real binary under `build/<config>/bin/` directly, reusing
the same incremental artifacts as `bt build`. It does not create the project's
`bin/` directory or create/update its public symlinks. An existing `bin/foo`
keeps pointing to the build selected by the last `bt build`, even when `bt run`
uses another compiler or configuration.

Compiler and linker flags preserve their declared order and repeated arguments.
Package flags are appended in `PKGCONFIG` order, and dependency traversal retains
encounter order when collecting directory linker flags. Older `buildvars.json`
caches are regenerated once to recover flags previously reordered or removed
by set-based collection.

## Injecting a filesystem

Build configurations default to `RealFileSystem`. Tests explicitly pass a
`MemoryFileSystem` to the configuration and to path methods that perform I/O:

```python
import buildtool as bt  # Or: import deps.buildtool as bt

fs = bt.MemoryFileSystem(cwd="/workspace")
cfg = bt.BuildConfig(vfs=fs)
fs.makedirs("src")
fs.write_text("src/main.cc", "int main() {}\n")

path = bt.Path("src/main.cc")
assert path.read_text(fs) == "int main() {}\n"
assert path.exists(fs)

# Run build operations with this configuration and a fake compiler.
# Between independent build invocations, retain files but clear build state:
cfg.reset_build_state()
```

The public package API exports `FileSystem`, `RealFileSystem`,
`MemoryFileSystem`, and `reset_build_state(cfg)` (equivalent to
`cfg.reset_build_state()`). `main()` also accepts `vfs=...`.

Paths contain only a path value. Joins, copies, parent traversal, and suffix
changes need no filesystem. I/O methods (`try_stat`, `mtime`, `exists`,
`read_text`, `is_dir`, and `is_file`) require an explicit `vfs` argument.
File discovery, hashing, read/write helpers, and working-directory resolution
also require one. Build operations pass `cfg.vfs`; there is no active-filesystem
global or scope to enter. The same path can be used with different filesystems.

Path equality and hashing depend only on the path value. Each `BuildConfig`
owns its source-file, compiled-module, header-dependency, directory-configuration,
and compiler-command caches. Configurations can use identical filenames and
module names with independent cached objects, even on the same filesystem.
Use separate output directories when building different configurations on disk.
`cfg.reset_build_state()` clears only that configuration's caches between
independent invocations; create new targets and sources afterward. Creating a
new configuration starts with empty caches, and discarded configurations can be
garbage-collected with their cached objects. This does not make the CLI's
remaining global configuration or process execution thread-safe.

The filesystem boundary covers file reads and writes, metadata, directory
creation and traversal, atomic file replacement, hashing, and working-directory
resolution. Path operations read current file statistics. `HeaderDep` caches
input-header timestamps for a build invocation; resetting the configuration
clears those dependencies so the next build observes header edits.
Build timestamps come from output files.
`RealFileSystem` streams file hashes to avoid copying large PCMs into memory.

The in-memory backend advances a deterministic clock on mutations; `advance()`
can simulate additional elapsed time. Its working directory is virtual and
never changes the host process's working directory. It models files and
directories and symbolic links, but not permissions.

Compiler execution is a separate boundary: subprocesses, their pipes, and
arbitrary filesystem calls inside user-written `BUILD.py` code are not
virtualized. Memory-based build tests therefore replace the compiler backend
with a fake that reads and writes through the injected filesystem.

## Coverage

- `FlagOrderTests`: GCC/Clang commands remain unchanged across Python hash seeds;
  project and package flags retain order and repetitions; dependency ordering
  survives metadata round trips; old directory caches are regenerated.
- `CompilerOutputTests`: alternating GCC/Clang CLI builds retain independent
  outputs and update the public symlink without recompilation or relinking when
  revisited, with and without verbose output; failures preserve the public entry.
- `BuildConfigCacheTests`: cache reuse, isolation between configurations on the
  same filesystem, independent resets, and collection of discarded caches.
- `BuildDecisionTests`: ordering of dependency checks, recompilation, and metadata
  updates; unchanged module interfaces avoid unnecessary compilation.
- `IncrementalModuleTests`: real build traversal, module hashes, command
  comparisons, and `.info` metadata using in-memory files and a fake compiler.
  JSON fixtures stand in for C++ source; the fake compiler bakes exported values
  into objects so stale imports are observable. Tests cover direct/transitive
  edits, no-op rebuilds, unchanged interfaces, failed-compilation recovery, and
  independent builds using identical module names in two filesystems.
  A guard test rejects accidental host filesystem access or compiler execution.
- `test_vfs.py`: the same filesystem contract tests run against both backends;
  additional tests cover virtual cwd isolation, real symlink discovery, file
  mutations, explicit filesystem arguments, path reuse across filesystems,
  configuration cache isolation, and source
  discovery/compilation-database generation without host file access.

Subprocess transport tests launch tiny Python programs without a C++ compiler.
Incremental VFS build tests need no disk writes, sleeps, filesystem mocks, or
clock patches. Real filesystem, subprocess and opt-in compiler tests use
temporary storage.
