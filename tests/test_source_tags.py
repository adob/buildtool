"""Arbitrary filename tags control selection without implicit OS relationships."""

import contextlib
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import buildtool as bt


class SourceTagTests(unittest.TestCase):
    def setUp(self) -> None:
        """Create platform variants and tagged tests in a virtual directory."""
        # These scheduling tests use simulated compilers without executable files.
        self.enterContext(mock.patch.object(bt.BuildConfig, "compiler_identity",
                                            return_value=["/fake/compiler", 1]))
        self.fs = bt.MemoryFileSystem(cwd='/workspace')
        self.fs.makedirs('serial')
        for name in ('common.cc', 'serial+linux.cc', 'usbio+zephyr+posix.cc',
                     'serial_test+linux.cpp', 'usbio_test+zephyr+posix.cc'):
            self.fs.write_text('serial/' + name, '')

    def test_all_qualifiers_required_and_underscores_ordinary(self) -> None:
        """Every +tag must be active; underscores and unqualified files never constrain selection."""
        cfg = bt.BuildConfig(vfs=self.fs, TAGS={'zephyr', 'posix', 'custom-board'})
        for filename, expected in (
                ('usbio+zephyr+posix.cc', True), ('usbio+zephyr+linux.cc', False),
                ('serial+linux.cpp', False), ('startup+zephyr.S', True),
                ('code+custom-board.c', True), ('code+experimental.cc', False),
                ('code_experimental.cc', True), ('serial_linux.cc', True),
                ('code_zephyr.cc', True), ('ordinary.cc', True)):
            with self.subTest(filename=filename):
                self.assertEqual(bt.source_matches_target(bt.Path(filename), cfg), expected)

    def test_no_implicit_relationship_between_tags(self) -> None:
        """Enabling zephyr does not implicitly enable posix; callers can enable both."""
        cfg = bt.BuildConfig(vfs=self.fs, TAGS={'zephyr'})
        self.assertFalse(bt.source_matches_target(bt.Path('file+posix.cc'), cfg))
        self.assertFalse(bt.source_matches_target(bt.Path('file+zephyr+posix.cc'), cfg))
        cfg = bt.BuildConfig(vfs=self.fs, TAGS={'zephyr', 'posix'})
        self.assertTrue(bt.source_matches_target(bt.Path('file+zephyr+posix.cc'), cfg))

    def test_native_defaults_and_explicit_empty_tags(self) -> None:
        """Native defaults include posix where applicable; an empty set overrides them."""
        for platform, tags in (('linux', {'linux', 'posix'}), ('win32', {'windows'}),
                               ('darwin', {'darwin', 'posix'}), ('freebsd14', {'freebsd', 'posix'})):
            with mock.patch.object(sys, 'platform', platform):
                self.assertEqual(bt.BuildConfig(vfs=self.fs).TAGS, tags)
                self.assertEqual(bt.BuildConfig(vfs=self.fs, TAGS=set()).TAGS, set())

    def test_optional_known_tags_detect_typo(self) -> None:
        """Arbitrary inactive tags are skipped unless the caller opts into name validation."""
        cfg = bt.BuildConfig(vfs=self.fs, TAGS={'zephyr'})
        self.assertFalse(bt.source_matches_target(bt.Path('file+zephry.cc'), cfg))
        cfg = bt.BuildConfig(vfs=self.fs, TAGS={'zephyr'}, KNOWN_TAGS={'zephyr', 'posix'})
        self.assertFalse(bt.source_matches_target(bt.Path('file+posix.cc'), cfg))
        with self.assertRaisesRegex(ValueError, 'Unknown build tags'):
            bt.source_matches_target(bt.Path('file+zephry.cc'), cfg)
        with self.assertRaisesRegex(ValueError, 'Unknown active build tags'):
            bt.BuildConfig(vfs=self.fs, TAGS={'zephry'}, KNOWN_TAGS={'zephyr'})

    def test_invalid_names_rejected(self) -> None:
        """Reject malformed qualifiers and accidental string-valued configuration."""
        for tags in ({'bad tag'}, {''}, {'../path'}, 'linux'):
            with self.subTest(tags=tags), self.assertRaises(ValueError):
                bt.BuildConfig(vfs=self.fs, TAGS=tags)
        for filename in ('file+.cc', 'file++linux.cc'):
            with self.assertRaises(ValueError):
                bt.source_matches_target(bt.Path(filename), bt.BuildConfig(vfs=self.fs))

    def test_directory_and_test_selection(self) -> None:
        """Tags appear after the _test suffix and work for both C++ extensions."""
        for tags, source, test in (
                ({'linux'}, 'serial+linux.cc', 'serial_test+linux.cpp'),
                ({'zephyr', 'posix'}, 'usbio+zephyr+posix.cc', 'usbio_test+zephyr+posix.cc')):
            cfg = bt.BuildConfig(vfs=self.fs, TAGS=tags)
            self.assertEqual([p.name for p in bt.directory_sources(bt.Path('serial'), cfg)], ['common.cc', source])
            self.assertEqual([p.name for p in bt.directory_sources(bt.Path('serial'), cfg, tests=True)], [test])

    def test_recursive_selection_skips_inactive_directories(self) -> None:
        """A directory containing only inactive tagged sources is not a recursive target."""
        self.fs.makedirs('inactive')
        self.fs.write_text('inactive/only+zephyr.cc', '')
        cfg = bt.BuildConfig(vfs=self.fs, TAGS={'linux'})
        self.assertEqual(list(map(str, bt.expand_target_pattern(bt.Path('./...'), cfg))), ['serial'])

    def test_explicit_inactive_source_fails_before_compilation(self) -> None:
        """An explicit file request must still respect the active tags."""
        cfg = bt.BuildConfig(vfs=self.fs, TAGS={'linux'})
        with self.assertRaisesRegex(RuntimeError, 'inactive build tags'):
            bt.SourceFile.get(bt.Path('serial/usbio+zephyr+posix.cc'), cfg)

    def test_header_companions_obey_tags(self) -> None:
        """An included tagged header only schedules its matching active source."""
        dep = bt.HeaderDep(bt.Path('serial/usbio+zephyr+posix.h'))
        self.assertIsNone(dep.find_cpp(dep.path, bt.BuildConfig(vfs=self.fs, TAGS={'linux'})))
        self.assertEqual(dep.find_cpp(dep.path, bt.BuildConfig(vfs=self.fs, TAGS={'zephyr', 'posix'})),
                         bt.Path('serial/usbio+zephyr+posix.cc'))

    def test_ide_database_excludes_inactive_sources(self) -> None:
        """IDE entries use the same tags as actual builds."""
        cfg = bt.BuildConfig(vfs=self.fs, TAGS={'linux'})
        with mock.patch.object(bt.CompilationDatabase, 'add_standard_modules', new=mock.AsyncMock()), \
             contextlib.redirect_stdout(io.StringIO()):
            entries = json.loads(bt.make_compilation_database([bt.Path('serial')], cfg))
        self.assertEqual(len(entries), 3)
        self.assertFalse(any('zephyr' in entry['file'] for entry in entries))

    def test_tag_change_invalidates_cached_dependencies(self) -> None:
        """Tags are part of incremental metadata even when compiler flags stay the same."""
        first = bt.BuildConfig(vfs=self.fs, TAGS={'zephyr', 'posix'})
        source = bt.SourceFile.get(bt.Path('serial/common.cc'), first)
        self.fs.makedirs(source.infofile.parent, exist_ok=True)
        source.update(first)
        for tags, recompile in ((['posix', 'zephyr'], False), (['linux'], True)):
            cfg = bt.BuildConfig(vfs=self.fs, TAGS=tags)
            checked = bt.SourceFile.get(source.path, cfg)
            checked.check_up_to_date(cfg)
            self.assertEqual(checked.need_recompile, recompile)

    def test_cli_replaces_tags_and_normalizes_artifact_identity(self) -> None:
        """CLI tags override launcher defaults; their ordering does not change output paths."""
        artifacts = []
        for flags, expected in (([], {'zephyr', 'posix'}),
                                (['--tags', 'posix,zephyr'], {'zephyr', 'posix'}),
                                (['--tags', 'linux,posix'], {'linux', 'posix'}),
                                (['--tags', ''], set())):
            with mock.patch.dict(vars(bt)), mock.patch.object(bt, 'native_tags', return_value=frozenset({'linux', 'posix'})), \
                 mock.patch.object(bt, 'ROOT', '.'), \
                 mock.patch.object(sys, 'argv', ['bt', 'build', *flags, 'serial']), \
                 mock.patch.object(bt, 'build') as build:
                bt.main(vfs=self.fs, TAGS={'zephyr', 'posix'})
            cfg = build.call_args.args[1]
            self.assertEqual(cfg.TAGS, expected)
            artifacts.append(str(cfg.OBJDIR))
        self.assertEqual(artifacts[0], artifacts[1])
        self.assertEqual(artifacts[2], 'build/release')
        self.assertEqual(len(set(artifacts)), 3)


class RealSourceTagTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get('BT_TEST_GCC'), 'set BT_TEST_GCC for real compilation')
    def test_tagged_build_and_test_sources(self) -> None:
        """Compile matching sources and tagged tests while rejecting inactive files before GCC sees them."""
        with tempfile.TemporaryDirectory(prefix='buildtool-tags-') as directory:
            previous = os.getcwd()
            os.chdir(directory)
            try:
                Path('pkg').mkdir()
                Path('pkg/main.cc').write_text('int selected(); int main() { return selected() != 42; }\n')
                Path('pkg/driver+zephyr+posix.cc').write_text('int selected() { return 42; }\n')
                Path('pkg/driver+linux.cc').write_text('#error inactive Linux implementation\n')
                Path('pkg/driver_test+zephyr+posix.cpp').write_text('int check() { return 0; }\n')
                Path('pkg/driver_test+linux.cc').write_text('#error inactive Linux test\n')
                Path('runner.cc').write_text('int check(); int main() { return check(); }\n')
                cfg = bt.BuildConfig(CXX=os.environ['BT_TEST_GCC'], CXXFLAGS=['-std=c++20'],
                    TAGS={'zephyr', 'posix'}, STD_HEADER_UNIT=False,
                    LDFLAGS=shlex.split(os.environ.get('BT_TEST_GCC_LDFLAGS', '')))
                with contextlib.redirect_stdout(io.StringIO()), mock.patch.object(bt, 'ROOT', directory), \
                     mock.patch.object(bt, 'TESTMAIN', 'runner.cc'):
                    binary = bt.build(bt.Path('pkg'), cfg, publish=False)
                    subprocess.run([str(Path(str(binary)).resolve())], check=True)
                    bt.run_tests(['pkg/...'], cfg)
                self.assertTrue(Path('build/release/pkg/driver+zephyr+posix.o').exists())
                self.assertFalse(Path('build/release/pkg/driver+linux.o').exists())
                self.assertTrue(Path('build/release/pkg/driver_test+zephyr+posix.o').exists())
            finally:
                os.chdir(previous)
