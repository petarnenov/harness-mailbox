#!/usr/bin/env python3
"""Queue and record the Jira updates the orchestrator is allowed to make.

Usage:
  jira_sync.py --root <mailbox> queue --issue <KEY> --event start|pr|verified|worklog [--ref <text>]
                                      [--in-progress-at <ISO time from the Jira history>]
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

A worklog covers one cycle: from the issue's applied `start` to its latest applied
`verified`, in whole hours rounded up. A reopened issue gets one worklog per cycle.
When the cycle's `start` was skipped because Jira was already In Progress, the time of
that transition from the Jira history must be passed with --in-progress-at. The request
is refused for a cycle that already has a pending or applied worklog, and for an issue
that issues.json lists as the parent of others, whose children carry the worklogs.
"""
import argparse
import json
import math
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple, Optional

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


def completion_times(root, issue, event, outcome):
    return sorted(datetime.fromisoformat(r['completed_at']) for r in read_requests(root, 'done')
                  if r['issue'] == issue and r['event'] == event and r['outcome'] == outcome)


class Cycle(NamedTuple):
    """One stay of an issue between the previous Closed (if any) and this Closed."""
    previous_closed_at: Optional[datetime]
    closed_at: datetime

    def contains(self, moment):
        after_previous = self.previous_closed_at is None or moment > self.previous_closed_at
        return after_previous and moment <= self.closed_at


def last_closed_cycle(root, issue):
    closes = completion_times(root, issue, 'verified', 'applied')
    if not closes:
        raise ValueError(f'{issue} needs an applied verified update before its worklog')
    return Cycle(closes[-2] if len(closes) > 1 else None, closes[-1])


def has_worklog(root, issue, cycle):
    logged = pending(root) + [r for r in read_requests(root, 'done') if r['outcome'] == 'applied']
    return any(r['issue'] == issue and r['event'] == WORKLOG and r['closed_at'] == cycle.closed_at.isoformat()
               for r in logged)


def has_children(root, issue):
    for issues_file in (root / 'epics').glob('*/issues.json'):
        for record in json.loads(issues_file.read_text()).get('issues', []):
            if issue in (record.get('parent_key'), (record.get('parent') or {}).get('key')):
                return True
    return False


def in_progress_moment(root, issue, cycle, jira_in_progress_at):
    """The applied start of the cycle, or the Jira history time when that start was skipped."""
    applied_starts = [t for t in completion_times(root, issue, 'start', 'applied') if cycle.contains(t)]
    if applied_starts:
        if jira_in_progress_at:
            raise ValueError(f'{issue} has an applied start; --in-progress-at is only for a skipped start')
        return applied_starts[-1], 'applied start'
    if not any(cycle.contains(t) for t in completion_times(root, issue, 'start', 'skipped')):
        raise ValueError(f'{issue} needs an applied or skipped start update before its worklog')
    if jira_in_progress_at is None:
        raise ValueError(f'{issue} had its start skipped; pass --in-progress-at from the Jira history')
    if not cycle.contains(jira_in_progress_at):
        raise ValueError('--in-progress-at must fall after the previous Closed and not after this Closed')
    return jira_in_progress_at, 'jira history'


def machine_hours(in_progress_at, closed_at):
    elapsed = (closed_at - in_progress_at).total_seconds()
    return max(1, math.ceil(elapsed / SECONDS_PER_HOUR))


def transition_effect(event):
    return {'target_status': TARGET_STATUS[event], 'assign_current_user': event == 'start'}


def worklog_effect(root, issue, jira_in_progress_at):
    if has_children(root, issue):
        raise ValueError(f'{issue} is a parent issue; its children carry the worklogs')
    cycle = last_closed_cycle(root, issue)
    if has_worklog(root, issue, cycle):
        raise ValueError(f'{issue} already has a pending or applied worklog for this Closed')
    in_progress_at, source = in_progress_moment(root, issue, cycle, jira_in_progress_at)
    return {'time_spent': f'{machine_hours(in_progress_at, cycle.closed_at)}h',
            'started': in_progress_at.strftime(JIRA_STARTED_FORMAT),
            'in_progress_at': in_progress_at.isoformat(), 'in_progress_source': source,
            'closed_at': cycle.closed_at.isoformat()}


def queue(root, issue, event, ref='', jira_in_progress_at=None):
    if event not in EVENTS:
        raise ValueError(f'Unknown event {event}')
    if event != WORKLOG and jira_in_progress_at:
        raise ValueError('--in-progress-at applies to the worklog event only')
    effect = worklog_effect(root, issue, jira_in_progress_at) if event == WORKLOG else transition_effect(event)
    request = {'id': str(uuid.uuid4()), 'issue': issue, 'event': event, **effect,
               'ref': ref, 'queued_at': now()}
    write_atomic(root / 'jira-sync' / 'pending' / f"{request['id']}.json", request)
    return request


def jira_history_time(text):
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        raise ValueError('--in-progress-at needs a UTC offset')
    return moment.astimezone(timezone.utc)


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


def run():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--root', type=Path, required=True)
    commands = parser.add_subparsers(dest='command', required=True)
    queue_cmd = commands.add_parser('queue')
    queue_cmd.add_argument('--issue', required=True)
    queue_cmd.add_argument('--event', required=True, choices=sorted(EVENTS))
    queue_cmd.add_argument('--ref', default='')
    queue_cmd.add_argument('--in-progress-at', type=jira_history_time)
    commands.add_parser('pending')
    done_cmd = commands.add_parser('done')
    done_cmd.add_argument('--id', required=True)
    done_cmd.add_argument('--outcome', required=True, choices=['applied', 'skipped'])
    done_cmd.add_argument('--detail', required=True)
    args = parser.parse_args()
    if args.command == 'queue':
        result = queue(args.root, args.issue, args.event, args.ref, args.in_progress_at)
    elif args.command == 'pending':
        result = pending(args.root)
    else:
        result = done(args.root, args.id, args.outcome, args.detail)
    print(json.dumps(result, indent=2))


def main():
    try:
        run()
    except ValueError as error:
        sys.exit(f'jira_sync: {error}')


if __name__ == '__main__':
    main()
