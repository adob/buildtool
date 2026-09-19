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

## Generated module sources

`BUILD.py` may declare generated files owned by that directory. The initial
implementation resolves these declarations lazily for named C++ module imports;
generated textual `#include` files are not yet supported.

```python
GENERATED = [{
    "inputs": ["schema.txt", "gen.py"],
    "outputs": ["foo.cc"],
    "tools": ["protoc"],
    "command": ["python3", "gen.py", "schema.txt", "{outdir}/foo.cc"],
}]
```

Inputs and outputs are relative to the directory containing `BUILD.py`. Outputs
must remain below that directory and each logical output may have only one
producer. Commands run in the `BUILD.py` directory without a shell.
The following substitutions are available in command arguments:

- `{outdir}`: the absolute generated-output directory for this package
- `{srcdir}`: the absolute source directory containing `BUILD.py`
- `{root}`: the absolute source root

If `import pkg.foo;` cannot resolve any ordinary source layout, Buildtool checks
the `BUILD.py` owning each logical candidate. A declaration for `pkg/foo.cc`
materializes it at `build/<configuration>/generated/pkg/foo.cc`, then compiles
that physical source as the requested module. Ordinary source files always take
precedence over generated declarations.

For a missing module source, Buildtool checks the normal module lookup candidates
against generated outputs as it walks ancestor `BUILD.py` files. For example,
`import pkg.foo.client;` first considers `pkg/foo/client.cc`, then its directory
fallbacks; an ancestor `pkg/BUILD.py` may declare `foo/client.cc` as an output.

Generation is incremental. Buildtool records the expanded command, generator
executable identity, identities of optional `tools`, and hashes of declared
inputs; changing any of these, or removing an output, reruns the action. Use
`tools` for executables invoked indirectly by a generator wrapper, such as a
Python script that runs `protoc`. Multiple imports of outputs from the same
action share one generation job.
