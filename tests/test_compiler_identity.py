"""Compiler replacements invalidate objects and module metadata before reuse."""

import json
import os
import unittest
from unittest import mock

import buildtool as bt


class CompilerIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        """Install two virtual compilers behind one PATH-visible symlink."""
        self.fs = bt.MemoryFileSystem()
        self.fs.makedirs('/bin')
        self.fs.makedirs('/compilers')
        self.fs.write_text('/compilers/gcc15', 'compiler')
        self.fs.write_text('/compilers/gcc16', 'compiler')
        self.fs.symlink('/compilers/gcc15', '/bin/g++')
        self.enterContext(mock.patch.dict(os.environ, {'PATH': '/bin'}))
        self.cfg = bt.BuildConfig(vfs=self.fs, CXX='g++', CC='g++', INCFLAGS=[])

    def cached_source(self, kind: bt.SourceType) -> bt.SourceFile:
        """Write successful build metadata for kind using the current compiler."""
        self.fs.write_text('unit.cc', '')
        source = bt.SourceFile(bt.Path('unit.cc'), kind, None, self.cfg)
        self.fs.makedirs(source.infofile.parent, exist_ok=True)
        self.fs.write_text(source.infofile, json.dumps({
            'command': source.compiler_cmd(self.cfg),
            'compiler_identity': self.cfg.compiler_identity('g++'),
            'tags': sorted(self.cfg.TAGS),
            'deps': [],
        }))
        return source

    def test_replacement_invalidates_all_source_kinds(self) -> None:
        """Both symlink changes and in-place updates invalidate each artifact kind."""
        for kind in (bt.SourceType.C, bt.SourceType.ASM, bt.SourceType.CPP, bt.SourceType.MODULE,
                     bt.SourceType.USER_HEADER, bt.SourceType.SYSTEM_HEADER):
            for change in ('path', 'mtime', 'legacy'):
                with self.subTest(kind=kind, change=change):
                    source = self.cached_source(kind)
                    if change == 'path':
                        current = self.fs.realpath('/bin/g++')
                        other = '/compilers/gcc16' if current.endswith('gcc15') else '/compilers/gcc15'
                        self.fs.unlink('/bin/g++')
                        self.fs.symlink(other, '/bin/g++')
                    elif change == 'mtime':
                        self.fs.write_text('/bin/g++', 'compiler')
                    else:
                        data = json.loads(self.fs.read_text(source.infofile))
                        del data['compiler_identity']
                        self.fs.write_text(source.infofile, json.dumps(data))
                    self.cfg.reset_build_state()
                    source.check_up_to_date(self.cfg)
                    self.assertTrue(source.need_recompile)
                    self.assertFalse(source.up_to_date)

    def test_metadata_writer_records_identity(self) -> None:
        """Successful source updates persist identity for the next invocation."""
        source = self.cached_source(bt.SourceType.CPP)
        source.update(self.cfg)
        data = json.loads(self.fs.read_text(source.infofile))
        self.assertEqual(data['compiler_identity'], self.cfg.compiler_identity('g++'))

    def test_unchanged_compiler_preserves_cached_output(self) -> None:
        """An unchanged executable keeps the source eligible for dependency checks."""
        source = self.cached_source(bt.SourceType.CPP)
        self.cfg.reset_build_state()
        source.check_up_to_date(self.cfg)
        self.assertFalse(source.need_recompile)

    def test_identity_cached_until_next_build(self) -> None:
        """Repeated lookups reuse identity; a new build observes replacements."""
        original = self.cfg.compiler_identity('g++')
        self.assertEqual(original[0], '/compilers/gcc15')
        self.fs.write_text('/bin/g++', 'new compiler')
        self.assertEqual(self.cfg.compiler_identity('g++'), original)
        self.cfg.reset_build_state()
        self.assertNotEqual(self.cfg.compiler_identity('g++'), original)

    def test_missing_compiler_fails(self) -> None:
        """A missing executable cannot acquire a reusable cache identity."""
        self.fs.unlink('/bin/g++')
        with self.assertRaises(FileNotFoundError):
            self.cfg.compiler_identity('g++')
