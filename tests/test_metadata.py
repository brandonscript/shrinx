import json
from datetime import datetime, timezone
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import shrinx
from metadata import map_iptc, photoshop_resources, older_valid_date, valid_timestamp
from test_shrinx import JPEG_FIXTURE


class DatePolicyTests(unittest.TestCase):
    def test_invalid_placeholder_and_future_dates_are_rejected(self):
        now = datetime(2026, 10, 22, 20, 0, tzinfo=timezone.utc)
        for bad in ('2020:00:03 23:26:06-07:00', '2020:02:30', '2076:01:01',
                    '1904:01:01 00:00:00Z', '1970:01:01 00:00:00Z',
                    '1980:01:01', '2001:01:01 00:00:00Z', '1969:12:31 16:00:00-08:00', 'nonsense',
                    '2026:10:22 21:00:00Z'):
            with self.subTest(value=bad):
                self.assertIsNone(valid_timestamp(bad, now))
        self.assertIsNotNone(valid_timestamp('1980:06:15 12:00:00Z', now))
        self.assertIsNotNone(valid_timestamp('1950:05:10', now))

    def test_choose_older_valid_instant_with_timezones(self):
        self.assertEqual(older_valid_date('2020:03:07 15:49:54-08:00', '2020:00:03 15:49:54-08:00'), '2020:03:07 15:49:54-08:00')
        self.assertEqual(older_valid_date('2020:01:01 00:00:00+02:00', '2019:12:31 23:00:00Z'), '2020:01:01 00:00:00+02:00')
        self.assertIsNone(older_valid_date('2076:01:01', '1970:01:01'))


@unittest.skipUnless(shutil.which('exiftool') and shutil.which('cwebp'), 'ExifTool and WebP required')
class MetadataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'photo.jpg'
        self.path.write_bytes(JPEG_FIXTURE)
        self.cfg = shrinx.load_config(shrinx.ROOT / 'config.example.json')
        self.cfg.update(minimum_age_seconds=0, only_if_smaller=False, trash_originals=False, map_iptc_to_xmp=True)

    def write(self, *flags):
        subprocess.run(['exiftool', '-overwrite_original', *flags, str(self.path)], check=True, capture_output=True)

    def test_mapping_opt_in_and_field_values(self):
        self.write('-IPTC:Caption-Abstract=A city', '-IPTC:Keywords=roads', '-IPTC:Keywords+=trains',
                   '-IPTC:DateCreated=2020:03:29', '-IPTC:TimeCreated=23:26:06-07:00')
        self.assertIsNotNone(shrinx.unsupported_metadata(self.path))
        self.assertIsNone(shrinx.unsupported_metadata(self.path, self.cfg))
        original = self.path.read_bytes()
        self.assertEqual(shrinx.convert(self.path, self.cfg, shutil.which('cwebp')), 'converted')
        tags = json.loads(subprocess.check_output(['exiftool', '-j', '-s', '-n', '-XMP:all', str(self.path.with_suffix('.webp'))]))[0]
        self.assertEqual(tags['Description'], 'A city')
        self.assertEqual(tags['Subject'], ['roads', 'trains'])
        self.assertEqual(tags['DateCreated'], '2020:03:29 23:26:06-07:00')
        self.assertEqual(self.path.read_bytes(), original)

    def test_conflict_is_retained_unless_field_exempted(self):
        self.write('-IPTC:Caption-Abstract=IPTC text', '-XMP-dc:Description=Keep existing XMP')
        with self.assertRaisesRegex(ValueError, 'conflicts'):
            shrinx.convert(self.path, self.cfg, shutil.which('cwebp'))
        self.assertFalse(self.path.with_suffix('.webp').exists())
        self.cfg['metadata_exemptions'] = ['iptc:Caption-Abstract']
        self.assertEqual(shrinx.convert(self.path, self.cfg, shutil.which('cwebp')), 'converted')

    def test_block_exemption_without_mapping(self):
        self.write('-IPTC:Caption-Abstract=Explicitly discarded')
        self.cfg.update(map_iptc_to_xmp=False, metadata_exemptions=['jpeg:photoshop'])
        self.assertIsNone(shrinx.unsupported_metadata(self.path, self.cfg))
        self.assertEqual(shrinx.convert(self.path, self.cfg, shutil.which('cwebp')), 'converted')

    def test_unknown_fields_require_explicit_exemption(self):
        self.write('-IPTC:Urgency=3')
        with self.assertRaisesRegex(ValueError, 'Unmapped IPTC'):
            shrinx.convert(self.path, self.cfg, shutil.which('cwebp'))
        self.assertFalse(self.path.with_suffix('.webp').exists())
        self.cfg['metadata_exemptions'] = ['iptc:Urgency']
        self.assertEqual(shrinx.convert(self.path, self.cfg, shutil.which('cwebp')), 'converted')

    def test_older_date_selection_and_digital_creation_mapping(self):
        self.write('-IPTC:DateCreated=2020:03:29', '-IPTC:TimeCreated=23:26:06-07:00',
                   '-XMP-photoshop:DateCreated=2019:04:01 12:00:00-07:00',
                   '-IPTC:DigitalCreationDate=2022:07:20', '-IPTC:DigitalCreationTime=12:46:59')
        self.assertEqual(shrinx.convert(self.path, self.cfg, shutil.which('cwebp')), 'converted')
        tags = json.loads(subprocess.check_output(['exiftool', '-j', '-n', '-DateCreated', '-CreateDate', str(self.path.with_suffix('.webp'))]))[0]
        self.assertEqual(tags['DateCreated'], '2019:04:01 12:00:00-07:00')
        self.assertTrue(tags['CreateDate'].startswith('2022:07:20 12:46:59'))
        self.assertIsNotNone(valid_timestamp(tags['CreateDate']))

    def test_placeholder_xmp_is_replaced_with_valid_iptc(self):
        self.write('-IPTC:DateCreated=2020:03:29', '-XMP-photoshop:DateCreated=1980:01:01 00:00:00')
        self.assertEqual(shrinx.convert(self.path, self.cfg, shutil.which('cwebp')), 'converted')
        tags = json.loads(subprocess.check_output(['exiftool', '-j', '-DateCreated', str(self.path.with_suffix('.webp'))]))[0]
        self.assertEqual(tags['DateCreated'], '2020:03:29')

    def test_invalid_options_and_resources(self):
        path = self.path.parent / 'config.json'
        for key, value in [('map_iptc_to_xmp', 'yes'), ('metadata_exemptions', ['typo']), ('metadata_exemptions', 'jpeg:photoshop')]:
            cfg = {**self.cfg, key: value}
            path.write_text(json.dumps(cfg))
            with self.assertRaises(ValueError):
                shrinx.load_config(path)
        with self.assertRaises(ValueError):
            photoshop_resources(b'Photoshop 3.0\0truncated')
