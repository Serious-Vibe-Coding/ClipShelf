These are one-second silent H.264 MP4 and QuickTime MOV test patterns.
The MP4 files were generated locally with ffmpeg. city-quicktime.mov preserves
city-walk.mp4's H.264 samples and atom lengths while using QuickTime ftyp brands.
They contain no personal footage and are never loaded into the normal library.
The integration suite copies them into a new temporary library per test.

Optional regeneration:
  python3 tests/generate_fixtures.py --ffmpeg /path/to/ffmpeg

Recreate only the QuickTime fixture from the existing MP4 without dependencies:
  python3 tests/generate_fixtures.py --mov-only
