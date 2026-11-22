import csv
import importlib.util
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location(
    'preserve_dashboard', Path(__file__).resolve().parents[1] / 'scripts/preserve_dashboard.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def write_snapshot(site, dates, raw=True):
    site.mkdir(parents=True, exist_ok=True)
    records = [{'initialization': date, 'metrics': {'gefs_reduced': {
        'dry': {'post': {'bss': .2}, 'raw': {'bss': .1} if raw else None}}}}
        for date in dates]
    data = {'dates': records, 'latestVerifiedInitialization': dates[-1],
            'observationLatest': '2026-11-20',
            'pooledMaps': {'dry': 'assets/verification/pooled.png'}}
    (site / 'verification-data.js').write_text('window.VERIFICATION_DATA = ' + json.dumps(data) + ';\n')
    (site / 'downloads').mkdir(exist_ok=True)
    with (site / 'downloads/verification_metrics.csv').open('w') as handle:
        writer = csv.writer(handle)
        writer.writerow(['initialization'])
        writer.writerows([[date] for date in dates])
    (site / 'assets/verification').mkdir(parents=True, exist_ok=True)
    (site / 'assets/verification/pooled.png').write_bytes(b'map-' + dates[-1].encode())
    return data


class PreservationTests(unittest.TestCase):
    def test_restore_uses_complete_deployment_and_preserves_new_html(self):
        with tempfile.TemporaryDirectory() as root:
            old, live = Path(root) / 'checkout', Path(root) / 'deployed'
            write_snapshot(old, ['20261021'])
            write_snapshot(live, ['20261021', '20261110'])
            (old / 'verification.html').write_text('new interface')
            (live / 'verification.html').write_text('old interface')
            (live / 'data.js').write_text('current forecast')

            def download(command, **kwargs):
                destination = Path(command[-1])
                destination.mkdir(parents=True)
                with tarfile.open(destination / 'artifact.tar', 'w') as archive:
                    archive.add(live, arcname='.')

            responses = [{'artifacts': [{'id': 1, 'name': 'github-pages',
                          'expired': False, 'workflow_run': {'id': 100}}]},
                         {'conclusion': 'success', 'head_branch': 'main',
                          'path': '.github/workflows/update-from-drive.yml'}]
            with patch.object(module, 'gh_json', side_effect=responses), \
                    patch.object(module.subprocess, 'run', side_effect=download):
                module.restore(old, 'owner/repo')
            self.assertEqual(module.validate(old)['latestVerifiedInitialization'], '20261110')
            self.assertEqual((old / 'verification.html').read_text(), 'new interface')
            self.assertEqual((old / 'data.js').read_text(), 'current forecast')

    def test_rejects_lost_dates_even_when_latest_advances(self):
        with tempfile.TemporaryDirectory() as root:
            old = write_snapshot(Path(root) / 'old', ['20261015', '20261110'])
            new = write_snapshot(Path(root) / 'new', ['20261110', '20261111'])
            with self.assertRaisesRegex(ValueError, 'remove previously'):
                module.ensure_no_regression(old, new)

    def test_rejects_loss_of_raw_results(self):
        with tempfile.TemporaryDirectory() as root:
            old = write_snapshot(Path(root) / 'old', ['20261110'])
            new = write_snapshot(Path(root) / 'new', ['20261110'], raw=False)
            with self.assertRaisesRegex(ValueError, 'lose'):
                module.ensure_no_regression(old, new)

    def test_missing_map_is_not_a_complete_snapshot(self):
        with tempfile.TemporaryDirectory() as root:
            site = Path(root)
            write_snapshot(site, ['20261110'])
            (site / 'assets/verification/pooled.png').unlink()
            with self.assertRaisesRegex(ValueError, 'Missing'):
                module.validate(site)

    def test_failed_refresh_preserves_all_previous_files(self):
        with tempfile.TemporaryDirectory() as root:
            site = Path(root) / 'site'
            write_snapshot(site, ['20261110'])
            before = {p.relative_to(site): p.read_bytes() for p in site.rglob('*') if p.is_file()}
            # Simulate a producer that writes partial output and then fails.
            code = ('import pathlib,sys; p=pathlib.Path(sys.argv[-1]); '
                    '(p/"verification-data.js").write_text("broken"); sys.exit(1)')
            self.assertFalse(module.refresh(site, [sys.executable, '-c', code]))
            after = {p.relative_to(site): p.read_bytes() for p in site.rglob('*') if p.is_file()}
            self.assertEqual(before, after)

    def test_successful_refresh_replaces_bundle_but_keeps_forecast(self):
        with tempfile.TemporaryDirectory() as root:
            site, incoming = Path(root) / 'site', Path(root) / 'incoming'
            write_snapshot(site, ['20261110'])
            write_snapshot(incoming, ['20261110', '20261111'])
            (site / 'data.js').write_text('latest forecast')
            code = ('import shutil,sys; shutil.copytree(sys.argv[1],sys.argv[-1],dirs_exist_ok=True)')
            self.assertTrue(module.refresh(site, [sys.executable, '-c', code, str(incoming)]))
            self.assertEqual(module.validate(site)['latestVerifiedInitialization'], '20261111')
            self.assertEqual((site / 'data.js').read_text(), 'latest forecast')
            self.assertEqual((site / 'assets/verification/pooled.png').read_bytes(), b'map-20261111')


if __name__ == '__main__':
    unittest.main()
