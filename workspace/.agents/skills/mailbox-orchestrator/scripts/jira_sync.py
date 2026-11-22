#!/usr/bin/env python3
"""Queue and record the Jira updates the orchestrator is allowed to make.

Usage:
  jira_sync.py --root <mailbox> queue --issue <KEY> --event start|pr|verified|worklog [--ref <text>]
  jira_sync.py --root <mailbox> pending
  jira_sync.py --root <mailbox> done --id <UUID> --outcome applied|skipped --detail <text>

Events and their Jira effect (applied by the orchestrator through its Jira tools):
  start     assign to the user running the orchestrator, move to In Progress
  pr        move to Development Peer Review (only work types that have it)
  verified  move to Closed
  worklog   log the machine hours from In Progress to Closed as one worklog

The script never talks to Jira. It keeps a durable queue under jira-sync/pending
and an audit trail under jira-sync/done, so a failed or skipped update stays visible.
An applied update also refreshes the issue's `source_status` snapshot in every
epics/*/issues.json that lists it, because the dashboard reads Jira status from there.

A worklog is measured between the moments the issue's `start` and `verified` updates
were applied, in whole hours rounded up. It is refused until both were applied, and
when the issue already has a pending or applied worklog.
"""
import argparse
import json
import math
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

TARGET_STATUS = {'start': 'In Progress', 'pr': 'Development Peer Review', 'verified': 'Closed'}
WORKLOG = 'worklog'
EVENTS = (*TARGET_STATUS, WORKLOG)
SECONDS_PER_HOUR = 3600
JIRA_STARTED_FORMAT = '%Y-%m-%dT%H:%M:%S.000%z'


def now():
    return datetime.now(timezone.utc).isoformat()


def write_atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(data, indent=2) + '\n')
    os.replace(temporary, path)


def read_requests(root, state):
    folder = root / 'jira-sync' / state
    return [json.loads(p.read_text()) for p in folder.glob('*.json')] if folder.exists() else []


def pending(root):
    return sorted(read_requests(root, 'pending'), key=lambda r: r['queued_at'])


def applied(root, issue, event):
    return [r for r in read_requests(root, 'done')
            if r['issue'] == issue and r['event'] == event and r['outcome'] == 'applied']


def has_worklog(root, issue):
    queued = [r for r in pending(root) if r['issue'] == issue and r['event'] == WORKLOG]
    return bool(queued or applied(root, issue, WORKLOG))


def machine_hours(in_progress_at, closed_at):
    elapsed = (closed_at - in_progress_at).total_seconds()
    if elapsed < 0:
        raise ValueError('Closed precedes In Progress')
    return max(1, math.ceil(elapsed / SECONDS_PER_HOUR))


def transition_effect(event):
    return {'target_status': TARGET_STATUS[event], 'assign_current_user': event == 'start'}


def worklog_effect(root, issue):
    if has_worklog(root, issue):
        raise ValueError(f'{issue} already has a pending or applied worklog')
    starts, closes = applied(root, issue, 'start'), applied(root, issue, 'verified')
    if not starts or not closes:
        raise ValueError(f'{issue} needs an applied start and an applied verified update before its worklog')
    in_progress_at = min(datetime.fromisoformat(r['completed_at']) for r in starts)
    closed_at = max(datetime.fromisoformat(r['completed_at']) for r in closes)
    return {'time_spent': f'{machine_hours(in_progress_at, closed_at)}h',
            'started': in_progress_at.strftime(JIRA_STARTED_FORMAT),
            'in_progress_at': in_progress_at.isoformat(), 'closed_at': closed_at.isoformat()}


def queue(root, issue, event, ref=''):
    if event not in EVENTS:
        raise ValueError(f'Unknown event {event}')
    effect = worklog_effect(root, issue) if event == WORKLOG else transition_effect(event)
    request = {'id': str(uuid.uuid4()), 'issue': issue, 'event': event, **effect,
               'ref': ref, 'queued_at': now()}
    write_atomic(root / 'jira-sync' / 'pending' / f"{request['id']}.json", request)
    return request


def record_source_status(root, issue, status, synced_at):
    """Copy an applied Jira status into the local snapshots of every Epic that lists the issue."""
    for issues_file in sorted((root / 'epics').glob('*/issues.json')):
        snapshot = json.loads(issues_file.read_text())
        records = [r for r in snapshot.get('issues', []) if r.get('key', r.get('issue_key')) == issue]
        for record in records:
            record.update(source_status=status, source_status_synced_at=synced_at)
        if records:
            write_atomic(issues_file, snapshot)


def done(root, request_id, outcome, detail):
    if outcome not in ('applied', 'skipped'):
        raise ValueError('Outcome must be applied or skipped')
    if not detail.strip():
        raise ValueError('Detail must say what happened in Jira')
    source = root / 'jira-sync' / 'pending' / f'{request_id}.json'
    request = json.loads(source.read_text())
    request.update(outcome=outcome, detail=detail, completed_at=now())
    write_atomic(root / 'jira-sync' / 'done' / f'{request_id}.json', request)
    source.unlink()
    if outcome == 'applied' and request['event'] in TARGET_STATUS:
        record_source_status(root, request['issue'], request['target_status'], request['completed_at'])
    return request


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--root', type=Path, required=True)
    commands = parser.add_subparsers(dest='command', required=True)
    queue_cmd = commands.add_parser('queue')
    queue_cmd.add_argument('--issue', required=True)
    queue_cmd.add_argument('--event', required=True, choices=sorted(EVENTS))
    queue_cmd.add_argument('--ref', default='')
    commands.add_parser('pending')
    done_cmd = commands.add_parser('done')
    done_cmd.add_argument('--id', required=True)
    done_cmd.add_argument('--outcome', required=True, choices=['applied', 'skipped'])
    done_cmd.add_argument('--detail', required=True)
    args = parser.parse_args()
    if args.command == 'queue':
        result = queue(args.root, args.issue, args.event, args.ref)
    elif args.command == 'pending':
        result = pending(args.root)
    else:
        result = done(args.root, args.id, args.outcome, args.detail)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
