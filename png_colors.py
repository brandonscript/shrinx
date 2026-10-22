"""Translate recognized PNG color hints into an explicit ICC profile."""
from pathlib import Path
import struct
import zlib

PROFILE_ROOT = Path('/System/Library/ColorSync/Profiles')
COLOR_TAGS = (b'rXYZ', b'gXYZ', b'bXYZ', b'rTRC', b'gTRC', b'bTRC', b'wtpt', b'chad')


def icc_tags(data):
    if len(data) < 132 or data[36:40] != b'acsp':
        raise ValueError('Invalid ICC profile')
    count = struct.unpack_from('>I', data, 128)[0]
    if count > (len(data) - 132) // 12:
        raise ValueError('Invalid ICC directory')
    tags = {}
    for i in range(count):
        tag, offset, size = struct.unpack_from('>4sII', data, 132 + i * 12)
        if offset + size > len(data):
            raise ValueError('Invalid ICC tag range')
        tags[tag] = data[offset:offset + size]
    return tags


def has_embedded_icc(path):
    with path.open('rb') as f:
        f.seek(8)
        while header := f.read(8):
            if len(header) != 8:
                return False
            size, tag = struct.unpack('>I4s', header)
            if tag == b'iCCP':
                return True
            if tag == b'IEND':
                return False
            f.seek(size + 4, 1)
    return False


def color_plan(path):
    chunks = {}
    with path.open('rb') as f:
        f.seek(8)
        while header := f.read(8):
            if len(header) != 8:
                raise ValueError('Truncated PNG')
            size, tag = struct.unpack('>I4s', header)
            if tag in (b'sRGB', b'gAMA', b'cHRM', b'cICP', b'iCCP'):
                if tag in chunks or size > 8_000_000:
                    raise ValueError('Duplicate or oversized PNG color metadata')
                chunks[tag] = f.read(size)
                if len(chunks[tag]) != size:
                    raise ValueError('Truncated PNG color metadata')
            else:
                f.seek(size, 1)
            f.seek(4, 1)
            if tag == b'IEND':
                break
    remove, profile = set(), None
    if b'iCCP' in chunks:
        body = chunks[b'iCCP'].split(b'\0', 1)[1]
        if not body or body[0] != 0:
            raise ValueError('Invalid ICC compression')
        profile = zlib.decompress(body[1:])
        icc_tags(profile)
        if b'sRGB' in chunks:
            raise ValueError('Conflicting explicit and implicit PNG profiles')
    elif b'sRGB' in chunks:
        intent = chunks[b'sRGB']
        if len(intent) != 1 or intent[0] > 3:
            raise ValueError('Invalid PNG rendering intent')
        data = bytearray((PROFILE_ROOT / 'sRGB Profile.icc').read_bytes())
        data[64:68] = struct.pack('>I', intent[0])
        data[84:100] = bytes(16)  # Clear profile ID after adjusting rendering intent.
        profile = bytes(data)
        remove.add(b'sRGB')
    if b'cICP' in chunks:
        if chunks[b'cICP'] != bytes((12, 13, 0, 1)) or not profile:
            raise ValueError('Unsupported PNG cICP color space')
        reference = icc_tags((PROFILE_ROOT / 'Display P3.icc').read_bytes())
        actual = icc_tags(profile)
        if any(actual.get(tag) != reference.get(tag) or tag not in actual for tag in COLOR_TAGS):
            raise ValueError('PNG cICP does not match embedded ICC color space')
        remove.add(b'cICP')
    if profile:
        # ICC (or equivalent cICP) governs rendering; these legacy hints are redundant.
        remove.update(tag for tag in (b'gAMA', b'cHRM') if tag in chunks)
    return remove, profile if b'iCCP' not in chunks else None


def prepare_png(source, destination, plan):
    remove, profile = plan
    if not remove and profile is None:
        return source
    def chunk(tag, data):
        return struct.pack('>I', len(data)) + tag + data + struct.pack('>I', zlib.crc32(tag + data))
    with source.open('rb') as src, destination.open('wb') as dst:
        dst.write(src.read(8))
        while header := src.read(8):
            size, tag = struct.unpack('>I4s', header)
            data = src.read(size + 4)
            if len(data) != size + 4:
                raise ValueError('Truncated PNG chunk')
            if tag not in remove:
                dst.write(header + data)
            if tag == b'IHDR' and profile is not None:
                dst.write(chunk(b'iCCP', b'Shrinx sRGB\0\0' + zlib.compress(profile)))
    return destination
