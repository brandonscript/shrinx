import json
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zlib
import shrinx


def png(path, depth=8, animated=False):
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
    pixels = bytes([23, 123, 234, 255]) * 64
    data = b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 64, 64, depth, 6, 0, 0, 0))
    if animated:
        data += chunk(b'acTL', struct.pack('>II', 1, 0))
    data += chunk(b'IDAT', zlib.compress((b'\0' + pixels) * 64)) + chunk(b'IEND', b'')
    path.write_bytes(data)


@unittest.skipUnless(shutil.which('cwebp') and shutil.which('dwebp'), 'WebP tools required')
class ConversionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'Screenshot with spaces.PNG'
        png(self.path)
        self.original = self.path.read_bytes()
        self.cfg = json.loads((shrinx.ROOT / 'config.example.json').read_text())
        self.cfg.update(minimum_age_seconds=0, only_if_smaller=False, trash_originals=False)
        self.encoder = shutil.which('cwebp')

    def test_lossless_roundtrip_and_repeat(self):
        self.cfg['mode'] = 'lossless'
        self.assertEqual(shrinx.convert(self.path, self.cfg, self.encoder), 'converted')
        output = self.path.with_suffix('.webp')
        pam = subprocess.check_output(['dwebp', '-quiet', str(output), '-pam', '-o', '-'])
        raw = pam.split(b'ENDHDR\n', 1)[1]
        self.assertEqual(raw, bytes([23, 123, 234, 255]) * 64 * 64)
        self.assertEqual(self.path.read_bytes(), self.original)
        before = output.read_bytes()
        self.assertEqual(shrinx.convert(self.path, self.cfg, self.encoder), 'skipped')
        self.assertEqual(output.read_bytes(), before)

    def test_auto_chooses_smallest(self):
        sizes = []
        for mode in ('lossless', 'lossy', 'auto'):
            self.cfg['mode'] = mode
            self.assertEqual(shrinx.convert(self.path, self.cfg, self.encoder), 'converted')
            output = self.path.with_suffix('.webp')
            sizes.append(output.stat().st_size)
            output.unlink()
        self.assertEqual(sizes[2], min(sizes[:2]))

    def test_dry_run_and_young_file(self):
        self.assertEqual(shrinx.convert(self.path, self.cfg, self.encoder, True), 'planned')
        self.assertFalse(self.path.with_suffix('.webp').exists())
        self.cfg['minimum_age_seconds'] = 60
        self.assertEqual(shrinx.convert(self.path, self.cfg, self.encoder), 'skipped')

    def test_unsupported_and_collision(self):
        for depth, animated in ((16, False), (8, True)):
            png(self.path, depth, animated)
            self.assertEqual(shrinx.convert(self.path, self.cfg, self.encoder), 'skipped')
        png(self.path)
        self.path.with_suffix('.webp').write_bytes(b'existing')
        self.assertEqual(shrinx.convert(self.path, self.cfg, self.encoder), 'skipped')
        self.assertEqual(self.path.with_suffix('.webp').read_bytes(), b'existing')

    def test_cleanup_existing_verified_output(self):
        shrinx.convert(self.path, self.cfg, self.encoder)
        self.cfg['trash_originals'] = True
        with patch('shrinx.trash') as trash:
            self.assertEqual(shrinx.convert(self.path, self.cfg, self.encoder), 'trashed')
            trash.assert_called_once_with(self.path)

    def test_cleanup_unrelated_or_changed_output_keeps_png(self):
        self.path.with_suffix('.webp').write_bytes(b'unrelated')
        self.cfg['trash_originals'] = True
        with patch('shrinx.trash') as trash:
            self.assertEqual(shrinx.convert(self.path, self.cfg, self.encoder), 'skipped')
            trash.assert_not_called()
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_new_cleanup_and_dry_run(self):
        self.cfg['trash_originals'] = True
        with patch('shrinx.trash') as trash:
            self.assertEqual(shrinx.convert(self.path, self.cfg, self.encoder, True), 'planned')
            trash.assert_not_called()
            self.assertEqual(shrinx.convert(self.path, self.cfg, self.encoder), 'trashed')
            trash.assert_called_once_with(self.path)
        self.assertTrue(self.path.with_suffix('.webp').exists())

    def test_decode_failure_keeps_png(self):
        self.cfg.update(trash_originals=True, dwebp='/usr/bin/false')
        with patch('shrinx.trash') as trash:
            with self.assertRaises(subprocess.CalledProcessError):
                shrinx.convert(self.path, self.cfg, self.encoder)
            trash.assert_not_called()
        self.assertEqual(self.path.read_bytes(), self.original)

    def run_report(self, dry_run=False):
        self.cfg.update(folders=[str(self.path.parent)], reports_directory='reports')
        with patch('shrinx.ROOT', self.path.parent):
            result = shrinx.run(self.cfg, self.path.parent / 'config.json', dry_run)
        reports = sorted((self.path.parent / 'reports').glob('run-*.json'))
        return result, json.loads(reports[-1].read_text())

    def test_report_retained_then_existing_cleanup(self):
        result, report = self.run_report()
        output_size = self.path.with_suffix('.webp').stat().st_size
        self.assertEqual(result, 0)
        self.assertEqual(report['files_processed'], 1)
        self.assertEqual(report['webps_created'], 1)
        self.assertEqual(report['compression_savings_bytes'], len(self.original) - output_size)
        self.assertEqual(report['current_disk_usage_change_bytes'], output_size)
        self.cfg['trash_originals'] = True
        with patch('shrinx.trash', side_effect=lambda p: p.unlink()):
            result, report = self.run_report()
        self.assertEqual(result, 0)
        self.assertEqual(report['webps_created'], 0)
        self.assertEqual(report['pngs_trashed'], 1)
        self.assertEqual(report['current_disk_usage_change_bytes'], 0)
        self.assertEqual(report['disk_usage_change_after_emptying_trash_bytes'], -len(self.original))
        self.assertEqual(report['compression_savings_bytes'], len(self.original) - output_size)
        result, report = self.run_report()
        self.assertEqual(report['files_scanned'], 0)
        self.assertEqual(report['compression_savings_bytes'], 0)

    def test_report_failure_and_dry_run(self):
        result, report = self.run_report(True)
        self.assertEqual(report['counts']['planned'], 1)
        self.assertEqual(report['compression_savings_bytes'], 0)
        self.cfg['cwebp'] = '/usr/bin/false'
        result, report = self.run_report()
        self.assertEqual(result, 1)
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(report['counts']['failed'], 1)
        self.assertEqual(report['files_processed'], 0)
        self.assertEqual(report['new_webp_bytes'], 0)

    def test_report_partial_failure_counts_created_output(self):
        self.cfg['trash_originals'] = True
        with patch('shrinx.trash', side_effect=OSError('Trash unavailable')):
            result, report = self.run_report()
        self.assertEqual(result, 1)
        self.assertEqual(report['webps_created'], 1)
        self.assertGreater(report['current_disk_usage_change_bytes'], 0)
        self.assertEqual(report['pngs_trashed'], 0)
        self.assertEqual(report['compression_savings_bytes'], 0)
        self.assertTrue(self.path.exists())

    def test_failure_retains_original(self):
        with self.assertRaises(subprocess.CalledProcessError):
            shrinx.convert(self.path, self.cfg, '/usr/bin/false')
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertFalse(self.path.with_suffix('.webp').exists())
        self.assertFalse(list(self.path.parent.glob('.shrinx-*')))


if __name__ == '__main__':
    unittest.main()
