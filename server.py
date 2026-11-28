"""Read-only mailbox projection. No Jira access and no writes to mailbox data."""
import json
import math
import os
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from dependency_graph import build_graph
from delivery_process import STAGES, project_delivery

ROOT = Path(__file__).parent
VENDORED_SCRIPTS = {f'/vendor/{name}': (f'vendor/{name}', 'text/javascript; charset=utf-8')
                    for name in ('pixi-8.23.0.min.js', 'pixi-unsafe-eval-8.23.0.min.js', 'gsap-3.15.1.min.js')}
CLOSED_TASK_STATUSES = ('done', 'cancelled')
STATUSES = ('pending', 'in_progress', 'blocked', 'awaiting_verification', 'verified_done', 'deferred')


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def records(value, field):
    """Accept documented list, wrapped list or keyed-object snapshot formats."""
    if isinstance(value, dict):
        value = value.get(field, value)
        if isinstance(value, dict):
            return [dict(v, **({'key': k} if 'key' not in v else {})) for k, v in value.items() if isinstance(v, dict)]
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


def strings(value):
    if isinstance(value, str):
        return [value]
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


TEAM_ALIASES = {'be': 'BE', 'backend': 'BE', 'back-end': 'BE',
                'fe': 'FE', 'frontend': 'FE', 'front-end': 'FE',
                'qa': 'QA', 'quality assurance': 'QA'}
PROJECT_TEAMS = {'frontend': ['FE'], 'geowealth': ['BE'], 'admin-center': ['BE'],
                 'identity-service': ['BE'], 'user-context': ['BE']}


def explicit_teams(record):
    value = record.get('teams', record.get('team'))
    if not isinstance(value, (str, list)):
        return None
    return sorted({TEAM_ALIASES[v.strip().lower()] for v in strings(value)
                   if v.strip().lower() in TEAM_ALIASES})


def title_teams(record):
    title = str(record.get('title', record.get('summary', ''))).strip()
    if re.search(r'\b(?:tests?|testing|qa|quality[ -]assurance)\b', title, re.I):
        return ['QA']
    if re.match(r'^\[?(?:UI|FE|frontend|front-end)\b', title, re.I):
        return ['FE']
    if re.match(r'^\[?(?:DEV|BE|backend|back-end)\b', title, re.I):
        return ['BE']
    return None


def record_teams(record, fallback):
    explicit = explicit_teams(record)
    return explicit if explicit is not None else title_teams(record) or fallback


class UnreadableRecord(Exception):
    """A mailbox file the guarded reader refuses; the message is a code-written reason."""


def reject_non_finite(literal):
    raise UnreadableRecord('Non-finite JSON number: ' + literal)


def finite_float(literal):
    value = float(literal)
    if not math.isfinite(value):
        reject_non_finite(literal)
    return value


def finite_int(literal):
    finite_float(literal)
    return int(literal)


def load_guarded_json(root, path):
    try:
        if not path.resolve().is_relative_to(root.resolve()) or path.is_symlink():
            raise UnreadableRecord('symlink/outside data root')
        if path.stat().st_size > 4_000_000:
            raise UnreadableRecord('exceeds 4 MB limit')
        return json.loads(path.read_text(encoding='utf-8'), parse_constant=reject_non_finite, parse_float=finite_float, parse_int=finite_int)
    except OSError:
        raise UnreadableRecord('unreadable file') from None
    except ValueError:
        raise UnreadableRecord('invalid JSON') from None


def record_label(root, path):
    try:
        return str(path.relative_to(root))
    except ValueError:
        return path.name


def read_json(root, path, warnings):
    try:
        return load_guarded_json(root, path)
    except UnreadableRecord as rejection:
        warnings.append(f'{record_label(root, path)}: {rejection}')
        return None


def task_link_fields(data, source, warnings):
    """Task issue_key, epic_key and status as projected; malformed values are reported, never raised."""
    fields = {'issue_key': data.get('issue_key'), 'epic_key': data.get('epic_key'), 'status': data.get('status', 'pending')}
    if not isinstance(fields['issue_key'], (str, list, type(None))):
        warnings.append(f'{source}: invalid issue_key ignored')
        fields['issue_key'] = None
    if not isinstance(fields['epic_key'], (str, type(None))):
        warnings.append(f'{source}: invalid epic_key ignored')
        fields['epic_key'] = None
    if not isinstance(fields['status'], str):
        warnings.append(f'{source}: invalid task status ignored')
        fields['status'] = 'unknown'
    return fields


def withhold_duplicate_task_evidence(tasks, warnings):
    """A task id found in several records has no trustworthy completion evidence, whatever the file order."""
    sources = {}
    for task in tasks:
        sources.setdefault(task['id'], []).append(task['source'])
    duplicated = {task_id for task_id, found_in in sources.items() if len(found_in) > 1}
    for task_id in sorted(duplicated):
        warnings.append(f"{task_id}: duplicate task identity in {', '.join(sorted(sources[task_id]))}; completion evidence ignored")
    for task in tasks:
        if task['id'] in duplicated:
            task['evidenced'] = False


def read_scope(root, folder, warnings):
    """The epic scope object, `{}` when the file is absent, `None` after a warning when it is not an object."""
    path = folder / 'scope.json'
    if not path.exists():
        return {}
    try:
        scope = load_guarded_json(root, path)
    except UnreadableRecord as rejection:
        warnings.append(f'{record_label(root, path)}: {rejection}')
        return None
    if isinstance(scope, dict):
        return scope
    warnings.append(f'{path.relative_to(root)}: scope is not a JSON object')
    return None


def task_delivery_stage(root, task_id, warnings):
    """Process stage of a task from its orchestrator-owned deliveries/<task-id>.json record."""
    if Path(task_id).name != task_id:
        return 'unknown'
    path = root / 'deliveries' / f'{task_id}.json'
    if not path.is_file():
        return 'unknown'
    data = read_json(root, path, warnings)
    stage = data.get('delivery_stage') if isinstance(data, dict) else None
    if stage not in STAGES:
        warnings.append(f'{path.relative_to(root)}: unsupported or missing delivery_stage')
        return 'unknown'
    return stage


def reported_estimate_hours(record):
    """`codex_estimate_hours` is the original name of the host-neutral `agent_estimate_hours`."""
    return estimate_range(record.get('agent_estimate_hours')) or estimate_range(record.get('codex_estimate_hours'))


def original_estimate_hours(record):
    """The Jira original estimate, which an import records as `estimate: {value, unit: "hours"}`."""
    estimate = record.get('estimate')
    if isinstance(estimate, dict) and estimate.get('unit') == 'hours' and number(estimate.get('value')):
        return estimate['value']
    return None


def worked_so_far(root, warnings):
    """Hours the agents worked per issue and when they were measured (`jira_sync.py worked`)."""
    path = root / 'jira-sync' / 'worked-so-far.json'
    snapshot = read_json(root, path, warnings) if path.exists() else None
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    seconds = snapshot.get('worked_seconds')
    hours = {key: round(value / 3600, 2) for key, value in seconds.items() if number(value)} if isinstance(seconds, dict) else {}
    return {'measured_at': snapshot.get('measured_at') if isinstance(snapshot.get('measured_at'), str) else None, 'hours': hours}


def estimate_range(value):
    if isinstance(value, dict) and all(number(value.get(k)) for k in ('low', 'likely', 'high')):
        if value['low'] <= value['likely'] <= value['high']:
            return {k: value[k] for k in ('low', 'likely', 'high')}
    return None


def evidence(value):
    return bool(value.get('evidence') or value.get('acceptance_criterion_evidence') or value.get('acceptance_criteria_evidence'))


def evidence_refs(record):
    """Preserve strings in structured worker evidence as well as path lists."""
    pending = [record.get(key) for key in ('evidence', 'acceptance_criterion_evidence', 'acceptance_criteria_evidence')]
    result = []
    while pending:
        value = pending.pop()
        if isinstance(value, str) and value.strip():
            result.append(value)
        elif isinstance(value, dict):
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
    return sorted(set(result))


def verification_passed(value):
    verification = value.get('verification', value.get('verification_outcome'))
    if isinstance(verification, dict):
        verification = verification.get('outcome', verification.get('status'))
    return verification in ('passed', 'verified', 'pass') and evidence(value)


OVERFLOW_WARNING = 'Estimate or measured-hour totals exceed the supported number range; the affected totals are unavailable'


def rounded_range(sums):
    """Rounded low/likely/high sums, or None when any of the three is not finite."""
    return {k: round(v, 2) for k, v in sums.items()} if all(math.isfinite(v) for v in sums.values()) else None


def within_float_range(hours):
    """Exact for integers of any size, unlike math.isfinite, which converts them to float."""
    return abs(hours) <= sys.float_info.max


def rounded_hours_total(hours):
    """Rounded sum of measured hours, or None when the sum is beyond the float range."""
    try:
        total = sum(hours)
    except OverflowError:
        # An exact integer running total beyond the float range met a decimal; every value is >= 0, so the sum is beyond the range.
        return None
    return round(total, 2) if within_float_range(total) else None


def has_overflowed_total(progress):
    return ((progress['known_issues'] > 0 and (progress['estimate'] is None or progress['remaining_estimate'] is None))
            or (progress['measured_issues'] > 0 and progress['actual_machine_hours'] is None))


def has_overflowed_allocation(project):
    return bool(project['issue_keys']) and project['allocated_estimate'] is None


def has_overflowed_totals(totals, epics, projects):
    return (has_overflowed_total(totals)
            or any(has_overflowed_total(e['progress']) for e in epics)
            or any(has_overflowed_total(p['progress']) or has_overflowed_allocation(p) for p in projects))


def metrics(issues, complete, model):
    counts = Counter(i['local_status'] for i in issues)
    total = len(issues)
    verified = sum(i['verified'] for i in issues)
    totals = {k: 0.0 for k in ('low', 'likely', 'high')}
    remaining = totals.copy()
    actuals = []
    for issue in issues:
        estimate = issue['estimate'] or {k: model[k] * model['review_multiplier'] for k in totals}
        for key in totals:
            totals[key] += estimate[key]
            if not issue['verified']:
                remaining[key] += estimate[key]
        if issue['actual_machine_hours'] is not None:
            actuals.append(issue['actual_machine_hours'])
    return {'known_issues': total, 'verified_issues': verified, 'status_counts': dict(counts),
            'scope_complete': complete, 'percent': round(100 * verified / total, 1) if complete and total else None,
            'estimate': rounded_range(totals) if total else None,
            'remaining_estimate': rounded_range(remaining) if total else None,
            'estimate_basis': 'Complete enumerated issue scope' if complete and total else 'Known issue subset only; full estimate unavailable',
            'explicit_estimates': sum(i['estimate'] is not None for i in issues),
            'actual_machine_hours': rounded_hours_total(actuals) if actuals else None, 'measured_issues': len(actuals)}


def build_dashboard(root, model=None, mode='live'):
    root = Path(root)
    model = model or {'low': 0.5, 'likely': 1.5, 'high': 4.0, 'review_multiplier': 1.25}
    warnings, delivery_warnings, projects, tasks, epics, raw_issues, reported, external = [], [], {}, [], [], [], [], []

    def project(pid, name=None, registered=False):
        if not isinstance(pid, str) or not pid:
            warnings.append('Invalid project identifier ignored')
            return
        name = name if isinstance(name, str) else pid
        if pid == 'orchestrator':
            name = 'Mailbox coordination'
        entry = projects.setdefault(pid, {'id': pid, 'name': name or pid, 'registered': False, 'kind': 'coordination' if pid == 'orchestrator' else 'project',
                                         'reported_estimate': None, 'reported_actual_machine_hours': None, 'teams': PROJECT_TEAMS.get(pid, [])})
        if registered:
            entry.update(name=name or pid, registered=True)
        return entry

    def reported_stats(data, source, project_id=None, epic_key=None):
        if not isinstance(data, dict):
            return
        stats = {k: data[k] for k in ('metrics', 'statistics', 'progress', 'totals', 'counts', 'percent',
                 'total_issues', 'verified_done', 'pending', 'in_progress', 'blocked', 'awaiting_verification',
                 'completion_percentage', 'scope_complete', 'enumeration_complete', 'estimate', 'machine_hours') if k in data}
        if stats:
            reported.append({'source': source, 'project': project_id, 'epic': epic_key,
                             'updated_at': data.get('generated_at', data.get('updated_at')), 'values': stats,
                             'teams': explicit_teams(data) or []})
        for field in ('per_project', 'project_metrics', 'projects'):
            children = data.get(field)
            if isinstance(children, dict):
                for pid, child in children.items():
                    project(pid)
                    reported_stats(child, source, pid, epic_key)
            elif isinstance(children, list):
                for child in children:
                    if isinstance(child, dict) and isinstance(child.get('project', child.get('id')), str):
                        pid = child.get('project', child.get('id'))
                        project(pid)
                        reported_stats(child, source, pid, epic_key)
        if project_id and (p := project(project_id)):
            p['reported_estimate'] = reported_estimate_hours(data) or p['reported_estimate']
            actual = data.get('actual_machine_hours')
            if number(actual):
                p['reported_actual_machine_hours'] = actual

    inventory = read_json(root, root / 'projects.json', warnings)
    for item in records(inventory, 'projects'):
        entry = project(item.get('id'), item.get('name'), True)
        if entry is not None and explicit_teams(item) is not None:
            entry['teams'] = explicit_teams(item)
    for path in sorted((root / 'projects').glob('*.json')):
        data = read_json(root, path, warnings)
        if isinstance(data, dict):
            pid = data.get('id', path.stem)
            if not isinstance(pid, str) or not pid:
                warnings.append(f'{path.name}: invalid project id')
                continue
            entry = project(pid, data.get('name'))
            if explicit_teams(data) is not None:
                entry['teams'] = explicit_teams(data)
            reported_stats(data, str(path.relative_to(root)), pid)
    for path in sorted((root / 'tasks').glob('*/*.json')):
        data = read_json(root, path, warnings)
        if not isinstance(data, dict):
            continue
        pid = data.get('project', path.parent.name)
        if not isinstance(pid, str) or not pid or not isinstance(data.get('id', path.stem), str):
            warnings.append(f'{path.name}: invalid task identity')
            continue
        project(pid)
        source = str(path.relative_to(root))
        link_fields = task_link_fields(data, source, warnings)
        tasks.append({'id': data.get('id', path.stem), 'project': pid, 'title': data.get('title', path.stem),
                      'status': link_fields['status'], 'issue_key': link_fields['issue_key'],
                      'epic_key': link_fields['epic_key'], 'issue_keys': sorted(set(strings(link_fields['issue_key']) + strings(data.get('issue_keys')))),
                      'blockers': strings(data.get('blockers')), 'source': source,
                      'depends_on': strings(data.get('depends_on')),
                      'evidenced': link_fields['status'] in CLOSED_TASK_STATUSES and evidence(data),
                      'evidence': evidence_refs(data),
                      'updated_at': data.get('updated_at'),
                      'owner': data.get('owner'),
                      'owner_session_id': data.get('owner_session_id'),
                      'delivery_stage': task_delivery_stage(root, data.get('id', path.stem), delivery_warnings),
                      'teams': record_teams(data, projects[pid]['teams']),
                      '_inherit_teams': explicit_teams(data) is None and title_teams(data) is None})
    withhold_duplicate_task_evidence(tasks, warnings)
    task_map = {t['id']: t for t in tasks}
    for folder in sorted((root / 'epics').glob('*')):
        if not folder.is_dir():
            continue
        scope = read_scope(root, folder, warnings)
        scope_is_object = scope is not None
        scope = scope or {}
        key = scope.get('epic_key', scope.get('key', folder.name))
        if not isinstance(key, str):
            warnings.append(f'{folder.name}: invalid epic key')
            key = folder.name
        dependencies = scope.get('external_dependencies', [])
        if isinstance(dependencies, list):
            for dependency in dependencies:
                if isinstance(dependency, str):
                    external.append({'key': dependency, 'epic': key})
                elif isinstance(dependency, dict) and isinstance(dependency.get('key'), str):
                    external.append({**dependency, 'epic': key})
        complete = scope_is_object and scope.get('enumeration_complete', scope.get('scope_complete')) is True
        declared = strings(scope.get('issue_keys', scope.get('in_scope_issue_keys', scope.get('issues'))))
        issue_path = folder / 'issues.json'
        items = read_json(root, issue_path, warnings) if issue_path.exists() else None
        shape = items.get('issues', items) if isinstance(items, dict) else items
        if not isinstance(shape, (list, dict)) or any(not isinstance(v, dict) for v in (shape.values() if isinstance(shape, dict) else shape or [])):
            warnings.append(f'{key}: missing, unreadable or malformed issue snapshot; scope incomplete')
            complete = False
        items = records(items, 'issues')
        valid_items = [i for i in items if isinstance(i.get('key', i.get('issue_key')), str)]
        if len(valid_items) != len(items):
            warnings.append(f'{key}: issue records with invalid keys ignored')
            complete = False
        items = valid_items
        actual_keys = [i.get('key', i.get('issue_key')) for i in items]
        missing = set(declared) - set(actual_keys)
        if missing:
            warnings.append(f'{key}: missing issue records: {", ".join(sorted(missing))}')
            complete = False
        epics.append({'key': key, 'title': scope.get('title', scope.get('summary', key)),
                      'issue_type': scope.get('source_issue_type', 'Epic'),
                      'source_status': scope.get('source_status', 'Unknown'),
                      'scope_complete': complete, 'issue_keys': sorted(set(actual_keys + declared) - {None}),
                      'updated_at': scope.get('latest_sync_at', scope.get('updated_at'))})
        for item in items:
            item = dict(item)
            item['_epic'] = key
            item['_source'] = str(issue_path.relative_to(root))
            raw_issues.append(item)
        for missing_key in missing:
            raw_issues.append({'key': missing_key, '_epic': key, '_missing': True})
        progress_path = folder / 'progress.json'
        if progress_path.exists():
            reported_stats(read_json(root, progress_path, warnings), str(progress_path.relative_to(root)), epic_key=key)
    # Resolve both directions and every duplicate snapshot before verification.
    links = {}
    for item in raw_issues:
        key = item.get('key', item.get('issue_key'))
        links.setdefault(key, set()).update(strings(item.get('task_ids')) + strings(item.get('local_task_ids')))
    for task in tasks:
        for key in task['issue_keys']:
            if key in links:
                links[key].add(task['id'])
    issues = {}
    worked = worked_so_far(root, delivery_warnings)
    for item in raw_issues:
        key = item.get('key', item.get('issue_key'))
        if not isinstance(key, str):
            warnings.append('Issue without a string key ignored')
            continue
        pids = [pid for pid in sorted(set(strings(item.get('projects', item.get('affected_projects'))))) if project(pid)]
        linked = sorted(links[key])
        status = item.get('local_status', 'pending')
        if status not in STATUSES:
            status = 'pending'
        verified = status == 'verified_done' and verification_passed(item) and all(t in task_map and task_map[t]['evidenced'] for t in linked)
        if status == 'verified_done' and not verified:
            status = 'awaiting_verification'
        source_status = item.get('source_status', item.get('jira_status', 'Unknown'))
        if isinstance(source_status, dict):
            source_status = source_status.get('name', 'Unknown')
        normalized = {'key': key, 'title': item.get('title', item.get('summary', key)), 'epic_keys': [item['_epic']],
                      'issue_type': item.get('issue_type'),
                      'projects': pids, 'local_status': status, 'recorded_local_status': item.get('local_status', 'pending'),
                      'recorded_verification_reason': item.get('verification', {}).get('reason', '') if isinstance(item.get('verification'), dict) else '',
                      'sources': strings(item.get('_source')), 'updated_at': item.get('updated_at'),
                      'verified_at': item.get('verification', {}).get('verified_at') if isinstance(item.get('verification'), dict) else None,
                      'evidence': evidence_refs(item),
                      'source_status': str(source_status), 'verified': verified,
                      'dependencies': strings(item.get('depends_on', item.get('dependencies'))), 'task_ids': linked,
                      'estimate': reported_estimate_hours(item),
                      'original_estimate_hours': original_estimate_hours(item),
                      'worked_hours': worked['hours'].get(key),
                      'actual_machine_hours': item.get('actual_machine_hours') if number(item.get('actual_machine_hours')) else None,
                      'missing_record': item.get('_missing', False), 'conflict': False,
                      'teams': record_teams(item, sorted({team for pid in pids for team in projects[pid]['teams']}))}
        if key not in issues:
            issues[key] = normalized
        else:
            previous = issues[key]
            for field in ('projects', 'epic_keys', 'dependencies', 'task_ids', 'teams', 'sources', 'evidence'):
                previous[field] = sorted(set(previous[field] + normalized[field]))
            if any(previous[k] != normalized[k] for k in ('local_status', 'verified', 'estimate', 'actual_machine_hours')):
                previous.update(verified=False, local_status='awaiting_verification', conflict=True, estimate=None, actual_machine_hours=None)
                warnings.append(f'{key}: conflicting snapshots; verification and estimates require reconciliation')
    issue_list = list(issues.values())
    epic_keys = {e['key'] for e in epics}
    for issue in issue_list:
        linked = [task_map[t] for t in issue['task_ids'] if t in task_map]
        missing = [t for t in issue['task_ids'] if t not in task_map]
        unevidenced = [t['id'] for t in linked if not t['evidenced']]
        counts = dict(Counter(t['status'] for t in linked))
        issue['task_execution'] = {'total': len(issue['task_ids']), 'counts': counts,
                                   'missing': missing, 'without_completion_evidence': unevidenced}
        issue['execution_status'] = ('blocked' if counts.get('blocked') else
                                     'in_progress' if counts.get('in_progress') else
                                     'awaiting_verification' if linked and all(t['status'] in CLOSED_TASK_STATUSES for t in linked) and not missing else
                                     'pending')
        if issue['conflict']:
            reason = 'Conflicting issue snapshots require reconciliation.'
        elif issue['verified']:
            reason = 'Passing issue verification and required task evidence recorded.'
        elif missing or unevidenced:
            reason = 'Required tasks missing or without completion evidence: ' + ', '.join(missing + unevidenced)
        elif linked:
            reason = 'Task execution recorded; issue acceptance verification is not yet passed.'
        else:
            reason = 'No issue-level tasks linked; acceptance verification is not yet passed.'
        issue['verification_reason'] = reason
        if missing:
            warnings.append(f"{issue['key']}: referenced tasks missing: {', '.join(missing)}")
    for task in tasks:
        # An issue-side link is equally authoritative, including forward-only links.
        task['declared_issue_keys'] = task['issue_keys']
        task['unresolved_issue_keys'] = [key for key in task['declared_issue_keys'] if key not in issues and key not in epic_keys]
        task['issue_keys'] = sorted({i['key'] for i in issue_list if task['id'] in i['task_ids']})
        names_epic = any(isinstance(name, str) and name in epic_keys for name in (task['issue_key'], task['epic_key']))
        task['mapping_kind'] = 'issue' if task['issue_keys'] else 'epic' if names_epic else 'unmapped'
        if task['mapping_kind'] == 'epic' and not task['epic_key']:
            task['epic_key'] = task['issue_key']
        if task['unresolved_issue_keys']:
            task['mapping_kind'] = 'unresolved'
            warnings.append(f"{task['id']}: unresolved issue mapping {', '.join(task['unresolved_issue_keys'])}")
        if not task['epic_key'] and task['issue_keys']:
            memberships = sorted({e for key in task['issue_keys'] for e in issues[key]['epic_keys']})
            if len(memberships) == 1:
                task['epic_key'] = memberships[0]
        task['epic_keys'] = sorted(set(strings(task['epic_key'])) | {e for key in task['issue_keys'] for e in issues[key]['epic_keys']})
        inherit = task.pop('_inherit_teams')
        if inherit and task['issue_keys']:
            task['teams'] = sorted({team for key in task['issue_keys'] for team in issues[key]['teams']})
    complete = bool(epics) and all(e['scope_complete'] for e in epics) and not any(i['missing_record'] for i in issue_list)
    for e in epics:
        selected = [i for i in issue_list if e['key'] in i['epic_keys']]
        e['progress'] = metrics(selected, e['scope_complete'], model)
    for p in projects.values():
        selected = [i for i in issue_list if p['id'] in i['projects']]
        p['issue_keys'] = [i['key'] for i in selected]
        p['task_ids'] = [t['id'] for t in tasks if t['project'] == p['id']]
        p['progress'] = metrics(selected, complete, model)
        # Project allocation prevents double-counting multi-project work.
        allocated = {k: 0.0 for k in ('low', 'likely', 'high')}
        for i in selected:
            estimate = i['estimate'] or {k: model[k] * model['review_multiplier'] for k in allocated}
            for k in allocated:
                allocated[k] += estimate[k] / len(i['projects'])
        p['allocated_estimate'] = rounded_range(allocated) if selected else None
    dependencies = [{'from': dep, 'to': i['key'], 'kind': 'depends_on', 'unresolved': dep not in issues}
                    for i in issue_list for dep in i['dependencies']]
    graph = build_graph(root, list(projects.values()), epics, issue_list, tasks, raw_issues, external, read_json, warnings)
    project_delivery(issue_list, tasks, raw_issues, graph, delivery_warnings)
    totals = metrics(issue_list, complete, model)
    if has_overflowed_totals(totals, epics, projects.values()):
        delivery_warnings.append(OVERFLOW_WARNING)
    return {'generated_at': datetime.now(timezone.utc).isoformat(), 'mode': mode, 'warnings': warnings,
            'delivery_warnings': delivery_warnings,
            'projects': sorted(projects.values(), key=lambda p: p['name']), 'epics': epics, 'issues': issue_list,
            'tasks': tasks, 'dependencies': dependencies, 'external_dependencies': external,
            'graph': graph,
            'task_dependencies': [{'from': dep, 'to': t['id'], 'kind': 'task_dependency', 'unresolved': dep not in task_map} for t in tasks for dep in t['depends_on']],
            'reported_statistics': reported, 'worked_measured_at': worked['measured_at'],
            'totals': totals, 'estimate_method': model}


MODEL_RANGE_MESSAGE = 'Model requires 0 ≤ low ≤ likely ≤ high ≤ 1000 and 1 ≤ review multiplier ≤ 1000'
MODEL_DEFAULTS = {'low': .5, 'likely': 1.5, 'high': 4, 'review_multiplier': 1.25}


class InvalidRequest(Exception):
    """A query the client got wrong; the message is a fixed text that is safe to return."""


def parse_model(query):
    try:
        model = {k: float(query.get(k, [default])[0]) for k, default in MODEL_DEFAULTS.items()}
    except ValueError:
        raise InvalidRequest(MODEL_RANGE_MESSAGE) from None
    if not all(number(v) and v <= 1000 for v in model.values()) or not model['low'] <= model['likely'] <= model['high'] or model['review_multiplier'] < 1:
        raise InvalidRequest(MODEL_RANGE_MESSAGE)
    return model


def parse_mode(query):
    mode = query.get('mode', ['live'])[0]
    if mode not in ('live', 'demo'):
        raise InvalidRequest('Unknown mode')
    return mode


NODE_PARAMETER = re.compile(r'(?:task|issue):[A-Za-z0-9][A-Za-z0-9._-]{0,127}')
NODE_MESSAGE = 'Query parameter node must be task:<id> or issue:<key>'


def parse_node(query):
    values = query.get('node', [])
    if len(values) != 1 or not NODE_PARAMETER.fullmatch(values[0]):
        raise InvalidRequest(NODE_MESSAGE)
    return values[0]


def context_for(root, node_id):
    """The context pack of a dashboard graph node, or None when the graph has no such node."""
    from context_graph import build_context_pack  # lazy: dispatch_gate imports server and must not load it
    return build_context_pack(build_dashboard(root), root, read_json, node_id)


def factory_for(root, mode):
    """The factory animation projection of the mailbox at root."""
    from factory_floor import build_factory  # lazy: dispatch_gate imports server and must not load it
    return build_factory(build_dashboard(root, mode=mode), root, read_json, mode)


def mode_root(mode):
    return ROOT / 'demo' if mode == 'demo' else Path(os.environ.get('MAILBOX_ROOT', '/data'))


def error_body(status):
    if status == 501:
        code, message = 'unsupported_method', 'Unsupported method'
    elif 400 <= status < 500:
        code, message = 'bad_request', 'Bad request'
    else:
        code, message = 'internal_error', 'Request failed'
    return {'error': {'code': code, 'message': message}}


class Handler(BaseHTTPRequestHandler):
    def reply(self, status, data, content_type='application/json; charset=utf-8'):
        payload = json.dumps(data, allow_nan=False).encode() if isinstance(data, (dict, list)) else data
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(payload)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; object-src 'none'; frame-ancestors 'none'")
        if self.close_connection:
            self.send_header('Connection', 'close')
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(payload)

    def send_error(self, code, message=None, explain=None):
        self.log_error('code %d, message %s', code, message)
        self.close_connection = True
        self.reply(code, error_body(int(code)))

    def dashboard_payload(self, query):
        model, mode = parse_model(query), parse_mode(query)
        return json.dumps(build_dashboard(mode_root(mode), model, mode), allow_nan=False).encode()

    def context_payload(self, query):
        """The serialized context pack, or None when the node is unknown."""
        mode, node_id = parse_mode(query), parse_node(query)
        pack = context_for(mode_root(mode), node_id)
        return None if pack is None else json.dumps({**pack, 'mode': mode}, allow_nan=False).encode()

    def factory_payload(self, query):
        mode = parse_mode(query)
        return json.dumps(factory_for(mode_root(mode), mode), allow_nan=False).encode()

    def reply_dashboard(self, query):
        try:
            payload = self.dashboard_payload(query)
        except InvalidRequest as invalid:
            return self.reply(400, {'error': {'code': 'invalid_request', 'message': str(invalid)}})
        except Exception as failure:
            self.log_error('Dashboard projection failed: %r', failure)
            return self.reply(500, {'error': {'code': 'internal_error', 'message': 'Dashboard data could not be read'}})
        return self.reply(200, payload)

    def reply_context(self, query):
        try:
            payload = self.context_payload(query)
        except InvalidRequest as invalid:
            return self.reply(400, {'error': {'code': 'invalid_request', 'message': str(invalid)}})
        except Exception as failure:
            self.log_error('Context projection failed: %r', failure)
            return self.reply(500, {'error': {'code': 'internal_error', 'message': 'Context data could not be read'}})
        if payload is None:
            return self.reply(404, {'error': {'code': 'not_found', 'message': 'Context node not found'}})
        return self.reply(200, payload)

    def reply_factory(self, query):
        try:
            payload = self.factory_payload(query)
        except InvalidRequest as invalid:
            return self.reply(400, {'error': {'code': 'invalid_request', 'message': str(invalid)}})
        except Exception as failure:
            self.log_error('Factory projection failed: %r', failure)
            return self.reply(500, {'error': {'code': 'internal_error', 'message': 'Factory data could not be read'}})
        return self.reply(200, payload)

    def do_GET(self):
        parsed = urlsplit(self.path)
        if parsed.path == '/healthz':
            return self.reply(200, {'status': 'ok'})
        if parsed.path == '/api/dashboard':
            return self.reply_dashboard(parse_qs(parsed.query))
        if parsed.path == '/api/context':
            return self.reply_context(parse_qs(parsed.query))
        if parsed.path == '/api/factory':
            return self.reply_factory(parse_qs(parsed.query))
        files = {'/': ('index.html', 'text/html; charset=utf-8'), '/app.js': ('app.js', 'text/javascript; charset=utf-8'), '/theme.js': ('theme.js', 'text/javascript; charset=utf-8'), '/style.css': ('style.css', 'text/css; charset=utf-8'), '/factory.js': ('factory.js', 'text/javascript; charset=utf-8'), **VENDORED_SCRIPTS}
        if parsed.path in files:
            filename, mime = files[parsed.path]
            return self.reply(200, (ROOT / 'static' / filename).read_bytes(), mime)
        return self.reply(404, {'error': {'code': 'not_found', 'message': 'Not found'}})

    def reject_mutation(self):
        self.reply(405, {'error': {'code': 'read_only', 'message': 'This dashboard is read-only'}})
    do_POST = do_PUT = do_PATCH = do_DELETE = reject_mutation


def main():
    ThreadingHTTPServer(('0.0.0.0', int(os.environ.get('PORT', '8080'))), Handler).serve_forever()


if __name__ == '__main__':
    main()
