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
