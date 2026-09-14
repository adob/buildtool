"""Check jobserver accounting with real Make and Ninja schedulers."""

import asyncio
from collections import Counter
import io
import json
import os
from pathlib import Path
import select
import signal
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

from jobserver import JobServer, advertised_jobs, make_suppresses_execution
from memory import GIB, MemoryBudget
from scheduler import BuildSession, Job


@unittest.skipUnless(sys.platform == 'linux', 'pipe fixtures require Linux procfs')
class JobServerTests(unittest.IsolatedAsyncioTestCase):
    def server(self, tokens: bytes = b'xy', jobs: int | None = None) -> tuple[JobServer, int]:
        """Create a pipe with tokens and advertised jobs; return client and original reader."""
        reader, writer = os.pipe()
        self.addCleanup(os.close, reader)
        self.addCleanup(os.close, writer)
        os.write(writer, tokens)
        limit = str(jobs) if jobs is not None else ''
        server = JobServer.from_environment({'MAKEFLAGS': f'-j{limit} --jobserver-auth={reader},{writer}'})
        self.assertIsNotNone(server)
        self.addCleanup(server.close)
        return server, reader

    def available_tokens(self, reader: int) -> bytes:
        """Drain available bytes from reader without waiting for nonexistent tokens."""
        result = b''
        while select.select([reader], [], [], 0)[0]:
            result += os.read(reader, 1)
        return result

    async def test_budget_and_pipe_flags(self) -> None:
        """Limit active jobs to tokens plus the implicit slot, preserving Make's FD flags."""
        server, reader = self.server()
        self.assertTrue(os.get_blocking(reader))
        session = BuildSession(jobs=8, output=io.StringIO(), jobserver=server)
        active = maximum = 0

        async def compile(job: Job) -> None:
            """Hold a compiler slot briefly while tracking concurrent active jobs."""
            nonlocal active, maximum
            async with job.compiler_slot():
                active += 1
                maximum = max(maximum, active)
                await asyncio.sleep(0.02)
                active -= 1

        for index in range(12):
            session.schedule(index, compile)
        await asyncio.wait_for(session.finish(), 5)
        self.assertEqual(maximum, 3)
        self.assertEqual(active, 0)
        self.assertEqual(server.active, 0)
        self.assertEqual(Counter(self.available_tokens(reader)), Counter(b'xy'))
        self.assertTrue(os.get_blocking(reader))

    async def test_import_wait_lends_implicit_slot(self) -> None:
        """A one-slot parent can compile an imported dependency without deadlocking."""
        server, reader = self.server(b'')
        session = BuildSession(jobs=8, output=io.StringIO(), jobserver=server)
        events = []

        async def dependency(job: Job) -> None:
            """Record dependency execution while the importer lends its implicit slot."""
            async with job.compiler_slot():
                events.append('dependency')

        async def importer(job: Job) -> None:
            """Request a dependency while already holding the only runnable slot."""
            async with job.compiler_slot():
                events.append('importer')
                await job.wait_for_dependency(session.schedule('dependency', dependency, parent=job))
                events.append('resumed')

        session.schedule('importer', importer)
        await asyncio.wait_for(session.finish(), 5)
        self.assertEqual(events, ['importer', 'dependency', 'resumed'])
        self.assertEqual(self.available_tokens(reader), b'')

    async def test_failure_and_cancellation_return_tokens(self) -> None:
        """Reap running and token-waiting jobs without leaking or inventing slots."""
        server, reader = self.server()
        session = BuildSession(jobs=8, output=io.StringIO(), jobserver=server)
        started = 0
        ready = asyncio.Event()

        async def compile(job: Job) -> None:
            """Fail one worker after every available execution slot is occupied."""
            nonlocal started
            async with job.compiler_slot():
                started += 1
                if started == 3:
                    ready.set()
                if job.key == 0:
                    await ready.wait()
                    raise RuntimeError('compiler failed')
                await asyncio.Event().wait()

        for index in range(8):
            session.schedule(index, compile)
        with self.assertRaisesRegex(RuntimeError, 'compiler failed'):
            await asyncio.wait_for(session.finish(), 5)
        self.assertEqual(server.active, 0)
        self.assertEqual(Counter(self.available_tokens(reader)), Counter(b'xy'))

    async def test_cancel_after_grant_returns_token(self) -> None:
        """Cancellation between dispatch and slot entry returns the already granted token."""
        server, reader = self.server(b'z')
        self.assertTrue(server.try_acquire())  # Keep the implicit slot occupied.
        session = BuildSession(jobs=2, output=io.StringIO(), jobserver=server)

        async def empty(job: Job) -> None:
            """Provide a job identity for the explicitly controlled slot acquisition."""

        job = session.schedule('job', empty)
        waiter = asyncio.create_task(session.slots.acquire(job))
        await asyncio.sleep(0)
        session.slots.dispatch()
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        await session.close()
        server.release()
        self.assertEqual(self.available_tokens(reader), b'z')

    async def test_fifo_and_last_authorization(self) -> None:
        """Open the last advertised FIFO, preserve token bytes, and close idempotently."""
        with tempfile.TemporaryDirectory() as temporary:
            fifo = Path(temporary) / 'jobserver fifo'
            os.mkfifo(fifo)
            keeper = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)
            try:
                os.write(keeper, b'Q')
                flags = '--jobserver-auth=-1,-1 ' + shlex.quote('--jobserver-auth=fifo:' + str(fifo))
                server = JobServer.from_environment({'MAKEFLAGS': flags})
                try:
                    self.assertTrue(server.try_acquire())
                    self.assertTrue(server.try_acquire())
                    self.assertFalse(server.try_acquire())
                finally:
                    server.close()
                server.close()
                self.assertEqual(os.read(keeper, 10), b'Q')
            finally:
                os.close(keeper)

    async def test_unavailable_server_falls_back(self) -> None:
        """Missing, disabled, or stale authorization must not enable extra workers."""
        self.assertIsNone(JobServer.from_environment({}))
        self.assertIsNone(JobServer.from_environment({'MAKEFLAGS': '--jobserver-auth=-1,-1'}))
        with self.assertWarnsRegex(UserWarning, 'using one compiler'):
            self.assertIsNone(JobServer.from_environment({'MAKEFLAGS': '--jobserver-auth=99998,99999'}))

    async def test_make_execution_modes(self) -> None:
        """Honor no-execution flags without confusing variable assignments or FIFO paths."""
        for flags, expected in (('nrw -j4', True), ('t', True), ('q', True),
                                ('--dry-run', True), ('rw -j4 --jobserver-auth=fifo:/tmp/tokens', False),
                                ('', False), ('OUTPUT=notes', False)):
            self.assertEqual(make_suppresses_execution({'MAKEFLAGS': flags}), expected)

    async def test_advertised_job_count(self) -> None:
        """Read short/long Make job limits and honor a later unlimited setting."""
        for flags, expected in (('-j8', 8), ('-j 8', 8), ('--jobs=8', 8), ('--jobs 8', 8),
                                ('-j8 -j4', 4), ('-j8 -j --jobserver-auth=3,4', None),
                                ('-j --jobserver-auth=3,4', None), ('rw', None)):
            self.assertEqual(advertised_jobs(shlex.split(flags)), expected)

    async def test_banner_reports_make_request_not_cpu_limit(self) -> None:
        """Report Make's request separately from a CPU/memory ceiling or unknown budget."""
        for requested, local, expected in ((8, 32, 8), (8, 4, 4), (None, 32, 32)):
            with self.subTest(requested=requested, local=local):
                server, _ = self.server(b'', jobs=requested)
                output = io.StringIO()
                session = BuildSession(jobs=local, jobserver=server, output=output,
                                       memory=MemoryBudget(available=lambda: 128 * GIB))

                async def compile(job: Job) -> None:
                    """Start one compilation to trigger the concurrency banner."""
                    async with job.compiler_slot():
                        job.message('compiling')

                session.schedule('compile', compile)
                await session.finish()
                self.assertEqual(session.slots.limit, expected)
                self.assertIn(f'Concurrency: up to {expected} compiler jobs', output.getvalue())
                if requested is None:
                    self.assertIn(f'local limit: {local}', output.getvalue())
                    self.assertNotIn('requested:', output.getvalue())
                else:
                    self.assertIn(f'requested: {requested}', output.getvalue())

    async def test_bridge_termination_returns_tokens(self) -> None:
        """SIGTERM cancels live compilers before the bridge returns borrowed tokens."""
        reader, writer = os.pipe()
        self.addCleanup(os.close, reader)
        self.addCleanup(os.close, writer)
        os.write(writer, b'Z')
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            environment = dict(os.environ, MAKEFLAGS=f'--jobserver-auth={reader},{writer}')
            process = subprocess.Popen([sys.executable, '-c', SIGNAL_WORKER, str(directory)],
                                       cwd=Path(__file__).resolve().parents[1], env=environment,
                                       pass_fds=(reader, writer), stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE)
            try:
                self.assertTrue(select.select([process.stdout], [], [], 10)[0])
                self.assertEqual(process.stdout.readline().strip(), b'ready')
                self.assertFalse(select.select([reader], [], [], 0)[0])
                process.send_signal(signal.SIGTERM)
                process.communicate(timeout=10)
                self.assertNotEqual(process.returncode, 0)
                self.assertEqual(self.available_tokens(reader), b'Z')
                for pid in json.loads((directory / 'pids.json').read_text()):
                    with self.assertRaises(ProcessLookupError):
                        os.kill(pid, 0)
            finally:
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=10)


SIGNAL_WORKER = r'''
import asyncio
import io
import json
from pathlib import Path
import sys
from unittest.mock import patch
import cmake_modules as bridge
from compiler import run_compiler
from jobserver import JobServer
from scheduler import BuildSession, Job

def build(directory: Path) -> None:
    """Run actual sleeping compiler processes under the bridge's signal handler."""
    server = JobServer.from_environment()
    async def compile_all() -> None:
        """Start two compilers, announcing readiness once both own execution slots."""
        session = BuildSession(jobs=2, output=io.StringIO(), jobserver=server)
        pids = []
        async def protocol(process: asyncio.subprocess.Process) -> None:
            """Record process and wait until termination interrupts compilation."""
            pids.append(process.pid)
            if len(pids) == 2:
                (directory / 'pids.json').write_text(json.dumps(pids))
                print('ready', flush=True)
            await process.wait()
        async def work(job: Job) -> None:
            """Hold a slot while running a cancellable external compiler substitute."""
            await run_compiler(job, [sys.executable, '-c', 'import time; time.sleep(60)'],
                               protocol=protocol)
        session.schedule('a', work)
        session.schedule('b', work)
        await session.finish()
    try:
        asyncio.run(compile_all())
    finally:
        server.close()
with patch.object(bridge, 'build_modules', side_effect=build):
    bridge.main()
'''


POOL_WORKER = r'''
import asyncio
import fcntl
import io
import json
import sys
from pathlib import Path
from jobserver import JobServer
from scheduler import BuildSession, Job

def change(delta: int) -> None:
    """Update the shared count of active workers across build commands."""
    with open('counter.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = Path('counter.json')
        state = json.loads(path.read_text()) if path.exists() else {'active': 0, 'maximum': 0}
        state['active'] += delta
        state['maximum'] = max(state['maximum'], state['active'])
        path.write_text(json.dumps(state))

async def main() -> None:
    """Run nested workers, or one ordinary build command, against a shared counter."""
    ordinary = '--ordinary' in sys.argv
    server = None if ordinary else JobServer.from_environment()
    print(f'advertised jobs: {server.jobs if server else 1}')
    session = BuildSession(jobs=8 if server else 1, output=io.StringIO(), jobserver=server)
    async def work(job: Job) -> None:
        """Hold a shared slot while accounting for concurrent recipe workers."""
        async with job.compiler_slot():
            change(1)
            try:
                await asyncio.sleep(0.05)
            finally:
                change(-1)
    try:
        for index in range(1 if ordinary else 12):
            session.schedule(index, work)
        await session.finish()
    finally:
        if server:
            server.close()
asyncio.run(main())
'''


@unittest.skipUnless(shutil.which('make'), 'requires GNU Make')
class MakeJobServerTests(unittest.TestCase):
    def test_global_recipe_budget(self) -> None:
        """Two real Make recipes share -j3 for FIFO/pipe; -j1 remains serial."""
        version = subprocess.check_output(['make', '--version'], text=True)
        if 'GNU Make' not in version or tuple(map(int, version.split()[2].split('.')[:2])) < (4, 4):
            self.skipTest('requires GNU Make 4.4+ to exercise both transports')
        cases = [('fifo', 3), ('fifo', 1)]
        if sys.platform == 'linux':
            cases.append(('pipe', 3))
        for style, jobs in cases:
            with self.subTest(style=style, jobs=jobs), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / 'worker.py').write_text(POOL_WORKER)
                (root / 'Makefile').write_text('.PHONY: all a b\nall: a b\na b:\n\t+' +
                                              shlex.quote(sys.executable) + ' worker.py\n')
                environment = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
                environment.pop('MAKEFLAGS', None)
                result = subprocess.run(['make', f'-j{jobs}', f'--jobserver-style={style}'], cwd=root,
                                        env=environment, capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(result.stderr, '')
                self.assertEqual(result.stdout.count(f'advertised jobs: {jobs}'), 2, result.stdout)
                self.assertEqual(json.loads((root / 'counter.json').read_text()),
                                 {'active': 0, 'maximum': jobs})


class NinjaJobServerTests(unittest.TestCase):
    def test_global_command_budget(self) -> None:
        """Nested buildtool workers and ordinary Ninja commands share -j1/-j3/-j8."""
        ninja = os.environ.get('BT_TEST_NINJA', shutil.which('ninja'))
        if not ninja or '--jobserver-pool' not in subprocess.run(
                [ninja, '--help'], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True).stdout:
            self.skipTest('requires Ninja with --jobserver-pool (or BT_TEST_NINJA)')
        for jobs in (1, 3, 8):
            with self.subTest(jobs=jobs), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / 'worker.py').write_text(POOL_WORKER)
                (root / 'build.ninja').write_text(
                    'rule nested\n  command = ' + shlex.quote(sys.executable) + ' worker.py\n'
                    'rule ordinary\n  command = ' + shlex.quote(sys.executable) + ' worker.py --ordinary\n'
                    'build a: nested\nbuild b: nested\n'
                    'build c: ordinary\nbuild d: ordinary\nbuild e: ordinary\n')
                environment = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
                environment.pop('MAKEFLAGS', None)
                result = subprocess.run([ninja, f'-j{jobs}', '--jobserver-pool'], cwd=root,
                                        env=environment, capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(result.stderr, '')
                self.assertEqual(json.loads((root / 'counter.json').read_text()),
                                 {'active': 0, 'maximum': jobs})


if __name__ == '__main__':
    unittest.main()
