"""Exercise bridge lock ownership across independent buildtool processes."""

from contextlib import ExitStack
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import cmake_modules as bridge


# Use real manifest discovery and file locking, replacing compilation with a
# controllable job. Each child has its own filesystem/cache view and working dir.
WORKER = r'''
from collections.abc import Iterable
from contextlib import ExitStack
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch
import cmake_modules as bridge

directory = Path(sys.argv[1])
registry = Path(bridge.lines(directory, 'registry')[0])
original_lock_files = bridge.lock_files

def lock_files(stack: ExitStack, paths: Iterable[Path]) -> None:
    """Report the attempt before acquiring paths through the real lock helper."""
    print('locking', flush=True)
    original_lock_files(stack, paths)

def configuration(manifest: Path) -> SimpleNamespace:
    """Snapshot manifest's cache when the bridge creates a fresh configuration."""
    cached = manifest / 'cached'
    return SimpleNamespace(manifest=manifest,
                           cached=cached.read_text() if cached.exists() else 'missing')

if len(sys.argv) > 2 and sys.argv[2] == 'metadata':
    def publish(directory: Path, manifest: dict[str, Any], fingerprint: str) -> str:
        """Pause directory's publication between reading and writing shared data."""
        # Model a read/modify/write of shared collation data while using the real
        # publication entry point and its metadata lock.
        shared = registry.parent / 'updates.json'
        previous = json.loads(shared.read_text()) if shared.exists() else []
        print(json.dumps(previous), flush=True)
        input()
        shared.write_text(json.dumps([*previous, directory.name]))
        return fingerprint

    with patch.object(bridge, 'lock_files', side_effect=lock_files), \
         patch.object(bridge, 'publish_cmake_metadata', side_effect=publish):
        bridge.publish_consumer_files(directory, {'modules': {}, 'headers': {}})
    sys.exit(0)

class Target:
    def __init__(self, cfg: SimpleNamespace, roots: list[str], projects: dict[Path, 'Target']) -> None:
        """Retain cfg and participating projects for the simulated compilation."""
        self.cfg, self.projects, self.objs = cfg, projects, []

    def mod2src(self, name: str, kind: bridge.bt.SourceType) -> str:
        """Return name as a placeholder for the requested module kind."""
        return name

    def compile_many(self, sources: list[tuple[Any, ...]]) -> None:
        """Pause the source build until instructed to publish caches or fail."""
        print(json.dumps({project.cfg.manifest.parent.name: project.cfg.cached
                          for project in self.projects.values()}), flush=True)
        if input() == 'fail':
            raise RuntimeError('simulated compiler failure')
        for project in self.projects.values():
            (project.cfg.manifest / 'cached').write_text('built')

with patch.object(bridge, 'cmake_targets', return_value=(registry.parent, [
         {'name': entry.name} for entry in registry.iterdir()])), \
     patch.object(bridge, 'lock_files', side_effect=lock_files), \
     patch.object(bridge, 'configuration', side_effect=configuration), \
     patch.object(bridge, 'ModuleTarget', Target), \
     patch.object(bridge, 'archive_objects'), \
     patch.object(bridge, 'artifact_manifest', return_value={}), \
     patch.object(bridge, 'publish_consumer_files'):
    bridge.build_modules(directory)
'''


class LibraryLockTests(unittest.TestCase):
    def setUp(self) -> None:
        """Create a manifest tree and register cleanup for all child processes."""
        temporary = tempfile.TemporaryDirectory(prefix='buildtool-locks-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.registry = self.root / 'build/libraries'
        self.registry.mkdir(parents=True)
        self.processes: list[subprocess.Popen[str]] = []
        self.addCleanup(self.stop_workers)

    def stop_workers(self) -> None:
        """Terminate remaining workers before removing their temporary files."""
        for process in self.processes:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=10)

    def library(self, name: str, dependencies: tuple[str, ...] = ()) -> Path:
        """Create name's manifest with its registered dependencies and a consumer."""
        source = self.root / 'src' / name
        source.mkdir(parents=True)
        manifest = self.registry / name / 'Release'
        manifest.mkdir(parents=True)
        (manifest / 'root').write_text(str(source) + '\n')
        (manifest / 'roots').write_text('\n'.join(
            str(self.root / 'src' / entry) for entry in (name, *dependencies)) + '\n')
        consumer = self.root / name
        consumer.mkdir()
        for field, value in dict(library=manifest, registry=self.registry,
                                 configuration='Release', modules='lib.example').items():
            (consumer / field).write_text(str(value) + '\n')
        return consumer

    def start(self, consumer: Path, mode: str = 'build') -> subprocess.Popen[str]:
        """Start consumer's build/metadata worker in mode and await its lock attempt."""
        process = subprocess.Popen([sys.executable, '-c', WORKER, str(consumer), mode],
                                   cwd=Path(bridge.__file__).parent,
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True)
        self.processes.append(process)
        self.assertEqual(self.read_line(process), 'locking')
        return process

    def read_line(self, process: subprocess.Popen[str]) -> str:
        """Read one worker event with a deadline so a deadlock fails the test."""
        deadline = time.monotonic() + 10
        line = bytearray()
        while True:
            self.assertTrue(select.select([process.stdout], [], [],
                                          max(0, deadline - time.monotonic()))[0], 'worker timed out')
            # Avoid TextIOWrapper read-ahead hiding a queued event from select().
            byte = os.read(process.stdout.fileno(), 1)
            self.assertTrue(byte, 'worker exited before reporting its build state')
            if byte == b'\n':
                return line.decode()
            line.extend(byte)

    def finish(self, process: subprocess.Popen[str], fail: bool = False) -> None:
        """Release the worker's simulated compile, optionally making it fail."""
        process.stdin.write('fail\n' if fail else 'finish\n')
        process.stdin.flush()
        self.assertEqual(process.wait(timeout=10), 1 if fail else 0)

    def test_shared_dependency_waits_and_rechecks_cache(self) -> None:
        """A dependent request locks baselib too, then reloads state after waiting."""
        baselib = self.library('baselib')
        serialrpc = self.library('serialrpc', ('baselib',))
        first = self.start(baselib)
        self.assertEqual(json.loads(self.read_line(first)), {'baselib': 'missing'})
        second = self.start(serialrpc)
        self.assertFalse(select.select([second.stdout], [], [], 0.2)[0])
        self.finish(first)
        self.assertEqual(json.loads(self.read_line(second)),
                         {'baselib': 'built', 'serialrpc': 'missing'})
        self.finish(second)

    def test_unrelated_libraries_overlap(self) -> None:
        """An unrelated library enters compilation while the first is still running."""
        first = self.start(self.library('baselib'))
        self.read_line(first)
        second = self.start(self.library('other'))
        self.assertEqual(json.loads(self.read_line(second)), {'other': 'missing'})
        self.assertIsNone(first.poll())
        self.finish(second)
        self.finish(first)

    def test_failure_releases_dependency_locks(self) -> None:
        """A failed dependent compilation releases both its own and baselib's lock."""
        baselib = self.library('baselib')
        serialrpc = self.library('serialrpc', ('baselib',))
        failed = self.start(serialrpc)
        self.read_line(failed)
        self.finish(failed, fail=True)
        for consumer in (baselib, serialrpc):
            retry = self.start(consumer)
            self.read_line(retry)
            self.finish(retry)

    def test_lock_order_is_canonical_and_duplicates_are_removed(self) -> None:
        """Different dependency orders and aliases select the same lock sequence."""
        first, second = self.root / 'a.lock', self.root / 'b.lock'
        alias = self.root / 'alias.lock'
        alias.symlink_to(first)
        for paths in ([second, alias, first], [first, second]):
            acquired = []
            with ExitStack() as locks, patch.object(
                    bridge.fcntl, 'flock', side_effect=lambda file, mode: acquired.append(file)):
                bridge.lock_files(locks, paths)
                self.assertEqual([Path(file.name) for file in acquired], [first, second])
                self.assertTrue(all(not file.closed for file in acquired))
            self.assertTrue(all(file.closed for file in acquired))

    def test_native_publication_serializes_shared_metadata(self) -> None:
        """Unrelated requests serialize the shared metadata read/modify/write step."""
        consumers = [self.library(name) for name in ('baselib', 'other')]
        for consumer in consumers:
            (consumer / 'native_modules').write_text('TRUE\n')
        first = self.start(consumers[0], mode='metadata')
        self.assertEqual(json.loads(self.read_line(first)), [])
        second = self.start(consumers[1], mode='metadata')
        self.assertFalse(select.select([second.stdout], [], [], 0.2)[0])
        self.finish(first)
        self.assertEqual(json.loads(self.read_line(second)), ['baselib'])
        self.finish(second)
        self.assertEqual(json.loads((self.registry.parent / 'updates.json').read_text()),
                         ['baselib', 'other'])


if __name__ == '__main__':
    unittest.main()
