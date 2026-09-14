# Baselib CMake hello world

This example imports `lib.fmt` and calls `lib::fmt::printf("hello\n")`.
Baselib's printf function belongs to `lib::fmt`, not `lib::io`.
Buildtool builds the module dependencies; CMake compiles and links `main.cc`.
Baselib's top-level CMake file registers its source root, defines
`baselib::baselib`, and declares its dependencies by default. The example adds
the checkout with `add_subdirectory` and requests `lib.fmt` from that target.
`BASELIB_USE_MODULES=OFF` selects the older conventional source build instead.

The example uses standalone mapper files by default. Uncomment `NATIVE_MODULES`
to let CMake generate GCC's module map from buildtool's provider metadata instead;
that experimental integration requires Ninja.

Baselib publishes its public C++23 minimum through its CMake target. Its modules
use baselib's own build settings, including build-wide optimization flags; private
options on `hello` do not change the library build.

With GCC, Ninja, CMake 3.30+, Python 3.10+, and the `{fmt}` 12 development package
available, run from the buildtool directory:

```sh
cmake -G Ninja -S examples/baselib-hello -B build/baselib-hello -DCMAKE_CXX_COMPILER=g++ -DCMAKE_BUILD_TYPE=Release
cmake --build build/baselib-hello
./build/baselib-hello/hello
```

For parallel module compilation with Ninja's `--jobserver-pool` support (tested
with `1.14.0.git`), configure and build with:

```sh
cmake -G Ninja -S examples/baselib-hello -B build/baselib-hello \
  -DCMAKE_CXX_COMPILER=g++ -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_MAKE_PROGRAM=/path/to/new/ninja
cmake --build build/baselib-hello -j8 -- --jobserver-pool
```

Ninja and buildtool automatically share the eight slots. Without an advertised
jobserver, buildtool uses one worker. No bridge opt-in is needed.

The default layout has `baselib` and `buildtool` checked out alongside each other.
For another layout, add `-DBASELIB_SOURCE_ROOT=/path/to/baselib` to configuration.

Consumers using CPM can replace the `add_subdirectory` and source-root setting
with their normal `CPMAddPackage(NAME baselib GITHUB_REPOSITORY adob/baselib
GIT_TAG <revision>)`, using a revision that contains this integration. Baselib
loads its pinned buildtool dependency automatically. Set
`-DCPM_buildtool_SOURCE=/path/to/buildtool` to use a local checkout instead.
No `BASELIB_USE_MODULES` option or explicit bridge include is needed.
