"""Periods in which an issue's agents waited for an answer instead of working.

A worker waits from a message that asks the orchestrator for something (a gate, a question, a
merge or a verification) until the orchestrator answers that task, either with a message to the
worker or with a recorded decision. An answer given only in a live session leaves no file, so
the worker's next message for the task closes the wait instead: work had resumed by then.
"""
import json
from datetime import datetime

AWAITING_ANSWER = frozenset({
    'question', 'blocker', 'gate', 'gate_request', 'intake_report', 'baseline_report',
    'g1_request', 'g1_package', 'g2_request', 'pr_opened', 'pr_ready', 'pr_report',
    'post_merge_verification', 'task_done', 'done', 'final_report',
})
ORCHESTRATOR = 'orchestrator'
ASK, ANSWER, RESUME = 'ask', 'answer', 'resume'


def parse_time(text):
    try:
        moment = datetime.fromisoformat(str(text).replace('Z', '+00:00'))
    except ValueError:
        return None
    return moment if moment.tzinfo else None


def read_json(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def sent_by_worker(folder, recipient):
    """Mailbox direction from the file location: outbox/<project> and the orchestrator inbox are the worker's."""
    return (folder == 'outbox') == (recipient != ORCHESTRATOR)


def message_events(root, task_ids):
    seen = set()
    for folder in ('outbox', 'inbox', 'archive'):
        for path in sorted(root.glob(f'{folder}/*/*.json')):
            message = read_json(path)
            if not isinstance(message, dict) or message.get('task_id') not in task_ids:
                continue
            moment = parse_time(message.get('created_at'))
            from_worker = sent_by_worker(folder, path.parent.name)
            key = (message.get('id') or str(path), from_worker)
            if moment is None or key in seen:
                continue
            seen.add(key)
            if from_worker:
                yield moment, ASK if message.get('type') in AWAITING_ANSWER else RESUME
            else:
                yield moment, ANSWER


def decision_events(root, task_ids):
    for path in root.glob('decisions/*/*.json'):
        decision = read_json(path)
        if not isinstance(decision, dict):
            continue
        if decision.get('task_id') in task_ids or decision.get('task') in task_ids:
            moment = parse_time(decision.get('decided_at') or decision.get('at'))
            if moment is not None:
                yield moment, ANSWER


def waiting_periods(root, task_ids):
    """Sorted (opened, closed) pairs; closed is None while the last wait is still unanswered."""
    task_ids = set(task_ids)
    periods, opened = [], None
    for moment, kind in sorted([*message_events(root, task_ids), *decision_events(root, task_ids)]):
        if kind == ASK:
            opened = opened or moment
        elif opened is not None:
            periods.append((opened, moment))
            opened = None
    if opened is not None:
        periods.append((opened, None))
    return periods


def waited_seconds(periods, start, end):
    """Total waiting inside [start, end]; an unanswered wait lasts until end."""
    total = 0.0
    for opened, closed in periods:
        overlap = (min(closed or end, end) - max(opened, start)).total_seconds()
        total += max(0.0, overlap)
    return total
