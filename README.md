# Shrinx

A small macOS service that converts images to smaller WebP files on a schedule. It writes each WebP beside its original, never overwrites an existing WebP, and keeps originals by default. Optional cleanup sends verified originals to Trash.

## Install with Homebrew

Requires macOS. The formula installs Python, WebP tools, and ExifTool:

```sh
brew tap brandonscript/tap
brew install brandonscript/tap/shrinx
mkdir -p ~/.config/shrinx
cp "$(brew --prefix shrinx)/share/shrinx/config.example.json" ~/.config/shrinx/config.json
```

Copy the example only on first setup; do not overwrite an existing personal configuration. Edit `~/.config/shrinx/config.json` to choose folders and settings, then preview and run:

```sh
shrinx --version
shrinx run --dry-run
shrinx run
shrinx install
```

`shrinx install` enables the configured schedule (daily at 1 am by default). Package installation and upgrades do not start conversion or register a schedule. Use Shrinx's scheduler, not `brew services`. Its Homebrew paths remain stable across package upgrades; personal settings live outside the Cellar.

```sh
brew upgrade brandonscript/tap/shrinx
shrinx uninstall
brew uninstall shrinx
```

Run `shrinx uninstall` before removing the package to unregister its schedule. Configuration, logs, reports, and images are retained. Running `shrinx install` replaces the existing Shrinx user schedule, including one installed from a source checkout.

Override the Homebrew config path with `--config` (highest priority) or `SHRINX_CONFIG`:

```sh
shrinx --config /path/to/settings.json run
```

## Run from source

Requires Python 3.10+ and Google's WebP tools. Install [ExifTool](https://exiftool.org/) for metadata transfers and optional IPTC mapping:

```sh
brew install webp exiftool
cp config.example.json config.json
python3 shrinx.py run --dry-run
python3 shrinx.py run
```

The source checkout defaults to its local `config.json`, which is ignored by Git; share [config.example.json](config.example.json) instead. Homebrew's `shrinx` command supports the same arguments as `python3 shrinx.py` in the examples below.

Paths support `~` and spaces. Missing folders are skipped, subfolders are excluded unless `recursive` is true, and files younger than `minimum_age_seconds` (default 60) wait until a later run. Set `recursive: true` to include subfolders; the same per-type settings, metadata checks, and Trash policy apply at every level.

## File types and compression

Each type has independent settings. This is the `file_types` section from the example configuration:

```json
{
  "file_types": {
    "png":  { "enabled": true,  "mode": "lossy",    "quality": 95, "relative": false },
    "jpeg": { "enabled": true,  "mode": "lossy",    "quality": 80, "relative": false },
    "tiff": { "enabled": true,  "mode": "lossless", "relative": false },
    "bmp":  { "enabled": false, "mode": "lossless", "relative": false }
  }
}
```

Copy the relevant settings into your existing configuration; these snippets are not complete configurations on their own.

| Type | Extensions, case-insensitive | Supported input |
| --- | --- | --- |
| PNG | `.png` | Static images up to 8 bits per channel |
| JPEG | `.jpg`, `.jpeg`, `.jpe`, `.jfif`, `.jif`, `.jfi` | JPEG images accepted by `cwebp` |
| TIFF | `.tif`, `.tiff` | Classic, single-page, unsigned images up to 8 bits per channel; grayscale, RGB, or palette color |
| BMP | `.bmp` | Uncompressed 24-bit RGB, converted through macOS `sips` |

Animated/high-bit-depth PNGs, multi-page TIFFs, BigTIFF, TIFF SubIFDs, and other unsupported variants stay untouched. A disabled or omitted type is excluded from conversion, cleanup, and report counts. A legacy list such as `"file_types": ["png", "jpeg"]` also works, using the global mode and quality.

Available modes:

- `lossy`: use the type's `quality`, from 0 to 100.
- `lossless`: preserve decoded pixels. It cannot restore detail already lost in a JPEG.
- `auto`: try lossless and lossy, then keep the smaller output.

Missing mode/quality settings inherit the global values. Quality is an encoder setting, not a percentage of retained image quality. By default, `only_if_smaller: true` keeps the original if the final WebP, including metadata, would be the same size or larger.

### Relative size fallback

Set `relative: true` for a type to try a lower lossy quality **only when normal conversion would grow the file or leave it the same size**:

```json
{
  "file_types": {
    "jpeg": { "enabled": true, "mode": "lossy", "quality": 80, "relative": true }
  }
}
```

Normal conversion starts at quality 80. If that output is too large, Shrinx searches lower integer quality settings for an output at or below approximately **80% of the original file size**. If normal conversion is already smaller, it keeps that result; it does not force every image to become 20% smaller.

The search tests at most seven lower-quality candidates and selects the highest tested quality that meets the byte limit, including metadata. It is a bounded approximation, not an exhaustive optimum: encoded sizes are not perfectly monotonic. The selected quality and achieved size appear in the log. If no tested quality meets the target, the original is kept. Explicit lossless mode never falls back to lossy, and image dimensions are never reduced.

`relative` defaults to false. Lower fallback quality can visibly reduce detail.

## Metadata preservation

`metadata_policy: "skip"` is the default. Files with unhandled metadata stay untouched. `metadata_policy: "discard"` broadly permits unsupported metadata to be omitted; recognized metadata is still copied where possible. Prefer the targeted exemptions below when only a particular field is expendable.

| Metadata | Handling |
| --- | --- |
| PNG EXIF, ICC profiles, standard XMP text packets | Verified byte-for-byte before publishing or translating density |
| PNG `pHYs` and standard JPEG JFIF density | Mapped into EXIF using ExifTool; source-format density overrides conflicting EXIF density, with the decision logged |
| Zero JPEG density / zero PNG image offsets | Ignored as unusable or redundant; pixel dimensions are unchanged |
| PNG sRGB hints | Replaced in temporary encoder input with an explicit system sRGB ICC profile and the same rendering intent |
| PNG gamma/chromaticity hints with an authoritative ICC profile | Removed from temporary encoder input to avoid an extra gamma adjustment; the ICC profile is retained |
| Display P3 `cICP` hints | Accepted only when embedded ICC colorant, tone-curve, white-point, and adaptation tags exactly match the system Display P3 profile |
| Apple `iDOT` | Recognized 28-byte PNG offset bookkeeping is omitted; unknown layouts stay skipped |
| Other PNG text, unknown color hints, Photoshop editing data | Skipped unless explicitly exempted or supported by optional mapping |

Density transfers verify the new resolution and preserve other EXIF fields and ICC/XMP payloads. Without ExifTool, density-bearing files stay skipped under the default policy. Color-profile translation uses macOS system profiles; unsupported or conflicting color combinations stay skipped.

These checks cover recognized metadata paths, not every possible metadata format. Exemptions and `discard` never enable unsupported image variants such as multi-page TIFFs.

### Optional IPTC to XMP mapping

`map_iptc_to_xmp` defaults to false. Enable it to translate recognized Photoshop/IPTC fields into standard WebP XMP:

```json
{
  "map_iptc_to_xmp": true,
  "metadata_exemptions": []
}
```

Supported fields include title, description, keywords, creator, copyright, credit/source, headline/instructions, city/state/country, creation date/time, and digital-creation date/time. Encoding/version markers and IPTC checksums are treated as bookkeeping. Mapped values are read back and verified, other XMP fields are preserved, and EXIF/color profiles are checked for changes. Empty XMP placeholders may disappear during rewriting.

Conflicting creation timestamps choose the **older valid instant**, accounting for timezone offsets. Invalid calendar dates and future timestamps are rejected, as are midnight/date-only placeholders on January 1 of 1601, 1904, 1970, 1980, and 2001. Offset-free timestamps use the Mac's local timezone. If neither candidate is valid, the mapped date is omitted rather than invented. Each decision is logged.

Non-date conflicts and unmapped fields keep the original and are logged as failures. Exempt a specific field when its loss is acceptable.

### Metadata exemption examples

`metadata_exemptions` defaults to an empty list. Names are exact and case-sensitive. An exemption permits metadata loss; it does not promise a transfer.

**Keep IPTC mapping, but omit an unsupported urgency field and a Photoshop thumbnail:**

```json
{
  "map_iptc_to_xmp": true,
  "metadata_exemptions": [
    "iptc:Urgency",
    "jpeg:photoshop:0x040c"
  ]
}
```

Other unmapped Photoshop resources still cause a skip. Use `jpeg:photoshop:0xNNNN` with four lowercase hexadecimal digits to exempt a resource identified in the log.

**Allow Photoshop editing/printing data and Adobe application metadata to be omitted:**

```json
{
  "metadata_policy": "skip",
  "metadata_exemptions": [
    "jpeg:photoshop",
    "jpeg:adobe"
  ]
}
```

`jpeg:photoshop` exempts the entire APP13 block, including IPTC, and bypasses IPTC mapping even if `map_iptc_to_xmp` is true. `jpeg:adobe` exempts APP14. Existing EXIF, XMP, and ICC metadata handled by the normal path remains preserved.

**Omit an advisory OpenImageIO color label while retaining the embedded ICC profile:**

```json
{
  "metadata_exemptions": ["png:oiio-colorspace"]
}
```

This applies only to the PNG `oiio:ColorSpace` text label and requires a validated embedded ICC profile that survives encoding. Other text fields remain protected; it cannot discard the file's only color information.

Additional targeted exemptions:

| Exemption | Effect |
| --- | --- |
| `jpeg:comment` | Allow JPEG comments to be omitted |
| `iptc:FieldName` | Omit that IPTC field during mapping, for example `iptc:Caption-Abstract` |
| `iptc:DateCreated` and `iptc:TimeCreated` | Omit both parts of the IPTC creation timestamp |
| `iptc:DigitalCreationDate` and `iptc:DigitalCreationTime` | Omit both parts of the IPTC digital-creation timestamp |

Exemptions do not bypass file-size, image-validity, decode, or Trash checks. Unknown Photoshop layouts require the whole-block exemption. Personal exemptions belong in your ignored `config.json`, not the public example.

## Original cleanup and repeat runs

Enable `trash_originals: true` to send originals to macOS Trash after conversion verification. Trash is never emptied automatically; space is reclaimed only when you empty it.

Existing WebPs are never overwritten. If cleanup is enabled, Shrinx re-encodes the original with current settings and requires a byte-for-byte match before trashing it. Unrelated, stale, corrupt, or mismatched outputs leave the original untouched. Changing quality, metadata options, or color profiles can make an older output no longer qualify. Remove a generated WebP manually if you want to regenerate it.

Files with the same base name, such as `photo.png` and `photo.jpg`, share one `photo.webp` destination. Shrinx does not overwrite that destination to resolve the collision. Files changed during conversion are retried later, and concurrent runs from the same installation are skipped.

## Schedule

```sh
python3 shrinx.py install
python3 shrinx.py uninstall
```

Install registers a per-user LaunchAgent scheduled daily at `daily_at` (default `01:00`, Mac local time). It does not immediately convert files. The service runs while you are logged in; a calendar run missed during sleep runs after wake, but does not wake the Mac. Set `daily_at` to null to use `interval_seconds` instead.

Run install again after changing the schedule or moving the project/Python installation. Other configuration changes apply on the next run. Uninstall removes only the schedule.

Service logs default to `~/Library/Logs/Shrinx/service.log` and `error.log`. macOS may require Files and Folders or Full Disk Access permission for the scheduled Python executable to read your chosen folders. Check the logs if scheduled access fails. Logs are appended without automatic rotation.

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

## License

MIT; see [LICENSE](LICENSE).
