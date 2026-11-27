"""Inspect pending coordinator requests. Every result is a pointer for agent review."""
import argparse
from datetime import datetime
import json
import math
import os
import re
import stat
import sys
import time
from contextlib import contextmanager
from pathlib import Path

UUID = re.compile(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}')


def regular(path):
    return path.resolve() == path and path.is_file()


def finite_number(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError('Invalid JSON number')
    return number


def read_json(path):
    if not regular(path):
        return None
    try:
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                return {}
            raw = stream.read(4 * 1024 * 1024 + 1)
        if len(raw) <= 4 * 1024 * 1024:
            return json.loads(raw.decode('utf-8'), parse_float=finite_number, parse_constant=finite_number)
    except (FileNotFoundError, UnicodeError, ValueError, RecursionError):
        pass
    return None


def envelope(path):
    value = read_json(path)
    return value if isinstance(value, dict) else {}


def project_ids(root):
    registry = read_json(root / 'projects.json')
    entries = registry.get('projects', registry) if isinstance(registry, dict) else registry
    if isinstance(entries, dict):
        entries = [dict({'id': key}, **entry) for key, entry in entries.items() if isinstance(entry, dict)]
    if not isinstance(entries, list):
        raise ValueError('Unavailable project registry')
    by_key = {}
    for entry in entries:
        project = entry.get('id') if isinstance(entry, dict) else None
        if isinstance(project, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', project):
            key = project.lower()
            if key != 'orchestrator':
                by_key[key] = None if key in by_key else project
    return {name for name in by_key.values() if name}


def sender_project(message):
    sender = message.get('from')
    return message.get('from_project') or (
        sender.get('project') if isinstance(sender, dict) else sender) or message.get('project')


def valid_message(message, projects):
    timestamp = message.get('created_at')
    if not isinstance(timestamp, str) or not re.search(r'(Z|[+-][0-9]{2}:[0-9]{2})$', timestamp):
        return False
    try:
        if datetime.fromisoformat(timestamp.replace('Z', '+00:00')).tzinfo is None:
            return False
    except ValueError:
        return False
    sender = sender_project(message)
    kind = message.get('type')
    reply = message.get('in_reply_to')
    if not isinstance(sender, str) or sender not in projects:
        return False
    if reply is not None and not (isinstance(reply, str) and UUID.fullmatch(reply)):
        return False
    if reply is None and not (isinstance(kind, str) and kind):
        return False
    if kind == 'registration_offer':
        origin = message.get('from')
        return (isinstance(origin, dict) and origin.get('project') == sender
                and message.get('executable') is False and isinstance(origin.get('session_id'), str)
                and UUID.fullmatch(origin['session_id']) is not None)
    return kind != 'execution_request' and message.get('executable') is not True


def destination(message):
    to = message.get('to')
    routes = [message.get('recipient'), message.get('to_project')]
    endpoints = [message.get(key) for key in ('to_endpoint', 'to_session_id', 'intended_endpoint')]
    if isinstance(to, dict):
        routes.extend((to.get('project'), to.get('role')))
        endpoints.extend(to.get(key) for key in ('session_id', 'endpoint', 'agent_id'))
    else:
        routes.append(to)
    return routes, endpoints


def belongs_to_other_session(root, reply, endpoint):
    request_id = reply.get('in_reply_to')
    project = sender_project(reply)
    if not isinstance(project, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', project):
        return False
    if not isinstance(request_id, str) or not UUID.fullmatch(request_id):
        return False
    for folder in ('inbox', 'archive'):
        request = envelope(root / folder / project / (request_id + '.json'))
        if request.get('id') != request_id:
            continue
        origin = request.get('from')
        owners = [request.get('from_session_id')]
        if isinstance(origin, dict):
            owners.append(origin.get('session_id'))
        if any(owner not in (None, endpoint) for owner in owners):
            return True
    return False


def pending(root, endpoint):
    inbox = root / 'inbox' / 'orchestrator'
    if inbox.resolve() != inbox:
        raise ValueError('Unexpected orchestrator inbox path')
    projects = project_ids(root)
    requests = []
    for path in sorted(inbox.iterdir()):
        if path.suffix != '.json' or not UUID.fullmatch(path.stem):
            continue
        message = envelope(path)
        if message.get('id') != path.stem or regular(root / 'archive' / 'orchestrator' / path.name):
            continue
        if not valid_message(message, projects):
            continue
        routes, endpoints = destination(message)
        if 'orchestrator' not in routes or any(route not in (None, 'orchestrator') for route in routes):
            continue
        if any(target not in (None, endpoint) for target in endpoints):
            continue
        if not any(endpoints) and belongs_to_other_session(root, message, endpoint):
            continue
        item = dict(id=path.stem, path=str(path.relative_to(root)), step='inspect')
        task = message.get('task_id')
        if any(endpoints) and isinstance(task, str) and UUID.fullmatch(task):
            decision = root / 'decisions' / task / path.name
            if regular(decision):
                item.update(step='reconcile', decision_path=str(decision.relative_to(root)))
            elif message.get('type') in ('g1_request', 'g2_request', 'pr_ready', 'pr_opened', 'escalation'):
                item['step'] = 'review'
        requests.append(item)
    return requests


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--endpoint', required=True)
    parser.add_argument('--active-goal', action='store_true')
    parser.add_argument('--wake-confirmed', action='store_true')
    parser.add_argument('--wait', type=int, choices=range(61), default=0)
    args = parser.parse_args()
    try:
        result = receive(args)
    except (OSError, RuntimeError, ValueError):
        parser.exit(1, 'Cannot inspect mailbox inbox/orchestrator. Check its path and permissions.\n')
    print(json.dumps(result))


def named_task(root, item):
    """The UUID task id the pending request's message names, or None."""
    task = envelope(root / item['path']).get('task_id')
    return task if isinstance(task, str) and UUID.fullmatch(task) else None


def unavailable_context():
    return {'available': False, 'packs': {}, 'omitted': 0}


def empty_context():
    return {'available': True, 'packs': {}, 'omitted': 0}


@contextmanager
def no_bytecode_written():
    """Imports inside the block write no __pycache__ files: this script must leave the mailbox untouched."""
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        yield
    finally:
        sys.dont_write_bytecode = previous


def attach_context(root, items, task_id_of):
    """The context block of the tasks the items name; marks each item that has a pack.

    Every failure, including a context_pack that is missing or fails on import, yields the unavailable block.
    """
    try:
        tasks = {id(item): task_id_of(item) for item in items}
        named = [item for item in items if tasks[id(item)]]
        if not named:
            return empty_context()
        with no_bytecode_written():
            import context_pack  # lazy: a missing or broken context_pack must not break the script that uses it
            block = context_pack.context_block(root, [tasks[id(item)] for item in named])
            context_pack.mark_context_nodes(named, lambda item: tasks[id(item)], block)
    except Exception:
        return unavailable_context()
    return block


def receive(args):
    if not args.active_goal:
        return dict(state='idle', requests=[], context=attach_context(args.root, [], named_task))
    root = args.root.resolve()
    requests = pending(root, args.endpoint)
    if not requests and args.wait and not args.wake_confirmed:
        time.sleep(args.wait)
        requests = pending(root, args.endpoint)
    state = 'pending' if requests else ('yield' if args.wake_confirmed else 'wait')
    return dict(state=state, requests=requests, context=attach_context(root, requests, lambda item: named_task(root, item)))


if __name__ == '__main__':
    main()
