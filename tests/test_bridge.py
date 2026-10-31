import unittest
from types import SimpleNamespace
from unittest.mock import patch
from omacarry import bridge, core

class BridgeTests(unittest.TestCase):
    def test_scan_contains_plugin_state(self):
        plugin = SimpleNamespace(id='demo', needs_attention=True, state='carrying',
                                 upstream_ref='origin/main', upstream_candidate=None,
                                 remotes={'origin': 'https://github.com/example/demo'},
                                 upstream_remote='origin', branch='my-branch')
        with patch.object(core, 'scan', return_value=[plugin]), \
             patch.object(core, 'plugin_state', return_value={'last_sync': {'ok': False, 'message': 'conflict'}}), \
             patch.object(core, 'policy', return_value={'auto': True}):
            result = bridge.run({'action': 'scan'})
        self.assertEqual(result['plugins'][0]['id'], 'demo')
        self.assertEqual(result['plugins'][0]['last']['message'], 'conflict')
        self.assertTrue(result['plugins'][0]['auto'])

    def test_sync_calls_shared_engine(self):
        with patch.object(bridge.prsync, 'sync', return_value=core.Result(True, 'sync', 'demo', 'done')) as sync:
            result = bridge.run({'action': 'sync', 'plugin': 'demo'})
        sync.assert_called_once_with('demo')
        self.assertTrue(result['ok'])
        self.assertEqual(result['message'], 'done')

    def test_copy_uses_diagnostic_context(self):
        with patch.object(bridge.agent, 'diagnostic_prompt', return_value='diagnostic context'):
            result = bridge.run({'action': 'copy', 'plugin': 'demo'})
        self.assertEqual(result['clipboard'], 'diagnostic context')

if __name__ == '__main__':
    unittest.main()
