import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import agent_activity


def at(hour, minute=0):
    return datetime.fromisoformat(f'2026-11-22T{hour:02d}:{minute:02d}:00+00:00')


def ts(hour, minute=0):
    return at(hour, minute).timestamp()


class AgentActivityTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.transcripts = self.root / 'transcripts'
        (self.root / 'projects.json').write_text(json.dumps({'projects': [
            {'id': 'proj', 'working_directory': '/work/proj.app', 'repository_path': '/work/proj.app'}]}))
        self.folder = agent_activity.transcript_folder(self.transcripts, '/work/proj.app')

    def write(self, relative, *entries):
        path = self.folder / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'a') as transcript:
            for entry in entries:
                transcript.write((entry if isinstance(entry, str) else json.dumps(entry)) + '\n')

    @staticmethod
    def message(hour, minute=0, kind='assistant', tool=False):
        content = [{'type': 'tool_use'}] if tool else [{'type': 'text'}]
        return {'type': kind, 'timestamp': at(hour, minute).isoformat(), 'message': {'content': content}}

    def activity(self):
        return agent_activity.project_activity(self.root, 'proj', self.transcripts)

    def test_folder_name_replaces_every_non_alphanumeric_character(self):
        self.assertEqual(self.transcripts / '-work-proj-app', self.folder)

    def test_close_messages_are_work_and_a_long_silence_is_waiting(self):
        self.write('s.jsonl', self.message(10), self.message(10, 4, 'user'), self.message(11))
        self.assertEqual([(ts(10), ts(10, 4))], self.activity())

    def test_a_running_tool_counts_up_to_the_tool_limit(self):
        self.write('s.jsonl', self.message(10, tool=True), self.message(10, 50, 'user'),
                   self.message(11, tool=True), self.message(12, 30, 'user'))
        self.assertEqual([(ts(10), ts(10, 50))], self.activity())

    def test_a_tool_that_asks_the_user_is_waiting_not_a_running_tool(self):
        question = self.message(10, tool=True)
        question['message']['content'] = [{'type': 'tool_use', 'name': 'AskUserQuestion'}]
        self.write('s.jsonl', question, self.message(10, 30, 'user'),
                   self.message(11, tool=True), self.message(11, 30, 'user'))
        self.assertEqual([(ts(11), ts(11, 30))], self.activity())

    def test_subagents_are_merged_so_parallel_work_counts_once(self):
        self.write('s.jsonl', self.message(10), self.message(10, 4))
        self.write('s/subagents/a.jsonl', self.message(10, 2), self.message(10, 6))
        self.assertEqual([(ts(10), ts(10, 6))], self.activity())

    def test_other_entry_types_and_broken_lines_are_ignored(self):
        self.write('s.jsonl', self.message(10), '{broken', {'type': 'queue-operation', 'timestamp': at(10, 2).isoformat()},
                   {'type': 'user', 'timestamp': 'not a time'}, self.message(10, 3))
        self.assertEqual([(ts(10), ts(10, 3))], self.activity())

    def test_a_project_without_transcripts_has_no_activity(self):
        self.assertEqual([], self.activity())
        self.assertEqual([], agent_activity.project_activity(self.root, 'unknown', self.transcripts))

    def write_lines(self, path, *entries):
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'a') as handle:
            for entry in entries:
                handle.write((entry if isinstance(entry, str) else json.dumps(entry)) + '\n')

    def copilot_session(self, name, cwd, *events):
        start = {'type': 'session.start', 'timestamp': at(9).isoformat(), 'data': {'context': {'cwd': cwd}}}
        self.write_lines(self.root / 'copilot' / name / 'events.jsonl', start, *events)

    @staticmethod
    def copilot_message(hour, minute, kind='assistant.message', tools=()):
        return {'type': kind, 'timestamp': at(hour, minute).isoformat(),
                'data': {'toolRequests': [{'name': name} for name in tools]}}

    def codex_rollout(self, name, cwd, *items):
        meta = {'type': 'session_meta', 'timestamp': at(9).isoformat(), 'payload': {'cwd': cwd}}
        self.write_lines(self.root / 'codex' / '2026' / '11' / '22' / f'rollout-{name}.jsonl', meta, *items)

    @staticmethod
    def codex_item(hour, minute, kind='message', name=None):
        return {'type': 'response_item', 'timestamp': at(hour, minute).isoformat(),
                'payload': {'type': kind, 'name': name}}

    def all_hosts(self):
        return agent_activity.Transcripts(self.transcripts, self.root / 'copilot', self.root / 'codex')

    def test_a_copilot_session_in_the_project_directory_is_work_and_a_long_silence_is_waiting(self):
        self.copilot_session('s1', '/work/proj.app', self.copilot_message(10, 0), self.copilot_message(10, 4, 'user.message'),
                             self.copilot_message(11, 0), {'type': 'hook.start', 'timestamp': at(11, 2).isoformat()})
        self.assertEqual([(ts(10), ts(10, 4))], agent_activity.project_activity(self.root, 'proj', self.all_hosts()))

    def test_a_copilot_tool_counts_up_to_the_tool_limit_but_asking_the_user_does_not(self):
        self.copilot_session('s1', '/work/proj.app', self.copilot_message(10, 0, tools=('bash',)),
                             self.copilot_message(10, 50), self.copilot_message(11, 0, tools=('ask_user',)),
                             self.copilot_message(11, 30))
        self.assertEqual([(ts(10), ts(10, 50))], agent_activity.project_activity(self.root, 'proj', self.all_hosts()))

    def test_a_codex_rollout_and_its_subagent_are_merged_and_other_projects_are_left_out(self):
        self.codex_rollout('a', '/work/proj.app', self.codex_item(10, 0, 'function_call', 'shell'),
                           self.codex_item(10, 40, 'function_call_output'), self.codex_item(12, 0))
        self.codex_rollout('b', '/work/proj.app/sub', self.codex_item(10, 38), self.codex_item(10, 43))
        self.codex_rollout('c', '/work/other', self.codex_item(10, 0), self.codex_item(10, 3))
        self.codex_rollout('d', '/work/proj.app', self.codex_item(14, 0, 'custom_tool_call', 'request_user_input'),
                           self.codex_item(14, 30, 'function_call_output'))
        self.assertEqual([(ts(10), ts(10, 43))], agent_activity.project_activity(self.root, 'proj', self.all_hosts()))

    def test_a_session_belongs_to_the_most_specific_registered_directory(self):
        (self.root / 'projects.json').write_text(json.dumps({'projects': [
            {'id': 'proj', 'working_directory': '/work/proj.app', 'repository_path': '/work/proj.app'},
            {'id': 'inner', 'working_directory': '/work/proj.app/inner', 'repository_path': '/work/proj.app/inner'}]}))
        self.codex_rollout('a', '/work/proj.app/inner/x', self.codex_item(10, 0), self.codex_item(10, 2))
        hosts = self.all_hosts()
        self.assertEqual([(ts(10), ts(10, 2))], agent_activity.project_activity(self.root, 'inner', hosts))
        self.assertEqual([], agent_activity.project_activity(self.root, 'proj', hosts))

    def test_a_bare_transcript_folder_reads_claude_code_only(self):
        self.copilot_session('s1', '/work/proj.app', self.copilot_message(10, 0), self.copilot_message(10, 2))
        self.assertEqual([], self.activity())

    def test_shared_seconds_clip_to_the_window_and_divide_overlaps(self):
        activity = [(ts(9), ts(12))]
        own = (ts(10), ts(13))
        self.assertEqual(1.5 * 3600, agent_activity.shared_seconds(activity, own, [own, (ts(11), ts(14))]))

    def test_active_seconds_are_reported_for_each_project_of_the_issue_tasks(self):
        (self.root / 'tasks' / 'proj').mkdir(parents=True)
        (self.root / 'tasks' / 'proj' / 't-1.json').write_text('{}')
        self.write('s.jsonl', self.message(10), self.message(10, 5))
        window = (ts(9), ts(12))
        seconds = agent_activity.active_seconds_by_project(self.root, {'t-1', 't-missing'}, window,
                                                           lambda _: [window], self.transcripts)
        self.assertEqual({'proj': 300}, seconds)


if __name__ == '__main__':
    unittest.main()
