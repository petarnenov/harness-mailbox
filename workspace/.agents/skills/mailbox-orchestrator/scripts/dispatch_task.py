#!/usr/bin/env python3
"""Run the mailbox dependency gate using the dashboard's shared graph projection."""
import sys

from mailbox_layout import gate_directory, mailbox_root

MAILBOX = mailbox_root(__file__)
sys.path.insert(0, str(gate_directory(__file__)))

from dispatch_gate import main


def names_root(arguments):
    return any(argument == '--root' or argument.startswith('--root=') for argument in arguments)


def with_mailbox_root(arguments):
    return arguments if names_root(arguments) else ['--root', str(MAILBOX), *arguments]


if __name__ == '__main__':
    raise SystemExit(main(with_mailbox_root(sys.argv[1:])))
