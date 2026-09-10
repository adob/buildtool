"""Command flags preserve caller order across processes and cached builds."""

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

import buildtool as bt


class FlagOrderTests(unittest.TestCase):
    def test_hash_seed_changes_do_not_trigger_recompilation(self):
        probe = '''
import json, sys
import buildtool as bt
saved = json.load(sys.stdin)
fs = bt.MemoryFileSystem()
fs.write_text("main.cc", "")
flags = ["-DA=1", "-DB=2", "-DC=3", "-DD=4", "-UA", "-DA=1"]
fs.write_text("BUILD.py", "CFLAGS = " + repr(flags))
clang = sys.argv[1] == "clang"
cfg = bt.BuildConfig(vfs=fs, CXX="clang++" if clang else "g++", CXXFLAGS=[], USECLANG=clang)
source = bt.SourceFile.get(bt.Path("main.cc"), cfg)
command = source.compiler_cmd(cfg)
fs.makedirs(source.infofile.parent, exist_ok=True)
fs.write_text(source.infofile, json.dumps({"command": command if saved is None else saved, "deps": []}))
source.check_up_to_date(cfg)
print(json.dumps({"command": command, "need_recompile": source.need_recompile}))
'''
        for compiler in ["gcc", "clang"]:
            saved = None
            for seed in ["1", "2", "3"]:
                with self.subTest(compiler=compiler, seed=seed):
                    result = subprocess.run(
                        [sys.executable, "-c", probe, compiler],
                        cwd=Path(bt.__file__).parent,
                        input=json.dumps(saved), text=True, capture_output=True,
                        env=dict(os.environ, PYTHONHASHSEED=seed,
                                 PYTHONDONTWRITEBYTECODE="1"), check=True,
                    )
                    data = json.loads(result.stdout)
                    self.assertFalse(data["need_recompile"])
                    if saved is not None:
                        self.assertEqual(data["command"], saved)
                    saved = data["command"]

    def test_project_and_package_flags_keep_order_and_repetitions(self):
        fs = bt.MemoryFileSystem()
        cflags = ["-Iz", "-Ia", "-DVALUE=1", "-UVALUE", "-DVALUE=1",
                  "-include", "first.hpp", "-include", "second.hpp"]
        ldflags = ["-lfirst", "-lsecond", "-lfirst"]
        fs.write_text("BUILD.py", f"CFLAGS = {cflags!r}\nLDFLAGS = {ldflags!r}\nPKGCONFIG = ['alpha', 'beta']\n")
        fs.write_text("main.cc", "")
        cfg = bt.BuildConfig(vfs=fs, LDFLAGS=["-lglobal", "-lglobal"])
        responses = {
            ("--libs", "alpha"): "-lalpha -lshared -lalpha",
            ("--libs", "beta"): "-lbeta -lshared",
            ("--cflags", "alpha"): "-Ialpha -DVALUE=2 -std=c++20",
            ("--cflags", "beta"): "-Ibeta -DVALUE=3",
        }
        with mock.patch.object(bt, "shell", side_effect=lambda tool, *args: responses[args]) as pkg:
            directory = bt.DirectoryConfig.get(bt.Path("."), cfg)
            self.assertEqual(pkg.call_count, 4)
        expected_cflags = cflags + ["-Ialpha", "-DVALUE=2", "-Ibeta", "-DVALUE=3"]
        expected_ldflags = ldflags + ["-lalpha", "-lshared", "-lalpha", "-lbeta", "-lshared"]
        self.assertEqual(directory.buildvars["CFLAGS"], expected_cflags)
        self.assertEqual(directory.linkflags, expected_ldflags)
        source = bt.SourceFile.get(bt.Path("main.cc"), cfg)
        self.assertEqual(source.compiler_extra_args(), [
            "-idirafter" + flag[2:] if flag.startswith("-I") else flag
            for flag in expected_cflags
        ])
        target = bt.Target(bt.Path("main"), cfg)
        target.add_config(directory)
        target.add_config(directory)
        self.assertEqual(target.get_linkflags(), cfg.LDFLAGS + expected_ldflags)

        cfg.reset_build_state()
        with mock.patch.object(bt, "shell", side_effect=AssertionError("pkg-config rerun")):
            cached = bt.DirectoryConfig.get(bt.Path("."), cfg)
        self.assertEqual(cached.buildvars, directory.buildvars)

    def test_dependency_encounter_order_survives_metadata_round_trip(self):
        fs = bt.MemoryFileSystem()
        fs.write_text("main.cc", "")
        cfg = bt.BuildConfig(vfs=fs, LDFLAGS=[])
        source = bt.SourceFile.get(bt.Path("main.cc"), cfg)
        fs.makedirs(source.makefile.parent)
        headers = ["z/header.h", "a/header.h", "m/header.h"]
        for header in headers:
            dirname = bt.Path(header).parent
            fs.makedirs(dirname)
            fs.write_text(header, "")
            fs.write_text(dirname / "BUILD.py", f"LDFLAGS = ['-l{dirname}']")
        fs.write_text(source.makefile, "main.o: " + " ".join(headers + headers))
        source.header_deps = {}
        source.process_makefile_deps()
        source.update(cfg)
        metadata = json.loads(fs.read_text(source.infofile))
        self.assertEqual(metadata["deps"], ["include:" + header for header in headers])
        target = bt.Target(bt.Path("main"), cfg)
        for header in source.header_deps:
            header.build(target)
        self.assertEqual(target.get_linkflags(), ["-lz", "-la", "-lm"])

        cfg.reset_build_state()
        source = bt.SourceFile.get(bt.Path("main.cc"), cfg)
        source.check_up_to_date(cfg)
        target = bt.Target(bt.Path("main"), cfg)
        asyncio.run(source.build_deps(target, cfg))
        self.assertEqual(target.get_linkflags(), ["-lz", "-la", "-lm"])

    def test_old_unordered_directory_cache_is_regenerated(self):
        fs = bt.MemoryFileSystem()
        fs.write_text("BUILD.py", 'CFLAGS = ["-Iz", "-Ia"]\nPKGCONFIG = ["package"]\n')
        cfg = bt.BuildConfig(vfs=fs)
        fs.makedirs(cfg.DEPDIR)
        fs.write_text(cfg.DEPDIR / "buildvars.json", json.dumps({
            "CFLAGS": ["-Ia", "-Iz"], "PKGCONFIG": ["package"],
        }))
        with mock.patch.object(bt, "shell", return_value="") as pkg:
            directory = bt.DirectoryConfig.get(bt.Path("."), cfg)
            self.assertEqual(pkg.call_count, 2)
        self.assertEqual(directory.buildvars["CFLAGS"], ["-Iz", "-Ia"])


if __name__ == "__main__":
    unittest.main()
