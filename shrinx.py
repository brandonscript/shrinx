#!/usr/bin/env python3
"""Scheduled PNG/JPEG/TIFF/BMP to WebP conversion. Requires Google's cwebp executable."""
import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import logging
import math
import os
from fractions import Fraction
from pathlib import Path
import plistlib
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import zlib
import re
from scheduling import serve, schedule_once, dispatch_once
from png_colors import color_plan, prepare_png, has_embedded_icc
from metadata import IPTC_MAP, BOILERPLATE, photoshop_resources, map_iptc

ROOT = Path(__file__).resolve().parent
VERSION = '0.1.1'
LABEL = 'dev.shrinx.screenshots'
LOG = logging.getLogger('shrinx')
JPEG_EXTENSIONS = {'.jpg', '.jpeg', '.jpe', '.jfif', '.jif', '.jfi'}
TIFF_EXTENSIONS = {'.tif', '.tiff'}
TYPE_EXTENSIONS = {'png': {'.png'}, 'jpeg': JPEG_EXTENSIONS,
                   'tiff': TIFF_EXTENSIONS, 'bmp': {'.bmp'}}
IMAGE_EXTENSIONS = set().union(*TYPE_EXTENSIONS.values())


def load_config(path):
    cfg = json.loads(path.read_text())
    if cfg.get('mode') not in ('auto', 'lossy', 'lossless'):
        raise ValueError('mode must be auto, lossy, or lossless')
    if not isinstance(cfg.get('quality'), (int, float)) or not 0 <= cfg['quality'] <= 100:
        raise ValueError('quality must be between 0 and 100')
    if not isinstance(cfg.get('folders'), list) or not cfg['folders'] or not all(isinstance(p, str) and p for p in cfg['folders']):
        raise ValueError('folders must be a nonempty list of paths')
    for key in ('recursive', 'only_if_smaller'):
        if not isinstance(cfg.get(key), bool):
            raise ValueError(f'{key} must be true or false')
    if not isinstance(cfg.get('trash_originals', False), bool):
        raise ValueError('trash_originals must be true or false')
    if not isinstance(cfg.get('reports_directory', '~/Library/Logs/Shrinx/reports'), str) or not cfg.get('reports_directory', '~/Library/Logs/Shrinx/reports').strip():
        raise ValueError('reports_directory must be a nonempty path')
    if cfg.get('metadata_policy', 'skip') not in ('skip', 'discard'):
        raise ValueError('metadata_policy must be skip or discard')
    if not isinstance(cfg.get('map_iptc_to_xmp', False), bool):
        raise ValueError('map_iptc_to_xmp must be true or false')
    exemptions = cfg.get('metadata_exemptions', [])
    allowed = {'jpeg:photoshop', 'jpeg:adobe', 'jpeg:comment', 'png:oiio-colorspace'} | {'iptc:' + name for name in set(IPTC_MAP) | BOILERPLATE | {'DateCreated', 'TimeCreated'}}
    if not isinstance(exemptions, list) or any(not isinstance(e, str) or (e not in allowed and not re.fullmatch(r'(?:jpeg:photoshop:0x[0-9a-f]{4}|iptc:[A-Za-z][A-Za-z0-9-]*)', e)) for e in exemptions):
        raise ValueError('Invalid metadata_exemptions entry')
    file_types = cfg.get('file_types', list(TYPE_EXTENSIONS))
    if not isinstance(file_types, (list, dict)) or any(not isinstance(t, str) or t not in TYPE_EXTENSIONS for t in file_types):
        raise ValueError('file_types must contain only png, jpeg, tiff, and/or bmp')
    if isinstance(file_types, dict):
        for kind, settings in file_types.items():
            if not isinstance(settings, dict) or set(settings) - {'enabled', 'mode', 'quality', 'relative'}:
                raise ValueError(f'invalid settings for {kind}')
            if not isinstance(settings.get('enabled', True), bool):
                raise ValueError(f'{kind}.enabled must be true or false')
            if settings.get('mode', cfg['mode']) not in ('auto', 'lossy', 'lossless'):
                raise ValueError(f'invalid mode for {kind}')
            if not isinstance(settings.get('relative', False), bool):
                raise ValueError(f'{kind}.relative must be true or false')
            quality = settings.get('quality', cfg['quality'])
            if isinstance(quality, bool) or not isinstance(quality, (int, float)) or not 0 <= quality <= 100:
                raise ValueError(f'invalid quality for {kind}')
    schedule = cfg.get('daily_at')
    if schedule is not None:
        try:
            hour, minute = map(int, schedule.split(':'))
            if not 0 <= hour <= 23 or not 0 <= minute <= 59:
                raise ValueError()
        except (ValueError, AttributeError):
            raise ValueError('daily_at must be HH:MM in local time')
    for key in ('interval_seconds', 'minimum_age_seconds'):
        if not isinstance(cfg.get(key), int) or cfg[key] < (60 if key == 'interval_seconds' else 0):
            raise ValueError(f'invalid {key}')
    return cfg


def png_supported(path):
    """Avoid silently flattening animation or reducing high-bit-depth originals."""
    with path.open('rb') as f:
        if f.read(8) != b'\x89PNG\r\n\x1a\n':
            return False
        first = True
        while True:
            header = f.read(8)
            if len(header) != 8:
                return False
            size, kind = struct.unpack('>I4s', header)
            if first:
                if kind != b'IHDR' or size != 13:
                    return False
                data = f.read(13)
                if len(data) != 13 or data[8] > 8:
                    return False
                f.seek(4, 1)
                first = False
                continue
            if kind == b'acTL':
                return False
            if kind == b'IDAT':
                return True
            if size > path.stat().st_size:
                return False
            f.seek(size + 4, 1)


def tiff_supported(path):
    """Accept classic, single-page, unsigned 8-bit-or-less TIFFs only."""
    with path.open('rb') as f:
        header = f.read(8)
        if len(header) != 8 or header[:2] not in (b'II', b'MM'):
            return False
        endian = '<' if header[:2] == b'II' else '>'
        magic, offset = struct.unpack(endian + 'HI', header[2:])
        if magic != 42 or offset < 8:
            return False
        f.seek(offset)
        raw = f.read(2)
        if len(raw) != 2:
            return False
        count = struct.unpack(endian + 'H', raw)[0]
        tags = {}
        for _ in range(count):
            entry = f.read(12)
            if len(entry) != 12:
                return False
            tag, kind, n = struct.unpack(endian + 'HHI', entry[:8])
            if tag == 330:  # SubIFDs may hold additional images.
                return False
            if tag not in (258, 262, 277, 339):
                continue
            if kind != 3 or not 0 < n <= 4:
                return False
            if n * 2 <= 4:
                data = entry[8:8 + n * 2]
            else:
                position = f.tell()
                f.seek(struct.unpack(endian + 'I', entry[8:])[0])
                data = f.read(n * 2)
                f.seek(position)
            if len(data) != n * 2:
                return False
            tags[tag] = struct.unpack(endian + 'H' * n, data)
        next_ifd = f.read(4)
        return (len(next_ifd) == 4 and struct.unpack(endian + 'I', next_ifd)[0] == 0
                and all(0 < b <= 8 for b in tags.get(258, (1,)))
                and tags.get(262, (0,))[0] in (0, 1, 2, 3)
                and tags.get(277, (1,))[0] <= 4
                and all(v == 1 for v in tags.get(339, (1,))))


def bmp_supported(path):
    # Restrict sips conversion to standard 24-bit RGB BMPs, avoiding alpha ambiguity.
    with path.open('rb') as f:
        header = f.read(54)
    return (len(header) == 54 and header[:2] == b'BM'
            and struct.unpack_from('<I', header, 14)[0] in (40, 108, 124)
            and struct.unpack_from('<HHI', header, 26) == (1, 24, 0))


def image_supported(path):
    if path.suffix.lower() == '.png':
        return png_supported(path)
    if path.suffix.lower() in JPEG_EXTENSIONS:
        with path.open('rb') as f:
            return f.read(3) == b'\xff\xd8\xff'
    if path.suffix.lower() in TIFF_EXTENSIONS:
        return tiff_supported(path)
    if path.suffix.lower() == '.bmp':
        return bmp_supported(path)
    return False


def png_xmp(data):
    keyword, body = data.split(b'\0', 1)
    if keyword != b'XML:com.adobe.xmp' or len(body) < 4 or body[0] not in (0, 1) or body[1] != 0:
        raise ValueError('Unsupported PNG text metadata')
    language, translated, text = body[2:].split(b'\0', 2)
    if language or translated:
        raise ValueError('PNG XMP language/translated keyword would be lost')
    return zlib.decompress(text) if body[0] else text


def verify_png_metadata(source, output):
    """Require byte-exact EXIF, ICC and XMP payloads before any translation."""
    expected = {}
    with source.open('rb') as f:
        f.seek(8)
        while header := f.read(8):
            if len(header) != 8:
                raise ValueError('Truncated PNG chunk')
            size, kind = struct.unpack('>I4s', header)
            if kind not in (b'eXIf', b'iCCP', b'iTXt'):
                f.seek(size + 4, 1)
                if kind == b'IEND':
                    break
                continue
            data = f.read(size)
            f.read(4)
            if kind == b'eXIf':
                tag, payload = b'EXIF', data
            elif kind == b'iCCP':
                name, body = data.split(b'\0', 1)
                if not body or body[0] != 0:
                    raise ValueError('Unsupported ICC compression')
                tag, payload = b'ICCP', zlib.decompress(body[1:])
            else:
                tag, payload = b'XMP ', png_xmp(data)
            if tag in expected:
                raise ValueError('Duplicate PNG metadata payload')
            expected[tag] = payload
    actual = {}
    with output.open('rb') as f:
        header = f.read(12)
        if header[:4] != b'RIFF' or header[8:] != b'WEBP':
            raise ValueError('Invalid encoded WebP')
        while header := f.read(8):
            if len(header) != 8:
                raise ValueError('Truncated WebP chunk')
            tag, size = struct.unpack('<4sI', header)
            if tag in expected:
                if tag in actual:
                    raise ValueError('Duplicate WebP metadata payload')
                actual[tag] = f.read(size)
                f.seek(size % 2, 1)
            else:
                f.seek(size + size % 2, 1)
    if any(actual.get(tag) != payload for tag, payload in expected.items()):
        raise ValueError('PNG metadata did not survive WebP encoding unchanged')


def unsupported_metadata(path, cfg=None):
    cfg = cfg or {}
    exemptions = cfg.get('metadata_exemptions', [])
    """Conservative check for metadata the current conversion path cannot retain."""
    extension = path.suffix.lower()
    with path.open('rb') as f:
        if extension == '.png':
            try:
                handled_colors, profile = color_plan(path)
            except (OSError, ValueError, zlib.error, IndexError):
                return 'unsupported or conflicting PNG color metadata'
            f.seek(8)
            while True:
                header = f.read(8)
                if len(header) != 8:
                    return 'unreadable PNG chunks'
                size, kind = struct.unpack('>I4s', header)
                if kind in handled_colors:
                    f.seek(size + 4, 1)
                    continue
                if kind == b'oFFs':
                    data = f.read(size)
                    if data != bytes(9):
                        return 'nonzero PNG image offset'
                    f.seek(4, 1)
                    continue
                if kind == b'tEXt' and 'png:oiio-colorspace' in exemptions:
                    if size > path.stat().st_size:
                        return 'invalid PNG text size'
                    data = f.read(size)
                    if data.split(b'\0', 1)[0] != b'oiio:ColorSpace' or b'\0' not in data:
                        return 'unexempted PNG text metadata'
                    if not has_embedded_icc(path):
                        return 'advisory color exemption requires embedded ICC profile'
                    LOG.info('Exempting advisory oiio:ColorSpace label; retaining embedded ICC profile: %s', path)
                    f.seek(4, 1)
                    continue
                if kind == b'iTXt':
                    if size > path.stat().st_size:
                        return 'invalid PNG text size'
                    try:
                        png_xmp(f.read(size))
                    except (ValueError, zlib.error):
                        return 'unsupported PNG text metadata'
                    f.seek(4, 1)
                    continue
                if kind == b'iDOT':
                    if size != 28:
                        return 'unknown Apple PNG offset layout'
                    # PNG byte offsets are encoding bookkeeping, not portable metadata.
                    f.seek(size + 4, 1)
                    continue
                if kind == b'pHYs':
                    if size != 9 or not shutil.which('exiftool'):
                        return 'PNG resolution requires ExifTool'
                    data = f.read(size)
                    if len(data) != 9 or data[8] not in (0, 1) or 0 in struct.unpack('>II', data[:8]):
                        return 'unsupported PNG resolution'
                    f.seek(4, 1)
                    continue
                if kind not in (b'IHDR', b'PLTE', b'IDAT', b'IEND', b'tRNS', b'iCCP', b'eXIf'):
                    return 'PNG chunk ' + kind.decode('ascii', errors='replace')
                if kind == b'IEND':
                    return None
                if size > path.stat().st_size:
                    return 'invalid PNG chunk size'
                f.seek(size + 4, 1)
        if extension in JPEG_EXTENSIONS:
            data = f.read()
            position = 2
            while position < len(data):
                start = data.find(b'\xff', position)
                if start < 0:
                    return 'unreadable JPEG markers'
                position = start + 1
                while position < len(data) and data[position] == 255:
                    position += 1
                if position >= len(data):
                    return 'truncated JPEG marker'
                marker = data[position]
                position += 1
                if marker == 0 or 0xD0 <= marker <= 0xD7:
                    continue  # Escaped entropy bytes and restart markers.
                if marker == 0xD9:
                    return None if position == len(data) else 'JPEG trailing data'
                if position + 2 > len(data):
                    return 'truncated JPEG segment'
                size = int.from_bytes(data[position:position + 2], 'big')
                payload = data[position + 2:position + size]
                if size < 2 or position + size > len(data):
                    return 'invalid JPEG segment size'
                if marker == 0xE0:
                    if (len(payload) != 14 or not payload.startswith(b'JFIF\0')
                            or payload[7] not in (0, 1, 2) or payload[12:14] != b'\0\0'):
                        return 'JPEG thumbnail or unknown APP0'
                    if payload[7:14] != b'\0\0\1\0\1\0\0' and not shutil.which('exiftool'):
                        return 'JPEG density requires ExifTool'
                    if 0 in struct.unpack('>HH', payload[8:12]):
                        LOG.info('Ignoring unusable zero JPEG density: %s', path)
                elif marker == 0xE1:
                    if not payload.startswith((b'Exif\0\0', b'http://ns.adobe.com/xap/1.0/\0')):
                        return 'unknown JPEG APP1'
                elif marker == 0xE2:
                    if not payload.startswith(b'ICC_PROFILE\0'):
                        return 'unknown JPEG APP2'
                elif marker == 0xED:
                    if 'jpeg:photoshop' in exemptions:
                        LOG.info('Exempting Photoshop APP13 metadata: %s', path)
                    else:
                        try:
                            resources = photoshop_resources(payload)
                        except ValueError:
                            return 'unrecognized Photoshop resource layout'
                        for resource in resources:
                            key = f'jpeg:photoshop:0x{resource:04x}'
                            if key in exemptions:
                                LOG.info('Exempting %s: %s', key, path)
                            elif resource in (0x0404, 0x0425) and cfg.get('map_iptc_to_xmp') and shutil.which('exiftool'):
                                continue
                            else:
                                return 'unmapped Photoshop resource ' + key
                elif marker == 0xEE and 'jpeg:adobe' in exemptions:
                    LOG.info('Exempting Adobe APP14 metadata: %s', path)
                elif marker == 0xFE and 'jpeg:comment' in exemptions:
                    LOG.info('Exempting JPEG comment: %s', path)
                elif 0xE3 <= marker <= 0xEF or marker == 0xFE:
                    return 'JPEG APP/comment metadata'
                position += size
            return 'JPEG end marker missing'
        if extension in TIFF_EXTENSIONS:
            header = f.read(8)
            endian = '<' if header[:2] == b'II' else '>'
            f.seek(struct.unpack(endian + 'I', header[4:])[0])
            raw = f.read(2)
            if len(raw) != 2:
                return 'unreadable TIFF directory'
            # Only decoding structure is allowed; TIFF metadata is not reliably copied.
            structural = {254, 255, 256, 257, 258, 259, 262, 263, 266, 273, 277,
                          278, 279, 284, 292, 293, 317, 320, 322, 323, 324, 325,
                          338, 339, 347, 529, 530, 531, 532}
            for _ in range(struct.unpack(endian + 'H', raw)[0]):
                entry = f.read(12)
                if len(entry) != 12:
                    return 'truncated TIFF directory'
                tag = struct.unpack(endian + 'H', entry[:2])[0]
                if tag == 274 and entry[2:8] == struct.pack(endian + 'HI', 3, 1) and struct.unpack(endian + 'H', entry[8:10])[0] == 1:
                    continue
                if tag not in structural:
                    return 'TIFF metadata tag ' + str(tag)
            return None
        if extension == '.bmp':
            header = f.read(54)
            if struct.unpack_from('<I', header, 14)[0] != 40:
                return 'BMP extended color/profile metadata'
            if any(struct.unpack_from('<ii', header, 38)):
                return 'BMP pixel density'
    return None


def density_mapping(path):
    """Map physical density/aspect ratio to standard EXIF rationals and units."""
    with path.open('rb') as f:
        if path.suffix.lower() == '.png':
            f.seek(8)
            while header := f.read(8):
                if len(header) != 8:
                    break
                size, kind = struct.unpack('>I4s', header)
                if kind == b'pHYs' and size == 9:
                    data = f.read(9)
                    x, y, unit = struct.unpack('>IIB', data)
                    if unit == 1 and x and y:
                        return (Fraction(x, 100), Fraction(y, 100), 3)
                    if unit == 0 and x and y:
                        return (Fraction(x), Fraction(y), 1)
                    return None
                f.seek(size + 4, 1)
                if kind == b'IEND':
                    break
        elif path.suffix.lower() in JPEG_EXTENSIONS:
            data = f.read()
            position = 2
            while position + 4 <= len(data) and data[position] == 255:
                marker = data[position + 1]
                if marker == 0xDA:
                    break
                size = int.from_bytes(data[position + 2:position + 4], 'big')
                if size < 2:
                    break
                payload = data[position + 4:position + 2 + size]
                if marker == 0xE0 and len(payload) == 14 and payload.startswith(b'JFIF\0'):
                    unit = payload[7]
                    x, y = struct.unpack('>HH', payload[8:12])
                    if payload[7:14] == b'\0\0\1\0\1\0\0':
                        return None
                    if unit in (0, 1, 2) and x and y:
                        return Fraction(x), Fraction(y), {0: 1, 1: 2, 2: 3}[unit]
                position += 2 + size
    return None


def transfer_density(source, output, mapping):
    """Verify native metadata, translate density, then verify semantic preservation."""
    tool = shutil.which('exiftool')
    if not tool:
        raise ValueError('ExifTool is required to transfer resolution metadata')

    def binary(tag, path):
        return subprocess.check_output([tool, '-b', '-' + tag, str(path)], timeout=60)

    def exif(path):
        data = json.loads(subprocess.check_output(
            [tool, '-j', '-n', '-s', '-G1', '-EXIF:all', str(path)], timeout=60))[0]
        data.pop('SourceFile', None)
        return data

    original_exif = exif(source)
    # All metadata cwebp claims to copy must arrive unchanged before translation.
    preserved = {}
    for tag in ('EXIF', 'ICC_Profile', 'XMP'):
        raw = binary(tag, source)
        encoded = binary(tag, output)
        if raw and encoded != raw:
            raise ValueError(f'{tag} did not survive encoding unchanged')
        preserved[tag] = encoded
    x, y, unit = mapping
    destination = exif(output)
    # Source-format density wins over redundant/conflicting EXIF density.
    for name, expected in (('XResolution', float(x)), ('YResolution', float(y)), ('ResolutionUnit', unit)):
        values = [value for key, value in destination.items() if key.endswith(':' + name)]
        if any(value != expected for value in values):
            LOG.info('Using source-format %s=%s instead of EXIF values %s: %s', name, expected, values, source)
    subprocess.run([tool, '-overwrite_original', '-EXIF:XResolution=' + str(x),
                    '-EXIF:YResolution=' + str(y), '-EXIF:ResolutionUnit#=' + str(unit),
                    str(output)], check=True, capture_output=True, timeout=60)
    updated = exif(output)
    density_fields = {'IFD0:XResolution', 'IFD0:YResolution', 'IFD0:ResolutionUnit'}
    if any(updated.get(key) != value for key, value in original_exif.items() if key not in density_fields):
        raise ValueError('Existing EXIF fields changed during density transfer')
    for name, expected in (('XResolution', float(x)), ('YResolution', float(y)), ('ResolutionUnit', unit)):
        if not any(key.endswith(':' + name) and abs(value - expected) < 1e-7
                   for key, value in updated.items() if isinstance(value, (int, float))):
            raise ValueError('Resolution transfer verification failed: ' + name)
    for tag in ('ICC_Profile', 'XMP'):
        if preserved[tag] and binary(tag, output) != preserved[tag]:
            raise ValueError(tag + ' changed during density transfer')
    LOG.info('Verified density transfer into EXIF: %s', source)


def signature(path):
    s = path.stat()
    return (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns)


def digest(path):
    with path.open('rb') as f:
        checksum = hashlib.sha256()
        for block in iter(lambda: f.read(1024 * 1024), b''):
            checksum.update(block)
        return checksum.hexdigest()


def trash(path):
    # Native macOS Trash API; no Finder automation or permanent unlink.
    script = """ObjC.import('Foundation');
    function run(args) {
        var err = Ref();
        var ok = $.NSFileManager.defaultManager.trashItemAtURLResultingItemURLError(
            $.NSURL.fileURLWithPath(args[0]), Ref(), err);
        if (!ok) throw Error(ObjC.unwrap(err[0].localizedDescription));
    }"""
    subprocess.run(['/usr/bin/osascript', '-l', 'JavaScript', '-e', script, str(path.resolve())],
                   check=True, capture_output=True, timeout=60)


def type_settings(cfg, kind):
    types = cfg.get('file_types', list(TYPE_EXTENSIONS))
    if isinstance(types, list):
        return dict(enabled=kind in types, mode=cfg['mode'], quality=cfg['quality'], relative=False)
    options = types.get(kind, {})
    return dict(enabled=kind in types and options.get('enabled', True),
                mode=options.get('mode', cfg['mode']), quality=options.get('quality', cfg['quality']), relative=options.get('relative', False))


def relative_candidate(encode, quality, target_bytes):
    """Bounded search for the highest tested integer quality that meets the target."""
    if target_bytes <= 0:
        return None
    low, high, best = 0, max(0, math.ceil(quality) - 1), None
    while low <= high:
        q = (low + high) // 2
        output = encode(q)
        if output.stat().st_size <= target_bytes:
            best = output
            low = q + 1
        else:
            high = q - 1
    return best


def convert(path, cfg, encoder, dry_run=False, metrics=None):
    metrics = metrics if metrics is not None else new_metrics()
    kind = next((t for t, extensions in TYPE_EXTENSIONS.items() if path.suffix.lower() in extensions), None)
    if kind is None:
        return 'skipped'
    settings = type_settings(cfg, kind)
    if not settings['enabled']:
        return 'skipped'
    cfg = {**cfg, 'mode': settings['mode'], 'quality': settings['quality']}
    target = path.with_suffix('.webp')
    cleanup = cfg.get('trash_originals', False)
    if path.is_symlink() or target.is_symlink():
        return 'skipped'
    existing = target.exists()
    if existing and (not cleanup or not target.is_file()):
        return 'skipped'
    before = signature(path)
    if time.time() - path.stat().st_mtime < cfg['minimum_age_seconds']:
        return 'skipped'
    if not image_supported(path):
        LOG.info('Skipped unsupported image (unsupported format variant or invalid file): %s', path)
        return 'skipped'
    if cfg.get('metadata_policy', 'skip') == 'skip':
        reason = unsupported_metadata(path, cfg)
        if reason:
            LOG.info('Keeping original because metadata may be lost (%s): %s', reason, path)
            return 'skipped'
    if dry_run:
        LOG.info('Would %s: %s', 'verify WebP and trash original' if cleanup else 'convert', path)
        return 'planned'
    # Same filesystem permits atomic publication without overwriting existing files.
    with tempfile.TemporaryDirectory(prefix='.shrinx-', dir=path.parent) as folder:
        input_path = path
        if path.suffix.lower() == '.bmp':
            input_path = Path(folder) / 'input.png'
            subprocess.run(['/usr/bin/sips', '-s', 'format', 'png', str(path), '--out', str(input_path)],
                           check=True, capture_output=True, timeout=180)
            if not png_supported(input_path):
                raise ValueError('BMP conversion did not produce a supported PNG')
        if kind == 'png' and cfg.get('metadata_policy', 'skip') == 'skip':
            input_path = prepare_png(path, Path(folder) / 'normalized.png', color_plan(path))
        modes = ('lossless', 'lossy') if cfg['mode'] == 'auto' else (cfg['mode'],)
        mapping = density_mapping(path)
        if cfg.get('metadata_policy', 'skip') == 'discard' and not shutil.which('exiftool'):
            mapping = None
        candidates = []
        def encode(mode, quality, label):
            output = Path(folder) / f'{label}.webp'
            flags = ['-lossless', '-exact'] if mode == 'lossless' else ['-q', str(quality), '-alpha_q', '100', '-sharp_yuv']
            subprocess.run([encoder, '-quiet', '-m', '6', '-metadata', 'all', *flags, str(input_path), '-o', str(output)], check=True, capture_output=True, timeout=180)
            if kind == 'png' and cfg.get('metadata_policy', 'skip') == 'skip':
                verify_png_metadata(input_path, output)
            if mapping:
                transfer_density(path, output, mapping)
            if kind == 'jpeg' and cfg.get('map_iptc_to_xmp') and not {'jpeg:photoshop', 'jpeg:photoshop:0x0404'}.intersection(cfg.get('metadata_exemptions', [])):
                map_iptc(path, output, cfg.get('metadata_exemptions', []))
            candidates.append(output)
            return output
        for mode in modes:
            encode(mode, cfg['quality'], mode)
        initial = min(candidates, key=lambda p: p.stat().st_size)
        if settings['relative'] and cfg['mode'] != 'lossless' and initial.stat().st_size >= before[2]:
            target_bytes = min(int(before[2] * cfg['quality'] / 100), before[2] - 1)
            best = relative_candidate(lambda q: encode('lossy', q, f'lossy-q{q}'), cfg['quality'], target_bytes)
            if best is None:
                LOG.info('Relative size target unattainable; keeping original: %s', path)
                return 'skipped'
            # Only target-compliant trials may be selected or adopted for cleanup.
            candidates = [best]
            LOG.info('Relative mode: target %d bytes (%.1f%% of original); selected %s at %d bytes: %s',
                     target_bytes, cfg['quality'], best.stem, best.stat().st_size, path)
        if existing:
            target_hash = digest(target)
            matches = [p for p in candidates if digest(p) == target_hash]
            if not matches:
                LOG.warning('Existing WebP does not match current PNG/settings; keeping original: %s', path)
                return 'skipped'
            winner = matches[0]
        else:
            winner = min(candidates, key=lambda p: p.stat().st_size)
        size = winner.stat().st_size
        if signature(path) != before:
            LOG.info('Source changed during conversion; retry next run: %s', path)
            return 'skipped'
        if cfg['only_if_smaller'] and size >= before[2]:
            LOG.info('Original already smaller: %s', path)
            return 'skipped'
        os.chmod(winner, path.stat().st_mode & 0o777)
        os.utime(winner, ns=(path.stat().st_atime_ns, before[3]))
        if cleanup:
            decoder = shutil.which(cfg.get('dwebp', 'dwebp'))
            if not decoder:
                raise ValueError('dwebp is required before trashing originals')
            subprocess.run([decoder, '-quiet', str(winner), '-o', os.devnull],
                           check=True, capture_output=True, timeout=180)
        if not existing:
            try:
                os.link(winner, target)
            except FileExistsError:
                return 'skipped'
            metrics['webps_created'] += 1
            metrics['new_webp_bytes'] += size
            LOG.info('Created %s (%s; %d → %d bytes, %.1f%% smaller)', target, winner.stem, before[2], size, 100 * (1 - size / before[2]))
        if cleanup:
            if path.is_symlink() or target.is_symlink() or signature(path) != before or digest(target) != digest(winner):
                LOG.warning('Files changed before cleanup; keeping original: %s', path)
                return 'skipped'
            trash(path)
            metrics['originals_trashed'] += 1
            metrics['trashed_original_bytes'] += before[2]
            if path.suffix.lower() == '.png':
                metrics['pngs_trashed'] += 1
                metrics['trashed_png_bytes'] += before[2]
            elif path.suffix.lower() in JPEG_EXTENSIONS:
                metrics['jpegs_trashed'] += 1
            metrics['source_bytes'] += before[2]
            metrics['webp_bytes'] += size
            LOG.info('Trashed original after verified conversion: %s', path)
            return 'trashed'
        metrics['source_bytes'] += before[2]
        metrics['webp_bytes'] += size
        return 'converted'


def new_metrics():
    return dict(webps_created=0, new_webp_bytes=0, originals_trashed=0,
                trashed_original_bytes=0, jpegs_trashed=0, pngs_trashed=0,
                trashed_png_bytes=0, source_bytes=0, webp_bytes=0)


def write_report(directory, started, counts, metrics, scanned, folder_errors, dry_run):
    finished = datetime.now(timezone.utc)
    savings = metrics['source_bytes'] - metrics['webp_bytes']
    percent = 100 * savings / metrics['source_bytes'] if metrics['source_bytes'] else 0
    report = dict(started_at=started.isoformat(), finished_at=finished.isoformat(),
                  duration_seconds=round((finished - started).total_seconds(), 3),
                  dry_run=dry_run, status='failed' if counts['failed'] or folder_errors else 'ok',
                  files_scanned=scanned, files_processed=counts['converted'] + counts['trashed'],
                  counts=counts, folder_errors=folder_errors, **metrics,
                  compression_savings_bytes=savings, compression_savings_percent=round(percent, 2),
                  current_disk_usage_change_bytes=metrics['new_webp_bytes'],
                  disk_usage_change_after_emptying_trash_bytes=metrics['new_webp_bytes'] - metrics['trashed_original_bytes'],
                  note='Approximate file sizes; Trash still occupies disk space until emptied. Negative disk usage change means savings.')
    path = directory / ('run-' + started.strftime('%Y%m%dT%H%M%S.%fZ') + '.json')
    with tempfile.NamedTemporaryFile(mode='w', dir=directory, prefix='.report-', delete=False) as f:
        temporary = Path(f.name)
        try:
            json.dump(report, f, indent=2)
            f.write('\n')
            f.close()
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    LOG.info('Run report: processed %d/%d images; created %d WebPs; trashed %d originals; skipped %d; failed %d; folder errors %d; compression savings %.2f MB (%.1f%%). Trash must be emptied to reclaim space. Report: %s',
             report['files_processed'], scanned, metrics['webps_created'], metrics['originals_trashed'],
             counts['skipped'], counts['failed'], folder_errors, savings / 1_000_000, percent, path)
    return report


def run(cfg, config_path, dry_run, busy_exit_code=0):
    encoder = shutil.which(cfg.get('cwebp', 'cwebp'))
    if not encoder:
        raise ValueError('cwebp not found. Install with: brew install webp')
    lock_id = hashlib.sha256(str(ROOT).encode()).hexdigest()[:20]
    lock_path = Path(tempfile.gettempdir()) / f'shrinx-{os.getuid()}-{lock_id}.lock'
    with lock_path.open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            LOG.info('Another run is active')
            return busy_exit_code
        started = datetime.now(timezone.utc)
        reports = Path(cfg.get('reports_directory', '~/Library/Logs/Shrinx/reports')).expanduser()
        if not reports.is_absolute():
            reports = config_path.resolve().parent / reports
        reports.mkdir(parents=True, exist_ok=True)
        metrics = new_metrics()
        scanned = 0
        folder_errors = 0
        counts = dict(converted=0, trashed=0, skipped=0, planned=0, failed=0)
        enabled_extensions = set().union(*(extensions for kind, extensions in TYPE_EXTENSIONS.items() if type_settings(cfg, kind)['enabled']))
        seen = set()
        for name in cfg['folders']:
            folder = Path(name).expanduser()
            if not folder.exists():
                LOG.info('Folder missing, skipping: %s', folder)
                continue
            try:
                paths = folder.rglob('*') if cfg['recursive'] else folder.iterdir()
                for path in paths:
                    if path.suffix.lower() not in enabled_extensions or not path.is_file() or path.resolve() in seen:
                        continue
                    seen.add(path.resolve())
                    scanned += 1
                    try:
                        counts[convert(path, cfg, encoder, dry_run, metrics)] += 1
                    except (ValueError, zlib.error, OSError, subprocess.SubprocessError) as e:
                        LOG.error('Failed %s: %s %s', path, e, getattr(e, 'stderr', b''))
                        counts['failed'] += 1
            except OSError as e:
                LOG.error('Cannot scan %s: %s', folder, e)
                folder_errors += 1
        write_report(reports, started, counts, metrics, scanned, folder_errors, dry_run)
        return int(counts['failed'] > 0 or folder_errors > 0)


def agent(cfg, config_path, action):
    path = Path.home() / 'Library/LaunchAgents' / f'{LABEL}.plist'
    domain = f'gui/{os.getuid()}'
    if action == 'uninstall':
        subprocess.run(['launchctl', 'bootout', f'{domain}/{LABEL}'], capture_output=True)
        path.unlink(missing_ok=True)
        LOG.info('Schedule removed; images and configuration retained')
        return
    logs = Path.home() / 'Library/Logs/Shrinx'
    logs.mkdir(parents=True, exist_ok=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(Label=LABEL, ProgramArguments=[os.environ.get('SHRINX_PYTHON', sys.executable), os.environ.get('SHRINX_SCRIPT', str(ROOT / 'shrinx.py')), '--config', str(config_path.resolve()), 'run'], ProcessType='Background', LowPriorityIO=True, Nice=10, StandardOutPath=str(logs / 'service.log'), StandardErrorPath=str(logs / 'error.log'), EnvironmentVariables={'PATH': '/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin'})
    if cfg.get('daily_at') is not None:
        hour, minute = map(int, cfg['daily_at'].split(':'))
        payload['StartCalendarInterval'] = {'Hour': hour, 'Minute': minute}
    else:
        payload['StartInterval'] = cfg['interval_seconds']
    with path.open('wb') as f:
        plistlib.dump(payload, f)
    subprocess.run(['launchctl', 'bootout', f'{domain}/{LABEL}'], capture_output=True)
    subprocess.run(['launchctl', 'bootstrap', domain, str(path)], check=True)
    LOG.info('Installed schedule: %s while logged in', ('daily at ' + cfg['daily_at'] + ' local time') if cfg.get('daily_at') else ('every %d seconds' % cfg['interval_seconds']))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', action='version', version='%(prog)s ' + VERSION)
    parser.add_argument('--config', type=Path, default=Path(os.environ.get('SHRINX_CONFIG', str(ROOT / 'config.json'))).expanduser())
    parser.add_argument('action', choices=['run', 'service', 'schedule-once', 'install', 'uninstall'], nargs='?', default='run')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--once', action='store_true', help='Run service work once immediately')
    parser.add_argument('--at', help='Future ISO date/time for schedule-once (local time if no offset)')
    parser.add_argument('--dispatch-once', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    try:
        runner = lambda cfg, path, dry: run(cfg, path, dry, busy_exit_code=75)
        if args.dispatch_once:
            return dispatch_once(args.dispatch_once, load_config, runner)
        if args.action == 'service':
            return serve(args.config, load_config, runner, args.dry_run, args.once)
        if args.action == 'schedule-once':
            load_config(args.config)
            if not args.at:
                raise ValueError('schedule-once requires --at')
            command = [os.environ.get('SHRINX_PYTHON', sys.executable), os.environ.get('SHRINX_SCRIPT', str(ROOT / 'shrinx.py'))]
            identifier = schedule_once(args.config, args.at, command, args.dry_run)
            print(identifier)
            return 0
        if args.action == 'uninstall':
            agent({}, args.config, args.action)
            return 0
        cfg = load_config(args.config)
        if args.action == 'run':
            return run(cfg, args.config, args.dry_run)
        agent(cfg, args.config, args.action)
        return 0
    except KeyboardInterrupt:
        return 0
    except (ValueError, zlib.error, OSError, subprocess.SubprocessError) as e:
        LOG.error('%s', e)
        return 1


if __name__ == '__main__':
    sys.exit(main())
