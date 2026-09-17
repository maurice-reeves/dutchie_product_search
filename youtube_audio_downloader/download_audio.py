#!/usr/bin/env python3
"""Download the audio from your own YouTube videos as MP3s, via yt-dlp.

Usage:
    python download_audio.py --url https://www.youtube.com/@yourchannel/videos
    python download_audio.py --url https://www.youtube.com/watch?v=... --url https://youtu.be/...
    python download_audio.py --urls-file my_videos.txt

Requires:
    pip install -r requirements.txt
    ffmpeg installed and on PATH (used by yt-dlp to extract/convert audio)

Only use this on videos you own or otherwise have the rights to download.
"""

import argparse
import sys
from pathlib import Path

import yt_dlp


def load_urls(args: argparse.Namespace) -> list[str]:
    urls = list(args.url or [])
    if args.urls_file:
        text = Path(args.urls_file).read_text(encoding="utf-8")
        urls.extend(
            line.strip()
            for line in text.splitlines()
            if line.strip() and not line.strip().startswith("#")
        )
    if not urls:
        sys.exit("No URLs given. Pass --url one or more times, or --urls-file FILE.")
    return urls


def build_options(args: argparse.Namespace) -> dict:
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    options = {
        "format": "bestaudio/best",
        "outtmpl": str(output_dir / "%(uploader)s/%(title)s.%(ext)s"),
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": str(args.quality),
            }
        ],
        # Records downloaded video IDs so re-running the script skips
        # videos already saved instead of re-downloading everything.
        "download_archive": str(output_dir / "download_archive.txt"),
        "ignoreerrors": True,
        "noplaylist": False,
        "quiet": False,
    }
    if args.cookies:
        options["cookiefile"] = args.cookies
    return options


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--url",
        action="append",
        help="A video, playlist, or channel URL. Repeat for multiple.",
    )
    parser.add_argument(
        "--urls-file",
        help="Path to a text file with one URL per line (# comments allowed).",
    )
    parser.add_argument(
        "--output-dir",
        default="downloads",
        help="Where to save MP3s, organized in a subfolder per uploader (default: ./downloads).",
    )
    parser.add_argument(
        "--quality",
        default="192",
        help="MP3 bitrate in kbps (default: 192).",
    )
    parser.add_argument(
        "--cookies",
        help="Optional path to a cookies.txt file, needed for private/unlisted videos.",
    )
    args = parser.parse_args()

    urls = load_urls(args)
    options = build_options(args)

    with yt_dlp.YoutubeDL(options) as ydl:
        ydl.download(urls)


if __name__ == "__main__":
    main()
