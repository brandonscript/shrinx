"""Opt-in, verified IPTC to XMP translation and explicit JPEG metadata exemptions."""
import json
import logging
from datetime import datetime, timezone
import re
import shutil
import struct
import subprocess

IPTC_MAP = {
    'ObjectName': 'XMP-dc:Title', 'Caption-Abstract': 'XMP-dc:Description',
    'Keywords': 'XMP-dc:Subject', 'By-line': 'XMP-dc:Creator',
    'CopyrightNotice': 'XMP-dc:Rights', 'Credit': 'XMP-photoshop:Credit',
    'Source': 'XMP-photoshop:Source', 'City': 'XMP-photoshop:City',
    'Province-State': 'XMP-photoshop:State', 'Country-PrimaryLocationName': 'XMP-photoshop:Country',
    'Country-PrimaryLocationCode': 'XMP-iptcCore:CountryCode',
    'Headline': 'XMP-photoshop:Headline', 'SpecialInstructions': 'XMP-photoshop:Instructions',
}
LIST_TAGS = {'XMP-dc:Subject', 'XMP-dc:Creator'}
BOILERPLATE = {'CodedCharacterSet', 'ApplicationRecordVersion', 'EnvelopeRecordVersion'}


DATE_PAIRS = {
    ('DateCreated', 'TimeCreated'): 'XMP-photoshop:DateCreated',
    ('DigitalCreationDate', 'DigitalCreationTime'): 'XMP-xmp:CreateDate',
}
DATE_TAGS = set(DATE_PAIRS.values())
LOG = logging.getLogger('shrinx')
PLACEHOLDER_DAYS = {'1601-01-01', '1904-01-01', '1970-01-01', '1980-01-01', '2001-01-01'}


def valid_timestamp(value, now=None):
    if not isinstance(value, str) or not re.fullmatch(
            r'\d{4}[:-]\d{2}[:-]\d{2}(?:[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)?', value):
        return None
    normalized = value[:10].replace(':', '-') + value[10:]
    try:
        parsed = datetime.fromisoformat(normalized.replace('Z', '+00:00'))
        if parsed.strftime('%Y-%m-%d') in PLACEHOLDER_DAYS and parsed.hour == parsed.minute == parsed.second == parsed.microsecond == 0:
            return None
        # Offset-free metadata uses the Mac's local timezone at that date.
        aware = parsed.astimezone(timezone.utc)
        if aware.strftime('%Y-%m-%d') in PLACEHOLDER_DAYS and aware.hour == aware.minute == aware.second == aware.microsecond == 0:
            return None
        if aware > (now or datetime.now(timezone.utc)):
            return None
        return aware
    except (ValueError, OverflowError, OSError):
        return None


def older_valid_date(iptc, xmp, now=None):
    candidates = [(stamp, value) for value in (iptc, xmp)
                  if (stamp := valid_timestamp(value, now)) is not None]
    return min(candidates, key=lambda pair: pair[0])[1] if candidates else None


def photoshop_resources(payload):
    if not payload.startswith(b'Photoshop 3.0\0'):
        raise ValueError('Unknown Photoshop APP13 header')
    pos = 14
    resources = []
    while pos < len(payload):
        if payload[pos:pos + 4] != b'8BIM' or pos + 7 > len(payload):
            raise ValueError('Invalid Photoshop resource')
        resource = struct.unpack('>H', payload[pos + 4:pos + 6])[0]
        name_size = payload[pos + 6]
        pos += 6 + ((name_size + 2) // 2) * 2
        if pos + 4 > len(payload):
            raise ValueError('Truncated Photoshop resource')
        size = struct.unpack('>I', payload[pos:pos + 4])[0]
        pos += 4
        if pos + size + size % 2 > len(payload):
            raise ValueError('Truncated Photoshop resource payload')
        resources.append(resource)
        pos += size + size % 2
    return resources


def read_tags(tool, path, group):
    values = json.loads(subprocess.check_output(
        [tool, '-j', '-n', '-s', '-G1', '-' + group + ':all', str(path)], timeout=60))[0]
    values.pop('SourceFile', None)
    return values


def map_iptc(source, output, exemptions):
    tool = shutil.which('exiftool')
    if not tool:
        raise ValueError('ExifTool required for IPTC mapping')
    iptc = read_tags(tool, source, 'IPTC')
    planned = {}
    fields = {key.split(':', 1)[-1]: value for key, value in iptc.items()}
    for name, value in fields.items():
        if name in BOILERPLATE or 'iptc:' + name in exemptions:
            continue
        if any(name in pair for pair in DATE_PAIRS):
            continue
        if name not in IPTC_MAP:
            raise ValueError('Unmapped IPTC field: ' + name)
        planned[IPTC_MAP[name]] = value
    for (date_name, time_name), tag in DATE_PAIRS.items():
        if date_name in fields and 'iptc:' + date_name not in exemptions:
            value = fields[date_name]
            if time_name in fields and 'iptc:' + time_name not in exemptions:
                value += ' ' + fields[time_name]
            planned[tag] = value
        elif time_name in fields and 'iptc:' + time_name not in exemptions:
            raise ValueError('IPTC ' + time_name + ' cannot map without ' + date_name)
    for tag in ('EXIF', 'ICC_Profile', 'XMP'):
        original = subprocess.check_output([tool, '-b', '-' + tag, str(source)], timeout=60)
        encoded = subprocess.check_output([tool, '-b', '-' + tag, str(output)], timeout=60)
        # Density translation may intentionally rewrite EXIF before this mapping.
        if tag != 'EXIF' and original and original != encoded:
            raise ValueError('Source metadata missing before IPTC mapping: ' + tag)
    before = read_tags(tool, output, 'XMP')
    changed_dates = set()
    for tag in DATE_TAGS.intersection(planned):
        iptc_date, existing = planned[tag], before.get(tag)
        chosen = older_valid_date(iptc_date, existing)
        planned[tag] = chosen
        if chosen != existing:
            changed_dates.add(tag)
        LOG.info('Date selection %s: IPTC=%r, XMP=%r, selected=%r (oldest valid, nonfuture timestamp)',
                 tag, iptc_date, existing, chosen)
    flags = []
    for tag, value in planned.items():
        if tag in DATE_TAGS:
            if value != before.get(tag):
                flags.append('-' + tag + '=' + (value or ''))
            continue
        if tag in before:
            left = before[tag]
            same = ([left] if not isinstance(left, list) else left) == ([value] if not isinstance(value, list) else value) if tag in LIST_TAGS else left == value
            if not same:
                raise ValueError('Existing XMP conflicts with mapped IPTC: ' + tag)
            continue
        values = value if isinstance(value, list) else [value]
        for i, item in enumerate(values):
            flags.append('-' + tag + ('+=' if i else '=') + str(item))
    if not flags:
        return
    # Ensure that adding XMP does not alter EXIF or embedded color profiles.
    binary = lambda tag: subprocess.check_output([tool, '-b', '-' + tag, str(output)], timeout=60)
    preserved = {tag: binary(tag) for tag in ('EXIF', 'ICC_Profile')}
    subprocess.run([tool, '-overwrite_original', *flags, str(output)], check=True, capture_output=True, timeout=60)
    after = read_tags(tool, output, 'XMP')
    for tag, value in before.items():
        if tag != 'XMP-x:XMPToolkit' and tag not in changed_dates and value not in ('', [], None) and after.get(tag) != value:
            raise ValueError('Existing XMP changed: ' + tag)
    for tag, expected in planned.items():
        actual = after.get(tag)
        if tag in LIST_TAGS:
            actual = actual if isinstance(actual, list) else [actual]
            expected = expected if isinstance(expected, list) else [expected]
        if actual != expected:
            raise ValueError('IPTC mapping verification failed: ' + tag)
    for tag, value in preserved.items():
        if binary(tag) != value:
            raise ValueError('Metadata changed during IPTC mapping: ' + tag)
