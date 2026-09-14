"""Check shared header leases without running compilers."""

from contextlib import ExitStack
import asyncio
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import cmake_modules
from header_unit_cache import HeaderUnitBusy, HeaderUnitLocks


class HeaderUnitCacheTests(unittest.TestCase):
    def test_failed_publication_preserves_previous_unit(self) -> None:
        """A failed compiler cannot replace the existing CMI or leave a partial unit."""
        with tempfile.TemporaryDirectory() as temporary:
            final = Path(temporary) / 'unit.pcm'
            final.write_bytes(b'previous complete unit')
            source = SimpleNamespace(cmpath=cmake_modules.bt.Path(str(final)),
                                     type=cmake_modules.bt.SourceType.SYSTEM_HEADER)

            async def compile(target: object, cfg: object) -> None:
                """Simulate a compiler writing partial output before failure."""
                Path(str(source.cmpath)).write_bytes(b'partial')
                raise RuntimeError('compiler failed')

            source.compile = compile
            target = object.__new__(cmake_modules.HeaderUnitTarget)
            with self.assertRaisesRegex(RuntimeError, 'compiler failed'):
                asyncio.run(target.compile_source(source, None))
            self.assertEqual(final.read_bytes(), b'previous complete unit')
            self.assertEqual(str(source.cmpath), str(final))
            self.assertEqual(list(final.parent.iterdir()), [final])

    def test_process_contention_and_release(self) -> None:
        """Other processes may lock unrelated headers, but cannot replace a leased unit."""
        worker = '''
from contextlib import ExitStack
from pathlib import Path
import sys
from header_unit_cache import HeaderUnitLocks, HeaderUnitBusy
with ExitStack() as stack:
    leases = HeaderUnitLocks(Path(sys.argv[1]), stack)
    try:
        leases.acquire(Path(sys.argv[2]))
    except HeaderUnitBusy:
        sys.exit(9)
'''
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            header = root / 'header.h'
            header.touch()
            alias = root / 'alias.h'
            alias.symlink_to(header)

            def attempt(path: Path) -> int:
                """Return a separate process's lease attempt result for path."""
                return subprocess.run([sys.executable, '-c', worker, str(root / 'locks'), str(path)],
                                      cwd=Path(__file__).resolve().parents[1], timeout=10).returncode

            with ExitStack() as stack:
                leases = HeaderUnitLocks(root / 'locks', stack)
                leases.acquire(header)
                leases.acquire(alias)
                self.assertEqual(attempt(alias), 9)
                self.assertEqual(attempt(root / 'other.h'), 0)
            self.assertEqual(attempt(header), 0)

    def test_retry_releases_entire_attempt(self) -> None:
        """Wait only after all attempt leases unwind, then create a fresh attempt."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            calls = []

            def attempt(directory: Path) -> None:
                """Simulate nested contention while holding a different header lease."""
                calls.append(directory)
                with ExitStack() as stack:
                    HeaderUnitLocks(root, stack).acquire(root / 'a.h')
                    if len(calls) == 1:
                        raise HeaderUnitBusy(root / 'b.lock')

            def wait(busy: HeaderUnitBusy) -> None:
                """Verify the prior attempt released a.h before waiting for b."""
                with ExitStack() as stack:
                    HeaderUnitLocks(root, stack).acquire(root / 'a.h')

            with patch.object(cmake_modules, 'build_modules_attempt', side_effect=attempt), \
                    patch.object(HeaderUnitBusy, 'wait', wait):
                cmake_modules.build_modules(root)
            self.assertEqual(calls, [root, root])
