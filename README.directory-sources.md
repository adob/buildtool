# Directory source discovery

`bt build path/to/directory` builds the eligible immediate sources as one target.
`bt build path/to/...` repeats this for directories below that path, sharing the
compilation graph. Test sources are selected by `bt test` instead.

A directory's `BUILD.py` can reserve sources for explicit selection:

```python
EXPLICIT_SOURCES = ["testmain.cc", "benchmain.cc", "debug_stub.cc"]
```

These exact filenames are omitted from automatic directory discovery for both
build and test. They remain available as explicit targets or dependencies; build
tags still apply. The setting applies only to that directory.

This is useful for alternative entry points or replacement implementations that
must not be linked together. Baselib's testing directory uses it to keep the test
runner, benchmark runner, and CMake-only debug stub out of ordinary library builds.

Named module lookup also accepts active `+tag` variants when no unqualified
module layout exists in that search root. For example, `import lib.serial.usbio`
can resolve `lib/serial/usbio+zephyr.cc` when the `zephyr` tag is active. More than
one matching active variant is an error; buildtool does not guess between them.
