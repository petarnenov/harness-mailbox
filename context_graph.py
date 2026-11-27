"""Read-only context projection layered on the dashboard dependency graph.

Standard library only: no mailbox write, network or credential access. Every node and edge is
derived from fields of existing mailbox records and names the record it came from. A reference
to a record that cannot be found or read stays unresolved; no node is invented for it.
"""
import copy
import re
from collections import defaultdict
from datetime import datetime
from pathlib import PurePosixPath

IDENTIFIER = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}')
COMMIT = re.compile(r'[0-9a-f]{7,40}')
MESSAGE_BOXES = ('inbox', 'outbox', 'archive')
RECORD_ROOTS = ('decisions', 'reports', 'dispatch-gates') + MESSAGE_BOXES
LIMITS = {'ancestors': 20, 'unmet_prerequisites': 20, 'decisions': 10, 'open_requests': 10, 'evidence': 10, 'warnings': 50}
RATIONALE_LIMIT = 600
CONDITION_COUNT = 10
CONDITION_LIMIT = 200
SUBJECT_LIMIT = 200


def text(value):
    return value if isinstance(value, str) else None


def nonempty_text(value):
    return value if isinstance(value, str) and value else None


def first_text(record, *fields):
    return next((record[field] for field in fields if isinstance(record.get(field), str)), None)


def identifier(value):
    return value if isinstance(value, str) and IDENTIFIER.fullmatch(value) else None


def commit_sha(value):
    return value if isinstance(value, str) and COMMIT.fullmatch(value) else None


def aware_time(value):
    """The moment a timezone-aware ISO-8601 string names, else None."""
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.utcoffset() is not None else None


def time_text(value):
    return value if aware_time(value) is not None else None


def newest_first(items, time_of):
    """Newest first; equal and missing times fall back to id order, missing times last."""
    ordered = sorted(items, key=lambda item: item['id'])
    dated = [item for item in ordered if aware_time(time_of(item)) is not None]
    undated = [item for item in ordered if aware_time(time_of(item)) is None]
    dated.sort(key=lambda item: aware_time(time_of(item)), reverse=True)
    return dated + undated


def relative(root, path):
    return path.relative_to(root).as_posix()


def list_directory(root, directory, warnings):
    try:
        return sorted(directory.iterdir())
    except FileNotFoundError:
        return []
    except OSError:
        warnings.append(f'{relative(root, directory)}: unreadable directory')
        return []


def record_paths(root, folder, depth, warnings):
    """JSON files below root/folder sorted by path; depth is the directory level they sit at, None for any.

    A symlinked directory is reported and never entered.
    """
    start = root / folder
    if start.is_symlink():
        warnings.append(f'{folder}: symlinked directory not followed')
        return []
    found, pending = [], [(start, 0)]
    while pending:
        directory, level = pending.pop()
        for entry in list_directory(root, directory, warnings):
            if entry.is_symlink() and entry.is_dir():
                warnings.append(f'{relative(root, entry)}: symlinked directory not followed')
            elif entry.is_dir():
                if depth is None or level < depth:
                    pending.append((entry, level + 1))
            elif entry.suffix == '.json' and (depth is None or depth == level):
                found.append(entry)
    return sorted(found, key=lambda path: relative(root, path))


def read_records(root, folder, depth, read_json, warnings):
    """(source, record) for every readable JSON object below the folder; others leave a warning."""
    records = []
    for path in record_paths(root, folder, depth, warnings):
        source, warned = relative(root, path), len(warnings)
        data = read_json(root, path, warnings)
        if isinstance(data, dict):
            records.append((source, data))
        elif len(warnings) == warned:
            warnings.append(f'{source}: record is not a JSON object')
    return records


def reference_path(value):
    """The record path a string refers to, or None when it is not a record reference."""
    if not isinstance(value, str):
        return None
    path = value.removeprefix('./')
    parts = path.split('/')
    if not path.endswith('.json') or len(parts) < 2 or parts[0] not in RECORD_ROOTS:
        return None
    return None if any(part in ('', '.', '..') or '\\' in part for part in parts) else path


def subject_ids(references):
    return [f'{prefix}:{references[field]}' for prefix, field in (('task', 'task_id'), ('issue', 'issue_key')) if references[field]]


class Projection:
    """The dashboard graph plus the context nodes and edges derived so far."""

    def __init__(self, graph):
        self.nodes = {node['id']: node for node in copy.deepcopy(graph['nodes'])}
        self.dashboard_edges = copy.deepcopy(graph['edges'])
        self.context_ids = set()
        self.edges = {}
        self.paths = {}
        self.warnings = []

    def add_node(self, node_id, kind, source, label, **attributes):
        if node_id not in self.nodes:
            self.nodes[node_id] = dict(id=node_id, kind=kind, source=source, label=label, **attributes)
            self.context_ids.add(node_id)

    def link(self, kind, start, end, source):
        key = (start, end, kind)
        if key in self.edges:
            return
        missing = [node_id for node_id in (start, end) if node_id not in self.nodes]
        if missing:
            self.warnings.append(f'{source}: {kind} reference could not be resolved')
        unresolved = bool(missing) or any(self.nodes[node_id].get('unresolved') for node_id in (start, end))
        self.edges[key] = {'from': start, 'to': end, 'kind': kind, 'source': source, 'unresolved': unresolved}

    def reference_target(self, value):
        """The node a record reference resolves to, else a record: id that no node carries."""
        path = reference_path(value)
        if path is None:
            return None
        return self.paths.get(path, 'record:' + path)

    def of_kind(self, kind):
        return [self.nodes[node_id] for node_id in sorted(self.context_ids) if self.nodes[node_id]['kind'] == kind]

    def graph(self):
        context_nodes = [self.nodes[node_id] for node_id in sorted(self.context_ids)]
        context_edges = [self.edges[key] for key in sorted(self.edges, key=lambda key: (key[2], key[0], key[1]))]
        dashboard_nodes = [node for node in self.nodes.values() if node['id'] not in self.context_ids]
        return {'nodes': dashboard_nodes + context_nodes, 'edges': self.dashboard_edges + context_edges}


def add_decisions(projection, records):
    for source, record in records:
        name = source.removeprefix('decisions/').removesuffix('.json')
        conditions = record.get('conditions')
        decision_type = first_text(record, 'decision_type', 'type')
        decided_at = time_text(first_text(record, 'decided_at', 'created_at'))
        projection.paths[source] = 'decision:' + name
        projection.add_node(
            'decision:' + name, 'decision', source, name, decision_type=decision_type,
            verdict=first_text(record, 'verdict', 'result', 'decision'), decided_at=decided_at,
            rationale=text(record.get('rationale')),
            conditions=[item for item in conditions if isinstance(item, str)] if isinstance(conditions, list) else [],
            references={'task_id': identifier(record.get('task_id')), 'issue_key': identifier(record.get('issue_key')),
                        'request_id': identifier(record.get('request_id')), 'supersedes': record.get('supersedes')},
            record_label=decision_type, recorded_at=decided_at)


def endpoint_project(value):
    """The project a message from/to value names: a plain string or an object with a project."""
    return text(value.get('project')) if isinstance(value, dict) else text(value)


def add_messages(projection, records):
    for source, record in sorted(records, key=lambda found: found[0]):
        message_id = nonempty_text(record.get('id')) or PurePosixPath(source).stem
        subject = first_text(record, 'subject', 'title')
        created_at = time_text(record.get('created_at'))
        message_type = text(record.get('type'))
        projection.paths[source] = 'message:' + message_id
        projection.add_node(
            'message:' + message_id, 'message', source, message_id, message_id=message_id, message_type=message_type,
            subject=subject[:SUBJECT_LIMIT] if subject else None,
            from_project=text(record.get('from_project')) or endpoint_project(record.get('from')) or text(record.get('project')),
            to_project=text(record.get('to_project')) or text(record.get('recipient')) or endpoint_project(record.get('to')),
            created_at=created_at,
            references={'task_id': identifier(record.get('task_id')), 'issue_key': identifier(record.get('issue_key')),
                        'in_reply_to': identifier(record.get('in_reply_to'))},
            record_label=message_type, recorded_at=created_at)


def commits_named_by(record):
    return [sha for sha in (commit_sha(record.get('head_sha')), commit_sha(record.get('merge_sha'))) if sha]


def add_reports(projection, records):
    for source, record in records:
        name = source.removeprefix('reports/').removesuffix('.json')
        report_kind = first_text(record, 'kind', 'type')
        created_at = time_text(record.get('created_at'))
        projection.paths[source] = 'evidence:report:' + name
        projection.add_node(
            'evidence:report:' + name, 'evidence', source, name, evidence_kind='report', report_id=text(record.get('id')),
            references={'task_id': identifier(record.get('task_id')), 'issue_key': identifier(record.get('issue_key')),
                        'supersedes_report': identifier(record.get('supersedes_report'))},
            commits=commits_named_by(record), record_label=report_kind, recorded_at=created_at)


def receipt_outcome(operation, allowed):
    if operation is None or allowed is None:
        return operation
    return f'{operation} {"allowed" if allowed else "denied"}'


def add_receipts(projection, records):
    for source, record in records:
        parts = PurePosixPath(source).parts
        name = f'{parts[1]}/{PurePosixPath(parts[2]).stem}'
        operation = text(record.get('operation'))
        allowed = record.get('allowed') if isinstance(record.get('allowed'), bool) else None
        projection.paths[source] = 'evidence:receipt:' + name
        projection.add_node(
            'evidence:receipt:' + name, 'evidence', source, name, evidence_kind='receipt', operation=operation, allowed=allowed,
            message_id=identifier(record.get('message_id')),
            references={'task_id': identifier(record.get('task_id')), 'issue_key': None},
            record_label=receipt_outcome(operation, allowed), recorded_at=time_text(record.get('created_at')))


def add_sessions(projection, records):
    for source, record in records:
        session_id = nonempty_text(record.get('session_id')) or PurePosixPath(source).stem
        last_seen_at = text(record.get('last_seen_at'))
        projection.add_node(
            'session:' + session_id, 'session', source, session_id, project=text(record.get('project')),
            role=text(record.get('role')), host=text(record.get('host')), status=text(record.get('status')),
            last_seen_at=last_seen_at, record_label=text(record.get('status')), recorded_at=time_text(last_seen_at))


def add_commits(projection, namings):
    """One commit node per sha; its source is the first record, in path order, that names it."""
    for sha, source in sorted(namings, key=lambda naming: (naming[1], naming[0])):
        projection.add_node('evidence:commit:' + sha, 'evidence', source, sha, evidence_kind='commit',
                            record_label=sha, recorded_at=None)


def read_task_records(root, tasks, read_json, warnings):
    """The raw record of every readable dashboard task, by task id."""
    records = {}
    for task in tasks:
        data = read_json(root, root / task['source'], warnings)
        if isinstance(data, dict):
            records[task['id']] = data
    return records


def link_subjects_to(projection, kind, node):
    """An edge of the kind from each task or issue the record names to the record."""
    for subject in subject_ids(node['references']):
        projection.link(kind, subject, node['id'], node['source'])


def decision_subjects(projection, node):
    explicit = subject_ids(node['references'])
    if explicit:
        return explicit
    parts = PurePosixPath(node['source']).parts
    parent = parts[-2] if len(parts) > 2 else None
    return [subject for subject in (f'task:{parent}', f'issue:{parent}') if parent and subject in projection.nodes]


def names_decision(target):
    return target is not None and (target.startswith('decision:') or target.startswith('record:decisions/'))


def link_decisions(projection):
    for node in projection.of_kind('decision'):
        for subject in decision_subjects(projection, node):
            projection.link('decides', node['id'], subject, node['source'])
        request_id = node['references']['request_id']
        if request_id:
            projection.link('in_reply_to', node['id'], 'message:' + request_id, node['source'])
        target = projection.reference_target(node['references']['supersedes'])
        if names_decision(target):
            projection.link('supersedes', node['id'], target, node['source'])


def link_messages(projection):
    for node in projection.of_kind('message'):
        reply = node['references']['in_reply_to']
        if reply:
            projection.link('in_reply_to', node['id'], 'message:' + reply, node['source'])


def link_reports(projection):
    report_ids = {}
    for node in projection.of_kind('evidence'):
        if node['evidence_kind'] == 'report' and node['report_id']:
            report_ids.setdefault(node['report_id'], node['id'])
    for node in projection.of_kind('evidence'):
        if node['evidence_kind'] != 'report':
            continue
        link_subjects_to(projection, 'evidenced_by', node)
        for sha in node['commits']:
            for subject in subject_ids(node['references']):
                projection.link('evidenced_by', subject, 'evidence:commit:' + sha, node['source'])
        superseded = node['references']['supersedes_report']
        if superseded:
            projection.link('supersedes', node['id'], report_ids.get(superseded, 'record:report-id:' + superseded), node['source'])


def link_receipts(projection):
    for node in projection.of_kind('evidence'):
        if node['evidence_kind'] != 'receipt':
            continue
        link_subjects_to(projection, 'evidenced_by', node)
        if node['operation'] == 'dispatch' and node['allowed'] is True:
            for subject in subject_ids(node['references']):
                projection.link('authorizes', node['id'], subject, node['source'])


def link_task(projection, task, record):
    node_id, source = 'task:' + task['id'], task['source']
    session_id = identifier(task.get('owner_session_id'))
    if session_id:
        projection.link('owned_by', node_id, 'session:' + session_id, source)
    evidence = record.get('evidence')
    for value in evidence if isinstance(evidence, list) else []:
        target = projection.reference_target(value)
        if target:
            projection.link('evidenced_by', node_id, target, source)
    for sha in commits_named_by(record):
        projection.link('evidenced_by', node_id, 'evidence:commit:' + sha, source)
    for field in ('g1_decision', 'g2_decision'):
        target = projection.reference_target(record.get(field))
        if names_decision(target):
            projection.link('authorizes', target, node_id, source)


def build_context_graph(dashboard, root, read_json, warnings):
    """The dashboard graph with decision, message, evidence and session nodes and their edges."""
    projection = Projection(dashboard['graph'])
    tasks = sorted(dashboard['tasks'], key=lambda task: task['id'])
    task_records = read_task_records(root, tasks, read_json, projection.warnings)
    add_decisions(projection, read_records(root, 'decisions', None, read_json, projection.warnings))
    messages = [found for box in MESSAGE_BOXES for found in read_records(root, box, 1, read_json, projection.warnings)]
    add_messages(projection, messages)
    add_reports(projection, read_records(root, 'reports', None, read_json, projection.warnings))
    add_receipts(projection, read_records(root, 'dispatch-gates', 1, read_json, projection.warnings))
    add_sessions(projection, read_records(root, 'sessions', 0, read_json, projection.warnings))
    namings = [(sha, node['source']) for node in projection.of_kind('evidence') for sha in node.get('commits', [])]
    namings += [(sha, task['source']) for task in tasks for sha in commits_named_by(task_records.get(task['id'], {}))]
    add_commits(projection, namings)
    link_decisions(projection)
    link_messages(projection)
    link_reports(projection)
    link_receipts(projection)
    for task in tasks:
        link_task(projection, task, task_records.get(task['id'], {}))
    warnings.extend(projection.warnings)
    return projection.graph()


class GraphIndex:
    def __init__(self, graph):
        self.nodes = {node['id']: node for node in graph['nodes']}
        self.by_kind = defaultdict(list)
        self.parents, self.members = defaultdict(set), defaultdict(set)
        for edge in graph['edges']:
            self.by_kind[edge['kind']].append(edge)
        for edge in self.by_kind['membership']:
            self.parents[edge['to']].add(edge['from'])
            self.members[edge['from']].add(edge['to'])

    def of_kind(self, kind):
        return sorted((node for node in self.nodes.values() if node['kind'] == kind), key=lambda node: node['id'])

    def scope_links(self, kind, scope, scope_end, other_end):
        """{other node id: sorted scope ids} for edges of the kind that touch the scope and an existing node."""
        found = defaultdict(set)
        for edge in self.by_kind[kind]:
            if edge[scope_end] in scope and edge[other_end] in self.nodes:
                found[edge[other_end]].add(edge[scope_end])
        return {node_id: sorted(ids) for node_id, ids in found.items()}


def ancestor_ids(index, node_id):
    """Membership ancestors as the dispatch gate walks them, nearest first, ties by id."""
    seen, ordered, frontier = {node_id}, [], [node_id]
    while frontier:
        frontier = sorted({parent for item in frontier for parent in index.parents.get(item, ())} - seen)
        seen.update(frontier)
        ordered.extend(frontier)
    return ordered


def pack_scope(index, node_id):
    """An issue pack covers the issue and its member tasks; a task pack covers the task."""
    members = sorted(item for item in index.members.get(node_id, ()) if item.startswith('task:'))
    return [node_id, *members] if node_id.startswith('issue:') else [node_id]


def string_or(value, fallback):
    return value if isinstance(value, str) else fallback


def graph_reference(node):
    return {'id': node['id'], 'kind': node['kind'], 'label': string_or(node.get('label'), node['id']),
            'status': string_or(node.get('status'), 'unknown')}


def pack_node(node):
    source = text(node.get('source')) or ''
    return {**graph_reference(node), 'ready': node.get('ready') is True, 'source': source.split(', ')[0] or None}


def owner_entry(index, task):
    session = index.nodes.get('session:' + string_or(task.get('owner_session_id'), ''), {})
    return {'task_id': task['id'], 'owner': text(task.get('owner')), 'session_id': text(task.get('owner_session_id')),
            'project': session.get('project'), 'last_seen_at': time_text(session.get('last_seen_at'))}


def prerequisite_owners(index, tasks_by_id, prerequisite_id):
    if prerequisite_id.startswith('task:'):
        task_ids = [prerequisite_id.removeprefix('task:')]
    else:
        task_ids = [item.removeprefix('task:') for item in sorted(index.members.get(prerequisite_id, ())) if item.startswith('task:')]
    return [owner_entry(index, tasks_by_id[task_id]) for task_id in task_ids if task_id in tasks_by_id]


def unmet_prerequisites(index, tasks_by_id, node_id, ancestors):
    """Own, then inherited, unmet prerequisites as the graph records them; each is listed once at its nearest origin."""
    listed, items = set(), []
    for holder, origin in [(node_id, None), *((ancestor, ancestor) for ancestor in ancestors)]:
        for prerequisite_id in sorted(index.nodes.get(holder, {}).get('unmet_prerequisites', ())):
            if prerequisite_id in listed or prerequisite_id not in index.nodes:
                continue
            listed.add(prerequisite_id)
            items.append({**graph_reference(index.nodes[prerequisite_id]), 'inherited_from': origin,
                          'owners': prerequisite_owners(index, tasks_by_id, prerequisite_id)})
    return items


def superseded_decisions(index):
    return {edge['from']: edge['to'] for edge in sorted(index.by_kind['supersedes'], key=lambda e: (e['from'], e['to']))
            if edge['to'] in index.nodes and not edge['unresolved']}


def decision_item(node, about, superseded):
    rationale = node['rationale']
    return {'id': node['id'], 'decision_type': node['decision_type'], 'verdict': node['verdict'], 'decided_at': node['decided_at'],
            'rationale': rationale[:RATIONALE_LIMIT] if rationale is not None else None,
            'rationale_truncated': rationale is not None and len(rationale) > RATIONALE_LIMIT,
            'conditions': [condition[:CONDITION_LIMIT] for condition in node['conditions'][:CONDITION_COUNT]],
            'about': about, 'supersedes': superseded.get(node['id']), 'source': node['source']}


def scope_decisions(index, scope):
    superseded = superseded_decisions(index)
    about = index.scope_links('decides', scope, 'to', 'from')
    items = [decision_item(index.nodes[node_id], ids, superseded) for node_id, ids in about.items()
             if index.nodes[node_id]['kind'] == 'decision']
    return newest_first(items, lambda item: item['decided_at'])


def answered_request_ids(index):
    """Message ids that have a reply message, a decision, or a validating receipt."""
    replied = {node['references']['in_reply_to'] for node in index.of_kind('message')}
    decided = {node['references']['request_id'] for node in index.of_kind('decision')}
    validated = {node['message_id'] for node in index.of_kind('evidence')
                 if node['evidence_kind'] == 'receipt' and node['operation'] == 'validate' and node['allowed'] is True}
    return replied, decided, validated


def is_open_request(node, answered):
    replied, decided, validated = answered
    message_id = node['message_id']
    if 'request' not in (node['message_type'] or '') or message_id in replied or message_id in decided:
        return False
    return not (node['message_type'] == 'execution_request' and message_id in validated)


def open_requests(index, scope):
    answered = answered_request_ids(index)
    items = []
    for node in index.of_kind('message'):
        about = sorted(set(subject_ids(node['references'])) & set(scope))
        if about and is_open_request(node, answered):
            items.append({'id': node['id'], 'type': node['message_type'], 'subject': node['subject'],
                          'from_project': node['from_project'], 'to_project': node['to_project'],
                          'created_at': node['created_at'], 'about': about, 'source': node['source']})
    return newest_first(items, lambda item: item['created_at'])


def evidence_item(node, about):
    return {'id': node['id'], 'kind': node.get('evidence_kind', 'record'), 'label': node.get('record_label'),
            'at': node.get('recorded_at'), 'about': about, 'source': text(node.get('source')) or None}


def scope_evidence(index, scope):
    about = index.scope_links('evidenced_by', scope, 'from', 'to')
    return newest_first([evidence_item(index.nodes[node_id], ids) for node_id, ids in about.items()],
                        lambda item: item['at'])


def assemble_pack(graph, tasks, warnings, generated_at, node_id):
    """The bounded context pack of a node of the graph; every list reports how many items it omitted."""
    index = GraphIndex(graph)
    tasks_by_id = {task['id']: task for task in tasks}
    ancestors = ancestor_ids(index, node_id)
    scope = pack_scope(index, node_id)
    lists = {'ancestors': [graph_reference(index.nodes[ancestor]) for ancestor in ancestors if ancestor in index.nodes],
             'unmet_prerequisites': unmet_prerequisites(index, tasks_by_id, node_id, ancestors),
             'decisions': scope_decisions(index, scope), 'open_requests': open_requests(index, scope),
             'evidence': scope_evidence(index, scope), 'warnings': sorted(set(warnings))}
    kept = {name: items[:LIMITS[name]] for name, items in lists.items()}
    return {'generated_at': generated_at, 'node': pack_node(index.nodes[node_id]), 'ancestors': kept['ancestors'],
            'unmet_prerequisites': kept['unmet_prerequisites'], 'decisions': kept['decisions'],
            'open_requests': kept['open_requests'], 'evidence': kept['evidence'],
            'omitted': {name: len(items) - len(kept[name]) for name, items in lists.items()}, 'warnings': kept['warnings']}


def build_context_pack(dashboard, root, read_json, node_id):
    """The context pack of a dashboard graph node, or None when the graph has no such node."""
    if not any(node['id'] == node_id for node in dashboard['graph']['nodes']):
        return None
    warnings = []
    graph = build_context_graph(dashboard, root, read_json, warnings)
    return assemble_pack(graph, dashboard['tasks'], warnings, dashboard['generated_at'], node_id)
