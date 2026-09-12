import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import buildtool as bt


class FileSystemContract:
    """The same operations must behave alike on disk and in memory."""

    def test_stat_has_nanosecond_timestamps(self):
        """Both filesystems supply numeric timestamps without caller fallbacks."""
        self.fs.write_text('timestamps.txt', '')
        status = self.fs.stat('timestamps.txt')
        for name in ('st_atime', 'st_mtime', 'st_ctime'):
            nanoseconds = getattr(status, name + '_ns')
            self.assertIsInstance(nanoseconds, int)
            self.assertAlmostEqual(nanoseconds / 1_000_000_000,
                                   getattr(status, name), places=6)

    def test_relative_symlinks_and_atomic_replacement(self):
        self.fs.makedirs("bin")
        self.fs.makedirs("build")
        self.fs.write_text("build/program", "first")
        self.fs.symlink("../build/program", "bin/program")
        self.assertEqual(self.fs.readlink("bin/program"), "../build/program")
        self.assertEqual(self.fs.read_text("bin/program"), "first")
        self.assertEqual(self.fs.stat("bin/program"), self.fs.stat("build/program"))
        entry = self.fs.scandir("bin")[0]
        self.assertTrue(entry.is_symlink)
        self.assertTrue(entry.is_file)
        self.fs.write_text("build/other", "second")
        self.fs.symlink("../build/other", "bin/temporary")
        self.fs.replace("bin/temporary", "bin/program")
        self.assertEqual(self.fs.read_text("bin/program"), "second")
        self.assertEqual(self.fs.read_text("build/program"), "first")
        self.fs.unlink("bin/program")
        self.assertEqual(self.fs.read_text("build/other"), "second")

    def test_dangling_symlink_and_writes_through_links(self):
        self.fs.symlink("missing", "link")
        self.assertEqual(self.fs.readlink("link"), "missing")
        self.assertFalse(self.fs.is_file("link"))
        with self.assertRaises(FileExistsError):
            self.fs.symlink("other", "link")
        self.fs.write_text("link", "created")
        self.assertEqual(self.fs.read_text("missing"), "created")
        self.assertEqual(self.fs.readlink("link"), "missing")

    def test_directory_symlink(self):
        self.fs.makedirs("directory")
        self.fs.symlink("directory", "link")
        self.fs.write_text("link/file", "data")
        self.assertEqual(self.fs.read_text("directory/file"), "data")
        self.assertEqual(self.fs.scandir("link")[0].name, "file")
        self.fs.unlink("link")
        self.assertTrue(self.fs.is_dir("directory"))

    def test_text_bytes_and_hash(self):
        self.fs.write_text("text", "hello λ\n")
        self.assertEqual(self.fs.read_text("text"), "hello λ\n")
        data = b"\x00\xffbinary"
        self.fs.write_bytes("binary", data)
        self.assertEqual(self.fs.read_bytes("binary"), data)
        self.assertEqual(self.fs.stat("binary").st_size, len(data))
        self.assertEqual(self.fs.sha256("binary"), hashlib.sha256(data).hexdigest())

    def test_missing_files_and_invalid_parents(self):
        with self.assertRaises(FileNotFoundError):
            self.fs.read_bytes("missing")
        with self.assertRaises(FileNotFoundError):
            self.fs.stat("missing")
        with self.assertRaises(FileNotFoundError):
            self.fs.write_text("missing/child", "data")
        self.fs.write_text("file", "data")
        with self.assertRaises(NotADirectoryError):
            self.fs.stat("file/child")
        with self.assertRaises(NotADirectoryError):
            self.fs.write_text("file/child", "data")
        with self.assertRaises(NotADirectoryError):
            self.fs.makedirs("file/child")
        self.assertFalse(self.fs.is_file("file/child"))
        self.assertFalse(self.fs.is_dir("missing"))

    def test_directories_and_scanning(self):
        self.fs.makedirs("a/b")
        self.fs.makedirs("a/b", exist_ok=True)
        with self.assertRaises(FileExistsError):
            self.fs.makedirs("a/b")
        with self.assertRaises(IsADirectoryError):
            self.fs.read_text("a")
        with self.assertRaises(IsADirectoryError):
            self.fs.write_text("a", "data")
        self.fs.write_text("a/file", "data")
        entries = {entry.name: entry for entry in self.fs.scandir("a")}
        self.assertEqual(set(entries), {"b", "file"})
        self.assertTrue(entries["b"].is_dir)
        self.assertTrue(entries["file"].is_file)
        self.assertFalse(entries["file"].is_symlink)
        self.assertEqual(entries["file"].path, os.path.join("a", "file"))

    def test_replace_and_unlink(self):
        self.fs.write_text("old", "old")
        self.fs.write_text("new", "new")
        mtime = self.fs.stat("new").st_mtime
        self.fs.replace("new", "old")
        self.assertEqual(self.fs.read_text("old"), "new")
        self.assertEqual(self.fs.stat("old").st_mtime, mtime)
        self.assertFalse(self.fs.is_file("new"))
        self.fs.replace("old", "old")
        self.assertEqual(self.fs.read_text("old"), "new")
        self.fs.unlink("old")
        with self.assertRaises(FileNotFoundError):
            self.fs.unlink("old")

    def test_failed_replace_preserves_destination(self):
        self.fs.write_text("destination", "keep")
        with self.assertRaises(FileNotFoundError):
            self.fs.replace("missing", "destination")
        self.assertEqual(self.fs.read_text("destination"), "keep")

    def test_relative_and_absolute_paths(self):
        root = self.fs.getcwd()
        self.fs.makedirs("sub")
        self.fs.write_text("source", "data")
        self.fs.chdir("sub")
        self.assertEqual(self.fs.abspath("../source"), os.path.join(root, "source"))
        self.assertEqual(self.fs.read_text("../source"), "data")
        self.fs.chdir(root)
        self.assertEqual(self.fs.getcwd(), root)
        with self.assertRaises(FileNotFoundError):
            self.fs.chdir("missing")
        self.assertEqual(self.fs.getcwd(), root)


class MemoryFileSystemTests(FileSystemContract, unittest.TestCase):
    def setUp(self):
        self.fs = bt.MemoryFileSystem()

    def test_virtual_cwd_does_not_change_host_cwd(self):
        host_cwd = os.getcwd()
        self.fs.makedirs("sub")
        self.fs.chdir("sub")
        self.assertEqual(os.getcwd(), host_cwd)
        self.assertNotEqual(self.fs.getcwd(), host_cwd)

    def test_monotonic_file_timestamps(self):
        self.fs.write_text("file", "old")
        old = self.fs.stat("file").st_mtime
        self.fs.advance(10)
        self.fs.write_text("file", "new")
        self.assertGreater(self.fs.stat("file").st_mtime, old + 10)


class RealFileSystemTests(FileSystemContract, unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        previous = os.getcwd()
        self.fs = bt.RealFileSystem()
        self.fs.chdir(directory)
        self.addCleanup(self.fs.chdir, previous)

    def test_real_writes_are_visible_to_external_tools(self):
        self.fs.write_text("output", "content")
        self.assertEqual(Path("output").read_text(), "content")
        Path("output").write_text("updated externally")
        self.assertEqual(self.fs.read_text("output"), "updated externally")

    def test_scandir_preserves_symlink_information(self):
        self.fs.makedirs("directory")
        os.symlink("directory", "link")
        link = next(entry for entry in self.fs.scandir(".") if entry.name == "link")
        self.assertTrue(link.is_dir)
        self.assertTrue(link.is_symlink)


class BuildtoolFileSystemTests(unittest.TestCase):
    def setUp(self):
        self.fs = bt.MemoryFileSystem()
        self.cfg = bt.BuildConfig(vfs=self.fs, DEPDIR="build")

    def test_path_observes_creation_changes_and_deletion(self):
        path = bt.Path("file")
        self.assertFalse(path.exists(self.fs))
        self.assertEqual(path.mtime(self.fs), 0)
        self.fs.write_text(path, "first")
        self.assertTrue(path.exists(self.fs))
        old_mtime = path.mtime(self.fs)
        self.fs.write_text(path, "second")
        self.assertEqual(path.read_text(self.fs), "second")
        self.assertGreater(path.mtime(self.fs), old_mtime)
        self.fs.unlink(path)
        self.assertFalse(path.exists(self.fs))

    def test_directory_configuration_cache_can_be_reset(self):
        self.fs.write_text("BUILD.py", 'CFLAGS = ["-DFIRST"]\n')
        config = bt.DirectoryConfig.get(bt.Path("."), self.cfg)
        self.assertEqual(config.buildvars["CFLAGS"], ["-DFIRST"])
        self.cfg.reset_build_state()
        cached = bt.DirectoryConfig.get(bt.Path("."), self.cfg)
        self.assertEqual(cached.buildvars, config.buildvars)
        self.fs.write_text("BUILD.py", 'CFLAGS = ["-DSECOND"]\n')
        self.cfg.reset_build_state()
        changed = bt.DirectoryConfig.get(bt.Path("."), self.cfg)
        self.assertEqual(changed.buildvars["CFLAGS"], ["-DSECOND"])

    def test_equal_paths_have_independent_cached_configuration(self):
        self.fs.write_text("BUILD.py", 'CFLAGS = ["-DFIRST"]\n')
        original_path = bt.Path(".")
        original = bt.DirectoryConfig.get(original_path, self.cfg)
        second = bt.MemoryFileSystem()
        second.write_text("BUILD.py", 'CFLAGS = ["-DSECOND"]\n')
        second_cfg = bt.BuildConfig(vfs=second)
        other_path = bt.Path(".")
        other = bt.DirectoryConfig.get(other_path, second_cfg)
        self.assertEqual(other.buildvars["CFLAGS"], ["-DSECOND"])
        self.assertEqual(original.buildvars["CFLAGS"], ["-DFIRST"])
        self.assertEqual(original_path, other_path)
        self.assertEqual(len({original_path, other_path}), 1)
        self.assertIs(bt.DirectoryConfig.get(original_path, self.cfg), original)
        self.assertEqual((original_path / "BUILD.py").read_text(self.fs),
                         'CFLAGS = ["-DFIRST"]\n')
        self.assertEqual((original_path / "BUILD.py").read_text(second),
                         'CFLAGS = ["-DSECOND"]\n')

    def test_paths_are_filesystem_independent_values(self):
        source = bt.Path("src/main.cc")
        cases = [
            (source.parent, "src"),
            (source.parent / "extra.cc", "src/extra.cc"),
            ("prefix" / source, "prefix/src/main.cc"),
            (bt.Path(source), "src/main.cc"),
            (source.with_suffix(".o"), "src/main.o"),
            (source.with_name("other.cc"), "src/other.cc"),
            (source.relative_to("src"), "main.cc"),
            (bt.mod2path("m:part", bt.SourceType.MODULE), "m/part.cc"),
            (bt.Path("build") / bt.Path("/usr/include/file.h"),
             "build/SYSTEM/usr/include/file.h"),
        ]
        for derived, expected in cases:
            with self.subTest(path=expected):
                self.assertEqual(derived, bt.Path(expected))
                self.assertFalse(hasattr(derived, "vfs"))

    def test_path_io_requires_explicit_filesystem(self):
        path = bt.Path("file")
        for method in [path.try_stat, path.mtime, path.exists,
                       path.read_text, path.is_dir, path.is_file]:
            with self.subTest(method=method.__name__):
                with self.assertRaises(TypeError):
                    method()

    def test_one_path_can_use_multiple_filesystems(self):
        path = bt.Path("file")
        second = bt.MemoryFileSystem()
        self.fs.write_text(path, "first")
        second.makedirs(path)
        self.assertTrue(path.is_file(self.fs))
        self.assertFalse(path.is_dir(self.fs))
        self.assertFalse(path.is_file(second))
        self.assertTrue(path.is_dir(second))
        self.assertEqual(path.read_text(self.fs), "first")
        self.assertEqual(path.try_stat(second), second.stat(path))
        self.fs.unlink(path)
        self.assertFalse(path.exists(self.fs))
        self.assertTrue(path.exists(second))

    def test_discovery_and_compilation_database_use_virtual_files(self):
        self.fs.makedirs("src/nested")
        self.fs.makedirs("src/.hidden")
        for name in ["src/main.cc", "src/nested/extra.cc", "src/ignore.txt",
                     "src/.hidden/hidden.cc"]:
            self.fs.write_text(name, "")
        # Fail if source discovery/metadata accidentally bypass the VFS.
        with mock.patch("builtins.open", side_effect=AssertionError("host open")), \
             mock.patch("os.scandir", side_effect=AssertionError("host scandir")), \
             mock.patch("os.stat", side_effect=AssertionError("host stat")):
            files = list(bt.find_files([bt.Path("src")], suffixes=(".cc",), vfs=self.fs))
            self.assertEqual({str(path) for path in files},
                             {"src/main.cc", "src/nested/extra.cc"})
            bt.build_compilation_database(bt.Path("compile_commands.json"),
                                          [bt.Path("src")], self.cfg)
            database = json.loads(self.fs.read_text("compile_commands.json"))
        self.assertEqual(len(database), 2)
        self.assertTrue(all(row["directory"] == self.fs.getcwd() for row in database))
        self.assertFalse(self.fs.is_file("compile_commands.json.tmp"))


if __name__ == "__main__":
    unittest.main()
