# Shrinx

Make your screenshots take up less space.

Shrinx converts PNG, JPEG, TIFF, and BMP images to WebP, right beside the originals. Choose folders, compression settings, and whether to include subfolders. Originals stay put unless you enable verified cleanup to Trash.

## Install

Homebrew is the easiest way to install on macOS:

```sh
brew install brandonscript/tap/shrinx
```

Installation doesn't convert anything or start a background service.

## Get started

Create your configuration once:

```sh
mkdir -p ~/.config/shrinx
cp "$(brew --prefix shrinx)/share/shrinx/config.example.json" ~/.config/shrinx/config.json
```

Edit `~/.config/shrinx/config.json` to choose your folders. Preview what would happen, then run it:

```sh
shrinx run --dry-run
shrinx run
```

WebPs are saved only when smaller. Set `"trash_originals": true` when you're happy with the results, and `"recursive": true` to include subfolders. Originals go to Trash after verification; Shrinx never empties it.

## Run on a schedule

Run daily at 1 am with Homebrew services:

```sh
brew services start shrinx
```

Change `daily_at` in your configuration to choose another time. Stop with `brew services stop shrinx`. Starting the service waits for the next scheduled time.

For a single run later:

```sh
shrinx schedule-once --at "2026-10-23T01:00"
```

Use a future local date/time. See the [scheduling guide](docs/scheduling.md) for one-off jobs, source installs, and migrating an existing schedule.

## Reports

Each run logs file counts and estimated savings. JSON reports go to `~/Library/Logs/Shrinx/reports`; change `reports_directory` to put them elsewhere. Space occupied by originals is reclaimed after you empty Trash.

## More details

- [Compression, file types, and metadata exemptions](docs/configuration.md)
- [Scheduling and one-off jobs](docs/scheduling.md)
- [Commands, reports, and running from source](docs/usage.md)

## License

[MIT](LICENSE).
