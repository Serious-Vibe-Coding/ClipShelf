"""Recreate small silent H.264 fixtures; ffmpeg is not needed to run tests."""

import argparse
from pathlib import Path
import shutil
import subprocess


def make_quicktime(directory):
    # MP4 and QuickTime share atom structure and H.264 sample descriptions.
    # Change only ftyp brands; preserving every atom's length keeps the sample
    # offsets valid and preserves the independently generated city test pattern.
    data = bytearray((directory / "city-walk.mp4").read_bytes())
    atom_size = int.from_bytes(data[:4], "big")
    if data[4:8] != b"ftyp" or not 16 <= atom_size <= len(data):
        raise ValueError("Expected an MP4 fixture beginning with a file-type atom")
    data[8:12] = b"qt  "
    for position in range(16, atom_size, 4):
        data[position:position + 4] = b"qt  "
    (directory / "city-quicktime.mov").write_bytes(data)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ffmpeg", default=shutil.which("ffmpeg"))
    parser.add_argument("--mov-only", action="store_true", help="recreate MOV from the existing MP4 fixture without ffmpeg")
    args = parser.parse_args()
    if not args.ffmpeg and not args.mov_only:
        parser.error("Pass --ffmpeg /path/to/ffmpeg to regenerate the committed fixtures.")
    directory = Path(__file__).resolve().parent / "fixtures"
    directory.mkdir(exist_ok=True)
    if args.mov_only:
        make_quicktime(directory)
        return
    sources = {
        "desk-garden.mp4": "color=c=0x196a64:s=320x180:r=24:d=1,drawbox=x=40:y=35:w=90:h=110:color=0x8ed6aa:t=fill,drawbox=x=165:y=55:w=110:h=90:color=0xd2edbe:t=fill",
        "city-walk.mp4": "testsrc2=s=320x180:r=24:d=1",
    }
    for filename, source in sources.items():
        subprocess.run([
            args.ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", source,
            "-c:v", "libopenh264", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
            "-an", "-y", str(directory / filename),
        ], check=True)
    make_quicktime(directory)


if __name__ == "__main__":
    main()
