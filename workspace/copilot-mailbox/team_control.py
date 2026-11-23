"""Discover and stop only tmux panes belonging to the observed mailbox team."""
from collections import Counter
import fcntl
import os
from pathlib import Path
import re
import runpy
import stat
import subprocess
import uuid


TEAM = Path(__file__).with_name("team")
SESSION_IDENTITY = "#{session_id}|#{pid}|#{session_created}|#{session_name}|#{@mailbox_team_token}"
IDENTITY = "#{session_id}|#{pid}|#{session_created}|#{pane_pid}|#{@mailbox_team_token}|#{@mailbox_pane_token}|#{@copilot_route}|#{@mailbox_initial_pid}"
FORMAT = ("#{session_id}|#{pane_id}|#{pane_dead}|#{@copilot_route}|#{@copilot_projects}|"
          "#{@mailbox_team_token}|#{@mailbox_pane_token}|#{pid}|#{session_created}|#{pane_pid}|#{@mailbox_initial_pid}")


def coordinator_locked(root):
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    with os.fdopen(os.open(root / ".copilot-mailbox-orchestrator.lock", flags), "rb") as lock:
        if stat.S_ISREG(os.fstat(lock.fileno()).st_mode):
            try:
                fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
    return False


def parse_roster(roster):
    """Require each project's original slot and consecutive numbered siblings."""
    observed = roster.split(",")
    counts = Counter(slot.partition("+")[0] for slot in observed)
    if (not all(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", project) for project in counts)
            or any(project.casefold() == "orchestrator" for project in counts)
            or len({project.casefold() for project in counts}) != len(counts)):
        raise ValueError
    ordered = []
    for project, count in sorted(counts.items()):
        ordered.append(project)
        ordered.extend(f"{project}+{number}" for number in range(2, count + 1))
    if set(observed) != set(ordered):
        raise ValueError
    return tuple(ordered)


def managed_team(root, *, include_retired=False):
    """Return live pane identities and the intended roster, including stopped workers."""
    root = Path(root).resolve()
    try:
        name = runpy.run_path(str(TEAM))["session_name"](root)
        result = subprocess.run(["tmux", "list-panes", "-s", "-t", "=" + name + ":", "-F", FORMAT],
                                capture_output=True, text=True, check=True, timeout=2)
        panes = {}
        owned = {}
        identities = {}
        foreign = False
        all_pinned = True
        snapshot = None
        for line in result.stdout.splitlines():
            session, pane, dead, role, roster, token, marker, server, created, pid, pinned = line.split("|")
            if (not re.fullmatch(r"\$[0-9]+", session) or not re.fullmatch(r"%[0-9]+", pane)
                    or dead not in ("0", "1") or not all(i.isdigit() for i in (server, created, pid))):
                return None
            current = (session, roster, token, server, created)
            if snapshot is None:
                snapshot = current
                projects = parse_roster(roster)
                if token and not re.fullmatch(r"[a-f0-9]{32}", token):
                    return None
            elif snapshot != current:
                return None
            if role not in (*projects, "orchestrator") or marker != token or (pinned and pinned != pid):
                foreign = True
                continue
            if role in owned or pane in identities:
                return None
            if not pinned:
                all_pinned = False
            owned[role] = pane
            identities[pane] = "|".join((session, server, created, pid, token, marker, role, pinned))
            if dead == "0":
                panes[role] = pane
        # A verified session's final worker can exit while a user-created split remains.
        # Expose that retired roster only to the launcher holding the startup lock.
        if not snapshot or (not owned and not (include_retired and token and foreign)):
            return None
        if not token and (foreign or "orchestrator" not in panes or not coordinator_locked(root)):
            return None
        return {"projects": projects, "panes": panes, "session": session, "token": token,
                "session_identity": "|".join((session, server, created, name, token)),
                "owned": owned, "identities": identities, "foreign": foreign, "can_stop": bool(token and all_pinned)}
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def stop_managed(root, expected, role=None):
    """Stop the confirmed team or a single live project worker, serializing with startup."""
    root = Path(root).resolve()
    try:
        with runpy.run_path(str(TEAM))["open_lock"](root / ".copilot-mailbox-team.lock") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            current = managed_team(root)
            if not current or current != expected:
                raise ValueError("The team changed. Refresh the participant list.")
            if not current["can_stop"]:
                raise ValueError("This team only supports reconnecting. Use a participant's terminal to stop it.")
            owned = current["owned"]
            if role is None:
                roles = sorted(owned, key=lambda item: item == "orchestrator")
            elif role in current["projects"] and role in current["panes"]:
                roles = [role]
            else:
                raise ValueError("The selected worker is no longer running.")
            # Evaluate ownership inside tmux as well. A pane can be moved by an external client
            # between the snapshot and this command. Kill only the individually verified panes.
            commands = ["tmux"]
            for participant in roles:
                pane = owned[participant]
                condition = "#{==:" + IDENTITY + "," + current["identities"][pane] + "}"
                if len(commands) > 1:
                    commands.append(";")
                commands.extend(["if-shell", "-F", "-t", pane, condition, "kill-pane -t " + pane,
                                 "display-message -p changed"])
            result = subprocess.run(commands, capture_output=True, text=True, check=True, timeout=5)
            if result.stdout.strip():
                raise ValueError("The team changed during shutdown. Refresh the participant list.")
            if role is None:
                release_surviving_session(current)
    except (OSError, subprocess.SubprocessError):
        raise ValueError("Shutdown did not finish. Refresh the participant list.") from None


def release_surviving_session(team):
    # Closing the last managed pane normally also closes the session. An unowned pane
    # can still be present or be added during shutdown; leave it running under a fresh name.
    target = team["session"]
    if subprocess.run(["tmux", "has-session", "-t", target], capture_output=True, timeout=2).returncode:
        return
    released = "mailbox-released-" + uuid.uuid4().hex
    condition = "#{==:" + SESSION_IDENTITY + "," + team["session_identity"] + "}"
    command = "rename-session -t '" + target + "' " + released
    result = subprocess.run(["tmux", "if-shell", "-F", "-t", target, condition, command,
                             "display-message -p changed"], capture_output=True, text=True, check=True, timeout=2)
    if result.stdout.strip():
        raise ValueError("The team changed while releasing its tmux session name. Check the open sessions.")
