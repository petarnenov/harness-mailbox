"""Dependency eligibility and immutable mailbox dispatch receipts (stdlib only).

This is a supported protocol gate, not an interceptor for arbitrary filesystem writes.
Workers must validate immediately before claiming; existing task ownership/loop gates apply.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import uuid

from server import build_dashboard

SCHEMA_VERSION = 2
MAX_BYTES = 4_000_000
_LOADED_CODE = {name: hashlib.sha256((Path(__file__).parent / name).read_bytes()).hexdigest()
                for name in ('dispatch_gate.py', 'server.py', 'dependency_graph.py', 'delivery_process.py')}


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()


def _hash(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


def _uuid(value):
    return isinstance(value, str) and str(uuid.UUID(value)) == value


def _root(value):
    original = Path(value).absolute()
    if original.is_symlink() or not original.is_dir():
        raise ValueError('Mailbox root must be an existing non-symlink directory')
    return original.resolve()


def _safe(root, value, must_exist=True):
    path = Path(value)
    path = path if path.is_absolute() else root / path
    if '..' in path.parts:
        raise ValueError('Parent path traversal is not supported')
    if not path.resolve().is_relative_to(root):
        raise ValueError('Path is outside mailbox root')
    relative = path.relative_to(root)
    cursor = root
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ValueError('Symlink input/output is not supported: ' + str(relative))
    if must_exist and not path.is_file():
        raise ValueError('Missing input file: ' + str(relative))
    return path


def _read(root, value):
    path = _safe(root, value)
    if path.stat().st_size > MAX_BYTES:
        raise ValueError('Input exceeds 4 MB: ' + str(path.relative_to(root)))

    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                raise ValueError('Duplicate JSON key in ' + str(path.relative_to(root)) + ': ' + key)
            result[key] = item
        return result

    return json.loads(path.read_text(), object_pairs_hook=pairs,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError('Non-finite JSON: ' + value)))


def _snapshot(root):
    """Enumerated content hashes include additions/deletions and every projection input."""
    paths = [root / 'projects.json']
    for pattern in ('projects/*.json', 'tasks/*/*.json', 'epics/*/scope.json', 'epics/*/issues.json',
                    'epics/*/dependencies.json', 'epics/*/progress.json', 'sessions/*.json'):
        paths.extend(root.glob(pattern))
    inputs = {}
    for path in sorted(set(paths)):
        data = _read(root, path)
        relative = str(path.relative_to(root))
        if relative.startswith('sessions/'):
            if not isinstance(data, dict):
                raise ValueError('Malformed session registration: ' + relative)
            # Check-ins do not change dependency eligibility. Identity changes do.
            data = {key: data.get(key) for key in ('role', 'project', 'session_id', 'agent_id',
                                                 'working_directory', 'startup_working_directory')}
            inputs[relative] = _hash(data)
        else:
            inputs[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    for name in ('dispatch_gate.py', 'server.py', 'dependency_graph.py', 'delivery_process.py'):
        path = Path(__file__).parent / name
        if path.is_symlink() or not path.is_file():
            raise ValueError('Invalid gate code input: ' + name)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != _LOADED_CODE[name]:
            raise ValueError('Gate/projection code changed; restart this process: ' + name)
        inputs['@code/' + name] = digest
    return inputs, _hash(inputs)


def _base(operation, root=None):
    return dict(schema_version=SCHEMA_VERSION, operation=operation, allowed=False, reasons=[],
                root=str(root) if root else None, task_id=None, task_path=None, project=None,
                endpoint=None, message_id=None, fingerprint=None, prerequisites=[])


def _registry(root):
    registry = _read(root, 'projects.json')
    if not isinstance(registry, dict) or not isinstance(registry.get('projects'), list):
        raise ValueError('Malformed project registry')
    projects = {}
    for entry in registry['projects']:
        if not isinstance(entry, dict):
            raise ValueError('Malformed project registry entry: expected an object')
        key = entry.get('id')
        if not isinstance(key, str) or not key.strip() or '/' in key or '\\' in key or key in ('.', '..'):
            raise ValueError('Malformed project registry entry: invalid routing ID')
        if key in projects:
            raise ValueError('Duplicate project registry identity: ' + key)
        if not all(isinstance(entry.get(field), str) and entry[field].strip()
                   for field in ('working_directory', 'repository_path')):
            raise ValueError('Malformed project registry directory fields: ' + key)
        projects[key] = entry
    return projects


def _target(root, task_path):
    path = _safe(root, task_path)
    relative = path.relative_to(root)
    if len(relative.parts) != 3 or relative.parts[0] != 'tasks' or path.suffix != '.json' or not _uuid(path.stem):
        raise ValueError('Target must be tasks/<registered-project>/<canonical-UUID>.json')
    task = _read(root, path)
    if not isinstance(task, dict) or task.get('id') != path.stem or task.get('project') != relative.parts[1]:
        raise ValueError('Task filename, ID and project must agree')
    registry = _registry(root)
    if task['project'] not in registry:
        raise ValueError('Task project must have exactly one registry entry')
    identities = []
    for candidate in root.glob('tasks/*/*.json'):
        data = _read(root, candidate)
        if isinstance(data, dict) and data.get('id') == task['id']:
            identities.append(candidate)
    if len(identities) != 1:
        raise ValueError('Duplicate task identity')
    return task, str(relative), registry[task['project']]


def _list_of_strings(value):
    return isinstance(value, list) and all(isinstance(v, str) and bool(v.strip()) for v in value)


def _raw_dependencies(root):
    task_ids = set()
    for path in root.glob('tasks/*/*.json'):
        data = _read(root, path)
        if not isinstance(data, dict):
            continue  # The shared projection reports malformed record shapes.
        identity = data.get('id', path.stem)
        if isinstance(identity, str):
            if identity in task_ids:
                raise ValueError('Duplicate task identity in authoritative inputs: ' + identity)
            task_ids.add(identity)
        if 'depends_on' in data and not _list_of_strings(data['depends_on']):
            raise ValueError('Malformed depends_on declaration: ' + str(path.relative_to(root)))
        for field in ('issue_key', 'epic_key'):
            if data.get(field) is not None and (not isinstance(data[field], str) or not data[field].strip()):
                raise ValueError('Malformed task ownership ' + field + ': ' + str(path.relative_to(root)))
        if 'issue_keys' in data and not _list_of_strings(data['issue_keys']):
            raise ValueError('Malformed task ownership issue_keys: ' + str(path.relative_to(root)))
    for path in root.glob('epics/*/issues.json'):
        data = _read(root, path)
        if isinstance(data, dict):
            data = data.get('issues', data)
            if isinstance(data, dict):
                data = list(data.values())
        for issue in data if isinstance(data, list) else []:
            if isinstance(issue, dict):
                parent_key = issue.get('parent_key')
                parent = issue.get('parent')
                if parent_key is not None and (not isinstance(parent_key, str) or not parent_key.strip()):
                    raise ValueError('Malformed issue parent_key: ' + str(path.relative_to(root)))
                if parent is not None:
                    if not isinstance(parent, dict) or not isinstance(parent.get('key'), str) or not parent['key'].strip():
                        raise ValueError('Malformed issue parent.key: ' + str(path.relative_to(root)))
                    if parent_key is not None and parent_key != parent['key']:
                        raise ValueError('Conflicting issue parent declarations: ' + str(path.relative_to(root)))
                for field in ('depends_on', 'dependencies'):
                    if field in issue and not _list_of_strings(issue[field]):
                        raise ValueError('Malformed ' + field + ' declaration: ' + str(path.relative_to(root)))


# Fields that never decide eligibility: linked task lists (bound through the graph), edit times and the
# Jira status snapshot, which jira_sync.py refreshes and which never counts as local completion.
UNBOUND_ISSUE_FIELDS = ('task_ids', 'local_task_ids', 'updated_at', 'source_status', 'source_status_synced_at')


def _dependency_fingerprint(root, task, project, dashboard, context, prerequisites, snapshot):
    """Bind the task's incoming dependency closure, not unrelated mailbox activity.

    The whole graph is still rebuilt on validation, so newly added incoming edges,
    parent changes and prerequisite removals cannot disappear from this binding.
    Ancestor completion is not required; sibling task progress must not affect it.
    """
    nodes = []
    for node in dashboard['graph']['nodes']:
        if node['id'] not in context:
            continue
        fields = ('id', 'kind', 'unresolved')
        if node['id'] in prerequisites:
            fields += ('ready', 'status', 'evidence')
        nodes.append({key: node.get(key) for key in fields})
    edges = [edge for edge in dashboard['graph']['edges']
             if edge['to'] in context]
    records = {}
    supporting_tasks = {task_id for issue in dashboard['issues']
                        if 'issue:' + issue['key'] in prerequisites
                        for task_id in issue['task_ids']}
    for path in root.glob('tasks/*/*.json'):
        data = _read(root, path)
        if isinstance(data, dict) and ('task:' + str(data.get('id')) in context
                                      or data.get('id') in supporting_tasks):
            records[str(path.relative_to(root))] = data
    for path in root.glob('epics/*/scope.json'):
        data = _read(root, path)
        key = data.get('epic_key', data.get('key', path.parent.name))
        if 'epic:' + key in context:
            records[str(path.relative_to(root))] = {
                k: data[k] for k in ('acceptance_criteria', 'user_overrides',
                                    'delivery_scope', 'execution_scope_pending') if k in data}
    for path in root.glob('epics/*/issues.json'):
        data = _read(root, path)
        data = data.get('issues', data) if isinstance(data, dict) else data
        data = list(data.values()) if isinstance(data, dict) else data
        selected = []
        for issue in data if isinstance(data, list) else []:
            if not isinstance(issue, dict):
                continue
            key = 'issue:' + str(issue.get('key', issue.get('issue_key')))
            if key in context:
                # Sibling membership/progress does not authorize this task. The
                # graph above still binds membership of relevant tasks and the
                # computed readiness of an issue that IS a prerequisite.
                selected.append({k: v for k, v in issue.items() if k not in UNBOUND_ISSUE_FIELDS})
        if selected:
            records[str(path.relative_to(root))] = selected
    return _hash(dict(version=SCHEMA_VERSION, task=task, project=project,
                      nodes=sorted(nodes, key=lambda n: n['id']),
                      edges=sorted(edges, key=lambda e: (e['from'], e['to'], e['kind'])),
                      records=records,
                      loop_sha256=hashlib.sha256(Path(task['required_loop_path']).read_bytes()).hexdigest(),
                      code={k: v for k, v in snapshot.items() if k.startswith('@code/')}))


INVESTIGATION_TASK_KINDS = ('intake_baseline', 'intake_planning', 'baseline', 'evidence', 'investigation')


def _delivery_unit_violations(task, dashboard):
    """One execution task delivers at most one Jira Sub-task, so each Sub-task can be verified and closed
    after its own merge. Read-only investigation tasks may span several Sub-tasks."""
    if task.get('task_kind') in INVESTIGATION_TASK_KINDS:
        return []
    linked = sorted(issue['key'] for issue in dashboard['issues']
                    if issue.get('issue_type') == 'Sub-task' and task['id'] in issue.get('task_ids', []))
    if len(linked) > 1:
        return ['Execution task is linked to several Sub-tasks (' + ', '.join(linked) +
                '); create one execution task per Sub-task']
    return []


def _eligible(root, task_path, stabilize=True):
    result = _base('check', root)
    before, fingerprint = _snapshot(root)
    task, relative, project = _target(root, task_path)
    result.update(task_id=task['id'], task_path=relative, project=task['project'], fingerprint=fingerprint)
    reasons = result['reasons']
    if not _list_of_strings(task.get('depends_on')):
        reasons.append('Target depends_on must be an explicit list of nonblank strings')
    if task.get('status') in ('blocked', 'done'):
        reasons.append('Target status is an explicit hold: ' + task['status'])
    elif task.get('status') not in ('pending', 'in_progress'):
        reasons.append('Target status must be pending or in_progress')
    if task.get('blockers'):
        reasons.append('Target has explicit blockers; dependency eligibility does not clear holds')
    loop, loop_path = task.get('required_loop_skill'), task.get('required_loop_path')
    if not isinstance(loop, str) or not loop.strip() or not isinstance(loop_path, str) or not loop_path.strip() or not Path(loop_path).is_file():
        reasons.append('Target requires a named, existing project loop skill')
    _raw_dependencies(root)
    dashboard = build_dashboard(root)
    for warning in dashboard['warnings']:
        if not any(fragment in warning for fragment in ('unresolved graph endpoint', 'unresolved issue mapping', 'referenced tasks missing')):
            reasons.append('Authoritative graph warning: ' + warning)
    reasons.extend(_delivery_unit_violations(task, dashboard))
    nodes = {node['id']: node for node in dashboard['graph']['nodes']}
    target = 'task:' + task['id']
    if target not in nodes:
        reasons.append('Target missing from dashboard graph')
    raw_issue_keys = task.get('issue_keys', [])
    raw_issue_keys = raw_issue_keys if isinstance(raw_issue_keys, list) else [raw_issue_keys]
    raw_issue_keys += [task.get('issue_key'), task.get('epic_key')]
    for key in raw_issue_keys:
        if isinstance(key, str) and key and 'issue:' + key not in nodes and 'epic:' + key not in nodes:
            reasons.append('Target has unresolved ownership mapping: ' + key)
    membership, requires = {}, {}
    for edge in dashboard['graph']['edges']:
        destination = membership if edge['kind'] == 'membership' else requires
        destination.setdefault(edge['to'], set()).add(edge['from'])

    def ancestors(key):
        found, active = set(), set()

        def visit(item):
            if item in active:
                reasons.append('Relevant membership cycle: ' + item)
                return
            if item in found:
                return
            found.add(item)
            if item not in nodes or nodes[item].get('unresolved'):
                reasons.append('Unresolved membership context: ' + item)
            active.add(item)
            for parent in membership.get(item, ()):
                visit(parent)
            active.remove(item)
        visit(key)
        return found

    target_ancestors = ancestors(target)
    visited, visiting, prerequisites = set(), set(), set()
    context = set(target_ancestors)

    def walk(key):
        if key in visiting:
            reasons.append('Relevant dependency cycle: ' + key)
            return
        if key in visited:
            return
        visiting.add(key)
        inherited = ancestors(key)
        context.update(inherited)
        for owner in sorted(inherited):
            for prerequisite in sorted(requires.get(owner, ())):
                prerequisites.add(prerequisite)
                if prerequisite in target_ancestors:
                    reasons.append('Prerequisite equals target or its membership ancestor (cycle): ' + prerequisite)
                node = nodes.get(prerequisite)
                if not node or node.get('unresolved') or node.get('ready') is not True:
                    reasons.append('Unmet prerequisite: ' + prerequisite)
                walk(prerequisite)
        visiting.remove(key)
        visited.add(key)
    if target in nodes:
        walk(target)
    result['prerequisites'] = sorted(prerequisites)
    if not reasons:
        result['fingerprint'] = _dependency_fingerprint(
            root, task, project, dashboard, context, prerequisites, before)
    after, current = _snapshot(root)
    if before != after or current != fingerprint:
        # A different worker can publish while the graph is read. Rebuild once
        # from stable inputs; accept only the same relevant closure, never a
        # changed authorization. Sustained churn still fails closed.
        fresh = _eligible(root, task_path, stabilize=False) if stabilize and not reasons else None
        if not fresh or not fresh['allowed'] or fresh['fingerprint'] != result['fingerprint']:
            reasons.append('Authoritative input changed during eligibility evaluation')
    result['reasons'] = list(dict.fromkeys(reasons))
    result['allowed'] = not result['reasons']
    return result


def evaluate(root, task_path):
    """Pure dependency check; no audit or execution envelope is written."""
    try:
        canonical_root = _root(root)
        return _eligible(canonical_root, task_path)
    except (ValueError, OSError, TypeError, KeyError, RecursionError) as exc:
        result = _base('check', root)
        result.update(reasons=[str(exc)], input_error=True)
        # Preserve the requested task binding even when another source was malformed.
        try:
            canonical_root = _root(root)
            requested = _safe(canonical_root, task_path, must_exist=False).relative_to(canonical_root)
            result.update(root=str(canonical_root), task_path=str(requested))
            if len(requested.parts) == 3 and requested.parts[0] == 'tasks' and _uuid(requested.stem):
                result.update(task_id=requested.stem, project=requested.parts[1])
        except (ValueError, OSError, TypeError):
            pass
        return result


def _mkdir(root, path):
    path = _safe(root, path, must_exist=False)
    path.mkdir(parents=True, exist_ok=True)
    return _safe(root, path, must_exist=False)


def _publish(root, path, value):
    path = _safe(root, path, must_exist=False)
    _mkdir(root, path.parent)
    temporary = path.parent / ('.' + str(uuid.uuid4()) + '.tmp')
    try:
        with temporary.open('xb') as handle:
            handle.write(_canonical(value) + b'\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path)  # Atomic publication and no overwrite, even on racing writers.
    finally:
        temporary.unlink(missing_ok=True)


def _audit(root, result, identifier=None):
    identifier = identifier or str(uuid.uuid4())
    task_id = result.get('task_id')
    task_id = task_id if isinstance(task_id, str) and _uuid(task_id) else str(uuid.UUID(int=0))
    path = Path('dispatch-gates') / task_id / (identifier + '.json')
    record = dict(result, id=identifier, created_at=datetime.now(timezone.utc).isoformat(), audit_path=str(path))
    _publish(root, path, record)
    return record


def _worker(root, endpoint, project):
    if not isinstance(endpoint, str) or not endpoint.strip():
        raise ValueError('An exact worker endpoint is required')
    registry = _registry(root)
    if project not in registry:
        raise ValueError('Worker project is not registered')
    project_record = registry[project]
    registrations = []
    for path in root.glob('sessions/*.json'):
        data = _read(root, path)
        if isinstance(data, dict) and data.get('role') == 'worker' and data.get('project') == project and endpoint in (data.get('session_id'), data.get('agent_id')):
            cwd = data.get('working_directory', data.get('startup_working_directory'))
            expected = (project_record.get('working_directory'), project_record.get('repository_path'))
            if cwd in expected and isinstance(cwd, str):
                registrations.append({key: data.get(key) for key in
                    ('role', 'project', 'session_id', 'agent_id',
                     'working_directory', 'startup_working_directory')})
    if not registrations:
        raise ValueError('Worker endpoint is not self-registered for this exact project/directory')
    return _hash(sorted(registrations, key=lambda value: _canonical(value)))


def _paths_with_message(root, message_id):
    return list(root.glob('inbox/*/' + message_id + '.json')) + list(root.glob('archive/*/' + message_id + '.json'))


def _validate(root, message_path, endpoint):
    result = _base('validate', root)
    path = _safe(root, message_path)
    relative = path.relative_to(root)
    if len(relative.parts) != 3 or relative.parts[0] != 'inbox' or path.suffix != '.json' or not _uuid(path.stem):
        raise ValueError('Worker validates an existing UUID inbox envelope only; archived requests cannot execute')
    if _paths_with_message(root, path.stem) != [path]:
        raise ValueError('Message UUID is archived or duplicated; only its single original inbox location can execute')
    message = _read(root, path)
    if not isinstance(message, dict) or message.get('schema_version') != SCHEMA_VERSION or message.get('type') != 'execution_request' or message.get('id') != path.stem:
        raise ValueError('Unsupported or mismatched execution envelope')
    receipt_path = message.get('receipt_path')
    if not isinstance(receipt_path, str):
        raise ValueError('Execution envelope has no dispatch receipt')
    receipt = _read(root, receipt_path)
    if not isinstance(receipt, dict) or receipt.get('schema_version') != SCHEMA_VERSION or receipt.get('operation') != 'dispatch' or receipt.get('allowed') is not True:
        raise ValueError('Only an allowed dispatch receipt can authorize a request')
    if not _uuid(receipt.get('id')):
        raise ValueError('Dispatch receipt ID must be a canonical UUID')
    expected_receipt = 'dispatch-gates/' + str(message.get('task_id')) + '/' + str(receipt.get('id')) + '.json'
    if receipt_path != expected_receipt or receipt.get('audit_path') != receipt_path:
        raise ValueError('Receipt path/identity mismatch')
    decision = evaluate(root, message.get('task_path', ''))
    result.update(decision, operation='validate', endpoint=endpoint, message_id=message['id'], message_path=str(relative))
    checks = dict(root=str(root), task_id=decision.get('task_id'), task_path=decision.get('task_path'),
                  project=decision.get('project'), endpoint=endpoint, message_id=message['id'])
    for field, expected in checks.items():
        if receipt.get(field) != expected:
            result['reasons'].append('Receipt binding mismatch: ' + field)
    if message.get('task_id') != decision.get('task_id') or message.get('project') != decision.get('project') or relative.parts[1] != decision.get('project') or message.get('intended_endpoint') != endpoint:
        result['reasons'].append('Execution envelope task/project/recipient mismatch')
    if receipt.get('message_hash') != _hash(message):
        result['reasons'].append('Execution envelope content hash mismatch')
    if receipt.get('fingerprint') != decision.get('fingerprint'):
        result['reasons'].append('Dispatch receipt is stale: authoritative input fingerprint changed')
    if receipt.get('worker_fingerprint') != _worker(root, endpoint, decision.get('project')):
        result['reasons'].append('Dispatch receipt is stale: worker registration changed')
    if _paths_with_message(root, path.stem) != [path]:
        result['reasons'].append('Message UUID location changed during validation or is archived/duplicated')
    result['allowed'] = not result['reasons']
    return result


def validate(root, message_path, endpoint):
    """Audit worker validation immediately before claim; never claim or execute work."""
    try:
        root = _root(root)
        result = _validate(root, message_path, endpoint)
    except (ValueError, OSError, TypeError, KeyError, StopIteration, RecursionError) as exc:
        result = _base('validate', root)
        result.update(reasons=[str(exc)], endpoint=endpoint, input_error=True)
    try:
        return _audit(_root(root), result)
    except (ValueError, OSError, TypeError) as exc:
        result.update(allowed=False, input_error=True, reasons=result['reasons'] + ['Audit publication failed: ' + str(exc)])
        return result


def dispatch(root, task_path, endpoint, message_id=None):
    """Publish a receipt and fixed execution envelope only after fresh eligibility."""
    result = evaluate(root, task_path)
    result.update(operation='dispatch', endpoint=endpoint, message_id=message_id or str(uuid.uuid4()))
    lock = None
    try:
        root = _root(root)
        if not _uuid(result['message_id']):
            raise ValueError('Message ID must be a canonical UUID')
        if not result['allowed']:
            return _audit(root, result)
        result['worker_fingerprint'] = _worker(root, endpoint, result['project'])
        directory = _mkdir(root, Path('dispatch-gates') / result['task_id'])
        lock = directory / '.dispatch.lock'
        try:
            lock.mkdir()
        except FileExistsError:
            lock = None
            raise ValueError('Task dispatch is already being serialized')
        previous = _paths_with_message(root, result['message_id'])
        if previous:
            if len(previous) != 1 or previous[0].relative_to(root).parts[0] != 'inbox':
                raise ValueError('Message ID was already archived or has conflicting bindings; never republish')
            checked = _validate(root, previous[0], endpoint)
            if not checked['allowed'] or checked['task_id'] != result['task_id']:
                raise ValueError('Existing message UUID is stale, tampered or bound to another task/worker')
            return _audit(root, dict(result, message_path=str(previous[0].relative_to(root)),
                                     receipt_path=_read(root, previous[0])['receipt_path'], idempotent=True))
        for path in root.glob('inbox/*/*.json'):
            message = _read(root, path)
            if isinstance(message, dict) and message.get('type') == 'execution_request' and message.get('task_id') == result['task_id']:
                receipt = _read(root, message.get('receipt_path', ''))
                if isinstance(receipt, dict) and receipt.get('fingerprint') == result['fingerprint'] and receipt.get('allowed') is True:
                    raise ValueError('Task has an outstanding fresh execution envelope: ' + str(path.relative_to(root)))
        receipt_id = str(uuid.uuid4())
        receipt_path = str(Path('dispatch-gates') / result['task_id'] / (receipt_id + '.json'))
        message_path = str(Path('inbox') / result['project'] / (result['message_id'] + '.json'))
        message = dict(schema_version=SCHEMA_VERSION, type='execution_request', id=result['message_id'],
                       created_at=datetime.now(timezone.utc).isoformat(), sender='orchestrator',
                       recipient=result['project'], project=result['project'], task_id=result['task_id'],
                       task_path=result['task_path'], intended_endpoint=endpoint, receipt_path=receipt_path,
                       body='Validate this exact dispatch envelope before claiming or executing work. Read the bound task and actual project loop. Dependency eligibility does not replace user scope, integration synchronization, task ownership, G1/G2, quota or reachability requirements. Jira remains read-only. No execution is authorized by a missing, failed or stale receipt.')
        result.update(message_path=message_path, message_hash=_hash(message))
        current = evaluate(root, task_path)
        if not current['allowed'] or current['fingerprint'] != result['fingerprint']:
            raise ValueError('Authoritative input changed before receipt publication')
        receipt = _audit(root, result, receipt_id)
        current = evaluate(root, task_path)
        if not current['allowed'] or current['fingerprint'] != result['fingerprint']:
            raise ValueError('Authoritative input changed before inbox publication; receipt alone cannot execute')
        if _worker(root, endpoint, result['project']) != result['worker_fingerprint']:
            raise ValueError('Worker registration changed before inbox publication')
        _publish(root, message_path, message)
        return receipt
    except (ValueError, OSError, TypeError, KeyError, StopIteration, RecursionError) as exc:
        result.update(allowed=False, input_error=True, reasons=result['reasons'] + [str(exc)])
        try:
            return _audit(_root(root), result)
        except (ValueError, OSError, TypeError) as audit_error:
            result['reasons'].append('Audit publication failed: ' + str(audit_error))
            return result
    finally:
        if lock is not None:
            lock.rmdir()


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def main(argv=None):
    try:
        parser = _Parser(description=__doc__)
        parser.add_argument('--root', required=True)
        commands = parser.add_subparsers(dest='operation', required=True, parser_class=_Parser)
        for operation in ('check', 'dispatch', 'validate'):
            command = commands.add_parser(operation)
            command.add_argument('--message' if operation == 'validate' else '--task', required=True)
            if operation != 'check':
                command.add_argument('--worker-id', required=True)
            if operation == 'dispatch':
                command.add_argument('--message-id')
        args = parser.parse_args(argv)
        if args.operation == 'check':
            result = _audit(_root(args.root), evaluate(args.root, args.task))
        elif args.operation == 'dispatch':
            result = dispatch(args.root, args.task, args.worker_id, args.message_id)
        else:
            result = validate(args.root, args.message, args.worker_id)
    except (ValueError, OSError, TypeError) as exc:
        result = dict(_base('error'), input_error=True, reasons=[str(exc)])
    print(json.dumps(result, sort_keys=True))
    return 0 if result['allowed'] else 1 if result.get('input_error') else 2


if __name__ == '__main__':
    sys.exit(main())
