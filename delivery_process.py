"""Evidence-oriented process display; does not grant eligibility or change verification."""
from html.parser import HTMLParser
from datetime import datetime
import json

STAGES = {'uninvestigated', 'investigating', 'ready', 'implementation', 'review', 'verification', 'complete', 'deferred', 'unknown'}
TRACE_STEPS = ('written', 'reviewed', 'integrated', 'tested_integrated')
CLOSED_TASK_STATUSES = ('done', 'cancelled')


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.hidden += 1
        if tag in ('br', 'p', 'div', 'li', 'ul', 'ol', 'h1', 'h2', 'h3', 'tr'):
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            self.hidden = max(0, self.hidden - 1)
        if tag in ('p', 'div', 'li', 'ul', 'ol', 'h1', 'h2', 'h3', 'tr'):
            self.parts.append('\n')

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def plain(value):
    if isinstance(value, dict):
        # ADF text nodes contain literal text, not HTML. The client uses textContent.
        if value.get('type') == 'text':
            return value.get('text') if isinstance(value.get('text'), str) else ''
        content = value.get('content')
        return '\n'.join(filter(None, (plain(v) for v in content))) if isinstance(content, list) else ''
    if isinstance(value, list):
        return '\n'.join(filter(None, (plain(v) for v in value)))
    if not isinstance(value, str):
        return ''
    parser = _Text()
    parser.feed(value)
    parser.close()
    return '\n'.join(line.strip() for line in ''.join(parser.parts).splitlines() if line.strip())


def _refs(value):
    pending, result = [value], set()
    while pending:
        item = pending.pop()
        if isinstance(item, str) and item.strip():
            result.add(item.strip())
        elif isinstance(item, dict):
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    return sorted(result)


def _person(value):
    if isinstance(value, dict):
        value = next((value[k] for k in ('display_name', 'displayName', 'name', 'session_id', 'agent_id')
                      if isinstance(value.get(k), str) and value[k].strip()), None)
    return value.strip() if isinstance(value, str) and value.strip() else None


def _string(value):
    return value if isinstance(value, str) and value.strip() else None


def _field(raw, name):
    explicit = raw.get('delivery')
    if isinstance(explicit, dict) and name in explicit:
        return explicit[name]
    if name == 'stage':
        return raw.get('delivery_stage', raw.get('process_stage'))
    return raw.get(name)


def _values(records, name):
    return [value for raw in records if (value := _field(raw, name)) is not None]


def _first(records, name, default=None):
    return next(iter(_values(records, name)), default)


def _role(members, missing, reason, conflict=False):
    sources = list(dict.fromkeys(m['source'] for m in members if m['source']))
    names = list({m['identity']: m['name'] for m in members}.values())
    return dict(name=None if conflict or not names else ', '.join(names),
                status='conflict' if conflict else 'assigned' if members else missing,
                source=', '.join(sources) or None, members=members, reason=reason)


def _ownership(records, tasks):
    """Keep independent recorded roles; none establish availability or eligibility."""
    jira_members, states, malformed, retrieved, sources = [], set(), False, [], []
    implementation_members, implementation_bad = [], False
    for raw in records:
        source = _string(raw.get('_source'))
        if 'jira_assignee' in raw:
            jira_source = _string(raw.get('jira_assignee_source')) or source
            if jira_source:
                sources.append(jira_source)
            at = _string(raw.get('jira_assignee_retrieved_at'))
            if at:
                retrieved.append(at)
            value = raw['jira_assignee']
            if value is None:
                states.add(None)
            elif isinstance(value, dict):
                account = _string(value.get('accountId'))
                label = _string(value.get('displayName'))
                invalid = any(key in value and value[key] is not None and not _string(value[key])
                              for key in ('accountId', 'displayName'))
                if invalid or not (account or label):
                    malformed = True
                else:
                    identity = (account or label).strip()
                    states.add(identity)
                    jira_members.append(dict(name=(label or account).strip(), identity=identity, source=jira_source))
            else:
                malformed = True
        value = _field(raw, 'implementation_owner')
        if value is not None:
            if _string(value):
                implementation_members.append(dict(name=value.strip(), identity=value.strip(), source=source))
            else:
                implementation_bad = True
    jira_conflict = malformed or len(states) > 1
    jira = _role(jira_members, 'unassigned' if states == {None} else 'not_loaded',
                 'Malformed or contradictory Jira assignee snapshots.' if jira_conflict else
                 'Explicit Jira assignee snapshot; retrieval time is not a full issue update time.' if states else
                 'Jira assignee was not loaded for this issue.', jira_conflict)
    jira['source'] = ', '.join(dict.fromkeys(sources)) or None
    jira['retrieved_at'] = max(retrieved) if retrieved else None
    # Keep every observation inspectable, including differing labels for one stable account.
    jira['observations'] = [dict(source=_string(raw.get('jira_assignee_source')) or _string(raw.get('_source')),
                                 retrieved_at=_string(raw.get('jira_assignee_retrieved_at')))
                            for raw in records if 'jira_assignee' in raw]
    implementation_conflict = implementation_bad or len({m['identity'] for m in implementation_members}) > 1
    implementation = _role(implementation_members, 'not_recorded',
                           'Malformed or contradictory explicit implementation owner/project records.' if implementation_conflict else
                           'Explicit implementation owner/project records only.' if implementation_members else
                           'No implementation owner/project is explicitly recorded.', implementation_conflict)
    execution_members, execution_bad, claims = [], False, {}
    for task in tasks:
        owner, session = task.get('owner'), task.get('owner_session_id')
        session_id = _string(session)
        label = _person(owner)
        owner_id = (_string(owner.get('session_id')) or _string(owner.get('agent_id')) or label
                    if isinstance(owner, dict) else label)
        if (owner is not None and label is None) or (session is not None and session_id is None):
            execution_bad = True
        if owner_id and session_id and owner_id != session_id:
            execution_bad = True
        identity = session_id or owner_id
        if identity:
            identity = identity.strip()
            claims.setdefault(task['id'], set()).add(identity)
            execution_members.append(dict(name=label or identity, identity=identity,
                source=_string(task.get('source')), task_id=task['id'], project=task.get('project'),
                task_status=task.get('status')))
    execution_bad = execution_bad or any(len(values) > 1 for values in claims.values())
    execution = _role(execution_members, 'not_recorded',
                      'Contradictory or malformed ownership on a linked task.' if execution_bad else
                      'Recorded linked-task assignments; these snapshots do not establish current worker availability.',
                      execution_bad)
    return dict(jira=jira, implementation=implementation, execution=execution)


def _conflicts(records):
    fields = ('stage', 'baseline_status', 'baseline_assessment', 'existing_work', 'remainder',
              'owner', 'criteria', 'verification', 'trace', 'investigation_status')
    return [field for field in fields if len({json.dumps(value, sort_keys=True) for value in _values(records, field)}) > 1]


def _criteria(value):
    result = []
    for criterion in value if isinstance(value, list) else []:
        if not isinstance(criterion, dict) or not plain(criterion.get('text')):
            continue
        evidence = _refs(criterion.get('evidence'))
        status = criterion.get('status')
        status = ('passed' if evidence else 'unknown') if status in ('passed', 'pass', 'verified') else status if status in ('failed', 'pending', 'unknown') else 'unknown'
        result.append(dict(text=plain(criterion['text']), status=status, evidence=evidence))
    return result


def _trace(value):
    value = value if isinstance(value, dict) else {}
    result = []
    for step in TRACE_STEPS:
        raw = value.get(step)
        raw = raw if isinstance(raw, dict) else {}
        refs, revision = _refs(raw.get('evidence')), _string(raw.get('revision'))
        accepted = bool(refs and revision)
        reason = 'Explicit revision and evidence recorded.' if accepted else 'Revision and step-specific evidence are not both recorded.'
        if step == 'tested_integrated':
            integrated = next(item for item in result if item['step'] == 'integrated')
            if integrated['status'] != 'evidenced' or not revision or revision != integrated['revision']:
                accepted = False
                reason = 'Tested revision does not match an explicitly evidenced integrated revision.'
        result.append(dict(step=step, status='evidenced' if accepted else 'unknown', revision=revision,
                           evidence=refs, at=_string(raw.get('at')), reason=reason))
    return result


def _dependency_blockers(graph, targets):
    nodes = {node['id']: node for node in graph['nodes']}
    parents, required = {}, {}
    for edge in graph['edges']:
        (parents if edge['kind'] == 'membership' else required).setdefault(edge['to'], set()).add(edge['from'])
    blockers, seen, active = set(), set(), set()

    def owners(key):
        result, pending = set(), [key]
        while pending:
            item = pending.pop()
            if item not in result:
                result.add(item)
                pending.extend(parents.get(item, ()))
        return result

    def visit(key):
        if key in active:
            blockers.add('Dependency cycle: ' + key)
            return
        if key in seen:
            return
        seen.add(key)
        active.add(key)
        for owner in owners(key):
            for prerequisite in required.get(owner, ()):
                node = nodes.get(prerequisite, {})
                if node.get('ready') is not True or node.get('unresolved'):
                    blockers.add('Unmet prerequisite: ' + prerequisite)
                visit(prerequisite)
        active.remove(key)
    for target in targets:
        visit(target)
    return sorted(blockers)


def _stage(issue, records, tasks, conflict):
    if issue['verified']:
        return 'complete', 'Normalized local acceptance verification and required task evidence passed.'
    if issue['local_status'] == 'deferred':
        return 'deferred', 'Deferred by a recorded decision; the issue stays in scope but is not scheduled.'
    if conflict or issue['conflict'] or issue['missing_record']:
        return 'unknown', 'Missing or conflicting process evidence requires reconciliation.'
    explicit = _first(records, 'stage')
    if explicit is not None:
        if not isinstance(explicit, str) or explicit not in STAGES:
            return 'unknown', 'Recorded stage is unsupported or malformed.'
        if explicit == 'complete':
            return 'verification', 'Completion was recorded, but normalized acceptance verification has not passed.'
        return explicit, 'Explicit recorded process stage; dependency eligibility and loop gates remain separate.'
    if any(task['status'] == 'in_progress' for task in tasks):
        return 'implementation', 'Linked local task snapshot records execution in progress; current worker activity and issue completion are not established.'
    if tasks and all(task['evidenced'] and task['evidence'] for task in tasks):
        return 'verification', 'Linked local tasks have completion evidence; issue acceptance verification is still required.'
    baseline = _first(records, 'baseline_status')
    if baseline == 'partial':
        return 'investigating', 'Initial assessment exists; investigation incomplete.'
    if baseline == 'complete':
        return 'verification', 'Complete baseline assessment is a verification candidate, not delivered implementation.'
    if _first(records, 'investigation_status') == 'not_started':
        return 'uninvestigated', 'Investigation is explicitly recorded as not started.'
    return 'unknown', 'No reliable current process stage is recorded.'


def _freshness(records, tasks):
    source = _first(records, 'freshness', {})
    source = source if isinstance(source, dict) else {}
    worker = _string(source.get('worker')) or _string(_first(records, 'worker_updated_at'))
    basis = 'explicit_report_time' if worker else 'unknown'
    if not worker:
        times = sorted({t['updated_at'] for t in tasks if _string(t.get('updated_at'))})
        if times:
            parsed, invalid = [], []
            for value in times:
                try:
                    instant = datetime.fromisoformat(value.replace('Z', '+00:00'))
                    if instant.tzinfo is None:
                        raise ValueError('Timestamp has no explicit timezone')
                    parsed.append((instant, value))
                except ValueError:
                    invalid.append(value)
            # Invalid candidates prevent a trustworthy latest-report assertion;
            # retain the raw value so the client can explain the invalid source.
            worker = invalid[0] if invalid else max(parsed, key=lambda item: item[0])[1]
            basis = 'linked_task_updated_at_proxy'
    return dict(jira=_string(source.get('jira')) or _string(_first(records, 'source_updated_at')),
                worker=worker, evidence=_string(source.get('evidence')) or _string(_first(records, 'evidence_updated_at')),
                worker_basis=basis)


def project_delivery(issues, tasks, raw_issues, graph, warnings):
    """Attach display-only delivery metadata without modifying graph or completion."""
    by_key = {}
    task_map = {task['id']: task for task in tasks}
    for raw in raw_issues:
        by_key.setdefault(raw.get('key', raw.get('issue_key')), []).append(raw)
    for issue in issues:
        records = by_key.get(issue['key'], [])
        linked = [task_map[key] for key in issue['task_ids'] if key in task_map]
        conflicts = _conflicts(records)
        if conflicts:
            warnings.append(issue['key'] + ': conflicting delivery metadata: ' + ', '.join(conflicts))
        blockers = set()
        for raw in records:
            raw_blockers = raw.get('blockers')
            raw_blockers = raw_blockers if isinstance(raw_blockers, list) else []
            blockers.update(plain(v) for v in raw_blockers if isinstance(v, str))
            if raw.get('local_status') == 'blocked':
                blockers.add('Recorded issue status is blocked.')
        for task in linked:
            blockers.update(plain(value) for value in task['blockers'])
            if task['status'] == 'blocked':
                blockers.add('Local task ' + task['id'] + ' is blocked.')
        if not issue['verified']:  # Prerequisites gate execution; a verified issue has nothing left to execute.
            blockers.update(_dependency_blockers(graph, ['issue:' + issue['key']] + ['task:' + task['id'] for task in linked]))
        # A task shared with a dependent issue must not list this issue as its own prerequisite.
        blockers.discard('Unmet prerequisite: issue:' + issue['key'])
        blockers.discard('')
        stage, reason = _stage(issue, records, linked, conflicts)
        raw_verification = _first(records, 'verification', {})
        verification = raw_verification if isinstance(raw_verification, dict) else {'status': raw_verification}
        verification_evidence = _refs(verification.get('evidence')) or issue['evidence']
        recorded_status = verification.get('status', verification.get('outcome'))
        verification_status = ('passed' if issue['verified'] else 'failed' if recorded_status in ('failed', 'fail') else
                               'pending' if recorded_status in ('passed', 'pass', 'verified', 'pending', 'not_verified', 'awaiting_verification') else 'unknown')
        owner = None if conflicts else _person(_first(records, 'owner'))
        if owner is None and not conflicts:
            owners = {_person(task.get('owner')) or _person(task.get('owner_session_id')) for task in linked} - {None}
            owner = next(iter(owners)) if len(owners) == 1 else None
        existing = plain(_first(records, 'existing_work')) or plain(_first(records, 'baseline_assessment')) or 'Existing work has not been assessed in the supplied snapshot.'
        remainder = plain(_first(records, 'remainder')) or plain(verification.get('reason')) or 'Remaining work and criterion coverage are not explicitly recorded.'
        suggested = plain(_first(records, 'next_action'))
        if blockers:
            next_action = 'Resolve recorded blockers before execution.' + (' Recorded suggestion: ' + suggested if suggested else '')
        elif suggested:
            next_action = suggested
        else:
            next_action = {
                'uninvestigated': 'Investigate existing work and record a scoped implementation assessment.',
                'investigating': 'Complete the initial investigation and record remaining requirements.',
                'ready': 'Validate dependency eligibility and the applicable project-loop gates before execution.',
                'implementation': 'Continue only through an authorized fresh dispatch and the project loop.',
                'review': 'Complete independent review and record its findings and evidence.',
                'verification': 'Verify acceptance criteria against the exact integrated revision and record evidence.',
                'complete': 'No further delivery action is recorded after local acceptance verification.',
                'deferred': 'No delivery action until the recorded deferral is lifted.',
                'unknown': 'Record or reconcile investigation, process stage and remaining work before planning execution.'}[stage]
        criteria = [] if conflicts else _criteria(_first(records, 'criteria'))
        trace = _trace(None if conflicts else _first(records, 'trace'))
        acceptance = list(dict.fromkeys(plain(raw.get('acceptance_criteria')) for raw in records if plain(raw.get('acceptance_criteria'))))
        issue['delivery'] = dict(stage=stage, stage_reason=reason, blocked=bool(blockers), blockers=sorted(blockers),
            existing_work=existing, remainder=remainder, owner=owner, next_action=next_action,
            ownership=_ownership(records, [task for task in tasks if task['id'] in issue['task_ids']]),
            acceptance_criteria='\n\n'.join(acceptance), criteria=criteria,
            verification=dict(status='unknown' if conflicts else verification_status,
                              verifier=None if conflicts else _person(verification.get('verifier')),
                              evidence=verification_evidence, reason=plain(verification.get('reason')) or issue['verification_reason']),
            trace=trace, freshness=_freshness(records, linked), source=', '.join(issue['sources']))
