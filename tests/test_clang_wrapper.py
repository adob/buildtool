"""Mapper protocol tests, plus opt-in end-to-end tests with patched Clang."""

import asyncio
import contextlib
import io
import json
import os
import shlex
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import buildtool as bt
from clang_mapper import compile_with_mapper


class ClangMapperTests(unittest.TestCase):
    def test_mapper_reply_includes_transitive_named_modules(self) -> None:
        """Importing std.compat must also tell ASTReader where to load std."""
        fs = bt.MemoryFileSystem(cwd='/workspace')
        fs.write_text('main.cc', '')
        cfg = bt.BuildConfig(vfs=fs, USECLANG=True)
        source = bt.SourceFile.get(bt.Path('main.cc'), cfg)
        module = mock.Mock(cmpath=bt.Path('build/std.compat.pcm'),
                           srcfile=mock.Mock(clang_module_files={'std': bt.Path('build/std.pcm')}))
        module.build = mock.AsyncMock(return_value='hash')
        with mock.patch.object(bt.CompiledModule, 'get', return_value=module):
            reply = asyncio.run(source.resolve_clang_request(
                {'kind': 'module', 'name': 'std.compat'}, bt.Target(bt.Path('main'), cfg), cfg))
        self.assertEqual(reply, {'pcm': '/workspace/build/std.compat.pcm', 'modules': {
            'std': '/workspace/build/std.pcm', 'std.compat': '/workspace/build/std.compat.pcm'}})

    def test_resolved_header_and_named_module_record_hash_dependencies(self):
        fs = bt.MemoryFileSystem(cwd="/workspace")
        fs.write_text("main.cc", "")
        cfg = bt.BuildConfig(vfs=fs, USECLANG=True, CLANG_WRAPPER="wrapper")
        source = bt.SourceFile.get(bt.Path("main.cc"), cfg)
        target = bt.Target(bt.Path("main"), cfg)
        for request, name in [
            ({"kind": "header", "path": '/sdk/a "quoted" header.h'}, '/sdk/a "quoted" header.h'),
            ({"kind": "module", "name": "math:detail"}, "math:detail"),
        ]:
            module = mock.Mock(cmpath=bt.Path("build/test.pcm"), srcfile=None)
            module.build = mock.AsyncMock(return_value="hash")
            with mock.patch.object(bt.CompiledModule, "get", return_value=module) as get:
                self.assertEqual(asyncio.run(source.resolve_clang_module(request, target, cfg)),
                                 "/workspace/build/test.pcm")
            get.assert_called_once_with(name, cfg,
                                        bt.SourceType.USER_HEADER if request['kind'] == 'header' else None)
            self.assertTrue(any(dep.name == name and dep.sha256 == "hash"
                                for dep in source.deps))

    def test_invalid_requests_are_rejected(self):
        fs = bt.MemoryFileSystem()
        fs.write_text("main.cc", "")
        cfg = bt.BuildConfig(vfs=fs)
        source = bt.SourceFile.get(bt.Path("main.cc"), cfg)
        for request in [None, {}, {"kind": "header", "path": "relative.h"},
                        {"kind": "module", "name": "../escape"}]:
            with self.subTest(request=request), self.assertRaises(ValueError):
                asyncio.run(source.resolve_clang_module(request, None, cfg))

    def test_header_command_keeps_package_flags_and_depfile(self):
        fs = bt.MemoryFileSystem()
        fs.write_text("BUILD.py", "CFLAGS = ['-DVALUE=42', '-I/sdk']")
        fs.makedirs("/sdk", exist_ok=True)
        fs.write_text("/sdk/a.h", "")
        cfg = bt.BuildConfig(vfs=fs, USECLANG=True, CLANG_WRAPPER="wrapper")
        directory = bt.DirectoryConfig.get(bt.Path("."), cfg)
        header = bt.SourceFile.get(bt.Path("/sdk/a.h"), cfg,
                                   type=bt.SourceType.SYSTEM_HEADER,
                                   modname="/sdk/a.h", inherited_dircfg=directory)
        command = header.compiler_cmd(cfg)
        for flag in ["-DVALUE=42", "-idirafter/sdk", "-MD", f"-MF{header.makefile}"]:
            self.assertIn(flag, command)
        self.assertEqual(command[:2], ["wrapper", "--"])

    def test_protocol_quotes_paths_and_propagates_resolver_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            wrapper = Path(tmp) / "fake-wrapper"
            # Exercise real pipes with a tiny fake frontend, without Clang.
            import sys
            wrapper.write_text(f"#!{sys.executable}\n" + '''
import json, os, sys
with os.fdopen(int(sys.argv[2])) as replies, os.fdopen(int(sys.argv[3]), "w") as requests:
    requests.write(json.dumps({"kind": "header", "path": '/a "quoted" header.h'}) + "\\n")
    requests.flush()
    reply = json.loads(replies.readline())
    if "error" in reply:
        sys.exit(1)
    assert reply["pcm"] == '/a "quoted" module.pcm'
''')
            wrapper.chmod(0o755)
            compile_with_mapper(str(wrapper), ["clang++", "-c", "main.cc"],
                                lambda request: '/a "quoted" module.pcm')
            def fail(request):
                raise RuntimeError("dependency failed")
            with self.assertRaisesRegex(RuntimeError, "dependency failed"):
                compile_with_mapper(str(wrapper), ["clang++", "-c", "main.cc"], fail)

    def test_depfile_escaping(self):
        self.assertEqual(bt.parse_makefile_rules(
            'out.o: source.cc dir/with\\ space.h hash\\#name.h dollar$$.h ' + '\\\n' + ' colon\\:name.h'),
            ['source.cc', 'dir/with space.h', 'hash#name.h', 'dollar$.h', 'colon:name.h'])


@unittest.skipUnless(os.environ.get("BT_TEST_CLANG_WRAPPER") and os.environ.get("BT_TEST_CLANG"),
                     "set BT_TEST_CLANG_WRAPPER and BT_TEST_CLANG for real compiler tests")
class ClangWrapperIntegrationTests(unittest.TestCase):
    def test_system_header_unit_warning_classification(self) -> None:
        """Suppress system input warnings while retaining user and opt-in warnings."""
        header = Path("narrow.h").resolve()
        header.write_text("inline short narrow(long value) { return value; }\n")
        for flavor, extra, warned in (("system", [], False),
                                       ("user", [], True),
                                       ("system", ["-Wsystem-headers"], True)):
            with self.subTest(flavor=flavor, extra=extra), \
                 open(os.devnull, "rb") as replies, open(os.devnull, "wb") as requests:
                result = subprocess.run([
                    self.cfg.CLANG_WRAPPER, "--mapper-fds", str(replies.fileno()),
                    str(requests.fileno()), "--", self.cfg.CXX, "-std=c++20",
                    "-Wconversion", *extra, "-xc++-header",
                    f"-fmodule-header={flavor}", str(header), "-o", "narrow.pcm",
                ], pass_fds=(replies.fileno(), requests.fileno()),
                    capture_output=True, text=True, check=False)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual("-Wimplicit-int-conversion" in result.stderr, warned,
                                 result.stderr)

    def test_pcm_codegen_honors_driver_warning_options(self) -> None:
        """Driver warnings must honor suppression and promotion to errors."""
        Path("example.cc").write_text("export module example;\nexport int value() { return 7; }\n")

        def compile_args(args: list[str]) -> subprocess.CompletedProcess[str]:
            """Run the wrapper with compiler args; this fixture makes no mapper requests."""
            with open(os.devnull, "rb") as replies, open(os.devnull, "wb") as requests:
                return subprocess.run([
                    self.cfg.CLANG_WRAPPER, "--mapper-fds", str(replies.fileno()),
                    str(requests.fileno()), "--", self.cfg.CXX, "-std=c++20", *args,
                ], pass_fds=(replies.fileno(), requests.fileno()),
                    capture_output=True, text=True, check=False)

        result = compile_args(["-x", "c++-module", "--precompile", "example.cc", "-o", "example.pcm"])
        self.assertEqual(result.returncode, 0, result.stderr)
        codegen = ["-x", "pcm", "-c", "example.pcm", "-o", "example.o", "-I."]
        result = compile_args(codegen)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("argument unused during compilation", result.stderr)
        result = compile_args([*codegen, "-Wno-unused-command-line-argument"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("argument unused during compilation", result.stderr)
        result = compile_args([*codegen, "-Werror=unused-command-line-argument"])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("argument unused during compilation", result.stderr)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="buildtool-clang-")
        self.addCleanup(self.tmp.cleanup)
        cwd = os.getcwd()
        os.chdir(self.tmp.name)
        self.addCleanup(os.chdir, cwd)
        self.cfg = bt.BuildConfig(
            CXX=os.environ["BT_TEST_CLANG"], USECLANG=True,
            CLANG_WRAPPER=os.environ["BT_TEST_CLANG_WRAPPER"],
            CXXFLAGS=["-std=c++20", "-Wno-experimental-header-units"],
            CFLAGS=[], LDFLAGS=shlex.split(os.environ.get("BT_TEST_CLANG_LDFLAGS", "")),
            INCFLAGS=["-I."])

    def build(self):
        self.cfg.reset_build_state()
        with contextlib.redirect_stdout(io.StringIO()):
            return bt.build(bt.Path("main.cc"), self.cfg)

    def run_binary(self, binary):
        return subprocess.run([os.path.abspath(binary)], check=False).returncode

    def test_recursive_module_discovery_and_rebuild(self) -> None:
        """Recursive roots and imports share one job regardless of discovery order."""
        Path("pkg").mkdir()
        Path("pkg/math.cc").write_text(
            'export module pkg.math;\nexport template<class T> T twice(T x) { return x+x; }\n')
        Path("pkg/use.cc").write_text(
            'import pkg.math;\nint use() { return twice(3); }\n')
        Path("pkg/plain.cc").write_text('int plain() { return 1; }\n')
        Path("main.cc").write_text('import pkg.math;\nint main() { return twice(3); }\n')
        for rebuild in (True, False, True):
            self.cfg.REBUILD = rebuild
            self.cfg.reset_build_state()
            with contextlib.redirect_stdout(io.StringIO()):
                bt.build_targets(bt.Path("pkg/..."), self.cfg)
            module = self.cfg.source_files[bt.Path("pkg/math.cc")]
            self.assertTrue(Path(module.objpath).is_file())
            self.assertTrue(Path(module.cmpath).is_file())
            plain = self.cfg.source_files[bt.Path("pkg/plain.cc")]
            self.assertTrue(Path(plain.objpath).is_file())
            self.assertFalse(Path(plain.cmpath).exists())
            self.assertEqual(self.run_binary(self.build()), 6)

    def test_removed_module_declaration_does_not_reuse_old_pcm(self) -> None:
        """A remaining PCM must not satisfy an import after its export is removed."""
        Path("math.cc").write_text('export module math;\nexport int value() { return 5; }\n')
        Path("main.cc").write_text('import math;\nint main() { return value(); }\n')
        self.assertEqual(self.run_binary(self.build()), 5)
        Path("math.cc").write_text('int value() { return 6; }\n')
        self.cfg.REBUILD = True
        with self.assertRaisesRegex(RuntimeError, 'does not export module math'):
            self.build()

    def test_partition_and_primary_implementation_outputs(self) -> None:
        """Partitions emit PCMs, whereas a primary implementation emits only an object."""
        Path("pkg/math").mkdir(parents=True)
        Path("pkg/math/bits.cc").write_text(
            'export module pkg.math:bits;\nexport int value() { return 5; }\n')
        Path("pkg/math.cc").write_text(
            'export module pkg.math;\nexport import :bits;\n')
        Path("pkg/impl.cc").write_text('module pkg.math;\nint internal() { return value(); }\n')
        with contextlib.redirect_stdout(io.StringIO()):
            bt.build_targets(bt.Path("pkg/..."), self.cfg)
        partition = self.cfg.source_files[bt.Path("pkg/math/bits.cc")]
        implementation = self.cfg.source_files[bt.Path("pkg/impl.cc")]
        self.assertTrue(Path(partition.cmpath).is_file())
        self.assertTrue(Path(implementation.objpath).is_file())
        self.assertFalse(Path(implementation.cmpath).exists())

    def test_nested_macro_import_named_module_and_incremental_rebuild(self):
        Path("leaf.h").write_text("#pragma once\ninline int leaf() { return 7; }\n")
        Path("config.h").write_text('#define USE_EXTRA 1\nimport "leaf.h";\n')
        Path("extra.h").write_text("inline int extra() { return 3; }\n")
        Path("math.cc").write_text('export module math;\nimport "leaf.h";\nexport int value() { return leaf(); }\n')
        Path("main.cc").write_text('''import <config.h>;
#if USE_EXTRA
import "extra.h";
#endif
import math;
int main() { return value() + extra(); }
''')
        binary = self.build()
        self.assertEqual(self.run_binary(binary), 10)
        before = {p: p.stat().st_mtime_ns for p in Path("build").rglob("*") if p.is_file()}
        self.build()
        self.assertEqual(before, {p: p.stat().st_mtime_ns for p in before})
        Path("leaf.h").write_text("#pragma once\ninline int leaf() { return 9; }\n")
        self.build()
        self.assertEqual(self.run_binary(binary), 12)
        # New imports are discovered during compilation, without a scan pass.
        Path("new.h").write_text("inline int added() { return 1; }\n")
        Path("extra.h").write_text('import "new.h";\ninline int extra() { return 3 + added(); }\n')
        self.build()
        self.assertEqual(self.run_binary(binary), 13)
        # A named module can preload a header-unit PCM before its direct
        # import; both routes must reuse the same module identity.
        text = Path("main.cc").read_text().replace('import math;\n', '')
        Path("main.cc").write_text('import math;\n' + text)
        self.build()
        self.assertEqual(self.run_binary(binary), 13)

    def test_textual_header_changes_rebuild_header_unit(self):
        Path("inner.h").write_text("inline int result() { return 5; }\n")
        Path("outer.h").write_text('#include "inner.h"\n')
        Path("main.cc").write_text('import "outer.h";\nint main() { return result(); }\n')
        binary = self.build()
        self.assertEqual(self.run_binary(binary), 5)
        Path("inner.h").write_text("inline int result() { return 6; }\n")
        self.build()
        self.assertEqual(self.run_binary(binary), 6)

    def test_cycle_fails_and_can_be_fixed(self):
        Path("a.h").write_text('import "b.h";\n')
        Path("b.h").write_text('import "a.h";\n')
        Path("main.cc").write_text('import "a.h";\nint main() {}\n')
        with self.assertRaisesRegex(RuntimeError, "Cyclic module import"):
            self.build()
        self.assertFalse(Path("build/release/main.info").exists())
        Path("b.h").write_text("inline int value = 1;\n")
        self.assertEqual(self.run_binary(self.build()), 0)

    def test_compiler_error_leaves_no_success_metadata(self):
        Path("bad.h").write_text("this is invalid C++;\n")
        Path("main.cc").write_text('import "bad.h";\nint main() {}\n')
        with self.assertRaises(subprocess.CalledProcessError):
            self.build()
        self.assertFalse(Path("build/release/main.info").exists())

    def test_header_paths_with_spaces_and_system_lookup(self):
        Path("system headers").mkdir()
        Path("system headers/inner file.h").write_text('inline int result() { return 5; }\n')
        Path("system headers/outer file.h").write_text('#include "inner file.h"\n')
        self.cfg.INCFLAGS += ["-isystem", "system headers"]
        Path("main.cc").write_text('import <outer file.h>;\nint main() { return result(); }\n')
        binary = self.build()
        self.assertEqual(self.run_binary(binary), 5)
        headers = [f for f in self.cfg.source_files.values()
                   if f.type == bt.SourceType.SYSTEM_HEADER]
        self.assertEqual(len(headers), 1)
        Path("system headers/inner file.h").write_text('inline int result() { return 6; }\n')
        self.build()
        self.assertEqual(self.run_binary(binary), 6)
