"""A synthetic mailbox for the factory animation: tests build it in a temporary directory, and
"python3 tests/factory_fixture.py <directory>" writes the same records for a real-browser proof.

Every name, id and time is invented. The module reads and writes nothing outside the given directory.
"""
import json
import sys
from pathlib import Path

ALPHA, BETA, ORCHESTRATOR = 'sess-alpha', 'sess-beta', 'sess-orchestrator'
GHOST_SESSION = 'sess-gone'
STATION_TASKS = {'investigating': 'T-STATION-1', 'implementation': 'T-STATION-2', 'review': 'T-STATION-3',
                 'verification': 'T-STATION-4'}
REFUSED_G1, REFUSED_G2, RETURNED, COMPLETE, GHOST = 'T-REFUSED-G1', 'T-REFUSED-G2', 'T-RETURNED', 'T-COMPLETE', 'T-GHOST'
CANCELLED = 'T-CANCELLED'
STAGE_ORDER = ('investigating', 'implementation', 'review', 'verification')


def stamp(minute):
    """A UTC timestamp `minute` minutes after the fixture's start."""
    return f'2026-11-20T{9 + minute // 60:02d}:{minute % 60:02d}:00Z'


def put(root, relative, record):
    path = Path(root) / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(record if isinstance(record, str) else json.dumps(record))
    return path


def history(*stages_at):
    return [{'stage': stage, 'at': stamp(minute)} for stage, minute in stages_at]


def put_task(root, project, task_id, owner_session_id, stages_at, decisions=None, status='in_progress'):
    """A task owned by a session, its delivery record (stage = the last history stage) and its decisions."""
    put(root, f'tasks/{project}/{task_id}.json', {'id': task_id, 'project': project, 'title': f'Title of {task_id}',
                                                   'status': status, 'owner_session_id': owner_session_id})
    put(root, f'deliveries/{task_id}.json', {'task_id': task_id, 'delivery_stage': stages_at[-1][0],
                                              'history': history(*stages_at)})
    for name, record in (decisions or {}).items():
        put(root, f'decisions/{task_id}/{name}.json', record)


def put_status_report(root, box, name, session_id, status, minute):
    put(root, f'{box}/{name}.json', {'id': name, 'type': 'status_report', 'to': 'orchestrator', 'from_session_id': session_id,
                                     'created_at': stamp(minute), 'status': status, 'summary': f'Synthetic report {name}'})


def put_sessions(root):
    put(root, 'projects.json', {'projects': [{'id': 'proj-alpha', 'name': 'Alpha'}, {'id': 'proj-beta', 'name': 'Beta'}]})
    for session_id, project, name in ((ALPHA, 'proj-alpha', 'alpha-worker'), (BETA, 'proj-beta', 'beta-worker')):
        put(root, f'sessions/{name}.json', {'session_id': session_id, 'project': project, 'role': 'worker', 'session_name': name,
                                           'status': 'active', 'last_seen_at': stamp(200)})
    put(root, 'sessions/orchestrator.json', {'session_id': ORCHESTRATOR, 'project': 'orchestrator', 'role': 'orchestrator',
                                            'session_name': 'orchestrator', 'status': 'active', 'last_seen_at': stamp(200)})


def put_tasks(root):
    alpha_stages = {'investigating': [('investigating', 1)], 'implementation': [('investigating', 1), ('implementation', 5)]}
    for stage, stages_at in alpha_stages.items():
        put_task(root, 'proj-alpha', STATION_TASKS[stage], ALPHA, stages_at)
    put_task(root, 'proj-beta', STATION_TASKS['review'], BETA, [('investigating', 2), ('implementation', 6), ('review', 10)])
    put_task(root, 'proj-beta', STATION_TASKS['verification'], BETA,
             [('investigating', 3), ('implementation', 7), ('review', 11), ('verification', 15)])
    put_task(root, 'proj-alpha', REFUSED_G1, ALPHA, [('investigating', 4)],
             {'g1': {'decision_type': 'g1', 'verdict': 'rejected', 'decided_at': stamp(8)}})
    put_task(root, 'proj-beta', REFUSED_G2, BETA, [('investigating', 4), ('implementation', 8), ('review', 12)],
             {'g2': {'decision_type': 'g2', 'verdict': 'changes_requested', 'decided_at': stamp(14)}})
    put_task(root, 'proj-alpha', RETURNED, ALPHA, [('investigating', 5), ('implementation', 9)],
             {'report-review': {'decision_type': 'report_review', 'verdict': 'returned', 'decided_at': stamp(13)}})
    put_task(root, 'proj-beta', COMPLETE, BETA,
             [('investigating', 6), ('implementation', 10), ('review', 14), ('verification', 18), ('complete', 22)])
    put_task(root, 'proj-alpha', GHOST, GHOST_SESSION, [('investigating', 7)])
    put_task(root, 'proj-alpha', CANCELLED, ALPHA, [('investigating', 8), ('implementation', 12)], status='cancelled')


def put_status_reports(root):
    put_status_report(root, 'inbox/orchestrator', 'report-newest', ALPHA, 'working', 190)
    put_status_report(root, 'archive/orchestrator', 'report-archived', ALPHA, 'idle', 100)
    put_status_report(root, 'outbox/proj-alpha', 'report-sent', ALPHA, 'blocked', 50)


def write_mailbox(root):
    """Write the synthetic mailbox into root: two worker lanes, a box at every station, the refusal cases and a cancelled task."""
    put_sessions(root)
    put_tasks(root)
    put_status_reports(root)


if __name__ == '__main__':
    if len(sys.argv) != 2:
        sys.exit('usage: python3 tests/factory_fixture.py <directory>')
    write_mailbox(sys.argv[1])
