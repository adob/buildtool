"""Check that the CMake entry point preserves failures without hiding diagnostics."""

from pathlib import Path
import subprocess
import sys
import unittest


class CMakeExitStatusTests(unittest.TestCase):
    def run_bridge(self, failure: str) -> subprocess.CompletedProcess[str]:
        """Run the bridge entry point with failure replacing the manifest build."""
        script = '''
import sys
from unittest.mock import patch
import cmake_modules as bridge

failure = sys.argv[1]

def build(directory: bridge.Path) -> None:
    """Inject the requested failure instead of compiling a manifest."""
    exec(failure)

with patch.object(bridge, 'build_modules', build), \
     patch.object(bridge, 'make_suppresses_execution', return_value=False), \
     patch.object(sys, 'argv', ['cmake_modules.py', '.']):
    bridge.main()
'''
        return subprocess.run([sys.executable, '-c', script, failure],
                              cwd=Path(__file__).resolve().parents[1],
                              capture_output=True, text=True)

    def test_compiler_failure(self) -> None:
        """A failed child keeps its diagnostics and status, without a Python trace."""
        result = self.run_bridge(
            "import subprocess, sys; subprocess.run([sys.executable, '-c', "
            "\"import sys; print('compiler diagnostic', file=sys.stderr); sys.exit(7)\"], check=True)")
        self.assertEqual(result.returncode, 7)
        self.assertEqual(result.stderr, 'compiler diagnostic\n')

    def test_unexpected_failure(self) -> None:
        """Programming errors still provide a traceback for debugging."""
        result = self.run_bridge("raise RuntimeError('unexpected failure')")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Traceback', result.stderr)
        self.assertIn('unexpected failure', result.stderr)
