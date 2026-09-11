"""Toolchain nm discovery is shared without caching per-target symbol checks."""

import asyncio
import unittest
from unittest import mock

import buildtool as bt


class NmCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_targets_share_lookup_but_inspect_each_object(self) -> None:
        """Overlapping target checks launch one discovery command and two nm commands."""
        cfg = bt.BuildConfig(vfs=bt.MemoryFileSystem())
        calls = []

        async def shell(job: bt.Job, *args: str | bt.Path) -> str:
            """Record args and suspend discovery so another target can join the lookup."""
            calls.append(args)
            if args[1] == '-print-prog-name=nm':
                await asyncio.sleep(0)
                return 'toolchain-nm\n'
            return 'main T 0 10\n'

        targets = [bt.Target(bt.Path(name), cfg) for name in ('a', 'b')]
        for target in targets:
            target.objs = [target.path.with_suffix('.o')]
        with mock.patch.object(bt, 'shell_async', side_effect=shell):
            results = await asyncio.gather(*(target.defines_main_async(mock.Mock())
                                             for target in targets))
        self.assertEqual(results, [True, True])
        self.assertEqual(sum(args[1] == '-print-prog-name=nm' for args in calls), 1)
        self.assertEqual(sum(args[0] == 'toolchain-nm' for args in calls), 2)
        with mock.patch.object(bt, 'shell') as sync_shell:
            self.assertEqual(cfg.get_nm(), 'toolchain-nm')
            sync_shell.assert_not_called()

    async def test_failed_lookup_is_retried(self) -> None:
        """A failed compiler probe must not poison subsequent discovery."""
        cfg = bt.BuildConfig(vfs=bt.MemoryFileSystem())
        with mock.patch.object(bt, 'shell_async', side_effect=[RuntimeError('failed'), 'nm\n']) as shell:
            with self.assertRaisesRegex(RuntimeError, 'failed'):
                await cfg.get_nm_async(mock.Mock())
            self.assertEqual(await cfg.get_nm_async(mock.Mock()), 'nm')
            self.assertEqual(shell.call_count, 2)

    async def test_cache_is_per_compiler_and_configuration_and_resets(self) -> None:
        """Keep compiler/configuration lookups separate and clear them between builds."""
        cfg = bt.BuildConfig(vfs=bt.MemoryFileSystem(), CXX='g++')
        other = bt.BuildConfig(vfs=bt.MemoryFileSystem(), CXX='g++')
        with mock.patch.object(bt, 'shell', return_value='nm\n') as shell:
            self.assertEqual(cfg.get_nm(), 'nm')
            self.assertEqual(cfg.get_nm(), 'nm')
            cfg.CXX = 'clang++'
            self.assertEqual(cfg.get_nm(), 'nm')
            other.get_nm()
            self.assertEqual(shell.call_count, 3)
            cfg.reset_build_state()
            cfg.get_nm()
            self.assertEqual(shell.call_count, 4)
        with mock.patch.object(bt, 'shell_async') as async_shell:
            self.assertEqual(await cfg.get_nm_async(mock.Mock()), 'nm')
            async_shell.assert_not_called()
