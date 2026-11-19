#!/usr/bin/env python3
"""ClipShelf: a local, dependency-free MP4 and MOV library.

Run ``python3 server.py`` and open http://127.0.0.1:8765.
Metadata is stored in SQLite; original video bytes stay in the data directory.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import date, datetime, timezone
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import socket
import sqlite3
import sys
import tempfile
import time
from urllib.parse import parse_qs, unquote, unquote_to_bytes, urlsplit
import uuid

from auth import (DUMMY_PASSWORD_HASH, SESSION_COOKIE, SESSION_SECONDS, SESSION_TOKEN,
                  hash_password, normalize_email, token_hash, validate_password,
                  validate_signup, verify_password)

MAX_CLIP_BYTES = 250 * 1024 * 1024
MAX_JSON_BYTES = 64 * 1024
CHUNK_SIZE = 1024 * 1024
MEDIA_TYPES = {".mp4": "video/mp4", ".mov": "video/quicktime"}
MAX_CONTAINER_ATOMS = 100_000
CLIP_ID = re.compile(r"[0-9a-f]{32}\Z")
ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}\Z", re.ASCII)
ISO_MONTH = re.compile(r"\d{4}-\d{2}\Z", re.ASCII)
STATIC_FILES = {"/": ("index.html", "text/html; charset=utf-8"),
                "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                "/styles.css": ("styles.css", "text/css; charset=utf-8")}


def valid_unicode(value: str) -> bool:
    try:
        value.encode("utf-8")
        return True
    except UnicodeError:
        return False


class RequestError(Exception):
    def __init__(self, status: int, message: str, headers: dict[str, str] | None = None):
        self.status, self.message, self.headers = status, message, headers or {}
        super().__init__(message)


def validate_details(payload: object) -> dict:
    if not isinstance(payload, dict):
        raise RequestError(400, "Clip details must be a JSON object.")
    if {"owner_id", "user_id", "owner", "user"}.intersection(payload):
        raise RequestError(400, "Clip ownership comes from the signed-in account and cannot be supplied or changed.")
    details: dict = {}
    for key, limit, label in (("title", 160, "Title"), ("subject", 80, "Subject")):
        value = payload.get(key)
        if not isinstance(value, str) or not value.strip():
            raise RequestError(400, f"{label} is required.")
        value = value.strip()
        if len(value) > limit or "\x00" in value or not valid_unicode(value):
            raise RequestError(400, f"{label} must be valid text of at most {limit} characters and contain no null characters.")
        details[key] = value
    recorded = payload.get("recording_date")
    if recorded is not None:
        if not isinstance(recorded, str) or not ISO_DATE.fullmatch(recorded):
            raise RequestError(400, "Recording date must be a real date in YYYY-MM-DD format, or null.")
        try:
            date.fromisoformat(recorded)
        except ValueError:
            raise RequestError(400, "Recording date must be a real calendar date.") from None
    details["recording_date"] = recorded
    notes = payload.get("notes", "")
    if not isinstance(notes, str) or len(notes) > 5000 or "\x00" in notes or not valid_unicode(notes):
        raise RequestError(400, "Notes must be text of at most 5,000 characters and contain no null characters.")
    details["notes"] = notes
    return details


def serialize_clip(row: sqlite3.Row) -> dict:
    result = dict(row)
    result.pop("owner_id", None)
    result["video_url"] = f"/private-media/{result['id']}"
    result["media_type"] = MEDIA_TYPES[media_suffix(result["original_filename"])]
    return result


def media_suffix(filename: str) -> str:
    # endswith also preserves the old server's accepted filename ".mp4".
    return next((suffix for suffix in MEDIA_TYPES if filename.lower().endswith(suffix)), "")


def validate_video_container(path: Path, suffix: str) -> None:
    """Check bounded atom structure without decoding or changing original bytes.

    QuickTime MOV can omit ftyp and start with wide/free, moov, or mdat.
    Both containers need movie metadata and embedded media. Atom sizes include
    their headers; extended sizes and a final atom extending to EOF are valid.
    See https://developer.apple.com/documentation/quicktime-file-format/atoms.
    Codec support remains the browser's responsibility.
    """
    size = path.stat().st_size
    found = set()
    atom_count = 0
    with path.open("rb") as source:
        position = 0
        while position < size:
            atom_count += 1
            if atom_count > MAX_CONTAINER_ATOMS or size - position < 8:
                raise RequestError(415, "This file does not contain a valid MP4 or MOV video container.")
            source.seek(position)
            header = source.read(8)
            atom_size, kind = int.from_bytes(header[:4], "big"), header[4:8]
            header_size = 8
            if atom_size == 1:
                extended = source.read(8)
                if len(extended) != 8:
                    raise RequestError(415, "The video contains an incomplete atom header.")
                atom_size, header_size = int.from_bytes(extended, "big"), 16
            elif atom_size == 0:
                atom_size = size - position
            if atom_size < header_size or position + atom_size > size:
                raise RequestError(415, "The video contains an invalid or truncated atom.")
            payload_size = atom_size - header_size
            if kind == b"ftyp":
                if payload_size < 8 or payload_size % 4:
                    raise RequestError(415, "The video has an invalid file-type atom.")
                found.add(kind)
            elif kind in {b"moov", b"mdat"} and payload_size > 0:
                found.add(kind)
            position += atom_size
    required = {b"moov", b"mdat"} | ({b"ftyp"} if suffix == ".mp4" else set())
    if not required.issubset(found):
        raise RequestError(415, "Choose a complete MP4 or MOV video with movie metadata and embedded media.")


class Library:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir.resolve()
        self.media_dir = self.data_dir / "videos"
        self.static_dir = Path(__file__).resolve().parent / "static"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.media_dir.mkdir(exist_ok=True)
        self.db_path = self.data_dir / "library.sqlite3"
        with closing(self.connect()) as db, db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > 2:
                raise sqlite3.DatabaseError("This library was created by a newer ClipShelf version.")
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("BEGIN IMMEDIATE")
            db.execute("""CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                expires_at INTEGER NOT NULL,
                created_at INTEGER NOT NULL
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS sessions_expiry ON sessions(expires_at)")
            db.execute("""CREATE TABLE IF NOT EXISTS app_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS clips (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                subject TEXT NOT NULL,
                recording_date TEXT,
                notes TEXT NOT NULL DEFAULT '',
                uploaded_at TEXT NOT NULL,
                original_filename TEXT NOT NULL,
                size_bytes INTEGER NOT NULL CHECK (size_bytes > 0),
                owner_id TEXT REFERENCES users(id)
            )""")
            columns = {row[1] for row in db.execute("PRAGMA table_info(clips)")}
            if "owner_id" not in columns:
                # NULL owners stay invisible until the explicit, backed-up seed
                # command makes the user's requested legacy-library assignment.
                db.execute("ALTER TABLE clips ADD COLUMN owner_id TEXT REFERENCES users(id)")
            db.execute("CREATE INDEX IF NOT EXISTS clips_subject ON clips(subject)")
            db.execute("CREATE INDEX IF NOT EXISTS clips_recording_date ON clips(recording_date)")
            db.execute("CREATE INDEX IF NOT EXISTS clips_owner ON clips(owner_id)")
            db.execute("PRAGMA user_version=2")

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path, timeout=15)
        db.row_factory = sqlite3.Row
        db.create_function("casefold", 1, lambda value: value.casefold(), deterministic=True)
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA foreign_keys=ON")
        return db

    @staticmethod
    def public_user(row: sqlite3.Row | dict) -> dict:
        return {key: row[key] for key in ("id", "name", "email")}

    def signup(self, payload: object) -> dict:
        try:
            name, email, password = validate_signup(payload)
        except ValueError as error:
            raise RequestError(400, str(error)) from None
        user_id = uuid.uuid4().hex
        password_digest = hash_password(password)
        try:
            with closing(self.connect()) as db, db:
                db.execute("INSERT INTO users (id,name,email,password_hash,created_at) VALUES (?,?,?,?,?)",
                           (user_id, name, email, password_digest, datetime.now(timezone.utc).isoformat()))
        except sqlite3.IntegrityError:
            raise RequestError(409, "An account with this email already exists. Sign in instead.") from None
        return {"id": user_id, "name": name, "email": email}

    def login(self, payload: object) -> dict:
        if not isinstance(payload, dict):
            raise RequestError(400, "Sign-in details must be a JSON object.")
        try:
            email = normalize_email(payload.get("email"))
            password = validate_password(payload.get("password"))
        except ValueError:
            raise RequestError(401, "Email or password is incorrect.") from None
        with closing(self.connect()) as db:
            user = db.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        valid = verify_password(password, user["password_hash"] if user else DUMMY_PASSWORD_HASH)
        if user is None or not valid:
            raise RequestError(401, "Email or password is incorrect.")
        return self.public_user(user)

    def create_session(self, user_id: str, previous_token: str | None = None) -> str:
        token = secrets.token_urlsafe(32)
        now = int(time.time())
        with closing(self.connect()) as db, db:
            db.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))
            if previous_token:
                db.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash(previous_token),))
            db.execute("INSERT INTO sessions (token_hash,user_id,expires_at,created_at) VALUES (?,?,?,?)",
                       (token_hash(token), user_id, now + SESSION_SECONDS, now))
        return token

    def session_user(self, token: str | None) -> dict | None:
        if token is None:
            return None
        with closing(self.connect()) as db:
            user = db.execute("""SELECT users.id,users.name,users.email FROM users JOIN sessions ON users.id = sessions.user_id
                WHERE sessions.token_hash = ? AND sessions.expires_at > ?""", (token_hash(token), int(time.time()))).fetchone()
        return self.public_user(user) if user else None

    def logout(self, token: str | None) -> None:
        if token:
            with closing(self.connect()) as db, db:
                db.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash(token),))

    def get_clip(self, clip_id: str, owner_id: str) -> dict:
        if not CLIP_ID.fullmatch(clip_id):
            raise RequestError(404, "Clip not found.")
        with closing(self.connect()) as db:
            row = db.execute("SELECT * FROM clips WHERE id = ? AND owner_id = ?", (clip_id, owner_id)).fetchone()
        if row is None:
            raise RequestError(404, "Clip not found.")
        return serialize_clip(row)

    def list_clips(self, query: dict[str, list[str]], owner_id: str) -> dict:
        q = query.get("q", [""])[0].strip()
        subject = query.get("subject", [""])[0]
        month = query.get("month", [""])[0]
        if len(q) > 160 or len(subject) > 80:
            raise RequestError(400, "Search title or subject is too long.")
        conditions, args = ["owner_id = ?"], [owner_id]
        if q:
            conditions.append("instr(casefold(title), ?) > 0")
            args.append(q.casefold())
        if subject:
            conditions.append("subject = ?")
            args.append(subject)
        if month == "undated":
            conditions.append("recording_date IS NULL")
        elif month:
            if not ISO_MONTH.fullmatch(month):
                raise RequestError(400, "Recording month must be YYYY-MM or undated.")
            try:
                date.fromisoformat(month + "-01")
            except ValueError:
                raise RequestError(400, "Recording month must be a real calendar month.") from None
            conditions.append("substr(recording_date, 1, 7) = ?")
            args.append(month)
        sql = "SELECT * FROM clips" + (" WHERE " + " AND ".join(conditions) if conditions else "")
        sql += " ORDER BY uploaded_at DESC, id DESC"
        with closing(self.connect()) as db:
            clips = [serialize_clip(row) for row in db.execute(sql, args)]
            subjects = [dict(row) for row in db.execute(
                "SELECT subject AS name, COUNT(*) AS count FROM clips WHERE owner_id = ? GROUP BY subject ORDER BY subject COLLATE NOCASE, subject", (owner_id,))]
            months = [row[0] for row in db.execute(
                "SELECT DISTINCT substr(recording_date, 1, 7) AS month FROM clips WHERE owner_id = ? AND recording_date IS NOT NULL ORDER BY month DESC", (owner_id,))]
            stats = dict(db.execute("""SELECT COUNT(*) AS total_clips,
                COALESCE(SUM(size_bytes), 0) AS total_bytes,
                COALESCE(SUM(recording_date IS NULL), 0) AS undated_clips FROM clips WHERE owner_id = ?""", (owner_id,)).fetchone())
        return {"clips": clips, "subjects": subjects, "months": months, "stats": stats}


class ClipShelfServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], library: Library):
        self.library = library
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ClipShelf/1.0"
    sys_version = ""

    @property
    def library(self) -> Library:
        return self.server.library

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(120)

    def log_message(self, message: str, *args) -> None:
        # Do not put titles, notes, or search terms in terminal logs.
        sys.stderr.write(f"ClipShelf {getattr(self, 'command', '-')} {getattr(self, '_log_route', '/')} {args[1] if len(args) > 1 else ''}\n")

    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Vary", "Cookie")
        if getattr(self, "_clear_cache", False):
            # Earlier ClipShelf versions cached shared /media URLs immutably.
            self.send_header("Clear-Site-Data", '"cache"')
        # Codex browser annotations inject a stylesheet into their shadow root.
        # Permit stylesheet elements while keeping scripts and style attributes
        # restricted by the existing script-src/style-src directives.
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; style-src-elem 'self' 'unsafe-inline'; img-src 'self' blob: data:; media-src 'self' blob:; connect-src 'self'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'; form-action 'self'")
        super().end_headers()

    def _check_origin(self) -> None:
        port = self.server.server_address[1]
        allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if port == 80:
            allowed |= {"127.0.0.1", "localhost"}
        hosts = self.headers.get_all("Host", [])
        if len(hosts) != 1 or hosts[0].lower() not in allowed:
            raise RequestError(403, "ClipShelf is only available on this computer through localhost or 127.0.0.1.")
        origins = self.headers.get_all("Origin", [])
        if origins and (len(origins) != 1 or origins[0].lower() != f"http://{hosts[0].lower()}"):
            raise RequestError(403, "This request must come from the local ClipShelf app.")
        if self.command in {"POST", "PATCH", "DELETE", "PUT"} and self.headers.get("Sec-Fetch-Site") == "cross-site":
            raise RequestError(403, "This request must come from the local ClipShelf app.")

    def _route(self) -> tuple[str, dict[str, list[str]]]:
        self._check_origin()
        try:
            target = urlsplit(self.path)
            if target.scheme or target.netloc or re.search(r"%(?![0-9A-Fa-f]{2})", target.path):
                raise ValueError
            path = unquote(target.path, encoding="utf-8", errors="strict")
            if "\x00" in path:
                raise ValueError
            query = parse_qs(target.query, keep_blank_values=True, max_num_fields=20, encoding="utf-8", errors="strict")
        except (ValueError, UnicodeError):
            raise RequestError(400, "The request URL is invalid.") from None
        self._clear_cache = path == "/" or path.startswith("/api/auth/")
        self._log_route = "/private-media/:id" if path.startswith("/private-media/") else "/media/:id" if path.startswith("/media/") else "/api/clips/:id" if path.startswith("/api/clips/") else path
        return path, query

    def _json(self, status: int, payload: dict, headers: dict[str, str] | None = None) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _run(self, action) -> None:
        self._clear_cache = False
        try:
            path, query = self._route()
            action(path, query)
        except RequestError as error:
            # There may be an unread request body; never parse it as another request.
            self.close_connection = True
            try:
                self._json(error.status, {"error": error.message}, error.headers)
            except (BrokenPipeError, ConnectionResetError, socket.timeout):
                pass
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            self.close_connection = True
        except (OSError, sqlite3.Error) as error:
            self.close_connection = True
            print(f"ClipShelf storage error: {type(error).__name__}", file=sys.stderr)
            self._json(500, {"error": "ClipShelf could not access local storage. Check free disk space and data-folder permissions."})

    def _session_token(self) -> str | None:
        headers = self.headers.get_all("Cookie", [])
        if len(headers) != 1:
            return None
        cookie = SimpleCookie()
        try:
            cookie.load(headers[0])
        except CookieError:
            return None
        value = cookie.get(SESSION_COOKIE)
        return value.value if value and SESSION_TOKEN.fullmatch(value.value) else None

    def _require_user(self) -> dict:
        user = self.library.session_user(self._session_token())
        if user is None:
            raise RequestError(401, "Sign in to access your clip library.")
        return user

    def _require_current_session(self, db: sqlite3.Connection, owner_id: str) -> None:
        """Call within an IMMEDIATE transaction before a private mutation.

        Streaming an upload can take minutes. A token revoked or expired during
        that stream must not authorize its final INSERT. The write lock orders
        this check/commit atomically against sign-out and account switching.
        """
        token = self._session_token()
        if token is None or db.execute("SELECT 1 FROM sessions WHERE token_hash = ? AND user_id = ? AND expires_at > ?",
                                      (token_hash(token), owner_id, int(time.time()))).fetchone() is None:
            raise RequestError(401, "Your session ended. Sign in and try again.")

    def _auth_post(self, path: str) -> None:
        payload = self._read_json()
        previous_token = self._session_token()
        if path == "/api/auth/logout":
            if not isinstance(payload, dict):
                raise RequestError(400, "Sign-out details must be a JSON object.")
            self.library.logout(previous_token)
            cookie = f"{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"
            self._json(200, {"user": None}, {"Set-Cookie": cookie})
            return
        user = self.library.signup(payload) if path == "/api/auth/signup" else self.library.login(payload)
        token = self.library.create_session(user["id"], previous_token)
        cookie = f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_SECONDS}"
        self._json(201 if path == "/api/auth/signup" else 200, {"user": user}, {"Set-Cookie": cookie})

    def _content_length(self, maximum: int) -> int:
        if self.headers.get("Transfer-Encoding"):
            raise RequestError(400, "Chunked uploads are not supported. Send a Content-Length header.")
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1 or not re.fullmatch(r"[0-9]+", lengths[0], re.ASCII):
            raise RequestError(411, "A valid Content-Length header is required.")
        if len(lengths[0]) > 20:
            raise RequestError(413, "The request body is too large.")
        length = int(lengths[0])
        if length <= 0:
            raise RequestError(400, "The request body is empty.")
        if length > maximum:
            raise RequestError(413, "Clips must be 250 MB or smaller." if maximum == MAX_CLIP_BYTES else "Clip details are too large.")
        return length

    def _read_json(self) -> object:
        if self.headers.get_content_type() != "application/json":
            raise RequestError(415, "Send clip details as application/json.")
        length = self._content_length(MAX_JSON_BYTES)
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise RequestError(400, "The request ended before all details arrived.")
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeError, ValueError, RecursionError):
            raise RequestError(400, "Clip details must contain valid UTF-8 JSON.") from None

    def do_GET(self) -> None:
        self._run(self._get)

    def do_HEAD(self) -> None:
        self._run(self._get)

    def _get(self, path: str, query: dict) -> None:
        if path == "/api/auth/session":
            self._json(200, {"user": self.library.session_user(self._session_token())})
        elif path == "/api/clips":
            self._json(200, self.library.list_clips(query, self._require_user()["id"]))
        elif path.startswith("/api/clips/"):
            self._json(200, self.library.get_clip(path[len("/api/clips/"):], self._require_user()["id"]))
        elif path.startswith("/private-media/"):
            self._media(path[len("/private-media/"):], self._require_user()["id"])
        elif path.startswith("/media/"):
            self._media(path[len("/media/"):], self._require_user()["id"])
        elif path in STATIC_FILES:
            name, content_type = STATIC_FILES[path]
            try:
                data = (self.library.static_dir / name).read_bytes()
            except FileNotFoundError:
                raise RequestError(404, "The app page was not found.") from None
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)
        elif path == "/favicon.ico":
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            raise RequestError(404, "Page not found.")

    def do_POST(self) -> None:
        self._run(self._post)

    def _post(self, path: str, query: dict) -> None:
        if path in {"/api/auth/signup", "/api/auth/login", "/api/auth/logout"}:
            self._auth_post(path)
            return
        if path != "/api/clips":
            raise RequestError(404, "Page not found.")
        owner_id = self._require_user()["id"]
        if self.headers.get_content_type() not in MEDIA_TYPES.values():
            raise RequestError(415, "Choose an MP4 or MOV video file.")
        length = self._content_length(MAX_CLIP_BYTES)
        metadata_headers = self.headers.get_all("X-Clip-Metadata", [])
        if len(metadata_headers) != 1:
            raise RequestError(400, "Clip details are missing. Add a title and subject.")
        encoded = metadata_headers[0]
        try:
            if re.search(r"%(?![0-9A-Fa-f]{2})", encoded):
                raise ValueError
            payload = json.loads(unquote_to_bytes(encoded).decode("utf-8"))
        except (UnicodeError, ValueError, RecursionError):
            raise RequestError(400, "Clip details must be URI-encoded UTF-8 JSON.") from None
        details = validate_details(payload)
        filename = payload.get("original_filename")
        if not isinstance(filename, str) or not filename or len(filename) > 255 or "\x00" in filename or not valid_unicode(filename):
            raise RequestError(400, "An original filename of at most 255 characters is required.")
        filename = filename.replace("\\", "/").rsplit("/", 1)[-1]
        suffix = media_suffix(filename)
        if suffix not in MEDIA_TYPES:
            raise RequestError(415, "Choose a file with an .mp4 or .mov extension.")
        clip_id = uuid.uuid4().hex
        final_path = self.library.media_dir / f"{clip_id}{suffix}"
        temp_path = None
        committed = False
        try:
            with tempfile.NamedTemporaryFile(mode="wb", prefix=".upload-", suffix=".tmp", dir=self.library.media_dir, delete=False) as output:
                temp_path = Path(output.name)
                remaining = length
                while remaining:
                    chunk = self.rfile.read(min(CHUNK_SIZE, remaining))
                    if not chunk:
                        raise RequestError(400, "The upload was interrupted. Please try again.")
                    output.write(chunk)
                    remaining -= len(chunk)
                output.flush()
                os.fsync(output.fileno())
            validate_video_container(temp_path, suffix)
            os.replace(temp_path, final_path)
            # Persist the rename before committing its database reference.
            directory_fd = os.open(self.library.media_dir, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            uploaded_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            with closing(self.library.connect()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                self._require_current_session(db, owner_id)
                db.execute("""INSERT INTO clips (id, title, subject, recording_date, notes, uploaded_at, original_filename, size_bytes, owner_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""", (clip_id, details["title"], details["subject"], details["recording_date"],
                    details["notes"], uploaded_at, filename, length, owner_id))
            committed = True
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
            if not committed:
                final_path.unlink(missing_ok=True)
        self._json(201, self.library.get_clip(clip_id, owner_id), {"Location": f"/api/clips/{clip_id}"})

    def do_PATCH(self) -> None:
        self._run(self._patch)

    def _patch(self, path: str, query: dict) -> None:
        if not path.startswith("/api/clips/"):
            raise RequestError(404, "Page not found.")
        owner_id = self._require_user()["id"]
        clip_id = path[len("/api/clips/"):]
        self.library.get_clip(clip_id, owner_id)
        details = validate_details(self._read_json())
        with closing(self.library.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            self._require_current_session(db, owner_id)
            db.execute("UPDATE clips SET title = ?, subject = ?, recording_date = ?, notes = ? WHERE id = ? AND owner_id = ?",
                       (details["title"], details["subject"], details["recording_date"], details["notes"], clip_id, owner_id))
        self._json(200, self.library.get_clip(clip_id, owner_id))

    def _media(self, clip_id: str, owner_id: str) -> None:
        clip = self.library.get_clip(clip_id, owner_id)
        suffix = media_suffix(clip["original_filename"])
        try:
            source = (self.library.media_dir / f"{clip_id}{suffix}").open("rb")
        except FileNotFoundError:
            raise RequestError(404, "The video file is missing from the local data folder.") from None
        with source:
            size = os.fstat(source.fileno()).st_size
            start, end, partial = 0, size - 1, False
            ranges = self.headers.get_all("Range", [])
            if ranges:
                match = re.fullmatch(r"bytes=(\d*)-(\d*)", ranges[0], re.ASCII) if len(ranges) == 1 else None
                if not match or not any(match.groups()) or any(len(value) > 20 for value in match.groups()):
                    raise RequestError(416, "This video range is not available.", {"Content-Range": f"bytes */{size}"})
                first, last = match.groups()
                if first:
                    start = int(first)
                    end = min(int(last), size - 1) if last else size - 1
                else:
                    suffix_bytes = int(last)
                    start = max(0, size - suffix_bytes)
                    end = size - 1
                    if suffix_bytes == 0:
                        start = size
                if start >= size or start > end:
                    raise RequestError(416, "This video range is not available.", {"Content-Range": f"bytes */{size}"})
                partial = True
            self.send_response(206 if partial else 200)
            self.send_header("Content-Type", clip["media_type"])
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Disposition", f'inline; filename="{clip_id}{suffix}"')
            if partial:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            if self.command == "HEAD":
                return
            source.seek(start)
            remaining = end - start + 1
            while remaining:
                chunk = source.read(min(CHUNK_SIZE, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def _unsupported(self) -> None:
        def reject(path: str, query: dict) -> None:
            raise RequestError(405, "This action is not supported.", {"Allow": "GET, HEAD, POST, PATCH"})
        self._run(reject)

    do_DELETE = do_PUT = do_OPTIONS = _unsupported


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run ClipShelf on this computer only.")
    parser.add_argument("--port", type=int, default=8765, help="local HTTP port (default: 8765)")
    parser.add_argument("--data-dir", type=Path, default=Path(__file__).resolve().parent / "data", help="folder for SQLite metadata and videos")
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")
    try:
        library = Library(args.data_dir)
        server = ClipShelfServer(("127.0.0.1", args.port), library)
    except (OSError, sqlite3.Error) as error:
        print(f"Could not start ClipShelf: {error}", file=sys.stderr)
        return 1
    print(f"ClipShelf is running at http://127.0.0.1:{server.server_address[1]}", flush=True)
    print(f"Local library: {library.data_dir}", flush=True)
    print("Press Ctrl+C to stop.", flush=True)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print("\nStopping ClipShelf.", flush=True)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
