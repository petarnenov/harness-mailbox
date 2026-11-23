"""Check the English team interface and its handling of external display names."""
import json
from pathlib import Path
import re
import runpy
import sys
import tempfile
import unittest
from unittest.mock import patch


WORKSPACE = Path(__file__).resolve().parents[1] / 'workspace'


class TeamLanguageTests(unittest.TestCase):
    def test_shipped_team_sources_do_not_reintroduce_bulgarian_copy(self):
        plugin = WORKSPACE / 'copilot-mailbox'
        sources = [
            WORKSPACE / 'team', WORKSPACE / 'install-team.sh', plugin / 'start', plugin / 'team',
            plugin / 'team_control.py', plugin / 'team_ui_data.py', plugin / 'install-windows-command.ps1',
            plugin / 'plugin.json', plugin / 'extensions/mailbox/extension.mjs',
            plugin / 'extensions/mailbox/transport.mjs',
        ]
        cyrillic_letter = re.compile(r'[\u0400-\u04ff]')
        for path in sources:
            with self.subTest(source=str(path.relative_to(WORKSPACE))):
                lines = path.read_text(encoding='utf-8').splitlines()
                self.assertEqual([n for n, line in enumerate(lines, 1) if cyrillic_letter.search(line)], [])

    def test_folder_registry_and_provider_model_labels_preserve_external_unicode(self):
        with patch.object(sys, 'path', sys.path.copy()):
            ui = runpy.run_path(str(WORKSPACE / 'team'))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            registry = root / 'projects.json'
            registry.write_text('{"projects":[]}', encoding='utf-8')
            project = root / '\u041f\u0440\u043e\u0435\u043a\u0442'
            project.mkdir()
            wizard = ui['Wizard']([], root=root)
            wizard.open_folder(root)
            self.assertEqual(dict(wizard.options())[project], project.name + '/')
            ui['data'].add_project(root, project, 'sample')
            record, = json.loads(registry.read_text(encoding='utf-8'))['projects']
            self.assertEqual((record['name'], record['working_directory']), (project.name, str(project)))

            model_name = '\u30e2\u30c7\u30eb'
            rows = [{'value': 'sample-id', 'displayName': model_name}]
            with patch.object(ui['data'], '_claude_rows', return_value=rows):
                wizard.current.catalog = ui['data'].models('claude')
            wizard.go('model')
            self.assertEqual(dict(wizard.options())['sample-id'], model_name + ' (sample-id)')
