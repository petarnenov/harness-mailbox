#!/usr/bin/env python3
"""Run the 15-minute status round of the orchestrator by itself, once per UTC slot.

Usage:
  status_round_runner.py [--root <DIR>] start --endpoint <ORCHESTRATOR-ENDPOINT>
  status_round_runner.py [--root <DIR>] stop
  status_round_runner.py [--root <DIR>] status

`start` starts one detached runner for the mailbox root while an orchestrator holds
.copilot-mailbox-orchestrator.lock. The runner holds .status-round-runner.lock for its whole life, which is
also its state file (pid, endpoint, start time). At every 15-minute UTC boundary it runs the `request` step of
status_round.py for the slot and the `collect` step for the previous slot and records both results in
status-rounds/<YYYYMMDDTHHMM00Z>.json. It stops by itself at the first boundary where no orchestrator holds its
lock. A second `start` for the same endpoint starts nothing and changes nothing. `status` reads and creates nothing.
A tick wakes no session; it only records.
"""
import argparse
import contextlib
import fcntl
import importlib
import json
import os
import re
import secrets
import select
import signal
import stat
import subprocess
import sys
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, NamedTuple

RUNNER_LOCK = '.status-round-runner.lock'
ORCHESTRATOR_LOCK = '.copilot-mailbox-orchestrator.lock'
TICK_DIRECTORY = 'status-rounds'
TICK_NAME = re.compile(r'\d{8}T\d{4}00Z\.json')
TICK_NAME_FORMAT = '%Y%m%dT%H%M00Z.json'
TICK_STEM_FORMAT = '%Y%m%dT%H%M%SZ'
TIMESTAMP_FORMAT = '%Y-%m-%dT%H:%M:%SZ'
SLOT_MINUTES = 15
WAIT_STEP_SECONDS = 30
POLL_SECONDS = 0.1
STATE_ATTEMPTS = 50
START_ATTEMPTS = 50
STOP_ATTEMPTS = 100
MAX_STATE_BYTES = 4096
SIBLING_NAMES = ('request_round', 'collect_round', 'with_context', 'discover_workers', 'request_id',
                 'floor_to_slot', 'archive_dir', 'MailboxError', 'NAMESPACE', 'SLOT_FORMAT', 'receive',
                 'default_root', 'resolved_root')

USAGE_LINE = 'status_round_runner: invalid arguments'
MAILBOX_LINE = 'status_round_runner: the mailbox could not be read'
FAILURE_LINE = 'status_round_runner: a mailbox record could not be read or written'
STATE_LINE = 'status_round_runner: the runner state could not be read'
SIBLING_LINE = 'status_round_runner: the installed status round scripts do not match this runner'
OTHER_ENDPOINT_LINE = 'status_round_runner: a runner for another orchestrator endpoint is running'
NO_ORCHESTRATOR_LINE = 'status_round_runner: no orchestrator is running in this mailbox'
START_LINE = 'status_round_runner: the runner could not be started'
STOP_LINE = 'status_round_runner: the runner did not stop'
NO_PREVIOUS_ROUND = 'no status request exists for the previous slot'
UNREADABLE_TICK = 'the newest tick record could not be read'

ANTICIPATED_FAILURES = (OSError, ValueError, OverflowError, RuntimeError)  # RuntimeError: symlink loop before 3.13

_NO_INTERRUPT = threading.Event()


class RunnerError(Exception):
    """An anticipated failure that ends the command with one fixed line."""

    def __init__(self, line, status=1):
        super().__init__(line)
        self.line = line
        self.status = status


class StopRequest:
    """A stop that a signal handler can raise: no lock is taken, one byte is written to a non-blocking pipe."""

    def __init__(self):
        self._requested = False
        self._reader, self._writer = os.pipe()
        os.set_blocking(self._reader, False)
        os.set_blocking(self._writer, False)

    def set(self):
        self._requested = True
        try:
            os.write(self._writer, b'x')
        except OSError:
            pass  # the pipe is full: a stop byte is already waiting

    def is_set(self):
        return self._requested

    def wait(self, seconds):
        """Block for at most seconds; True when a stop was requested. A pending byte ends the wait at once."""
        select.select([self._reader], [], [], seconds)
        return self._requested

    def close(self):
        os.close(self._reader)
        os.close(self._writer)


class RunnerGone(Exception):
    """The lock holder ended while its state was being read: the mailbox has no runner."""


class QuietParser(argparse.ArgumentParser):
    """Argument parser that never prints its own text."""

    def error(self, message):
        raise RunnerError(USAGE_LINE, 2)


def real_clock():
    return datetime.now(timezone.utc)


def real_wait(interrupt, seconds):
    """Block for at most seconds; True when interrupt was set."""
    return interrupt.wait(seconds)


def orchestrator_lock_held(root):
    """True when a process holds the orchestrator lock; same probe as team_control.coordinator_locked."""
    try:
        descriptor = os.open(root / ORCHESTRATOR_LOCK, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return False
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        return False
    with os.fdopen(descriptor, 'rb') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
    return False


def spawn_detached_runner(arguments, lock_descriptor):
    """Start this script in its own session with the locked descriptor inherited."""
    subprocess.Popen([sys.executable, str(Path(__file__).resolve()), *arguments],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True, close_fds=True, pass_fds=(lock_descriptor,))


class Services(NamedTuple):
    """The collaborators of the runner; tests replace them to run every path in-process."""
    clock: Callable = real_clock
    wait: Callable = real_wait
    spawn_runner: Callable = spawn_detached_runner
    send_signal: Callable = os.kill
    orchestrator_alive: Callable = orchestrator_lock_held


def pause(services):
    services.wait(_NO_INTERRUPT, POLL_SECONDS)


def load_status_round():
    """The sibling status_round module when it offers everything the runner uses, else None."""
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        module = importlib.import_module('status_round')
    except Exception:  # a missing or broken sibling must end in the fixed line, whatever it raises
        return None
    finally:
        sys.dont_write_bytecode = previous
    return module if all(hasattr(module, name) for name in SIBLING_NAMES) else None


def require_siblings():
    module = load_status_round()
    if module is None:
        raise RunnerError(SIBLING_LINE)
    return module


@contextlib.contextmanager
def fixed_errors(status_round):
    """Turn the anticipated failures of the mailbox into fixed lines."""
    mailbox_errors = (status_round.MailboxError,) if status_round else ()
    try:
        yield
    except mailbox_errors:
        raise RunnerError(MAILBOX_LINE) from None
    except ANTICIPATED_FAILURES:
        raise RunnerError(FAILURE_LINE) from None


def mailbox_failures(status_round):
    return (status_round.MailboxError,) + ANTICIPATED_FAILURES


def format_slot(moment, status_round):
    return moment.strftime(status_round.SLOT_FORMAT)


def format_timestamp(moment):
    return moment.strftime(TIMESTAMP_FORMAT)


# Runner lock and state file

def open_runner_lock(root):
    descriptor = os.open(root / RUNNER_LOCK, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise RunnerError(FAILURE_LINE)
    return descriptor


def runner_lock_busy(root):
    """True while a runner holds the lock; creates nothing."""
    try:
        descriptor = os.open(root / RUNNER_LOCK, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return False
    with os.fdopen(descriptor, 'rb') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
    return False


def valid_state(value):
    pid = value.get('pid') if isinstance(value, dict) else None
    return (isinstance(pid, int) and not isinstance(pid, bool) and pid > 0
            and isinstance(value.get('endpoint'), str))


def read_state(root):
    """The runner state in its file, or None while it is empty, partial or invalid."""
    try:
        descriptor = os.open(root / RUNNER_LOCK, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, 'rb') as stream:
            raw = stream.read(MAX_STATE_BYTES + 1)
        value = json.loads(raw.decode('utf-8')) if len(raw) <= MAX_STATE_BYTES else None
    except (OSError, ValueError, RecursionError):
        return None
    return value if valid_state(value) else None


def read_busy_state(root, services):
    """The state of the runner that holds the lock once two reads one pause apart agree; at most 5 s.

    Raises RunnerGone as soon as the lock is free again."""
    previous = None
    for _ in range(STATE_ATTEMPTS):
        state = read_state(root)
        if state and state == previous:
            return state
        if not runner_lock_busy(root):
            raise RunnerGone
        previous = state
        pause(services)
    raise RunnerError(STATE_LINE)


def write_state(descriptor, state):
    """Replace the content of the locked file: truncate, write, fsync."""
    data = json.dumps(state).encode('utf-8')
    os.ftruncate(descriptor, 0)
    os.lseek(descriptor, 0, os.SEEK_SET)
    while data:
        data = data[os.write(descriptor, data):]
    os.fsync(descriptor)


def state_summary(label, state):
    return {'state': label, 'pid': state['pid'], 'endpoint': state['endpoint'],
            'started_at': state.get('started_at')}


# Tick and tick record

def request_exists(root, project, message_id):
    name = f'{message_id}.json'
    return any(os.path.lexists(root / folder / project / name) for folder in ('inbox', 'archive'))


def split_by_request(root, workers, slot, status_round):
    """The workers that were asked in the slot and, as records, those that were not."""
    asked, not_asked = [], []
    for project, session in workers:
        message_id = status_round.request_id(slot, project, session)
        if request_exists(root, project, message_id):
            asked.append((project, session))
        else:
            not_asked.append({'project': project, 'session_id': session, 'request': message_id})
    return asked, not_asked


def collect_previous(root, endpoint, previous_slot, status_round):
    workers, _ = status_round.discover_workers(root)
    asked, not_requested = split_by_request(root, workers, previous_slot, status_round)
    if not asked:
        return {'not_requested': not_requested, 'collect': None, 'collect_omitted': NO_PREVIOUS_ROUND}
    collect = status_round.with_context(root, status_round.collect_round(root, endpoint, previous_slot))
    return {'not_requested': not_requested, 'collect': collect, 'collect_omitted': None}


class StepOutcome(NamedTuple):
    """The result of one tick step, or None with the fixed error entry of its failure."""
    result: object
    error: object


def attempt(status_round, step, action):
    try:
        return StepOutcome(action(), None)
    except mailbox_failures(status_round) as error:
        reason = MAILBOX_LINE if isinstance(error, status_round.MailboxError) else FAILURE_LINE
        return StepOutcome(None, {'step': step, 'reason': reason})


class Tick(NamedTuple):
    """The slot a tick runs for, the slot before it, the time of the tick and the orchestrator endpoint."""
    slot_time: datetime
    slot: str
    previous_slot: str
    now: datetime
    endpoint: str


def make_tick(now, endpoint, status_round):
    slot_time = status_round.floor_to_slot(now)
    previous_time = slot_time - timedelta(minutes=SLOT_MINUTES)
    return Tick(slot_time, format_slot(slot_time, status_round), format_slot(previous_time, status_round),
                now, endpoint)


def build_tick_record(tick, status_round, request, previous):
    collected = previous.result or {'not_requested': [], 'collect': None, 'collect_omitted': None}
    return {
        'id': str(uuid.uuid5(status_round.NAMESPACE, f'status-round-tick:{tick.slot}')),
        'type': 'status_round_tick', 'slot': tick.slot, 'recorded_at': format_timestamp(tick.now),
        'orchestrator_endpoint': tick.endpoint, 'request': request.result, 'previous_slot': tick.previous_slot,
        'not_requested': collected['not_requested'], 'collect': collected['collect'],
        'collect_omitted': collected['collect_omitted'],
        'errors': [outcome.error for outcome in (request, previous) if outcome.error]}


def tick_directory(root):
    """status-rounds/, created when missing; a symbolic link or an escaping path is refused."""
    folder = root / TICK_DIRECTORY
    if os.path.lexists(folder) and (folder.is_symlink() or folder.resolve() != folder):
        raise RunnerError(MAILBOX_LINE)
    folder.mkdir(exist_ok=True)
    if folder.is_symlink() or folder.resolve() != folder or not folder.is_dir():
        raise RunnerError(MAILBOX_LINE)
    return folder


def publish_record(folder, name, record):
    """Link the record into place unless it exists; True when this call published it."""
    final = folder / name
    if os.path.lexists(final):
        return False
    temporary = folder / f'.{name}.{secrets.token_hex(8)}.tmp'
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o666)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
            handle.write(json.dumps(record, indent=2, ensure_ascii=False, allow_nan=False))
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, final)
        except FileExistsError:
            return False
        return True
    finally:
        temporary.unlink(missing_ok=True)


def run_tick(root, endpoint, now, status_round):
    """Request the slot of now, collect the previous slot and record both; True when a record was published."""
    tick = make_tick(now, endpoint, status_round)
    request = attempt(status_round, 'request', lambda: status_round.request_round(root, endpoint, now))
    previous = attempt(status_round, 'collect',
                       lambda: collect_previous(root, endpoint, tick.previous_slot, status_round))
    record = build_tick_record(tick, status_round, request, previous)
    return publish_record(tick_directory(root), tick.slot_time.strftime(TICK_NAME_FORMAT), record)


# Runner loop

def seconds_until_next_slot(now, slot_time):
    boundary = slot_time + timedelta(minutes=SLOT_MINUTES)
    return (boundary - now).total_seconds()


def wait_for_next_slot(slot_time, services, stop):
    """Wait in steps against the wall clock until the slot of slot_time is over or stop is set."""
    while not stop.is_set():
        now = services.clock()
        remaining = seconds_until_next_slot(now, slot_time)
        if remaining <= 0:
            return
        services.wait(stop, min(WAIT_STEP_SECONDS, remaining))


def run_loop(root, endpoint, status_round, services, stop):
    """Tick for the current slot at once and at every boundary until stop is set or no orchestrator runs."""
    while not stop.is_set() and services.orchestrator_alive(root):
        now = services.clock()
        try:
            run_tick(root, endpoint, now, status_round)
        except (RunnerError,) + mailbox_failures(status_round):
            pass  # nothing else is written; status shows current_slot_recorded false
        wait_for_next_slot(status_round.floor_to_slot(now), services, stop)


@contextlib.contextmanager
def stop_on_signals():
    """A StopRequest that SIGTERM and SIGINT raise; the previous handlers return when the block ends."""
    stop = StopRequest()

    def handler(signum, frame):
        stop.set()
    previous = {number: signal.signal(number, handler) for number in (signal.SIGTERM, signal.SIGINT)}
    try:
        yield stop
    finally:
        for number, old in previous.items():
            signal.signal(number, old)
        stop.close()


def claim_runner_lock(root, descriptor):
    """Refuse unless descriptor is this root's runner lock and its exclusive lock can be taken."""
    try:
        held, named = os.fstat(descriptor), os.lstat(root / RUNNER_LOCK)
        if not stat.S_ISREG(held.st_mode) or (held.st_dev, held.st_ino) != (named.st_dev, named.st_ino):
            raise RunnerError(START_LINE)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        raise RunnerError(START_LINE) from None


def command_run(root, args, status_round, services):
    claim_runner_lock(root, args.lock_fd)
    state = {'pid': os.getpid(), 'endpoint': args.endpoint,
             'started_at': format_timestamp(services.clock()), 'token': args.token}
    with stop_on_signals() as stop:
        try:
            write_state(args.lock_fd, state)
            run_loop(root, args.endpoint, status_round, services, stop)
        finally:
            os.ftruncate(args.lock_fd, 0)
    return None


# start, stop, status

def runner_arguments(root, endpoint, descriptor, token):
    return ['--root', str(root), 'run', '--endpoint', endpoint, '--lock-fd', str(descriptor), '--token', token]


def await_started_state(root, token, services):
    for _ in range(START_ATTEMPTS):
        state = read_state(root)
        if state and state.get('token') == token:
            return state
        pause(services)
    raise RunnerError(START_LINE)


def start_runner(root, endpoint, descriptor, services):
    if not services.orchestrator_alive(root):
        raise RunnerError(NO_ORCHESTRATOR_LINE)
    token = secrets.token_hex(16)
    try:
        services.spawn_runner(runner_arguments(root, endpoint, descriptor, token), descriptor)
    except OSError:
        raise RunnerError(START_LINE) from None
    return state_summary('started', await_started_state(root, token, services))


def report_running(root, endpoint, services):
    state = read_busy_state(root, services)
    if state['endpoint'] != endpoint:
        raise RunnerError(OTHER_ENDPOINT_LINE)
    return state_summary('already_running', state)


def take_runner_lock(descriptor, services):
    """True when the exclusive lock was taken, False when a runner holds it; a mere probe is waited out."""
    for _ in range(START_ATTEMPTS):
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            pass
        try:
            fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        pause(services)
    raise RunnerError(STATE_LINE)


def command_start(root, args, status_round, services):
    descriptor = open_runner_lock(root)
    try:
        for _ in range(START_ATTEMPTS):
            if take_runner_lock(descriptor, services):
                os.ftruncate(descriptor, 0)
                return start_runner(root, args.endpoint, descriptor, services)
            try:
                return report_running(root, args.endpoint, services)
            except RunnerGone:
                continue
        raise RunnerError(STATE_LINE)
    finally:
        os.close(descriptor)


def command_stop(root, args, status_round, services):
    if not runner_lock_busy(root):
        return {'state': 'not_running'}
    try:
        state = read_busy_state(root, services)
    except RunnerGone:
        return {'state': 'not_running'}
    try:
        services.send_signal(state['pid'], signal.SIGTERM)
    except ProcessLookupError:
        pass
    except OSError:
        raise RunnerError(STOP_LINE) from None
    for _ in range(STOP_ATTEMPTS):
        if not runner_lock_busy(root):
            return {'state': 'stopped'}
        pause(services)
    raise RunnerError(STOP_LINE)


def tick_folder_for_reading(root):
    folder = root / TICK_DIRECTORY
    if not os.path.lexists(folder):
        return None
    if folder.is_symlink() or folder.resolve() != folder or not folder.is_dir():
        raise RunnerError(MAILBOX_LINE)
    return folder


def tick_names(folder):
    """The names of the tick records directly in the folder, oldest first."""
    if folder is None:
        return []
    names = []
    for name in os.listdir(folder):
        if TICK_NAME.fullmatch(name) and slot_of_name(name) is not None:
            names.append(name)
    return sorted(names)


def slot_of_name(name):
    try:
        return datetime.strptime(name[:-5], TICK_STEM_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def newest_tick(root, folder, names, status_round):
    """The path, slot, record and problem of the newest tick record."""
    if not names:
        return None, None, None, None
    name = names[-1]
    slot = format_slot(slot_of_name(name), status_round)
    try:
        record = status_round.receive.read_json(folder / name)
    except ANTICIPATED_FAILURES:
        record = None
    path = f'{TICK_DIRECTORY}/{name}'
    if isinstance(record, dict) and record.get('slot') == slot:
        return path, slot, record, None
    return path, slot, None, UNREADABLE_TICK


def runner_fields(root, services):
    no_runner = {'running': False, 'pid': None, 'endpoint': None, 'started_at': None}
    if not runner_lock_busy(root):
        return no_runner
    try:
        state = read_busy_state(root, services)
    except RunnerGone:
        return no_runner
    return {'running': True, 'pid': state['pid'], 'endpoint': state['endpoint'],
            'started_at': state.get('started_at')}


def command_status(root, args, status_round, services):
    folder = tick_folder_for_reading(root)
    names = tick_names(folder)
    current = status_round.floor_to_slot(services.clock())
    path, last_slot, record, problem = newest_tick(root, folder, names, status_round)
    report = runner_fields(root, services)
    report.update({
        'current_slot': format_slot(current, status_round), 'last_slot': last_slot,
        'current_slot_recorded': current.strftime(TICK_NAME_FORMAT) in names,
        'last_tick_path': path, 'last_tick': record, 'last_tick_problem': problem})
    return report


# Command line

def build_parser():
    parser = QuietParser(add_help=False, allow_abbrev=False)
    parser.add_argument('--root', type=Path)
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('start', 'stop', 'status', 'run'):
        command = commands.add_parser(name, add_help=False, allow_abbrev=False)
        command.add_argument('--root', type=Path, default=argparse.SUPPRESS)
        if name in ('start', 'run'):
            command.add_argument('--endpoint', required=True)
        if name == 'run':
            command.add_argument('--lock-fd', type=positive_integer, required=True)
            command.add_argument('--token', required=True)
    return parser


def positive_integer(text):
    number = int(text)
    if number < 1:
        raise ValueError(text)
    return number


COMMANDS = {'start': command_start, 'stop': command_stop, 'status': command_status, 'run': command_run}
SIBLINGS_REQUIRED = ('start', 'status', 'run')


def resolve_root(args, status_round):
    try:
        root = args.root if args.root is not None else status_round.default_root()
        return status_round.resolved_root(root)
    except RuntimeError:
        raise RunnerError(MAILBOX_LINE) from None


def existing_directory(path):
    try:
        root = path.resolve()
    except RuntimeError:
        raise RunnerError(MAILBOX_LINE) from None
    if not root.is_dir():
        raise RunnerError(MAILBOX_LINE)
    return root


def selected_siblings(args):
    if args.command in SIBLINGS_REQUIRED or args.root is None:
        return require_siblings()
    return load_status_round()


def execute(args, services):
    status_round = selected_siblings(args)
    with fixed_errors(status_round):
        root = resolve_root(args, status_round) if status_round else existing_directory(args.root)
        return COMMANDS[args.command](root, args, status_round, services)


def main(argv=None, services=None):
    """Run one command; returns the exit status. Failures print one fixed line on standard error."""
    try:
        args = build_parser().parse_args(argv)
        result = execute(args, services or Services())
    except RunnerError as error:
        print(error.line, file=sys.stderr)
        return error.status
    if result is not None:
        print(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
