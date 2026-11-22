#!/usr/bin/env python3
"""Reconcile worker session registrations with reality, without losing work.

Usage:
  reconcile_sessions.py inventory
  reconcile_sessions.py retire --session <ID> --evidence <text> [--by <who>]
  reconcile_sessions.py takeover --task <TASK-ID> --to <NEW-SESSION-ID> --evidence <text>

`inventory` (orchestrator, at activation and resume) lists every worker registration together with the
locks it holds and the unread messages addressed to it. Liveness cannot be judged from files: probe each
listed endpoint with the host's session tools, and treat no visibility plus no reply to a bounded PING as
unreachable.

`retire` (orchestrator, for an unreachable session only) moves the registration to
archive/sessions-removed/, so the dispatch gate no longer finds it. It also archives the unread messages
addressed to it, and records every task it still locks as orphaned under orphans/. Locks are never removed
or released.

`takeover` (run by the new owner, never by the orchestrator) is the only way to continue an orphaned task.
It requires the old session to be retired and the new one to be registered for the same project. It
rewrites the lock's owner with a succession entry and clears the orphan record. The new owner then updates
its own task record.

The user decided on 2026-11-21 that orphaned tasks always need an explicit takeover; locks are never
auto-released.
"""
import argparse
import json
import os
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

from mailbox_layout import mailbox_root

MAILBOX = mailbox_root(__file__)
ADDRESS_FIELDS = ('to_session_id', 'intended_endpoint', 'to_endpoint')


def now():
    return datetime.now(timezone.utc).isoformat()


def publish(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name('.' + path.name + '.tmp')
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False))
    os.replace(temporary, path)


def read(path):
    return json.loads(path.read_text())


def session_ids(record):
    return {value for value in (record.get('session_id'), record.get('agent_id')) if value}


def worker_registrations(root):
    for path in sorted((root / 'sessions').glob('*.json')):
        record = read(path)
        if record.get('role') == 'worker':
            yield path, record


def find_registration(root, session_id):
    matches = [(p, r) for p, r in worker_registrations(root) if session_id in session_ids(r)]
    if len(matches) != 1:
        raise ValueError(f'Expected exactly one worker registration for {session_id}, found {len(matches)}')
    return matches[0]


def locks_owned_by(root, session_id):
    for owner_file in sorted((root / 'tasks').glob('*/*.lock/owner.json')):
        if read(owner_file).get('owner_session_id') == session_id:
            yield owner_file


def messages_addressed_to(root, project, session_id):
    if not project:
        return
    for path in sorted((root / 'inbox' / project).glob('*.json')):
        message = read(path)
        if session_id in {message.get(field) for field in ADDRESS_FIELDS}:
            yield path


def candidate_worktrees(value):
    """Absolute directory paths named anywhere in a task record that are Git checkouts."""
    found = set()
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
        elif isinstance(item, str):
            for token in item.replace(',', ' ').split():
                path = Path(token.rstrip('.;:)'))
                if path.is_absolute() and (path / '.git').exists():
                    found.add(str(path))
    return sorted(found)


def uncommitted_changes(worktree):
    result = subprocess.run(['git', '-C', worktree, 'status', '--porcelain'], capture_output=True, text=True)
    return None if result.returncode else len(result.stdout.splitlines())


def inventory(root):
    report = []
    for path, record in worker_registrations(root):
        ids = session_ids(record)
        report.append({
            'registration': str(path.relative_to(root)), 'project': record.get('project'),
            'session_id': record.get('session_id'), 'agent_id': record.get('agent_id'),
            'session_name': record.get('session_name'), 'host': record.get('host'),
            'last_seen_at': record.get('last_seen_at'),
            'locks': [str(p.parent.relative_to(root)) for sid in ids for p in locks_owned_by(root, sid)],
            'unread_messages': [str(p.relative_to(root)) for sid in ids
                                for p in messages_addressed_to(root, record.get('project'), sid)],
            'liveness': 'probe required' if record.get('project') and ids else 'malformed registration'})
    return report


def retire(root, session_id, evidence, by):
    if not evidence.strip():
        raise ValueError('Retirement needs evidence of unreachability')
    path, record = find_registration(root, session_id)
    project, retired_at = record['project'], now()
    archived = root / 'archive' / 'sessions-removed' / path.name
    if archived.exists():
        raise ValueError(f'{archived} already exists')
    orphans = []
    for owner_file in locks_owned_by(root, session_id):
        lock = owner_file.parent
        task_file = lock.with_suffix('.json')
        task = read(task_file) if task_file.exists() else {}
        worktrees = candidate_worktrees(task)
        orphan = {'task_id': lock.stem, 'project': lock.parent.name, 'task': str(task_file.relative_to(root)),
                  'lock': str(lock.relative_to(root)), 'previous_owner': read(owner_file),
                  'task_status': task.get('status'), 'orphaned_at': retired_at, 'retired_session': session_id,
                  'worktrees': [{'path': w, 'uncommitted_changes': uncommitted_changes(w)} for w in worktrees],
                  'resolution': 'explicit takeover by a registered worker (reconcile_sessions.py takeover)'}
        publish(root / 'orphans' / f'{lock.stem}.json', orphan)
        orphans.append(orphan['task_id'])
    moved = []
    for message in messages_addressed_to(root, project, session_id):
        target = root / 'archive' / project / message.name
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(message, target)
        moved.append(str(target.relative_to(root)))
    archived.parent.mkdir(parents=True, exist_ok=True)
    os.replace(path, archived)
    retirement = {'id': str(uuid.uuid4()), 'session_id': session_id, 'project': project,
                  'session_name': record.get('session_name'), 'retired_at': retired_at, 'retired_by': by,
                  'evidence': evidence, 'archived_registration': str(archived.relative_to(root)),
                  'orphaned_tasks': orphans, 'archived_messages': moved}
    publish(root / 'decisions' / 'sessions' / f'{retirement["id"]}.json', retirement)
    return retirement


def takeover(root, task_id, new_session, evidence):
    orphan_path = root / 'orphans' / f'{task_id}.json'
    if not orphan_path.exists():
        raise ValueError(f'Task {task_id} is not orphaned; ask its owner, or retire an unreachable owner first')
    if not evidence.strip():
        raise ValueError('Takeover needs evidence: the inspected worktree state and what continues')
    orphan = read(orphan_path)
    _, record = find_registration(root, new_session)
    if record.get('project') != orphan['project']:
        raise ValueError(f'{new_session} is registered for {record.get("project")}, not {orphan["project"]}')
    if any(orphan['retired_session'] in session_ids(r) for _, r in worker_registrations(root)):
        raise ValueError('The previous owner is registered again; coordinate with it instead of taking over')
    owner_file = root / orphan['lock'] / 'owner.json'
    previous = read(owner_file)
    history = previous.get('succession', []) + [{
        'from': previous.get('owner_session_id'), 'to': new_session, 'at': now(), 'evidence': evidence,
        'orphan_record': str(orphan_path.relative_to(root))}]
    publish(owner_file, {'owner_session_id': new_session, 'session_name': record.get('session_name'),
                         'host': record.get('host'), 'claimed_at': history[-1]['at'], 'succession': history})
    resolved = root / 'archive' / 'orphans-resolved' / orphan_path.name
    resolved.parent.mkdir(parents=True, exist_ok=True)
    os.replace(orphan_path, resolved)
    return {'task_id': task_id, 'new_owner': new_session, 'succession': history}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--root', type=Path, default=MAILBOX)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('inventory')
    retire_command = commands.add_parser('retire')
    retire_command.add_argument('--session', required=True)
    retire_command.add_argument('--evidence', required=True)
    retire_command.add_argument('--by', default='orchestrator')
    takeover_command = commands.add_parser('takeover')
    takeover_command.add_argument('--task', required=True)
    takeover_command.add_argument('--to', required=True)
    takeover_command.add_argument('--evidence', required=True)
    args = parser.parse_args(argv)
    if args.command == 'inventory':
        result = inventory(args.root)
    elif args.command == 'retire':
        result = retire(args.root, args.session, args.evidence, args.by)
    else:
        result = takeover(args.root, args.task, args.to, args.evidence)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return result


if __name__ == '__main__':
    try:
        main()
    except ValueError as error:
        raise SystemExit(f'reconcile_sessions: {error}')
