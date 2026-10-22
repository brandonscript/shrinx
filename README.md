# Shrinx

A small macOS scheduled service that creates WebP copies of PNG screenshots. Existing WebP files are never overwritten. Originals are retained by default. Enable `trash_originals` to move verified PNG originals to macOS Trash.

## Run

Requires Python 3.10+ and Google's WebP tools (`brew install webp`). From this folder:

```sh
cp config.example.json config.json
python3 shrinx.py run --dry-run
python3 shrinx.py run
```

Edit the local, gitignored `config.json` to choose folders and settings. Folder paths support `~` and spaces. Missing folders are skipped. By default subfolders are excluded, files younger than 60 seconds are skipped, and output is saved alongside the source only when smaller.

## Compression

- `mode: "auto"` tries lossless and lossy, then saves the smaller result.
- `mode: "lossy"` uses `quality` (default 95, range 0–100).
- `mode: "lossless"` preserves exact pixels, including transparent pixel colors.

Quality is an encoder setting, not a percentage of retained quality. Quality 95 is a starting point for visual review; fine colored text can still show artifacts. Auto chooses by size, not perceptual analysis. PNGs above 8 bits per channel and animated PNGs are skipped. Supported metadata is copied, but WebP cannot hold every PNG metadata field.

`only_if_smaller: false` allows larger outputs. Existing WebP files are skipped unless cleanup is enabled; cleanup verifies them against the current PNG and settings. Remove a generated WebP if you want to regenerate it with different settings. When `trash_originals` is false, both files are retained and total disk usage increases. When true, Shrinx decodes the generated WebP successfully before trashing the PNG. For existing WebPs it re-encodes the PNG with current settings and requires a byte-for-byte match before cleanup; unrelated, corrupt, or stale outputs leave the PNG untouched. Skipped PNGs remain. Trash is never emptied automatically; disk space is reclaimed when you empty it.

## Schedule

```sh
python3 shrinx.py install
python3 shrinx.py uninstall
```

Install registers a per-user LaunchAgent scheduled daily at `daily_at` (default `01:00`, Mac local time). Installation does not immediately convert files. It runs while you are logged in; a missed calendar run during sleep runs after wake, but it does not wake the Mac. Set `daily_at` to null to use `interval_seconds` instead. Run install again after changing the schedule or moving this project/Python installation. Other configuration changes apply on the next run. Uninstall removes only the schedule.

Logs: `~/Library/Logs/Shrinx/service.log` and `error.log`. macOS may require Files and Folders or Full Disk Access permission for the scheduled Python executable to read Desktop/Documents/Pictures. Check logs if a scheduled run cannot access a folder. Logs are appended without automatic rotation.

Concurrent runs from this installation are skipped. Outputs are published atomically without replacing existing files, and files changed during conversion are retried later. Failed files are logged and retried on the next run. Sources already smaller than WebP are also retried each run.

## Verify

```sh
python3 -m unittest discover -s tests -v
```

## Run reports

Each completed run writes a summary to the log and a timestamped JSON file in `reports_directory` (default `~/Library/Logs/Shrinx/reports`). Change this setting in `config.json`; no schedule reinstall is needed. Relative directories resolve beside the configuration file. Reports are retained until you remove them.

Reports include PNGs scanned, files successfully processed, WebPs created, PNGs trashed, skipped/failed counts, duration, and approximate compression savings in bytes and percent. Compression savings compare PNG and WebP sizes for successful conversions or verified cleanup during that run; skipped files are excluded. Dry runs report planned counts with zero savings. New WebPs and cleanup of existing WebPs are counted separately.

The report also records the current disk usage increase from new WebPs and the expected disk usage change after emptying Trash. Moving a PNG to Trash does not immediately free disk space. Sizes are logical file sizes, so filesystem allocation and compression may differ. Concurrent runs that skip because another run is active do not create a report.

## Contributing

Keep personal paths, screenshots, generated images, logs, and local configuration out of changes. Tests generate their own synthetic images and require `cwebp` and `dwebp` on `PATH`; without them, conversion tests are skipped. To verify a clean checkout, copy the example configuration and run the test command above. No Python packages are required.

## License

MIT; see [LICENSE](LICENSE).
