from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import plistlib
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import scheduling


class SchedulingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.home_patch = patch('scheduling.Path.home', return_value=self.home)
        self.home_patch.start()
        self.addCleanup(self.home_patch.stop)
        self.cfg = self.home / 'config.json'
        self.cfg.write_text('{}')

    def test_daily_and_interval_next_due(self):
        cfg = {'daily_at': '01:00', 'interval_seconds': 900}
        now = datetime(2026, 10, 22, 0, 59)
        self.assertEqual(scheduling.next_due(cfg, now), datetime(2026, 10, 22, 1))
        self.assertEqual(scheduling.next_due(cfg, datetime(2026, 10, 22, 1)), datetime(2026, 10, 23, 1))
        cfg['daily_at'] = None
        self.assertEqual(scheduling.next_due(cfg, now), now + timedelta(seconds=900))

    def test_future_timestamp_validation(self):
        now = datetime(2026, 10, 22, tzinfo=timezone.utc)
        self.assertEqual(scheduling.parse_when('2026-10-23T01:00-07:00', now), datetime(2026, 10, 23, 8, tzinfo=timezone.utc))
        for value in ('yesterday', '2020-01-01T00:00Z', '2026-02-30T01:00'):
            with self.assertRaises(ValueError):
                scheduling.parse_when(value, now)

    def create_job(self):
        with patch('scheduling.subprocess.run') as command:
            identifier = scheduling.schedule_once(self.cfg, '2099-01-01T01:00Z', ['/usr/bin/python3', '/opt/shrinx.py'], True)
        job = scheduling.jobs_directory() / (identifier + '.json')
        data = json.loads(job.read_text())
        payload = plistlib.loads(Path(data['plist']).read_bytes())
        self.assertEqual(payload['StartInterval'], 60)
        self.assertIn('--dispatch-once', payload['ProgramArguments'])
        self.assertTrue(data['dry_run'])
        return job, data

    def test_oneoff_not_early_and_exactly_once(self):
        job, data = self.create_job()
        runner, loader = Mock(return_value=0), Mock(return_value={})
        self.assertEqual(scheduling.dispatch_once(job, loader, runner), 0)
        runner.assert_not_called()
        data['at'] = '2020-01-01T01:00:00+00:00'
        scheduling.write_job(job, data)
        with patch('scheduling.subprocess.run'):
            self.assertEqual(scheduling.dispatch_once(job, loader, runner), 0)
            self.assertEqual(scheduling.dispatch_once(job, loader, runner), 0)
        runner.assert_called_once_with({}, self.cfg.resolve(), True)
        self.assertEqual(json.loads(job.read_text())['status'], 'completed')
        self.assertFalse(Path(data['plist']).exists())

    def test_oneoff_busy_defers_and_failure_finalizes(self):
        job, data = self.create_job()
        data['at'] = '2020-01-01T01:00:00+00:00'
        scheduling.write_job(job, data)
        runner, loader = Mock(return_value=75), Mock(return_value={})
        self.assertEqual(scheduling.dispatch_once(job, loader, runner), 0)
        self.assertEqual(json.loads(job.read_text())['status'], 'pending')
        self.assertFalse(job.with_suffix('.claim').exists())
        runner.return_value = 1
        with patch('scheduling.subprocess.run'):
            self.assertEqual(scheduling.dispatch_once(job, loader, runner), 1)
        self.assertEqual(json.loads(job.read_text())['status'], 'failed')

    def test_claim_and_bootstrap_failure(self):
        job, data = self.create_job()
        data['at'] = '2020-01-01T01:00:00+00:00'
        scheduling.write_job(job, data)
        job.with_suffix('.claim').write_text('')
        runner = Mock()
        scheduling.dispatch_once(job, Mock(), runner)
        runner.assert_not_called()
        before = set(scheduling.jobs_directory().glob('*.json'))
        with patch('scheduling.subprocess.run', side_effect=subprocess.CalledProcessError(1, 'launchctl')):
            with self.assertRaises(subprocess.CalledProcessError):
                scheduling.schedule_once(self.cfg, '2099-01-01T01:00Z', ['python3'])
        self.assertEqual(set(scheduling.jobs_directory().glob('*.json')), before)

    def test_service_once_and_legacy_guard(self):
        loader, runner = Mock(return_value={}), Mock(return_value=0)
        self.assertEqual(scheduling.serve(self.cfg, loader, runner, True, True), 0)
        runner.assert_called_once_with({}, self.cfg, True)
        legacy = self.home / 'Library/LaunchAgents' / (scheduling.LABEL + '.plist')
        legacy.parent.mkdir(parents=True)
        legacy.write_text('legacy')
        with self.assertRaisesRegex(ValueError, 'Legacy'):
            scheduling.serve(self.cfg, loader, runner)

    def test_service_waits_until_due(self):
        cfg = {'daily_at': '01:00', 'interval_seconds': 900}
        clock = Mock()
        clock.now.side_effect = [datetime(2026, 10, 22, 0, 59), datetime(2026, 10, 22, 1), datetime(2026, 10, 22, 1, 0, 1)]
        runner = Mock(return_value=0)
        with patch('scheduling.datetime', clock), patch('scheduling.time.sleep', side_effect=[None, KeyboardInterrupt]):
            with self.assertRaises(KeyboardInterrupt):
                scheduling.serve(self.cfg, Mock(return_value=cfg), runner)
        runner.assert_called_once_with(cfg, self.cfg, False)
