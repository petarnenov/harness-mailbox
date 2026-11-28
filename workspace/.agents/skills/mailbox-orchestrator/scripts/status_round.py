#!/usr/bin/env python3
"""Ask every registered worker for a status report, once per 15-minute UTC slot.

Usage:
  status_round.py request --endpoint <ORCHESTRATOR-ENDPOINT> [--root <DIR>] [--at <ISO-TIME>]
  status_round.py collect --endpoint <ORCHESTRATOR-ENDPOINT> [--root <DIR>] [--slot <SLOT>]

`request` publishes one non-executable status_request into inbox/<project>/ for every worker
registered in sessions/*.json. The message id is derived from the slot, the project and the worker endpoint,
so a rerun within the same slot publishes nothing and touches no file. It then moves every older
unanswered status_request of each addressed worker from the inbox into archive/<project>/ (listed under
superseded; an archive name already taken leaves the inbox file and lists it under supersede_conflicts), so at
most one unanswered request per worker stays pending. A message already present in the
inbox or the archive is never overwritten. The request asks for a status report and, when the worker is
blocked, for the context of the blocker and one concrete question the orchestrator can decide.

`collect` is read-only. For a slot it reports each worker's reply or its absence and lists the blocked
reports as decisions for the orchestrator; a blocked report without a task, context or question is listed
as incomplete. The orchestrator answers a decision with decisions/<task_id>/<report-id>.json and a
non-executable reply; resuming work still goes through dispatch_task.py.

The cadence comes from status_round_runner.py, which runs `request` and `collect` for every 15-minute slot
and records the results; `request` stays safe to run by hand, a rerun within a slot touches nothing.
"""
import argparse
import importlib
import json
import os
import re
import secrets
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

NAMESPACE = uuid.UUID('6f1d2a54-8c3e-5b7a-9d41-2e0c7a5b9f13')
SLOT_MINUTES = 15
SLOT_FORMAT = '%Y-%m-%dT%H:%M:00Z'
TIMESTAMP_FORMAT = '%Y-%m-%dT%H:%M:%SZ'
PROJECT_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]*')
STATUSES = ('working', 'idle', 'waiting', 'blocked')
USAGE_LINE = 'status_round: invalid time or slot'
MAILBOX_LINE = 'status_round: the mailbox could not be read'
FAILURE_LINE = 'status_round: a mailbox record could not be read or written'


class UsageError(Exception):
    """A caller-supplied time or slot that is not accepted."""


class MailboxError(Exception):
    """The mailbox root, registry or inbox is unreadable."""


def import_without_bytecode(*names):
    """The named sibling modules, imported so that they leave no cache file beside the scripts."""
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        return [importlib.import_module(name) for name in names]
    finally:
        sys.dont_write_bytecode = previous


receive, mailbox_layout = import_without_bytecode('receive', 'mailbox_layout')
mailbox_root = mailbox_layout.mailbox_root


def default_root():
    """The mailbox root of the installed layout or of the nearest projects.json above this script."""
    try:
        return mailbox_root(__file__)
    except IndexError:  # fewer ancestors than the installed layout needs: no mailbox can be located
        raise MailboxError(MAILBOX_LINE) from None


def utc_now():
    return datetime.now(timezone.utc)


def parse_aware(text):
    """The timezone-aware time in text converted to UTC, or None."""
    if not isinstance(text, str):
        return None
    try:
        moment = datetime.fromisoformat(text)
        return moment.astimezone(timezone.utc) if moment.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


def floor_to_slot(moment):
    utc = moment.astimezone(timezone.utc)
    return utc.replace(minute=utc.minute - utc.minute % SLOT_MINUTES, second=0, microsecond=0)


def requested_moment(text):
    if text is None:
        return utc_now()
    moment = parse_aware(text)
    if moment is None:
        raise UsageError
    return moment.astimezone(timezone.utc)


def requested_slot(text, now):
    if text is None:
        return floor_to_slot(now).strftime(SLOT_FORMAT)
    moment = parse_aware(text)
    if moment is None:
        raise UsageError
    utc = moment.astimezone(timezone.utc)
    if utc != floor_to_slot(utc):
        raise UsageError
    return utc.strftime(SLOT_FORMAT)


def request_id(slot, project, endpoint):
    return str(uuid.uuid5(NAMESPACE, f'status-request:{slot}:{project}:{endpoint}'))


def relative(root, path):
    return str(path.relative_to(root))


def project_registry(root):
    try:
        return receive.project_ids(root)
    except (OSError, ValueError):
        raise MailboxError(MAILBOX_LINE) from None


def registration_files(root):
    sessions = root / 'sessions'
    if not os.path.lexists(sessions):
        return []
    try:
        return sorted(path for path in sessions.iterdir() if path.suffix == '.json')
    except OSError:
        raise MailboxError(MAILBOX_LINE) from None


def skipped(root, path, reason):
    return {'registration': relative(root, path), 'reason': reason}


def worker_endpoint(record):
    return record.get('session_id') or record.get('agent_id')


def inbox_problem(root, project):
    inbox = root / 'inbox' / project
    if inbox.is_symlink() or not inbox.is_dir() or inbox.resolve() != inbox:
        return 'inbox of the project is missing or unsafe'
    return None


def registration_problem(root, record, projects):
    """The fixed reason a worker registration cannot receive a request, or None."""
    project = record.get('project')
    if not isinstance(project, str) or not PROJECT_NAME.fullmatch(project):
        return 'project name is invalid'
    if project not in projects:
        return 'project is not registered'
    endpoint = worker_endpoint(record)
    if not isinstance(endpoint, str) or not endpoint:
        return 'worker endpoint is missing'
    return inbox_problem(root, project)


def discover_workers(root):
    """The workers to ask, one per (project, endpoint), and the registrations that were skipped."""
    projects = project_registry(root)
    workers, rejected, seen = [], [], set()
    for path in registration_files(root):
        record = receive.read_json(path)
        if not isinstance(record, dict):
            rejected.append(skipped(root, path, 'registration is unreadable'))
            continue
        if record.get('role') != 'worker':
            continue
        problem = registration_problem(root, record, projects)
        if problem:
            rejected.append(skipped(root, path, problem))
            continue
        worker = (record['project'], worker_endpoint(record))
        if worker not in seen:
            seen.add(worker)
            workers.append(worker)
    return workers, rejected


def build_expected_reply(project, endpoint, orchestrator_endpoint, message_id):
    return {
        'type': 'status_report', 'to': 'orchestrator', 'to_session_id': orchestrator_endpoint,
        'in_reply_to': message_id, 'from_project': project, 'from_session_id': endpoint,
        'id': 'canonical UUID, equal to the reply filename',
        'created_at': 'UTC time of the report, ending in Z',
        'status': '|'.join(STATUSES),
        'task_ids': ['canonical UUIDs of the tasks you own, or an empty list'],
        'summary': 'what is done, what is in progress and what comes next',
        'task_id': 'only when blocked: canonical UUID of the blocked task you own',
        'blocker': {
            'context': 'only when blocked: what blocks the task and what was already tried',
            'question': 'only when blocked: one concrete question the orchestrator must decide',
            'options': ['optional answers you can choose between']}}


def build_request(slot, project, endpoint, orchestrator_endpoint, created_at):
    message_id = request_id(slot, project, endpoint)
    return {
        'id': message_id, 'type': 'status_request', 'from': 'orchestrator',
        'from_session_id': orchestrator_endpoint, 'to_project': project, 'to_session_id': endpoint,
        'intended_endpoint': endpoint, 'executable': False, 'slot': slot, 'created_at': created_at,
        'reply_to': 'orchestrator',
        'subject': f'Status report requested for {slot}',
        'body': ('Report your current status to the orchestrator. Answer only the latest status_request '
                 'addressed to you and archive older unanswered ones. This request is not executable and '
                 'authorizes no work. Reply with a status_report in inbox/orchestrator in the shape of '
                 'expected_reply. When you are blocked, set status to blocked and add the task_id of the '
                 'blocked task you own, the context of the blocker and one concrete question the '
                 'orchestrator must decide to unblock you.'),
        'expected_reply': build_expected_reply(project, endpoint, orchestrator_endpoint, message_id)}


def publish_new(root, project, message):
    """Link the message into the inbox unless it already exists there or in the archive."""
    name = f'{message["id"]}.json'
    final = root / 'inbox' / project / name
    archived = root / 'archive' / project / name
    if os.path.lexists(final) or os.path.lexists(archived):
        return 'existing'
    temporary = final.with_name(f'.{name}.{secrets.token_hex(8)}.tmp')
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o666)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
            handle.write(json.dumps(message, indent=2, ensure_ascii=False))
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, final)
        except FileExistsError:
            return 'existing'
        if os.path.lexists(archived):
            final.unlink(missing_ok=True)
            return 'existing'
        return 'published'
    finally:
        temporary.unlink(missing_ok=True)


def request_round(root, orchestrator_endpoint, moment):
    slot = floor_to_slot(moment).strftime(SLOT_FORMAT)
    created_at = moment.strftime(TIMESTAMP_FORMAT)
    workers, rejected = discover_workers(root)
    outcome = {'published': [], 'existing': []}
    for project, endpoint in workers:
        message = build_request(slot, project, endpoint, orchestrator_endpoint, created_at)
        state = publish_new(root, project, message)
        outcome[state].append(f'inbox/{project}/{message["id"]}.json')
    superseded, conflicts = supersede_older_requests(root, workers, slot)
    return {'slot': slot, 'published': outcome['published'], 'existing': outcome['existing'],
            'skipped': rejected, 'superseded': superseded, 'supersede_conflicts': conflicts}


def older_slot(text, current_slot):
    """True when text is a 15-minute aligned timezone-aware UTC slot earlier than current_slot."""
    moment = parse_aware(text)
    return moment is not None and moment == floor_to_slot(moment) and moment < parse_aware(current_slot)


def own_request(path, project, endpoint, current_slot):
    """The older status_request this orchestrator addressed to the worker at path, or None."""
    if not receive.UUID.fullmatch(path.stem):
        return None
    message = receive.read_json(path)
    if not isinstance(message, dict):
        return None
    slot = message.get('slot')
    if (message.get('type') != 'status_request' or message.get('from') != 'orchestrator'
            or message.get('to_project') != project or message.get('to_session_id') != endpoint
            or message.get('executable') is not False
            or not isinstance(message.get('from_session_id'), str)
            or not older_slot(slot, current_slot)):
        return None
    if message.get('id') != path.stem or path.stem != request_id(slot, project, endpoint):
        return None
    return message


def older_requests(root, worker, current_slot):
    """The older status_requests of the worker in its inbox as (path, record) pairs."""
    project, endpoint = worker
    inbox = root / 'inbox' / project
    found = []
    for path in sorted(inbox.glob('*.json')):
        message = own_request(path, project, endpoint, current_slot)
        if message:
            found.append((path, message))
    return found


def reply_index(root):
    """Every readable orchestrator reply, grouped by the request id it answers."""
    index = {}
    for path in reply_files(root):
        message = receive.read_json(path)
        if isinstance(message, dict) and isinstance(message.get('in_reply_to'), str):
            index.setdefault(message['in_reply_to'], []).append((path, message))
    return index


def is_answered(request, worker, replies, projects):
    project, endpoint = worker
    return any(is_reply_to_worker(message, path, projects, project, endpoint, request['id'],
                                  request['from_session_id'])
               for path, message in replies.get(request['id'], []))


def archive_dir(root, project):
    """archive/<project>/, created when missing; a symbolic link or an escaping path is refused."""
    folder = root / 'archive'
    for part in (folder, folder / project):
        if os.path.lexists(part) and (part.is_symlink() or part.resolve() != part):
            raise MailboxError(MAILBOX_LINE)
        part.mkdir(exist_ok=True)
        if part.is_symlink() or part.resolve() != part or not part.is_dir():
            raise MailboxError(MAILBOX_LINE)
    return folder / project


def archive_request(root, path, project):
    """'moved', 'conflict' (archive name taken) or 'gone' (the worker archived it first)."""
    target = archive_dir(root, project) / path.name
    try:
        os.link(path, target)
    except FileExistsError:
        return 'conflict'
    except FileNotFoundError:
        return 'gone'
    path.unlink(missing_ok=True)
    return 'moved'


def supersede_older_requests(root, workers, current_slot):
    """Archive each worker's older unanswered requests; returns (archived paths, conflicting paths)."""
    pending = [(worker, path, message) for worker in workers
               for path, message in older_requests(root, worker, current_slot)]
    if not pending:
        return [], []
    replies, projects = reply_index(root), project_registry(root)
    superseded, conflicts = [], []
    for worker, path, message in pending:
        if is_answered(message, worker, replies, projects):
            continue
        state = archive_request(root, path, worker[0])
        if state == 'moved':
            superseded.append(f'archive/{worker[0]}/{path.name}')
        elif state == 'conflict':
            conflicts.append(relative(root, path))
    return superseded, conflicts


def reply_files(root):
    inbox = root / 'inbox' / 'orchestrator'
    if inbox.is_symlink() or inbox.resolve() != inbox:
        raise MailboxError(MAILBOX_LINE)
    folders = [inbox]
    archive = root / 'archive' / 'orchestrator'
    if os.path.lexists(archive):
        folders.append(archive)
    files = []
    for folder in folders:
        try:
            files.extend(path for path in sorted(folder.iterdir())
                         if path.suffix == '.json' and receive.UUID.fullmatch(path.stem))
        except OSError:
            raise MailboxError(MAILBOX_LINE) from None
    return files


def is_reply_to_worker(message, path, projects, project, endpoint, message_id, orchestrator_endpoint):
    if message.get('id') != path.stem or message.get('type') != 'status_report':
        return False
    if not receive.valid_message(message, projects) or parse_aware(message.get('created_at')) is None:
        return False
    routes, _ = receive.destination(message)
    if 'orchestrator' not in routes or any(route not in (None, 'orchestrator') for route in routes):
        return False
    return (message.get('from_project') == project and message.get('from_session_id') == endpoint
            and message.get('in_reply_to') == message_id
            and message.get('to_session_id') in (None, orchestrator_endpoint))


def latest_replies(root, wanted, projects, orchestrator_endpoint):
    """The newest valid reply path and record per request id; ties go to the greatest id."""
    best = {}
    for path in reply_files(root):
        message = receive.read_json(path)
        if not isinstance(message, dict):
            continue
        target = wanted.get(message.get('in_reply_to'))
        if not target or not is_reply_to_worker(message, path, projects, *target, message['in_reply_to'],
                                                orchestrator_endpoint):
            continue
        rank = (parse_aware(message['created_at']).astimezone(timezone.utc), path.stem)
        if message['in_reply_to'] not in best or rank > best[message['in_reply_to']][0]:
            best[message['in_reply_to']] = (rank, path, message)
    return {key: (path, message) for key, (_, path, message) in best.items()}


def text_field(container, key):
    value = container.get(key) if isinstance(container, dict) else None
    return value.strip() if isinstance(value, str) and value.strip() else None


def known_task(root, project, task_id):
    if not isinstance(task_id, str) or not receive.UUID.fullmatch(task_id):
        return False
    return isinstance(receive.read_json(root / 'tasks' / project / f'{task_id}.json'), dict)


def blocked_report(root, worker, reply_path, reply):
    """A decision for the orchestrator, or the list of fields that make the blocked report incomplete."""
    project, endpoint = worker
    blocker = reply.get('blocker')
    task_id = reply.get('task_id')
    context, question = text_field(blocker, 'context'), text_field(blocker, 'question')
    missing = [field for field, present in (
        ('task_id', known_task(root, project, task_id)),
        ('blocker.context', context), ('blocker.question', question)) if not present]
    reference = {'project': project, 'session_id': endpoint, 'reply': relative(root, reply_path)}
    if missing:
        return None, dict(reference, missing_fields=missing)
    options = blocker.get('options')
    decision = root / 'decisions' / task_id / reply_path.name
    decided = receive.regular(decision)
    return dict(reference, task_id=task_id, context=context, question=question,
                options=[item for item in options if isinstance(item, str)] if isinstance(options, list) else [],
                decided=decided, decision_path=relative(root, decision) if decided else None), None


def collect_round(root, orchestrator_endpoint, slot):
    workers, _ = discover_workers(root)
    projects = project_registry(root)
    ids = {worker: request_id(slot, *worker) for worker in workers}
    wanted = {message_id: (*worker, ) for worker, message_id in ids.items()}
    replies = latest_replies(root, wanted, projects, orchestrator_endpoint)
    report = {'slot': slot, 'workers': [], 'missing': [], 'decisions_needed': [], 'incomplete': []}
    for worker in workers:
        project, endpoint = worker
        message_id = ids[worker]
        found = replies.get(message_id)
        status = found[1].get('status') if found else None
        report['workers'].append({
            'project': project, 'session_id': endpoint, 'request': message_id,
            'reply': relative(root, found[0]) if found else None,
            'status': status if status in STATUSES else None})
        if not found:
            report['missing'].append({'project': project, 'session_id': endpoint, 'request': message_id})
        elif status == 'blocked':
            decision, incomplete = blocked_report(root, worker, *found)
            report['decisions_needed'].extend([decision] if decision else [])
            report['incomplete'].extend([incomplete] if incomplete else [])
    return report


def with_context(root, report):
    """The collect report plus the context block of the tasks its decisions name."""
    decisions = report['decisions_needed']
    block = receive.attach_context(root, decisions, lambda decision: decision['task_id'])
    return dict(report, context=block)


def resolved_root(path):
    root = path.resolve()
    if not root.is_dir():
        raise MailboxError(MAILBOX_LINE)
    return root


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--root', type=Path)
    commands = parser.add_subparsers(dest='command', required=True)
    request = commands.add_parser('request')
    request.add_argument('--endpoint', required=True)
    request.add_argument('--at')
    collect = commands.add_parser('collect')
    collect.add_argument('--endpoint', required=True)
    collect.add_argument('--slot')
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    root = resolved_root(args.root if args.root is not None else default_root())
    if args.command == 'request':
        result = request_round(root, args.endpoint, requested_moment(args.at))
    else:
        result = with_context(root, collect_round(root, args.endpoint, requested_slot(args.slot, utc_now())))
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return result


if __name__ == '__main__':
    try:
        main()
    except UsageError:
        print(USAGE_LINE, file=sys.stderr)
        raise SystemExit(2)
    except MailboxError as error:
        raise SystemExit(str(error))
    except (OSError, ValueError, OverflowError):
        raise SystemExit(FAILURE_LINE)
