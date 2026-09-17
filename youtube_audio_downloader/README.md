# YouTube Audio Downloader

A small standalone script for downloading the audio of your own YouTube
videos as MP3 files, using [yt-dlp](https://github.com/yt-dlp/yt-dlp). It's
unrelated to the rest of this repo — it just lives here because that's where
it was requested.

Only use this on videos you own or otherwise have the rights to download.

## Setup

```bash
cd youtube_audio_downloader
pip install -r requirements.txt
```

You also need [ffmpeg](https://ffmpeg.org/download.html) installed and on
your `PATH` — yt-dlp uses it to extract and convert audio. On macOS:
`brew install ffmpeg`. On Ubuntu/Debian: `sudo apt install ffmpeg`.

## Usage

Download every video from your channel's "Videos" tab:

```bash
python download_audio.py --url "https://www.youtube.com/@yourchannel/videos"
```

Download specific videos:

```bash
python download_audio.py --url "https://youtu.be/VIDEO_ID_1" --url "https://youtu.be/VIDEO_ID_2"
```

Or put URLs in a text file (one per line, `#` for comments) and run:

```bash
python download_audio.py --urls-file my_videos.txt
```

MP3s are saved under `downloads/<uploader name>/<title>.mp3` by default
(change with `--output-dir`). A `download_archive.txt` file is kept in the
output directory so re-running the script later only fetches new videos.

### Options

| Flag | Description |
| --- | --- |
| `--url` | A video, playlist, or channel URL. Repeatable. |
| `--urls-file` | Text file with one URL per line. |
| `--output-dir` | Where to save MP3s (default: `downloads`). |
| `--quality` | MP3 bitrate in kbps (default: `192`). |
| `--cookies` | Path to a `cookies.txt` file, for private/unlisted videos. |
