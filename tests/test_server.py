"""Black-box integration checks. Every test uses its own temporary library."""

from __future__ import annotations

import hashlib
import http.client
from http.cookies import SimpleCookie
import json
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.parse import quote, urlencode


APP_DIR = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
VIDEO = (FIXTURES / "desk-garden.mp4").read_bytes()
SECOND_VIDEO = (FIXTURES / "city-walk.mp4").read_bytes()
MOV_VIDEO = (FIXTURES / "city-quicktime.mov").read_bytes()


class ClipShelfIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="clipshelf-test-")
        self.addCleanup(self.temp.cleanup)
        self.data_dir = Path(self.temp.name) / "library"
        self.log = open(Path(self.temp.name) / "server.log", "w+")
        self.addCleanup(self.log.close)
        self.process = None
        self.cookie = None
        self.addCleanup(self.stop_server)
        self.start_server()
        self.user = self.signup("Alex", "alex@example.test", "alex-test-password")[1]["user"]

    def start_server(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        self.process = subprocess.Popen(
            [sys.executable, str(APP_DIR / "server.py"), "--port", str(self.port),
             "--data-dir", str(self.data_dir)],
            cwd=APP_DIR, stdout=self.log, stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                self.log.seek(0)
                self.fail("Server exited during startup:\n" + self.log.read())
            try:
                status, _, _ = self.request("GET", "/api/auth/session", authenticated=False)
                if status == 200:
                    return
            except (OSError, http.client.HTTPException):
                pass
            time.sleep(0.03)
        self.log.seek(0)
        self.fail("Server did not become ready:\n" + self.log.read())

    def stop_server(self):
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)

    def request(self, method, path, body=None, headers=None, *, authenticated=True):
        headers = dict(headers or {})
        if authenticated and self.cookie and "Cookie" not in headers:
            headers["Cookie"] = self.cookie
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def json_request(self, method, path, body=None, headers=None, *, authenticated=True):
        status, response_headers, payload = self.request(method, path, body, headers, authenticated=authenticated)
        self.assertIn("application/json", response_headers.get("Content-Type", ""))
        return status, json.loads(payload)

    def auth_request(self, action, payload):
        status, headers, body = self.request(
            "POST", "/api/auth/" + action, json.dumps(payload).encode(),
            {"Content-Type": "application/json"},
        )
        result = json.loads(body)
        if status in (200, 201) and "Set-Cookie" in headers:
            cookie = SimpleCookie()
            cookie.load(headers["Set-Cookie"])
            morsel = cookie.get("clipshelf_session")
            self.cookie = "clipshelf_session=" + morsel.value if morsel and morsel.value else None
        return status, result, headers

    def signup(self, name, email, password):
        result = self.auth_request("signup", dict(name=name, email=email, password=password))
        self.assertEqual(result[0], 201, result[1])
        return result

    def login(self, email, password):
        result = self.auth_request("login", dict(email=email, password=password))
        self.assertEqual(result[0], 200, result[1])
        return result

    def listing(self, **filters):
        path = "/api/clips" + ("?" + urlencode(filters) if filters else "")
        status, result = self.json_request("GET", path)
        self.assertEqual(status, 200, result)
        return result

    def upload(self, *, title="Garden in the morning", subject="Home",
               recording_date="2024-02-29", notes="First spring leaves",
               original_filename="garden.mp4", video=VIDEO, extra_headers=None,
               extra_metadata=None):
        metadata = dict(title=title, subject=subject, recording_date=recording_date,
                        notes=notes, original_filename=original_filename)
        metadata.update(extra_metadata or {})
        headers = {
            "Content-Type": "video/quicktime" if original_filename.lower().endswith(".mov") else "video/mp4",
            "X-Clip-Metadata": quote(json.dumps(metadata, ensure_ascii=False), safe=""),
        }
        headers.update(extra_headers or {})
        return self.json_request("POST", "/api/clips", video, headers)

    def add_clip(self, **kwargs):
        status, clip = self.upload(**kwargs)
        self.assertEqual(status, 201, clip)
        return clip

    def patch(self, clip_id, **metadata):
        return self.json_request(
            "PATCH", "/api/clips/" + clip_id,
            json.dumps(metadata).encode("utf-8"), {"Content-Type": "application/json"},
        )

    def test_new_library_is_empty(self):
        result = self.listing()
        self.assertEqual(result["clips"], [])
        self.assertEqual(result["subjects"], [])
        self.assertEqual(result["months"], [])
        self.assertEqual(result["stats"], {
            "total_clips": 0, "total_bytes": 0, "undated_clips": 0,
        })

    def test_upload_unicode_and_missing_recording_date_stays_missing(self):
        clip = self.add_clip(
            title="週末の散歩 🌿", subject="旅行", recording_date=None,
            notes="A quiet café — no recording date was supplied.",
            original_filename="散歩.MP4", video=SECOND_VIDEO,
        )
        self.assertEqual(clip["title"], "週末の散歩 🌿")
        self.assertEqual(clip["subject"], "旅行")
        self.assertIsNone(clip["recording_date"])
        self.assertEqual(clip["original_filename"], "散歩.MP4")
        self.assertEqual(clip["size_bytes"], len(SECOND_VIDEO))
        self.assertTrue(clip["uploaded_at"])
        self.assertTrue(clip["video_url"].startswith("/private-media/"))
        status, stored = self.json_request("GET", "/api/clips/" + clip["id"])
        self.assertEqual(status, 200)
        self.assertEqual(stored, clip)
        result = self.listing(month="undated")
        self.assertEqual([row["id"] for row in result["clips"]], [clip["id"]])
        self.assertEqual(result["months"], [])
        self.assertEqual(result["stats"]["undated_clips"], 1)

    def test_optional_date_and_notes_can_be_omitted(self):
        metadata = {"title": "Unlabelled afternoon", "subject": "Home", "original_filename": "afternoon.mp4"}
        status, clip = self.json_request("POST", "/api/clips", VIDEO, {
            "Content-Type": "video/mp4",
            "X-Clip-Metadata": quote(json.dumps(metadata), safe=""),
        })
        self.assertEqual(status, 201, clip)
        self.assertIsNone(clip["recording_date"])
        self.assertEqual(clip["notes"], "")
        self.assertEqual(self.listing()["months"], [])

    def test_search_is_title_only_case_insensitive_and_literal(self):
        guitar = self.add_clip(title="Guitar rehearsal", notes="At the studio")
        self.add_clip(title="Coffee break", subject="Guitar", notes="Guitar rehearsal")
        percent = self.add_clip(title="100% practice")
        underscore = self.add_clip(title="take_2")
        self.assertEqual([c["id"] for c in self.listing(q="gUiTaR")["clips"]], [guitar["id"]])
        self.assertEqual([c["id"] for c in self.listing(q="%")["clips"]], [percent["id"]])
        self.assertEqual([c["id"] for c in self.listing(q="_")["clips"]], [underscore["id"]])
        self.assertEqual(self.listing(q="no such title")["clips"], [])

    def test_combined_subject_and_recording_month_filters_and_facets(self):
        target = self.add_clip(title="Beach walk", subject="Travel", recording_date="2023-07-05")
        self.add_clip(title="Forest walk", subject="Travel", recording_date="2024-07-05")
        self.add_clip(title="Morning walk", subject="Home", recording_date="2023-07-15")
        self.add_clip(title="City walk", subject="Travel", recording_date=None)
        result = self.listing(q="walk", subject="Travel", month="2023-07")
        self.assertEqual([c["id"] for c in result["clips"]], [target["id"]])
        self.assertEqual(result["months"], ["2024-07", "2023-07"])
        self.assertEqual({row["name"]: row["count"] for row in result["subjects"]},
                         {"Travel": 3, "Home": 1})
        self.assertEqual(result["stats"]["total_clips"], 4)
        self.assertEqual(result["stats"]["total_bytes"], 4 * len(VIDEO))
        self.assertEqual(len(self.listing(subject="Travel", month="undated")["clips"]), 1)
        self.assertEqual(self.listing(subject="Missing")["clips"], [])

    def test_edit_all_details_and_clear_recording_date(self):
        original = self.add_clip()
        status, edited = self.patch(
            original["id"], title="Renamed garden", subject="Plants",
            recording_date="2021-11-02", notes="Corrected after checking my diary.",
        )
        self.assertEqual(status, 200, edited)
        self.assertEqual(edited["title"], "Renamed garden")
        self.assertEqual(edited["subject"], "Plants")
        self.assertEqual(edited["recording_date"], "2021-11-02")
        self.assertEqual(edited["notes"], "Corrected after checking my diary.")
        self.assertEqual(edited["uploaded_at"], original["uploaded_at"])
        self.assertEqual(edited["video_url"], original["video_url"])
        self.assertEqual(self.listing(month="2024-02")["clips"], [])
        status, cleared = self.patch(
            original["id"], title=edited["title"], subject=edited["subject"],
            recording_date=None, notes="",
        )
        self.assertEqual(status, 200, cleared)
        self.assertIsNone(cleared["recording_date"])
        self.assertEqual(cleared["notes"], "")
        self.assertEqual(self.listing()["months"], [])
        self.assertEqual(self.listing(month="undated")["clips"][0]["id"], original["id"])

    def test_files_and_edits_survive_server_restart(self):
        clip = self.add_clip(video=SECOND_VIDEO)
        status, edited = self.patch(
            clip["id"], title="Saved title", subject="Saved subject",
            recording_date=None, notes="Saved notes",
        )
        self.assertEqual(status, 200, edited)
        self.stop_server()
        self.start_server()
        result = self.listing()
        self.assertEqual(result["clips"], [edited])
        status, headers, media = self.request("GET", edited["video_url"])
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Type"), "video/mp4")
        self.assertEqual(hashlib.sha256(media).digest(), hashlib.sha256(SECOND_VIDEO).digest())

    def test_mov_upload_playback_ranges_and_edits_survive_restart(self):
        mp4 = self.add_clip(title="Existing MP4")
        mov = self.add_clip(
            title="QuickTime city", subject="Travel", recording_date=None,
            original_filename="City.MOV", video=MOV_VIDEO,
        )
        self.assertEqual(mov["media_type"], "video/quicktime")
        self.assertEqual(mp4["media_type"], "video/mp4")
        self.assertTrue((self.data_dir / "videos" / (mov["id"] + ".mov")).is_file())
        status, headers, media = self.request("GET", mov["video_url"], headers={"Range": "bytes=8-31"})
        self.assertEqual(status, 206)
        self.assertEqual(headers["Content-Type"], "video/quicktime")
        self.assertEqual(headers["Content-Range"], f"bytes 8-31/{len(MOV_VIDEO)}")
        self.assertEqual(media, MOV_VIDEO[8:32])
        status, headers, media = self.request("HEAD", mov["video_url"])
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "video/quicktime")
        self.assertEqual(int(headers["Content-Length"]), len(MOV_VIDEO))
        self.assertTrue(headers["Content-Disposition"].endswith('.mov"'))
        self.assertEqual(media, b"")
        status, edited = self.patch(mov["id"], title="Saved MOV", subject="City", recording_date="2022-05-09", notes="Original QuickTime bytes")
        self.assertEqual(status, 200, edited)
        self.stop_server()
        self.start_server()
        self.assertEqual({row["id"] for row in self.listing()["clips"]}, {mp4["id"], mov["id"]})
        self.assertEqual(self.listing(subject="City", month="2022-05")["clips"], [edited])
        for clip, original, media_type in [(mp4, VIDEO, "video/mp4"), (edited, MOV_VIDEO, "video/quicktime")]:
            status, headers, media = self.request("GET", clip["video_url"])
            self.assertEqual(status, 200)
            self.assertEqual(headers["Content-Type"], media_type)
            self.assertEqual(hashlib.sha256(media).digest(), hashlib.sha256(original).digest())

    def test_mov_allows_classic_extended_and_final_to_eof_atoms(self):
        # Reuse reserved space in place of ftyp, preserving all chunk offsets.
        classic_mov = MOV_VIDEO[:4] + b"free" + MOV_VIDEO[8:]
        position = 0
        while MOV_VIDEO[position + 4:position + 8] != b"mdat":
            position += int.from_bytes(MOV_VIDEO[position:position + 4], "big")
        self.assertEqual(MOV_VIDEO[position - 8:position], b"\x00\x00\x00\x08free")
        # Use the preceding free atom's space to enlarge the media header;
        # the H.264 sample bytes retain their original offsets.
        extended_mov = (MOV_VIDEO[:position - 8] + b"\x00\x00\x00\x01mdat" +
                        (len(MOV_VIDEO) - position + 8).to_bytes(8, "big") + MOV_VIDEO[position + 8:])
        eof_mov = MOV_VIDEO[:position] + b"\x00\x00\x00\x00" + MOV_VIDEO[position + 4:]
        for name, video in [("classic", classic_mov), ("extended", extended_mov), ("to-eof", eof_mov)]:
            with self.subTest(name=name):
                clip = self.add_clip(original_filename=name + ".mov", video=video)
                self.assertEqual(clip["media_type"], "video/quicktime")
                status, _, media = self.request("GET", clip["video_url"])
                self.assertEqual(status, 200)
                self.assertEqual(media, video)

    def test_invalid_mov_containers_leave_no_saved_files(self):
        for video in [
            b"not a QuickTime video",
            MOV_VIDEO[:int.from_bytes(MOV_VIDEO[:4], "big")],  # ftyp alone
            MOV_VIDEO[:-1],  # truncated media atom
            b"\x00\x00\x00\x04moov",  # atom smaller than its header
            b"\x00\x00\x00\x01mdat\x00\x00\x00\x00\x00\x00\x00\xff",  # oversized extended atom
            b"\x00\x00\x00\x08moov\x00\x00\x00\x08mdat",  # empty atoms
        ]:
            with self.subTest(video=video[:32]):
                status, error = self.upload(original_filename="invalid.mov", video=video)
                self.assertEqual(status, 415, error)
                self.assertEqual(self.listing()["clips"], [])
                self.assertEqual(list((self.data_dir / "videos").iterdir()), [])
        self.add_clip(original_filename="working.mov", video=MOV_VIDEO)

    def test_media_head_and_byte_ranges(self):
        clip = self.add_clip()
        path = clip["video_url"]
        status, headers, body = self.request("HEAD", path)
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")
        self.assertEqual(int(headers["Content-Length"]), len(VIDEO))
        self.assertEqual(headers["Accept-Ranges"], "bytes")
        for range_header, start, end in [
            ("bytes=0-31", 0, 31),
            ("bytes=32-", 32, len(VIDEO) - 1),
            ("bytes=-16", len(VIDEO) - 16, len(VIDEO) - 1),
            (f"bytes={len(VIDEO)-10}-{len(VIDEO)+100}", len(VIDEO) - 10, len(VIDEO) - 1),
        ]:
            with self.subTest(range_header=range_header):
                status, headers, body = self.request("GET", path, headers={"Range": range_header})
                self.assertEqual(status, 206)
                self.assertEqual(body, VIDEO[start:end + 1])
                self.assertEqual(headers["Content-Range"], f"bytes {start}-{end}/{len(VIDEO)}")
                self.assertEqual(int(headers["Content-Length"]), end - start + 1)
        status, headers, _ = self.request("GET", path, headers={"Range": f"bytes={len(VIDEO)}-"})
        self.assertEqual(status, 416)
        self.assertEqual(headers["Content-Range"], f"bytes */{len(VIDEO)}")

    def test_rejects_bad_uploads_without_adding_clips_or_files(self):
        for kwargs in [
            {"video": b"not a video"},
            {"original_filename": "garden.avi"},
            {"title": ""},
            {"subject": "  "},
            {"title": "a" * 161},
            {"subject": "a" * 81},
            {"notes": "a" * 5001},
            {"recording_date": "2025-02-29"},
            {"recording_date": "2024-2-9"},
            {"recording_date": "2024-02-29T10:00:00"},
        ]:
            with self.subTest(kwargs={k: str(v)[:50] for k, v in kwargs.items()}):
                status, error = self.upload(**kwargs)
                self.assertIn(status, (400, 415), error)
                self.assertTrue(error.get("error"))
                self.assertEqual(self.listing()["clips"], [])
        self.assertEqual(list(self.data_dir.rglob("*.mp4")), [])
        self.add_clip()  # A bad request must not poison later valid uploads.

    def test_rejects_missing_metadata_and_malformed_json(self):
        metadata = {"title": "A title", "original_filename": "clip.mp4"}
        status, result = self.json_request("POST", "/api/clips", VIDEO, {
            "Content-Type": "video/mp4",
            "X-Clip-Metadata": quote(json.dumps(metadata), safe=""),
        })
        self.assertEqual(status, 400, result)
        status, result = self.json_request("POST", "/api/clips", VIDEO, {"Content-Type": "video/mp4"})
        self.assertEqual(status, 400, result)
        clip = self.add_clip()
        status, result = self.json_request(
            "PATCH", "/api/clips/" + clip["id"], b"{broken", {"Content-Type": "application/json"},
        )
        self.assertEqual(status, 400, result)
        self.assertEqual(self.listing()["clips"], [clip])

    def test_invalid_edit_preserves_original(self):
        clip = self.add_clip()
        status, result = self.patch(
            clip["id"], title="Changed", subject="Home", recording_date="2030-04-31", notes="Changed",
        )
        self.assertEqual(status, 400, result)
        self.assertEqual(self.listing()["clips"], [clip])

    def test_oversized_upload_is_rejected_before_reading_body(self):
        status, error = self.upload(video=b"", extra_headers={"Content-Length": str(250 * 1024 * 1024 + 1)})
        self.assertEqual(status, 413, error)
        self.assertEqual(self.listing()["clips"], [])

    def test_cross_origin_write_is_rejected(self):
        status, result = self.upload(extra_headers={"Origin": "https://unrelated.example"})
        self.assertEqual(status, 403, result)
        self.assertEqual(self.listing()["clips"], [])

    def test_local_library_cannot_be_read_through_unrelated_host_or_origin(self):
        clip = self.add_clip()
        for headers in [
            {"Host": "unrelated.example"},
            {"Origin": "https://unrelated.example"},
        ]:
            for path in ["/api/clips", clip["video_url"]]:
                with self.subTest(headers=headers, path=path):
                    status, result = self.json_request("GET", path, headers=headers)
                    self.assertEqual(status, 403, result)
        status, result = self.json_request("GET", "/api/clips", headers={
            "Host": f"localhost:{self.port}", "Origin": f"http://localhost:{self.port}",
        })
        self.assertEqual(status, 200, result)
        self.assertEqual(result["clips"], [clip])

    def test_interrupted_upload_leaves_no_clip_or_temporary_video(self):
        metadata = {"title": "Interrupted", "subject": "Test", "original_filename": "partial.mp4"}
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            connection.request("POST", "/api/clips", body=VIDEO[:64], headers={
                "Cookie": self.cookie,
                "Content-Type": "video/mp4",
                "Content-Length": str(len(VIDEO)),
                "X-Clip-Metadata": quote(json.dumps(metadata), safe=""),
            })
            # Finish the client side early, while leaving the response readable.
            connection.sock.shutdown(socket.SHUT_WR)
            response = connection.getresponse()
            self.assertEqual(response.status, 400)
            self.assertIn("interrupted", json.loads(response.read())["error"])
        finally:
            connection.close()
        self.assertEqual(self.listing()["clips"], [])
        self.assertEqual(list((self.data_dir / "videos").iterdir()), [])
        self.add_clip()

    def test_invalid_byte_ranges_do_not_interrupt_later_playback(self):
        clip = self.add_clip()
        for range_header in ["bytes=", "bytes=-0", "bytes=9-3", "bytes=0-1,4-5", "bytes=" + "9" * 30 + "-"]:
            with self.subTest(range_header=range_header):
                status, headers, body = self.request("GET", clip["video_url"], headers={"Range": range_header})
                self.assertEqual(status, 416, body)
                self.assertEqual(headers["Content-Range"], f"bytes */{len(VIDEO)}")
        status, _, body = self.request("GET", clip["video_url"])
        self.assertEqual(status, 200)
        self.assertEqual(body, VIDEO)

    def test_missing_ids_and_invalid_month_do_not_break_library(self):
        status, _ = self.json_request("GET", "/api/clips/" + "0" * 32)
        self.assertEqual(status, 404)
        status, _, _ = self.request("GET", "/media/" + "0" * 32)
        self.assertEqual(status, 404)
        status, _ = self.json_request("GET", "/api/clips?month=2024-13")
        self.assertEqual(status, 400)
        self.assertEqual(self.listing()["clips"], [])

    def test_signup_creates_safe_session_cookie_and_hashes_secrets(self):
        status, result, headers = self.signup("Jamie", "Jamie@Example.test", "jamie-secret-password")
        self.assertEqual(status, 201)
        self.assertEqual(set(result["user"]), {"id", "name", "email"})
        self.assertEqual(result["user"]["email"], "jamie@example.test")
        cookie = SimpleCookie()
        cookie.load(headers["Set-Cookie"])
        session = cookie["clipshelf_session"]
        self.assertTrue(session["httponly"])
        self.assertEqual(session["samesite"].lower(), "strict")
        self.assertEqual(session["path"], "/")
        self.assertTrue(session["expires"] or session["max-age"])
        self.assertIn("no-store", headers.get("Cache-Control", ""))
        status, current = self.json_request("GET", "/api/auth/session")
        self.assertEqual((status, current), (200, {"user": result["user"]}))
        with sqlite3.connect(self.data_dir / "library.sqlite3") as db:
            password_hash = db.execute("SELECT password_hash FROM users WHERE id=?", (result["user"]["id"],)).fetchone()[0]
            tokens = [row[0] for row in db.execute("SELECT token_hash FROM sessions")]
            expiry = db.execute("SELECT expires_at FROM sessions WHERE user_id=?", (result["user"]["id"],)).fetchone()[0]
        self.assertNotIn("jamie-secret-password", password_hash)
        self.assertGreater(len(password_hash), 40)
        self.assertNotIn(session.value, tokens)
        self.assertTrue(tokens)
        self.assertAlmostEqual(expiry - time.time(), 30 * 24 * 60 * 60, delta=90)
        self.assertEqual(self.listing()["clips"], [])

    def test_signed_out_requests_cannot_read_upload_edit_or_play_clips(self):
        clip = self.add_clip()
        status, session = self.json_request("GET", "/api/auth/session", authenticated=False)
        self.assertEqual((status, session), (200, {"user": None}))
        paths = ["/api/clips", "/api/clips/" + clip["id"],
                 "/private-media/" + clip["id"], "/media/" + clip["id"]]
        for path in paths:
            for method, headers in [("GET", {}), ("HEAD", {}),
                                    ("GET", {"Range": "bytes=0-31"}),
                                    ("HEAD", {"Range": "bytes=0-31"})]:
                with self.subTest(path=path, method=method, headers=headers):
                    status, response_headers, body = self.request(method, path, headers=headers, authenticated=False)
                    self.assertEqual(status, 401, body)
                    self.assertIn("no-store", response_headers.get("Cache-Control", ""))
                    if method == "HEAD":
                        self.assertEqual(body, b"")
        metadata = {"title": "Unauthorized", "subject": "Private", "original_filename": "clip.mp4"}
        status, result = self.json_request("POST", "/api/clips", VIDEO, {
            "Content-Type": "video/mp4", "X-Clip-Metadata": quote(json.dumps(metadata), safe=""),
        }, authenticated=False)
        self.assertEqual(status, 401, result)
        status, result = self.json_request("PATCH", "/api/clips/" + clip["id"],
            json.dumps(metadata).encode(), {"Content-Type": "application/json"}, authenticated=False)
        self.assertEqual(status, 401, result)
        self.assertEqual(self.listing()["clips"], [clip])

    def test_signup_rejects_invalid_fields_short_passwords_and_duplicate_email(self):
        for fields in [
            {"name": "", "email": "new@example.test", "password": "secret123"},
            {"name": "New", "email": "not-an-email", "password": "secret123"},
            {"name": "New", "email": "new@example.test", "password": "12345"},
        ]:
            with self.subTest(fields={key: value for key, value in fields.items() if key != "password"}):
                status, result, _ = self.auth_request("signup", fields)
                self.assertEqual(status, 400, result)
                self.assertTrue(result.get("error"))
        status, result, _ = self.auth_request("signup", {
            "name": "Duplicate", "email": "ALEX@EXAMPLE.TEST", "password": "other-password",
        })
        self.assertEqual(status, 409, result)
        self.assertEqual(self.listing()["clips"], [])
        self.signup("Six characters", "six@example.test", "123456")

    def test_login_rejects_bad_credentials_and_accepts_normalized_email(self):
        self.cookie = None
        for email, password in [("alex@example.test", "wrong-password"),
                                ("missing@example.test", "alex-test-password")]:
            with self.subTest(email=email):
                status, result, _ = self.auth_request("login", {"email": email, "password": password})
                self.assertEqual(status, 401, result)
                self.assertIsNone(self.cookie)
        status, result, _ = self.login(" ALEX@EXAMPLE.TEST ", "alex-test-password")
        self.assertEqual(status, 200)
        self.assertEqual(result["user"], self.user)

    def test_logout_revokes_the_session_including_replayed_cookie(self):
        clip = self.add_clip()
        old_cookie = self.cookie
        status, result, headers = self.auth_request("logout", {})
        self.assertEqual((status, result), (200, {"user": None}))
        self.assertIsNone(self.cookie)
        self.assertIn('"cache"', headers.get("Clear-Site-Data", ""))
        self.assertIn("no-store", headers.get("Cache-Control", ""))
        for path in ["/api/clips", clip["video_url"], "/media/" + clip["id"]]:
            with self.subTest(path=path):
                status, _, _ = self.request("GET", path, headers={"Cookie": old_cookie})
                self.assertEqual(status, 401)
        status, current = self.json_request("GET", "/api/auth/session", headers={"Cookie": old_cookie})
        self.assertEqual((status, current), (200, {"user": None}))
        self.login("alex@example.test", "alex-test-password")
        self.assertNotEqual(self.cookie, old_cookie)
        self.assertEqual(self.listing()["clips"], [clip])

    def test_expired_and_unknown_sessions_do_not_authorize_requests(self):
        old_cookie = self.cookie
        with sqlite3.connect(self.data_dir / "library.sqlite3") as db:
            db.execute("UPDATE sessions SET expires_at=?", (int(time.time()) - 1,))
        for cookie in [old_cookie, "clipshelf_session=" + "0" * 64]:
            with self.subTest(cookie=cookie[:24]):
                status, result = self.json_request("GET", "/api/clips", headers={"Cookie": cookie})
                self.assertEqual(status, 401, result)
                status, result = self.json_request("GET", "/api/auth/session", headers={"Cookie": cookie})
                self.assertEqual((status, result), (200, {"user": None}))

    def test_logout_during_streaming_upload_rejects_commit_and_removes_files(self):
        metadata = {"title": "Revoked while uploading", "subject": "Private", "original_filename": "revoked.mp4"}
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            connection.putrequest("POST", "/api/clips")
            for name, value in {
                "Content-Type": "video/mp4", "Content-Length": str(len(VIDEO)),
                "Cookie": self.cookie,
                "X-Clip-Metadata": quote(json.dumps(metadata), safe=""),
            }.items():
                connection.putheader(name, value)
            connection.endheaders()
            connection.send(VIDEO[:64])
            deadline = time.monotonic() + 3
            while not list((self.data_dir / "videos").glob(".upload-*.tmp")):
                if time.monotonic() >= deadline:
                    self.fail("The upload did not reach staging before logout")
                time.sleep(0.01)
            status, result, _ = self.auth_request("logout", {})
            self.assertEqual((status, result), (200, {"user": None}))
            connection.send(VIDEO[64:])
            response = connection.getresponse()
            self.assertEqual(response.status, 401, response.read())
        finally:
            connection.close()
        self.login("alex@example.test", "alex-test-password")
        self.assertEqual(self.listing()["clips"], [])
        self.assertEqual(list((self.data_dir / "videos").iterdir()), [])

    def test_alex_and_jamie_have_isolated_facets_stats_playback_and_edits(self):
        alex = self.add_clip(title="Alex garden", subject="Alex plants", recording_date="2023-07-05")
        self.signup("Jamie", "jamie@example.test", "jamie-test-password")
        empty = self.listing()
        self.assertEqual(empty, {"clips": [], "subjects": [], "months": [], "stats": {
            "total_clips": 0, "total_bytes": 0, "undated_clips": 0,
        }})
        jamie = self.add_clip(title="Jamie city", subject="Jamie travel", recording_date=None,
            original_filename="jamie.mov", video=MOV_VIDEO)
        listing = self.listing()
        self.assertEqual(listing["clips"], [jamie])
        self.assertEqual(listing["subjects"], [{"name": "Jamie travel", "count": 1}])
        self.assertEqual(listing["months"], [])
        self.assertEqual(listing["stats"], {"total_clips": 1, "total_bytes": len(MOV_VIDEO), "undated_clips": 1})
        self.assertEqual(self.listing(q="Alex", subject="Alex plants", month="2023-07")["clips"], [])
        status, jamie = self.patch(jamie["id"], title="Jamie's edited MOV", subject="Jamie city",
            recording_date="2022-08-14", notes="Only Jamie can edit this.")
        self.assertEqual(status, 200)
        status, headers, body = self.request("GET", jamie["video_url"])
        self.assertEqual((status, headers["Content-Type"], body), (200, "video/quicktime", MOV_VIDEO))
        self.login("alex@example.test", "alex-test-password")
        listing = self.listing()
        self.assertEqual(listing["clips"], [alex])
        self.assertEqual(listing["subjects"], [{"name": "Alex plants", "count": 1}])
        self.assertEqual(listing["months"], ["2023-07"])
        self.assertEqual(listing["stats"], {"total_clips": 1, "total_bytes": len(VIDEO), "undated_clips": 0})
        self.assertEqual(self.listing(q="Jamie", subject="Jamie city", month="2022-08")["clips"], [])
        status, alex = self.patch(alex["id"], title="Alex's edited MP4", subject="Alex home", recording_date=None, notes="Alex only")
        self.assertEqual(status, 200)
        status, headers, body = self.request("GET", alex["video_url"])
        self.assertEqual((status, headers["Content-Type"], body), (200, "video/mp4", VIDEO))
        self.login("jamie@example.test", "jamie-test-password")
        self.assertEqual(self.listing()["clips"], [jamie])

    def test_nonowner_cannot_read_edit_or_play_mp4_mov_or_legacy_links(self):
        clips = [self.add_clip(), self.add_clip(original_filename="private.mov", video=MOV_VIDEO)]
        self.signup("Jamie", "jamie@example.test", "jamie-test-password")
        for clip in clips:
            for path in ["/api/clips/" + clip["id"], clip["video_url"], "/media/" + clip["id"]]:
                for method, headers in [("GET", {}), ("HEAD", {}),
                                        ("GET", {"Range": "bytes=0-31"}),
                                        ("HEAD", {"Range": "bytes=0-31"})]:
                    with self.subTest(path=path, method=method, headers=headers):
                        status, response_headers, body = self.request(method, path, headers=headers)
                        self.assertEqual(status, 404, body)
                        self.assertIn("no-store", response_headers.get("Cache-Control", ""))
            status, error = self.patch(clip["id"], title="Stolen", subject="Jamie", recording_date=None, notes="")
            self.assertEqual(status, 404, error)
        self.assertEqual(self.listing()["clips"], [])
        self.login("alex@example.test", "alex-test-password")
        self.assertEqual({clip["id"] for clip in self.listing()["clips"]}, {clip["id"] for clip in clips})
        for clip in clips:
            for path in [clip["video_url"], "/media/" + clip["id"]]:
                status, _, body = self.request("GET", path)
                self.assertEqual(status, 200)
                self.assertEqual(body, MOV_VIDEO if clip["media_type"] == "video/quicktime" else VIDEO)

    def test_client_supplied_ownership_fields_are_rejected(self):
        clip = self.add_clip()
        for field in ["owner_id", "user_id", "owner", "user"]:
            with self.subTest(field=field):
                status, error = self.upload(extra_metadata={field: self.user["id"]})
                self.assertEqual(status, 400, error)
                status, error = self.patch(clip["id"], title="Unchanged", subject="Home",
                    recording_date=None, notes="", **{field: self.user["id"]})
                self.assertEqual(status, 400, error)
        self.assertEqual(self.listing()["clips"], [clip])

    def test_private_responses_are_not_cached_and_root_clears_old_cache(self):
        clip = self.add_clip()
        for path in ["/", "/api/auth/session", "/api/clips", "/api/clips/" + clip["id"],
                     clip["video_url"], "/media/" + clip["id"]]:
            for method, headers in [("GET", {}), ("HEAD", {}), ("GET", {"Range": "bytes=0-15"})]:
                with self.subTest(path=path, method=method, headers=headers):
                    status, response_headers, _ = self.request(method, path, headers=headers)
                    self.assertIn(status, (200, 206))
                    self.assertIn("no-store", response_headers.get("Cache-Control", ""))
                    if path == "/":
                        self.assertIn('"cache"', response_headers.get("Clear-Site-Data", ""))

    def test_csp_allows_annotation_stylesheets_while_retaining_other_guards(self):
        for path in ["/", "/api/auth/session"]:
            with self.subTest(path=path):
                status, headers, _ = self.request("GET", path)
                self.assertEqual(status, 200)
                policy = dict((parts[0], parts[1:]) for directive in headers["Content-Security-Policy"].split(";")
                              if (parts := directive.split()))
                self.assertEqual(set(policy["style-src-elem"]), {"'self'", "'unsafe-inline'"})
                for directive in ["default-src", "script-src", "style-src", "connect-src", "form-action"]:
                    self.assertEqual(policy[directive], ["'self'"])
                # Omitted attribute/element overrides inherit these strict base
                # directives; annotation support must not permit inline code.
                self.assertEqual(policy.get("script-src-elem", policy["script-src"]), ["'self'"])
                self.assertEqual(policy.get("script-src-attr", policy["script-src"]), ["'self'"])
                self.assertEqual(policy.get("style-src-attr", policy["style-src"]), ["'self'"])
                for directive in ["base-uri", "object-src", "frame-ancestors"]:
                    self.assertEqual(policy[directive], ["'none'"])
                self.assertEqual(set(policy["media-src"]), {"'self'", "blob:"})
                self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
                self.assertEqual(headers["Referrer-Policy"], "no-referrer")

    def make_legacy_library(self):
        """Build authentic schema-1 examples in a fresh temporary folder only."""
        self.stop_server()
        self.data_dir = Path(self.temp.name) / "legacy-library"
        videos = self.data_dir / "videos"
        videos.mkdir(parents=True)
        rows = [
            ("1" * 32, "Legacy garden", "Legacy home", None, "Original undated notes", "2025-01-02T03:04:05.000Z", "legacy.MP4", len(VIDEO)),
            ("2" * 32, "Legacy city", "Legacy travel", "2022-05-09", "Original MOV notes", "2025-02-03T04:05:06.000Z", "legacy.MOV", len(MOV_VIDEO)),
        ]
        with sqlite3.connect(self.data_dir / "library.sqlite3") as db:
            db.execute("""CREATE TABLE clips (
                id TEXT PRIMARY KEY, title TEXT NOT NULL, subject TEXT NOT NULL,
                recording_date TEXT, notes TEXT NOT NULL DEFAULT '', uploaded_at TEXT NOT NULL,
                original_filename TEXT NOT NULL, size_bytes INTEGER NOT NULL CHECK(size_bytes>0))""")
            db.executemany("INSERT INTO clips VALUES(?,?,?,?,?,?,?,?)", rows)
            db.execute("PRAGMA user_version=1")
        (videos / (rows[0][0] + ".mp4")).write_bytes(VIDEO)
        (videos / (rows[1][0] + ".mov")).write_bytes(MOV_VIDEO)
        self.cookie = None
        return rows

    def run_seed(self, backup_name):
        backup_path = Path(self.temp.name) / backup_name
        result = subprocess.run([sys.executable, str(APP_DIR / "seed_demo.py"),
            "--data-dir", str(self.data_dir), "--backup-dir", str(backup_path)],
            cwd=APP_DIR, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return backup_path

    def test_schema1_migration_preserves_rows_files_and_claims_legacy_only_once(self):
        rows = self.make_legacy_library()
        original_files = {path.name: path.read_bytes() for path in (self.data_dir / "videos").iterdir()}
        self.start_server()
        self.signup("New person", "new@example.test", "new-person-password")
        self.assertEqual(self.listing()["clips"], [])
        with sqlite3.connect(self.data_dir / "library.sqlite3") as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)
            stored = db.execute("SELECT id,title,subject,recording_date,notes,uploaded_at,original_filename,size_bytes FROM clips ORDER BY id").fetchall()
            self.assertEqual(stored, rows)
            self.assertEqual(db.execute("SELECT DISTINCT owner_id FROM clips").fetchall(), [(None,)])
        self.stop_server()
        backup = self.run_seed("before-first-seed")
        self.assertTrue((backup / "manifest.json").is_file())
        self.start_server()
        self.login("alex@clipshelf.test", "demo123")
        alex_listing = self.listing()
        self.assertEqual({clip["id"] for clip in alex_listing["clips"]}, {row[0] for row in rows})
        for clip in alex_listing["clips"]:
            expected = next(row for row in rows if row[0] == clip["id"])
            for field, value in zip(["id", "title", "subject", "recording_date", "notes", "uploaded_at", "original_filename", "size_bytes"], expected):
                self.assertEqual(clip[field], value)
            status, _, video = self.request("GET", clip["video_url"])
            self.assertEqual(status, 200)
            self.assertEqual(video, original_files[clip["id"] + (".mov" if clip["original_filename"].lower().endswith(".mov") else ".mp4")])
        self.login("jamie@clipshelf.test", "demo123")
        self.assertEqual(self.listing()["clips"], [])
        self.login("new@example.test", "new-person-password")
        self.assertEqual(self.listing()["clips"], [])
        self.stop_server()
        # A later unowned row must not be silently claimed by a repeated seed.
        late_id = "3" * 32
        with sqlite3.connect(self.data_dir / "library.sqlite3") as db:
            db.execute("INSERT INTO clips(id,title,subject,recording_date,notes,uploaded_at,original_filename,size_bytes,owner_id) VALUES(?,?,?,?,?,?,?,?,NULL)",
                (late_id, "Later unowned clip", "Unclaimed", None, "Keep unclaimed", "2025-03-01T00:00:00.000Z", "later.mp4", len(VIDEO)))
        (self.data_dir / "videos" / (late_id + ".mp4")).write_bytes(VIDEO)
        original_files[late_id + ".mp4"] = VIDEO
        self.run_seed("before-repeat-seed")
        self.start_server()
        self.login("alex@clipshelf.test", "demo123")
        self.assertEqual(self.listing(), alex_listing)
        with sqlite3.connect(self.data_dir / "library.sqlite3") as db:
            self.assertIsNone(db.execute("SELECT owner_id FROM clips WHERE id=?", (late_id,)).fetchone()[0])
        self.assertEqual({path.name: path.read_bytes() for path in (self.data_dir / "videos").iterdir()}, original_files)

    def test_repeated_seed_preserves_existing_credentials_and_user_work(self):
        self.signup("Existing Alex name", "alex@clipshelf.test", "my-custom-alex-password")
        alex_user = self.json_request("GET", "/api/auth/session")[1]["user"]
        original = self.add_clip(title="Alex's new work", recording_date=None)
        self.stop_server()
        self.run_seed("before-existing-alex-seed")
        self.start_server()
        self.login("alex@clipshelf.test", "my-custom-alex-password")
        self.assertEqual(self.json_request("GET", "/api/auth/session")[1]["user"], alex_user)
        self.assertEqual(self.listing()["clips"], [original])
        status, edited = self.patch(original["id"], title="Edited after seeding", subject="Keep this subject", recording_date="2019-12-31", notes="Never reset this edit")
        self.assertEqual(status, 200)
        self.login("jamie@clipshelf.test", "demo123")
        self.assertEqual(self.listing()["clips"], [])
        jamie = self.add_clip(title="Jamie's own work", original_filename="jamie.mov", video=MOV_VIDEO)
        with sqlite3.connect(self.data_dir / "library.sqlite3") as db:
            before_users = db.execute("SELECT id,name,email,password_hash,created_at FROM users ORDER BY id").fetchall()
        self.stop_server()
        self.run_seed("before-third-seed")
        self.start_server()
        self.login("alex@clipshelf.test", "my-custom-alex-password")
        self.assertEqual(self.listing()["clips"], [edited])
        status, _, _ = self.auth_request("login", {"email": "alex@clipshelf.test", "password": "demo123"})
        self.assertEqual(status, 401)
        self.login("jamie@clipshelf.test", "demo123")
        self.assertEqual(self.listing()["clips"], [jamie])
        with sqlite3.connect(self.data_dir / "library.sqlite3") as db:
            self.assertEqual(db.execute("SELECT id,name,email,password_hash,created_at FROM users ORDER BY id").fetchall(), before_users)

    def test_backup_roundtrip_preserves_accounts_ownership_sessions_and_video_bytes(self):
        alex = self.add_clip(title="Backed up Alex")
        self.signup("Jamie", "jamie@example.test", "jamie-test-password")
        jamie = self.add_clip(title="Backed up Jamie", original_filename="saved.mov", video=MOV_VIDEO, recording_date=None)
        jamie_cookie = self.cookie
        tables = ["users", "clips", "sessions", "app_metadata"]
        with sqlite3.connect(self.data_dir / "library.sqlite3") as db:
            original = {table: db.execute("SELECT * FROM " + table + " ORDER BY 1").fetchall() for table in tables}
        backup = Path(self.temp.name) / "snapshot"
        restored = Path(self.temp.name) / "restored-library"
        for args in [
            ["backup", str(backup), "--data-dir", str(self.data_dir)],
            ["restore", str(backup), str(restored)],
        ]:
            result = subprocess.run([sys.executable, str(APP_DIR / "backup.py"), *args],
                cwd=APP_DIR, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        with sqlite3.connect(restored / "library.sqlite3") as db:
            self.assertEqual({table: db.execute("SELECT * FROM " + table + " ORDER BY 1").fetchall() for table in tables}, original)
        self.assertEqual((restored / "videos" / (alex["id"] + ".mp4")).read_bytes(), VIDEO)
        self.assertEqual((restored / "videos" / (jamie["id"] + ".mov")).read_bytes(), MOV_VIDEO)
        self.stop_server()
        self.data_dir = restored
        self.start_server()
        self.cookie = jamie_cookie
        self.assertEqual(self.listing()["clips"], [jamie])
        self.login("alex@example.test", "alex-test-password")
        self.assertEqual(self.listing()["clips"], [alex])


if __name__ == "__main__":
    unittest.main(verbosity=2)
