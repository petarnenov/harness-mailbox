"""Discover and stop only tmux panes belonging to the observed mailbox team."""
from collections import Counter, defaultdict, namedtuple
import copy
import fcntl
import functools
import os
from pathlib import Path
import re
import runpy
import shlex
import signal
import stat
import subprocess
import tempfile
import time
import uuid


TEAM = Path(__file__).with_name("team")
SESSION_IDENTITY = "#{session_id}|#{pid}|#{session_created}|#{session_name}|#{@mailbox_team_token}"
IDENTITY = "#{session_id}|#{pid}|#{session_created}|#{pane_pid}|#{@mailbox_team_token}|#{@mailbox_pane_token}|#{@copilot_route}|#{@mailbox_initial_pid}"
FORMAT = ("#{session_id}|#{pane_id}|#{pane_dead}|#{@copilot_route}|#{@copilot_projects}|"
          "#{@mailbox_team_token}|#{@mailbox_pane_token}|#{pid}|#{session_created}|#{pane_pid}|#{@mailbox_initial_pid}")
AGENT_IDENTITY = "#{session_id}|#{pid}|#{session_created}|#{pane_pid}|#{@mailbox_agent_pid}"
SOCKET_RECORD = ".copilot-mailbox-tmux-socket"
_RECORDED_SOCKET = object()
HANGUP_GRACE = 3.0
TERM_GRACE = 2.0
KILL_GRACE = 1.0
POLL_INTERVAL = 0.1
PROCESS_FAILURE = "Some team processes did not stop. Refresh the team and retry."
PROCESSES_UNCHECKED = "The team's processes could not be checked. Check for remaining agent processes."
CHANGED_DURING_SHUTDOWN = "The team changed during shutdown. Refresh the participant list."
SHUTDOWN_UNFINISHED = "Shutdown did not finish. Refresh the participant list."
DISPLAY_ONLY = frozenset({"agent_windows"})
Process = namedtuple("Process", "ppid uid start zombie")


def verify_socket(path):
    if not os.path.isabs(path):
        raise ValueError("Could not verify this team's tmux server.")
    info = os.lstat(path)
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError("Could not verify this team's tmux server.")


def team_socket(root):
    """A server is shared by its socket; session and pane IDs are local to that server."""
    try:
        fd = os.open(Path(root) / SOCKET_RECORD, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as source:
            info = os.fstat(source.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022
                    or not 0 < info.st_size <= 4096):
                raise ValueError("Could not verify this team's tmux server.")
            path = os.fsdecode(source.read(4097))
            if len(os.fsencode(path)) != info.st_size or "\x00" in path:
                raise ValueError("Could not verify this team's tmux server.")
        verify_socket(path)
        return path
    except FileNotFoundError:
        # Older teams have no record, and tmux removes the socket of a retired server.
        return None


def remember_socket(root, path):
    verify_socket(path)
    with tempfile.NamedTemporaryFile(dir=root, prefix=".team-socket-", delete=False) as stream:
        try:
            stream.write(os.fsencode(path))
            stream.flush()
            os.fsync(stream.fileno())
            os.replace(stream.name, Path(root) / SOCKET_RECORD)
        finally:
            Path(stream.name).unlink(missing_ok=True)


def tmux_client(socket):
    return ["tmux", "-S", socket] if socket else ["tmux"]


def legacy_status_command(socket, pane):
    # Older teams have the original launch arguments in tmux but no harness label.
    stock = "#{||:#{==:#{status-left},#{l:[#{session_name}] }},#{==:#{status-left},#{l:[#S] }}}"
    try:
        probe = subprocess.run([*tmux_client(socket), "display-message", "-p", "-t", pane,
                                "#{?" + stock + ",#{pane_start_command},}"],
                               check=True, capture_output=True, text=True, timeout=2)
        args = shlex.split(probe.stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return ""
    if (args[1:4] == ["--launch-pane", "orchestrator", "--harness"] and len(args) >= 5
            and args[4] in ("copilot", "claude", "codex")):
        label = args[4].capitalize()
        return f"if-shell -F -t {pane} '{stock}' {{ set-option -t {pane} status-left '[{label}] ' }} ; "
    return ""


def attach(socket, pane, identity, *, focus_only=False):
    """Check the observed coordinator in the same tmux client that changes focus."""
    fields = identity.split("|")
    if len(fields) != 8 or not re.fullmatch(r"%[0-9]+", pane):
        raise ValueError("The team connection changed. Reopen team to refresh its status.")
    session, server, created, pid, token, marker, role, pinned = fields
    if (not re.fullmatch(r"\$[0-9]+", session) or not all(re.fullmatch(r"[0-9]+", n) for n in (server, created, pid))
            or role != "orchestrator" or token != marker or (token and not re.fullmatch(r"[0-9a-f]{32}", token))
            or (pinned and pinned != pid)):
        raise ValueError("The team connection changed. Reopen team to refresh its status.")
    condition = "#{==:" + IDENTITY + "," + identity + "}"
    # Recheck ownership before altering the session label or changing focus.
    commands = legacy_status_command(socket, pane) + f"select-window -t {pane} ; select-pane -t {pane}"
    changed = "The team connection changed. Reopen team to refresh its status."
    client = [*tmux_client(socket), "if-shell", "-F", "-t", pane, condition]
    rejected = "display-message -p '" + changed + "'"
    if focus_only:
        result = subprocess.run([*client, commands, rejected], capture_output=True, check=True, timeout=2)
        if result.stdout.strip():
            raise ValueError(changed)
        return
    current = os.environ.get("TMUX", "").rsplit(",", 2)[0]
    nested = bool(current)
    if current and socket:
        try:
            nested = os.path.samefile(current, socket)
        except OSError:
            nested = False
        if not nested:
            os.environ.pop("TMUX", None)
            os.environ.pop("TMUX_PANE", None)
    action = "switch-client" if nested else "attach-session"
    os.execvp("tmux", [*client, commands + f" ; {action} -t '{session}'", rejected])


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


def tmux_literal(text):
    return text.replace("#", "##").replace(",", "#,").replace("}", "#}")


def pane_format(root):
    """Extend FORMAT with the agent markers; tmux compares the root so it stays out of the field syntax."""
    return FORMAT + "|#{==:#{@mailbox_root}," + tmux_literal(str(root)) + "}|#{@mailbox_agent_pid}|#{window_name}"


def read_linux_processes():
    table = {}
    for entry in os.scandir("/proc"):
        if not entry.name.isdigit():
            continue
        try:
            with open(f"/proc/{entry.name}/stat", encoding="utf-8", errors="replace") as source:
                after_name = source.read().rpartition(")")[2].split()
            table[int(entry.name)] = Process(int(after_name[1]), entry.stat().st_uid, after_name[19],
                                             after_name[0] == "Z")
        except (OSError, IndexError, ValueError):
            continue
    return table


def read_ps_processes():
    result = subprocess.run(["ps", "-A", "-o", "pid=", "-o", "ppid=", "-o", "uid=", "-o", "stat=", "-o", "lstart="],
                            capture_output=True, text=True, check=True, timeout=5)
    table = {}
    for line in result.stdout.splitlines():
        pid, ppid, uid, state, *started = line.split()
        table[int(pid)] = Process(int(ppid), int(uid), " ".join(started), state.startswith("Z"))
    return table


class ProcessTableError(ValueError):
    pass


def read_process_table():
    """Map pid to parent, owner, start time and zombie state; an unreadable table is an error, not empty."""
    try:
        return read_linux_processes() if os.path.isdir("/proc/self") else read_ps_processes()
    except (OSError, ValueError, subprocess.SubprocessError):
        raise ProcessTableError(PROCESS_FAILURE) from None


def read_environment(pid):
    try:
        with open(f"/proc/{pid}/environ", "rb") as source:
            entries = source.read().split(b"\0")
    except OSError:
        return {}
    return dict(entry.decode("utf-8", "replace").partition("=")[::2] for entry in entries if b"=" in entry)


def descendants(table, roots):
    """The roots present in the table and everything below them."""
    children = defaultdict(list)
    for pid, process in table.items():
        children[process.ppid].append(pid)
    found = set()
    pending = [pid for pid in roots if pid in table]
    while pending:
        pid = pending.pop()
        if pid not in found:
            found.add(pid)
            pending.extend(children[pid])
    return found


def ancestors(table, pid):
    chain = set()
    while pid in table and pid not in chain:
        chain.add(pid)
        pid = table[pid].ppid
    return chain


def agent_record(fields, processes):
    """Identity and window label of a live pane that an agent of this root marked, else None."""
    session, _, dead, _, _, _, _, server, created, pid, _, *extra = fields
    if dead != "0" or len(extra) != 3 or extra[0] != "1" or not extra[1].isdigit():
        return None
    try:
        agent_tree = descendants(processes(), [int(pid)])
    except ProcessTableError:
        return None
    if int(extra[1]) not in agent_tree:
        return None
    label = "".join(char for char in extra[2] if char.isprintable())[:40]
    return "|".join((session, server, created, pid, extra[1])), label


def managed_team(root, *, include_retired=False, socket=_RECORDED_SOCKET):
    """Return live pane identities and the intended roster, including stopped workers."""
    root = Path(root).resolve()
    try:
        name = runpy.run_path(str(TEAM))["session_name"](root)
        if socket is _RECORDED_SOCKET:
            socket = team_socket(root)
        result = subprocess.run([*tmux_client(socket), "list-panes", "-s", "-t", "=" + name + ":", "-F", pane_format(root)],
                                capture_output=True, text=True, check=True, timeout=2)
        panes = {}
        owned = {}
        identities = {}
        agents = {}
        agent_windows = {}
        processes = functools.cache(read_process_table)
        foreign = False
        all_pinned = True
        snapshot = None
        for line in result.stdout.splitlines():
            fields = line.split("|", 13)
            session, pane, dead, role, roster, token, marker, server, created, pid, pinned = fields[:11]
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
                agent = agent_record(fields, processes)
                if agent:
                    agents[pane], agent_windows[pane] = agent
                else:
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
        others = foreign or bool(agents)
        if not snapshot or (not owned and not (include_retired and token and others)):
            return None
        if not token and (others or "orchestrator" not in panes or not coordinator_locked(root)):
            return None
        return {"projects": projects, "panes": panes, "session": session, "token": token, "socket": socket,
                "session_identity": "|".join((session, server, created, name, token)),
                "owned": owned, "identities": identities, "foreign": foreign,
                "agents": agents, "agent_windows": agent_windows, "can_stop": bool(token and all_pinned)}
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def stop_managed(root, expected, role=None):
    """Stop the confirmed team or a single live project worker, serializing with startup."""
    root = Path(root).resolve()
    ignored = DISPLAY_ONLY if role is None else DISPLAY_ONLY | {"agents"}
    try:
        with runpy.run_path(str(TEAM))["open_lock"](root / ".copilot-mailbox-team.lock") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            current = managed_team(root)
            if (not current or not expected
                    or verified_snapshot(current, ignored) != verified_snapshot(expected, ignored)):
                raise ValueError("The team changed. Refresh the participant list.")
            if not current["can_stop"]:
                raise ValueError("This team only supports reconnecting. Use a participant's terminal to stop it.")
            if role is None:
                stop_team(root, current, sorted(current["owned"], key=lambda item: item == "orchestrator"))
            elif role in current["projects"] and role in current["panes"]:
                stop_worker(root, current, role)
            else:
                raise ValueError("The selected worker is no longer running.")
    except (OSError, subprocess.SubprocessError):
        raise ValueError(SHUTDOWN_UNFINISHED) from None


def stop_worker(root, current, role):
    refusal = send_kill_chain(current["socket"], kill_requests(root, current, [role], {}))
    if refusal:
        raise ValueError(refusal)


def stop_team(root, current, roles):
    processes = plan_processes(root, current)
    refusal = send_kill_chain(current["socket"], kill_requests(root, current, roles, current["agents"]))
    if refusal is None:
        try:
            release_surviving_session(current)
        finally:
            processes.terminate()
        return
    # Only panes that tmux confirms closed may lose their processes; a termination error wins.
    surviving_processes(processes, current["socket"]).terminate()
    raise ValueError(refusal)


def plan_processes(root, current):
    try:
        return TeamProcesses.below_panes(root, current)
    except ProcessTableError:
        return UncheckedProcesses()


def send_kill_chain(socket, requests):
    """Evaluate ownership inside tmux too: a pane can be moved by an external client after the snapshot.

    Returns the fixed refusal text, or None when every verified pane was closed."""
    commands = tmux_client(socket)
    for index, request in enumerate(requests):
        commands.extend([";", *request] if index else request)
    try:
        result = subprocess.run(commands, capture_output=True, text=True, check=True, timeout=5)
    except subprocess.SubprocessError:
        return SHUTDOWN_UNFINISHED
    return CHANGED_DURING_SHUTDOWN if result.stdout.strip() else None


def live_pane_pids(socket):
    result = subprocess.run([*tmux_client(socket), "list-panes", "-a", "-F", "#{pane_pid}"],
                            capture_output=True, text=True, check=True, timeout=2)
    return {int(line) for line in result.stdout.split()}


def surviving_processes(processes, socket):
    """The processes of panes confirmed closed; without that confirmation nothing may be signalled."""
    try:
        return processes.without_panes(live_pane_pids(socket))
    except (OSError, ValueError, subprocess.SubprocessError):
        return UncheckedProcesses()


def verified_snapshot(team, ignored=DISPLAY_ONLY):
    """Window labels are display-only; a tmux rename must not invalidate a confirmed stop."""
    return {key: value for key, value in team.items() if key not in ignored}


def verified_kill(pane, condition):
    return ["if-shell", "-F", "-t", pane, condition, "kill-pane -t " + pane, "display-message -p changed"]


def agent_condition(root, identity):
    return "#{&&:#{==:#{@mailbox_root}," + tmux_literal(str(root)) + "},#{==:" + AGENT_IDENTITY + "," + identity + "}}"


def kill_requests(root, current, roles, agents):
    """Agent panes added to the session come first; owned panes follow in stop order."""
    requests = [verified_kill(pane, agent_condition(root, identity)) for pane, identity in agents.items()]
    for participant in roles:
        pane = current["owned"][participant]
        requests.append(verified_kill(pane, "#{==:" + IDENTITY + "," + current["identities"][pane] + "}"))
    return requests


def open_handles(pids):
    """Pidfds opened before the table read pin the processes; None means signal by pid instead."""
    opener = getattr(os, "pidfd_open", None)
    handles = {}
    for pid in pids:
        try:
            handles[pid] = opener(pid) if opener else None
        except ProcessLookupError:
            continue
        except OSError:
            handles[pid] = None
    return handles


def deliver(pid, handle, signum):
    try:
        if handle is None:
            os.kill(pid, signum)
        else:
            signal.pidfd_send_signal(handle, signum)
    except OSError:
        pass


def send_signals(targets, signum):
    """One signal round: only processes that still have the recorded start time and our uid are signalled."""
    handles = open_handles(targets)
    try:
        table = read_process_table()
        for pid, handle in handles.items():
            process = table.get(pid)
            if process is not None and process.start == targets[pid] and process.uid == os.getuid():
                deliver(pid, handle, signum)
    except ProcessTableError:
        pass
    finally:
        for handle in handles.values():
            if handle is not None:
                os.close(handle)


def wait_until(done, grace):
    deadline = time.monotonic() + grace
    while not done() and time.monotonic() < deadline:
        time.sleep(POLL_INTERVAL)


class TeamProcesses:
    """Same-user processes that the stopped panes started, tracked by pid and start time."""

    def __init__(self, root, server_pid, pane_pids, table):
        self.root = str(root)
        self.server_pid = server_pid
        self.table = table
        self.pane_pids = pane_pids
        self.protected = {os.getpid(), server_pid} | ancestors(table, os.getpid())
        self.targets = self.eligible(table, descendants(table, pane_pids))
        self.agent_pids = {str(pid) for pid in self.targets
                           if read_environment(pid).get("HARNESS_MAILBOX_AGENT_PID") == str(pid)}

    @classmethod
    def below_panes(cls, root, current):
        rows = [identity.split("|") for identity in (*current["identities"].values(), *current["agents"].values())]
        server_pid = int(rows[0][1])
        table = read_process_table()
        # Only direct children of the tmux server are panes; a stale pid elsewhere is not a root.
        pane_pids = {int(row[3]) for row in rows if int(row[3]) in table and table[int(row[3])].ppid == server_pid}
        return cls(root, server_pid, pane_pids, table)

    def without_panes(self, open_pane_pids):
        """A copy that leaves out the panes still open and every process below them."""
        open_panes = self.pane_pids & open_pane_pids
        open_tree = descendants(self.table, open_panes)
        remaining = copy.copy(self)
        remaining.pane_pids = self.pane_pids - open_panes
        remaining.targets = {pid: start for pid, start in self.targets.items() if pid not in open_tree}
        remaining.agent_pids = {pid for pid in self.agent_pids if int(pid) in remaining.targets}
        return remaining

    def eligible(self, table, pids):
        return {pid: table[pid].start for pid in pids
                if pid not in self.protected and table[pid].uid == os.getuid() and not table[pid].zombie}

    def living(self, table):
        return {pid: start for pid, start in self.targets.items()
                if pid in table and table[pid].start == start and not table[pid].zombie}

    def exited(self):
        return not self.living(read_process_table())

    def marked(self, table):
        """Detached descendants keep the agent's environment even after losing their parent."""
        server_subtree = descendants(table, [self.server_pid]) - descendants(table, self.pane_pids)
        candidates = self.eligible(table, [pid for pid in table if pid not in server_subtree])
        return {pid for pid in candidates if self.carries_agent_marker(read_environment(pid))}

    def carries_agent_marker(self, environment):
        return (environment.get("HARNESS_MAILBOX_AGENT_PID") in self.agent_pids
                and environment.get("HARNESS_MAILBOX_ROOT") == self.root)

    def pending(self):
        table = read_process_table()
        living = self.living(table)
        found = self.eligible(table, descendants(table, living) | self.marked(table))
        return {**found, **living}

    def terminate(self):
        wait_until(self.exited, HANGUP_GRACE)
        for signum, grace in ((signal.SIGTERM, TERM_GRACE), (signal.SIGKILL, KILL_GRACE)):
            self.targets = self.pending()
            if not self.targets:
                return
            send_signals(self.targets, signum)
            wait_until(self.exited, grace)
        if self.pending():
            raise ValueError(PROCESS_FAILURE)


class UncheckedProcesses:
    """Stand-in when the process plan is unavailable: no process may be signalled."""

    def without_panes(self, open_pane_pids):
        return self

    def terminate(self):
        raise ValueError(PROCESSES_UNCHECKED)


def release_surviving_session(team):
    # Closing the last managed pane normally also closes the session. An unowned pane
    # can still be present or be added during shutdown; leave it running under a fresh name.
    target = team["session"]
    client = tmux_client(team.get("socket"))
    if subprocess.run([*client, "has-session", "-t", target], capture_output=True, timeout=2).returncode:
        return
    released = "mailbox-released-" + uuid.uuid4().hex
    condition = "#{==:" + SESSION_IDENTITY + "," + team["session_identity"] + "}"
    command = "rename-session -t '" + target + "' " + released
    result = subprocess.run([*client, "if-shell", "-F", "-t", target, condition, command,
                             "display-message -p changed"], capture_output=True, text=True, check=True, timeout=2)
    if result.stdout.strip():
        raise ValueError("The team changed while releasing its tmux session name. Check the open sessions.")
