import json
import base64
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


JPEG_FIXTURE = base64.b64decode('/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAIBAQEBAQIBAQECAgICAgQDAgICAgUEBAMEBgUGBgYFBgYGBwkIBgcJBwYGCAsICQoKCgoKBggLDAsKDAkKCgr/2wBDAQICAgICAgUDAwUKBwYHCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgr/wAARCAAQABADASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwD8pvhV+z3/AKv/AEH0/hr6W+FP7Pf+q/0H0/hr1L4Vfs9f6v8A0H0/hr6W+FX7Pf8Aq/8AQvT+Gvo8nnsev4J+L38L9526n//Z')


@unittest.skipUnless(shutil.which('cwebp') and shutil.which('dwebp'), 'WebP tools required')
class ConversionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'Screenshot with spaces.PNG'
        png(self.path)
        self.original = self.path.read_bytes()
        self.cfg = json.loads((shrinx.ROOT / 'config.example.json').read_text())
        self.cfg.update(minimum_age_seconds=0, only_if_smaller=False, trash_originals=False, file_types=list(shrinx.TYPE_EXTENSIONS))
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

    def test_jpeg_extensions_are_scanned_and_decodable(self):
        self.path.unlink()
        paths = []
        for index, extension in enumerate(sorted(shrinx.JPEG_EXTENSIONS)):
            for suffix in (extension, extension.upper()):
                p = self.path.parent / f'image-{len(paths)}{suffix}'
                p.write_bytes(JPEG_FIXTURE)
                paths.append(p)
        result, report = self.run_report()
        self.assertEqual(result, 0)
        self.assertEqual(report['files_scanned'], len(paths))
        self.assertEqual(report['webps_created'], len(paths))
        for p in paths:
            self.assertEqual(p.read_bytes(), JPEG_FIXTURE)
            subprocess.run(['dwebp', '-quiet', str(p.with_suffix('.webp')), '-o', '/dev/null'], check=True)

    def test_jpeg_existing_cleanup_reports_generic_totals(self):
        self.path.unlink()
        self.path = self.path.with_suffix('.JPEG')
        self.path.write_bytes(JPEG_FIXTURE)
        self.run_report()
        self.cfg['trash_originals'] = True
        with patch('shrinx.trash', side_effect=lambda p: p.unlink()):
            result, report = self.run_report()
        self.assertEqual(result, 0)
        self.assertEqual(report['originals_trashed'], 1)
        self.assertEqual(report['jpegs_trashed'], 1)
        self.assertEqual(report['pngs_trashed'], 0)
        self.assertEqual(report['trashed_original_bytes'], len(JPEG_FIXTURE))
        self.assertEqual(report['disk_usage_change_after_emptying_trash_bytes'], -len(JPEG_FIXTURE))

    def test_jpeg_invalid_and_shared_stem_keep_original(self):
        shrinx.convert(self.path, self.cfg, self.encoder)
        jpeg = self.path.with_suffix('.jpg')
        jpeg.write_bytes(JPEG_FIXTURE)
        self.cfg['trash_originals'] = True
        with patch('shrinx.trash') as trash:
            self.assertEqual(shrinx.convert(jpeg, self.cfg, self.encoder), 'skipped')
            trash.assert_not_called()
        self.assertEqual(jpeg.read_bytes(), JPEG_FIXTURE)
        invalid = self.path.parent / 'invalid.jpeg'
        invalid.write_bytes(b'not a jpeg')
        self.assertEqual(shrinx.convert(invalid, self.cfg, self.encoder), 'skipped')
        self.assertTrue(invalid.exists())

    @unittest.skipUnless(shutil.which('sips'), 'macOS sips required')
    def test_tiff_bmp_extensions_and_cleanup(self):
        self.cfg['metadata_policy'] = 'discard'
        self.cfg['mode'] = 'lossless'
        for extension in ('.tif', '.tiff', '.TIF', '.TIFF', '.bmp', '.BMP'):
            with self.subTest(extension=extension):
                source = self.path.parent / ('sample' + extension)
                if extension.lower() == '.bmp':
                    width = height = 8
                    pixels = bytes([234, 123, 23]) * width * height
                    source.write_bytes(struct.pack('<2sIHHI', b'BM', 54 + len(pixels), 0, 0, 54)
                        + struct.pack('<IiiHHIIiiII', 40, width, height, 1, 24, 0, len(pixels), 0, 0, 0, 0) + pixels)
                else:
                    subprocess.run(['sips', '-s', 'format', 'tiff', str(self.path), '--out', str(source)], check=True, capture_output=True)
                before = source.read_bytes()
                self.assertTrue(shrinx.image_supported(source))
                self.assertEqual(shrinx.convert(source, self.cfg, self.encoder), 'converted')
                output = source.with_suffix('.webp')
                if extension.lower() == '.bmp':
                    ppm = subprocess.check_output(['dwebp', '-quiet', str(output), '-ppm', '-o', '-'])
                    self.assertEqual(ppm.split(b'\n', 3)[3], bytes([23, 123, 234]) * 64)
                subprocess.run(['dwebp', '-quiet', str(output), '-o', '/dev/null'], check=True)
                self.cfg['trash_originals'] = True
                with patch('shrinx.trash') as trash:
                    self.assertEqual(shrinx.convert(source, self.cfg, self.encoder), 'trashed')
                    trash.assert_called_once_with(source)
                self.assertEqual(source.read_bytes(), before)
                self.cfg['trash_originals'] = False
                source.unlink()
                output.unlink()

    def test_tiff_unsafe_variants_are_retained(self):
        # A minimal classic TIFF IFD: bit depth, photometric RGB, sample count.
        for depth, next_ifd, subifd in ((16, 0, False), (8, 8, False), (8, 0, True)):
            entries = [(258, depth), (262, 2), (277, 3)]
            if subifd:
                entries.append((330, 1))
            data = b'II' + struct.pack('<HIH', 42, 8, len(entries))
            for tag, value in entries:
                data += struct.pack('<HHIHH', tag, 3, 1, value, 0)
            data += struct.pack('<I', next_ifd)
            source = self.path.parent / 'unsafe.tiff'
            source.write_bytes(data)
            self.cfg['trash_originals'] = True
            with patch('shrinx.trash') as trash:
                self.assertEqual(shrinx.convert(source, self.cfg, self.encoder), 'skipped')
                trash.assert_not_called()
            self.assertEqual(source.read_bytes(), data)

    def test_enabled_file_types_filter_scanning(self):
        jpeg = self.path.parent / 'photo.jpg'
        jpeg.write_bytes(JPEG_FIXTURE)
        self.cfg['file_types'] = ['png']
        result, report = self.run_report()
        self.assertEqual(report['files_scanned'], 1)
        self.assertFalse(jpeg.with_suffix('.webp').exists())
        self.cfg['file_types'] = ['jpeg']
        result, report = self.run_report()
        self.assertEqual(report['files_scanned'], 1)
        self.assertEqual(report['webps_created'], 1)
        self.cfg['file_types'] = []
        result, report = self.run_report()
        self.assertEqual(report['files_scanned'], 0)

    def test_per_type_settings_override_global_defaults(self):
        jpeg = self.path.parent / 'photo.jpg'
        jpeg.write_bytes(JPEG_FIXTURE)
        self.cfg['mode'] = 'lossless'
        self.cfg['file_types'] = {'jpeg': {'mode': 'lossy', 'quality': 80}}
        real_run = subprocess.run
        with patch('shrinx.subprocess.run', wraps=real_run) as run:
            self.assertEqual(shrinx.convert(jpeg, self.cfg, self.encoder), 'converted')
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(len(commands), 1)
        self.assertNotIn('-lossless', commands[0])
        self.assertEqual(commands[0][commands[0].index('-q') + 1], '80')
        self.assertEqual(shrinx.convert(self.path, self.cfg, self.encoder), 'skipped')

    def test_invalid_file_types_rejected(self):
        config = self.path.parent / 'settings.json'
        for types in (['jpg'], ['unknown'], 'png', [1]):
            self.cfg['file_types'] = types
            config.write_text(json.dumps(self.cfg))
            with self.assertRaises(ValueError):
                shrinx.load_config(config)

    def test_metadata_policy_skips_or_explicitly_discards(self):
        payload = b'Description\0private test note'
        chunk = struct.pack('>I', len(payload)) + b'tEXt' + payload + struct.pack('>I', zlib.crc32(b'tEXt' + payload))
        data = self.original[:33] + chunk + self.original[33:]
        self.path.write_bytes(data)
        self.cfg.update(trash_originals=True, metadata_policy='skip')
        with patch('shrinx.trash') as trash:
            self.assertEqual(shrinx.convert(self.path, self.cfg, self.encoder), 'skipped')
            trash.assert_not_called()
        self.assertFalse(self.path.with_suffix('.webp').exists())
        self.cfg.update(trash_originals=False, metadata_policy='discard')
        self.assertEqual(shrinx.convert(self.path, self.cfg, self.encoder), 'converted')
        self.assertEqual(self.path.read_bytes(), data)

    def test_jpeg_comment_metadata_default_skip(self):
        source = self.path.parent / 'comment.jpg'
        comment = b'test note'
        source.write_bytes(JPEG_FIXTURE[:2] + b'\xff\xfe' + struct.pack('>H', len(comment) + 2) + comment + JPEG_FIXTURE[2:])
        self.cfg.pop('metadata_policy', None)
        self.assertEqual(shrinx.convert(source, self.cfg, self.encoder), 'skipped')
        self.cfg['metadata_policy'] = 'discard'
        self.assertEqual(shrinx.convert(source, self.cfg, self.encoder), 'converted')

    def test_invalid_per_type_settings_and_metadata_policy(self):
        config = self.path.parent / 'settings.json'
        for options in ({'enabled': 'false'}, {'mode': 'bad'}, {'quality': 101}, {'quality': True}, {'unexpected': 1}):
            self.cfg['file_types'] = {'png': options}
            config.write_text(json.dumps(self.cfg))
            with self.assertRaises(ValueError):
                shrinx.load_config(config)
        self.cfg['file_types'] = []
        self.cfg['metadata_policy'] = 'unknown'
        config.write_text(json.dumps(self.cfg))
        with self.assertRaises(ValueError):
            shrinx.load_config(config)

    def test_failure_retains_original(self):
        with self.assertRaises(subprocess.CalledProcessError):
            shrinx.convert(self.path, self.cfg, '/usr/bin/false')
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertFalse(self.path.with_suffix('.webp').exists())
        self.assertFalse(list(self.path.parent.glob('.shrinx-*')))


if __name__ == '__main__':
    unittest.main()
