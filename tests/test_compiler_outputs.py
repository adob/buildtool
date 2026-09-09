"""Switching compilers preserves each compiler's artifacts and no-op builds."""

import sys
import unittest
from unittest import mock

import buildtool as bt


class CompilerOutputTests(unittest.TestCase):
    def test_alternating_compilers_keeps_independent_outputs(self):
        for debug in [False, True]:
            with self.subTest(debug=debug), mock.patch.dict(vars(bt)):
                fs = bt.MemoryFileSystem()
                fs.write_text("main.cc", "int main() {}\n")
                fs.write_text("BUILD.py", 'CFLAGS = ["-DPROJECT_FLAG"]\n')
                compiled = []
                linked = []
                mode = "debug" if debug else "release"
                binary = "main+debug" if debug else "main"
                fs.makedirs("bin")
                # Upgrade an executable left by the previous output layout.
                fs.write_text(f"bin/{binary}", "old executable")

                def compile_source(source, target, cfg):
                    compiled.append(cfg.CXX)
                    fs.write_text(source.objpath, cfg.CXX)

                def link(*args):
                    compiler = args[0]
                    objects = [arg for arg in args if str(arg).endswith(".o")]
                    self.assertEqual(len(objects), 1)
                    self.assertEqual(fs.read_text(objects[0]), compiler)
                    output = next(str(arg)[2:] for arg in args if str(arg).startswith("-o"))
                    fs.write_text(output, compiler)
                    linked.append(compiler)
                    return ""

                with mock.patch.object(bt, "ROOT", "."), \
                     mock.patch.object(bt.SourceFile, "compile_gcc", autospec=True,
                                       side_effect=compile_source), \
                     mock.patch.object(bt.SourceFile, "compile_clang", autospec=True,
                                       side_effect=compile_source), \
                     mock.patch.object(bt, "shell", side_effect=link):
                    for clang in [False, True, False, True]:
                        args = ["bt", "build", "main.cc"]
                        if debug:
                            args.append("--debug")
                        if clang:
                            args.append("--clang")
                        with mock.patch.object(sys, "argv", args):
                            bt.main(vfs=fs)
                        suffix = "+clang" if clang else ""
                        self.assertEqual(fs.readlink(f"bin/{binary}"),
                                         f"../build/{mode}{suffix}/bin/{binary}")
                        compiler = bt.CLANG_PATH + bt.CLANGXX if clang else bt.CXX
                        self.assertEqual(fs.read_text(f"bin/{binary}"), compiler)

                compilers = [bt.CXX, bt.CLANG_PATH + bt.CLANGXX]
                self.assertEqual(compiled, compilers)
                self.assertEqual(linked, compilers)
                for suffix, compiler in zip(["", "+clang"], compilers):
                    self.assertEqual(fs.read_text(f"build/{mode}{suffix}/main.o"), compiler)
                    self.assertTrue(fs.is_file(f"build/{mode}{suffix}/main.info"))
                    self.assertTrue(fs.is_file(f"build/{mode}{suffix}/buildvars.json"))
                    self.assertEqual(fs.read_text(f"build/{mode}{suffix}/bin/{binary}"), compiler)

    def test_failed_link_preserves_public_executable(self):
        fs = bt.MemoryFileSystem()
        fs.makedirs("build/release/bin")
        fs.makedirs("bin")
        fs.write_text("build/release/bin/main", "gcc executable")
        fs.symlink("../build/release/bin/main", "bin/main")
        cfg = bt.BuildConfig(vfs=fs, OBJDIR="build/release+clang", BINDIR="bin")
        target = bt.Target(bt.Path("main"), cfg)
        with mock.patch.object(bt, "shell", side_effect=SystemExit(1)):
            with self.assertRaises(SystemExit):
                target.link()
        self.assertEqual(fs.readlink("bin/main"), "../build/release/bin/main")
        self.assertEqual(fs.read_text("bin/main"), "gcc executable")

    def test_symlink_publication_failure_preserves_existing_file(self):
        fs = bt.MemoryFileSystem()
        fs.write_text("main", "old executable")
        with mock.patch.object(fs, "replace", side_effect=OSError("replace failed")):
            with self.assertRaises(OSError):
                bt.atomic_symlink(bt.Path("main"), "build/release/bin/main", fs)
        self.assertEqual(fs.read_text("main"), "old executable")
        self.assertEqual([entry.name for entry in fs.scandir(".")], ["main"])


if __name__ == "__main__":
    unittest.main()
