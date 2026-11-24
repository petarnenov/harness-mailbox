#!/usr/bin/env python3
"""Mirror acceptance-criterion tests of one Jira issue into AIO Tests (orchestrator only).

Usage:
  aio_sync.py cases --manifest <file> [--apply]
  aio_sync.py cycle --manifest <file> --sprint <N> [--apply]

`cases` runs when the issue's PR opens. It creates or updates one AIO Case per acceptance criterion in
`<epic_folder>/<story_folder>`, linked to the issue. `cycle` runs at post-merge verification. It creates
the Cycle `AI <year> Sprint <N> <issue>`, sets each Case's run from the JUnit results of the merged
revision, and attaches the JUnit, reported-evidence and coverage files as one ZIP. Without --apply nothing is written; the planned
writes are printed instead. Each recorded run (with its revision), the cycle key and title, and each attachment
are saved in aio/ledger/<issue>-sprint-<N>.json, so a rerun after a failure (AIO answers 429 when rate limited)
resumes without duplicating any of them, even across a change of calendar year.

The manifest comes from the worker's G2 / post-merge report:
  {"issue": "GEO-1", "epic_folder": "...", "story_folder": "...", "revision": "<sha>",
   "criteria": [{"id": "1-AC1", "title": "...", "type": "Security", "precondition": "...",
                 "steps": [{"step": "...", "expected": "..."}], "tests": ["Class#method"]}],
   "junit": ["<path to junit xml>"], "coverage": {"threshold": 85, "summary_file": "<path>"}}

Tests without JUnit output (SQL check blocks, manual owner evidence) go in "reported_results", taken from the
worker's verified post-merge report: {"<test id>": "passed" | "failed"}, with their report files in
"reported_evidence": ["<path>"]. If a test appears in both JUnit and the report, any failure wins.

Every JUnit, reported-evidence and coverage path must be a regular file, not a symlink, whose name does not
start with `.env`, inside the mailbox root, a registered project's repository or a linked Git worktree of it.

Policy: AGENTS.md Jira policy (AIO exception, 2026-11-21). Team is left empty until AIO offers an AI
option (decision ac36a314). The token is read from ~/harness-mailbox/.env.local and never printed.
"""
import argparse
import json
import re
import ssl
import tempfile
import time
import sys
import urllib.error
import urllib.request
import uuid
import xml.etree.ElementTree as ElementTree
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from mailbox_layout import mailbox_root

MAILBOX = mailbox_root(__file__)
API = 'https://tcms-prod-us.aiojiraapps.com/aio-tcms/api/v1/project/GEO'
CASE_TYPES = {'Functional': 3, 'API': 4, 'Performance': 5, 'Security': 6, 'UI': 12}
PUBLISHED, AUTOMATED = 3, 4
CLASSIC_SCRIPT = 1
PASSED, FAILED = 'Passed', 'Failed'
CA_BUNDLE = '/etc/ssl/cert.pem'
RATE_LIMIT_RETRIES = 6
ISSUE_PATTERN = re.compile(r'[A-Z][A-Z0-9_]*-[0-9]+')
SPRINT_PATTERN = re.compile(r'[0-9]+')


class AioClient:
    """Thin authenticated JSON/multipart client; the only place that knows the token."""

    def __init__(self, token):
        self._headers = {'Authorization': 'AioAuth ' + token, 'Accept': 'application/json'}
        self._ssl = ssl.create_default_context(cafile=CA_BUNDLE if Path(CA_BUNDLE).exists() else None)

    def _send(self, request, label, timeout):
        """Send with backoff on 429 (AIO rate limit); any other failure is raised at once as a fixed message."""
        for attempt in range(RATE_LIMIT_RETRIES + 1):
            try:
                with urllib.request.urlopen(request, context=self._ssl, timeout=timeout) as response:
                    return response.status, response.read()
            except urllib.error.HTTPError as error:
                if error.code != 429 or attempt == RATE_LIMIT_RETRIES:
                    raise RuntimeError(f'AIO {label} -> {error.code}') from None
                retry_after = error.headers.get('Retry-After', '')
                time.sleep(int(retry_after) if retry_after.isdigit() else min(60, 5 * 2 ** attempt))
            except OSError:
                raise RuntimeError(f'AIO {label} could not be reached') from None

    def call(self, method, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        headers = dict(self._headers, **({'Content-Type': 'application/json'} if data else {}))
        request = urllib.request.Request(API + path, data=data, headers=headers, method=method)
        label = f'{method} {path}'
        _, content = self._send(request, label, 60)
        try:
            return json.loads(content.decode()) if content else None
        except ValueError:
            raise RuntimeError(f'AIO {label} returned an unreadable response') from None

    def upload(self, path, file_path, fields=None):
        boundary = uuid.uuid4().hex
        parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
                 for k, v in (fields or {}).items()]
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{Path(file_path).name}"'
                     f'\r\nContent-Type: application/octet-stream\r\n\r\n'.encode() + Path(file_path).read_bytes()
                     + f'\r\n--{boundary}--\r\n'.encode())
        request = urllib.request.Request(API + path, data=b''.join(parts), method='POST',
                                         headers=dict(self._headers, **{'Content-Type': f'multipart/form-data; boundary={boundary}'}))
        status, _ = self._send(request, f'upload {path}', 120)
        return status


def read_token(root):
    try:
        lines = (root / '.env.local').read_text().splitlines()
    except (OSError, ValueError):
        raise ValueError('The token file .env.local could not be read') from None
    for line in lines:
        if line.startswith('AIO_API_TOKEN='):
            return line.split('=', 1)[1].strip()
    raise ValueError('AIO_API_TOKEN is missing from .env.local')


def load_manifest(path):
    try:
        manifest = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        raise ValueError('The manifest could not be read as JSON') from None
    if not isinstance(manifest, dict):
        raise ValueError('The manifest must be a JSON object')
    for field in ('issue', 'epic_folder', 'story_folder', 'criteria'):
        if not manifest.get(field):
            raise ValueError(f'Manifest field {field!r} is required')
    if not isinstance(manifest['issue'], str) or not ISSUE_PATTERN.fullmatch(manifest['issue']):
        raise ValueError('Manifest issue must look like GEO-1')
    if not isinstance(manifest.get('revision', ''), str):
        raise ValueError('Manifest revision must be a string')
    if not isinstance(manifest['criteria'], list) or not all(isinstance(c, dict) for c in manifest['criteria']):
        raise ValueError('Manifest criteria must be a list of objects')
    for criterion in manifest['criteria']:
        if not criterion.get('id') or not criterion.get('title') or not criterion.get('tests'):
            raise ValueError('Criterion needs id, title and tests')
        if criterion.get('type', 'Functional') not in CASE_TYPES:
            raise ValueError(f'Unknown case type {criterion.get("type")!r}')
    return manifest


def require_sprint(sprint):
    if not SPRINT_PATTERN.fullmatch(sprint):
        raise ValueError('Sprint must be a whole number')


def project_repositories(root):
    """Resolved `repository_path` of every registered project; none when the mailbox has no registry."""
    try:
        registry = json.loads((root / 'projects.json').read_text())
    except FileNotFoundError:
        return []
    except (OSError, ValueError):
        raise ValueError('projects.json: malformed registry') from None
    entries = registry.get('projects') if isinstance(registry, dict) else registry
    if not isinstance(entries, list):
        raise ValueError('projects.json: malformed registry')
    return [Path(entry['repository_path']).resolve() for entry in entries
            if isinstance(entry, dict) and isinstance(entry.get('repository_path'), str)]


def read_text_or_none(path):
    try:
        return path.read_text() if path.is_file() else None
    except (OSError, ValueError):
        return None


def worktree_admin_directory(directory):
    """The `gitdir:` target of `directory/.git`, or None when that is not a readable pointer file."""
    content = read_text_or_none(directory / '.git')
    if content is None or not content.startswith('gitdir:'):
        return None
    return (directory / content[len('gitdir:'):].strip()).resolve()


def is_linked_worktree_of(directory, repositories):
    """True when `directory/.git` names an existing `<repository>/.git/worktrees/<name>` that points back at it."""
    admin = worktree_admin_directory(directory)
    if admin is None or not admin.is_dir():
        return False
    if not any(admin.parent == repository / '.git' / 'worktrees' for repository in repositories):
        return False
    back_pointer = read_text_or_none(admin / 'gitdir')
    return back_pointer is not None and (admin / back_pointer.strip()).resolve() == directory / '.git'


def inside_evidence_location(resolved, root, repositories):
    if any(resolved.is_relative_to(base) for base in (root, *repositories)):
        return True
    return any(is_linked_worktree_of(directory, repositories) for directory in resolved.parents)


def is_env_name(name):
    return name.casefold().startswith('.env')


def checked_evidence_path(value, root, repositories):
    if not isinstance(value, str) or not value:
        raise ValueError('Evidence paths must be non-empty strings')
    path = Path(value)
    resolved = path.resolve()
    allowed = (not is_env_name(path.name) and not is_env_name(resolved.name) and not path.is_symlink()
               and path.is_file() and inside_evidence_location(resolved, root, repositories))
    if not allowed:
        raise ValueError(f'Evidence file {path.name} is not an allowed regular file')
    return str(resolved)


def checked_evidence_paths(values, root, repositories):
    if values is None:
        return []
    if not isinstance(values, list):
        raise ValueError('Evidence paths must be a list')
    return [checked_evidence_path(value, root, repositories) for value in values]


def with_checked_evidence(manifest, root):
    """Copy of the manifest whose JUnit, reported-evidence and coverage paths passed the upload rules."""
    repositories = project_repositories(root)
    root = root.resolve()
    checked = dict(manifest)
    checked['junit'] = checked_evidence_paths(manifest.get('junit'), root, repositories)
    checked['reported_evidence'] = checked_evidence_paths(manifest.get('reported_evidence'), root, repositories)
    coverage = manifest.get('coverage') or {}
    if not isinstance(coverage, dict):
        raise ValueError('Manifest coverage must be an object')
    if coverage.get('summary_file'):
        summary = checked_evidence_paths([coverage['summary_file']], root, repositories)[0]
        checked['coverage'] = dict(coverage, summary_file=summary)
    return checked


def case_title(criterion):
    return f'[{criterion["id"]}] {criterion["title"]}'


def case_body(manifest, criterion, folder_id):
    tests = criterion['tests']
    description = ('Automated tests: ' + ', '.join(tests)
                   + f'\nIssue {manifest["issue"]}; revision {manifest.get("revision", "unknown")}.')
    return {'title': case_title(criterion), 'description': description,
            'precondition': criterion.get('precondition', ''),
            'folder': {'ID': folder_id}, 'status': {'ID': PUBLISHED}, 'scriptType': {'ID': CLASSIC_SCRIPT},
            'type': {'ID': CASE_TYPES[criterion.get('type', 'Functional')]},
            'automationStatus': {'ID': AUTOMATED},
            'automationKey': tests[0] if len(tests) == 1 else None,
            'jiraRequirementIDs': [manifest['issue']],
            'steps': [{'stepType': 'TEXT', 'step': s['step'], 'expectedResult': s.get('expected', '')}
                      for s in criterion.get('steps', [])]}


def unexpected_reply(label):
    return RuntimeError(f'AIO {label} returned an unexpected response')


def reply_field(reply, field, label, kinds=(str,)):
    """The `field` of an AIO reply object; any other reply shape ends the run with a fixed message."""
    if not isinstance(reply, dict) or not isinstance(reply.get(field), kinds):
        raise unexpected_reply(label)
    return reply[field]


def reply_items(reply, label):
    """The `items` list of an AIO search reply; an empty reply or a missing `items` means no items."""
    reply = reply or {}
    items = reply.get('items', []) if isinstance(reply, dict) else None
    if not isinstance(items, list):
        raise unexpected_reply(label)
    return items


def folder_tree(reply, label):
    """The folder nodes of an AIO folder tree reply; every node needs an `ID`, `children` stays optional."""
    return folder_nodes(reply.get('children', []) if isinstance(reply, dict) else reply, label)


def folder_nodes(nodes, label):
    if not isinstance(nodes, list):
        raise unexpected_reply(label)
    for node in nodes:
        if not isinstance(node, dict) or 'ID' not in node:
            raise unexpected_reply(label)
        if node.get('children') is not None:
            folder_nodes(node['children'], label)
    return nodes


def find_folder(tree, names):
    """Return the ID of the folder path `names` in a folder tree, or None when any level is missing."""
    level = tree if isinstance(tree, list) else tree.get('children', [])
    found = None
    for name in names:
        found = next((node for node in level if node.get('name') == name), None)
        if found is None:
            return None
        level = found.get('children') or []
    return found['ID']


def find_case(client, folder_id, criterion):
    if folder_id is None:
        return None
    query = {'folderID': {'comparisonType': 'IN', 'list': [folder_id]},
             'title': {'comparisonType': 'CONTAINS', 'value': f'[{criterion["id"]}]'}}
    label = 'POST /testcase/search'
    items = reply_items(client.call('POST', '/testcase/search', query), label)
    for item in items:
        reply_field(item, 'title', label)
        reply_field(item, 'key', label)
    return next((item for item in items if item['title'].startswith(f'[{criterion["id"]}]')), None)


def story_folder_id(client, manifest):
    label = 'GET /testcase/folder'
    tree = folder_tree(client.call('GET', '/testcase/folder'), label)
    return find_folder(tree, [manifest['epic_folder'], manifest['story_folder']])


def sync_cases(client, manifest, apply):
    names = [manifest['epic_folder'], manifest['story_folder']]
    folder_id = story_folder_id(client, manifest)
    plan = []
    if folder_id is None:
        plan.append({'action': 'create_folder', 'path': names})
        if apply:
            created_folder = client.call('PUT', '/testcase/folder/hierarchy', {'folderHierarchy': names})
            folder_id = reply_field(created_folder, 'ID', 'PUT /testcase/folder/hierarchy', (int, str))
    for criterion in manifest['criteria']:
        existing = find_case(client, folder_id, criterion)
        body = case_body(manifest, criterion, folder_id)
        if existing:
            plan.append({'action': 'update_case', 'key': existing['key'], 'title': body['title']})
            if apply:
                client.call('PATCH', f'/testcase/{existing["key"]}/detail', body)
        else:
            plan.append({'action': 'create_case', 'title': body['title'], 'tests': criterion['tests']})
            if apply:
                created = client.call('POST', '/testcase', body)
                plan[-1]['key'] = reply_field(created, 'key', 'POST /testcase')
    return plan


def record_outcome(outcomes, key, status):
    outcomes[key] = FAILED if FAILED in (status, outcomes.get(key)) else status


def junit_cases(path):
    try:
        return list(ElementTree.parse(path).getroot().iter('testcase'))
    except (ElementTree.ParseError, OSError):
        raise ValueError(f'JUnit file {Path(path).name} is not readable XML') from None


def junit_outcomes(paths):
    """Map `SimpleClass#method` to passed/failed from JUnit XML files.

    Gradle names parameterized invocations by display name ("[1] MODERN") and drops the method, so each
    class also gets a `SimpleClass#*` entry. criterion_status falls back to it only when the exact method
    is absent, and any failure anywhere in that class marks the fallback as failed.
    """
    outcomes = {}
    for path in paths:
        for case in junit_cases(path):
            if case.find('skipped') is not None:
                continue
            simple_class = case.get('classname', '').rsplit('.', 1)[-1]
            failed = case.find('failure') is not None or case.find('error') is not None
            status = FAILED if failed else PASSED
            record_outcome(outcomes, simple_class + '#' + case.get('name', '').split('(')[0], status)
            record_outcome(outcomes, simple_class + '#*', status)
    return outcomes


def test_outcome(test, outcomes):
    if test in outcomes:
        return outcomes[test]
    return outcomes.get(test.split('#', 1)[0] + '#*')


def reported_outcomes(reported):
    """Map the worker-reported results of tests that produce no JUnit to passed/failed."""
    if reported is not None and not isinstance(reported, dict):
        raise ValueError('reported_results must be an object')
    outcomes = {}
    for test, result in (reported or {}).items():
        if result not in ('passed', 'failed'):
            raise ValueError(f'Reported result for {test} must be passed or failed, not {result!r}')
        outcomes[test] = PASSED if result == 'passed' else FAILED
    return outcomes


def combined_outcomes(manifest):
    outcomes = junit_outcomes(manifest.get('junit') or [])
    for test, status in reported_outcomes(manifest.get('reported_results')).items():
        record_outcome(outcomes, test, status)
    return outcomes


def criterion_status(criterion, outcomes):
    results = {t: test_outcome(t, outcomes) for t in criterion['tests']}
    missing = [t for t, status in results.items() if status is None]
    if missing:
        raise ValueError(f'{criterion["id"]}: no JUnit or reported result for {", ".join(missing)}')
    return FAILED if FAILED in results.values() else PASSED


class CycleLedger:
    """Remembers the cycle, runs and attachments a cycle already has, so a rerun resumes instead of duplicating.

    A run recorded without a revision (older ledgers) counts as recorded for every revision.
    """

    def __init__(self, path):
        self.path = path
        self.state = self._load(path)

    @staticmethod
    def _load(path):
        if not (path and path.exists()):
            return {'runs': {}, 'attachments': []}
        try:
            state = json.loads(path.read_text())
        except (OSError, ValueError):
            raise ValueError('The cycle ledger could not be read') from None
        runs = state.get('runs') if isinstance(state, dict) else None
        if not isinstance(runs, dict) or not all(isinstance(run, dict) for run in runs.values()):
            raise ValueError('The cycle ledger holds a malformed run')
        attachments = state.get('attachments')
        if not isinstance(attachments, list) or not all(isinstance(name, str) for name in attachments):
            raise ValueError('The cycle ledger holds a malformed attachment list')
        return state

    def recorded_cycle(self):
        cycle = self.state.get('cycle')
        if cycle is None:
            return None
        if not (isinstance(cycle, dict) and isinstance(cycle.get('key'), str) and isinstance(cycle.get('title'), str)):
            raise ValueError('The cycle ledger holds a malformed cycle')
        return cycle

    def has_run(self, criterion_id, revision):
        run = self.state['runs'].get(criterion_id)
        return run is not None and run.get('revision', revision) == revision

    def has_attachment(self, name):
        return name in self.state['attachments']

    def record_cycle(self, key, title):
        self.state['cycle'] = {'key': key, 'title': title}
        self._save()

    def record_run(self, criterion_id, case_key, status, revision):
        self.state['runs'][criterion_id] = {'case': case_key, 'status': status, 'revision': revision}
        self._save()

    def record_attachment(self, name):
        self.state['attachments'].append(name)
        self._save()

    def _save(self):
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name('.' + self.path.name + '.tmp')
            temporary.write_text(json.dumps(self.state, indent=2))
            temporary.replace(self.path)


def evidence_bundle(paths, name):
    """Zip the evidence files into one attachment: one upload per cycle instead of one per file (AIO 429)."""
    archive = Path(tempfile.mkdtemp()) / name
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as bundle:
        for path in paths:
            bundle.write(path, Path(path).name)
    return archive


def locate_cycle(client, manifest, sprint, ledger):
    """Return (action, title, key) of the cycle: the ledger's own, else one found by title, else a new one."""
    recorded = ledger.recorded_cycle()
    if recorded:
        return 'use_cycle', recorded['title'], recorded['key']
    title = f'AI {datetime.now(timezone.utc).year} Sprint {sprint} {manifest["issue"]}'
    label = 'POST /testcycle/search'
    reply = client.call('POST', '/testcycle/search', {'title': {'comparisonType': 'EXACT_MATCH', 'value': title}})
    found = reply_items(reply, label)
    return ('use_cycle', title, reply_field(found[0], 'key', label)) if found else ('create_cycle', title, None)


def create_cycle(client, manifest, title):
    created = client.call('POST', '/testcycle/detail', {
        'title': title, 'jiraTaskIDs': [manifest['issue']],
        'objective': f'Post-merge verification of {manifest["issue"]} on {manifest["revision"]}.'})
    return reply_field(created, 'key', 'POST /testcycle/detail')


def sync_cycle(client, manifest, sprint, apply, ledger=None):
    if not (manifest.get('junit') or manifest.get('reported_results')) or not manifest.get('revision'):
        raise ValueError('cycle needs the merged revision and JUnit files or reported results')
    outcomes = combined_outcomes(manifest)
    statuses = {c['id']: criterion_status(c, outcomes) for c in manifest['criteria']}
    folder_id = story_folder_id(client, manifest)
    cases = {c['id']: find_case(client, folder_id, c) for c in manifest['criteria']}
    absent = [k for k, v in cases.items() if v is None]
    if absent:
        raise ValueError('Run `cases --apply` first; no AIO Case for ' + ', '.join(absent))
    ledger = ledger or CycleLedger(None)
    action, title, cycle_key = locate_cycle(client, manifest, sprint, ledger)
    plan = [{'action': action, 'title': title}]
    if apply and not cycle_key:
        cycle_key = create_cycle(client, manifest, title)
    if apply and not ledger.recorded_cycle():
        ledger.record_cycle(cycle_key, title)
    revision = manifest['revision']
    for criterion_id, case in cases.items():
        if ledger.has_run(criterion_id, revision):
            plan.append({'action': 'run_already_recorded', 'case': case['key'], 'criterion': criterion_id})
            continue
        plan.append({'action': 'record_run', 'case': case['key'], 'criterion': criterion_id,
                     'status': statuses[criterion_id]})
        if apply:
            client.call('POST', f'/testcycle/{cycle_key}/testcase/{case["key"]}/testrun?createNewRun=true',
                        {'testRunStatus': statuses[criterion_id], 'isAutomated': True,
                         'comments': [f'Revision {revision}']})
            ledger.record_run(criterion_id, case['key'], statuses[criterion_id], revision)
    evidence = (list(manifest.get('junit') or []) + list(manifest.get('reported_evidence') or [])
                + [p for p in [manifest.get('coverage', {}).get('summary_file')] if p])
    bundle = f'{manifest["issue"]}-{revision[:7]}-evidence.zip'
    if ledger.has_attachment(bundle):
        plan.append({'action': 'already_attached', 'file': bundle})
    else:
        plan.append({'action': 'attach', 'file': bundle, 'contains': [Path(p).name for p in evidence]})
        if apply:
            archive = evidence_bundle(evidence, bundle)
            try:
                client.upload(f'/testcycle/{cycle_key}/attachment', archive)
            finally:
                archive.unlink()
            ledger.record_attachment(bundle)
    if cycle_key:
        plan[0]['key'] = cycle_key
    return plan


def main(argv=None, client=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--root', type=Path, default=MAILBOX)
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('cases', 'cycle'):
        command = commands.add_parser(name)
        command.add_argument('--manifest', required=True)
        command.add_argument('--apply', action='store_true')
        if name == 'cycle':
            command.add_argument('--sprint', required=True)
    args = parser.parse_args(argv)
    manifest = load_manifest(args.manifest)
    if args.command == 'cycle':
        require_sprint(args.sprint)
        manifest = with_checked_evidence(manifest, args.root)
    client = client or AioClient(read_token(args.root))
    if args.command == 'cases':
        plan = sync_cases(client, manifest, args.apply)
    else:
        ledger = CycleLedger(args.root / 'aio' / 'ledger' / f'{manifest["issue"]}-sprint-{args.sprint}.json')
        plan = sync_cycle(client, manifest, args.sprint, args.apply, ledger)
    print(json.dumps({'issue': manifest['issue'], 'command': args.command, 'applied': args.apply,
                      'plan': plan}, indent=2, ensure_ascii=False))
    return plan


if __name__ == '__main__':
    try:
        main()
    except (ValueError, RuntimeError) as error:
        sys.exit(f'aio_sync: {error}')
    except OSError:
        sys.exit('aio_sync: a file could not be read or written')
