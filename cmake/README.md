# CMake module bridge (GCC)

Use `buildtool_target_modules` to request modules for an ordinary CMake target.
Buildtool builds the requested interfaces, imported modules, header units, and
discovered implementation companions. CMake/Make/Ninja compiles the consumer's
sources and links its executable or library.

```cmake
include(/path/to/buildtool/cmake/Buildtool.cmake)

# In the supplying project's CMakeLists.txt: register its source root.
buildtool_register_project(baselib SOURCE_ROOT "${CMAKE_CURRENT_SOURCE_DIR}")
add_library(baselib::baselib ALIAS baselib)
target_compile_features(baselib PUBLIC cxx_std_23)
target_link_libraries(baselib PUBLIC Boost::headers Threads::Threads)

# In the consumer: main.cc may write `import lib.async;`.
add_executable(myproject main.cc)
buildtool_target_modules(myproject
  PRIVATE
  LIBRARY baselib::baselib
  MODULES lib.async)
```

No module inventory is required. Import names resolve through buildtool's normal
filename conventions in the registered source roots. Link a registered project
to other registered projects with ordinary `target_link_libraries(... PUBLIC|PRIVATE ...)`
to add their roots and conventional CMake dependencies. No unused module is built.
Register projects under new target names if a conventional library already exists.

## Building source files with buildtool

For an application, use:

```cmake
buildtool_add_executable(app
  LIBRARY baselib::baselib
  SOURCES main.cc helpers.cpp
  PRIVATE_LIBRARIES protobuf::libprotobuf
  DEPENDS generate_protocol)
```

Buildtool compiles the application sources, discovers imports and header units,
and builds their dependencies. CMake links the resulting archive into the executable.
`PRIVATE_LIBRARIES` supplies additional application compile and link requirements;
`DEPENDS` orders targets that generate inputs before compilation. Both are optional.
Source paths are relative to the calling directory. The helper registers that
directory for application compilation settings, inheriting `LIBRARY`'s public
requirements and build-wide flags without changing the library's own settings.
It hides the intermediate archive and empty CMake linking source. No module list
or consumer mapper is needed. It has the same native GCC/POSIX restrictions as
`buildtool_add_library` below. Compile settings added later to the executable
target affect only CMake's linking source; configure source requirements through
`LIBRARY` or the dependencies in `PRIVATE_LIBRARIES`.

Use `buildtool_add_library` when buildtool should compile the entry sources too:

```cmake
buildtool_add_library(baselib_test_main
  LIBRARY baselib::baselib
  SOURCES lib/testing/testmain.cc)
add_library(baselib::test_main ALIAS baselib_test_main)

add_executable(tests tests.cc)
target_link_libraries(tests PRIVATE baselib::test_main)
```

`SOURCES` accepts one or more `.cc`/`.cpp` paths relative to the calling source
directory, or absolute paths. Sources must belong to the registered library or
one of its registered dependencies. Buildtool compiles them, discovers named
module imports, builds explicit header units, and includes discovered companion
implementations in the archive. There is no manually maintained `MODULES` list.
Companion sources already listed as compilations in CMake's file API are left
to their native CMake targets, even inside a registered project's source tree
(for example, FetchContent dependencies under `build/_deps`). Their headers
still participate in dependency tracking. Link the corresponding CMake target
to supply its compiled code; this detection does not add link dependencies.

The result is an imported static-library target, built only when requested or
linked by another target. Configure compilation on the registered `LIBRARY`;
the new archive uses its settings, locks, shared artifact cache and jobserver.
Use `target_link_libraries(archive INTERFACE ...)` for additional final-link
dependencies. Make, Ninja and Ninja Multi-Config are supported, with the same
native GCC requirement as the module bridge. Generated entry sources require
an explicit dependency on their generator via `add_dependencies(archive_build ...)`.
Discovered `.c` companions use their registered owner's C compiler settings.
Enable C with `project(... LANGUAGES C CXX)` before registering that library.
Assembly companions still require a conventional CMake dependency.

By default, the archive propagates the registered library's public usage requirements, but
does not propagate a compiler response file or module map. An ordinary CMake
consumer that itself imports modules still needs `buildtool_target_modules`.
Alternatively, compile those consumer sources through this helper as well and
let CMake link the archive into an executable with a conventional entry point.
This helper creates archives, not executables, and does not expose a new native
CMake module provider target.

If the library's public headers contain imports, add `PUBLIC_MODULES` to
`buildtool_add_library`. This publishes a standalone mapper and state header
from the discovered module closure, with no manual module list. Ordinary CMake
consumers inherit `@consumer.rsp` and must keep native module scanning disabled.
Direct header-unit imports in these consumers still require building the
consumer sources with buildtool; the public mapper contains named modules only.

When another archive publishes its own complete closure, use
`target_link_libraries(child INTERFACE "$<LINK_ONLY:parent>")` to link the parent
archive without inheriting a second mapper. Each public map covers only the
imports discovered in that archive's sources and dependencies; it is not an
inventory of every module in the registered project.

## Library-owned compilation settings

Header units are shared across registered libraries in one CMake build configuration.
Their cache is under `buildtool/libraries/buildtool_header_units/<config>/artifacts`.
Named modules and ordinary object files remain in their owning libraries' caches.
All header imports resolve to the same CMI for a canonical header path, including
requests through symlinks.

The `buildtool_header_units` settings target uses build-wide compiler/toolchain
flags, the registered libraries' language requirements, and their include paths.
It defaults to strict C++ (`CXX_EXTENSIONS OFF`), matching baselib's dialect.
Library-specific definitions and options do not customize header units. Set any
required common header macros explicitly with
`target_compile_definitions(buildtool_header_units PRIVATE ...)`. A header that
needs conflicting macro configurations should remain textual; this cache does
not create consumer-specific variants. Changing the common configuration
invalidates units through the normal compiler-command and dependency checks.

Each header has its own lock file. A process retains its acquired leases until
its compilation session ends, preventing replacement while a consumer reads a CMI.
Unrelated headers can build concurrently. If a required lease is held elsewhere,
the bridge cancels the entire attempt, stops its compiler processes, releases all
header and library locks, waits for the contended lease, and retries with fresh
metadata. This conservative retry avoids nested-lock deadlocks at the cost of
possibly repeating unfinished compilations. The jobserver and memory budget still
limit compiler jobs; lock waits do not reserve compiler tokens. Completed CMIs
are published by atomic rename, and failed attempts discard their temporary files.

A registered project is an object-library target with an empty C++ settings source
and, when C is enabled, an empty C settings source.
This lets CMake evaluate its compiler settings through the file API, including
toolchain flags, build-wide optimization/debug flags, transitive dependencies,
and target properties such as PIC. Buildtool compiles the real module sources;
do not list them with `target_sources` on the settings target.

C sources use `CMAKE_C_COMPILER` and their owner's evaluated C flags, including
`CMAKE_C_FLAGS_<CONFIG>`, `C_STANDARD`, PIC, include paths, and language-specific
definitions/options. C++ module flags are not passed to C compilations. Use
`$<COMPILE_LANGUAGE:C>` / `$<COMPILE_LANGUAGE:CXX>` to restrict language-specific
requirements as with an ordinary mixed-language CMake target. C depfiles track
both relative and absolute headers for incremental rebuilding.

Use normal CMake scopes on this target. `PRIVATE` settings affect the library's
own compilation, `PUBLIC` settings also reach consumers, and `INTERFACE` settings
affect only consumers. A dependency's module sources use that dependency's own
settings, even when discovered while compiling another registered library.
Consumers' private definitions, include paths, warning and optimization options
do not affect library compilation or create additional library variants.

Registration publishes `cxx_std_20`, the minimum needed for module imports.
Libraries can publish a higher public minimum with `target_compile_features`,
as baselib does with `cxx_std_23`. The consumer helper does not overwrite
`CXX_STANDARD`, `CXX_STANDARD_REQUIRED`, or `CXX_EXTENSIONS`. Compiler-specific
BMI compatibility rules still apply; a public minimum does not promise that all
higher language dialects or ABI-changing options can consume the same BMI.

Build-wide settings such as `CMAKE_CXX_FLAGS_RELEASE` still apply. For example,
configuring with `-DCMAKE_BUILD_TYPE=Release` builds an optimized library even
if one consumer adds `target_compile_options(app PRIVATE -O0)`. To change the
library's settings, configure the registered library target itself. Set PIC
on it when its objects will be linked into a shared library.

## Native CMake modules (experimental)

With Ninja or Ninja Multi-Config, native integration is selected automatically
when the consumer's `CXX_SCAN_FOR_MODULES` property is enabled or it already has
a `CXX_MODULES` file set. This includes scanning enabled through
`CMAKE_CXX_SCAN_FOR_MODULES` before creating the target. Otherwise, the helper
defaults to standalone mode. Add `NATIVE_MODULES` to explicitly enable native
integration, for example when importing modules from another CMake target without
enabling scanning beforehand.

Detection uses the consumer's settings when `buildtool_target_modules` is called;
declare its module file sets and scanning settings first. A module file set selects
native integration even with scanning explicitly disabled, because CMake always
scans module file sets. Native integration selected by any of these mechanisms
requires a Ninja generator; unsupported generators produce a configuration error.

In native mode CMake generates the consumer's module maps. The same consumer can then import modules from buildtool
and ordinary CMake targets, and can define its own modules in `CXX_MODULES` file
sets. Buildtool still compiles only the requested modules and their dependencies:

```cmake
add_library(native STATIC)
target_sources(native PUBLIC FILE_SET CXX_MODULES FILES greeting.cc)
buildtool_target_modules(native
  PUBLIC
  LIBRARY baselib::baselib
  MODULES lib.fmt)

add_executable(app main.cc)
target_link_libraries(app PRIVATE native)
```

Here `greeting.cc` may define a named module and import `lib.fmt`; `main.cc` may
import either. Native consumers must have `CXX_SCAN_FOR_MODULES ON`; the helper
sets this on its consumer, and CMake 3.30's default scanning policy also enables
it on C++20+ downstream targets. An explicitly disabled downstream target is
rejected. The existing one-bundle restriction still applies.
Buildtool modules cannot import native CMake modules in this mode.

The bridge writes a `CXXModules.json` containing module locations, references,
and transitive named imports. Before scans and collation, its build job adds the
provider directory to `linked-target-dirs` in each inheriting target's generated
`CXXDependInfo.json`. CMake's own collator then creates `.modmap` and Ninja dyndep
files. No compiler launcher or second `-fmodule-mapper` is used. The generated
`state.h` covers provider metadata as well as BMI contents, forcing scans and
collation when the providers change in the same build invocation.

This deliberately uses **CMake's internal Ninja metadata**, not a public extension
API. The bridge validates the input structure, preserves existing native provider
directories, and restores the added entries on each build after CMake regeneration.
It does not rewrite `build.ninja` or `.modmap` files. Changes to CMake internals may
require adapting this integration. Regression tests exercise both Ninja generators,
including first builds, no-op builds, source/header edits, provider additions and
removals, regeneration, and clean rebuilds.

Header units inside buildtool's module closure work: native mode records absolute
dependency paths in GCC BMIs so they remain loadable with CMake's mapper root.
Explicit `import <header>` or `import "header"` in native CMake sources remains
outside this mode's support because CMake does not support header-unit scanning
and collation. Use named imports at the CMake boundary. Artifact files stay tied
to this build tree; relocation and installation are unsupported.

See `examples/baselib-hello` for a baselib consumer; enable its `NATIVE_MODULES`
option to use native integration explicitly.

## Standalone mapper mode

When native integration is neither detected nor explicitly requested, the helper
uses standalone mode. Call it once per consumer, listing all requested modules. `PUBLIC` also
propagates module access to downstream CMake consumers. Keep those consumers'
compiler, language standard and ABI settings compatible. Compiling module
interfaces in the consumer itself is outside this API: its sources are ordinary
translation units, and native CMake module scanning is disabled on that target.
For downstream targets that inherit a `PUBLIC` bundle without calling the helper,
set `CXX_SCAN_FOR_MODULES OFF` too (or set `CMAKE_CXX_SCAN_FOR_MODULES OFF` before
creating those targets). Otherwise Ninja's native CMake scanner installs a second
mapper that overrides the bridge. This CMake property is not a transitive usage
requirement and cannot be forwarded with interface compiler options.

The bridge reads CMake's generated [file API codemodel](https://cmake.org/cmake/help/latest/manual/cmake-file-api.7.html)
to obtain each registered library's effective compiler options, definitions, include directories
(including system classification), PIC setting, and language standard. This also
handles custom build configurations and standards raised by transitive compile
features. External library
dependencies build before the module job, so their generated headers are available.
Directory `BUILD.py` files and the user's buildtool configuration are not loaded;
express required flags and external dependencies as CMake usage requirements.
Companion implementation sources are discovered through project headers using
buildtool's normal conventions; unrelated implementation files are not scanned.

Consumer metadata lives under `buildtool/<consumer>_buildtool_modules/<configuration>`
in its CMake binary directory. The custom target runs incremental checks each build
and creates `providers.json`, `consumer.rsp`, and a `libmodules.a` symlink to a shared
archive. A small read-only GCC
module map, `consumer.modmap`, lists the named modules and their BMI paths.
`consumer.rsp` passes `-fmodule-mapper=<path>/consumer.modmap` to the compiler;
no mapper process is launched. Header units used internally by these modules load
through paths recorded in their BMIs. Header entries are deliberately omitted from
the map: GCC would otherwise translate matching textual `#include` directives into
imports, potentially changing macro behavior.

Consumer sources import named modules, for example `import lib.fmt;`.
Direct `import <header>;` or `import "header";` in consumer sources is not
supported. This boundary also applies to `NATIVE_MODULES`, which uses CMake's
own named-module maps.

A generated `state.h`, containing only a pragma and artifact fingerprint comment,
is force-included in consumers. Their normal compiler depfiles therefore trigger
recompilation when any provided BMI changes. Unchanged artifacts keep their
timestamps, so a no-op build neither recompiles nor relinks. Implementation-only
changes update the archive and trigger linking. GCC-specific module depfile rules
are disabled because this fingerprint supplies the dependency on the bundle.

Compiled objects and BMIs live under the top-level CMake build directory's
`buildtool/libraries/<library>/<configuration>/artifacts`. All consumers reuse
the library's compilation, including consumers in different CMake subdirectories
or with different private settings. Native and standalone mapping modes share
the same artifacts. Compiler identity and library flag changes are handled by
buildtool's ordinary incremental invalidation; no consumer-configuration hash is
needed. Different build configurations have separate artifact directories.

`cmake --build build --target clean` removes the registered libraries' compiled
artifact and archive directories, including BMIs, header units, objects, and
incremental `.info` files. `cmake --build build --clean-first -j4` therefore
recompiles the requested modules and their dependencies. Generated configuration
manifests and lock files remain in place, so the build can run without repeating
configuration and lock identities stay stable. This uses CMake's
`ADDITIONAL_CLEAN_FILES` support for Make and Ninja generators.

Overlapping requests share their common dependencies. Consumers needing the same object set and archiving
tools share one archive; different object sets get separate archives built from the
shared objects under the library's `archives` directory. Each consumer retains its own module map and change-tracking header.
For example, two static libraries can each privately request `lib.foo` and link
into one executable while compiling `lib.foo` only once.

Before reading cached build state, each bridge invocation locks the requested
library and all its registered dependencies in canonical
path order, holding those locks through compilation, archive creation, and publication.
Locks live at `buildtool/libraries/<library>/<configuration>/build.lock`. The consistent
order prevents deadlocks between overlapping requests. A waiting invocation then
rechecks and reuses completed artifacts. This conservatively locks registered
dependencies even when the requested modules do not use them.

Unrelated libraries can build concurrently, as can ordinary CMake compilation.
Native CMake metadata publication additionally takes a short-lived
`buildtool/metadata.lock`; no library locks are acquired while holding that lock.
Separate colcon package build trees do not share locks or compiled artifacts. Lock files remain on
disk; the OS releases their locks when a process closes them or exits.
Combining multiple public bundles' compiler usage requirements
in one consumer remains unsupported and is rejected by CMake's compatible-interface
check; request that consumer's required modules together.

## Shared compiler concurrency

The custom build step is described as `Building <library> modules` in CMake's
normal progress output. When compilation is needed, buildtool prints its indented
concurrency budget, followed by numbered compilation lines:

```text
[ 50%] Building baselib::baselib modules
       buildtool concurrency: 8; requested 8; 79 GB available; 2 GB/job estimate
       [0/1] Building module lib/fmt/fmt.cc
       [3/8] Building c++ lib/fmt/fmt_impl.cc
```

These counters mean successful compilations completed / compilations known to
need rebuilding in this invocation. Cache hits and link commands are excluded;
newly discovered imports can increase the total. A compilation may already have
finished when its buffered description is displayed. Descriptions and diagnostics
retain their logical job order, while the counters reflect work completed at the
time of display. They do not change CMake's outer percentage or Ninja's step count.
Progress descriptions are green on a terminal, respecting `NO_COLOR` and
`TERM=dumb`. Ninja retains its normal buffering of custom-command output.
Standalone `bt` commands retain their existing `BUILDING` messages.

The concurrency line appears once per bridge invocation that compiles sources,
including single-worker Ninja builds. Up-to-date invocations omit it. The reported
concurrency is a ceiling; shared tokens and memory pressure can reduce active work.

The bridge automatically joins any jobserver advertised through `MAKEFLAGS`,
regardless of the CMake generator. With Unix Makefiles,
`cmake --build build -j8` therefore shares eight execution slots
among Make recipes and buildtool compiler jobs, rather than allowing eight jobs
per bridge invocation. The custom target is marked `JOB_SERVER_AWARE` so Make
also passes the descriptors needed by its pipe transport.

The bridge uses its recipe's implicit slot for one worker and borrows one token
for each additional active worker. Tokens are returned when compilers finish,
fail, are cancelled, or wait for imported modules. Importers reacquire a slot
before resuming. Local CPU and available-memory limits can further reduce the
number of workers. The displayed concurrency is a local ceiling, not a count
of currently available tokens. The `requested` count comes from the jobserver's
advertised `-j` setting and also caps the local worker limit. If the jobserver
does not advertise a numeric count, the line shows `requested unknown`.
Make dry-run, touch, and question modes do not execute module builds despite the
jobserver-aware recipe's `+` prefix. Interrupt/termination cleanup stops active
compiler processes before returning borrowed tokens.

FIFO jobservers are supported on POSIX. Anonymous-pipe jobservers are supported
on Linux by reopening the read descriptor through `/proc/self/fd`, giving this
client nonblocking reads without changing Make's descriptor flags. An inaccessible
or unsupported jobserver produces a warning and falls back to one worker.
No advertised jobserver (including Make `-j1`) also uses one worker.

To share Ninja's budget, use a Ninja binary supporting `--jobserver-pool`
(tested with upstream `1.14.0.git`) and enable the pool at build:

```sh
cmake -G Ninja -S . -B build \
  -DCMAKE_MAKE_PROGRAM=/path/to/new/ninja
cmake --build build -j8 -- --jobserver-pool
```

Ninja creates the FIFO pool and publishes its limit through `MAKEFLAGS`;
ordinary Ninja commands and nested buildtool compilers share those eight slots.
`-j1` creates no pool and the bridge remains serial. Without `-j`, the pool uses
Ninja's default concurrency. With this flag, unlike client-only Ninja operation,
an explicit `-j8` is supported. No separate jobserver launcher is required.

No bridge opt-in or Ninja version check is needed. The bridge trusts the
advertised jobserver; the invoking build environment coordinates participation.
Without an advertised pool, it falls back to one worker.
The bridge does not treat `CMAKE_BUILD_PARALLEL_LEVEL` as a separate per-library
allowance, which would multiply the user's requested parallelism.

See the [GNU Make jobserver protocol](https://www.gnu.org/software/make/manual/html_node/Job-Slots.html)
and [CMake's JOB_SERVER_AWARE option](https://cmake.org/cmake/help/latest/command/add_custom_target.html).

Requires native GCC on POSIX, CMake 3.30+, and Python 3.10+. Tested with Make,
Ninja, and Ninja Multi-Config. Clang, cross-compilation, installation/export of
module bundles, compiler launchers for module jobs, and arbitrary per-source
flags on module sources are not implemented. PCH on registered libraries, assembly
companions, and library IPO/LTO are rejected. Common file options such as `-include` are
resolved against CMake's compiler working directory before building modules;
compiler response files on registered libraries are rejected. Each source root
must have one registered owner. Manifest fields cannot
contain literal newlines or semicolons. Source/build paths may contain spaces.
The real ROS workspace has not been switched to this API.

The native-consumer regression runs with Make and both Ninja generators, using
static maps. It covers transitive
registered projects, conventional CMake dependencies, PIC/definitions/include
flags, header units and textual include semantics, companion source discovery,
no-op builds, interface and implementation edits, and adding a new imported module
after configuration, custom configuration flags, transitive standard requirements,
relative forced includes, unregistered source-root shadowing, and duplicate object
basenames in the archive. A configure-only regression rejects conflicting module
bundles. These real-compiler tests require GCC 14+ and CMake 3.30+.
Sharing regressions cover targets in separate subdirectories linking into one
executable, reuse of the same archive despite private consumer flags, build-wide
optimization, header edits, no-op builds, and private library/dependency settings,
in both standalone and native modes.
`test_cmake_locks.py` uses separate processes and controlled compilation/publication
callbacks to check shared dependency locking, cache rechecks after waiting,
concurrent unrelated builds, failure cleanup, and serialized metadata updates.
`test_jobserver.py` checks exact token return, dependency suspension, cancellation,
and two real Make recipes sharing FIFO/pipe jobservers under `-j3` and `-j1`.
Set `BT_TEST_NINJA=/path/to/new/ninja` to also exercise Ninja's pool: nested
workers and ordinary commands must stay within the shared `-j1`, `-j3`, and
`-j8` budgets. The CMake bridge tests exercise both Ninja generators with it.
A separate smoke build of baselib's actual `lib.async` with
GCC 15.3 and Ninja compiled and ran a consumer calling `lib::async::go`.

Run the regression test with:

```sh
python3 -m unittest discover -s tests -p test_cmake_bridge.py -v
python3 -m unittest discover -s tests -p test_cmake_native_modules.py -v
```
