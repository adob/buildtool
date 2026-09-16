# Explicit modules in PlatformIO applications

The adapter lets PlatformIO compile application sources while buildtool builds
explicitly requested named modules and their dependencies. Arduino framework
compilation, firmware linking, and uploading remain owned by PlatformIO.

For the local Teensy example:

```ini
lib_deps =
    baselib=symlink://../../deps/baselib
    buildtool=symlink://../../deps/buildtool
custom_buildtool_modules =
    lib.types
```

Baselib's `library.json` loads its `platformio/build.py` script, which calls
`platformio_adapter.configure(env, projenv, root)`. Other libraries can use that
same registration hook, but only one library root is currently supported per
environment. Library manifests cannot yet request their own module dependencies.
Baselib disables ordinary PlatformIO source compilation in its manifest; without
explicit module requests, none of its implementation sources are built.

Buildtool is a PlatformIO library package containing Python tooling, with C/C++
source compilation disabled. Baselib declares it as a dependency, so ordinary
consumers need not specify a buildtool directory or a direct buildtool dependency.
The example's explicit `buildtool=symlink://...` selects the local development
checkout for adapter loading. PlatformIO can still install the declared Git
dependency alongside that symlink; explicit project dependencies take precedence
in adapter discovery. Use this standard dependency syntax to select a local
checkout; no separate directory override is needed.

The default dependency currently follows buildtool's `master` branch. The new
package metadata and adapter must be published there before remote consumers can
use this integration; until then use the local symlink dependency above.

The adapter takes the GCC toolchain, definitions, include paths, optimization,
CPU/ABI options and framework tags from PlatformIO. It selects GNU C++23 for both
the application and module builds; the framework keeps its original flags.
No host BUILD.py settings or host libraries are loaded.
Dependency include paths are captured when the SCons build action runs, after
PlatformIO has propagated the declared library dependencies. Baselib declares
Boost.Core, Boost.CircularBuffer and their header dependencies at Boost 1.87.0;
its script excludes the upstream repositories' probe/test sources from firmware.

A SCons action runs buildtool's incremental checks before application objects
are compiled. It publishes an archive, a static named-module mapper, and a state
header fingerprinting the BMIs. Application objects depend on these outputs;
unchanged contents avoid recompilation. SCons may still relink the firmware after
this always-checked action. The action runs with one compiler job.
Artifacts live under `$BUILD_DIR/buildtool-modules` and are removed by clean.

Only explicit top-level application module requests are implemented. Application
sources are never compiled by buildtool. Header units inside the requested
modules are built automatically, but direct application `import <header>` is not
supported by the static named-module map. Include Arduino/system headers before
imports when mixing textual headers and modules.

Validated by building Teensy 4.0 firmware with Arduino and ARM GCC 15.2.1,
importing `lib.types` and `lib.fmt` and calling `fmt::printf("hello\n")`.
Baselib declares fmt 12.1.0 and compiles its format.cc implementation; its Arduino
output streams write to Serial, which the application must initialize.
Hardware execution has not been tested. This does not establish embedded
compatibility for every baselib module.
