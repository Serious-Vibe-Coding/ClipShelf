# Testing ClipShelf

Run commands from the repository root. The app and integration tests use only Python's standard library; no package installation is required.

## Integration suite

```bash
python3 -m unittest discover -s tests -v
```

Each test creates a temporary library and manages its own server process. Tests do not add examples to the default `data/` directory.

Coverage includes:

- Local signup, password validation and hashing, sign-in, expiry, sign-out, and persisted sessions.
- Ownership checks for lists, filter options, totals, metadata, uploads, and MP4/MOV playback.
- Title search, combined subject/month filters, unknown recording dates, metadata edits, and restart persistence.
- Container validation, byte ranges, interrupted uploads, invalid input, and local request restrictions.
- Version 1 migration, repeatable demo setup, and backup/restore of accounts, ownership, sessions, metadata, and original video bytes.

## Manual browser example

Start a separate library, then create a new local account in the browser:

```bash
python3 server.py --data-dir .scratch/manual-library --port 8766
```

Open [http://127.0.0.1:8766](http://127.0.0.1:8766). Upload the synthetic one-second clips in `tests/fixtures/` with contrasting metadata:

| File | Example title | Subject | Recording date |
| --- | --- | --- | --- |
| `desk-garden.mp4` | Morning garden | Home | 2024-02-29 |
| `city-walk.mp4` | City walk | Travel | 2025-06-15 |
| `city-quicktime.mov` | QuickTime example | Travel | Leave empty |

Add notes, search by part of a title, combine subject/month filters, and use **Date not recorded**. Play the clips and edit their details. Restart with the same data folder and check persistence.

Sign out, create a second account, and verify its empty library. Upload a different clip there and confirm it does not appear in the first account. With two tabs open, check that sign-out clears the library and player in both tabs.

The fixtures have played as H.264 video at 320 × 180 with one-second duration in browser checks. Playback of other codecs depends on the browser.

## Backup exercise

With the separate example library running:

```bash
python3 backup.py backup backups/manual-snapshot --data-dir .scratch/manual-library
python3 backup.py restore backups/manual-snapshot .scratch/manual-restored
python3 server.py --data-dir .scratch/manual-restored --port 8767
```

Check each account's clips, metadata, unknown dates, and playback after restoration. Backups include all accounts and captured sessions. Existing output destinations are refused; use a new folder for every attempt.

## Fixture regeneration

The committed fixtures are generated test patterns with no personal footage. Normal app use and tests do not require ffmpeg.

Recreate the MOV container from the included MP4 using only Python:

```bash
python3 tests/generate_fixtures.py --mov-only
```

Regenerating both MP4 patterns requires a local ffmpeg build with the `libopenh264` encoder:

```bash
python3 tests/generate_fixtures.py --ffmpeg /path/to/ffmpeg
```

The generator overwrites fixture files, so use it only when intentionally updating those assets. MOV validation tests also construct classic containers without `ftyp`, extended atom sizes, and a final media atom extending to the end of the file. Container checks validate structure without decoding or transcoding video.
