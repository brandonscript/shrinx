# Shrinx

A small macOS scheduled service that creates WebP copies of PNG, JPEG, TIFF, and BMP images. Existing WebP files are never overwritten. Originals are retained by default. Enable `trash_originals` to move verified originals to macOS Trash.

## Run

Requires Python 3.10+ and Google's WebP tools (`brew install webp`). From this folder:

```sh
cp config.example.json config.json
python3 shrinx.py run --dry-run
python3 shrinx.py run
```

Supported extensions: `.png`, `.jpg`, `.jpeg`, `.jpe`, `.jfif`, `.jif`, `.jfi`, `.tif`, `.tiff`, and `.bmp`, case-insensitively. Each type has its own compression settings and cleanup checks. TIFF support covers classic, single-page, unsigned images up to 8 bits per channel in grayscale, RGB, or palette color; multi-page, SubIFD, BigTIFF, high-bit-depth, and other variants stay untouched. Standard uncompressed 24-bit RGB BMPs are converted through macOS `sips`; other BMP variants stay untouched. Files with the same base name share one WebP destination; existing WebPs are never overwritten, and cleanup retains originals that do not match.

Edit the local, gitignored `config.json` to choose folders and settings. Folder paths support `~` and spaces. Missing folders are skipped. By default subfolders are excluded, files younger than 60 seconds are skipped, and output is saved alongside the source only when smaller.

## Enabled file types

Configure each type in your local `config.json`:

```json
"file_types": {
  "png":  { "enabled": true,  "mode": "lossy",    "quality": 95 },
  "jpeg": { "enabled": true,  "mode": "lossy",    "quality": 80 },
  "tiff": { "enabled": true,  "mode": "lossless" },
  "bmp":  { "enabled": false, "mode": "lossless" }
}
```

These are the example defaults. `enabled` switches conversion and cleanup on or off for that type and all its extension aliases. Omitted types are disabled. Disabled types are excluded from reports. Settings apply on the next run without reinstalling the schedule. A legacy list such as `["png", "jpeg"]` also works, using the global mode and quality.

## Compression

Each type can use `lossy` at its own `quality` (0–100), `lossless`, or `auto`, which tries both and saves the smaller result. Missing mode/quality values inherit the global settings. Quality is an encoder setting, not a percentage of retained quality. Lossless WebP from a JPEG preserves decoded pixels; it cannot restore quality previously lost in the JPEG.

`only_if_smaller: true` keeps the original when WebP would be larger. Existing outputs are never overwritten. Cleanup re-encodes the original using current settings and requires a byte-for-byte match before sending it to Trash; if settings changed, an existing WebP may no longer qualify. Skipped originals stay untouched. Trash is never emptied automatically.

## Metadata

`metadata_policy: "skip"` is the default: skip conversion and cleanup when the current conversion path detects metadata it cannot reliably retain. Set `metadata_policy: "discard"` to allow that loss. Supported metadata is still copied where possible.

Checks are conservative: PNG text, resolution/color hints and unknown chunks; JPEG comments, nonstandard application segments, thumbnails/density and trailing data; TIFF descriptive/profile/resolution tags; and BMP density/extended headers can cause a skip. This is a format-specific screening policy, not a universal metadata fidelity guarantee. Unsupported image variants (such as multi-page TIFFs) remain skipped under either policy.

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

Reports include images scanned, files successfully processed, WebPs created, originals trashed, skipped/failed counts, duration, and approximate compression savings in bytes and percent. Compression savings compare source and WebP sizes for successful conversions or verified cleanup during that run; skipped files are excluded. Dry runs report planned counts with zero savings. New WebPs and cleanup of existing WebPs are counted separately.

The report also records the current disk usage increase from new WebPs and the expected disk usage change after emptying Trash. Moving an original to Trash does not immediately free disk space. Sizes are logical file sizes, so filesystem allocation and compression may differ. Concurrent runs that skip because another run is active do not create a report.

## Contributing

Keep personal paths, screenshots, generated images, logs, and local configuration out of changes. Tests generate their own synthetic images and require `cwebp` and `dwebp` on `PATH`; without them, conversion tests are skipped. To verify a clean checkout, copy the example configuration and run the test command above. No Python packages are required.

## License

MIT; see [LICENSE](LICENSE).
