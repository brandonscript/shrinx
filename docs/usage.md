# Commands, reports, and development

[Back to Shrinx](../README.md)

## Run from source

Requires Python 3.10+ and Google's WebP tools. Install [ExifTool](https://exiftool.org/) for metadata transfers and optional IPTC mapping:

```sh
brew install webp exiftool
cp config.example.json config.json
python3 shrinx.py run --dry-run
python3 shrinx.py run
```

The source checkout defaults to its local `config.json`, which is ignored by Git; share [config.example.json](../config.example.json) instead. Homebrew's `shrinx` command supports the same arguments as `python3 shrinx.py` in the examples below.

Paths support `~` and spaces. Missing folders are skipped, subfolders are excluded unless `recursive` is true, and files younger than `minimum_age_seconds` (default 60) wait until a later run. Set `recursive: true` to include subfolders; the same per-type settings, metadata checks, and Trash policy apply at every level.

## Original cleanup and repeat runs

Enable `trash_originals: true` to send originals to macOS Trash after conversion verification. Trash is never emptied automatically; space is reclaimed only when you empty it.

Existing WebPs are never overwritten. If cleanup is enabled, Shrinx re-encodes the original with current settings and requires a byte-for-byte match before trashing it. Unrelated, stale, corrupt, or mismatched outputs leave the original untouched. Changing quality, metadata options, or color profiles can make an older output no longer qualify. Remove a generated WebP manually if you want to regenerate it.

Files with the same base name, such as `photo.png` and `photo.jpg`, share one `photo.webp` destination. Shrinx does not overwrite that destination to resolve the collision. Files changed during conversion are retried later, and concurrent runs from the same installation are skipped.

## Run reports

Each completed run logs counts and approximate savings, and writes a timestamped JSON report. Configure its directory with:

```json
{
  "reports_directory": "~/Library/Logs/Shrinx/reports"
}
```

Relative directories resolve beside the configuration file. This changes report storage, not the LaunchAgent's stdout/stderr log paths. Reports are retained until you remove them.

Reports include images scanned, successfully processed files, WebPs created, originals trashed, skipped/failed counts, folder errors, duration, and compression savings in bytes and percent. Skipped files are excluded from savings. Dry runs report planned counts with zero savings. New outputs and cleanup of existing outputs are counted separately.

They also distinguish new WebP disk usage from the expected change after emptying Trash. Sizes are logical file sizes; filesystem allocation/compression may differ. A run skipped because another run holds the lock does not create a report. Sources already smaller than WebP are retried on later runs.

## Verify and contribute

```sh
python3 -m unittest discover -s tests -v
```

Tests use synthetic fixtures and temporary directories. Conversion tests need `cwebp`/`dwebp`; metadata-transfer tests additionally need ExifTool, and one relative-mode integration test needs `cjpeg`. Tests needing unavailable tools or macOS profiles are skipped. No Python packages are required.

Keep personal paths, screenshots, generated images, logs, and local configuration out of commits. Keep the example configuration conservative: originals retained, metadata skipped when unsupported, mapping/relative fallback off, and exemptions empty.

