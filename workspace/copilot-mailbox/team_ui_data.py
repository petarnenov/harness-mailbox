"""Local data sources for the interactive team launcher."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import runpy
import selectors
import signal
import stat
import subprocess
import time
import tempfile
import unicodedata
import uuid

from team_control import attach, coordinator_locked, managed_team, stop_managed, team_socket, tmux_client


TOOLS = Path(__file__).resolve().parent
SAFE_PROJECT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
THEME_FILE = "team-theme"
THEMES = {"dark", "light"}
READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
DASHBOARD_START_TIMEOUT = 120


def ensure_dashboard(root):
    """Serialize the status check and startup across terminals using this dashboard."""
    root = Path(root)
    workflow = root / "dashboard" / "scripts" / "workflow.sh"
    deadline = time.monotonic() + DASHBOARD_START_TIMEOUT
    try:
        if not workflow.is_file() and (root / "compose.yaml").is_file():
            workflow = root / "scripts" / "workflow.sh"
        # A lock on the workflow file needs no writable state in the user's checkout.
        with workflow.open("rb") as lock:
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        return False
                    time.sleep(0.1)
            with subprocess.Popen(["sh", str(workflow), "ensure-dashboard", str(root.resolve())], stdin=subprocess.DEVNULL,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                  start_new_session=True) as process:
                try:
                    return process.wait(timeout=max(0, deadline - time.monotonic())) == 0
                finally:
                    if process.returncode is None:
                        # The shell may still be waiting on Docker when the caller cancels.
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
    except (OSError, subprocess.TimeoutExpired):
        return False


def _project_id(value):
    return isinstance(value, str) and value.casefold() != "orchestrator" and SAFE_PROJECT.fullmatch(value)


def _directory_key(value):
    """Match existing folders by filesystem identity and offline entries by absolute path."""
    if not isinstance(value, str) or not value:
        return None
    try:
        path = Path(value).expanduser()
        if not path.is_absolute():
            return None
        path = path.resolve()
        try:
            info = path.stat()
            return (info.st_dev, info.st_ino)
        except OSError:
            return str(path)
    except (OSError, ValueError, RuntimeError):
        return None


_SIMPLE_FIELDS = {"id", "name", "repository_path", "working_directory"}


class _ProjectRegistry:
    """Project records retain their original keys; aliases are only a menu projection."""

    def __init__(self, root):
        self.content = runpy.run_path(str(TOOLS / "start"))["read_registry"](root)
        self.entries = self.content.get("projects", self.content) if isinstance(self.content, dict) else self.content
        if isinstance(self.entries, dict):
            items = self.entries.items()
        elif isinstance(self.entries, list):
            items = enumerate(self.entries)
        else:
            raise ValueError
        self.records = {}
        self.keys = {}
        by_directory = {}
        self.scopes = {}
        seen = set()
        for key, row in items:
            if not isinstance(row, dict):
                continue
            name = row.get("id", key)
            if not _project_id(name):
                continue
            if name.casefold() in seen:
                raise ValueError
            seen.add(name.casefold())
            self.records[name] = row
            self.keys[name] = key
            directory = _directory_key(row.get("working_directory"))
            repository = _directory_key(row.get("repository_path"))
            by_directory.setdefault(directory or ("id", name), []).append(name)
            if directory and repository and repository != directory:
                self.scopes.setdefault(repository, set()).add(directory)

        # A menu-created checkout-root alias can represent a single existing component.
        # Multiple independently configured working directories keep their own identities.
        for repository, components in self.scopes.items():
            if len(components) == 1 and repository in by_directory:
                root_ids = by_directory[repository]
                if all(self.records[name].keys() <= _SIMPLE_FIELDS and
                       _directory_key(self.records[name].get("repository_path")) == repository for name in root_ids):
                    component, = components
                    by_directory[component].extend(root_ids)
                    del by_directory[repository]
        self.groups = {}
        self.by_directory = {}
        for directory, names in by_directory.items():
            # Prefer an established route with project-specific configuration at the same directory.
            primary = next((name for name in names if self.records[name].keys() - _SIMPLE_FIELDS), names[0])
            members = (primary, *(name for name in names if name != primary))
            self.groups[primary] = members
            for name in names:
                key = _directory_key(self.records[name].get("working_directory"))
                if key is not None:
                    self.by_directory[key] = primary

    def existing(self, folder, repository):
        directory = _directory_key(str(folder))
        checkout = _directory_key(str(repository))
        if directory in self.by_directory:
            return self.by_directory[directory]
        scopes = self.scopes.get(directory, set())
        if scopes:
            if len(scopes) > 1:
                raise _RegistryActionError("This repository contains multiple projects. Select the project's working directory.")
            return self.by_directory[next(iter(scopes))]
        if checkout not in self.scopes:
            root_id = self.by_directory.get(checkout)
            if root_id is not None and self.records[root_id].keys() <= _SIMPLE_FIELDS:
                return root_id
        return None

    def snapshot(self, project_id):
        if project_id not in self.groups:
            raise _RegistryActionError("The project changed or no longer exists. Reopen the project list.")
        ids = self.groups[project_id]
        # List positions may shift when unrelated projects are removed.
        records = [(self.keys[name] if isinstance(self.entries, dict) else name, self.records[name]) for name in ids]
        digest = hashlib.sha256(json.dumps(records, sort_keys=True).encode()).hexdigest()
        return {"ids": ids, "working_directory": self.records[project_id].get("working_directory") or "",
                "fingerprint": digest}


class _RegistryActionError(ValueError):
    """Fixed operator-facing registry validation failure."""


def projects(root):
    """List each routable local project once without modifying its registry."""
    try:
        return sorted(_ProjectRegistry(Path(root)).groups)
    except (OSError, ValueError, TypeError):
        raise ValueError("The project registry is unavailable or contains ambiguous entries.") from None


def removal_snapshot(root, project_id):
    try:
        return _ProjectRegistry(Path(root)).snapshot(project_id)
    except _RegistryActionError:
        raise
    except (OSError, ValueError, TypeError):
        raise ValueError("Could not read the project from the registry.") from None


def folders(path):
    """List only immediate subdirectories; resolve links when the user selects one."""
    try:
        return sorted((p for p in Path(path).iterdir() if p.is_dir()),
                      key=lambda p: (p.name.startswith("."), p.name.casefold(), p.name))
    except (OSError, ValueError):
        raise ValueError("Could not open the folder.") from None


def suggested_project_id(folder):
    name = unicodedata.normalize("NFKD", Path(folder).name).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", name).strip("._-")[:120] or "project"
    return "orchestrator-project" if slug.casefold() == "orchestrator" else slug


def _repository_directory(folder):
    try:
        result = subprocess.run(["git", "-C", str(folder), "rev-parse", "--show-toplevel"],
                                capture_output=True, check=True, timeout=3)
        # Git terminates the path with exactly one newline, which can also occur in the name.
        candidate = Path(os.fsdecode(result.stdout[:-1])).resolve()
        if candidate == folder or candidate in folder.parents:
            return candidate
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return folder


@contextmanager
def _registry_lock(path):
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    with os.fdopen(fd, "rb") as lock:
        if not stat.S_ISREG(os.fstat(lock.fileno()).st_mode):
            raise OSError
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def _publish_registry(path, registry, before):
    # Keep UTF-8 names readable and round-trip POSIX filename bytes encoded by surrogateescape.
    contents = (json.dumps(registry.content, ensure_ascii=False, indent=2) + "\n").encode("utf-8", "backslashreplace")
    if len(contents) > 4 * 1024 * 1024:
        raise OSError
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".team-registry-", delete=False) as tmp:
        temporary = Path(tmp.name)
        try:
            tmp.write(contents)
            tmp.flush()
            os.fchmod(tmp.fileno(), stat.S_IMODE(before.st_mode))
            os.fsync(tmp.fileno())
            current = path.lstat()
            if (before.st_ino, before.st_mtime_ns, before.st_size) != (current.st_ino, current.st_mtime_ns, current.st_size):
                raise OSError
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


@contextmanager
def _registry_edit(root):
    path = root / "projects.json"
    try:
        with _registry_lock(root / ".team-registry.lock"):
            before = path.lstat()
            registry = _ProjectRegistry(root)
            if isinstance(registry.entries, list) and not all(isinstance(x, dict) for x in registry.entries):
                raise ValueError
            yield registry, lambda: _publish_registry(path, registry, before)
    except _RegistryActionError:
        raise
    except (OSError, ValueError, RuntimeError, TypeError):
        raise ValueError("Could not update the registry. Check projects.json and try again.") from None


def _directory_identity(directory):
    """Recognize a renamed folder without handing its history to a reused pathname or inode."""
    try:
        path = Path(directory).expanduser()
        if not path.is_absolute():
            return None
        info = path.stat()
        if not stat.S_ISDIR(info.st_mode) or not info.st_ino:
            return None
        created = getattr(info, "st_birthtime", None)
        if created is None:
            # Linux exposes birth time through statx, used by GNU stat, but not os.stat.
            result = subprocess.run(["/usr/bin/stat", "-c", "%w", "--", str(path.resolve())],
                                    capture_output=True, text=True, check=True, timeout=2,
                                    env={**os.environ, "LC_ALL": "C", "TZ": "UTC0"})
            created = result.stdout.strip()
        if not created or created == "-":
            return None
        return [info.st_dev, info.st_ino, created]
    except (OSError, RuntimeError, TypeError, ValueError, subprocess.SubprocessError):
        return None


def _route_binding(root, name, directory, *, reserve=False):
    """Keep removed route IDs bound to their original folder, independently of registry shape."""
    archive = root / "archive"
    if reserve:
        archive.mkdir(mode=0o700, exist_ok=True)
    try:
        parent = os.open(archive, READ_FLAGS | os.O_DIRECTORY)
    except FileNotFoundError:
        return False
    try:
        marker = ".team-route-" + hashlib.sha256(name.casefold().encode()).hexdigest()
        try:
            fd = os.open(marker, READ_FLAGS, dir_fd=parent)
        except FileNotFoundError:
            if not reserve:
                return False
            content = json.dumps([name, _directory_identity(directory)]).encode("ascii")
            # Commit each binding before the registry removal. A retry can reuse it.
            temporary = ".team-binding-" + uuid.uuid4().hex
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.link(temporary, marker, src_dir_fd=parent, dst_dir_fd=parent)
                os.fsync(parent)
            finally:
                os.unlink(temporary, dir_fd=parent)
            return True
        with os.fdopen(fd, "rb") as stream:
            if stat.S_ISREG(os.fstat(fd).st_mode):
                if reserve:
                    return True
                identity = _directory_identity(directory)
                content = json.dumps([name, identity]).encode("ascii")
                if identity is not None and stream.read(len(content) + 1) == content:
                    return True
        raise _RegistryActionError("This project ID belongs to a previously registered folder. Choose another ID.")
    finally:
        os.close(parent)


def _session_for_route(parent, filename, key):
    suffix = filename.casefold()[len(key) + 2:-5]
    if "--" not in suffix:
        return True
    # Both project IDs and endpoint names may contain "--". Consult the record
    # only when the filename alone could belong to two different projects.
    with os.fdopen(os.open(filename, READ_FLAGS, dir_fd=parent), "rb") as stream:
        limit = 4 * 1024 * 1024
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            return True
        data = stream.read(limit + 1)
    if len(data) <= limit:
        try:
            record = json.loads(data)
            project = record.get("project") if isinstance(record, dict) else None
            if _project_id(project) and filename.casefold().startswith(project.casefold() + "--"):
                return project.casefold() == key
        except (RecursionError, ValueError):
            pass
    return True


def _snapshot(parent, filename):
    """Read the dashboard's data-only snapshot formats from a verified directory."""
    try:
        with os.fdopen(os.open(filename, READ_FLAGS, dir_fd=parent), "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 4_000_000:
                return None
            contents = stream.read(4_000_001)
            if len(contents) <= 4_000_000:
                return json.loads(contents)
    except (OSError, RecursionError, ValueError):
        pass
    return None


def _matches_route(value, key):
    return isinstance(value, str) and value.casefold() == key


def _aggregate_mentions(data, key):
    pending = [data]
    while pending:
        record = pending.pop()
        if not isinstance(record, dict):
            continue
        for field in ("projects", "per_project", "project_metrics"):
            children = record.get(field)
            if isinstance(children, dict):
                if any(_matches_route(name, key) for name in children):
                    return True
                pending.extend(children.values())
            elif isinstance(children, list):
                for child in children:
                    if isinstance(child, dict) and isinstance(name := child.get("project", child.get("id")), str):
                        if _matches_route(name, key):
                            return True
                        pending.append(child)
    return False


def _epic_mentions(folder, key):
    issues = _snapshot(folder, "issues.json")
    if isinstance(issues, dict):
        issues = issues.get("issues", issues)
    if isinstance(issues, (dict, list)):
        for row in issues.values() if isinstance(issues, dict) else issues:
            if isinstance(row, dict):
                projects = row.get("projects", row.get("affected_projects", []))
                if _matches_route(projects, key) or (isinstance(projects, list) and
                                                     any(_matches_route(name, key) for name in projects)):
                    return True
    return _aggregate_mentions(_snapshot(folder, "progress.json"), key)


def _route_has_history(root, name):
    """A legacy mailbox may have records for an ID without a stored folder binding."""
    key = name.casefold()
    for tree in ("tasks", "inbox", "outbox", "archive", "projects", "sessions", "epics"):
        try:
            parent = os.open(root / tree, READ_FLAGS | os.O_DIRECTORY)
        except FileNotFoundError:
            continue
        try:
            for entry in os.listdir(parent):
                folded = entry.casefold()
                if tree in ("projects", "sessions") and folded.endswith(".json"):
                    if folded == key + ".json":
                        return True
                    if tree == "sessions" and folded.startswith(key + "--") and _session_for_route(parent, entry, key):
                        return True
                    if tree == "projects":
                        project = _snapshot(parent, entry)
                        if isinstance(project, dict) and (_matches_route(project.get("id"), key)
                                                         or _aggregate_mentions(project, key)):
                            return True
                if folded == key and tree != "epics":
                    child = os.open(entry, READ_FLAGS | os.O_DIRECTORY, dir_fd=parent)
                    try:
                        with os.scandir(child) as contents:
                            if next(contents, None) is not None:
                                return True
                    finally:
                        os.close(child)
                elif tree in ("epics", "tasks"):
                    if not stat.S_ISDIR(os.stat(entry, dir_fd=parent, follow_symlinks=False).st_mode):
                        continue
                    child = os.open(entry, READ_FLAGS | os.O_DIRECTORY, dir_fd=parent)
                    try:
                        if tree == "epics" and _epic_mentions(child, key):
                            return True
                        if tree == "tasks":
                            for filename in os.listdir(child):
                                if filename.endswith(".json"):
                                    task = _snapshot(child, filename)
                                    if isinstance(task, dict) and _matches_route(task.get("project"), key):
                                        return True
                    finally:
                        os.close(child)
        finally:
            os.close(parent)
    return False


def add_project(root, folder, project_id):
    """Reuse a local project or register its working directory."""
    root = Path(root)
    try:
        folder = Path(folder).expanduser().resolve(strict=True)
        if not folder.is_dir() or not os.access(folder, os.R_OK | os.X_OK):
            raise OSError
    except (OSError, ValueError, RuntimeError):
        raise ValueError("Choose an accessible local folder.") from None
    if not _project_id(project_id) or len(project_id) > 128:
        raise ValueError("Start the ID with a letter or digit. Use up to 128 ASCII letters, digits, periods, hyphens, or underscores.")
    repository = _repository_directory(folder)
    with _registry_edit(root) as (registry, publish):
        existing = registry.existing(folder, repository)
        if existing is not None:
            return existing
        entry = {"id": project_id, "name": folder.name or project_id, "working_directory": str(folder),
                 "repository_path": str(repository)}
        occupied = {key.casefold() for key in registry.records}
        if isinstance(registry.entries, dict):
            occupied.update(key.casefold() for key in registry.entries)
            if registry.entries is registry.content:
                occupied.add("projects")
        if project_id.casefold() in occupied:
            raise _RegistryActionError("This project ID is already in use or reserved. Enter a different ID.")
        if not _route_binding(root, project_id, folder) and _route_has_history(root, project_id):
            raise _RegistryActionError("This project ID has existing mailbox records. Choose another ID.")
        if isinstance(registry.entries, dict):
            registry.entries[project_id] = entry
        else:
            registry.entries.append(entry)
        publish()
    return project_id


def _unidentified_team(root, *, known_session=False):
    """A failed roster lookup cannot prove a worker has stopped."""
    try:
        if coordinator_locked(root):
            return True
    except FileNotFoundError:
        pass
    if known_session:
        return False
    name = runpy.run_path(str(TOOLS / "team"))["session_name"](root)
    try:
        result = subprocess.run([*tmux_client(team_socket(root)), "has-session", "-t", "=" + name], timeout=2,
                                capture_output=True, env={**os.environ, "LC_ALL": "C"})
    except FileNotFoundError:
        return False
    except (OSError, subprocess.SubprocessError):
        return True
    absent = (b"can't find session:", b"no server running on ", b"(No such file or directory)")
    return result.returncode != 1 or not any(reason in result.stderr for reason in absent)


def remove_project(root, project_id, expected):
    """Forget the confirmed registry entries, preserving every project file and mailbox record."""
    root = Path(root)
    try:
        with _registry_lock(root / ".copilot-mailbox-team.lock"), _registry_edit(root) as (registry, publish):
            current = registry.snapshot(project_id)
            if current != expected:
                raise _RegistryActionError("The registry entry changed. Review the project again before removing it.")
            active = managed_team(root, include_retired=True)
            if (not active or not active["panes"]) and _unidentified_team(root, known_session=active is not None):
                raise _RegistryActionError("Could not verify which workers are running. Try again.")
            if active and any(slot.partition("+")[0] in current["ids"] for slot in active["panes"]):
                raise _RegistryActionError("Stop all workers for this project before removing it.")
            keys = [registry.keys[name] for name in current["ids"]]
            for name in current["ids"]:
                _route_binding(root, name, registry.records[name].get("working_directory"), reserve=True)
            if isinstance(registry.entries, list):
                keys.sort(reverse=True)
            for key in keys:
                del registry.entries[key]
            publish()
    except OSError:
        raise ValueError("Could not lock the registry to remove the project.") from None


def active_team(root):
    """Observe a complete team, including the identity of a legacy coordinator."""
    root = Path(root).resolve()
    try:
        if not coordinator_locked(root):
            return None
        active = managed_team(root)
        if not active or set(active["panes"]) != {"orchestrator", *active["projects"]}:
            return None
        pane = active["panes"]["orchestrator"]
        return active["projects"], active["session"], pane, active["socket"], active["identities"][pane]
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def reconnect(root, expected):
    """Revalidate the team and its coordinator before attaching to the observed session."""
    if isinstance(expected, dict):
        active = managed_team(root)
        if not active or active != expected or "orchestrator" not in active["panes"]:
            raise ValueError("This team is no longer available. Reopen team to refresh its status.")
        pane = active["panes"]["orchestrator"]
        socket, identity = active["socket"], active["identities"][pane]
    else:
        active = active_team(root)
        if not active or active != expected:
            raise ValueError("This team is no longer available. Reopen team to refresh its status.")
        _, _, pane, socket, identity = active
    try:
        attach(socket, pane, identity)
    except OSError:
        raise ValueError("The team connection was interrupted.") from None


def valid_model_id(value):
    """Model arguments are individual CLI identifiers, including provider-qualified names."""
    return isinstance(value, str) and bool(re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._:/+@-]{0,254}(?:\[[a-zA-Z0-9]+\])?", value))


def _efforts(values):
    if not isinstance(values, list):
        return []
    return list(dict.fromkeys(item for item in values
                              if isinstance(item, str) and re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", item)))


def _copilot_rows():
    """Read models.list over the SDK's stdio transport. No session or prompt is created."""
    request = b'{"jsonrpc":"2.0","id":1,"method":"models.list","params":{}}'
    with subprocess.Popen(["copilot", "--headless", "--stdio"], cwd="/", stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True) as process:
        try:
            process.stdin.write(b"Content-Length: %d\r\n\r\n" % len(request) + request)
            process.stdin.flush()
            deadline = time.monotonic() + 6
            pending = bytearray()
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while selector.select(max(0, deadline - time.monotonic())):
                    chunk = os.read(process.stdout.fileno(), 65536)
                    if not chunk or time.monotonic() >= deadline:
                        break
                    pending.extend(chunk)
                    while b"\r\n\r\n" in pending:
                        header, _, body = pending.partition(b"\r\n\r\n")
                        length = re.search(rb"(?:^|\r\n)Content-Length: *([0-9]{1,7})(?:\r\n|$)",
                                           header, re.IGNORECASE)
                        if not length or int(length[1]) > 1048576:
                            return []
                        size = int(length[1])
                        if len(body) < size:
                            break
                        message = json.loads(body[:size])
                        pending = body[size:]
                        if isinstance(message, dict) and message.get("id") == 1:
                            return message.get("result", {}).get("models", [])
                    if len(pending) > 1052672:
                        break
        finally:
            # The npm shim can spawn the native CLI. Retire this probe's whole process group.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    return []


def _claude_rows():
    """Read the same model choices exposed during Claude Code SDK initialization."""
    request = {"type": "control_request", "request_id": "models", "request": {"subtype": "initialize"}}
    reply = subprocess.run(
        ["claude", "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
         "--no-session-persistence", "--strict-mcp-config", "--disable-slash-commands",
         "--settings", '{"disableAllHooks":true}'],
        cwd="/", input=json.dumps(request) + "\n", capture_output=True, check=True, timeout=6, text=True,
    )
    for line in reply.stdout.splitlines():
        frame = json.loads(line)
        if frame.get("type") == "control_response":
            response = frame.get("response", {})
            if response.get("request_id") == "models" and response.get("subtype") == "success":
                return response.get("response", {}).get("models", [])
    return []


class ModelCatalog(dict):
    """Map native CLI model IDs to effort levels and keep their optional menu labels."""
    def __init__(self):
        super().__init__()
        self.labels = {}

    def set_label(self, model, title):
        self.labels.pop(model, None)
        if isinstance(title, str) and len(title) <= 256 and title.isprintable():
            title = title.strip()
            if title and title != model:
                self.labels[model] = f"{title} ({model})"


def model_label(catalog, model):
    return getattr(catalog, "labels", {}).get(model, model)


def models(harness):
    """Return each installed harness's current model identifiers and model-specific efforts."""
    try:
        if harness == "copilot":
            rows = _copilot_rows()
        elif harness == "claude":
            rows = _claude_rows()
        elif harness == "codex":
            result = subprocess.run(["codex", "debug", "models"], capture_output=True,
                                    check=True, timeout=3, text=True)
            rows = json.loads(result.stdout)["models"]
        else:
            return {}
        catalog = ModelCatalog()
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            if harness == "copilot":
                policy = row.get("policy") or {}
                if not isinstance(policy, dict) or policy.get("state") not in (None, "enabled", "unconfigured"):
                    continue
                name, levels = row.get("id"), row.get("supportedReasoningEfforts")
            elif harness == "claude":
                name, levels = row.get("value"), row.get("supportedEffortLevels")
            else:
                if row.get("visibility") != "list":
                    continue
                name = row.get("slug")
                levels = row.get("supported_reasoning_levels")
                if isinstance(levels, list):
                    levels = [item.get("effort") for item in levels if isinstance(item, dict)]
            if valid_model_id(name) and name not in ("auto", "default"):
                catalog[name] = _efforts(levels)
                if harness == "claude":
                    catalog.set_label(name, row.get("displayName"))
        return catalog
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError, subprocess.SubprocessError):
        return {}


def _settings_directory(create):
    location = Path(os.environ.get("XDG_CONFIG_HOME", ""))
    if not location.is_absolute():
        location = Path.home() / ".config"
    if create:
        location.mkdir(mode=0o700, parents=True, exist_ok=True)
    parent = os.open(location, os.O_RDONLY | os.O_DIRECTORY)
    try:
        if create:
            try:
                os.mkdir("harness-mailbox", mode=0o700, dir_fd=parent)
            except FileExistsError:
                pass
        return os.open("harness-mailbox", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    finally:
        os.close(parent)


def read_theme():
    try:
        folder = _settings_directory(False)
        try:
            with os.fdopen(os.open(THEME_FILE, READ_FLAGS, dir_fd=folder), "rb") as stream:
                info = os.fstat(stream.fileno())
                if stat.S_ISREG(info.st_mode) and info.st_size <= 6 and stream.read(7) in (b"light", b"light\n"):
                    return "light"
        finally:
            os.close(folder)
    except OSError:
        pass
    return "dark"


def write_theme(theme):
    if theme not in THEMES:
        return False
    temporary = "." + uuid.uuid4().hex
    try:
        folder = _settings_directory(True)
        try:
            flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
            fd = os.open(temporary, flags, 0o600, dir_fd=folder)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write((theme + "\n").encode("ascii"))
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, THEME_FILE, src_dir_fd=folder, dst_dir_fd=folder)
            finally:
                try:
                    os.unlink(temporary, dir_fd=folder)
                except FileNotFoundError:
                    pass
        finally:
            os.close(folder)
    except OSError:
        return False
    return True


def launch(root, selected, orchestrator, worker):
    """Launch a new team using explicit, independent role configurations."""
    known = set(projects(root))
    if not selected or any(project not in known for project in selected):
        raise ValueError("Select at least one registered project.")
    launcher = Path(root).resolve() / "copilot-mailbox" / "team"
    args = [str(launcher), "--new-team", *selected]
    for role, config in (("orchestrator", orchestrator), ("worker", worker)):
        if not isinstance(config, dict) or config.get("harness") not in ("copilot", "claude", "codex"):
            raise ValueError("Choose a valid harness for each role.")
        args.extend([f"--{role}-harness", config["harness"]])
        model, effort = config.get("model"), config.get("effort")
        if model is not None:
            if not valid_model_id(model):
                raise ValueError("Enter a valid model ID.")
            args.extend([f"--{role}-model", model])
        if effort is not None:
            if model in (None, "auto") or not _efforts([effort]):
                raise ValueError("Choose a reasoning effort supported by the model.")
            args.extend([f"--{role}-effort", effort])
    try:
        os.execv(launcher, args)
    except OSError:
        raise ValueError("The team launcher is unavailable.") from None
