"""Read-only projection of the factory animation: lanes, boxes and ordered events.

Standard library only: no mailbox write, network or credential access. Lanes come from worker
session records, events from delivery history and decision records; nothing is inferred beyond
what those records state.
"""
from collections import Counter
from datetime import timezone

from context_graph import MESSAGE_BOXES, aware_time, first_text, identifier, nonempty_text, read_records, relative, text

STATIONS = ('investigating', 'implementation', 'review', 'verification')
STATUS_VALUES = ('working', 'idle', 'waiting', 'blocked')
REFUSAL_VERDICTS = ('changes_requested', 'returned', 'rejected', 'blocked', 'evidence_accepted_criteria_failed')
HISTORY_SOURCE, DECISION_SOURCE = 0, 1
MALFORMED_EVENT = 'malformed factory event'
DUPLICATE_TASK_IDENTITY = 'duplicate task identity'


def utc_moment(value):
    """The moment a timezone-aware ISO-8601 string names, expressed in UTC; None when it has none or UTC cannot hold it."""
    moment = aware_time(value)
    if moment is None:
        return None
    try:
        return moment.astimezone(timezone.utc)
    except OverflowError:
        return None


def utc_text(moment):
    return moment.isoformat().replace('+00:00', 'Z')


def lanes_from_sessions(session_records):
    """One lane per worker session id; the first record in path order wins."""
    lanes = {}
    for _source, record in session_records:
        session_id, project = nonempty_text(record.get('session_id')), nonempty_text(record.get('project'))
        if record.get('role') != 'worker' or not session_id or not project or session_id in lanes:
            continue
        lanes[session_id] = {
            'session_id': session_id, 'project': project, 'session_name': text(record.get('session_name')),
            'registered_status': text(record.get('status')), 'last_seen_at': text(record.get('last_seen_at')), 'last_status': None}
    return sorted(lanes.values(), key=lambda lane: (lane['project'], lane['session_id']))


def newest_status_reports(messages, session_ids):
    """The newest status_report by valid created_at, per session id in session_ids."""
    newest = {}
    for _source, message in messages:
        sender, moment = nonempty_text(message.get('from_session_id')), utc_moment(message.get('created_at'))
        if message.get('type') != 'status_report' or sender not in session_ids or moment is None:
            continue
        if sender not in newest or moment > newest[sender][0]:
            newest[sender] = (moment, message)
    return newest


def last_status(newest_report):
    if newest_report is None:
        return None
    moment, report = newest_report
    return {'status': report['status'], 'at': utc_text(moment)} if report.get('status') in STATUS_VALUES else None


def with_last_statuses(lanes, messages):
    newest = newest_status_reports(messages, {lane['session_id'] for lane in lanes})
    return [{**lane, 'last_status': last_status(newest.get(lane['session_id']))} for lane in lanes]


def history_entries(source, record, warnings):
    """(position, moment, entry) of every history entry with a valid time, in time order; others leave one warning."""
    history = record.get('history')
    entries, malformed = [], history is not None and not isinstance(history, list)
    for position, entry in enumerate(history if isinstance(history, list) else []):
        moment = utc_moment(entry.get('at')) if isinstance(entry, dict) else None
        if moment is None:
            malformed = True
        else:
            entries.append((position, moment, entry))
    if malformed:
        warnings.append(f'{source}: {MALFORMED_EVENT}')
    return sorted(entries, key=lambda item: (item[1], item[0]))


def stage_event(task_id, moment, kind, stage):
    event_id = f'{task_id}/exit' if kind == 'exit' else f'{task_id}/stage/{stage}'
    return {'id': event_id, 'at': utc_text(moment), 'task_id': task_id, 'kind': kind, 'stage': stage, 'gate': None}


def stage_events(task_id, entries):
    """enter, move and exit events from time-ordered history entries, each with its sort key.

    Returns (events, standing, exit_moment): standing lists (moment, station) after every enter and move,
    exit_moment is the time of the exit or None.
    """
    events, standing, exited = [], [], False
    for position, moment, entry in entries:
        stage = entry.get('stage')
        current = standing[-1][1] if standing else None
        if exited:
            break
        if stage == 'complete' and current:
            kind, stage = 'exit', current
            exited = True
        elif stage in STATIONS and current is None:
            kind = 'enter'
        elif stage in STATIONS and STATIONS.index(stage) > STATIONS.index(current):
            kind = 'move'
        else:
            continue
        if kind != 'exit':
            standing.append((moment, stage))
        events.append(((moment, task_id, HISTORY_SOURCE, position), stage_event(task_id, moment, kind, stage)))
    exit_moment = events[-1][0][0] if exited else None
    return events, standing, exit_moment


def refusal_gate(record):
    decision_type = first_text(record, 'decision_type', 'type') or ''
    return decision_type[:2] if decision_type[:2] in ('g1', 'g2') else 'report'


def is_refusal(record):
    return first_text(record, 'verdict', 'result', 'decision') in REFUSAL_VERDICTS


def station_at(standing, moment):
    """The station where the box stands at a moment: the latest enter or move not after it."""
    return [station for stood_at, station in standing if stood_at <= moment][-1]


def refusal_events(task_id, decisions, standing, exit_moment, warnings):
    events = []
    for source, record in decisions:
        if not is_refusal(record):
            continue
        moment = utc_moment(record.get('decided_at'))
        if moment is None:
            warnings.append(f'{source}: {MALFORMED_EVENT}')
        elif standing and standing[0][0] <= moment and (exit_moment is None or moment < exit_moment):
            name = source.rsplit('/', 1)[-1].removesuffix('.json')
            events.append(((moment, task_id, DECISION_SOURCE, name), {
                'id': f'{task_id}/refuse/{name}', 'at': utc_text(moment), 'task_id': task_id, 'kind': 'refuse',
                'stage': station_at(standing, moment), 'gate': refusal_gate(record)}))
    return events


def task_events(task_id, delivery, decisions, warnings):
    """The keyed events of one task from its delivery record (source, record) and decision records."""
    source, record = delivery
    stage_part, standing, exit_moment = stage_events(task_id, history_entries(source, record, warnings))
    return stage_part + refusal_events(task_id, decisions, standing, exit_moment, warnings)


def read_delivery(root, task_id, read_json, warnings):
    """(source, record) of deliveries/<task-id>.json, or None when it is absent or not a JSON object."""
    path = root / 'deliveries' / f'{task_id}.json'
    if not path.is_file():
        return None
    source, warned = relative(root, path), len(warnings)
    record = read_json(root, path, warnings)
    if isinstance(record, dict):
        return source, record
    if len(warnings) == warned:
        warnings.append(f'{source}: record is not a JSON object')
    return None


def keyed_events_of_task(root, task_id, read_json, warnings):
    delivery = read_delivery(root, task_id, read_json, warnings)
    if delivery is None:
        return []
    decisions = read_records(root, f'decisions/{task_id}', 0, read_json, warnings)
    return task_events(task_id, delivery, decisions, warnings)


def box_of(task):
    title = task.get('title')
    return {'task_id': task['id'], 'title': title if isinstance(title, str) else task['id'], 'project': task.get('project'),
            'session_id': task['owner_session_id'], 'stage': task.get('delivery_stage')}


def lane_tasks(tasks, lanes):
    """Dashboard tasks owned by a lane's session and carrying a valid identifier, sorted by id."""
    session_ids = {lane['session_id'] for lane in lanes}
    owned = [task for task in tasks if nonempty_text(task.get('owner_session_id')) in session_ids and identifier(task.get('id'))]
    return sorted(owned, key=lambda task: task['id'])


def duplicated_task_ids(tasks):
    """Valid task ids that the dashboard lists more than once."""
    counts = Counter(task['id'] for task in tasks if identifier(task.get('id')))
    return {task_id for task_id, count in counts.items() if count > 1}


def build_factory(dashboard, root, read_json, mode):
    """The GET /api/factory payload for a dashboard projection and the mailbox root."""
    warnings = []
    sessions = read_records(root, 'sessions', 0, read_json, warnings)
    messages = [found for box in MESSAGE_BOXES for found in read_records(root, box, 1, read_json, warnings)]
    lanes = with_last_statuses(lanes_from_sessions(sessions), messages)
    boxes, keyed_events = [], []
    duplicated = duplicated_task_ids(dashboard['tasks'])
    for task in lane_tasks(dashboard['tasks'], lanes):
        if task['id'] in duplicated:
            warnings.append(f"{task['source']}: {DUPLICATE_TASK_IDENTITY}")
            continue
        task_keyed = keyed_events_of_task(root, task['id'], read_json, warnings)
        if task_keyed:
            boxes.append(box_of(task))
            keyed_events += task_keyed
    events = [event for _key, event in sorted(keyed_events, key=lambda keyed: keyed[0])]
    return {'generated_at': dashboard['generated_at'], 'mode': mode, 'warnings': warnings, 'stations': list(STATIONS),
            'lanes': lanes, 'boxes': boxes, 'events': events}
