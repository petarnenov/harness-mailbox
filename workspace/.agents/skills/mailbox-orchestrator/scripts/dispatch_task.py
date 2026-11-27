#!/usr/bin/env python3
"""Run the mailbox dependency gate using the dashboard's shared graph projection."""
import sys

from mailbox_layout import REGISTRY, gate_directory, mailbox_root

MAILBOX = mailbox_root(__file__)
NO_REGISTRY = 'dispatch_task: no mailbox registry found; name the mailbox with --root'
GATE_NOT_LOADED = 'dispatch_task: dispatch gate could not be loaded'
GATE_FAILED = 'dispatch_task: dispatch gate failed'


def names_root(arguments):
    return any(argument == '--root' or argument.startswith('--root=') for argument in arguments)


def with_mailbox_root(arguments):
    return arguments if names_root(arguments) else ['--root', str(MAILBOX), *arguments]


def has_registry(root):
    return (root / REGISTRY).is_file()


def load_gate_main():
    """The gate's main, imported from the nearest dashboard copy above this script; None when it cannot be loaded."""
    try:
        directory = str(gate_directory(__file__))
        if directory not in sys.path:
            sys.path.insert(0, directory)
        from dispatch_gate import main
        return main
    except Exception:
        return None


def refuse(line):
    print(line, file=sys.stderr)
    return 1


def main(argv):
    if not names_root(argv) and not has_registry(MAILBOX):
        return refuse(NO_REGISTRY)
    gate_main = load_gate_main()
    if gate_main is None:
        return refuse(GATE_NOT_LOADED)
    try:
        return gate_main(with_mailbox_root(argv))
    except Exception:
        return refuse(GATE_FAILED)


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
