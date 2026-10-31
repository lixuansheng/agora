"""Outcome regressions derived from the real file-editing and review workflow."""
import hashlib
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from conductor_core import audit_judgment_progress, evidence_audit


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def record(**updates):
    return {
        'claim': 'the output is 3', 'source_type': 'tool_output',
        'source_ref': 'run://test-1/stdout', 'excerpt': 'OUTPUT=3',
        'independence_key': 'test-1', 'verification': 'observed',
        'relation': 'supports', **updates,
    }


class ProgressRegressionTests(unittest.TestCase):
    def test_paraphrased_claim_does_not_become_new_evidence(self):
        audit = evidence_audit([record(claim='this confirms a result of three')], [record()])
        self.assertEqual(audit['new_record_count'], 1)  # Keep the complete audit trail.
        self.assertEqual(audit['new_material_count'], 0)

    def test_changed_dependency_label_does_not_become_new_material(self):
        audit = evidence_audit([record(independence_key='another-label')], [record()])
        self.assertEqual(audit['new_material_count'], 0)

    def test_same_source_new_raw_output_is_new_material(self):
        audit = evidence_audit([record(excerpt='OUTPUT=4')], [record()])
        self.assertEqual(audit['new_material_count'], 1)

    def test_self_reported_verification_upgrade_is_not_new_material(self):
        audit = evidence_audit([record(verification='reproduced')], [record(verification='reported')])
        self.assertEqual(audit['new_material_count'], 0)

    def test_unattributed_reasoning_does_not_clear_stagnation(self):
        action = {'deliverable': 'same', 'evidence_records': [record(
            source_type='model_reasoning', source_ref='', independence_key='', excerpt='I agree')]}
        result = audit_judgment_progress({'progress': 'new_evidence'}, action, [], 1)
        self.assertEqual(result['progress'], 'none')

    def test_reworded_claim_does_not_clear_stagnation(self):
        history = [{'deliverable_sha256': digest('same'), 'evidence_records': [record()]}]
        action = {'deliverable': 'same', 'evidence_records': [record(claim='the value equals three')]}
        result = audit_judgment_progress({'progress': 'new_evidence'}, action, history, 2)
        self.assertEqual(result['progress'], 'none')

    def test_real_file_change_counts_with_unchanged_summary(self):
        before, after = {'C:/task/output.py': digest('old')}, {'C:/task/output.py': digest('new')}
        history = [{'deliverable_sha256': digest('C:/task/output.py'), 'file_sha256': before,
                    'evidence_records': []},
                   {'deliverable_sha256': digest('C:/task/output.py'), 'file_sha256': after,
                    'evidence_records': []}]
        action = {'deliverable': 'C:/task/output.py', 'files': list(after), 'evidence_records': []}
        result = audit_judgment_progress({'progress': 'improved'}, action, history, 2)
        self.assertEqual(result['progress'], 'improved')

    def test_reworded_file_summary_without_byte_change_is_not_improvement(self):
        files = {'C:/task/output.py': digest('same bytes')}
        history = [{'deliverable_sha256': digest('old summary'), 'file_sha256': files,
                    'evidence_records': []},
                   {'deliverable_sha256': digest('new summary'), 'file_sha256': files,
                    'evidence_records': []}]
        action = {'deliverable': 'new summary', 'files': list(files), 'evidence_records': []}
        result = audit_judgment_progress({'progress': 'improved'}, action, history, 2)
        self.assertEqual(result['progress'], 'none')

    def test_text_only_deliverable_change_still_counts(self):
        history = [{'deliverable_sha256': digest('old'), 'evidence_records': []},
                   {'deliverable_sha256': digest('new'), 'evidence_records': []}]
        result = audit_judgment_progress({'progress': 'improved'},
                                         {'deliverable': 'new', 'files': [], 'evidence_records': []}, history, 2)
        self.assertEqual(result['progress'], 'improved')

    def test_previous_edit_is_not_counted_again_in_next_discussion(self):
        history = [{'deliverable_sha256': digest('old'), 'evidence_records': []},
                   {'deliverable_sha256': digest('new'), 'evidence_records': []}]
        action = {'deliverable': 'new', 'files': [], 'evidence_records': []}
        result = audit_judgment_progress({'progress': 'improved'}, action, history, 3,
                                         previous_action_count=2)
        self.assertEqual(result['progress'], 'none')

    def test_previous_evidence_is_not_counted_again_in_next_discussion(self):
        history = [{'deliverable_sha256': digest('same'), 'evidence_records': []},
                   {'deliverable_sha256': digest('same'), 'evidence_records': [record()]}]
        action = {'deliverable': 'same', 'evidence_records': [record()]}
        result = audit_judgment_progress({'progress': 'new_evidence'}, action, history, 3,
                                         previous_action_count=2)
        self.assertEqual(result['progress'], 'none')

    def test_new_edit_after_previous_discussion_counts(self):
        history = [{'deliverable_sha256': digest('old'), 'evidence_records': []},
                   {'deliverable_sha256': digest('new'), 'evidence_records': []}]
        result = audit_judgment_progress({'progress': 'improved'},
                                         {'deliverable': 'new', 'evidence_records': []}, history, 2,
                                         previous_action_count=1)
        self.assertEqual(result['progress'], 'improved')

    def test_first_saved_file_counts_even_when_summary_is_unchanged(self):
        history = [{'deliverable_sha256': digest('summary'), 'file_sha256': {}, 'evidence_records': []},
                   {'deliverable_sha256': digest('summary'),
                    'file_sha256': {'C:/task/new.txt': digest('saved')}, 'evidence_records': []}]
        result = audit_judgment_progress({'progress': 'improved'},
                                         {'deliverable': 'summary', 'files': ['C:/task/new.txt'], 'evidence_records': []},
                                         history, 2, previous_action_count=1)
        self.assertEqual(result['progress'], 'improved')

    def test_workflow_changes_method_after_repeated_unbacked_progress_claims(self):
        from conductor_core import Conductor, DEFAULTS, Store
        from test_conductor import FakeCodex, FakeHarness, C_MODELS, ROUTE
        log = []
        with tempfile.TemporaryDirectory() as directory, patch('conductor_core.DATA_DIR', Path(directory)):
            settings = {**DEFAULTS, 'workspace': directory, 'max_discussion_rounds': 5}
            store = Store(':memory:')
            self.addCleanup(store.conn.close)
            engine = Conductor(settings, store)
            engine.codex, engine.harness = FakeCodex(log, ['continue'] * 4 + ['resolved']), FakeHarness(log)
            engine.codex_models = C_MODELS
            engine.refresh = lambda: ''
            engine.route = lambda *args: {**copy.deepcopy(ROUTE), 'use_discussion': True}
            original = engine.codex.ask

            def ask(thread_id, text, *args, **kwargs):
                data = json.loads(text.split('\n', 1)[1])
                answer = original(thread_id, text, *args, **kwargs)
                if data['phase'] == 'discussion_judge':
                    result = json.loads(answer)
                    result['next_step'] = '' if result['status'] == 'resolved' else 'check using method ' + str(engine.codex.judge_calls)
                    return json.dumps(result)
                return answer

            engine.codex.ask = ask
            self.assertEqual(engine.run('produce the requested result'), 'FINAL')
            phases = [data['phase'] for _, data in log]
            self.assertEqual(phases.count('discussion_evidence'), 1)
            round_three = next(data for _, data in log if data['phase'] == 'discussion' and data['round'] == 3)
            self.assertEqual(round_three['stalled_rounds'], 1)
            final = log[-1][1]
            self.assertTrue(final['discussion_resolved'])


if __name__ == '__main__':
    unittest.main()
