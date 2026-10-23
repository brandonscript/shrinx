"""Foreground recurring service and persistent one-off macOS jobs."""
from datetime import datetime, timedelta, timezone
import json
import logging
import os
from pathlib import Path
import plistlib
import subprocess
import time
import uuid

LOG = logging.getLogger('shrinx')
LABEL = 'dev.shrinx.screenshots'


def next_due(cfg, now):
    if cfg.get('daily_at') is None:
        return now + timedelta(seconds=cfg['interval_seconds'])
    hour, minute = map(int, cfg['daily_at'].split(':'))
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return candidate if candidate > now else candidate + timedelta(days=1)


def serve(config_path, loader, runner, dry_run=False, once=False):
    if once:
        return runner(loader(config_path), config_path, dry_run)
    legacy = Path.home() / 'Library/LaunchAgents' / (LABEL + '.plist')
    if legacy.exists():
        raise ValueError('Legacy Shrinx schedule exists; run shrinx uninstall before starting Homebrew services')
    due, key = None, None
    LOG.info('Service started; waiting for configured schedule')
    while True:
        try:
            cfg = loader(config_path)
            schedule = (cfg.get('daily_at'), cfg['interval_seconds'])
            now = datetime.now()  # Mac local wall clock; daily DST changes follow it.
            if schedule != key:
                due, key = next_due(cfg, now), schedule
                LOG.info('Next scheduled run: %s local time', due.isoformat())
            if now >= due:
                result = runner(cfg, config_path, dry_run)
                due = datetime.now() + timedelta(seconds=60) if result == 75 else next_due(cfg, datetime.now())
                LOG.info('Run exit code %d; next run %s', result, due.isoformat())
        except (ValueError, OSError, json.JSONDecodeError):
            LOG.exception('Service configuration/run failed; retrying in 30 seconds')
        time.sleep(30)


def parse_when(value, now=None):
    try:
        target = datetime.fromisoformat(value).astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise ValueError('--at must be an ISO date/time, such as 2026-10-23T01:00')
    if target <= (now or datetime.now(timezone.utc)):
        raise ValueError('--at must be in the future')
    return target


def jobs_directory():
    return Path.home() / 'Library/Application Support/Shrinx/jobs'


def write_job(path, data):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, indent=2) + '\n')
    os.replace(temporary, path)


def schedule_once(config_path, when, command, dry_run=False):
    target = parse_when(when)
    identifier = uuid.uuid4().hex
    label = LABEL + '.once.' + identifier
    jobs = jobs_directory()
    jobs.mkdir(parents=True, exist_ok=True)
    job = jobs / (identifier + '.json')
    plist = Path.home() / 'Library/LaunchAgents' / (label + '.plist')
    plist.parent.mkdir(parents=True, exist_ok=True)
    logs = Path.home() / 'Library/Logs/Shrinx'
    logs.mkdir(parents=True, exist_ok=True)
    data = dict(id=identifier, label=label, at=target.isoformat(), config=str(config_path.resolve()),
                dry_run=dry_run, status='pending', plist=str(plist))
    write_job(job, data)
    payload = dict(Label=label, ProgramArguments=[*command, '--dispatch-once', str(job)],
                   RunAtLoad=True, StartInterval=60, ProcessType='Background', Nice=10,
                   StandardOutPath=str(logs / 'once.log'), StandardErrorPath=str(logs / 'once-error.log'),
                   EnvironmentVariables={'PATH': '/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin'})
    plist.write_bytes(plistlib.dumps(payload))
    try:
        subprocess.run(['launchctl', 'bootstrap', f'gui/{os.getuid()}', str(plist)], check=True, capture_output=True)
    except (OSError, subprocess.SubprocessError):
        plist.unlink(missing_ok=True)
        job.unlink(missing_ok=True)
        raise
    LOG.info('Scheduled one-off job %s for %s; config: %s', identifier, target.astimezone().isoformat(), config_path)
    return identifier


def dispatch_once(path, loader, runner):
    # Accept only jobs created in our own state directory; never clean arbitrary paths.
    if path.is_symlink():
        raise ValueError('Invalid one-off job path')
    path = path.resolve()
    if path.parent != jobs_directory().resolve():
        raise ValueError('Invalid one-off job path')
    data = json.loads(path.read_text())
    label = LABEL + '.once.' + data['id']
    if path.stem != data['id'] or not data['id'].isalnum() or data['label'] != label:
        raise ValueError('Invalid one-off job identity')
    if datetime.now(timezone.utc) < datetime.fromisoformat(data['at']):
        return 0
    if data['status'] != 'pending':
        return 0
    claim = path.with_suffix('.claim')
    try:
        fd = os.open(claim, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
    except FileExistsError:
        return 0
    config_path = Path(data['config'])
    try:
        cfg = loader(config_path)
        code = runner(cfg, config_path, data['dry_run'])
        if code == 75:  # Active conversion: keep the pending job for next minute.
            claim.unlink(missing_ok=True)
            return 0
        data.update(status='completed' if code == 0 else 'failed', exit_code=code,
                    finished_at=datetime.now(timezone.utc).isoformat())
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        data.update(status='failed', error=str(error), finished_at=datetime.now(timezone.utc).isoformat())
        code = 1
    write_job(path, data)
    claim.unlink(missing_ok=True)
    plist = Path.home() / 'Library/LaunchAgents' / (label + '.plist')
    plist.unlink(missing_ok=True)
    LOG.info('One-off job %s %s', data['id'], data['status'])
    # State is finalized before unloading ourselves, which can terminate this process.
    subprocess.run(['launchctl', 'bootout', f'gui/{os.getuid()}/{label}'], capture_output=True)
    return code
