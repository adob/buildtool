"""Regression coverage for dependency flags populated after library discovery."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from platformio_adapter import configure


class PlatformIOAdapterTests(unittest.TestCase):
    def test_dependency_flags_are_read_at_action_time(self) -> None:
        """Includes added after registration must reach both module compiler commands."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            includes = []
            library = MagicMock()
            library.GetProjectOption.return_value = 'lib.types'
            library.get.return_value = ['arduino']
            variables = {'$BUILD_DIR': str(root / 'build'), '$PROJECT_DIR': str(root),
                         '$CC': '/usr/bin/gcc', '$CXX': '/usr/bin/g++',
                         '$AR': '/usr/bin/ar'}

            def substitute(expression: str) -> str:
                """Expose flags that change as dependency discovery completes."""
                return variables.get(expression, ' '.join(includes))

            library.subst.side_effect = substitute
            library.WhereIs.side_effect = lambda name: name
            project = MagicMock()
            project.get.side_effect = lambda key, default=None: default
            with patch('platformio_adapter.subprocess.check_output', return_value='/usr/bin/ranlib\n'):
                configure(library, project, root)
            action = project.Command.call_args.args[2]
            includes.append('-I' + str(root / 'dependency/include'))
            with patch('platformio_adapter.subprocess.call', return_value=0):
                self.assertEqual(action([], [], project), 0)
            request = json.loads((root / 'build/buildtool-modules/request.json').read_text())
            for field in ('cflags', 'cxxflags'):
                self.assertIn(includes[0], request[field])
