"""Build scheduling tests; no installed C++ compiler is required."""

import json
import unittest
from unittest import mock

import buildtool as bt


class BuildDecisionTests(unittest.TestCase):
    def setUp(self):
        self.fs = bt.MemoryFileSystem()
        self.fs.write_text("main.cc", "")
        self.cfg = bt.BuildConfig(vfs=self.fs)
        self.source = bt.SourceFile(
            bt.Path("main.cc"), bt.SourceType.CPP, None, self.cfg
        )
        self.source.up_to_date = False
        self.source.need_recompile = False
        self.source.deps = {bt.ModuleDep("value", "old-hash")}
        self.source.header_deps = set()
        self.target = mock.Mock(cfg=self.cfg)
        self.events = []
        self.enterContext(mock.patch.object(self.source, "check_up_to_date"))
        self.enterContext(mock.patch.object(self.source, "dircfg"))
        self.compile = self.enterContext(mock.patch.object(
            self.source, "compile",
            side_effect=lambda *args: self.events.append("compile"),
        ))
        self.update = self.enterContext(mock.patch.object(
            self.source, "update",
            side_effect=lambda *args: self.events.append("update"),
        ))
        self.module = mock.Mock()
        self.enterContext(mock.patch.object(
            bt.CompiledModule, "get", return_value=self.module
        ))

    def build_with_dependency_hash(self, digest):
        def build_dependency(*args, **kwargs):
            self.events.append("dependency")
            return digest

        self.module.build.side_effect = build_dependency
        self.source.build(self.target, self.cfg)

    def test_changed_module_is_checked_before_importer_recompile(self):
        self.build_with_dependency_hash("new-hash")
        self.assertEqual(self.events, ["dependency", "compile", "update"])
        self.compile.assert_called_once_with(self.target, self.cfg)
        self.update.assert_called_once_with(self.cfg)

    def test_unchanged_module_does_not_recompile_importer(self):
        self.build_with_dependency_hash("old-hash")
        self.assertEqual(self.events, ["dependency"])
        self.compile.assert_not_called()
        self.update.assert_not_called()


class IncrementalModuleTests(unittest.TestCase):
    """Use in-memory files and real build metadata, replacing the compiler backend.

    Fake .cc inputs are JSON: imports name modules, and value is an exported
    integer. The fake compiler bakes imported values into objects. This makes
    stale compilation observable without parsing C++ or spawning GCC.
    """

    def setUp(self):
        self.fs = bt.MemoryFileSystem()
        self.cfg = bt.BuildConfig(
            CXX="fake-c++", OBJDIR="obj", INCFLAGS=[], SRCDIR=".", vfs=self.fs
        )
        self.calls = []
        self.fail_source = None
        self.enterContext(mock.patch.object(
            bt.SourceFile, "compile_gcc", autospec=True, side_effect=self.fake_compile
        ))

    def write(self, path, text, *, vfs):
        vfs.makedirs(path.parent, exist_ok=True)
        vfs.write_text(path, text)

    def source(self, name, **contents):
        self.write(bt.Path(name + ".cc"), json.dumps(contents), vfs=self.fs)

    def fake_compile(self, source, target, cfg):
        name = str(source.path)
        if name == self.fail_source:
            raise RuntimeError("compiler failed: " + name)
        contents = json.loads(cfg.vfs.read_text(source.path))
        value = contents.get("value", 0)
        source.deps = set()
        for imported in contents.get("imports", []):
            module = bt.CompiledModule.get(imported, cfg)
            digest = module.build(target, inherited_dircfg=source.dircfg())
            source.deps.add(bt.ModuleDep(imported, digest))
            value += int(cfg.vfs.read_text(module.cmpath))
        self.calls.append(name)
        self.write(source.objpath, json.dumps({"value": value, "source": contents}), vfs=cfg.vfs)
        if source.type == bt.SourceType.MODULE:
            self.write(source.cmpath, str(value), vfs=cfg.vfs)

    def build(self):
        self.cfg.reset_build_state()
        self.calls = []
        target = bt.Target(bt.Path("main"), self.cfg)
        target.compile(bt.Path("main.cc"))
        return json.loads(self.fs.read_text("obj/main.o"))["value"]

    def assert_recorded_module_hash(self, importer, module):
        metadata = json.loads(self.fs.read_text(f"obj/{importer}.info"))
        digest = bt.sha256_file(bt.Path(f"obj/{module}.pcm"), self.fs)
        self.assertIn(f"module:{module}@{digest}", metadata["deps"])

    def test_build_does_not_access_host_files_or_start_a_compiler(self):
        self.source("value", value=1)
        self.source("main", imports=["value"])
        with mock.patch("builtins.open", side_effect=AssertionError("host open")), \
             mock.patch("os.stat", side_effect=AssertionError("host stat")), \
             mock.patch("os.makedirs", side_effect=AssertionError("host mkdir")), \
             mock.patch("os.replace", side_effect=AssertionError("host replace")), \
             mock.patch.object(bt.subprocess, "Popen", side_effect=AssertionError("compiler")):
            self.assertEqual(self.build(), 1)
            self.source("value", value=2)
            self.assertEqual(self.build(), 2)
            self.assert_recorded_module_hash("main", "value")

    def test_two_filesystems_build_identical_module_names_independently(self):
        self.source("value", value=1)
        self.source("main", imports=["value"])
        other_fs = bt.MemoryFileSystem()
        other_cfg = bt.BuildConfig(CXX="fake-c++", OBJDIR="obj", vfs=other_fs)
        other_fs.write_text("value.cc", json.dumps({"value": 9}))
        other_fs.write_text("main.cc", json.dumps({"imports": ["value"]}))

        # Keep both configurations and their cached build objects alive.
        first = bt.Target(bt.Path("main"), self.cfg)
        first.compile(bt.Path("main.cc"))
        second = bt.Target(bt.Path("main"), other_cfg)
        second.compile(bt.Path("main.cc"))
        self.assertEqual(json.loads(self.fs.read_text("obj/main.o"))["value"], 1)
        self.assertEqual(json.loads(other_fs.read_text("obj/main.o"))["value"], 9)
        self.assertIsNot(bt.CompiledModule.get("value", self.cfg),
                         bt.CompiledModule.get("value", other_cfg))

        other_source = bt.SourceFile.get(bt.Path("main.cc"), other_cfg)
        other_module = bt.CompiledModule.get("value", other_cfg)
        self.source("value", value=2)
        self.assertEqual(self.build(), 2)
        self.assertIs(bt.SourceFile.get(bt.Path("main.cc"), other_cfg), other_source)
        self.assertIs(bt.CompiledModule.get("value", other_cfg), other_module)
        self.calls = []
        bt.Target(bt.Path("main"), other_cfg).compile(bt.Path("main.cc"))
        self.assertEqual(self.calls, [])
        self.assertEqual(json.loads(other_fs.read_text("obj/main.o"))["value"], 9)

    def test_module_edit_recompiles_importer_and_updates_metadata(self):
        self.source("value", value=1)
        self.source("main", imports=["value"])
        self.assertEqual(self.build(), 1)
        self.assertEqual(self.calls, ["value.cc", "main.cc"])
        self.assertEqual(self.build(), 1)
        self.assertEqual(self.calls, [])
        old_object = self.fs.read_bytes("obj/main.o")

        self.source("value", value=2)
        self.assertEqual(self.build(), 2)
        self.assertEqual(self.calls, ["value.cc", "main.cc"])
        self.assertNotEqual(self.fs.read_bytes("obj/main.o"), old_object)
        self.assert_recorded_module_hash("main", "value")
        self.assertEqual(self.build(), 2)
        self.assertEqual(self.calls, [])

    def test_transitive_module_edit_rebuilds_chain(self):
        self.source("value", value=1)
        self.source("middle", imports=["value"])
        self.source("main", imports=["middle"])
        self.assertEqual(self.build(), 1)

        self.source("value", value=2)
        self.assertEqual(self.build(), 2)
        self.assertEqual(self.calls, ["value.cc", "middle.cc", "main.cc"])
        self.assert_recorded_module_hash("middle", "value")
        self.assert_recorded_module_hash("main", "middle")
        self.assertEqual(self.build(), 2)
        self.assertEqual(self.calls, [])

    def test_unchanged_module_interface_preserves_importer(self):
        self.source("value", value=1, implementation="before")
        self.source("main", imports=["value"])
        self.assertEqual(self.build(), 1)
        old_object = self.fs.read_bytes("obj/main.o")
        old_metadata = self.fs.read_bytes("obj/main.info")

        self.source("value", value=1, implementation="after")
        self.assertEqual(self.build(), 1)
        self.assertEqual(self.calls, ["value.cc"])
        self.assertEqual(self.fs.read_bytes("obj/main.o"), old_object)
        self.assertEqual(self.fs.read_bytes("obj/main.info"), old_metadata)

    def test_failed_recompile_does_not_publish_new_dependency_hash(self):
        self.source("value", value=1)
        self.source("main", imports=["value"])
        self.assertEqual(self.build(), 1)
        old_metadata = self.fs.read_bytes("obj/main.info")

        self.source("value", value=2)
        self.fail_source = "main.cc"
        with self.assertRaisesRegex(RuntimeError, "compiler failed: main.cc"):
            self.build()
        self.assertEqual(self.fs.read_bytes("obj/main.info"), old_metadata)

        self.fail_source = None
        self.assertEqual(self.build(), 2)
        self.assertEqual(self.calls, ["main.cc"])
        self.assert_recorded_module_hash("main", "value")


if __name__ == "__main__":
    unittest.main()
