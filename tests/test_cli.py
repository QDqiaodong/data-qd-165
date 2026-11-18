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
    def invoke(self, source, output):
        return subprocess.run([sys.executable, '-m', MODULE + '.cli', '--source', str(source), '--output', str(output)], capture_output=True, text=True, timeout=10)

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
