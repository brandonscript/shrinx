# Scheduling

[Back to Shrinx](../README.md)

## Homebrew services

Create and edit `~/.config/shrinx/config.json`, then:

```sh
brew services start shrinx
brew services list
brew services stop shrinx
```

Homebrew manages a foreground `shrinx service` worker. The worker waits for `daily_at` (default `01:00`, Mac local time), or `interval_seconds` when `daily_at` is null. Starting or restarting does not immediately convert files. It reloads settings every 30 seconds; schedule changes reset the next due time. Folder/type changes apply to the next run.

The Mac must be awake and logged in. A daily time crossed during sleep runs after wake while the worker remains active; it does not wake the Mac. If the worker was stopped or you were logged out at the due time, restarting waits for the next scheduled time. Daily runs use the Mac's local wall clock, including daylight-saving changes.

Homebrew logs go to `$(brew --prefix)/var/log/shrinx.log` and `shrinx-error.log`. Run reports use your configuration's `reports_directory`. Do not use `sudo brew services`: this is a per-user service for your image folders. Stop the service before uninstalling the package.

## One run at a later time

```sh
shrinx schedule-once --at "2026-10-23T01:00"
shrinx schedule-once --at "2026-10-23T01:00" --dry-run
shrinx --config /path/to/settings.json schedule-once --at "2026-10-23T01:00-07:00"
```

`--at` accepts a future ISO timestamp. Without an offset, it means Mac local time. The command prints a job ID and registers an independent per-user LaunchAgent; Homebrew services need not be running. It checks the due time about once a minute, including after wake/login, and runs once when due. If another conversion is active, it defers until a later check.

The job stores the configuration **path**, not a snapshot. Edits before the run take effect. A failed conversion attempt is recorded as failed rather than silently rerun. Completed/failed agents unregister themselves. Job records remain in `~/Library/Application Support/Shrinx/jobs`; logs are in `~/Library/Logs/Shrinx/once.log` and `once-error.log`. A crash during a claimed job leaves a `.claim` file for inspection rather than risking duplicate cleanup.

To cancel a pending job manually, unload its label and remove its plist (substitute the printed ID):

```sh
launchctl bootout "gui/$(id -u)/dev.shrinx.screenshots.once.JOB_ID"
rm ~/Library/LaunchAgents/dev.shrinx.screenshots.once.JOB_ID.plist
```

Do not remove a running job's claim file without checking whether its conversion is still active.

## Migrating the earlier Shrinx schedule

The earlier `shrinx install` / `python3 shrinx.py install` commands created a separate user LaunchAgent. Remove it before enabling Homebrew services to avoid duplicate schedules:

```sh
python3 /path/to/shrinx/shrinx.py uninstall
brew services start shrinx
```

Keep your existing personal configuration; copy it to `~/.config/shrinx/config.json` if moving from a checkout. The new worker refuses to start while the legacy plist exists. This change does not automatically migrate or alter an existing schedule.

## Without Homebrew

Install Python 3.10+, WebP tools, and ExifTool. From the source checkout:

```sh
python3 shrinx.py service
python3 shrinx.py service --once --dry-run
python3 shrinx.py schedule-once --at "2026-10-23T01:00"
```

`service` runs in the foreground until interrupted; `service --once` does the work immediately and exits. For compatibility, the older `install` and `uninstall` commands remain available, but use only one recurring scheduler.
