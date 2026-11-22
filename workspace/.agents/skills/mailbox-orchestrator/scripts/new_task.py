#!/usr/bin/env python3
"""Create a free-text task record with a fresh UUID for one registered project.

Usage: new_task.py --project <id> [--title <title>] <text | ->

The text is stored verbatim as the task description. The project's loop skill is
discovered from its repository; when it is missing or ambiguous the task records a
blocker, so the dependency gate refuses dispatch until the orchestrator resolves it.
"""
import argparse
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from mailbox_layout import mailbox_root

MAILBOX = mailbox_root(__file__)
SKILL_DIRS = ('.agents/skills', '.claude/skills', '.github/skills')
TITLE_LIMIT = 80


def registered_project(root, project_id):
    projects = json.loads((root / 'projects.json').read_text())['projects']
    for entry in projects:
        if entry.get('id') == project_id:
            return entry
    known = ', '.join(entry.get('id', '?') for entry in projects)
    raise ValueError(f'Unknown project {project_id!r}; registered projects: {known}')


def discover_loop_skills(project):
    bases = {Path(project['repository_path']), Path(project['working_directory'])}
    found = {}
    for base in bases:
        for skill_dir in SKILL_DIRS:
            for skill_file in (base / skill_dir).glob('*.loop/SKILL.md'):
                found.setdefault(skill_file.parent.name, str(skill_file.resolve()))
    return found


def default_title(text):
    first_line = text.strip().splitlines()[0].strip()
    if len(first_line) <= TITLE_LIMIT:
        return first_line
    return first_line[:TITLE_LIMIT - 1].rstrip() + '…'


def build_task(project, text, title, loop_skills, now):
    task = {
        'id': str(uuid.uuid4()),
        'project': project['id'],
        'source': 'free_text',
        'title': title or default_title(text),
        'description': text,
        'acceptance_criteria': [],
        'status': 'pending',
        'owner_session_id': None,
        'depends_on': [],
        'required_loop_skill': None,
        'required_loop_path': None,
        'allowed_paths': [],
        'evidence': [],
        'blockers': [],
        'created_at': now,
        'updated_at': now,
    }
    if len(loop_skills) == 1:
        (task['required_loop_skill'], task['required_loop_path']), = loop_skills.items()
    else:
        detail = 'none found' if not loop_skills else 'ambiguous: ' + ', '.join(sorted(loop_skills))
        task['blockers'].append(f'Project loop skill unresolved ({detail}); set required_loop_skill/path before dispatch')
    return task


def publish_new(path, value):
    """Write atomically and never replace an existing task file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f'.tmp-{path.name}'
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n')
    try:
        os.link(temporary, path)
    finally:
        temporary.unlink()


def create_task(root, project_id, text, title=None):
    if not text.strip():
        raise ValueError('Task text must not be empty')
    project = registered_project(root, project_id)
    now = datetime.now(timezone.utc).isoformat()
    task = build_task(project, text, title, discover_loop_skills(project), now)
    path = root / 'tasks' / project['id'] / f"{task['id']}.json"
    publish_new(path, task)
    return task, path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--project', required=True, help='Project ID from projects.json')
    parser.add_argument('--title', help='Short title; defaults to the first line of the text')
    parser.add_argument('--root', default=str(MAILBOX), help='Mailbox root')
    parser.add_argument('text', help='Free-text task, or - to read it from stdin')
    args = parser.parse_args(argv)
    text = sys.stdin.read() if args.text == '-' else args.text
    try:
        task, path = create_task(Path(args.root).resolve(), args.project, text, args.title)
    except (ValueError, OSError, KeyError) as error:
        print(json.dumps({'created': False, 'error': str(error)}, ensure_ascii=False))
        return 1
    print(json.dumps({'created': True, 'id': task['id'], 'project': task['project'],
                      'path': str(path), 'blockers': task['blockers']}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
