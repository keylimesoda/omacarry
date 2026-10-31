import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from omacarry import core, prsync


def git(path, *args):
    return subprocess.run(['git', '-C', str(path), *args], check=True, text=True, capture_output=True).stdout.strip()


class PRSyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {'OMARCHY_PLUGINS_DIR': str(root / 'plugins'),
            'OMACARRY_CONFIG_DIR': str(root / 'state'), 'GIT_AUTHOR_NAME': 'Test',
            'GIT_AUTHOR_EMAIL': 'test@example.invalid', 'GIT_COMMITTER_NAME': 'Test',
            'GIT_COMMITTER_EMAIL': 'test@example.invalid'})
        self.env.start()
        self.addCleanup(self.env.stop)
        upstream = root / 'upstream'
        upstream.mkdir()
        git(upstream, 'init', '-b', 'main')
        (upstream / 'manifest.json').write_text('{}')
        (upstream / 'base').write_text('base')
        git(upstream, 'add', '.')
        git(upstream, 'commit', '-m', 'base')
        self.repo = root / 'plugins' / 'demo'
        self.repo.parent.mkdir()
        subprocess.run(['git', 'clone', '-q', str(upstream), str(self.repo)], check=True)
        git(self.repo, 'checkout', '-b', 'feature')
        (self.repo / 'mine').write_text('mine')
        git(self.repo, 'add', '.')
        git(self.repo, 'commit', '-m', 'feature')
        self.fork = root / 'fork.git'
        subprocess.run(['git', 'clone', '--bare', '-q', str(self.repo), str(self.fork)], check=True)
        (upstream / 'up').write_text('upstream')
        git(upstream, 'add', '.')
        git(upstream, 'commit', '-m', 'upstream')
        self.upstream = upstream
        self.choice = {'head_repo': 'me/fork', 'head': 'feature', 'base': 'main',
                       'upstream': 'owner/project', 'prs': [{'number': 1}],
                       'key': 'me/fork|feature|main'}
        original = core.git
        def local_git(repo, *args, **kw):
            args = tuple(str(self.upstream) if x == 'https://github.com/owner/project.git' else
                         str(self.fork) if x == 'https://github.com/me/fork.git' else x for x in args)
            return original(repo, *args, **kw)
        self.patchers = [patch.object(prsync, 'discover_prs', return_value=[self.choice]),
                         patch.object(core, 'git', side_effect=local_git),
                         patch.object(core, 'plugin_valid', return_value=True),
                         patch.object(core, 'deploy', return_value=core.Result(True, 'install', 'demo', 'ok'))]
        for p in self.patchers:
            p.start(); self.addCleanup(p.stop)

    def test_updates_remote_then_local(self):
        result = prsync.sync('demo')
        self.assertTrue(result.ok, result.message)
        self.assertEqual(git(self.repo, 'rev-parse', 'HEAD'), git(self.fork, 'rev-parse', 'feature'))
        self.assertTrue((self.repo / 'up').exists())
        self.assertTrue((self.repo / 'mine').exists())

    def test_local_branch_can_have_a_different_name(self):
        git(self.repo, 'branch', '-m', 'local-feature')
        result = prsync.sync('demo')
        self.assertTrue(result.ok, result.message)
        self.assertEqual(git(self.repo, 'rev-parse', 'HEAD'), git(self.fork, 'rev-parse', 'feature'))

    def test_conflict_leaves_github_and_local_untouched(self):
        (self.repo / 'base').write_text('mine')
        git(self.repo, 'add', '.')
        git(self.repo, 'commit', '-m', 'edit base')
        git(self.repo, 'push', str(self.fork), 'feature')
        (self.upstream / 'base').write_text('upstream')
        git(self.upstream, 'add', '.')
        git(self.upstream, 'commit', '-m', 'edit base upstream')
        local_before = git(self.repo, 'rev-parse', 'HEAD')
        remote_before = git(self.fork, 'rev-parse', 'feature')
        result = prsync.sync('demo')
        self.assertFalse(result.ok)
        self.assertIn('base', result.files)
        self.assertEqual(local_before, git(self.repo, 'rev-parse', 'HEAD'))
        self.assertEqual(remote_before, git(self.fork, 'rev-parse', 'feature'))

    def test_tracked_branch_syncs_without_open_pr(self):
        configured = prsync.set_tracked_branch('demo', 'owner/project', 'main', 'me/fork', 'feature')
        self.assertTrue(configured.ok, configured.message)
        with patch.object(prsync, 'discover_prs', side_effect=AssertionError('PR discovery should not run')):
            result = prsync.sync('demo')
        self.assertTrue(result.ok, result.message)
        self.assertEqual(git(self.repo, 'rev-parse', 'HEAD'), git(self.fork, 'rev-parse', 'feature'))
        self.assertTrue((self.repo / 'up').exists())

    def test_post_update_checks_upstream_even_when_local_looks_current(self):
        from types import SimpleNamespace
        prsync.set_tracked_branch('demo', 'owner/project', 'main', 'me/fork', 'feature')
        with patch.object(core, 'scan', return_value=[SimpleNamespace(id='demo', state='stock')]):
            results = core.auto_pass()
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].ok, results[0].message)
        self.assertTrue((self.repo / 'up').exists())
        self.assertEqual(git(self.repo, 'rev-parse', 'HEAD'), git(self.fork, 'rev-parse', 'feature'))

    def test_tracked_branch_preserves_unpublished_commits(self):
        prsync.set_tracked_branch('demo', 'owner/project', 'main', 'me/fork', 'feature')
        (self.repo / 'unfinished').write_text('local')
        git(self.repo, 'add', '.')
        git(self.repo, 'commit', '-m', 'unpublished')
        before = git(self.fork, 'rev-parse', 'feature')
        result = prsync.sync('demo')
        self.assertFalse(result.ok)
        self.assertIn('not on the GitHub', result.message)
        self.assertEqual(before, git(self.fork, 'rev-parse', 'feature'))

    def test_tracking_accepts_github_urls_and_reports_missing_fork(self):
        result = prsync.set_tracked_branch('demo', 'https://github.com/Owner/Project.git', 'main', '', 'feature')
        self.assertFalse(result.ok)
        self.assertIn('your GitHub repository', result.message)
        result = prsync.set_tracked_branch('demo', 'https://github.com/Owner/Project.git', 'main',
                                           'https://github.com/Me/Fork/', 'feature')
        self.assertTrue(result.ok, result.message)
        tracked = core.plugin_state('demo')['tracked_branch']
        self.assertEqual(tracked['upstream'], 'Owner/Project')
        self.assertEqual(tracked['head_repo'], 'Me/Fork')
        self.assertFalse(prsync.set_tracked_branch('demo', 'https://example.com/a/b', 'main',
                                                   'Me/Fork', 'feature').ok)

    def test_edit_during_local_fetch_stops_before_install(self):
        original = core.git.side_effect
        def changing_git(repo, *args, **kwargs):
            result = original(repo, *args, **kwargs)
            if Path(repo) == self.repo and args and args[0] == 'fetch':
                (self.repo / 'unfinished').write_text('do not discard')
            return result
        core.git.side_effect = changing_git
        before = git(self.repo, 'rev-parse', 'HEAD')
        result = prsync.sync('demo')
        self.assertFalse(result.ok)
        self.assertIn('local checkout changed', result.message)
        self.assertEqual(git(self.repo, 'rev-parse', 'HEAD'), before)
        self.assertEqual((self.repo / 'unfinished').read_text(), 'do not discard')

    def test_tracked_branch_requires_valid_distinct_target(self):
        result = prsync.set_tracked_branch('demo', 'owner/project', 'main', 'owner/project', 'main')
        self.assertFalse(result.ok)
        result = prsync.set_tracked_branch('demo', 'owner/project', 'main', 'me/fork', '--bad')
        self.assertFalse(result.ok)
        self.assertNotIn('tracked_branch', core.plugin_state('demo'))

    def test_switching_back_to_prs_clears_tracked_branch(self):
        prsync.set_tracked_branch('demo', 'owner/project', 'main', 'me/fork', 'feature')
        prsync.clear_tracked_branch('demo')
        self.assertNotIn('tracked_branch', core.plugin_state('demo'))
        self.assertTrue(prsync.sync('demo').ok)

    def test_unpublished_local_commit_blocks_push(self):
        (self.repo / 'unfinished').write_text('local')
        git(self.repo, 'add', '.')
        git(self.repo, 'commit', '-m', 'unpublished')
        before = git(self.fork, 'rev-parse', 'feature')
        result = prsync.sync('demo')
        self.assertFalse(result.ok)
        self.assertIn('not on the GitHub', result.message)
        self.assertEqual(before, git(self.fork, 'rev-parse', 'feature'))

    def test_dirty_checkout_blocks_push(self):
        (self.repo / 'unfinished').write_text('local')
        before = git(self.fork, 'rev-parse', 'feature')
        result = prsync.sync('demo')
        self.assertFalse(result.ok)
        self.assertIn('Uncommitted', result.message)
        self.assertEqual(before, git(self.fork, 'rev-parse', 'feature'))

    def test_no_pr_does_not_touch_checkout(self):
        before = git(self.repo, 'rev-parse', 'HEAD')
        with patch.object(prsync, 'discover_prs', return_value=[]):
            result = prsync.sync('demo')
        self.assertFalse(result.ok)
        self.assertIn('No open PR', result.message)
        self.assertEqual(before, git(self.repo, 'rev-parse', 'HEAD'))


class DiscoveryTests(unittest.TestCase):
    def test_github_login_uses_authenticated_user(self):
        from types import SimpleNamespace
        with patch.object(prsync.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout='my-user\n')):
            self.assertEqual(prsync.github_login(), 'my-user')
        with patch.object(prsync.subprocess, 'run', return_value=SimpleNamespace(returncode=1, stdout='')):
            self.assertIsNone(prsync.github_login())

    def test_groups_prs_by_head_and_base(self):
        import json
        from types import SimpleNamespace
        prs = [{'number': n, 'url': f'https://example.invalid/{n}', 'headRefName': 'feature',
                'headRepository': {'name': 'fork'}, 'headRepositoryOwner': {'login': 'me'},
                'baseRefName': 'main'} for n in (1, 2, 3)]
        prs.append(dict(prs[0], number=4, baseRefName='next'))
        pl = SimpleNamespace(upstream_remote='upstream',
            remotes={'upstream': 'https://github.com/owner/project.git'}, upstream_candidate=None, upstream_ref='upstream/main')
        response = SimpleNamespace(returncode=0, stdout=json.dumps(prs), stderr='')
        with patch.object(core, 'inspect_plugin', return_value=pl), patch.object(prsync.subprocess, 'run', return_value=response):
            trace = {}
            groups = prsync.discover_prs('demo', trace=trace)
        self.assertEqual(trace['repository'], 'owner/project')
        self.assertEqual(trace['returned_prs'], 4)
        self.assertEqual(trace['matching_groups'], 2)
        self.assertEqual(sorted(len(group['prs']) for group in groups), [1, 3])


if __name__ == '__main__':
    unittest.main()
