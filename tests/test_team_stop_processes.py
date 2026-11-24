"""Entire-team stop: agent panes added after launch and the process trees below them."""
import errno
import os
from pathlib import Path
import runpy
import signal
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, mock_open, patch


PLUGIN = Path(__file__).resolve().parents[1] / 'workspace' / 'copilot-mailbox'
UID = os.getuid()
SURVIVORS = 'Some team processes did not stop. Refresh the team and retry.'
UNCHECKED = "The team's processes could not be checked. Check for remaining agent processes."
CHANGED = 'The team changed during shutdown. Refresh the participant list.'
UNFINISHED = 'Shutdown did not finish. Refresh the participant list.'
RAW_TMUX_ERROR = 'no server running on /home/private/tmux-1000/default'


def load_control():
    with patch.object(sys, 'path', [str(PLUGIN), *sys.path]):
        import team_control
    return team_control


control = load_control()
Process = control.Process
REAL_SEND_SIGNALS = control.send_signals


def proc(ppid, *, uid=UID, start='t0', zombie=False):
    return Process(ppid, uid, start, zombie)


def agent_marker(agent_pid, root):
    return {'HARNESS_MAILBOX_AGENT_PID': str(agent_pid), 'HARNESS_MAILBOX_ROOT': str(root)}


class AgentPaneTests(unittest.TestCase):
    SERVER = 700
    TEAM_PANES = {'%20': 900, '%21': 901, '%22': 902}

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.token = '0123456789abcdef' * 2
        self.table = self.fresh_table()
        self.environment = {950: agent_marker(950, self.root)}
        self.rows = [self.owned('%20', 'orchestrator', '900'), self.owned('%21', 'alpha', '901'),
                     self.owned('%22', 'beta', '902'), self.agent('%30', '950', '950', 'added')]
        self.mutations = []
        self.queries = []
        self.result = ''
        self.chain_failure = None
        self.live_panes = []
        self.live_query_failure = None
        self.keep_processes = False
        self.signals = []
        for patcher in (patch('subprocess.run', side_effect=self.respond),
                        patch.object(control, 'send_signals', side_effect=self.record_signals),
                        patch.object(control, 'read_process_table', side_effect=lambda: dict(self.table)),
                        patch.object(control, 'read_environment',
                                     side_effect=lambda pid: self.environment.get(pid, {})),
                        patch.multiple(control, HANGUP_GRACE=0, TERM_GRACE=0, KILL_GRACE=0)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def fresh_table(self):
        table = {pid: proc(self.SERVER) for pid in (*self.TEAM_PANES.values(), 950, 960)}
        table[951] = proc(950)
        table[self.SERVER] = proc(1)
        return table

    def base_fields(self, pane, role, pid, token, marker, dead='0'):
        return ['$3', pane, dead, role, 'alpha,beta', token, marker, str(self.SERVER), '500', pid,
                pid if role else '']

    def owned(self, pane, role, pid):
        return '|'.join(self.base_fields(pane, role, pid, self.token, self.token) + ['0', '', role])

    def agent(self, pane, pid, agent_pid, window, *, root_match='1', dead='0'):
        fields = self.base_fields(pane, '', pid, self.token, '', dead)
        return '|'.join(fields + [root_match, agent_pid, window])

    def respond(self, argv, **kwargs):
        if argv[1] == 'list-panes' and '-a' in argv:
            self.queries.append(argv)
            if self.live_query_failure:
                raise self.live_query_failure
            return subprocess.CompletedProcess(argv, 0, ''.join(pid + '\n' for pid in self.live_panes), '')
        if argv[1] == 'list-panes':
            return subprocess.CompletedProcess(argv, 0, '\n'.join(self.rows), '')
        if argv[1] == 'has-session':
            return subprocess.CompletedProcess(argv, 1, '', '')
        self.mutations.append(argv)
        if self.chain_failure:
            raise self.chain_failure
        if 'if-shell' in argv and not (self.result or self.keep_processes):
            self.table = {}
        return subprocess.CompletedProcess(argv, 0, self.result, '')

    def record_signals(self, targets, signum):
        for pid in targets:
            self.signals.append((pid, signum))
            if not self.keep_processes:
                self.table.pop(pid, None)

    def signalled(self, signum=signal.SIGTERM):
        return {pid for pid, number in self.signals if number == signum}

    def kills(self):
        return [arg for call in self.mutations for arg in call if arg.startswith('kill-')]

    def add_process(self, pid, ppid, **marker_of):
        self.table[pid] = proc(ppid)
        if marker_of:
            self.environment[pid] = agent_marker(marker_of['agent'], self.root)

    def stop_expecting(self, message, role=None):
        state = control.managed_team(self.root)
        with self.assertRaises(ValueError) as raised:
            control.stop_managed(self.root, state, role)
        self.assertEqual(str(raised.exception), message)
        return raised.exception

    def test_agent_pane_is_reported_with_its_window_label(self):
        state = control.managed_team(self.root)
        self.assertEqual(state['agents'], {'%30': '$3|700|500|950|950'})
        self.assertEqual(state['agent_windows'], {'%30': 'added'})
        self.assertFalse(state['foreign'])
        self.assertEqual(state['panes'], {'orchestrator': '%20', 'alpha': '%21', 'beta': '%22'})

    def test_agent_marker_on_a_descendant_of_the_pane_process_is_accepted(self):
        self.rows[3] = self.agent('%30', '950', '951', 'added')
        self.assertEqual(control.managed_team(self.root)['agents'], {'%30': '$3|700|500|950|951'})

    def test_window_label_is_printable_bounded_and_may_contain_the_field_separator(self):
        self.rows[3] = self.agent('%30', '950', '950', 'a|b\x1b[31m' + 'x' * 60)
        label = control.managed_team(self.root)['agent_windows']['%30']
        self.assertEqual(label, 'a|b[31m' + 'x' * 33)

    def test_owned_only_team_has_no_agents_or_windows(self):
        del self.rows[3]
        state = control.managed_team(self.root)
        self.assertEqual((state['agents'], state['agent_windows']), ({}, {}))
        self.assertFalse(state['foreign'])

    def test_unproven_agent_panes_stay_foreign_and_are_not_killed(self):
        legacy = '|'.join(self.base_fields('%30', '', '950', self.token, ''))
        cases = {'other root': self.agent('%30', '950', '950', 'w', root_match='0'),
                 'stale agent pid': self.agent('%30', '950', '999', 'w'),
                 'unrelated live pid': self.agent('%30', '950', '960', 'w'),
                 'non-numeric pid': self.agent('%30', '950', 'x', 'w'),
                 'dead pane': self.agent('%30', '950', '950', 'w', dead='1'),
                 'legacy 11-field row': legacy}
        for name, row in cases.items():
            with self.subTest(name):
                self.rows[3] = row
                self.mutations.clear()
                state = control.managed_team(self.root)
                self.assertTrue(state['foreign'])
                self.assertEqual((state['agents'], state['agent_windows']), ({}, {}))
                control.stop_managed(self.root, state)
                self.assertNotIn('kill-pane -t %30', self.kills())
                self.assertEqual(len(self.kills()), 3)
                self.table = self.fresh_table()

    def test_team_stop_kills_agent_pane_in_the_verified_chain_with_owned_panes(self):
        state = control.managed_team(self.root)
        control.stop_managed(self.root, state)
        command, = self.mutations
        self.assertEqual(self.kills(), ['kill-pane -t %30', 'kill-pane -t %21', 'kill-pane -t %22',
                                        'kill-pane -t %20'])
        self.assertEqual(command.count('if-shell'), 4)
        index = command.index('kill-pane -t %30')
        self.assertEqual(command[index - 5:index - 1], ['if-shell', '-F', '-t', '%30'])
        self.assertEqual(command[index + 1], 'display-message -p changed')
        condition = command[index - 1]
        self.assertIn('#{==:#{@mailbox_root},' + control.tmux_literal(str(self.root)) + '}', condition)
        self.assertIn(control.AGENT_IDENTITY + ',$3|700|500|950|950}', condition)

    def test_kill_requests_name_the_agent_panes_to_kill_explicitly(self):
        current = control.managed_team(self.root)
        with_agents = control.kill_requests(self.root, current, ['alpha'], current['agents'])
        without_agents = control.kill_requests(self.root, current, ['alpha'], {})
        self.assertEqual([request[3] for request in with_agents], ['%30', '%21'])
        self.assertEqual([request[3] for request in without_agents], ['%21'])

    def test_literal_root_is_escaped_for_tmux_formats(self):
        self.assertEqual(control.tmux_literal('a,b#c}d'), 'a#,b##c#}d')
        self.assertTrue(control.pane_format(Path('/m,x')).endswith(
            '|#{==:#{@mailbox_root},/m#,x}|#{@mailbox_agent_pid}|#{window_name}'))

    def test_window_rename_does_not_trigger_the_team_changed_error(self):
        state = control.managed_team(self.root)
        self.rows[3] = self.agent('%30', '950', '950', 'renamed')
        control.stop_managed(self.root, state)
        self.assertIn('kill-pane -t %30', self.kills())

    def test_changed_agent_identity_between_confirm_and_stop_is_refused(self):
        state = control.managed_team(self.root)
        self.rows[3] = self.agent('%30', '950', '951', 'added')
        with self.assertRaises(ValueError) as raised:
            control.stop_managed(self.root, state)
        self.assertEqual(str(raised.exception), 'The team changed. Refresh the participant list.')
        self.assertEqual(self.mutations, [])

    def test_single_worker_stop_ignores_agent_changes_that_a_team_stop_refuses(self):
        state = control.managed_team(self.root)
        self.rows[3] = self.agent('%30', '950', '951', 'added')
        with self.assertRaises(ValueError):
            control.stop_managed(self.root, state)
        self.assertEqual(self.mutations, [])
        control.stop_managed(self.root, state, 'beta')
        self.assertEqual(self.kills(), ['kill-pane -t %22'])

    def test_single_worker_stop_succeeds_when_an_agent_pane_disappeared(self):
        state = control.managed_team(self.root)
        del self.rows[3]
        control.stop_managed(self.root, state, 'beta')
        self.assertEqual(self.kills(), ['kill-pane -t %22'])

    def test_reconnect_only_team_refuses_a_stop_without_touching_tmux(self):
        self.rows[0] = '|'.join([*self.base_fields('%20', 'orchestrator', '900', self.token, self.token)[:10], '',
                                 '0', '', 'orchestrator'])
        state = control.managed_team(self.root)
        self.assertFalse(state['can_stop'])
        self.stop_expecting('This team only supports reconnecting. Use a participant\'s terminal to stop it.')
        self.assertEqual(self.mutations, [])

    def test_tmux_reporting_changed_raises_the_fixed_error(self):
        self.result = 'changed'
        self.stop_expecting(CHANGED)

    def test_single_worker_stop_builds_no_process_plan_and_leaves_agents(self):
        state = control.managed_team(self.root)
        with patch.object(control.TeamProcesses, 'below_panes') as plan:
            control.stop_managed(self.root, state, 'beta')
        plan.assert_not_called()
        self.assertEqual(self.kills(), ['kill-pane -t %22'])

    def test_survivors_after_closing_panes_raise_the_fixed_text(self):
        self.keep_processes = True
        self.stop_expecting(SURVIVORS)
        everything = {900, 901, 902, 950, 951}
        self.assertEqual(self.signalled(signal.SIGTERM), everything)
        self.assertEqual(self.signalled(signal.SIGKILL), everything)

    def test_processes_that_exit_with_their_panes_receive_no_signal(self):
        control.stop_managed(self.root, control.managed_team(self.root))
        self.assertEqual(self.signals, [])

    def test_single_worker_stop_sends_no_signal(self):
        control.stop_managed(self.root, control.managed_team(self.root), 'beta')
        self.assertEqual(self.signals, [])

    def test_closed_panes_release_the_session_name_before_their_processes_are_terminated(self):
        self.keep_processes = True
        events = []
        with (patch.object(control, 'release_surviving_session', side_effect=lambda team: events.append('release')),
              patch.object(control, 'send_signals', side_effect=lambda *_: events.append('signal'))):
            self.stop_expecting(SURVIVORS)
        self.assertEqual(events[0], 'release')
        self.assertIn('signal', events)

    def fail_table_from_call(self, number):
        calls = []

        def read():
            calls.append(number)
            if len(calls) >= number:
                raise control.ProcessTableError(SURVIVORS)
            return dict(self.table)
        return patch.object(control, 'read_process_table', side_effect=read)

    def test_unreadable_table_before_the_chain_still_closes_the_panes_and_shows_only_the_unchecked_text(self):
        del self.rows[3]
        with self.fail_table_from_call(1), patch.object(control, 'release_surviving_session') as release:
            error = self.stop_expecting(UNCHECKED)
        self.assertEqual(len(self.mutations), 1)
        self.assertEqual(self.kills(), ['kill-pane -t %21', 'kill-pane -t %22', 'kill-pane -t %20'])
        release.assert_called_once_with(control.managed_team(self.root))
        self.assertEqual(self.signals, [])
        self.assertNotIsInstance(error, control.ProcessTableError)

    def test_unreadable_table_in_a_later_read_gives_the_fixed_text(self):
        del self.rows[3]
        self.keep_processes = True
        for number in (2, 3, 7):  # exit check, first rescan, final check
            with self.subTest(failing_read=number), self.fail_table_from_call(number):
                error = self.stop_expecting(SURVIVORS)
            self.assertIsInstance(error, control.ProcessTableError)

    def test_unreadable_table_leaves_an_agent_candidate_foreign(self):
        with self.fail_table_from_call(1):
            state = control.managed_team(self.root)
        self.assertTrue(state['foreign'])
        self.assertEqual((state['agents'], state['agent_windows']), ({}, {}))

    def test_failed_session_release_still_terminates_processes_and_reports_the_release_error(self):
        state = control.managed_team(self.root)
        failure = ValueError('The team changed while releasing its tmux session name. Check the open sessions.')
        with (patch.object(control, 'release_surviving_session', side_effect=failure),
              patch.object(control.TeamProcesses, 'terminate') as terminate,
              self.assertRaises(ValueError) as raised):
            control.stop_managed(self.root, state)
        self.assertIs(raised.exception, failure)
        terminate.assert_called_once_with()

    def test_changed_chain_terminates_only_panes_that_tmux_confirms_closed(self):
        self.add_process(911, 901)
        self.add_process(971, 1, agent=950)  # detached child of the closed agent pane
        self.result = 'changed'
        self.live_panes = ['901']
        with patch.object(control, 'release_surviving_session') as release:
            self.stop_expecting(CHANGED)
        release.assert_not_called()
        self.assertEqual(self.signalled(signal.SIGTERM), {900, 902, 950, 951, 971})
        self.assertEqual(self.queries, [['tmux', 'list-panes', '-a', '-F', '#{pane_pid}']])
        self.assertTrue({901, 911} <= set(self.table))

    def test_refused_agent_pane_keeps_its_tree_and_every_process_carrying_its_marker(self):
        self.environment[951] = agent_marker(950, self.root)  # child below the refused pane
        self.add_process(970, 1, agent=950)  # detached, marked by the refused agent
        self.result = 'changed'
        self.live_panes = ['950']
        self.stop_expecting(CHANGED)
        self.assertEqual(self.signalled(signal.SIGTERM), {900, 901, 902})
        self.assertEqual(self.signalled(signal.SIGKILL), set())
        self.assertTrue({950, 951, 970} <= set(self.table))

    def stop_after_failed_chain(self, failure):
        self.chain_failure = failure
        self.live_panes = ['901']
        with patch.object(control, 'release_surviving_session') as release:
            error = self.stop_expecting(UNFINISHED)
        release.assert_not_called()
        self.assertEqual(self.signalled(signal.SIGTERM), {900, 902, 950, 951})
        self.assertIsNone(error.__cause__)
        self.assertNotIn('private', str(error))

    def test_failed_kill_chain_runs_the_restricted_termination_and_reports_the_fixed_text(self):
        self.stop_after_failed_chain(subprocess.CalledProcessError(1, ['tmux'], stderr=RAW_TMUX_ERROR))

    def test_timed_out_kill_chain_runs_the_restricted_termination_and_reports_the_fixed_text(self):
        self.stop_after_failed_chain(subprocess.TimeoutExpired(['tmux'], 5))

    def test_unlaunchable_kill_chain_shows_only_the_fixed_text_and_signals_nothing(self):
        self.chain_failure = OSError(errno.EIO, RAW_TMUX_ERROR)
        error = self.stop_expecting(UNFINISHED)
        self.assertIsNone(error.__cause__)
        self.assertEqual(self.signals, [])

    def test_failed_live_pane_query_signals_nothing_and_asks_for_a_manual_check(self):
        failures = (subprocess.CalledProcessError(1, ['tmux'], stderr=RAW_TMUX_ERROR),
                    subprocess.TimeoutExpired(['tmux'], 2), OSError(errno.EIO, RAW_TMUX_ERROR))
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                self.result = 'changed'
                self.live_query_failure = failure
                self.signals.clear()
                self.stop_expecting(UNCHECKED)
                self.assertEqual(self.signals, [])

    def test_unparsable_live_pane_list_signals_nothing_and_asks_for_a_manual_check(self):
        self.result = 'changed'
        self.live_panes = ['not-a-pid']
        self.stop_expecting(UNCHECKED)
        self.assertEqual(self.signals, [])

    def test_survivors_win_over_the_changed_text(self):
        self.result = 'changed'
        self.keep_processes = True
        self.stop_expecting(SURVIVORS)
        self.assertTrue(self.signalled(signal.SIGKILL))

    def test_unchecked_text_wins_over_the_changed_text_when_the_plan_was_unreadable(self):
        del self.rows[3]
        self.result = 'changed'
        with self.fail_table_from_call(1):
            self.stop_expecting(UNCHECKED)
        self.assertEqual(self.signals, [])

    def test_unchecked_text_wins_over_the_failed_chain_text(self):
        del self.rows[3]
        self.chain_failure = subprocess.TimeoutExpired(['tmux'], 5)
        with self.fail_table_from_call(1):
            self.stop_expecting(UNCHECKED)
        self.assertEqual(self.signals, [])


class TeamProcessesTests(unittest.TestCase):
    SERVER = 100

    def setUp(self):
        self.me = 250
        getpid = patch.object(control.os, 'getpid', return_value=self.me)
        getpid.start()
        self.addCleanup(getpid.stop)
        self.table = {1: proc(0, uid=0), self.SERVER: proc(1),
                      200: proc(self.SERVER), 201: proc(200), 202: proc(201),
                      210: proc(self.SERVER), 211: proc(210), 220: proc(self.SERVER), self.me: proc(220)}
        self.environment = {200: agent_marker(200, '/mail')}
        self.signals = []
        self.ignoring_term = set()
        for patcher in (patch.object(control, 'read_process_table', side_effect=lambda: dict(self.table)),
                        patch.object(control, 'read_environment',
                                     side_effect=lambda pid: self.environment.get(pid, {})),
                        patch.object(control, 'send_signals', side_effect=self.deliver),
                        patch.multiple(control, HANGUP_GRACE=0, TERM_GRACE=0, KILL_GRACE=0)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def deliver(self, targets, signum):
        for pid in targets:
            self.signals.append((pid, signum))
            if signum == signal.SIGKILL or pid not in self.ignoring_term:
                self.table.pop(pid, None)

    def signalled(self, signum):
        return {pid for pid, number in self.signals if number == signum}

    def plan(self, panes=(200,)):
        return control.TeamProcesses('/mail', self.SERVER, set(panes), dict(self.table))

    def test_term_then_kill_of_survivors_in_every_descendant_generation(self):
        self.ignoring_term = {202}
        self.plan().terminate()
        self.assertEqual(self.signalled(signal.SIGTERM), {200, 201, 202})
        self.assertEqual(self.signalled(signal.SIGKILL), {202})
        self.assertLess(self.signals.index((201, signal.SIGTERM)), self.signals.index((202, signal.SIGKILL)))

    def test_hangup_grace_that_ends_the_processes_sends_no_signal(self):
        plan = self.plan()
        for pid in (200, 201, 202):
            del self.table[pid]
        plan.terminate()
        self.assertEqual(self.signals, [])

    def test_detached_child_is_found_by_environment_marker_and_root(self):
        agent = agent_marker(200, '/mail')
        other_root = agent_marker(200, '/elsewhere')
        self.table.update({300: proc(1), 301: proc(1), 302: proc(1, uid=UID + 1), 303: proc(1, zombie=True),
                           304: proc(1), 400: proc(self.SERVER), 401: proc(400)})
        self.environment = {200: agent, 300: agent, 301: other_root, 302: agent, 303: agent,
                            304: agent_marker(999, '/mail'),
                            400: agent, 401: agent, self.me: agent, 220: agent, self.SERVER: agent}
        self.plan().terminate()
        self.assertEqual(self.signalled(signal.SIGTERM), {200, 201, 202, 300})
        self.assertEqual(self.signals, [(pid, signal.SIGTERM) for pid, _ in self.signals])

    def test_detached_process_is_matched_only_by_the_marker_of_a_real_agent_process(self):
        self.table.update({300: proc(1), 301: proc(1)})
        self.environment = {200: agent_marker(200, '/mail'),
                            201: agent_marker(200, '/mail'),  # a nested start keeps the outer agent's marker
                            300: agent_marker(201, '/mail'),
                            301: agent_marker(200, '/mail')}
        self.plan().terminate()
        self.assertEqual(self.signalled(signal.SIGTERM), {200, 201, 202, 301})

    def test_own_process_and_its_ancestors_are_never_signalled_below_a_pane(self):
        self.table[self.me] = proc(201)
        self.table[202] = proc(self.me)
        self.plan().terminate()
        self.assertEqual(self.signalled(signal.SIGTERM), {202})
        self.assertFalse({self.me, 200, 201, self.SERVER, 1} & {pid for pid, _ in self.signals})

    def test_tmux_server_and_other_panes_are_not_signalled(self):
        self.plan().terminate()
        self.assertFalse({self.SERVER, 210, 211, 220, 1} & {pid for pid, _ in self.signals})

    def test_other_user_and_zombie_descendants_are_skipped_and_do_not_count_as_survivors(self):
        self.table.update({203: proc(200, uid=UID + 1), 204: proc(200, zombie=True)})
        self.plan().terminate()
        self.assertEqual(self.signalled(signal.SIGTERM), {200, 201, 202})
        self.assertIn(203, self.table)

    def test_reused_pid_with_another_start_time_is_not_signalled(self):
        plan = self.plan()
        self.table[201] = proc(1, start='reused')
        plan.terminate()
        self.assertNotIn(201, self.signalled(signal.SIGTERM))
        self.assertIn(201, self.table)
        self.assertEqual(self.signalled(signal.SIGTERM), {200, 202})

    def test_survivors_raise_exactly_the_fixed_text(self):
        self.ignoring_term = {202}
        with patch.object(control, 'send_signals'), self.assertRaises(ValueError) as raised:
            self.plan().terminate()
        self.assertEqual(str(raised.exception), SURVIVORS)

    def test_permission_denied_signal_leaves_a_survivor(self):
        with (patch.object(control, 'send_signals', REAL_SEND_SIGNALS),
              patch.object(control.os, 'pidfd_open', create=True, side_effect=PermissionError),
              patch.object(control.os, 'kill', side_effect=PermissionError) as kill,
              self.assertRaises(ValueError) as raised):
            self.plan().terminate()
        self.assertEqual(str(raised.exception), SURVIVORS)
        self.assertIn((200, signal.SIGTERM), [call.args for call in kill.call_args_list])

    def test_pane_pid_that_is_not_a_child_of_the_server_is_not_a_root(self):
        self.table.update({500: proc(1), 501: proc(500)})
        token = 'a' * 32
        current = {'identities': {'%1': '$3|100|9|200|' + token, '%2': '$3|100|9|500|' + token},
                   'agents': {'%30': '$3|100|9|999|999'}}
        plan = control.TeamProcesses.below_panes('/mail', current)
        self.assertEqual(set(plan.targets), {200, 201, 202})
        plan.terminate()
        self.assertFalse({500, 501, 999} & {pid for pid, _ in self.signals})

    def test_wait_until_polls_until_done_or_the_deadline(self):
        clock = iter([0, 0.2, 0.6, 1.2])
        sleeps = []
        with (patch.object(control.time, 'monotonic', side_effect=lambda: next(clock)),
              patch.object(control.time, 'sleep', side_effect=sleeps.append)):
            control.wait_until(lambda: False, 1)
        self.assertEqual(sleeps, [control.POLL_INTERVAL] * 2)
        with patch.object(control.time, 'sleep', side_effect=sleeps.append):
            control.wait_until(lambda: True, 5)
        self.assertEqual(len(sleeps), 2)

    def test_process_ancestry_terminates_on_cycles(self):
        self.assertEqual(control.ancestors({1: proc(2), 2: proc(1)}, 1), {1, 2})


class SendSignalsTests(unittest.TestCase):
    def spawn(self, code='import time; time.sleep(30)'):
        child = subprocess.Popen([sys.executable, '-c', code])
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        return child

    def start_of(self, child):
        return control.read_process_table()[child.pid].start

    def test_signal_reaches_a_child_whose_start_time_matches(self):
        child = self.spawn()
        control.send_signals({child.pid: self.start_of(child)}, signal.SIGTERM)
        self.assertEqual(child.wait(timeout=5), -signal.SIGTERM)

    def test_changed_start_time_sends_nothing(self):
        child = self.spawn()
        control.send_signals({child.pid: 'another start'}, signal.SIGTERM)
        with self.assertRaises(subprocess.TimeoutExpired):
            child.wait(timeout=0.3)

    def test_vanished_process_counts_as_stopped(self):
        child = subprocess.Popen([sys.executable, '-c', 'pass'])
        start = control.read_process_table()[child.pid].start
        child.wait()
        control.send_signals({child.pid: start}, signal.SIGTERM)

    def test_fallback_without_pidfd_uses_kill_after_the_same_start_check(self):
        child = self.spawn()
        start = self.start_of(child)
        with patch.object(control.os, 'pidfd_open', create=True):
            del control.os.pidfd_open
            control.send_signals({child.pid: 'another start'}, signal.SIGTERM)
            with self.assertRaises(subprocess.TimeoutExpired):
                child.wait(timeout=0.3)
            control.send_signals({child.pid: start}, signal.SIGTERM)
        self.assertEqual(child.wait(timeout=5), -signal.SIGTERM)


class SignalRoundTests(unittest.TestCase):
    """One round with fake handles: pidfds first, a single table read, then per-process delivery."""
    PIDS = (7, 8, 9)

    def setUp(self):
        self.events = []
        self.table = {pid: proc(1, start='s') for pid in self.PIDS}
        self.targets = {pid: 's' for pid in self.PIDS}
        self.open_failures = {}
        self.send_failures = {}
        self.kill_failures = {}
        self.sent = []
        self.killed = []
        self.closed = []
        self.read = MagicMock(side_effect=self.read_table)
        for patcher in (patch.object(control, 'read_process_table', self.read),
                        patch.object(control.os, 'pidfd_open', create=True, side_effect=self.open_handle),
                        patch.object(control.signal, 'pidfd_send_signal', create=True, side_effect=self.send),
                        patch.object(control.os, 'kill', side_effect=self.kill),
                        patch.object(control.os, 'close', side_effect=self.closed.append)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def read_table(self):
        self.events.append('read')
        return dict(self.table)

    def open_handle(self, pid):
        self.events.append(('open', pid))
        if pid in self.open_failures:
            raise self.open_failures[pid]
        return 1000 + pid

    def send(self, handle, signum):
        self.sent.append((handle, signum))
        if handle - 1000 in self.send_failures:
            raise self.send_failures[handle - 1000]

    def kill(self, pid, signum):
        self.killed.append((pid, signum))
        if pid in self.kill_failures:
            raise self.kill_failures[pid]

    def test_handles_are_opened_before_the_one_table_read_of_the_round(self):
        control.send_signals(self.targets, signal.SIGTERM)
        self.assertEqual(self.events, [('open', 7), ('open', 8), ('open', 9), 'read'])
        self.assertEqual(self.read.call_count, 1)
        self.assertEqual(self.sent, [(1007, signal.SIGTERM), (1008, signal.SIGTERM), (1009, signal.SIGTERM)])
        self.assertEqual(self.closed, [1007, 1008, 1009])

    def test_pidfd_exhaustion_falls_back_to_kill_only_when_start_time_and_owner_match(self):
        cases = {'match': ({7: proc(1, start='s')}, [(7, signal.SIGTERM)]),
                 'changed start time': ({7: proc(1, start='reused')}, []),
                 'other owner': ({7: proc(1, uid=UID + 1, start='s')}, []),
                 'gone from the table': ({}, [])}
        for name, (table, expected) in cases.items():
            with self.subTest(name):
                self.open_failures = {7: OSError(errno.EMFILE, 'Too many open files')}
                self.table = table
                self.killed.clear()
                control.send_signals({7: 's'}, signal.SIGTERM)
                self.assertEqual(self.killed, expected)
                self.assertEqual(self.sent, [])

    def test_process_that_vanished_before_its_handle_opened_is_not_signalled(self):
        self.open_failures = {7: ProcessLookupError(errno.ESRCH, 'No such process')}
        control.send_signals(self.targets, signal.SIGTERM)
        self.assertEqual([handle for handle, _ in self.sent], [1008, 1009])
        self.assertEqual(self.killed, [])

    def test_failed_delivery_to_one_process_does_not_stop_the_round(self):
        self.send_failures = {7: OSError(errno.EIO, 'Input/output error'), 8: PermissionError()}
        control.send_signals(self.targets, signal.SIGTERM)
        self.assertEqual([handle for handle, _ in self.sent], [1007, 1008, 1009])
        self.assertEqual(self.closed, [1007, 1008, 1009])

    def test_failed_kill_fallback_for_one_process_does_not_stop_the_round(self):
        self.open_failures = {pid: OSError(errno.EMFILE, 'Too many open files') for pid in self.PIDS}
        self.kill_failures = {7: PermissionError(), 8: OSError(errno.EPERM, 'denied')}
        control.send_signals(self.targets, signal.SIGKILL)
        self.assertEqual(self.killed, [(7, signal.SIGKILL), (8, signal.SIGKILL), (9, signal.SIGKILL)])
        self.assertEqual(self.closed, [])

    def test_unreadable_table_ends_the_round_without_a_signal_and_closes_every_handle(self):
        self.read.side_effect = control.ProcessTableError(SURVIVORS)
        control.send_signals(self.targets, signal.SIGTERM)
        self.assertEqual((self.sent, self.killed), ([], []))
        self.assertEqual(self.closed, [1007, 1008, 1009])

    def test_every_handle_is_closed_when_the_table_read_fails_unexpectedly(self):
        self.read.side_effect = RuntimeError('boom')
        with self.assertRaises(RuntimeError):
            control.send_signals(self.targets, signal.SIGTERM)
        self.assertEqual(self.closed, [1007, 1008, 1009])


class ProcessTableTests(unittest.TestCase):
    def test_linux_table_reports_parent_owner_start_and_zombie_state(self):
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        entry = control.read_process_table()[child.pid]
        self.assertEqual((entry.ppid, entry.uid, entry.zombie), (os.getpid(), UID, False))
        self.assertTrue(entry.start.isdigit())

    def test_command_names_with_spaces_and_parentheses_do_not_shift_the_fields(self):
        code = "import time; open('/proc/self/comm', 'w').write('we ) ird (x'); print(1, flush=True); time.sleep(30)"
        child = subprocess.Popen([sys.executable, '-c', code], stdout=subprocess.PIPE)
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        self.addCleanup(child.stdout.close)
        child.stdout.readline()
        self.assertEqual(control.read_process_table()[child.pid].ppid, os.getpid())

    def test_terminated_but_unreaped_child_is_a_zombie(self):
        child = subprocess.Popen([sys.executable, '-c', 'pass'])
        self.addCleanup(child.wait)
        deadline = time.monotonic() + 5
        while not control.read_process_table()[child.pid].zombie and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(control.read_process_table()[child.pid].zombie)

    def test_unreadable_or_malformed_linux_entries_are_skipped(self):
        listing = [SimpleNamespace(name=name, stat=lambda: SimpleNamespace(st_uid=UID)) for name in ('abc', '12')]
        with patch.object(control.os, 'scandir', return_value=listing):
            for opener in (patch('builtins.open', side_effect=OSError),
                           patch('builtins.open', mock_open(read_data='short')),
                           patch('builtins.open', mock_open(read_data='1 (x) S 0'))):
                with self.subTest(opener=opener), opener:
                    self.assertEqual(control.read_process_table(), {})

    def test_unreadable_proc_directory_raises_the_fixed_process_error(self):
        with patch.object(control.os, 'scandir', side_effect=PermissionError), \
                self.assertRaises(control.ProcessTableError) as raised:
            control.read_process_table()
        self.assertEqual(str(raised.exception), SURVIVORS)

    def test_ps_fallback_parses_rows_including_multiword_start_times(self):
        output = ('    1     0     0 Ss   Mon Nov 24 10:00:00 2026\n'
                  ' 4242     1  1000 Z+   Mon Nov 24 10:05:09 2026\n')
        with (patch.object(control.os.path, 'isdir', return_value=False),
              patch.object(control.subprocess, 'run',
                           return_value=subprocess.CompletedProcess([], 0, output, '')) as ps):
            table = control.read_process_table()
        self.assertEqual(table, {1: Process(0, 0, 'Mon Nov 24 10:00:00 2026', False),
                                 4242: Process(1, 1000, 'Mon Nov 24 10:05:09 2026', True)})
        self.assertEqual(ps.call_args.args[0][0], 'ps')

    def test_ps_fallback_failures_raise_the_fixed_process_error(self):
        failures = (subprocess.TimeoutExpired('ps', 5), subprocess.CalledProcessError(1, 'ps'),
                    FileNotFoundError(), subprocess.CompletedProcess([], 0, 'garbage\n', ''))
        with patch.object(control.os.path, 'isdir', return_value=False):
            for failure in failures:
                outcome = ({'return_value': failure} if isinstance(failure, subprocess.CompletedProcess)
                           else {'side_effect': failure})
                with self.subTest(failure=failure), patch.object(control.subprocess, 'run', **outcome), \
                        self.assertRaises(control.ProcessTableError) as raised:
                    control.read_process_table()
                self.assertEqual(str(raised.exception), SURVIVORS)

    def test_environment_keeps_values_with_equals_and_skips_entries_without_one(self):
        with patch('builtins.open', mock_open(read_data=b'A=1\0junk\0B=x=y\0')):
            self.assertEqual(control.read_environment(5), {'A': '1', 'B': 'x=y'})

    def test_unreadable_environment_is_empty(self):
        with patch('builtins.open', side_effect=PermissionError):
            self.assertEqual(control.read_environment(5), {})


class MarkAgentPaneTests(unittest.TestCase):
    OWN_PID, PARENT_PID = 500, 400
    ANCESTRY = {500: 400, 400: 300, 300: 1}

    @classmethod
    def setUpClass(cls):
        cls.start = runpy.run_path(str(PLUGIN / 'start'))
        cls.mark = staticmethod(cls.start['mark_agent_pane'])

    def setUp(self):
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        for name in ('TMUX_PANE', 'HARNESS_MAILBOX_AGENT_PID'):
            os.environ.pop(name, None)
        self.ancestry = dict(self.ANCESTRY)
        self.pane_pid = str(self.OWN_PID)
        self.display_failure = None
        self.set_option_failure = None
        self.calls = []
        for patcher in (patch('subprocess.run', side_effect=self.tmux),
                        patch.object(os, 'getpid', return_value=self.OWN_PID),
                        patch.object(os, 'getppid', return_value=self.PARENT_PID),
                        patch.dict(self.mark.__globals__, parent_pid=lambda pid: self.ancestry.get(pid))):
            patcher.start()
            self.addCleanup(patcher.stop)

    def tmux(self, argv, **kwargs):
        self.calls.append(argv)
        failure = self.display_failure if argv[1] == 'display-message' else self.set_option_failure
        if failure:
            raise failure
        return subprocess.CompletedProcess(argv, 0, self.pane_pid + '\n' if argv[1] == 'display-message' else '', '')

    def set_options(self):
        return [call for call in self.calls if call[1] == 'set-option']

    def test_agent_pid_is_exported_even_outside_tmux(self):
        self.mark('/mail')
        self.assertEqual(os.environ['HARNESS_MAILBOX_AGENT_PID'], '500')
        self.assertEqual(self.calls, [])

    def test_only_a_percent_pane_id_reaches_tmux(self):
        for pane in ('', 'main', '%', '%1x', '%-1', ' %1', '%1\n'):
            with self.subTest(pane=pane):
                os.environ['TMUX_PANE'] = pane
                self.mark('/mail')
                self.assertEqual(self.calls, [])

    def test_marker_of_a_live_ancestor_leaves_the_environment_and_the_pane_alone(self):
        os.environ['TMUX_PANE'] = '%17'
        for inherited in ('400', '300', '1'):
            with self.subTest(inherited=inherited):
                os.environ['HARNESS_MAILBOX_AGENT_PID'] = inherited
                self.mark('/mail')
                self.assertEqual(os.environ['HARNESS_MAILBOX_AGENT_PID'], inherited)
                self.assertEqual(self.calls, [])

    def test_stale_or_unusable_inherited_marker_is_replaced_and_the_pane_marked(self):
        os.environ['TMUX_PANE'] = '%17'
        for inherited in ('999', '500', 'abc', '', '-1'):
            with self.subTest(inherited=inherited):
                os.environ['HARNESS_MAILBOX_AGENT_PID'] = inherited
                self.calls.clear()
                self.mark(Path('/mail'))
                self.assertEqual(os.environ['HARNESS_MAILBOX_AGENT_PID'], '500')
                self.assertEqual(self.calls[0], ['tmux', 'display-message', '-p', '-t', '%17', '#{pane_pid}'])
                self.assertEqual(self.set_options(),
                                 [['tmux', 'set-option', '-p', '-t', '%17', '@mailbox_root', '/mail', ';',
                                   'set-option', '-p', '-t', '%17', '@mailbox_agent_pid', '500']])

    def test_unreadable_ancestry_or_a_parent_cycle_is_not_nested(self):
        os.environ['TMUX_PANE'] = '%17'
        for name, ancestry in (('unreadable parent', {}), ('cycle', {500: 400, 400: 500})):
            with self.subTest(name):
                os.environ['HARNESS_MAILBOX_AGENT_PID'] = '300'
                self.ancestry = ancestry
                self.mark('/mail')
                self.assertEqual(os.environ['HARNESS_MAILBOX_AGENT_PID'], '500')

    def test_pane_whose_process_is_the_parent_is_marked(self):
        os.environ['TMUX_PANE'] = '%17'
        self.pane_pid = '400'
        self.mark('/mail')
        self.assertEqual(len(self.set_options()), 1)

    def test_pane_of_another_process_is_not_marked_but_the_pid_is_still_exported(self):
        os.environ['TMUX_PANE'] = '%17'
        for output in ('777', 'garbage', ''):
            with self.subTest(output=output):
                self.pane_pid = output
                self.calls.clear()
                self.mark('/mail')
                self.assertEqual(os.environ['HARNESS_MAILBOX_AGENT_PID'], '500')
                self.assertEqual(self.set_options(), [])

    def test_tmux_failures_are_ignored_silently(self):
        os.environ['TMUX_PANE'] = '%17'
        failures = (FileNotFoundError(), subprocess.TimeoutExpired('tmux', 2),
                    subprocess.CalledProcessError(1, 'tmux', stderr=RAW_TMUX_ERROR))
        for failure in failures:
            for attribute in ('display_failure', 'set_option_failure'):
                with self.subTest(failure=type(failure).__name__, step=attribute), \
                        patch('sys.stdout') as out, patch('sys.stderr') as err:
                    setattr(self, attribute, failure)
                    self.mark('/mail')
                    setattr(self, attribute, None)
                    out.write.assert_not_called()
                    err.write.assert_not_called()


class ParentPidTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.parent_pid = staticmethod(runpy.run_path(str(PLUGIN / 'start'))['parent_pid'])

    def test_parent_is_read_from_proc_even_when_the_command_name_has_parentheses(self):
        with patch('builtins.open', mock_open(read_data='5 (we ) ird) S 42 5 5 0')):
            self.assertEqual(self.parent_pid(5), 42)

    def test_unreadable_or_malformed_proc_entry_falls_back_to_ps(self):
        for opener in (patch('builtins.open', side_effect=OSError), patch('builtins.open', mock_open(read_data='x'))):
            with self.subTest(opener=opener), opener, \
                    patch('subprocess.run', return_value=subprocess.CompletedProcess([], 0, '  77\n', '')) as ps:
                self.assertEqual(self.parent_pid(5), 77)
                self.assertEqual(ps.call_args.args[0], ['ps', '-o', 'ppid=', '-p', '5'])

    def test_unreadable_parent_is_none(self):
        failures = (FileNotFoundError(), subprocess.TimeoutExpired('ps', 2), subprocess.CalledProcessError(1, 'ps'))
        outputs = [subprocess.CompletedProcess([], 0, 'junk', '')]
        with patch('builtins.open', side_effect=OSError):
            for failure in (*failures, *outputs):
                outcome = ({'return_value': failure} if isinstance(failure, subprocess.CompletedProcess)
                           else {'side_effect': failure})
                with self.subTest(failure=failure), patch('subprocess.run', **outcome):
                    self.assertIsNone(self.parent_pid(5))


if __name__ == '__main__':
    unittest.main()
