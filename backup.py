#!/usr/bin/env python3
"""Create verified local library snapshots, or restore one into a new folder."""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import sqlite3
import sys


APP_DIR = Path(__file__).resolve().parent
CLIP_ID = re.compile(r"[0-9a-f]{32}\Z")
CHUNK_SIZE = 1024 * 1024


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(CHUNK_SIZE), b""):
            result.update(chunk)
    return result.hexdigest()


def readonly_db(path: Path) -> sqlite3.Connection:
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"Missing regular database file: {path}")
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)


def clip_files(database: Path) -> dict[str, int]:
    with closing(readonly_db(database)) as db:
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("Database integrity check failed.")
        if db.execute("PRAGMA user_version").fetchone()[0] not in {1, 2}:
            raise ValueError("This backup tool supports ClipShelf database versions 1 and 2.")
        if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ValueError("Database ownership or session references are invalid.")
        files = {}
        for clip_id, size, original_filename in db.execute("SELECT id, size_bytes, original_filename FROM clips"):
            if not isinstance(clip_id, str) or not CLIP_ID.fullmatch(clip_id):
                raise ValueError("Database contains an invalid clip ID.")
            if not isinstance(size, int) or size <= 0:
                raise ValueError("Database contains an invalid video size.")
            suffix = next((extension for extension in (".mp4", ".mov")
                           if isinstance(original_filename, str) and original_filename.lower().endswith(extension)), None)
            if suffix is None:
                raise ValueError("Database contains an unsupported video filename.")
            files[f"{clip_id}{suffix}"] = size
        return files


def copy_video(source: Path, destination: Path, size: int, expected_hash: str | None = None) -> str:
    if source.is_symlink() or not source.is_file() or source.stat().st_size != size:
        raise ValueError(f"Video is missing or has the wrong size: {source.name}")
    shutil.copyfile(source, destination)
    destination.chmod(0o600)
    actual_hash = digest(destination)
    if destination.stat().st_size != size or (expected_hash and actual_hash != expected_hash):
        raise ValueError(f"Video verification failed: {source.name}")
    return actual_hash


def backup(source: Path, destination: Path) -> int:
    """Database snapshot first, then original immutable files referenced by it."""
    with closing(readonly_db(source / "library.sqlite3")) as original:
        destination.mkdir(mode=0o700, parents=True, exist_ok=False)
        try:
            database = destination / "library.sqlite3"
            database.touch(mode=0o600, exist_ok=False)
            with closing(sqlite3.connect(database)) as snapshot:
                original.backup(snapshot)
                snapshot.execute("PRAGMA journal_mode=DELETE")
            files = clip_files(database)
            (destination / "videos").mkdir(mode=0o700)
            manifest = {
                "format": "clipshelf-backup-v1",
                "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "database_sha256": digest(database),
                "videos": {},
            }
            for name, size in files.items():
                video_hash = copy_video(source / "videos" / name, destination / "videos" / name, size)
                manifest["videos"][name] = {"size_bytes": size, "sha256": video_hash}
            # This completion marker is written only after all files verify.
            (destination / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
            (destination / "manifest.json").chmod(0o600)
        except BaseException:
            shutil.rmtree(destination)
            raise
    return len(files)


def restore(source: Path, destination: Path) -> int:
    manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("format") != "clipshelf-backup-v1":
        raise ValueError("This is not a completed ClipShelf backup.")
    database = source / "library.sqlite3"
    files = clip_files(database)
    if digest(database) != manifest.get("database_sha256"):
        raise ValueError("Backup database checksum does not match.")
    records = manifest.get("videos")
    if not isinstance(records, dict) or set(records) != set(files):
        raise ValueError("Backup video list does not match its database.")
    for name, size in files.items():
        record = records[name]
        if not isinstance(record, dict) or record.get("size_bytes") != size or not re.fullmatch(r"[0-9a-f]{64}", str(record.get("sha256", ""))):
            raise ValueError(f"Invalid video manifest: {name}")
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    try:
        (destination / "videos").mkdir(mode=0o700)
        shutil.copyfile(database, destination / "library.sqlite3")
        (destination / "library.sqlite3").chmod(0o600)
        if digest(destination / "library.sqlite3") != manifest["database_sha256"]:
            raise ValueError("Restored database checksum does not match.")
        for name, size in files.items():
            copy_video(source / "videos" / name, destination / "videos" / name, size, records[name]["sha256"])
        clip_files(destination / "library.sqlite3")
    except BaseException:
        shutil.rmtree(destination)
        raise
    return len(files)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    save = commands.add_parser("backup", help="write a new backup directory; safe while ClipShelf is running")
    save.add_argument("destination", type=Path, help="new backup folder; must not exist")
    save.add_argument("--data-dir", type=Path, default=APP_DIR / "data")
    load = commands.add_parser("restore", help="restore a verified backup into a new data directory")
    load.add_argument("source", type=Path, help="completed backup folder")
    load.add_argument("destination", type=Path, help="new library folder; must not exist")
    args = parser.parse_args(argv)
    try:
        if args.command == "backup":
            count = backup(args.data_dir.resolve(), args.destination.absolute())
        else:
            count = restore(args.source.resolve(), args.destination.absolute())
    except (OSError, ValueError, sqlite3.Error) as error:
        print(f"Could not {args.command}: {error}", file=sys.stderr)
        return 1
    print(f"{args.command.capitalize()} complete: {count} clips in {args.destination.absolute()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
