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
                            bt.refresh_compilation_database(cfg, database)
                    except RuntimeError as error:
                        self.assertEqual(str(error), 'build failed')
                    self.assertEqual(write.call_count, int(trigger != 'none'))
                    self.assertIsNone(cfg.compilation_database_mtime)
