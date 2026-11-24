"""Shared access to the Epic issue snapshots (epics/<EPIC>/issues.json) and atomic JSON publication.

Snapshots come as a list, `{"issues": [...]}`, a keyed object or `{"issues": {keyed}}`. An issue is
identified by its own `key`, then its `issue_key`, then the object key of a keyed entry. A snapshot is
written back in the shape it was read, with its other top-level fields untouched.
"""
import contextlib
import fcntl
import json
import os
import secrets
from pathlib import Path

SNAPSHOT = 'issues.json'
EPICS = 'epics'
TEMPORARY_PREFIX = '.tmp-'
TEMPORARY_SUFFIX = '.part'
UNREADABLE_JSON = 'a mailbox record is not readable JSON'
UNUSABLE_FILE = 'a mailbox file could not be read or written'


class SnapshotError(ValueError):
    """A snapshot that cannot be used; the message is written by this module."""


def refusal_text(error):
    """One code-written line for an anticipated failure; decoder and OS text never passes through."""
    if isinstance(error, (json.JSONDecodeError, UnicodeDecodeError)):
        return UNREADABLE_JSON
    if isinstance(error, OSError):
        return UNUSABLE_FILE
    return str(error)


def reject_constant(literal):
    raise ValueError(f'non-finite number {literal}')


def _container(document):
    wrapped = isinstance(document, dict) and isinstance(document.get('issues'), (list, dict))
    return document['issues'] if wrapped else document


def _identity(record, object_key):
    for candidate in (record.get('key'), record.get('issue_key'), object_key):
        if isinstance(candidate, str) and candidate:
            return candidate
    return None


def issue_records(document):
    """(identity, record) of every issue object in the document; the records are the live objects."""
    container = _container(document)
    if isinstance(container, dict):
        entries = container.items()
    else:
        entries = ((None, record) for record in container) if isinstance(container, list) else ()
    return [(_identity(record, name), record) for name, record in entries if isinstance(record, dict)]


def require_real_epics_folder(root):
    if Path(root, EPICS).is_symlink():
        raise SnapshotError('The epics folder must not be a symbolic link')


def epic_snapshot_paths(root):
    """The regular, non-symlinked snapshot of every Epic folder, in name order."""
    require_real_epics_folder(root)
    paths = []
    for folder in sorted(Path(root, EPICS).glob('*')):
        path = folder / SNAPSHOT
        if folder.is_dir() and not folder.is_symlink() and path.is_file() and not path.is_symlink():
            paths.append(path)
    return paths


def epic_snapshot_path(root, epic):
    """The snapshot of one Epic named on the command line; the name is a single folder under epics."""
    if not epic or epic in ('.', '..') or Path(epic).name != epic or '\0' in epic:
        raise SnapshotError('The Epic must be the name of one folder under epics')
    require_real_epics_folder(root)
    folder = Path(root, EPICS, epic)
    path = folder / SNAPSHOT
    if folder.is_symlink() or path.is_symlink():
        raise SnapshotError(f'Epic {epic} must not be a symbolic link')
    if not path.is_file():
        raise SnapshotError(f'Epic {epic} has no {SNAPSHOT}')
    return path


def load_snapshot(path):
    try:
        document = json.loads(path.read_text(encoding='utf-8'), parse_constant=reject_constant)
    except (OSError, ValueError):
        raise SnapshotError(f'{path.parent.name}/{SNAPSHOT} is not readable JSON') from None
    if not isinstance(_container(document), (list, dict)):
        raise SnapshotError(f'{path.parent.name}/{SNAPSHOT} is not an issue snapshot')
    return document


def all_issue_records(root):
    return [entry for path in epic_snapshot_paths(root) for entry in issue_records(load_snapshot(path))]


@contextlib.contextmanager
def _locked(directory):
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


@contextlib.contextmanager
def _temporary_json(directory, data):
    """A flushed temporary file holding data, in the destination folder, removed when the block ends."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f'{TEMPORARY_PREFIX}{secrets.token_hex(8)}{TEMPORARY_SUFFIX}'
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
            handle.write(json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False) + '\n')
            handle.flush()
            os.fsync(handle.fileno())
        yield path
    finally:
        path.unlink(missing_ok=True)


def replace_json(path, data):
    with _temporary_json(path.parent, data) as temporary:
        os.replace(temporary, path)


def publish_new_json(path, data):
    """Publish data under a name that must be free; FileExistsError when it is taken."""
    with _temporary_json(path.parent, data) as temporary:
        os.link(temporary, path)


def update_snapshot(path, edit):
    """Under an exclusive lock on the Epic folder, let edit(records) change the snapshot.

    edit returns True when it changed a record; only then is the snapshot written back.
    """
    with _locked(path.parent):
        document = load_snapshot(path)
        if edit(issue_records(document)):
            replace_json(path, document)
