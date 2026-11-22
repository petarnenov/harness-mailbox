import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import agent_waits

TASK = 't-1'


def at(hour, minute=0):
    return datetime.fromisoformat(f'2026-11-22T{hour:02d}:{minute:02d}:00+00:00')


class AgentWaitsTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.count = 0

    def write(self, relative, record):
        self.count += 1
        path = self.root / relative / f'{self.count}.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record))

    def from_worker(self, kind, hour, minute=0, folder='outbox/proj', task=TASK):
        self.write(folder, {'id': f'w{self.count}', 'type': kind, 'task_id': task,
                            'created_at': at(hour, minute).isoformat()})

    def to_worker(self, hour, minute=0, folder='inbox/proj'):
        self.write(folder, {'id': f'o{self.count}', 'type': 'g1_acceptance', 'task_id': TASK,
                            'created_at': at(hour, minute).isoformat()})

    def periods(self):
        return agent_waits.waiting_periods(self.root, {TASK})

    def test_a_gate_request_waits_until_the_orchestrator_answers(self):
        self.from_worker('g1_request', 10)
        self.to_worker(12)
        self.assertEqual([(at(10), at(12))], self.periods())

    def test_a_recorded_decision_is_an_answer(self):
        self.from_worker('question', 10)
        self.write('decisions/proj', {'task_id': TASK, 'decided_at': at(11).isoformat()})
        self.write('decisions/proj', {'task': 'other', 'at': at(10, 30).isoformat()})
        self.assertEqual([(at(10), at(11))], self.periods())

    def test_without_an_answer_file_the_next_worker_message_ends_the_wait(self):
        self.from_worker('g2_request', 10)
        self.from_worker('progress_report', 13, folder='inbox/orchestrator')
        self.assertEqual([(at(10), at(13))], self.periods())

    def test_progress_messages_and_answers_outside_a_wait_change_nothing(self):
        self.to_worker(9)
        self.from_worker('progress_report', 10)
        self.assertEqual([], self.periods())

    def test_repeated_asks_extend_one_wait_and_an_unanswered_wait_stays_open(self):
        self.from_worker('g1_request', 10)
        self.from_worker('g1_request', 11, folder='archive/orchestrator')
        self.to_worker(12, folder='outbox/orchestrator')
        self.from_worker('task_done', 15)
        self.assertEqual([(at(10), at(12)), (at(15), None)], self.periods())

    def test_the_same_message_in_outbox_and_orchestrator_inbox_counts_once(self):
        record = {'id': 'same', 'type': 'g1_request', 'task_id': TASK, 'created_at': at(10).isoformat()}
        self.write('outbox/proj', record)
        self.write('inbox/orchestrator', record)
        self.to_worker(11)
        self.assertEqual([(at(10), at(11))], self.periods())

    def test_other_tasks_and_unreadable_records_are_ignored(self):
        self.from_worker('g1_request', 10, task='other')
        self.write('outbox/proj', {'type': 'g1_request', 'task_id': TASK, 'created_at': 'not a time'})
        (self.root / 'outbox' / 'proj' / 'broken.json').write_text('{')
        self.assertEqual([], self.periods())

    def test_waited_seconds_clips_each_period_to_the_cycle(self):
        periods = [(at(8), at(10)), (at(11), at(12)), (at(13), None)]
        self.assertEqual(3 * 3600, agent_waits.waited_seconds(periods, at(9), at(14)))


if __name__ == '__main__':
    unittest.main()
