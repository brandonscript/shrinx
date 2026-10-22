#!/usr/bin/env python3
"""Scheduled PNG to WebP conversion. Requires Google's cwebp executable."""
import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import plistlib
import shutil
import struct
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent
LABEL = 'dev.shrinx.screenshots'
LOG = logging.getLogger('shrinx')


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


def convert(path, cfg, encoder, dry_run=False, metrics=None):
    metrics = metrics if metrics is not None else new_metrics()
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
    if not png_supported(path):
        LOG.info('Skipped unsupported PNG (animated, high bit depth, or invalid): %s', path)
        return 'skipped'
    if dry_run:
        LOG.info('Would %s: %s', 'verify WebP and trash PNG' if cleanup else 'convert', path)
        return 'planned'
    # Same filesystem permits atomic publication without overwriting existing files.
    with tempfile.TemporaryDirectory(prefix='.shrinx-', dir=path.parent) as folder:
        modes = ('lossless', 'lossy') if cfg['mode'] == 'auto' else (cfg['mode'],)
        candidates = []
        for mode in modes:
            output = Path(folder) / f'{mode}.webp'
            flags = ['-lossless', '-exact'] if mode == 'lossless' else ['-q', str(cfg['quality']), '-alpha_q', '100', '-sharp_yuv']
            subprocess.run([encoder, '-quiet', '-m', '6', '-metadata', 'all', *flags, str(path), '-o', str(output)], check=True, capture_output=True, timeout=180)
            candidates.append(output)
        if existing:
            target_hash = digest(target)
            matches = [p for p in candidates if digest(p) == target_hash]
            if not matches:
                LOG.warning('Existing WebP does not match current PNG/settings; keeping PNG: %s', path)
                return 'skipped'
            winner = matches[0]
        else:
            winner = min(candidates, key=lambda p: p.stat().st_size)
        size = winner.stat().st_size
        if signature(path) != before:
            LOG.info('Source changed during conversion; retry next run: %s', path)
            return 'skipped'
        if cfg['only_if_smaller'] and size >= before[2]:
            LOG.info('PNG already smaller: %s', path)
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
                LOG.warning('Files changed before cleanup; keeping PNG: %s', path)
                return 'skipped'
            trash(path)
            metrics['pngs_trashed'] += 1
            metrics['trashed_png_bytes'] += before[2]
            metrics['source_bytes'] += before[2]
            metrics['webp_bytes'] += size
            LOG.info('Trashed PNG after verified conversion: %s', path)
            return 'trashed'
        metrics['source_bytes'] += before[2]
        metrics['webp_bytes'] += size
        return 'converted'


def new_metrics():
    return dict(webps_created=0, new_webp_bytes=0, pngs_trashed=0,
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
                  disk_usage_change_after_emptying_trash_bytes=metrics['new_webp_bytes'] - metrics['trashed_png_bytes'],
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
    LOG.info('Run report: processed %d/%d PNGs; created %d WebPs; trashed %d PNGs; skipped %d; failed %d; folder errors %d; compression savings %.2f MB (%.1f%%). Trash must be emptied to reclaim space. Report: %s',
             report['files_processed'], scanned, metrics['webps_created'], metrics['pngs_trashed'],
             counts['skipped'], counts['failed'], folder_errors, savings / 1_000_000, percent, path)
    return report


def run(cfg, config_path, dry_run):
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
            return 0
        started = datetime.now(timezone.utc)
        reports = Path(cfg.get('reports_directory', '~/Library/Logs/Shrinx/reports')).expanduser()
        if not reports.is_absolute():
            reports = config_path.resolve().parent / reports
        reports.mkdir(parents=True, exist_ok=True)
        metrics = new_metrics()
        scanned = 0
        folder_errors = 0
        counts = dict(converted=0, trashed=0, skipped=0, planned=0, failed=0)
        seen = set()
        for name in cfg['folders']:
            folder = Path(name).expanduser()
            if not folder.exists():
                LOG.info('Folder missing, skipping: %s', folder)
                continue
            try:
                paths = folder.rglob('*') if cfg['recursive'] else folder.iterdir()
                for path in paths:
                    if path.suffix.lower() != '.png' or not path.is_file() or path.resolve() in seen:
                        continue
                    seen.add(path.resolve())
                    scanned += 1
                    try:
                        counts[convert(path, cfg, encoder, dry_run, metrics)] += 1
                    except (ValueError, OSError, subprocess.SubprocessError) as e:
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
    payload = dict(Label=LABEL, ProgramArguments=[sys.executable, str(ROOT / 'shrinx.py'), '--config', str(config_path.resolve()), 'run'], ProcessType='Background', LowPriorityIO=True, Nice=10, StandardOutPath=str(logs / 'service.log'), StandardErrorPath=str(logs / 'error.log'), EnvironmentVariables={'PATH': '/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin'})
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
    parser.add_argument('--config', type=Path, default=ROOT / 'config.json')
    parser.add_argument('action', choices=['run', 'install', 'uninstall'], nargs='?', default='run')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    try:
        if args.action == 'uninstall':
            agent({}, args.config, args.action)
            return 0
        cfg = load_config(args.config)
        if args.action == 'run':
            return run(cfg, args.config, args.dry_run)
        agent(cfg, args.config, args.action)
        return 0
    except (ValueError, OSError, subprocess.SubprocessError) as e:
        LOG.error('%s', e)
        return 1


if __name__ == '__main__':
    sys.exit(main())
