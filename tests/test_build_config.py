"""Build caches belong to the configuration that created them."""

import gc
import unittest
from unittest import mock
import weakref

import buildtool as bt


class BuildConfigCacheTests(unittest.TestCase):
    def setUp(self):
        self.fs = bt.MemoryFileSystem()
        self.fs.write_text("main.cc", "")
        self.fs.write_text("BUILD.py", 'CFLAGS = ["-DFIRST"]\n')

    def cached_objects(self, cfg):
        source = bt.SourceFile.get(bt.Path("main.cc"), cfg)
        return {
            "source": source,
            "module": bt.CompiledModule.get("value", cfg),
            "header": bt.HeaderDep.get(bt.Path("header.h"), cfg),
            "directory": source.dircfg(),
            "command": source.compiler_cmd(cfg),
        }

    def test_same_paths_are_cached_separately_for_each_configuration(self):
        first_cfg = bt.BuildConfig(vfs=self.fs, CXX="first-c++", OBJDIR="build/first")
        second_cfg = bt.BuildConfig(vfs=self.fs, CXX="second-c++", OBJDIR="build/second")
        first = self.cached_objects(first_cfg)
        second = self.cached_objects(second_cfg)
        for name, value in self.cached_objects(first_cfg).items():
            with self.subTest(cache=name):
                self.assertIs(value, first[name])
                self.assertIsNot(value, second[name])
        self.assertEqual(first["command"][0], "first-c++")
        self.assertEqual(second["command"][0], "second-c++")
        self.assertIn("-obuild/first/main.o", first["command"])
        self.assertIn("-obuild/second/main.o", second["command"])
        self.assertIs(bt.SourceFile.get(bt.Path("./main.cc"), first_cfg), first["source"])
        self.assertIs(bt.HeaderDep.get(bt.Path("./header.h"), first_cfg), first["header"])
        self.assertIs(bt.DirectoryConfig.get(bt.Path("./"), first_cfg), first["directory"])

    def test_reset_refreshes_only_the_selected_configuration(self):
        first_cfg = bt.BuildConfig(vfs=self.fs)
        second_cfg = bt.BuildConfig(vfs=self.fs)
        first = self.cached_objects(first_cfg)
        second = self.cached_objects(second_cfg)
        first["source"].processed = True
        first["header"].built = True
        first["module"].cmhash = "old-hash"
        self.fs.write_text("BUILD.py", 'CFLAGS = ["-DSECOND"]\n')

        bt.reset_build_state(first_cfg)
        refreshed = self.cached_objects(first_cfg)
        for name, value in self.cached_objects(second_cfg).items():
            with self.subTest(cache=name):
                self.assertIs(value, second[name])
                self.assertIsNot(refreshed[name], first[name])
        self.assertFalse(refreshed["source"].processed)
        self.assertFalse(refreshed["header"].built)
        self.assertIsNone(refreshed["module"].cmhash)
        self.assertIn("-DSECOND", refreshed["command"])
        self.assertNotIn("-DFIRST", refreshed["command"])
        self.assertIn("-DFIRST", second["command"])
        self.assertEqual(self.fs.read_text("main.cc"), "")

    def test_discarded_configuration_and_caches_can_be_collected(self):
        def populate():
            cfg = bt.BuildConfig(vfs=self.fs)
            objects = self.cached_objects(cfg)
            objects["header"].mtime(self.fs)
            return [weakref.ref(cfg)] + [
                weakref.ref(value) for name, value in objects.items() if name != "command"
            ]

        references = populate()
        gc.collect()
        self.assertTrue(all(reference() is None for reference in references))

    def test_header_timestamp_is_cached_until_configuration_reset(self):
        cfg = bt.BuildConfig(vfs=self.fs)
        self.fs.write_text("header.h", "first")
        path = bt.Path("header.h")
        header = bt.HeaderDep.get(path, cfg)
        with mock.patch.object(self.fs, "stat", wraps=self.fs.stat) as stat:
            original_mtime = header.mtime(self.fs)
            self.assertEqual(bt.HeaderDep.get(path, cfg).mtime(self.fs), original_mtime)
            stat.assert_called_once()

        # The next build observes edits made between invocations.
        self.fs.write_text(path, "second")
        cfg.reset_build_state()
        refreshed = bt.HeaderDep.get(path, cfg)
        self.assertGreater(refreshed.mtime(self.fs), original_mtime)

    def test_header_timestamp_cache_distinguishes_filesystems(self):
        header = bt.HeaderDep(bt.Path("header.h"))
        other_fs = bt.MemoryFileSystem()
        self.fs.write_text("header.h", "first")
        self.assertEqual(header.mtime(other_fs), 0)
        self.assertGreater(header.mtime(self.fs), 0)


if __name__ == "__main__":
    unittest.main()
