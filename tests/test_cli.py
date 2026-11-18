import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
MODULE = 'api_quota_audit'


class CliTests(unittest.TestCase):
    def invoke(self, source, output, mode=None):
        command = [sys.executable, '-m', MODULE + '.cli', '--source', str(source), '--output', str(output)]
        if mode is not None:
            command += ['--mode', mode]
        return subprocess.run(command, capture_output=True, text=True, timeout=10)

    def test_published_result_provenance_and_repeat(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'report.json'
            source = ROOT / 'fixtures/input.json'
            first = self.invoke(source, output)
            self.assertEqual(first.returncode, 0, first.stderr)
            report = json.loads(output.read_text())
            self.assertEqual(report, json.loads(first.stdout))
            self.assertEqual(report['source_sha256'], hashlib.sha256(source.read_bytes()).hexdigest())
            original = output.read_bytes()
            self.assertEqual(self.invoke(source, output).returncode, 0)
            self.assertEqual(output.read_bytes(), original)

    def test_malformed_and_invalid_inputs_preserve_existing_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'report.json'
            source = Path(temporary) / 'bad.json'
            output.write_text('previous report')
            for raw in ('{', '{"x":1,"x":2}', '{"x":NaN}', '{}'):
                with self.subTest(raw=raw):
                    source.write_text(raw)
                    result = self.invoke(source, output)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertEqual(output.read_text(), 'previous report')

    def test_source_output_collision_preserves_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / 'input.json'
            source.write_bytes((ROOT / 'fixtures/input.json').read_bytes())
            original = source.read_bytes()
            self.assertEqual(self.invoke(source, source).returncode, 2)
            self.assertEqual(source.read_bytes(), original)

    def test_symlink_output_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / 'target'
            target.write_text('unchanged')
            output = Path(temporary) / 'report.json'
            output.symlink_to(target)
            result = self.invoke(ROOT / 'fixtures/input.json', output)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(target.read_text(), 'unchanged')

    def test_missing_source_does_not_create_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'report.json'
            self.assertEqual(self.invoke(Path(temporary) / 'absent.json', output).returncode, 2)
            self.assertFalse(output.exists())


RETENTION_DOCUMENT = {
    'principal_policies': [{'principal': 'a', 'window_seconds': 3600, 'limit': 1}],
    'route_policies': [{'route': '1', 'window_seconds': 3600, 'limit': 1}],
    'requests': [
        {'id': 'r1', 'principal': 'a', 'route': '1', 'at': '2026-01-01T00:00:00Z', 'priority': 1},
        {'id': 'r2', 'principal': 'a', 'route': '1', 'at': '2026-01-01T00:10:00Z', 'priority': 2},
    ],
    'appeals': ['r1'],
}


class RetentionCliTests(unittest.TestCase):
    def invoke(self, source, output, mode):
        return subprocess.run(
            [sys.executable, '-m', MODULE + '.cli', '--mode', mode,
             '--source', str(source), '--output', str(output)],
            capture_output=True, text=True, timeout=10)

    def test_retention_report_contract_and_provenance(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / 'input.json'
            output = Path(temporary) / 'report.json'
            source.write_text(json.dumps(RETENTION_DOCUMENT))
            result = self.invoke(source, output, 'retention')
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(output.read_text())
            self.assertEqual(set(report), {'schema_version', 'mode', 'source_sha256', 'result'})
            self.assertEqual(report['schema_version'], 2)
            self.assertEqual(report['mode'], 'retention')
            self.assertEqual(report['source_sha256'], hashlib.sha256(source.read_bytes()).hexdigest())
            self.assertEqual(set(report['result']), {'baseline', 'appeals', 'deduplicated_request_count'})
            self.assertEqual(report['result']['baseline']['kept_ids'], ['r2'])
            self.assertEqual(report['result']['baseline']['dropped_ids'], ['r1'])
            self.assertEqual(report['result']['deduplicated_request_count'], 2)
            appeal = report['result']['appeals'][0]
            self.assertEqual(set(appeal), {
                'request_id', 'kept_ids', 'dropped_ids', 'kept_count', 'priority_sum',
                'entered_ids', 'removed_ids', 'count_loss', 'priority_loss'})
            self.assertEqual(appeal['kept_ids'], ['r1'])
            self.assertEqual(appeal['entered_ids'], ['r1'])
            self.assertEqual(appeal['removed_ids'], ['r2'])
            self.assertEqual(appeal['priority_loss'], 1)
            self.assertEqual(json.loads(result.stdout), report)

    def test_default_mode_keeps_v1_contract(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'report.json'
            source = ROOT / 'fixtures' / 'input.json'
            self.assertEqual(self.invoke(source, output, 'audit').returncode, 0)
            report = json.loads(output.read_text())
            self.assertEqual(set(report), {'schema_version', 'source_sha256', 'result'})
            self.assertEqual(report['schema_version'], 1)
            self.assertNotIn('mode', report)

    def test_invalid_retention_input_preserves_existing_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / 'bad.json'
            output = Path(temporary) / 'report.json'
            output.write_text('previous report')
            for raw in ('{', '{}', json.dumps({'principal_policies': [], 'route_policies': [],
                                                'requests': [], 'appeals': ['ghost']})):
                source.write_text(raw)
                result = self.invoke(source, output, 'retention')
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(output.read_text(), 'previous report')

    def test_source_file_bytes_are_never_modified(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / 'input.json'
            output = Path(temporary) / 'report.json'
            source.write_text(json.dumps(RETENTION_DOCUMENT))
            original = source.read_bytes()
            self.assertEqual(self.invoke(source, output, 'retention').returncode, 0)
            self.assertEqual(source.read_bytes(), original)


RECONCILE_DOCUMENT = {
    'timeline': ['2026-01-01T00:00:00Z', '2026-01-01T01:00:00Z',
                 '2026-01-01T02:00:00Z', '2026-01-01T03:00:00Z'],
    'max_slot_units': 10,
    'meters': [{'id': 'total', 'start': 0, 'end': 3, 'lower': 10, 'upper': 10}],
    'invoices': [{'id': 'left', 'start': 0, 'end': 2, 'units': 2},
                 {'id': 'right', 'start': 1, 'end': 3, 'units': 2}],
    'queries': [{'id': 'q', 'start': 0, 'end': 3}],
}


class ReconcileCliTests(unittest.TestCase):
    def invoke(self, source, output, mode='reconcile'):
        return subprocess.run(
            [sys.executable, '-m', MODULE + '.cli', '--mode', mode,
             '--source', str(source), '--output', str(output)],
            capture_output=True, text=True, timeout=10)

    def test_feasible_report_contract(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / 'input.json'
            output = Path(temporary) / 'report.json'
            source.write_text(json.dumps(dict(RECONCILE_DOCUMENT, invoices=[])))
            result = self.invoke(source, output)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(output.read_text())
            self.assertEqual(set(report), {'schema_version', 'mode', 'source_sha256', 'result'})
            self.assertEqual(report['schema_version'], 3)
            self.assertEqual(report['mode'], 'reconcile')
            self.assertEqual(report['source_sha256'], hashlib.sha256(source.read_bytes()).hexdigest())
            self.assertEqual(set(report['result']), {'status', 'ranges', 'certificate'})
            self.assertEqual(report['result']['status'], 'feasible')
            self.assertEqual(report['result']['certificate'], [])
            entry = report['result']['ranges'][0]
            self.assertEqual(set(entry), {'id', 'min_units', 'max_units',
                                          'min_allocation', 'max_allocation'})
            self.assertEqual((entry['min_units'], entry['max_units']), (10, 10))
            self.assertEqual(len(entry['min_allocation']), 3)
            self.assertEqual(len(entry['max_allocation']), 3)
            self.assertEqual(json.loads(result.stdout), report)

    def test_conflicts_are_successful_reports(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / 'input.json'
            output = Path(temporary) / 'report.json'
            source.write_text(json.dumps(RECONCILE_DOCUMENT))
            result = self.invoke(source, output)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(output.read_text())
            self.assertEqual(report['result']['status'], 'invoice_conflict')
            self.assertEqual(report['result']['ranges'], [])
            self.assertTrue(report['result']['certificate'])

            source.write_text(json.dumps(dict(
                RECONCILE_DOCUMENT,
                meters=[{'id': 'total', 'start': 0, 'end': 3, 'lower': 31, 'upper': 31}],
                invoices=[])))
            result = self.invoke(source, output)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(output.read_text())['result']['status'], 'meter_conflict')

    def test_invalid_reconcile_input_preserves_existing_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / 'bad.json'
            output = Path(temporary) / 'report.json'
            output.write_text('previous report')
            bad_documents = [
                '{}',
                json.dumps(dict(RECONCILE_DOCUMENT, extra=1)),
                json.dumps(dict(RECONCILE_DOCUMENT, timeline=['2026-01-01T00:00:00Z'])),
                json.dumps(dict(RECONCILE_DOCUMENT, max_slot_units=True)),
                json.dumps(dict(RECONCILE_DOCUMENT, invoices=[
                    {'id': 'i', 'start': 0, 'end': 9, 'units': 1}])),
                json.dumps(dict(RECONCILE_DOCUMENT, queries=[
                    {'id': 'q', 'start': 0, 'end': 1},
                    {'id': 'q', 'start': 1, 'end': 2}])),
            ]
            for raw in bad_documents:
                with self.subTest(raw=raw[:80]):
                    source.write_text(raw)
                    result = self.invoke(source, output)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertEqual(output.read_text(), 'previous report')

    def test_reconcile_output_protection(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / 'input.json'
            source.write_text(json.dumps(RECONCILE_DOCUMENT))
            self.assertEqual(self.invoke(source, source).returncode, 2)
            self.assertEqual(json.loads(source.read_text()), RECONCILE_DOCUMENT)
            target = Path(temporary) / 'target'
            target.write_text('unchanged')
            link = Path(temporary) / 'link.json'
            link.symlink_to(target)
            self.assertEqual(self.invoke(source, link).returncode, 2)
            self.assertEqual(target.read_text(), 'unchanged')

    def test_fixture_runs(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'report.json'
            result = self.invoke(ROOT / 'fixtures/reconcile.json', output)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(output.read_text())
            self.assertEqual(report['result']['status'], 'feasible')
            self.assertEqual([x['id'] for x in report['result']['ranges']], ['middle', 'whole'])
