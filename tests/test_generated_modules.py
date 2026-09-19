"""Generated module sources materialize lazily from BUILD.py declarations."""

import asyncio
import subprocess
import unittest
from unittest import mock

import buildtool as bt


class GeneratedModuleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fs = bt.MemoryFileSystem()
        self.fs.makedirs("pkg/generated")
        self.fs.makedirs("/tools")
        self.fs.write_text("/tools/gen", "tool")
        self.fs.write_text("/tools/protoc", "protoc")
        self.fs.write_text("pkg/generated/schema.txt", "42\n")
        self.fs.write_text(
            "pkg/generated/BUILD.py",
            """GENERATED = [{
    "inputs": ["schema.txt"],
    "outputs": ["foo.cc"],
    "tools": ["/tools/protoc"],
    "command": ["/tools/gen", "schema.txt", "{outdir}/foo.cc"],
}]
""",
        )
        self.cfg = bt.BuildConfig(
            vfs=self.fs,
            SRCDIR=".",
            INCFLAGS=["-I."],
            OBJDIR="build/release",
            DEPDIR="build/release",
            CXXFLAGS=[],
            LDFLAGS=[],
            STD_HEADER_UNIT=False,
            JOBS=2,
        )
        self.invocations: list[list[str]] = []

    async def run_generator(
        self, job: bt.Job, command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        """Simulate the declared generator and materialize its output in the VFS."""
        self.invocations.append(list(map(str, command)))
        self.assertEqual(kwargs.get("cwd"), "/workspace/pkg/generated")
        output = bt.Path(command[-1])
        value = self.fs.read_text("pkg/generated/schema.txt").strip()
        self.fs.write_text(
            output,
            f"export module pkg.generated.foo;\n"
            f"export int answer() {{ return {value}; }}\n",
        )
        return subprocess.CompletedProcess(command, 0, b"", b"")

    def resolve(self) -> bt.Path:
        """Resolve one module through a real BuildSession and return its source path."""
        target = bt.Target(bt.Path("main"), self.cfg)

        async def run() -> bt.Path:
            graph = bt.CompilationGraph(self.cfg)
            target.session = graph.session

            result: bt.Path | None = None

            async def import_module(job: bt.Job) -> None:
                nonlocal result
                result = await target.resolve_module_source(
                    "pkg.generated.foo", bt.SourceType.MODULE, None, job
                )

            try:
                root = graph.session.schedule("import", import_module)
                await graph.session.finish()
                if root.error:
                    raise root.error
                assert result is not None
                return result
            finally:
                await graph.session.close()
                target.session = None

        return asyncio.run(run())

    def test_missing_module_is_generated_into_configuration_tree(self) -> None:
        """A mapper-time module miss loads its parent BUILD.py and runs its producer."""
        with mock.patch.object(bt, "run_compiler", side_effect=self.run_generator):
            path = self.resolve()

        self.assertEqual(
            path, bt.Path("build/release/generated/pkg/generated/foo.cc")
        )
        self.assertEqual(len(self.invocations), 1)
        self.assertTrue(path.is_file(self.fs))
        source = bt.SourceFile.get(
            path,
            self.cfg,
            type=bt.SourceType.MODULE,
            modname="pkg.generated.foo",
        )
        self.assertEqual(source.logical_path, bt.Path("pkg/generated/foo.cc"))
        self.assertEqual(source.dirname, bt.Path("pkg/generated"))
        self.assertEqual(source.objpath, bt.Path("build/release/pkg/generated/foo.o"))
        self.assertIn("-iquotepkg/generated", source.compiler_extra_args())

    def test_generated_action_is_cached_and_input_change_invalidates_it(self) -> None:
        """The action cache covers command/tool identity and declared input contents."""
        with mock.patch.object(bt, "run_compiler", side_effect=self.run_generator):
            first = self.resolve()
            self.cfg.reset_build_state()
            second = self.resolve()
            self.assertEqual(first, second)
            self.assertEqual(len(self.invocations), 1)

            self.fs.write_text("pkg/generated/schema.txt", "43\n")
            self.cfg.reset_build_state()
            third = self.resolve()

        self.assertEqual(third, first)
        self.assertEqual(len(self.invocations), 2)
        self.assertIn("return 43", self.fs.read_text(third))

        with mock.patch.object(bt, "run_compiler", side_effect=self.run_generator):
            self.fs.write_text("/tools/protoc", "updated protoc")
            self.cfg.reset_build_state()
            self.resolve()
        self.assertEqual(len(self.invocations), 3)

        with mock.patch.object(bt, "run_compiler", side_effect=self.run_generator):
            self.fs.write_text("/tools/gen", "updated tool")
            self.cfg.reset_build_state()
            self.resolve()
        self.assertEqual(len(self.invocations), 4)

    def test_generated_header_and_companion_metadata_do_not_collide(self) -> None:
        """Generated foo.pb.h and foo.pb.cc retain distinct cache and BMI paths."""
        header = bt.Path("build/release/generated/pkg/generated/foo.pb.h")
        source = bt.Path("build/release/generated/pkg/generated/foo.pb.cc")
        self.fs.makedirs(header.parent, exist_ok=True)
        self.fs.write_text(header, "#pragma once\n")
        self.fs.write_text(source, "")
        self.cfg.generated_logical_paths[header] = bt.Path("pkg/generated/foo.pb.h")
        self.cfg.generated_logical_paths[source] = bt.Path("pkg/generated/foo.pb.cc")

        hfile = bt.SourceFile.get(
            header,
            self.cfg,
            type=bt.SourceType.USER_HEADER,
            modname="./build/release/generated/pkg/generated/foo.pb.h",
        )
        ccfile = bt.SourceFile.get(source, self.cfg, type=bt.SourceType.CPP)

        self.assertEqual(hfile.cmpath, bt.Path("build/release/pkg/generated/foo.pb.h.pcm"))
        self.assertEqual(hfile.infofile, bt.Path("build/release/pkg/generated/foo.pb.h.info"))
        self.assertEqual(ccfile.infofile, bt.Path("build/release/pkg/generated/foo.pb.cc.info"))
        self.assertNotEqual(hfile.infofile, ccfile.infofile)

    def test_source_tree_module_precedes_generated_declaration(self) -> None:
        """An existing ordinary module wins without loading or running its generator."""
        self.fs.write_text(
            "pkg/generated/foo.cc",
            "export module pkg.generated.foo;\nexport int answer() { return 7; }\n",
        )
        target = bt.Target(bt.Path("main"), self.cfg)
        self.assertEqual(
            target.mod2src("pkg.generated.foo", bt.SourceType.MODULE),
            bt.Path("pkg/generated/foo.cc"),
        )
        self.assertEqual(self.cfg.generated_outputs, {})

    def test_ancestor_build_may_generate_nested_module_candidate(self) -> None:
        """An ancestor BUILD.py may generate a normal nested module candidate."""
        self.fs.write_text(
            "pkg/generated/BUILD.py",
            """GENERATED = [{
    "outputs": ["foo/client.cc"],
    "command": ["/tools/gen", "{outdir}/foo/client.cc"],
}]
""",
        )

        async def generate(
            job: bt.Job, command: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[bytes]:
            self.fs.makedirs(bt.Path(command[-1]).parent, exist_ok=True)
            self.fs.write_text(
                command[-1],
                "export module pkg.generated.foo.client;\nexport int answer() { return 42; }\n",
            )
            return subprocess.CompletedProcess(command, 0, b"", b"")

        with mock.patch.object(bt, "run_compiler", side_effect=generate):
            target = bt.Target(bt.Path("main"), self.cfg)

            async def run() -> bt.Path:
                graph = bt.CompilationGraph(self.cfg)
                target.session = graph.session
                result: bt.Path | None = None

                async def resolve(job: bt.Job) -> None:
                    nonlocal result
                    result = await target.resolve_module_source(
                        "pkg.generated.foo.client", bt.SourceType.MODULE, None, job
                    )

                try:
                    root = graph.session.schedule("resolve", resolve)
                    await graph.session.finish()
                    if root.error:
                        raise root.error
                    assert result is not None
                    return result
                finally:
                    await graph.session.close()
                    target.session = None

            path = asyncio.run(run())

        self.assertEqual(
            path, bt.Path("build/release/generated/pkg/generated/foo/client.cc")
        )
        source = bt.SourceFile.get(
            path,
            self.cfg,
            type=bt.SourceType.MODULE,
            modname="pkg.generated.foo.client",
        )
        self.assertEqual(source.logical_path, bt.Path("pkg/generated/foo/client.cc"))

    def test_tagged_partition_fallback_may_be_generated(self) -> None:
        """An explicit :tag import can generate the sibling primary+tag.cc layout."""
        self.fs.makedirs("lib/sync")
        self.fs.write_text(
            "lib/sync/BUILD.py",
            """GENERATED = [{
    "outputs": ["cond+teensy.cc"],
    "command": ["/tools/gen", "{outdir}/cond+teensy.cc"],
}]
""",
        )
        self.cfg.TAGS = {"linux"}

        async def generate(
            job: bt.Job, command: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[bytes]:
            self.fs.write_text(
                command[-1],
                "export module lib.sync.cond:teensy;\nexport int value() { return 1; }\n",
            )
            return subprocess.CompletedProcess(command, 0, b"", b"")

        target = bt.Target(bt.Path("main"), self.cfg)

        async def run() -> bt.Path:
            graph = bt.CompilationGraph(self.cfg)
            target.session = graph.session
            result: bt.Path | None = None

            async def resolve(job: bt.Job) -> None:
                nonlocal result
                result = await target.resolve_module_source(
                    "lib.sync.cond:teensy", bt.SourceType.MODULE, None, job
                )

            try:
                root = graph.session.schedule("partition", resolve)
                await graph.session.finish()
                if root.error:
                    raise root.error
                assert result is not None
                return result
            finally:
                await graph.session.close()
                target.session = None

        with mock.patch.object(bt, "run_compiler", side_effect=generate):
            path = asyncio.run(run())
        self.assertEqual(
            path, bt.Path("build/release/generated/lib/sync/cond+teensy.cc")
        )
        source = bt.SourceFile.get(
            path,
            self.cfg,
            type=bt.SourceType.MODULE,
            modname="lib.sync.cond:teensy",
        )
        self.assertEqual(source.logical_path, bt.Path("lib/sync/cond+teensy.cc"))

    def test_duplicate_generated_output_is_rejected(self) -> None:
        """One logical generated output may have only one producer."""
        self.fs.write_text(
            "pkg/generated/BUILD.py",
            """GENERATED = [
    {"outputs": ["foo.cc"], "command": ["/tools/gen", "{outdir}/foo.cc"]},
    {"outputs": ["foo.cc"], "command": ["/tools/gen", "{outdir}/foo.cc"]},
]
""",
        )
        with self.assertRaisesRegex(ValueError, "multiple producers"):
            bt.DirectoryConfig.get(bt.Path("pkg/generated"), self.cfg)

    def test_directory_config_disabled_does_not_load_generator_declarations(self) -> None:
        """CMake-style consumers keep treating BUILD.py as completely out of band."""
        self.fs.write_text("pkg/generated/BUILD.py", 'raise RuntimeError("loaded")\n')
        cfg = bt.BuildConfig(
            vfs=self.fs,
            SRCDIR=".",
            INCFLAGS=["-I."],
            USE_DIRECTORY_CONFIG=False,
        )
        target = bt.Target(bt.Path("main"), cfg)

        async def run() -> None:
            graph = bt.CompilationGraph(cfg)
            target.session = graph.session

            async def resolve(job: bt.Job) -> None:
                with self.assertRaisesRegex(RuntimeError, "Unable to locate module"):
                    await target.resolve_module_source(
                        "pkg.generated.foo", bt.SourceType.MODULE, None, job
                    )

            try:
                root = graph.session.schedule("resolve", resolve)
                await graph.session.finish()
                if root.error:
                    raise root.error
            finally:
                await graph.session.close()
                target.session = None

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
