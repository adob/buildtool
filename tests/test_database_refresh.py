"""Database refresh follows timestamps of processed files."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch
import buildtool as bt


class DatabaseRefreshTests(unittest.TestCase):
    def test_timestamp_selection(self) -> None:
        fs = bt.MemoryFileSystem()
        cfg = bt.BuildConfig(vfs=fs)
        for config, ctime, mtime, expected in (
            (False, 11, 1, True), (False, 1, 11, False),
            (True, 11, 1, False), (True, 1, 11, True),
            (False, 10, 10, False),
        ):
            with self.subTest(config=config, ctime=ctime, mtime=mtime):
                cfg.compilation_database_mtime = 10
                cfg.compilation_database_dirty = False
                with patch.object(fs, 'stat', return_value=SimpleNamespace(st_ctime=ctime, st_mtime=mtime)):
                    cfg.check_database_timestamp(bt.Path('file'), build_config=config)
                self.assertEqual(cfg.compilation_database_dirty, expected)

    def test_refresh_triggers(self) -> None:
        for trigger in ('none', 'source', 'config', 'missing', 'failure'):
            with self.subTest(trigger=trigger):
                fs = bt.MemoryFileSystem()
                cfg = bt.BuildConfig(vfs=fs)
                if trigger != 'missing':
                    fs.write_text('compile_commands.json', '[]')
                with patch.object(bt, 'ROOT', '.'), patch.object(bt, 'build_compilation_database') as write:
                    try:
                        database = bt.track_compilation_database(cfg)
                        try:
                            if trigger in ('source', 'failure'):
                                for name in ('first.cc', 'second.cc'):
                                    fs.write_text(name, '')
                                    bt.SourceFile.get(bt.Path(name), cfg)
                            if trigger == 'config':
                                fs.write_text('BUILD.py', 'CFLAGS = []')
                                bt.DirectoryConfig.get(bt.Path('.'), cfg)
                            if trigger == 'failure':
                                raise RuntimeError('build failed')
                        finally:
                            bt.refresh_compilation_database(cfg, database, ['src'])
                    except RuntimeError as error:
                        self.assertEqual(str(error), 'build failed')
                    self.assertEqual(write.call_count, int(trigger != 'none'))
                    self.assertIsNone(cfg.compilation_database_mtime)

    def test_cli_refresh_uses_configured_roots(self) -> None:
        roots = ['project/lib', 'project/cmd']
        for command in ('build', 'run', 'test'):
            with self.subTest(command=command):
                fs = bt.MemoryFileSystem()
                with patch.object(bt, 'ROOT', '.'), \
                     patch.object(bt.sys, 'argv', ['bt', command, 'project/cmd/main.cc']), \
                     patch.object(bt, 'build_targets'), \
                     patch.object(bt, 'build', return_value='build/bin/main'), \
                     patch.object(bt, 'run_tests'), \
                     patch.object(bt.os, 'execv'), \
                     patch.object(bt, 'build_compilation_database') as write:
                    bt._main(SRC_ROOTS=roots, vfs=fs)
                    write.assert_called_once()
                    self.assertEqual(write.call_args.args[1], [bt.Path(p) for p in roots])
