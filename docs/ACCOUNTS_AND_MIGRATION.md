# Accounts, backups, and migration

Run commands from the repository root.

## Local accounts

Choose **Create account** in the app to make a local account with a name, email, and password of at least six characters. The email is a sign-in identifier; no email is sent. Every new account starts with an empty library. There are no roles, invitations, or cloud accounts.

Each upload belongs to its signed-in account. Lists, title searches, subject/month options, totals, edits, and playback check ownership on the server. Signed-out visitors cannot read library records or video files. Sessions persist across server restarts for up to 30 days; signing out revokes the current session.

Sign out before sharing the browser. Tabs share its session. Account isolation applies to the app; someone who can read the data folder through the operating system can read its database and videos.

## Optional fictional demo accounts

Normal startup does not create demonstration accounts or clips. To prepare a separate example library:

```bash
python3 seed_demo.py --data-dir .scratch/demo-library
python3 server.py --data-dir .scratch/demo-library --port 8766
```

The fictional account definitions live in `seed_demo.py`. The command creates missing accounts with stable fixture identities. Existing accounts matched by email retain their ID, name, password hash, clips, and edits. Repeated setup does not reset credentials or upload sample videos.

Before changing a nonempty database, setup creates a verified backup in a new sibling `backups/` folder. To choose the destination explicitly:

```bash
python3 seed_demo.py --data-dir .scratch/demo-library --backup-dir backups/before-demo-setup
```

The backup destination must be new and outside the live data folder. An existing destination, including a dangling symbolic link, is refused.

## Data format and older libraries

The original shared-library schema is version 1. The account schema is version 2: `users`, `sessions`, and `app_metadata` tables, plus a nullable `clips.owner_id` reference. Passwords are salted hashes. Recording dates remain separate from upload timestamps; missing recording dates stay null.

The current server upgrades version 1 storage without guessing an owner. Unowned legacy clips remain invisible until explicitly assigned. Running `seed_demo.py` assigns unowned legacy clips to the first demo account once, then records the `legacy_clips_assigned_to_demo_alex_v1` marker. Later runs preserve that assignment and do not claim subsequently unowned rows. No sample videos are inserted.

Back up an older library before opening it with a newer app version. Keep its matching earlier source if you need to return to that version.

## Backup and restore

```bash
python3 backup.py backup backups/my-snapshot
python3 backup.py restore backups/my-snapshot .local/restored-library
python3 server.py --data-dir .local/restored-library --port 8766
```

Use `--data-dir` on the backup command for a nondefault library. Both backup and restore require a new destination and leave the source unchanged. Failed copies remove their incomplete output.

The tool reads database schemas 1 and 2. It uses SQLite's snapshot API and copies each video referenced by that snapshot. All users, password hashes, sessions, ownership references, clip details, and migration markers are included. Restore checks database integrity, foreign-key references, file sizes, and SHA-256 checksums before completing.

Restore copies the database unchanged. Captured sessions retain their expiry times, so a browser with a matching cookie may remain signed in. Restoration does not silently revoke sessions. Output directories request mode 0700 and files mode 0600 on systems that support those permissions. Keep backups private.

## Return to an earlier version

1. Stop the server with **Ctrl+C**.
2. Keep the current data folder intact and restore the earlier backup into a new directory.
3. Run the matching earlier application source against that restored directory with `--data-dir`.

A database backup does not include application source. Restore a version 1 backup with the earlier server if the goal is to retain the shared-library schema; opening it with the current server upgrades it again. Stop one server before reusing its port for another version.
