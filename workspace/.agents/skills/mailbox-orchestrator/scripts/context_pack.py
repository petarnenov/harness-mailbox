#!/usr/bin/env python3
"""Print the bounded, read-only context pack of a task or issue node for a mailbox.

The pack is advisory: it authorizes no work and replaces neither the dependency gate nor the
G1/G2 gates. Every failure prints one fixed line to stderr; the JSON result is the only stdout.
"""
import json
import re
import sys
from pathlib import Path

from mailbox_layout import REGISTRY, gate_directory, mailbox_root

MAILBOX = mailbox_root(__file__)
LOOKUP_LIMIT = 10
NODE = re.compile(r'(?:task|issue):[A-Za-z0-9][A-Za-z0-9._-]{0,127}')
USAGE = 'context_pack: usage: context_pack.py [--root <mailbox>] <task:id|issue:KEY>'
INVALID_NODE = 'context_pack: invalid node'
NO_REGISTRY = 'context_pack: no mailbox registry found; name the mailbox with --root'
CODE_NOT_LOADED = 'context_pack: dashboard code could not be loaded'
NODE_NOT_FOUND = 'context_pack: node not found'
UNREADABLE = 'context_pack: mailbox could not be read'


class Refusal(Exception):
    """A failure that ends the run with a fixed line and exit code."""

    def __init__(self, line, code):
        super().__init__(line)
        self.line, self.code = line, code


def parse_arguments(argv):
    """(named root or None, node) from the command line."""
    root, positional, rest = None, [], list(argv)
    while rest:
        argument = rest.pop(0)
        if argument == '--root' and rest and root is None:
            root = rest.pop(0)
        elif argument.startswith('--root=') and root is None:
            root = argument.removeprefix('--root=')
        elif argument.startswith('--'):
            raise Refusal(USAGE, 2)
        else:
            positional.append(argument)
    if len(positional) != 1 or not root and root is not None:
        raise Refusal(USAGE, 2)
    if not NODE.fullmatch(positional[0]):
        raise Refusal(INVALID_NODE, 2)
    return root, positional[0]


def mailbox_to_read(named_root):
    if named_root is not None:
        return Path(named_root)
    if not (MAILBOX / REGISTRY).is_file():
        raise Refusal(NO_REGISTRY, 1)
    return MAILBOX


def load_dashboard_modules():
    """(server, context_graph), imported from the nearest dashboard copy above this script."""
    try:
        directory = str(gate_directory(__file__))
        if directory not in sys.path:
            sys.path.insert(0, directory)
        import context_graph  # server imports it lazily; load it here so a broken copy is reported as unloadable code
        import server
        return server, context_graph
    except Exception:
        raise Refusal(CODE_NOT_LOADED, 1) from None


def load_context_for():
    server, _ = load_dashboard_modules()
    try:
        return server.context_for
    except Exception:
        raise Refusal(CODE_NOT_LOADED, 1) from None


def read_pack(root, node):
    context_for = load_context_for()
    try:
        pack = context_for(root, node)
    except Exception:
        raise Refusal(UNREADABLE, 1) from None
    if pack is None:
        raise Refusal(NODE_NOT_FOUND, 1)
    return pack


def task_node(task_id):
    return f'task:{task_id}'


def distinct(task_ids):
    return list(dict.fromkeys(task_ids))


def context_block(root, task_ids):
    """The 'context' object for the tasks a script names; never raises and prints nothing.

    The dashboard code is loaded, and the dashboard and the graph built, once and only when a task is named.
    """
    named = distinct(task_ids)
    block = {'available': True, 'packs': {}, 'omitted': max(0, len(named) - LOOKUP_LIMIT)}
    if not named:
        return block
    try:
        server, context_graph = load_dashboard_modules()
        nodes = [task_node(task_id) for task_id in named[:LOOKUP_LIMIT]]
        block['packs'] = context_graph.build_context_packs(server.build_dashboard(root), root, server.read_json, nodes)
        require_printable(block)
    except Exception:
        return unavailable_context()
    return block


def require_printable(block):
    """Raise when the block cannot be printed as strict JSON, so the caller reports it unavailable."""
    json.dumps(block, allow_nan=False)


def unavailable_context():
    return {'available': False, 'packs': {}, 'omitted': 0}


def mark_context_nodes(items, task_id_of, block):
    """Give each item whose task has a pack in the block its 'context_node'."""
    for item in items:
        node = task_node(task_id_of(item))
        if node in block['packs']:
            item['context_node'] = node


def main(argv):
    try:
        named_root, node = parse_arguments(argv)
        pack = read_pack(mailbox_to_read(named_root), node)
        output = json.dumps(pack, indent=2, allow_nan=False)
    except Refusal as refusal:
        print(refusal.line, file=sys.stderr)
        return refusal.code
    except Exception:
        print(UNREADABLE, file=sys.stderr)
        return 1
    print(output, file=sys.stdout)
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
