#!/usr/bin/env python3
"""Queue and record the Jira updates the orchestrator is allowed to make.

Usage:
  jira_sync.py [--root <mailbox>] queue --issue <KEY> --event start|pr|verified|worklog|estimate [--ref <text>]
                                      [--hours <person-hours, estimate only>]
                                      [--in-progress-at <ISO time from the Jira history>]
                                      [--transcripts <folder of Claude Code session transcripts>]
  jira_sync.py [--root <mailbox>] pending
  jira_sync.py [--root <mailbox>] done --id <UUID> --outcome applied|skipped --detail <text>
  jira_sync.py [--root <mailbox>] worked [--transcripts <folder of Claude Code session transcripts>]

Events and their Jira effect (applied by the orchestrator through its Jira tools):
  start     assign to the user running the orchestrator, move to In Progress
  pr        move to Development Peer Review (only work types that have it)
  verified  move to Closed
  worklog   log the agents' active hours from In Progress to Closed as one worklog
  estimate  set an empty Original Estimate to the given person-hours, before the first dispatch

The script never talks to Jira. It keeps a durable queue under jira-sync/pending
and an audit trail under jira-sync/done, so a failed or skipped update stays visible.
An applied update also refreshes the issue's `source_status` snapshot in every
epics/*/issues.json that lists it, because the dashboard reads Jira status from there.

A worklog covers one cycle: from the issue's applied `start` to its latest applied
`verified`. It logs only the time in which the agents of the issue's projects worked, read
from their session transcripts (see agent_activity.py), shared with the other issues of the
same project open at that moment, in whole hours rounded up. Waiting for an answer, a merge
or the next message is left out. A reopened issue gets one worklog per cycle. The request is
refused when the issue has no linked task, or when any of its projects left no recorded work
in the cycle: a partial number never reaches Jira.
When the cycle's `start` was skipped because Jira was already In Progress, the time of
that transition from the Jira history must be passed with --in-progress-at. The request
is refused for a cycle that already has a pending or applied worklog, and for an issue
that issues.json lists as the parent of others, whose children carry the worklogs.

An estimate is the orchestrator's judgement of a developer's effort in person-hours, not agent
time. The orchestrator reads Jira first and queues it only when Original Estimate is empty; a
Jira value found at apply time is recorded as skipped, never overwritten. The request is
refused for an issue that already has a pending or applied estimate. An applied estimate is
copied into the issue's `estimate` in every epics/*/issues.json that lists it.

`worked` changes nothing in Jira and queues nothing. It measures the same way the seconds the
agents worked so far on every started issue, over all its stays in progress up to now, and
saves them in jira-sync/worked-so-far.json for the dashboard, which cannot read transcripts.
"""
import argparse
import json
import math
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import NamedTuple, Optional

import agent_activity
import issue_snapshots
from mailbox_layout import mailbox_root

TARGET_STATUS = {'start': 'In Progress', 'pr': 'Development Peer Review', 'verified': 'Closed'}
WORKLOG = 'worklog'
ESTIMATE = 'estimate'
EVENTS = (*TARGET_STATUS, WORKLOG, ESTIMATE)
SECONDS_PER_HOUR = 3600
JIRA_STARTED_FORMAT = '%Y-%m-%dT%H:%M:%S.000%z'
TRANSCRIPTS = Path.home() / '.claude' / 'projects'
MAILBOX = mailbox_root(__file__)
ABANDONED_AFTER = timedelta(days=3)
WORKED_SO_FAR = Path('jira-sync') / 'worked-so-far.json'
APPLIED_START = 'applied start'


def now():
    return datetime.now(timezone.utc).isoformat()


REQUIRED_FIELDS = {'pending': ('id', 'issue', 'event', 'queued_at'),
                   'done': ('id', 'issue', 'event', 'queued_at', 'outcome', 'completed_at')}
TIME_FIELDS = ('queued_at', 'completed_at', 'in_progress_at')


def is_aware_time(text):
    try:
        return datetime.fromisoformat(text).tzinfo is not None
    except (TypeError, ValueError):
        return False


def is_request(record, state):
    return (isinstance(record, dict) and all(isinstance(record.get(f), str) for f in REQUIRED_FIELDS[state])
            and all(is_aware_time(record[f]) for f in TIME_FIELDS if f in record))


def read_request(path, state):
    try:
        record = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        record = None
    if not is_request(record, state):
        raise ValueError(f'Request record {state}/{path.name} is malformed')
    return record


def read_requests(root, state):
    folder = root / 'jira-sync' / state
    return [read_request(path, state) for path in sorted(folder.glob('*.json'))]


def is_canonical_uuid(text):
    try:
        return str(uuid.UUID(text)) == text
    except ValueError:
        return False


def publish_request(path, request):
    try:
        issue_snapshots.publish_new_json(path, request)
    except FileExistsError:
        raise ValueError(f'Request {request["id"]} already exists') from None


def pending(root):
    completed = {path.stem for path in (root / 'jira-sync' / 'done').glob('*.json')}
    waiting = [r for r in read_requests(root, 'pending') if r['id'] not in completed]
    return sorted(waiting, key=lambda r: r['queued_at'])


def live_requests(root):
    """Requests that still count for their issue: waiting ones and applied ones."""
    return pending(root) + [r for r in read_requests(root, 'done') if r['outcome'] == 'applied']


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

    def overlaps(self, opened_at, closed_at):
        starts_before_close = self.previous_closed_at is None or self.previous_closed_at < closed_at
        return starts_before_close and self.closed_at > opened_at


def last_closed_cycle(root, issue):
    closes = completion_times(root, issue, 'verified', 'applied')
    if not closes:
        raise ValueError(f'{issue} needs an applied verified update before its worklog')
    return Cycle(closes[-2] if len(closes) > 1 else None, closes[-1])


def has_worklog(root, issue, cycle):
    return any(r['issue'] == issue and r['event'] == WORKLOG and r.get('closed_at') == cycle.closed_at.isoformat()
               for r in live_requests(root))


def recorded_in_progress_at(root, issue, cycle):
    """The in-progress time of the cycle's queued or applied worklog, which may come from the Jira history."""
    for request in live_requests(root):
        if (request['issue'] == issue and request['event'] == WORKLOG
                and request.get('closed_at') == cycle.closed_at.isoformat() and 'in_progress_at' in request):
            return datetime.fromisoformat(request['in_progress_at'])
    return None


def cycle_opening(root, issue, cycle):
    """When the issue went In Progress in the cycle: its first applied start, else the time its worklog
    took from the Jira history, else its first skipped start; None when there is no start."""
    applied = starts_in(root, issue, cycle, 'applied')
    if applied:
        return applied[0]
    recorded = recorded_in_progress_at(root, issue, cycle)
    if recorded:
        return recorded
    skipped = starts_in(root, issue, cycle, 'skipped')
    return skipped[0] if skipped else None


def starts_in(root, issue, cycle, outcome):
    return [t for t in completion_times(root, issue, 'start', outcome) if cycle.contains(t)]


def closed_cycles(root, issue):
    closes = completion_times(root, issue, 'verified', 'applied')
    return [Cycle(previous, closed) for previous, closed in zip([None, *closes], closes)]


def opening_known_only_from_skipped_start(root, issue, cycle):
    no_applied_start = not starts_in(root, issue, cycle, 'applied')
    return (no_applied_start and recorded_in_progress_at(root, issue, cycle) is None
            and bool(starts_in(root, issue, cycle, 'skipped')))


def require_neighbours_worklogs_first(root, issue, in_progress_at, closed_at):
    """Refuse while a project neighbour's overlapping cycle has no real In Progress time recorded yet."""
    task_ids = task_ids_by_issue(root)
    projects = agent_activity.task_projects(root, task_ids.get(issue, ()))
    for other, other_tasks in sorted(task_ids.items()):
        shares_project = projects & agent_activity.task_projects(root, other_tasks)
        if other == issue or not shares_project or has_children(root, other):
            continue
        for cycle in closed_cycles(root, other):
            if cycle.overlaps(in_progress_at, closed_at) and opening_known_only_from_skipped_start(root, other, cycle):
                raise ValueError(f'{issue} shares a project with {other}, whose start was skipped; '
                                 f'queue the worklog of {other} first')


def has_children(root, issue):
    for _, record in issue_snapshots.all_issue_records(root):
        if issue in (record.get('parent_key'), (record.get('parent') or {}).get('key')):
            return True
    return False


def in_progress_moment(root, issue, cycle, jira_in_progress_at):
    """The applied start of the cycle, or the Jira history time when that start was skipped."""
    applied_starts = [t for t in completion_times(root, issue, 'start', 'applied') if cycle.contains(t)]
    if applied_starts:
        if jira_in_progress_at:
            raise ValueError(f'{issue} has an applied start; --in-progress-at is only for a skipped start')
        return applied_starts[0], APPLIED_START
    if not any(cycle.contains(t) for t in completion_times(root, issue, 'start', 'skipped')):
        raise ValueError(f'{issue} needs an applied or skipped start update before its worklog')
    if jira_in_progress_at is None:
        raise ValueError(f'{issue} had its start skipped; pass --in-progress-at from the Jira history')
    if not cycle.contains(jira_in_progress_at):
        raise ValueError('--in-progress-at must fall after the previous Closed and not after this Closed')
    return jira_in_progress_at, 'jira history'


def task_ids_by_issue(root):
    task_ids = {}
    for issue, record in issue_snapshots.all_issue_records(root):
        if issue:
            linked = (record.get('task_ids') or []) + (record.get('local_task_ids') or [])
            task_ids.setdefault(issue, set()).update(linked)
    return task_ids


def last_sign_of_life(root, issue, task_ids):
    """POSIX time of the issue's latest Jira update or change to one of its tasks."""
    requests = [r for state in ('pending', 'done') for r in read_requests(root, state) if r['issue'] == issue]
    signs = [datetime.fromisoformat(r.get('completed_at', r['queued_at'])).timestamp() for r in requests]
    for task_id in task_ids:
        for path in root.glob(f'tasks/*/{task_id}.json'):
            signs.append(agent_activity.parse_time((agent_activity.read_json(path) or {}).get('updated_at')))
    return max(sign for sign in signs if sign is not None)


def work_windows(root, issue, task_ids):
    """(opened, closed) POSIX times of each stay in progress, one per cycle.

    The stay that is still in progress lasts ABANDONED_AFTER beyond the issue's last sign of
    life. While it shows life it shares its project's work with the issues open beside it;
    once abandoned it no longer takes a share of every later issue.
    """
    starts = sorted(completion_times(root, issue, 'start', 'applied') + completion_times(root, issue, 'start', 'skipped'))
    windows, previous_closed = [], None
    for closed in completion_times(root, issue, 'verified', 'applied'):
        cycle = Cycle(previous_closed, closed)
        opened = cycle_opening(root, issue, cycle)
        if opened:
            windows.append((opened.timestamp(), closed.timestamp()))
        previous_closed = closed
    still_open = next((moment for moment in starts if previous_closed is None or moment > previous_closed), None)
    if still_open:
        alive_until = last_sign_of_life(root, issue, task_ids) + ABANDONED_AFTER.total_seconds()
        windows.append((still_open.timestamp(), alive_until))
    return windows


def project_windows(root, issue, own_window):
    """For each project: this issue's cycle as own_window, plus the work windows of its other issues.

    A parent issue shares nothing: it is open as long as its children and gets no worklog of its own.
    """
    task_ids = {other: tasks for other, tasks in task_ids_by_issue(root).items()
                if other != issue and not has_children(root, other)}

    def windows_of(project):
        windows = [own_window]
        for other, other_tasks in task_ids.items():
            if project in agent_activity.task_projects(root, other_tasks):
                windows += work_windows(root, other, other_tasks)
        return windows
    return windows_of


def worked_seconds(root, issue, in_progress_at, closed_at, transcripts_root):
    task_ids = task_ids_by_issue(root).get(issue)
    if not task_ids:
        raise ValueError(f'{issue} has no linked task; its agents\' work cannot be measured')
    window = (in_progress_at.timestamp(), closed_at.timestamp())
    by_project = agent_activity.active_seconds_by_project(root, task_ids, window,
                                                          project_windows(root, issue, window), transcripts_root)
    unmeasured = sorted(project for project, seconds in by_project.items() if seconds <= 0)
    if unmeasured or not by_project:
        raise ValueError(f'{issue} has no recorded agent work between In Progress and Closed'
                         f" in: {', '.join(unmeasured) or 'any project'}")
    return sum(by_project.values())


def seconds_worked_in(root, issue, task_ids, stay, transcripts_root):
    by_project = agent_activity.active_seconds_by_project(root, task_ids, stay,
                                                          project_windows(root, issue, stay), transcripts_root)
    return sum(by_project.values())


def worked_so_far(root, transcripts_root):
    """Save the seconds the agents worked on each started issue up to now.

    A parent issue is left out: its children carry the work, as they carry the worklogs.
    """
    measured_at = now()
    until = datetime.fromisoformat(measured_at).timestamp()
    worked = {}
    for issue, task_ids in task_ids_by_issue(root).items():
        if not issue or not task_ids or has_children(root, issue):
            continue
        stays = [(opened, min(closed, until)) for opened, closed in work_windows(root, issue, task_ids)]
        if stays:
            worked[issue] = round(sum(seconds_worked_in(root, issue, task_ids, stay, transcripts_root)
                                      for stay in stays))
    snapshot = {'measured_at': measured_at, 'worked_seconds': worked}
    issue_snapshots.replace_json(root / WORKED_SO_FAR, snapshot)
    return snapshot


def whole_hours(seconds):
    return math.ceil(seconds / SECONDS_PER_HOUR)


def transition_effect(event):
    return {'target_status': TARGET_STATUS[event], 'assign_current_user': event == 'start'}


def worklog_effect(root, issue, jira_in_progress_at, transcripts_root):
    if has_children(root, issue):
        raise ValueError(f'{issue} is a parent issue; its children carry the worklogs')
    cycle = last_closed_cycle(root, issue)
    if has_worklog(root, issue, cycle):
        raise ValueError(f'{issue} already has a pending or applied worklog for this Closed')
    in_progress_at, source = in_progress_moment(root, issue, cycle, jira_in_progress_at)
    if source == APPLIED_START:
        require_neighbours_worklogs_first(root, issue, in_progress_at, cycle.closed_at)
    elapsed = (cycle.closed_at - in_progress_at).total_seconds()
    worked = worked_seconds(root, issue, in_progress_at, cycle.closed_at, transcripts_root)
    return {'time_spent': f'{whole_hours(worked)}h',
            'elapsed_seconds': round(elapsed), 'worked_seconds': round(worked),
            'started': in_progress_at.strftime(JIRA_STARTED_FORMAT),
            'in_progress_at': in_progress_at.isoformat(), 'in_progress_source': source,
            'closed_at': cycle.closed_at.isoformat()}


def has_estimate(root, issue):
    return any(r['issue'] == issue and r['event'] == ESTIMATE for r in live_requests(root))


def estimate_effect(root, issue, hours):
    if hours is None or not math.isfinite(hours) or hours <= 0:
        raise ValueError('The estimate event needs --hours, a positive number of person-hours')
    if has_estimate(root, issue):
        raise ValueError(f'{issue} already has a pending or applied estimate; it is never overwritten')
    return {'original_estimate': f'{hours:g}h', 'hours': hours, 'unit': 'person-hours'}


def queue(root, issue, event, ref='', jira_in_progress_at=None, transcripts_root=None, hours=None):
    if event not in EVENTS:
        raise ValueError(f'Unknown event {event}')
    if event != WORKLOG and jira_in_progress_at:
        raise ValueError('--in-progress-at applies to the worklog event only')
    if event != ESTIMATE and hours is not None:
        raise ValueError('--hours applies to the estimate event only')
    if event == WORKLOG:
        effect = worklog_effect(root, issue, jira_in_progress_at, transcripts_root or TRANSCRIPTS)
    elif event == ESTIMATE:
        effect = estimate_effect(root, issue, hours)
    else:
        effect = transition_effect(event)
    request = {'id': str(uuid.uuid4()), 'issue': issue, 'event': event, **effect,
               'ref': ref, 'queued_at': now()}
    publish_request(root / 'jira-sync' / 'pending' / f"{request['id']}.json", request)
    return request


def jira_history_time(text):
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        raise ValueError('--in-progress-at needs a UTC offset')
    return moment.astimezone(timezone.utc)


def update_issue_records(root, issue, fields):
    """Write fields into the issue's record in every Epic snapshot that lists it."""
    def edit(records):
        matching = [record for identity, record in records if identity == issue]
        for record in matching:
            record.update(fields)
        return bool(matching)
    for snapshot in issue_snapshots.epic_snapshot_paths(root):
        issue_snapshots.update_snapshot(snapshot, edit)


def record_source_status(root, issue, status, synced_at):
    """Copy an applied Jira status into the local snapshots of every Epic that lists the issue."""
    update_issue_records(root, issue, {'source_status': status, 'source_status_synced_at': synced_at})


def record_estimate(root, issue, hours, applied_at):
    """Copy an applied Original Estimate into the local snapshots of every Epic that lists the issue."""
    update_issue_records(root, issue, {'estimate': {'value': float(hours), 'unit': 'hours',
                                                    'source': 'jira timeoriginalestimate',
                                                    'retrieved_at': applied_at}})


def done(root, request_id, outcome, detail):
    if outcome not in ('applied', 'skipped'):
        raise ValueError('Outcome must be applied or skipped')
    if not detail.strip():
        raise ValueError('Detail must say what happened in Jira')
    if not is_canonical_uuid(request_id):
        raise ValueError('The request id must be a canonical UUID')
    source = root / 'jira-sync' / 'pending' / f'{request_id}.json'
    completed = root / 'jira-sync' / 'done' / f'{request_id}.json'
    if completed.exists():
        raise ValueError(f'Request {request_id} is already completed')
    if not source.is_file():
        raise ValueError(f'No pending request {request_id}')
    request = read_request(source, 'pending')
    request.update(outcome=outcome, detail=detail, completed_at=now())
    if outcome == 'applied' and request['event'] in TARGET_STATUS:
        record_source_status(root, request['issue'], request['target_status'], request['completed_at'])
    if outcome == 'applied' and request['event'] == ESTIMATE:
        record_estimate(root, request['issue'], request['hours'], request['completed_at'])
    publish_request(completed, request)
    source.unlink()
    return request


def run():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--root', type=Path, default=MAILBOX)
    commands = parser.add_subparsers(dest='command', required=True)
    queue_cmd = commands.add_parser('queue')
    queue_cmd.add_argument('--issue', required=True)
    queue_cmd.add_argument('--event', required=True, choices=sorted(EVENTS))
    queue_cmd.add_argument('--ref', default='')
    queue_cmd.add_argument('--in-progress-at', type=jira_history_time)
    queue_cmd.add_argument('--transcripts', type=Path)
    queue_cmd.add_argument('--hours', type=float)
    commands.add_parser('pending')
    done_cmd = commands.add_parser('done')
    done_cmd.add_argument('--id', required=True)
    done_cmd.add_argument('--outcome', required=True, choices=['applied', 'skipped'])
    done_cmd.add_argument('--detail', required=True)
    worked_cmd = commands.add_parser('worked')
    worked_cmd.add_argument('--transcripts', type=Path)
    args = parser.parse_args()
    if args.command == 'queue':
        result = queue(args.root, args.issue, args.event, args.ref, args.in_progress_at, args.transcripts,
                       args.hours)
    elif args.command == 'pending':
        result = pending(args.root)
    elif args.command == 'worked':
        result = worked_so_far(args.root, args.transcripts or TRANSCRIPTS)
    else:
        result = done(args.root, args.id, args.outcome, args.detail)
    print(json.dumps(result, indent=2))


def main():
    try:
        run()
    except (ValueError, OSError) as error:
        sys.exit(f'jira_sync: {issue_snapshots.refusal_text(error)}')


if __name__ == '__main__':
    main()
