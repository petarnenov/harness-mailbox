#!/usr/bin/env python3
"""Record Epic issue prerequisites and verification in the shape the dashboard reads.

Usage:
  record_issue.py --epic <KEY> --issue <KEY> depends-on <KEY>...
  record_issue.py --epic <KEY> --issue <KEY> verified --evidence <ref>... [--verifier <who>]

The dashboard counts an issue as verified only when `local_status` is
`verified_done`, `verification.status` is `passed` and `evidence` is non-empty.
It draws execution prerequisites only from `depends_on`; free-text blockers such
as "depends on GEO-1" create no graph edge, so unmet prerequisites show as zero.

`verified` is refused while any linked local task (listed in the issue's
`task_ids`/`local_task_ids` or naming the issue in `issue_key`/`issue_keys`) is
not `done` or `cancelled` with evidence or still holds its `.lock` directory. The dashboard
applies the same rule, so recording earlier would show the issue as unfinished.
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import issue_snapshots
from mailbox_layout import mailbox_root

MAILBOX = mailbox_root(__file__)


def find_issue(records, key):
    for identity, issue in records:
        if identity == key:
            return issue
    raise ValueError(f'Issue {key} is not in the Epic snapshot')


def set_prerequisites(issue, key, prerequisites, now):
    if key in prerequisites:
        raise ValueError('An issue cannot depend on itself')
    issue['depends_on'] = sorted(set(prerequisites))
    issue['blockers'] = [b for b in issue.get('blockers') or [] if not b.lower().startswith('depends on')]
    issue['updated_at'] = now


def set_verified(issue, evidence, verifier, now):
    references = [ref.strip() for ref in evidence if ref.strip()]
    if not references:
        raise ValueError('Verified issues need at least one evidence reference')
    issue['local_status'] = 'verified_done'
    issue['verification'] = {'status': 'passed', 'verifier': verifier, 'evidence': references, 'verified_at': now}
    issue['evidence'] = references
    issue['blockers'] = []
    issue['updated_at'] = now


# A cancelled task is closed too, when its evidence records the decision that cancelled it.
CLOSED_STATUSES = ('done', 'cancelled')


def as_list(value):
    if isinstance(value, str):
        return [value]
    return [item for item in value or [] if isinstance(item, str)]


def has_evidence(task):
    return bool(task.get('evidence') or task.get('acceptance_criterion_evidence')
                or task.get('acceptance_criteria_evidence'))


def read_task(root, path):
    try:
        task = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        task = None
    if not isinstance(task, dict):
        raise ValueError(f'Task record {path.relative_to(root)} is not a readable JSON object')
    return task


def unfinished_linked_tasks(root, key, issue):
    """Return why each linked local task blocks verification of the issue."""
    linked = set(as_list(issue.get('task_ids')) + as_list(issue.get('local_task_ids')))
    tasks = {}
    for path in sorted((root / 'tasks').glob('*/*.json')):
        task = read_task(root, path)
        task_id = task.get('id', path.stem)
        tasks[task_id] = (path, task)
        if key in as_list(task.get('issue_key')) + as_list(task.get('issue_keys')):
            linked.add(task_id)
    problems = []
    for task_id in sorted(linked):
        if task_id not in tasks:
            problems.append(f'{task_id}: task record missing')
            continue
        path, task = tasks[task_id]
        status = task.get('status')
        if status not in CLOSED_STATUSES:
            problems.append(f'{task_id}: status {status!r}, expected one of {", ".join(CLOSED_STATUSES)}')
        elif not has_evidence(task):
            problems.append(f'{task_id}: {status} without evidence')
        if path.with_suffix('.lock').exists():
            problems.append(f'{task_id}: lock still held')
    return problems


def require_finished_tasks(root, key, issue):
    problems = unfinished_linked_tasks(root, key, issue)
    if problems:
        raise ValueError(f'{key} cannot be verified until its owners close linked tasks: '
                         + '; '.join(problems))


def record(root, epic, key, apply):
    def edit(records):
        apply(find_issue(records, key))
        return True
    issue_snapshots.update_snapshot(issue_snapshots.epic_snapshot_path(root, epic), edit)


def run(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--root', type=Path, default=MAILBOX)
    parser.add_argument('--epic', required=True)
    parser.add_argument('--issue', required=True)
    commands = parser.add_subparsers(dest='command', required=True)
    depends = commands.add_parser('depends-on')
    depends.add_argument('prerequisites', nargs='+')
    verified = commands.add_parser('verified')
    verified.add_argument('--evidence', nargs='+', required=True)
    verified.add_argument('--verifier', default='orchestrator')
    args = parser.parse_args(argv)
    now = datetime.now(timezone.utc).isoformat()
    if args.command == 'depends-on':
        record(args.root, args.epic, args.issue, lambda issue: set_prerequisites(issue, args.issue, args.prerequisites, now))
    else:
        def verify(issue):
            require_finished_tasks(args.root, args.issue, issue)
            set_verified(issue, args.evidence, args.verifier, now)
        record(args.root, args.epic, args.issue, verify)
    print(json.dumps({'epic': args.epic, 'issue': args.issue, 'recorded': args.command}))


def main(argv=None):
    try:
        run(argv)
    except (ValueError, OSError) as error:
        sys.exit(f'record_issue: {issue_snapshots.refusal_text(error)}')


if __name__ == '__main__':
    main()
