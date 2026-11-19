#!/usr/bin/env python3
"""Create two local example accounts and assign the legacy library once.

Existing accounts, passwords, clip details and media bytes are never reset.
Every invocation backs up nonempty state before schema or account changes.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys
import uuid

from auth import hash_password
from backup import backup, readonly_db
from server import Library


APP_DIR = Path(__file__).resolve().parent
LEGACY_ASSIGNMENT_MARKER = "legacy_clips_assigned_to_demo_alex_v1"
DEMO_USERS = (
    {"id": uuid.uuid5(uuid.NAMESPACE_URL, "clipshelf.local/demo/alex").hex,
     "name": "Alex", "email": "alex@clipshelf.test"},
    {"id": uuid.uuid5(uuid.NAMESPACE_URL, "clipshelf.local/demo/jamie").hex,
     "name": "Jamie", "email": "jamie@clipshelf.test"},
)


def has_records(database: Path) -> bool:
    if not database.exists():
        return False
    with closing(readonly_db(database)) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        return any(table in tables and db.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
                   for table in ("clips", "users"))


def seed_demo(data_dir: Path, backup_dir: Path | None = None) -> dict:
    data_dir = data_dir.resolve()
    saved_backup = None
    if has_records(data_dir / "library.sqlite3"):
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        saved_backup = (backup_dir or data_dir.parent / "backups" /
                        f"pre-seed-demo-{timestamp}-{uuid.uuid4().hex[:8]}").absolute()
        resolved_backup = saved_backup.resolve()
        if resolved_backup == data_dir or data_dir in resolved_backup.parents:
            raise ValueError("Place the backup outside the live data directory.")
        backup(data_dir, saved_backup)
    library = Library(data_dir)
    users = []
    with closing(library.connect()) as db, db:
        db.execute("BEGIN IMMEDIATE")
        for fixture in DEMO_USERS:
            existing = db.execute("SELECT id,name,email FROM users WHERE email = ?", (fixture["email"],)).fetchone()
            created = existing is None
            if created:
                if db.execute("SELECT 1 FROM users WHERE id = ?", (fixture["id"],)).fetchone():
                    raise ValueError("A demo account ID is already used by a different account; no accounts were changed.")
                db.execute("INSERT INTO users (id,name,email,password_hash,created_at) VALUES (?,?,?,?,?)",
                           (fixture["id"], fixture["name"], fixture["email"], hash_password("demo123"),
                            datetime.now(timezone.utc).isoformat()))
                existing = fixture
            users.append({key: existing[key] for key in ("id", "name", "email")} | {"created": created})
        marker = db.execute("SELECT value FROM app_metadata WHERE key = ?", (LEGACY_ASSIGNMENT_MARKER,)).fetchone()
        assigned = 0
        if marker is None:
            assigned = db.execute("UPDATE clips SET owner_id = ? WHERE owner_id IS NULL", (users[0]["id"],)).rowcount
            db.execute("INSERT INTO app_metadata (key,value) VALUES (?,?)", (LEGACY_ASSIGNMENT_MARKER, json.dumps({
                "owner_id": users[0]["id"], "assigned_clips": assigned,
                "completed_at": datetime.now(timezone.utc).isoformat(),
            }, sort_keys=True)))
    return {"users": users, "assigned_legacy_clips": assigned,
            "backup_dir": str(saved_backup) if saved_backup else None}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=APP_DIR / "data")
    parser.add_argument("--backup-dir", type=Path, help="new backup folder; defaults to a timestamped sibling backups folder")
    args = parser.parse_args(argv)
    try:
        result = seed_demo(args.data_dir, args.backup_dir)
    except (OSError, ValueError, sqlite3.Error) as error:
        print(f"Could not seed demo accounts: {error}", file=sys.stderr)
        return 1
    # Passwords and password hashes deliberately never appear in command output.
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
