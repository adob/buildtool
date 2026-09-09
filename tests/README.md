# Buildtool regression tests

Run from the buildtool repository directory:

```sh
python3 -m unittest discover -s tests -v
```

The tests use the Python standard library. No GCC, Clang, pkg-config, or
third-party Python packages are required.

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
directories, not permissions or symlinks.

Compiler execution is a separate boundary: subprocesses, their pipes, and
arbitrary filesystem calls inside user-written `BUILD.py` code are not
virtualized. Memory-based build tests therefore replace the compiler backend
with a fake that reads and writes through the injected filesystem.

## Coverage

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

Only the real-filesystem contract tests use temporary directories. Incremental
build tests need no disk writes, sleeps, filesystem mocks, or clock patches.
