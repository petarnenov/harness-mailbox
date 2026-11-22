"""Where the mailbox and its dependency gate live, for scripts started without --root.

The skills sit at <mailbox>/.agents/skills/ in an installed workspace and at
<mailbox>/workspace/.agents/skills/ in the application checkout, so the depth of a script
does not tell where the mailbox is. Its registry does.
"""
from pathlib import Path

REGISTRY = 'projects.json'
GATE = 'dispatch_gate.py'
INSTALLED_DEPTH = 4


def mailbox_root(script):
    """The nearest directory above the script that holds the project registry.

    Without a registry anywhere above, the installed layout is assumed.
    """
    ancestors = Path(script).resolve().parents
    return next((folder for folder in ancestors if (folder / REGISTRY).is_file()), ancestors[INSTALLED_DEPTH])


def gate_directory(script):
    """The nearest directory above the script that holds the gate, itself or in dashboard/."""
    for folder in Path(script).resolve().parents:
        for candidate in (folder / 'dashboard', folder):
            if (candidate / GATE).is_file():
                return candidate
    raise FileNotFoundError(f'{GATE} was not found above {script}')
