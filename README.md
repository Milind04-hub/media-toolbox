<p align="center">
  <img src="static/icon.svg" width="88" alt="">
</p>

<h1 align="center">Media Toolbox</h1>

<p align="center">
  A local, all-in-one media web app: download videos, convert audio/video and images,
  split songs into vocals + instrumental, cut big videos into shareable clips, and build PDFs.<br>
  Everything runs on your own machine — no uploads to anyone's server, no accounts, no limits.
</p>

---

## Features

| Tab | What it does |
|---|---|
| **Download** | Paste links (one per line) from YouTube, Instagram, TikTok, X, Facebook and [many more](https://github.com/yt-dlp/yt-dlp/blob/master/supported-sites.md) — as MP4 or MP3. Optional browser-cookie auth for login-protected posts. |
| **Audio / Video** | Convert between MP4, MOV, MKV, AVI, WebM, MP3, WAV, M4A, FLAC, OGG, AAC — plus **GIF** with a per-clip colour palette, and adjustable frame rate and width. Copies streams when the container allows it, so most conversions are instant and lossless. |
| **Vocal Split** | Separate any song into **vocals** and **instrumental** using Demucs. Seconds on an NVIDIA GPU. |
| **Split for Sharing** | Cut a big video into clips under a size limit (2 GB for WhatsApp, 64 MB, custom) or a fixed length. Stream copy, so it's near-instant with no quality loss. |
| **Image Convert** | JPG, PNG, WebP, BMP, TIFF, GIF, ICO, plus HEIC (iPhone) and EXR (HDR) input. |
| **Compress** | Shrink images to a target size in KB — it finds the best quality that fits, and only downscales if it must. |
| **Images → PDF** | Auto layout, or a drag-and-resize editor with preset grids (2×2, side-by-side, …). Reorderable pages. |
| **PDF Tools** | Merge PDFs; extract pages back to PNG/JPG/WebP at a chosen DPI. |

Batches are supported everywhere: multiple results come back as a single ZIP, and media
is stored uncompressed inside it (already-compressed files gain nothing from deflating).

### Choose from this PC — no upload
Every file tab can pick files **already on the machine running the app**, with thumbnails
and List / Medium / Large icon views. Nothing is uploaded, so even a 2 GB video starts
instantly. Useful when the app runs on your desktop and you open it from your phone.
Source files are only ever read, never modified or deleted.

## Setup

Requires **Python 3.9+**. No separate ffmpeg install — it ships with `imageio-ffmpeg`.

```bash
git clone https://github.com/<you>/media-toolbox.git
cd media-toolbox
pip install -r requirements.txt
```

Run it:

```bash
python app.py
```

Then open <http://127.0.0.1:5000>. On Windows you can double-click **`Start Media Toolbox.bat`**.

### GPU acceleration (optional, NVIDIA)
Vocal separation and video re-encoding use the GPU automatically when one is available.
The default `torch` is CPU-only; for a recent NVIDIA card (RTX 30/40/50 series):

```bash
pip uninstall -y torch torchaudio
pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu128
```

Check with `python -c "import torch; print(torch.cuda.is_available())"` → `True`.
A full song then separates in seconds instead of minutes. Hardware encoding (NVENC)
is used automatically when a conversion genuinely needs re-encoding.

### YouTube needs a JavaScript runtime
yt-dlp needs one for YouTube extraction. Install [Deno](https://deno.com/) (`winget install DenoLand.Deno`)
and the app will find it. If downloads start failing with 403 or "unable to extract",
update yt-dlp — YouTube changes often:

```bash
pip install --upgrade yt-dlp
```

## Configuration

Optional. Copy `config.example.json` to `config.json`:

```json
{
  "host": "127.0.0.1",
  "port": 5000,
  "library_roots": {
    "icloud": ["iCloud Photos", "F:\\iCloudPhotos\\Photos"]
  }
}
```

- **`host`** — `127.0.0.1` (default) means this PC only. See the security note below before changing it.
- **`library_roots`** — extra folders the "Choose from this PC" picker may read. Downloads,
  Videos, Pictures and Desktop are included automatically. Nothing outside these is reachable.

## ⚠️ Security

**This app has no authentication, and it can browse and serve files from `library_roots`.**
It is built to run on your own machine, for you.

- The default (`127.0.0.1`) is safe: only your PC can reach it.
- Setting `host` to `0.0.0.0` exposes it to your whole network. Only do that on a network
  you trust, and prefer a private mesh VPN such as [Tailscale](https://tailscale.com/)
  so only your own devices can connect.
- **Never port-forward it or put it on a public server.** Anyone who reaches it could read
  any file inside `library_roots`.

Using it to download content you don't have the rights to may breach a site's terms of
service or local copyright law. That's on you.

## Notes

- Results land in `output/`, and are also offered as browser downloads. Nothing is cleaned
  up automatically, so clear that folder occasionally.
- Cloud-synced placeholder files (iCloud/OneDrive on-demand) are downloaded when first used;
  the picker shows a tap-to-preview tile with a time estimate rather than silently pulling gigabytes.
- `smoke_test.py` exercises the main endpoints against a running server.

## Built with

[yt-dlp](https://github.com/yt-dlp/yt-dlp) ·
[FFmpeg](https://ffmpeg.org/) ·
[Demucs](https://github.com/adefossez/demucs) ·
[PyTorch](https://pytorch.org/) ·
[Pillow](https://python-pillow.org/) ·
[PyMuPDF](https://pymupdf.readthedocs.io/) ·
[Flask](https://flask.palletsprojects.com/)
