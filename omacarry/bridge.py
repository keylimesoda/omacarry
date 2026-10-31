"""JSON interface between the Qt window and the sync engine."""
import json
import sys
from . import agent, core, prsync


def run(request):
    action = request['action']
    pid = request.get('plugin', '')
    if action == 'scan':
        rows = []
        for pl in core.scan():
            entry = core.plugin_state(pl.id)
            last = entry.get('last_sync') or {}
            rows.append({'id': pl.id, 'attention': (pl.needs_attention or pl.state == 'merged'
                                                   or bool(entry.get('tracked_branch'))
                                                   or (bool(last) and not last.get('ok'))),
                         'auto': core.policy(pl.id)['auto'], 'last': entry.get('last_sync'),
                         'tracked': entry.get('tracked_branch'), 'selected': entry.get('pr_selection'),
                         'upstream': pl.upstream_ref or '', 'candidate': pl.upstream_candidate or '',
                         'remotes': pl.remotes, 'upstream_remote': pl.upstream_remote or '',
                         'branch': pl.branch or ''})
        return {'ok': True, 'plugins': rows}
    if action == 'prs':
        return {'ok': True, 'choices': prsync.discover_prs(pid)}
    if action == 'select':
        entry = core.plugin_state(pid)
        entry['pr_selection'] = request['key']
        core.save_plugin_state(pid, entry)
        return {'ok': True, 'message': 'PR selected'}
    if action == 'policy':
        result = core.set_policy(pid, auto=request['enabled'])
    elif action == 'track':
        result = prsync.set_tracked_branch(pid, request['upstream'], request['base'],
                                           request['head_repo'], request['head'])
    elif action == 'clear_track':
        result = prsync.clear_tracked_branch(pid)
    elif action == 'sync':
        result = prsync.sync(pid)
    elif action == 'sync_all':
        results = core.auto_pass()
        failures = [r for r in results if not r.ok]
        return {'ok': not failures, 'message': f'{len(results)} checked · {len(failures)} need attention'}
    elif action == 'hook':
        result = core.install_hook()
    elif action == 'upstream':
        result = core.set_upstream(pid, request['url'])
    elif action == 'agent':
        result = agent.launch(pid)
    elif action == 'copy':
        return {'ok': True, 'clipboard': agent.diagnostic_prompt(pid), 'message': 'Error details copied'}
    elif action == 'login':
        return {'ok': True, 'login': prsync.github_login() or ''}
    else:
        raise ValueError('Unknown action: ' + action)
    return result.as_dict()


def main():
    try:
        print(json.dumps(run(json.loads(sys.stdin.read()))))
    except (ValueError, core.GitError, OSError) as exc:
        print(json.dumps({'ok': False, 'message': str(exc)}))
    except Exception as exc:
        print(json.dumps({'ok': False, 'message': f'{type(exc).__name__}: {exc}'}))

if __name__ == '__main__':
    main()
