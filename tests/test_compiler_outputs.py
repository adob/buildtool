"""Switching compilers preserves each compiler's artifacts and no-op builds."""

import sys
import unittest
from unittest import mock

import buildtool as bt


class CompilerOutputTests(unittest.TestCase):
    def test_run_uses_build_artifact_without_publishing(self) -> None:
        """Run reuses artifacts and preserves public links across compiler/debug choices."""
        for debug in (False, True):
            with self.subTest(debug=debug), mock.patch.dict(vars(bt)):
                fs = bt.MemoryFileSystem()
                fs.write_text('main.cc', 'int main() {}\n')
                compiled, linked = [], []
                mode = 'debug' if debug else 'release'
                binary = 'main+debug' if debug else 'main'

                def compile_source(source: bt.SourceFile, target: bt.Target, cfg: bt.BuildConfig) -> None:
                    """Write source's fake object using cfg, recording actual compilation."""
                    compiled.append(str(source.objpath))
                    fs.write_text(source.objpath, cfg.CXX)

                def link(*args: object, verbose: bool = False) -> str:
                    """Write the executable named by args and record the link invocation."""
                    output = next(str(arg)[2:] for arg in args if str(arg).startswith('-o'))
                    fs.write_text(output, str(args[0]))
                    linked.append(output)
                    return ''

                def invoke(command: str, clang: bool = False) -> None:
                    """Invoke command with the selected compiler and this case's build mode."""
                    argv = ['bt', command]
                    if debug:
                        argv.append('--debug')
                    if clang:
                        argv.append('--clang')
                    argv.append('main.cc')
                    if command == 'run':
                        argv += ['--option', 'value']
                    with mock.patch.object(sys, 'argv', argv):
                        bt.main(vfs=fs)

                with mock.patch.object(bt, 'ROOT', '.'), \
                     mock.patch.object(bt.SourceFile, 'compile_gcc', autospec=True, side_effect=compile_source), \
                     mock.patch.object(bt.SourceFile, 'compile_clang', autospec=True, side_effect=compile_source), \
                     mock.patch.object(bt, 'shell', side_effect=link), \
                     mock.patch.object(bt.os, 'execv') as execute:
                    invoke('run')
                    gcc_binary = fs.abspath(f'build/{mode}/bin/{binary}')
                    execute.assert_called_with(gcc_binary, [gcc_binary, '--option', 'value'])
                    self.assertFalse(fs.is_dir('bin'))
                    invoke('run')
                    self.assertEqual(len(compiled), 1)
                    self.assertEqual(len(linked), 1)
                    self.assertFalse(fs.is_dir('bin'))

                    invoke('build')
                    public = f'bin/{binary}'
                    expected = f'../build/{mode}/bin/{binary}'
                    self.assertEqual(fs.readlink(public), expected)
                    self.assertEqual(len(compiled), 1)
                    self.assertEqual(len(linked), 1)

                    invoke('run', clang=True)
                    clang_binary = fs.abspath(f'build/{mode}+clang/bin/{binary}')
                    execute.assert_called_with(clang_binary, [clang_binary, '--option', 'value'])
                    self.assertEqual(fs.readlink(public), expected)
                    self.assertEqual(len(compiled), 2)
                    self.assertEqual(len(linked), 2)
                    invoke('run', clang=True)
                    self.assertEqual(fs.readlink(public), expected)
                    self.assertEqual(len(compiled), 2)
                    self.assertEqual(len(linked), 2)

    def test_alternating_compilers_keeps_independent_outputs(self):
        for debug, verbose in [(False, False), (False, True), (True, False), (True, True)]:
            with self.subTest(debug=debug, verbose=verbose), mock.patch.dict(vars(bt)):
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

                def link(*args, verbose=False):
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
                        args = ["bt", "build"]
                        if debug:
                            args.append("--debug")
                        if verbose:
                            args.append("--verbose")
                        if clang:
                            args.append("--clang")
                        args.append("main.cc")
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
