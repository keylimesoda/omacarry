import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from omacarry import agent


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.plugin = SimpleNamespace(path=Path('/plugins/demo'), repo=Path('/plugins/demo'),
                                      branch='feature', head='abc123', upstream_ref='upstream/main')
        self.entry = {'pr_selection': 'me/fork|feature|main', 'last_sync': {
            'message': 'Upstream conflicts', 'files': ['Widget.qml'], 'details': ['fetch succeeded'], 'time': 1}}
        for p in (patch.object(agent.core, 'inspect_plugin', return_value=self.plugin),
                  patch.object(agent.core, 'plugin_state', return_value=self.entry),
                  patch.object(agent.core, 'git', return_value=(0, '## feature', ''))):
            p.start()
            self.addCleanup(p.stop)

    def test_prompt_has_failure_and_checkout_context(self):
        prompt = agent.diagnostic_prompt('demo')
        for text in ('demo', '/plugins/demo', 'feature', 'abc123', 'upstream/main',
                     'Upstream conflicts', 'Widget.qml', 'fetch succeeded', 'Preserve'):
            self.assertIn(text, prompt)

    def test_discovery_evidence_includes_specific_query(self):
        self.entry['last_sync']['discovery'] = {'repository': 'owner/project',
            'author': '@me', 'returned_prs': 0, 'matching_groups': 0}
        prompt = agent.diagnostic_prompt('demo')
        self.assertIn('owner/project', prompt)
        self.assertIn('returned_prs', prompt)
        self.assertIn('gh pr list --repo owner/project', prompt)
        self.assertIn('gh api user --jq .login', prompt)

    def test_launch_uses_omarchy_default_agent_without_shell(self):
        with patch.object(agent.subprocess, 'run', return_value=SimpleNamespace(returncode=0)) as run:
            result = agent.launch('demo')
        self.assertTrue(result.ok)
        args = run.call_args.args[0]
        self.assertEqual(args[:3], ['omarchy', 'agent', 'prompt'])
        self.assertIn('Widget.qml', args[3])
        self.assertEqual(run.call_args.kwargs['cwd'], self.plugin.repo)

    def test_launch_reports_missing_agent(self):
        result_obj = SimpleNamespace(returncode=1, stderr='Choose default agent', stdout='')
        with patch.object(agent.subprocess, 'run', return_value=result_obj):
            result = agent.launch('demo')
        self.assertFalse(result.ok)
        self.assertIn('Choose default agent', result.message)


if __name__ == '__main__':
    unittest.main()
