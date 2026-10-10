import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
import zipfile

from flask import Flask, jsonify, render_template, request, send_file
from PIL import Image
import fitz  # PyMuPDF
import imageio_ffmpeg
import yt_dlp

app = Flask(__name__)
app.config["TEMPLATES_AUTO_RELOAD"] = True

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Optional local settings (not in git). Copy config.example.json to config.json.
CONFIG = {}
_cfg_path = os.path.join(BASE_DIR, "config.json")
if os.path.exists(_cfg_path):
    try:
        with open(_cfg_path, encoding="utf-8") as _fh:
            CONFIG = json.load(_fh)
    except (OSError, ValueError) as _e:
        print(f"warning: ignoring config.json ({_e})")

OUTPUT_DIR = os.path.join(BASE_DIR, "output")
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(UPLOAD_DIR, exist_ok=True)

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()


def _ensure_js_runtime():
    """yt-dlp needs a JS runtime for YouTube; add Deno to PATH if it's installed.

    winget only updates PATH for newly-launched shells, so a server started from
    an existing terminal won't see Deno without this.
    """
    if shutil.which("deno"):
        return True
    local = os.environ.get("LOCALAPPDATA", "")
    candidates = [
        os.path.join(local, "Microsoft", "WinGet", "Links"),
        os.path.join(local, "Microsoft", "WinGet", "Packages",
                     "DenoLand.Deno_Microsoft.Winget.Source_8wekyb3d8bbwe"),
        os.path.join(os.path.expanduser("~"), ".deno", "bin"),
    ]
    for d in candidates:
        if os.path.isfile(os.path.join(d, "deno.exe")):
            os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
            return True
    return False


HAS_JS_RUNTIME = _ensure_js_runtime()

# job_id -> {"status": "running"|"done"|"error", "progress": str, "file": path, "error": str}
jobs = {}

VIDEO_FORMATS = {"mp4", "mov", "avi", "mkv", "webm"}
AUDIO_FORMATS = {"mp3", "wav", "m4a", "aac", "flac", "ogg"}
ALLOWED_TARGETS = VIDEO_FORMATS | AUDIO_FORMATS | {"gif"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tiff", ".gif"}

# iPhone photos are HEIC; teach Pillow to open them if pillow-heif is installed
try:
    import pillow_heif
    pillow_heif.register_heif_opener()
    IMAGE_EXTS |= {".heic", ".heif"}
except ImportError:
    pass


def new_job():
    job_id = uuid.uuid4().hex[:12]
    jobs[job_id] = {"status": "running", "progress": "starting...", "file": None, "error": None}
    return job_id


# Windows caps full paths at 260 chars, so keep generated names short and portable.
MAX_BASE_LEN = 60


def safe_base(name, max_len=MAX_BASE_LEN):
    """Strip the extension, drop characters Windows rejects, and truncate."""
    base = os.path.splitext(os.path.basename(name))[0]
    base = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", base)   # illegal on Windows
    base = re.sub(r"\s+", " ", base).strip(" .")        # collapse spaces, no trailing dot
    if len(base) > max_len:
        base = base[:max_len].rstrip(" .-_")
    return base or "output"


# Formats that are already compressed: deflating them saves ~0% and wastes CPU,
# so they're stored as-is. Only raw formats (WAV, BMP, ...) get compressed.
ALREADY_COMPRESSED = {
    ".mp4", ".mov", ".mkv", ".webm", ".avi", ".mp3", ".m4a", ".aac", ".ogg", ".opus",
    ".flac", ".jpg", ".jpeg", ".png", ".webp", ".gif", ".zip", ".pdf",
}


def make_zip(out_path, entries):
    """Write (name_in_zip, source_path_or_bytes) entries to a ZIP; duplicate names get (2), (3)..."""
    used = set()
    with zipfile.ZipFile(out_path, "w") as zf:
        for name, src in entries:
            stem, ext = os.path.splitext(name)
            unique, n = name, 2
            while unique.lower() in used:
                unique, n = f"{stem} ({n}){ext}", n + 1
            used.add(unique.lower())
            method = (zipfile.ZIP_STORED if ext.lower() in ALREADY_COMPRESSED
                      else zipfile.ZIP_DEFLATED)
            if isinstance(src, (bytes, bytearray)):
                zf.writestr(unique, src, compress_type=method)
            else:
                zf.write(src, unique, compress_type=method)


def finish_batch(job, job_id, outputs, zip_label, errors=None):
    """Mark a batch job done: one file is returned as-is, several are bundled into a ZIP.

    outputs: list of (display_name, path). errors: list of "name: reason" strings
    for items that failed; they're listed in the ZIP so partial batches still deliver.
    """
    if not outputs:
        raise RuntimeError(errors[0] if errors else "nothing was produced")
    if len(outputs) == 1 and not errors:
        name, path = outputs[0]
        final = os.path.join(OUTPUT_DIR, f"{job_id}_{name}")
        if os.path.abspath(path) != os.path.abspath(final):
            os.replace(path, final)
        job["file"] = final
    else:
        job["progress"] = f"bundling {len(outputs)} files into a ZIP..."
        final = os.path.join(OUTPUT_DIR, f"{job_id}_{zip_label}.zip")
        entries = list(outputs)
        if errors:
            entries.append(("FAILED_ITEMS.txt", "\n".join(errors).encode("utf-8")))
        make_zip(final, entries)
        for _, path in outputs:
            try:
                os.remove(path)
            except OSError:
                pass
        job["file"] = final
    if errors:
        job["warning"] = f"{len(errors)} of {len(outputs) + len(errors)} failed (see FAILED_ITEMS.txt in the ZIP)"
    job["status"] = "done"
    job["progress"] = "done"


# ---------------- Online video download (YouTube, Instagram, etc.) ----------------

def yt_worker(job_id, url, mode, cookies_browser=None):
    """Download one or more URLs (urls: list). Several results come back as a ZIP."""
    job = jobs[job_id]
    urls = url if isinstance(url, list) else [url]
    outputs, errors = [], []
    for i, u in enumerate(urls, 1):
        tag = f"[{i}/{len(urls)}] " if len(urls) > 1 else ""
        try:
            path = download_one(u, mode, cookies_browser, f"{job_id}_{i}_", job, tag)
            # strip the "<job>_<i>_" prefix to get the clean title-based name
            outputs.append((os.path.basename(path).split("_", 2)[2], path))
        except Exception as e:
            errors.append(f"{u}\n  -> {explain_download_error(str(e))}")
    try:
        finish_batch(job, job_id, outputs, "downloads", errors)
    except Exception as e:
        job["status"] = "error"
        job["error"] = explain_download_error(str(e))


def download_one(url, mode, cookies_browser, prefix, job, tag):
    """Download a single URL with yt-dlp; returns the path of the produced file."""
    def hook(d):
        if d["status"] == "downloading":
            pct = d.get("_percent_str", "").strip()
            speed = d.get("_speed_str", "").strip()
            job["progress"] = f"{tag}downloading {pct} at {speed}"
        elif d["status"] == "finished":
            job["progress"] = f"{tag}processing..."

    # truncate the title: long video names blow past Windows' 260-char path limit
    outtmpl = os.path.join(OUTPUT_DIR, f"{prefix}%(title).{MAX_BASE_LEN}s.%(ext)s")
    opts = {
        "outtmpl": outtmpl,
        "progress_hooks": [hook],
        "ffmpeg_location": FFMPEG,
        "noplaylist": True,
        "quiet": True,
    }
    if cookies_browser:
        opts["cookiesfrombrowser"] = (cookies_browser,)
    if mode == "mp3":
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}]
    else:
        opts["format"] = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"
        opts["merge_output_format"] = "mp4"

    job["progress"] = f"{tag}starting..."
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.extract_info(url, download=True)
    produced = [f for f in os.listdir(OUTPUT_DIR) if f.startswith(prefix)]
    if not produced:
        raise RuntimeError("download finished but output file not found")
    return os.path.join(OUTPUT_DIR, produced[0])


def explain_download_error(msg):
    """Turn raw yt-dlp errors into advice the user can act on."""
    low = msg.lower()
    if "not a bot" in low or "429" in low or "too many requests" in low:
        msg = ("YouTube is rate-limiting this network (HTTP 429) and is asking for bot "
               "verification. This is a temporary block on your IP address, not a problem "
               "with the app or the video — the same link may have worked earlier. "
               "What to do: (1) wait it out — usually 30 minutes to a few hours, and avoid "
               "retrying repeatedly, which extends the block; (2) selecting your browser in "
               "the cookies dropdown can help once the block lifts; (3) a different network "
               "(e.g. phone hotspot) uses a different IP and will normally work right away. "
               f"Raw error: {msg}")
    elif "could not copy" in low and "cookie" in low:
        msg = ("Could not read the browser's cookies because the browser is running and has "
               "the cookie store locked. Close ALL windows of that browser (check the system "
               "tray too), then retry. If it still fails, try the Firefox option instead — "
               "Firefox cookies can be read while it is open.")
    elif "failed to decrypt" in low and "cookie" in low:
        msg = ("The browser's cookies are protected by app-bound encryption and can't be "
               "decrypted. Easiest fix: log into the site once in Firefox and choose the "
               "Firefox cookies option.")
    elif "login" in low or "rate-limit" in low or "private" in low or "empty media response" in low:
        msg = ("This post may be private or login-protected. Public posts/reels work; "
               "private or age-restricted content needs you to be logged in. (" + msg + ")")
    return msg


@app.route("/api/youtube", methods=["POST"])
def api_youtube():
    data = request.get_json(force=True)
    # one or more links, separated by newlines/spaces; duplicates dropped, order kept
    raw = data.get("url") or ""
    urls = list(dict.fromkeys(u for u in re.split(r"\s+", raw.strip()) if u))
    mode = data.get("mode", "mp4")
    cookies_browser = data.get("cookies") or None
    if cookies_browser not in (None, "chrome", "edge", "firefox", "brave", "opera", "vivaldi"):
        return jsonify({"error": "Unsupported browser for cookies"}), 400
    if not urls:
        return jsonify({"error": "URL is required"}), 400
    if len(urls) > 50:
        return jsonify({"error": "Up to 50 links at a time"}), 400
    job_id = new_job()
    threading.Thread(target=yt_worker, args=(job_id, urls, mode, cookies_browser), daemon=True).start()
    return jsonify({"job_id": job_id})


# ---------------- File conversion ----------------

# Codecs each container can hold as-is. If the input already matches, we copy the
# streams instead of re-encoding: seconds instead of minutes, and zero quality loss.
COPY_VIDEO = {
    "mp4": {"h264", "hevc", "av1", "mpeg4"},
    "mov": {"h264", "hevc", "av1", "mpeg4", "prores"},
    "mkv": {"h264", "hevc", "av1", "vp8", "vp9", "mpeg4"},
    "webm": {"vp8", "vp9", "av1"},
}
COPY_AUDIO = {
    "mp4": {"aac", "mp3", "ac3", "eac3", "alac"},
    "mov": {"aac", "mp3", "ac3", "eac3", "alac", "pcm_s16le"},
    "mkv": {"aac", "mp3", "ac3", "eac3", "opus", "vorbis", "flac"},
    "webm": {"opus", "vorbis"},
    "mp3": {"mp3"}, "m4a": {"aac", "alac"}, "aac": {"aac"},
    "flac": {"flac"}, "ogg": {"vorbis", "opus"}, "wav": {"pcm_s16le"},
}

_nvenc_ok = None


def nvenc_available():
    """True if the NVIDIA hardware encoder works (checked once, then cached)."""
    global _nvenc_ok
    if _nvenc_ok is None:
        try:
            r = subprocess.run(
                [FFMPEG, "-hide_banner", "-f", "lavfi", "-i", "testsrc=duration=0.2:size=256x144",
                 "-c:v", "h264_nvenc", "-f", "null", "-"],
                capture_output=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
            _nvenc_ok = r.returncode == 0
        except Exception:
            _nvenc_ok = False
    return _nvenc_ok


def probe_media(path):
    """Return (duration_seconds, video_codec, audio_codec) by parsing `ffmpeg -i`."""
    r = subprocess.run([FFMPEG, "-hide_banner", "-i", path], capture_output=True, text=True,
                       errors="ignore", creationflags=subprocess.CREATE_NO_WINDOW)
    info = r.stderr
    duration = 0.0
    m = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", info)
    if m:
        duration = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    v = re.search(r"Stream #\S+.*?: Video: (\w+)", info)
    a = re.search(r"Stream #\S+.*?: Audio: (\w+)", info)
    return duration, (v.group(1) if v else None), (a.group(1) if a else None)


def build_convert_cmd(in_path, out_path, target, vcodec, acodec, copy, opts=None):
    """ffmpeg command for this conversion; copy=True remuxes without re-encoding."""
    opts = opts or {}
    cmd = [FFMPEG, "-y", "-hide_banner", "-i", in_path]
    if target == "gif":
        # GIF holds only 256 colours, so build a palette from the clip itself and
        # apply it in the same pass — otherwise colours band badly and files balloon.
        fps = max(1, min(30, int(opts.get("gif_fps", 12))))
        width = max(64, min(1920, int(opts.get("gif_width", 480))))
        filters = (f"[0:v]fps={fps},scale={width}:-2:flags=lanczos,split[a][b];"
                   "[a]palettegen=stats_mode=diff[p];"
                   "[b][p]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle[out]")
        cmd += ["-filter_complex", filters, "-map", "[out]", "-an", "-loop", "0"]
    elif target in AUDIO_FORMATS:
        cmd += ["-map", "0:a:0", "-vn"]
        if copy:
            cmd += ["-c:a", "copy"]
        elif target == "mp3":
            cmd += ["-c:a", "libmp3lame", "-b:a", "192k"]
        elif target in ("m4a", "aac"):
            cmd += ["-c:a", "aac", "-b:a", "192k"]
        elif target == "ogg":
            cmd += ["-c:a", "libvorbis", "-q:a", "5"]
    else:
        # first video + first audio only: skips phone extras like spatial audio/metadata
        cmd += ["-map", "0:v:0", "-map", "0:a:0?"]
        if copy:
            cmd += ["-c", "copy"]
            if vcodec == "hevc" and target in ("mp4", "mov"):
                cmd += ["-tag:v", "hvc1"]          # so Apple/QuickTime players accept it
        else:
            gpu = nvenc_available()
            if target == "webm":
                cmd += (["-c:v", "av1_nvenc", "-cq", "30"] if gpu
                        else ["-c:v", "libvpx-vp9", "-crf", "32", "-b:v", "0", "-row-mt", "1", "-cpu-used", "4"])
                cmd += ["-c:a", "libopus", "-b:a", "128k"]
            elif target == "avi":
                cmd += ["-c:v", "mpeg4", "-q:v", "3", "-c:a", "libmp3lame", "-b:a", "192k"]
            else:                                   # mp4 / mov / mkv -> H.264 + AAC
                cmd += (["-c:v", "h264_nvenc", "-preset", "p5", "-cq", "23"] if gpu
                        else ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23"])
                cmd += ["-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k"]
        if target in ("mp4", "mov"):
            cmd += ["-movflags", "+faststart"]
    cmd += ["-progress", "pipe:1", "-nostats", out_path]
    return cmd


def run_ffmpeg(cmd, job, duration, label):
    """Run ffmpeg, streaming its progress into job['progress']. Raises on failure."""
    err_file = io.StringIO()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            errors="ignore", creationflags=subprocess.CREATE_NO_WINDOW)
    # drain stderr on a thread so a full pipe can't stall ffmpeg
    t = threading.Thread(target=lambda: err_file.write(proc.stderr.read()), daemon=True)
    t.start()
    speed = ""
    for line in proc.stdout:
        key, _, val = line.strip().partition("=")
        if key == "speed" and val not in ("N/A", ""):
            speed = val
        elif key == "out_time_us" and val.isdigit() and duration > 0:
            pct = min(99, int(int(val) / 1e6 / duration * 100))
            job["progress"] = f"{label} {pct}%" + (f" ({speed} realtime)" if speed else "")
    proc.wait()
    t.join(timeout=5)
    if proc.returncode != 0:
        raise RuntimeError(err_file.getvalue()[-800:] or "ffmpeg failed")


# Folders the "From this PC" picker may read. Nothing outside these is reachable.
# Add your own (e.g. an iCloud/OneDrive photo folder) in config.json — see
# config.example.json. Keys are ids, values are [label, path].
HOME = os.path.expanduser("~")
LIBRARY_ROOTS = {
    "downloads": ("Downloads", os.path.join(HOME, "Downloads")),
    "videos": ("Videos", os.path.join(HOME, "Videos")),
    "pictures": ("Pictures", os.path.join(HOME, "Pictures")),
    "desktop": ("Desktop", os.path.join(HOME, "Desktop")),
    "output": ("Media Toolbox results", OUTPUT_DIR),
}
for key, entry in CONFIG.get("library_roots", {}).items():
    if isinstance(entry, (list, tuple)) and len(entry) == 2:
        LIBRARY_ROOTS[key] = (entry[0], os.path.expanduser(entry[1]))


def resolve_library_path(rel):
    """Turn 'root/sub/file.mp4' into a real path, refusing anything outside the roots."""
    root_key, _, sub = rel.replace("\\", "/").partition("/")
    entry = LIBRARY_ROOTS.get(root_key)
    if not entry:
        raise ValueError("unknown folder")
    base = os.path.realpath(entry[1])
    full = os.path.realpath(os.path.join(base, sub))
    if full != base and not full.startswith(base + os.sep):
        raise ValueError("path outside the allowed folder")   # blocks ../ traversal
    return full


class ServerFile:
    """A file already on this PC, shaped like an uploaded file so the workers don't care."""

    def __init__(self, path):
        self.path = path
        self.filename = os.path.basename(path)
        self._stream = None

    @property
    def stream(self):
        if self._stream is None:
            self._stream = open(self.path, "rb")
        return self._stream

    def read(self):
        with open(self.path, "rb") as fh:
            return fh.read()

    def save(self, dst):
        shutil.copyfile(self.path, dst)


def save_uploads(job_id, files):
    """Stage inputs for a job; returns [(path, original_name, is_temp)].

    Uploads are copied into the staging folder (and deleted afterwards). Files picked
    from this PC are used where they are, and must never be deleted.
    """
    saved = []
    for i, f in enumerate(files, 1):
        if isinstance(f, ServerFile):
            saved.append((f.path, f.filename, False))
            continue
        ext = os.path.splitext(f.filename)[1][:10]
        path = os.path.join(UPLOAD_DIR, f"{job_id}_{i}_{safe_base(f.filename)}{ext}")
        f.save(path)
        saved.append((path, f.filename, True))
    return saved


def uploaded_files(field="files"):
    """Uploaded files plus any files chosen from this PC."""
    files = [f for f in request.files.getlist(field) + request.files.getlist("file")
             if f and f.filename]
    picked = request.form.get("server_files")
    if picked:
        for rel in json.loads(picked):
            path = resolve_library_path(rel)
            if os.path.isfile(path):
                files.append(ServerFile(path))
    return files


MEDIA_KINDS = {
    "video": {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"},
    "audio": {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus"},
    "image": IMAGE_EXTS | {".exr"},
    "pdf": {".pdf"},
}


THUMB_DIR = os.path.join(BASE_DIR, "thumbs")
os.makedirs(THUMB_DIR, exist_ok=True)
# reading a cloud placeholder downloads it, so only do that automatically for small files
AUTO_THUMB_MAX = 40 * 1048576


def is_placeholder(path):
    """True if the file is a cloud placeholder that must be downloaded before reading."""
    try:
        a = os.stat(path).st_file_attributes
        return bool(a & 0x400000 or a & 0x40000 or a & 0x1000)   # RECALL_ON_DATA_ACCESS / RECALL_ON_OPEN / OFFLINE
    except (OSError, AttributeError):
        return False


def make_thumb(path, size=320):
    """Return JPEG thumbnail bytes for an image or video, cached on disk."""
    st = os.stat(path)
    key = hashlib.md5(f"{path}|{st.st_mtime_ns}|{size}".encode("utf-8")).hexdigest()
    cached = os.path.join(THUMB_DIR, f"{key}.jpg")
    if os.path.exists(cached):
        with open(cached, "rb") as fh:
            return fh.read()

    ext = os.path.splitext(path)[1].lower()
    if ext in MEDIA_KINDS["video"]:
        # grab a frame a little way in, so we don't get a black opening frame
        r = subprocess.run(
            [FFMPEG, "-hide_banner", "-loglevel", "error", "-ss", "1", "-i", path,
             "-frames:v", "1", "-vf", f"scale={size}:-1:force_original_aspect_ratio=decrease",
             "-f", "image2", "-c:v", "mjpeg", "-"],
            capture_output=True, timeout=900, creationflags=subprocess.CREATE_NO_WINDOW)
        data = r.stdout
        if not data:      # very short clip: retry from the first frame
            r = subprocess.run(
                [FFMPEG, "-hide_banner", "-loglevel", "error", "-i", path,
                 "-frames:v", "1", "-vf", f"scale={size}:-1:force_original_aspect_ratio=decrease",
                 "-f", "image2", "-c:v", "mjpeg", "-"],
                capture_output=True, timeout=900, creationflags=subprocess.CREATE_NO_WINDOW)
            data = r.stdout
        if not data:
            raise RuntimeError("could not read a frame")
    else:
        img = Image.open(path)
        img.draft("RGB", (size * 2, size * 2))        # fast partial decode for big JPEGs
        img = img.convert("RGB")
        img.thumbnail((size, size), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=80)
        img.close()
        data = buf.getvalue()

    with open(cached, "wb") as fh:
        fh.write(data)
    return data


# Windows gives no progress while iCloud downloads a placeholder: the first read
# blocks until the whole file arrives, and its on-disk size stays 0 until the end.
# So we estimate from measured throughput, refined after each download.
_hydrate_speed = 18.0          # MB/s, seeded from measurements on this machine
_speed_lock = threading.Lock()


def hydrate_worker(job_id, path):
    """Download a cloud placeholder, then build its thumbnail. Shows a time estimate."""
    global _hydrate_speed
    job = jobs[job_id]
    total_mb = os.path.getsize(path) / 1048576
    t0 = time.time()
    result = {}

    def pull():
        try:
            with open(path, "rb") as fh:
                fh.read(1048576)      # the first read is what triggers the download
            result["ok"] = True
        except Exception as e:
            result["err"] = str(e)[:300]

    t = threading.Thread(target=pull, daemon=True)
    t.start()

    def fmt(secs):
        secs = int(secs)
        return f"{secs}s" if secs < 60 else f"{secs // 60}m {secs % 60:02d}s"

    with _speed_lock:
        eta = max(3.0, total_mb / _hydrate_speed)
    while t.is_alive():
        if job.get("cancel"):
            # the read can't be interrupted; let it finish in the background
            job["status"] = "error"
            job["error"] = "cancelled"
            return
        left = max(0, eta - (time.time() - t0))
        job["progress"] = f"{fmt(left)} left" if left > 1 else "almost there"
        t.join(timeout=1)

    if "err" in result:
        job["status"] = "error"
        job["error"] = result["err"]
        return
    took = max(0.5, time.time() - t0)
    with _speed_lock:      # learn this machine's real speed for the next estimate
        _hydrate_speed = max(1.0, 0.7 * _hydrate_speed + 0.3 * (total_mb / took))
    try:
        job["progress"] = "almost there"
        make_thumb(path)
        job["status"] = "done"
        job["progress"] = "done"
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)[:300]


@app.route("/api/thumb_start", methods=["POST"])
def api_thumb_start():
    """Kick off downloading + previewing one cloud file; poll /api/status for progress."""
    try:
        path = resolve_library_path((request.get_json(silent=True) or {}).get("path", ""))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if not os.path.isfile(path):
        return jsonify({"error": "not found"}), 404
    job_id = new_job()
    threading.Thread(target=hydrate_worker, args=(job_id, path), daemon=True).start()
    return jsonify({"job_id": job_id, "mb": round(os.path.getsize(path) / 1048576, 1)})


@app.route("/api/thumb_cancel/<job_id>", methods=["POST"])
def api_thumb_cancel(job_id):
    job = jobs.get(job_id)
    if job:
        job["cancel"] = True
    return jsonify({"ok": True})


@app.route("/api/thumb")
def api_thumb():
    try:
        path = resolve_library_path(request.args.get("path", ""))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if not os.path.isfile(path):
        return jsonify({"error": "not found"}), 404
    ext = os.path.splitext(path)[1].lower()
    if ext not in MEDIA_KINDS["video"] | MEDIA_KINDS["image"]:
        return jsonify({"error": "no preview for this type"}), 415
    # don't silently download a huge cloud file just to draw a thumbnail
    if (request.args.get("force") != "1" and is_placeholder(path)
            and os.path.getsize(path) > AUTO_THUMB_MAX):
        return jsonify({"error": "cloud file", "needs_download": True,
                        "mb": round(os.path.getsize(path) / 1048576, 1)}), 409
    try:
        data = make_thumb(path)
    except Exception as e:
        return jsonify({"error": str(e)[:200]}), 500
    resp = app.response_class(data, mimetype="image/jpeg")
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


@app.route("/api/browse")
def api_browse():
    """List folders/files under one of the allowed roots, newest first."""
    rel = request.args.get("path", "")
    kinds = [k for k in request.args.get("kind", "").split(",") if k in MEDIA_KINDS]
    query = request.args.get("q", "").strip().lower()
    offset = max(0, int(request.args.get("offset", 0)))
    limit = min(200, max(1, int(request.args.get("limit", 100))))

    if not rel:      # top level: the roots themselves
        return jsonify({"path": "", "parent": None, "dirs": [
            {"name": label, "path": key} for key, (label, p) in LIBRARY_ROOTS.items()
            if os.path.isdir(p)], "files": [], "total": 0})
    try:
        full = resolve_library_path(rel)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if not os.path.isdir(full):
        return jsonify({"error": "not a folder"}), 404

    wanted = set().union(*(MEDIA_KINDS[k] for k in kinds)) if kinds else None
    dirs, files = [], []
    with os.scandir(full) as it:
        for e in it:
            if e.name.startswith("."):
                continue
            if query and query not in e.name.lower():
                continue
            try:
                if e.is_dir():
                    dirs.append({"name": e.name, "path": f"{rel}/{e.name}"})
                elif wanted is None or os.path.splitext(e.name)[1].lower() in wanted:
                    st = e.stat()
                    ext = os.path.splitext(e.name)[1].lower()
                    cloud = bool(getattr(st, "st_file_attributes", 0) & 0x400000
                                 or getattr(st, "st_file_attributes", 0) & 0x40000)
                    files.append({
                        "name": e.name, "path": f"{rel}/{e.name}",
                        "mb": round(st.st_size / 1048576, 1), "mtime": st.st_mtime,
                        "kind": "video" if ext in MEDIA_KINDS["video"]
                                else "image" if ext in MEDIA_KINDS["image"]
                                else "audio" if ext in MEDIA_KINDS["audio"] else "file",
                        # previews for big cloud files need an explicit tap (avoids surprise downloads)
                        "cloud": cloud and st.st_size > AUTO_THUMB_MAX,
                    })
            except OSError:
                continue
    dirs.sort(key=lambda d: d["name"].lower())
    files.sort(key=lambda f: f["mtime"], reverse=True)          # newest first
    total = len(files)
    parent = rel.rsplit("/", 1)[0] if "/" in rel else ""
    return jsonify({"path": rel, "parent": parent, "dirs": dirs,
                    "files": files[offset:offset + limit], "total": total,
                    "offset": offset})


def convert_one(in_path, out_path, target, job, tag, opts=None):
    """Convert one file, copying streams when possible and re-encoding otherwise."""
    job["progress"] = f"{tag}analyzing file..."
    duration, vcodec, acodec = probe_media(in_path)
    if target == "gif":
        run_ffmpeg(build_convert_cmd(in_path, out_path, target, vcodec, acodec, False, opts),
                   job, duration, f"{tag}making GIF")
        return
    if target in AUDIO_FORMATS:
        can_copy = acodec in COPY_AUDIO.get(target, set())
    else:
        can_copy = (vcodec in COPY_VIDEO.get(target, set())
                    and (acodec is None or acodec in COPY_AUDIO.get(target, set())))
    if can_copy:
        try:
            run_ffmpeg(build_convert_cmd(in_path, out_path, target, vcodec, acodec, True),
                       job, duration, f"{tag}repackaging (no re-encode)")
            return
        except RuntimeError:
            pass                                      # fall back to a full re-encode
    label = "converting on GPU" if (target not in AUDIO_FORMATS and target != "avi"
                                    and nvenc_available()) else "converting"
    run_ffmpeg(build_convert_cmd(in_path, out_path, target, vcodec, acodec, False, opts),
               job, duration, f"{tag}{label}")


def convert_worker(job_id, items, target, opts=None):
    """items: [(upload_path, original_name)]. Several outputs come back as one ZIP."""
    job = jobs[job_id]
    outputs, errors = [], []
    for i, (in_path, orig, is_temp) in enumerate(items, 1):
        tag = f"[{i}/{len(items)}] " if len(items) > 1 else ""
        name = f"{safe_base(orig)}.{target}"
        out_path = os.path.join(OUTPUT_DIR, f"{job_id}_{i}_{name}")
        try:
            convert_one(in_path, out_path, target, job, tag, opts)
            outputs.append((name, out_path))
        except Exception as e:
            errors.append(f"{orig}: {str(e)[-300:]}")
        finally:
            if is_temp:       # never delete a file the user picked from this PC
                try:
                    os.remove(in_path)
                except OSError:
                    pass
    try:
        finish_batch(job, job_id, outputs, f"converted_{target}", errors)
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)


@app.route("/api/convert", methods=["POST"])
def api_convert():
    files = uploaded_files()
    target = (request.form.get("target") or "").lower().lstrip(".")
    if not files:
        return jsonify({"error": "No file uploaded"}), 400
    if target not in ALLOWED_TARGETS:
        return jsonify({"error": f"Unsupported target format: {target}"}), 400
    try:
        opts = {"gif_fps": int(request.form.get("gif_fps", 12)),
                "gif_width": int(request.form.get("gif_width", 480))}
    except ValueError:
        return jsonify({"error": "GIF frame rate and width must be numbers"}), 400
    job_id = new_job()
    items = save_uploads(job_id, files)
    threading.Thread(target=convert_worker, args=(job_id, items, target, opts), daemon=True).start()
    return jsonify({"job_id": job_id})


# ---------------- Vocal / instrumental separation (Demucs) ----------------

def explain_separation_error(msg):
    if "Application Control policy" in msg or "WinError 4551" in msg:
        return ("Windows blocked a PyTorch file needed for vocal separation "
                "(an Application Control / WDAC policy on this machine, likely from "
                "an IT-managed or security-hardened setup, is preventing "
                "torch\\lib\\shm.dll from loading). This isn't something the app can "
                "work around by itself. Options: (1) ask whoever manages this policy "
                "to allow-list the Python venv's site-packages\\torch folder, or "
                "(2) run the app from a user account/machine without that restriction. "
                f"Raw error: {msg}")
    return msg


def separate_worker(job_id, items, out_fmt):
    """Split each song into vocals + instrumental. With several songs, each gets its own folder in the ZIP."""
    job = jobs[job_id]
    ext = "mp3" if out_fmt == "mp3" else "wav"
    outputs, errors = [], []
    try:
        job["progress"] = "loading AI model (first run downloads ~80 MB)..."
        import separator
    except Exception as e:
        job["status"] = "error"
        job["error"] = explain_separation_error(str(e))
        for p, _, is_temp in items:
            if is_temp:
                try:
                    os.remove(p)
                except OSError:
                    pass
        return
    for i, (in_path, orig, is_temp) in enumerate(items, 1):
        tag = f"[{i}/{len(items)}] " if len(items) > 1 else ""
        # leave room for the "_instrumental.mp3" suffix within Windows' path limit
        base = safe_base(orig, 45)
        folder = f"{base}/" if len(items) > 1 else ""
        vocals_path = os.path.join(OUTPUT_DIR, f"{job_id}_{i}_vocals.{ext}")
        instr_path = os.path.join(OUTPUT_DIR, f"{job_id}_{i}_instrumental.{ext}")
        try:
            separator.separate(FFMPEG, in_path, vocals_path, instr_path, out_fmt,
                               progress=lambda m, t=tag: job.__setitem__("progress", t + m))
            outputs.append((f"{folder}{base}_vocals.{ext}", vocals_path))
            outputs.append((f"{folder}{base}_instrumental.{ext}", instr_path))
        except Exception as e:
            errors.append(f"{orig}: {explain_separation_error(str(e))[-300:]}")
            for p in (vocals_path, instr_path):
                try:
                    os.remove(p)
                except OSError:
                    pass
        finally:
            if is_temp:       # never delete a file the user picked from this PC
                try:
                    os.remove(in_path)
                except OSError:
                    pass
    label = f"{safe_base(items[0][1], 45)}_stems" if len(items) == 1 else "vocal_splits"
    try:
        finish_batch(job, job_id, outputs, label, errors)
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)


@app.route("/api/separate", methods=["POST"])
def api_separate():
    files = uploaded_files()
    out_fmt = (request.form.get("format") or "mp3").lower()
    if not files:
        return jsonify({"error": "No audio/video file uploaded"}), 400
    if out_fmt not in ("mp3", "wav"):
        return jsonify({"error": "Output format must be mp3 or wav"}), 400
    job_id = new_job()
    items = save_uploads(job_id, files)
    threading.Thread(target=separate_worker, args=(job_id, items, out_fmt), daemon=True).start()
    return jsonify({"job_id": job_id})


# ---------------- Images -> PDF (customizable layout) ----------------

PAGE_SIZES = {  # points (1 pt = 1/72 inch)
    "a4": (595, 842),
    "letter": (612, 792),
    "legal": (612, 1008),
    "a5": (420, 595),
    "fit": None,  # page sized to the image
}


def pdf_image_bytes(f):
    """Image bytes PyMuPDF can embed. It can't read HEIC, so those become high-quality JPEGs."""
    data = f.read()
    if os.path.splitext(f.filename)[1].lower() in (".heic", ".heif"):
        img = Image.open(io.BytesIO(data))
        buf = io.BytesIO()
        img.convert("RGB").save(buf, "JPEG", quality=95)
        data = buf.getvalue()
    return data


def finish_job(out_path):
    job_id = uuid.uuid4().hex[:12]
    jobs[job_id] = {"status": "done", "progress": "done", "file": out_path, "error": None}
    return jsonify({"job_id": job_id})


@app.route("/api/img2pdf", methods=["POST"])
def api_img2pdf():
    files = [f for f in uploaded_files()
             if os.path.splitext(f.filename)[1].lower() in IMAGE_EXTS]
    if not files:
        return jsonify({"error": "No image files uploaded (jpg/png/bmp/webp/tiff/gif)"}), 400

    form = request.form
    page_size = form.get("page_size", "a4").lower()
    orientation = form.get("orientation", "portrait")
    margin = max(0.0, float(form.get("margin", 36)))          # points
    scale = min(100.0, max(1.0, float(form.get("scale", 100))))  # % of available area
    align = form.get("align", "center")  # e.g. "top-left", "center", "bottom-right"
    if page_size not in PAGE_SIZES:
        return jsonify({"error": f"Unknown page size: {page_size}"}), 400

    try:
        doc = fitz.open()
        for f in files:
            img_bytes = pdf_image_bytes(f)
            pix = fitz.Pixmap(img_bytes)
            iw, ih = pix.width, pix.height
            pix = None
            if PAGE_SIZES[page_size] is None:
                pw, ph = iw * 72 / 96, ih * 72 / 96  # assume 96 dpi
                page = doc.new_page(width=pw, height=ph)
                page.insert_image(page.rect, stream=img_bytes)
                continue
            pw, ph = PAGE_SIZES[page_size]
            if orientation == "landscape":
                pw, ph = ph, pw
            page = doc.new_page(width=pw, height=ph)
            avail_w = pw - 2 * margin
            avail_h = ph - 2 * margin
            if avail_w <= 0 or avail_h <= 0:
                doc.close()
                return jsonify({"error": "Margin too large for the page size"}), 400
            # fit image into available area, then apply scale %
            ratio = min(avail_w / iw, avail_h / ih) * (scale / 100.0)
            w, h = iw * ratio, ih * ratio
            vert, _, horiz = align.partition("-")
            if not horiz:  # "center"
                vert = horiz = "center"
            x = {"left": margin, "center": (pw - w) / 2, "right": pw - margin - w}[horiz]
            y = {"top": margin, "center": (ph - h) / 2, "bottom": ph - margin - h}[vert]
            page.insert_image(fitz.Rect(x, y, x + w, y + h), stream=img_bytes)
        out_path = os.path.join(OUTPUT_DIR, f"{uuid.uuid4().hex[:12]}_images.pdf")
        doc.save(out_path)
        doc.close()
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return finish_job(out_path)


# ---------------- Images -> PDF (free-form layout from visual editor) ----------------

@app.route("/api/img2pdf_custom", methods=["POST"])
def api_img2pdf_custom():
    import json
    files = uploaded_files()
    try:
        placements = json.loads(request.form.get("placements", "[]"))
    except json.JSONDecodeError:
        return jsonify({"error": "Bad placements data"}), 400
    if not files or not placements:
        return jsonify({"error": "No images or placements provided"}), 400

    page_size = request.form.get("page_size", "a4").lower()
    orientation = request.form.get("orientation", "portrait")
    if PAGE_SIZES.get(page_size) is None:
        return jsonify({"error": "Custom layout needs a fixed page size"}), 400
    pw, ph = PAGE_SIZES[page_size]
    if orientation == "landscape":
        pw, ph = ph, pw

    file_bytes = [pdf_image_bytes(f) for f in files]
    try:
        doc = fitz.open()
        n_pages = max(int(p["page"]) for p in placements) + 1
        for _ in range(n_pages):
            doc.new_page(width=pw, height=ph)
        for p in placements:
            idx = int(p["file"])
            if not (0 <= idx < len(file_bytes)):
                continue
            # x, y, w, h are fractions of the page
            x, y = float(p["x"]) * pw, float(p["y"]) * ph
            w, h = float(p["w"]) * pw, float(p["h"]) * ph
            page = doc[int(p["page"])]
            page.insert_image(fitz.Rect(x, y, x + w, y + h),
                              stream=file_bytes[idx], keep_proportion=False)
        out_path = os.path.join(OUTPUT_DIR, f"{uuid.uuid4().hex[:12]}_layout.pdf")
        doc.save(out_path)
        doc.close()
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return finish_job(out_path)


# ---------------- PDF merge ----------------

@app.route("/api/pdf/merge", methods=["POST"])
def api_pdf_merge():
    files = [f for f in uploaded_files() if f.filename.lower().endswith(".pdf")]
    if len(files) < 2:
        return jsonify({"error": "Upload at least two PDF files"}), 400
    try:
        merged = fitz.open()
        for f in files:
            src = fitz.open(stream=f.read(), filetype="pdf")
            merged.insert_pdf(src)
            src.close()
        out_path = os.path.join(OUTPUT_DIR, f"{uuid.uuid4().hex[:12]}_merged.pdf")
        merged.save(out_path)
        merged.close()
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return finish_job(out_path)


# ---------------- PDF -> images (reverse) ----------------

@app.route("/api/pdf2img", methods=["POST"])
def api_pdf2img():
    picked = uploaded_files()
    f = picked[0] if picked else None
    fmt = request.form.get("format", "png").lower()
    dpi = min(600, max(36, int(request.form.get("dpi", 150))))
    if not f or not f.filename.lower().endswith(".pdf"):
        return jsonify({"error": "Upload a PDF file"}), 400
    if fmt not in ("png", "jpg", "jpeg", "webp"):
        return jsonify({"error": "Format must be png, jpg or webp"}), 400
    base = safe_base(f.filename)
    try:
        doc = fitz.open(stream=f.read(), filetype="pdf")
        job_id = uuid.uuid4().hex[:12]
        if doc.page_count == 1:
            pix = doc[0].get_pixmap(dpi=dpi)
            out_path = os.path.join(OUTPUT_DIR, f"{job_id}_{base}.{fmt}")
            pix.save(out_path)
        else:
            out_path = os.path.join(OUTPUT_DIR, f"{job_id}_{base}_pages.zip")
            make_zip(out_path, [(f"{base}_page{i:03d}.{fmt}", page.get_pixmap(dpi=dpi).tobytes(fmt))
                                for i, page in enumerate(doc, 1)])
        doc.close()
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    jobs[job_id] = {"status": "done", "progress": "done", "file": out_path, "error": None}
    return jsonify({"job_id": job_id})


# ---------------- Image format conversion ----------------

IMG_TARGETS = {"jpg", "jpeg", "png", "webp", "bmp", "tiff", "gif", "ico"}


def load_exr(data):
    """Decode an EXR (HDR float) image and tone-map it to an 8-bit PIL image."""
    import imageio.v3 as iio
    import numpy as np
    tmp = os.path.join(UPLOAD_DIR, f"{uuid.uuid4().hex[:12]}.exr")
    with open(tmp, "wb") as fh:
        fh.write(data)
    try:
        arr = iio.imread(tmp, plugin="EXR-FI")
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    arr = np.asarray(arr, dtype="float32")
    if arr.ndim == 2:                       # grayscale
        arr = arr[:, :, None].repeat(3, axis=2)
    alpha = None
    if arr.shape[2] == 4:
        alpha = arr[:, :, 3]
        arr = arr[:, :, :3]
    # linear HDR -> sRGB: clip negatives, gamma-encode, 8-bit
    arr = np.clip(arr, 0, None) ** (1 / 2.2)
    arr = np.clip(arr * 255, 0, 255).astype("uint8")
    if alpha is not None:
        a = np.clip(alpha * 255, 0, 255).astype("uint8")
        return Image.fromarray(np.dstack([arr, a]), "RGBA")
    return Image.fromarray(arr, "RGB")


@app.route("/api/imgconvert", methods=["POST"])
def api_imgconvert():
    files = [f for f in uploaded_files()
             if os.path.splitext(f.filename)[1].lower() in (IMAGE_EXTS | {".exr"})]
    target = (request.form.get("target") or "").lower().lstrip(".")
    if not files:
        return jsonify({"error": "No image files uploaded"}), 400
    if target not in IMG_TARGETS:
        return jsonify({"error": f"Unsupported target format: {target}"}), 400
    pil_fmt = {"jpg": "JPEG", "jpeg": "JPEG", "tiff": "TIFF", "ico": "ICO"}.get(target, target.upper())
    job_id = uuid.uuid4().hex[:12]
    try:
        outputs = []
        for f in files:
            if f.filename.lower().endswith(".exr"):
                img = load_exr(f.read())
            else:
                img = Image.open(f.stream)
            if pil_fmt == "JPEG" and img.mode in ("RGBA", "P", "LA"):
                img = img.convert("RGB")
            base = safe_base(f.filename)
            buf = io.BytesIO()
            img.save(buf, pil_fmt)
            img.close()
            outputs.append((f"{base}.{target}", buf.getvalue()))
        if len(outputs) == 1:
            out_path = os.path.join(OUTPUT_DIR, f"{job_id}_{outputs[0][0]}")
            with open(out_path, "wb") as fh:
                fh.write(outputs[0][1])
        else:
            out_path = os.path.join(OUTPUT_DIR, f"{job_id}_converted_images.zip")
            make_zip(out_path, outputs)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    jobs[job_id] = {"status": "done", "progress": "done", "file": out_path, "error": None}
    return jsonify({"job_id": job_id})


# ---------------- Image compression ----------------

def encode_image(img, fmt, quality):
    """Encode a PIL image to bytes at the given quality."""
    buf = io.BytesIO()
    if fmt == "PNG":
        # PNG is lossless: quality maps to the palette size used to shrink it
        colors = max(2, min(256, int(2 + (quality / 100) * 254)))
        img.convert("RGB").quantize(colors=colors).save(buf, "PNG", optimize=True)
    elif fmt == "WEBP":
        img.save(buf, "WEBP", quality=quality, method=4)
    else:
        img.save(buf, "JPEG", quality=quality, optimize=True, progressive=True)
    return buf.getvalue()


def compress_to_target(img, fmt, target_bytes):
    """Shrink an image toward target_bytes: binary-search quality, then downscale.

    Returns (data, quality, scale_percent). Never returns something larger than
    the smallest option it found.
    """
    best = None                       # smallest result seen so far
    for scale in (1.0, 0.85, 0.7, 0.55, 0.4, 0.3, 0.2):
        work = img
        if scale < 1.0:
            w, h = max(1, int(img.width * scale)), max(1, int(img.height * scale))
            work = img.resize((w, h), Image.LANCZOS)
        lo, hi, fit = 5, 95, None
        while lo <= hi:                # binary search the highest quality that fits
            mid = (lo + hi) // 2
            data = encode_image(work, fmt, mid)
            if best is None or len(data) < len(best[0]):
                best = (data, mid, int(scale * 100))
            if len(data) <= target_bytes:
                fit = (data, mid, int(scale * 100))
                lo = mid + 1
            else:
                hi = mid - 1
        if fit:
            return fit                 # hit the target at this scale
        if work is not img:
            work.close()
    return best                        # couldn't reach it; return the smallest we got


@app.route("/api/imgcompress", methods=["POST"])
def api_imgcompress():
    files = [f for f in uploaded_files()
             if os.path.splitext(f.filename)[1].lower() in (IMAGE_EXTS | {".exr"})]
    if not files:
        return jsonify({"error": "No image files uploaded"}), 400

    mode = request.form.get("mode", "target")          # "target" (KB) or "quality"
    out_fmt = (request.form.get("format") or "jpg").lower()
    fmt = {"jpg": "JPEG", "jpeg": "JPEG", "webp": "WEBP", "png": "PNG"}.get(out_fmt)
    if not fmt:
        return jsonify({"error": "Output format must be jpg, webp or png"}), 400
    try:
        target_kb = float(request.form.get("target_kb", 200))
        quality = int(request.form.get("quality", 80))
    except ValueError:
        return jsonify({"error": "Target size and quality must be numbers"}), 400
    if mode == "target" and target_kb <= 0:
        return jsonify({"error": "Target size must be greater than 0 KB"}), 400
    quality = max(5, min(95, quality))
    ext = "jpg" if fmt == "JPEG" else out_fmt

    job_id = uuid.uuid4().hex[:12]
    try:
        outputs, report = [], []
        for f in files:
            raw = f.read()
            img = load_exr(raw) if f.filename.lower().endswith(".exr") else Image.open(io.BytesIO(raw))
            img.load()
            if fmt in ("JPEG", "PNG") and img.mode in ("RGBA", "P", "LA"):
                img = img.convert("RGB")
            elif img.mode not in ("RGB", "RGBA", "L"):
                img = img.convert("RGB")

            if mode == "target":
                data, used_q, used_scale = compress_to_target(img, fmt, int(target_kb * 1024))
            else:
                data, used_q, used_scale = encode_image(img, fmt, quality), quality, 100
            img.close()

            base = safe_base(f.filename)
            outputs.append((f"{base}.{ext}", data))
            report.append({
                "name": f"{base}.{ext}",
                "original_kb": round(len(raw) / 1024, 1),
                "new_kb": round(len(data) / 1024, 1),
                "quality": used_q,
                "scale": used_scale,
                "hit_target": mode != "target" or len(data) <= int(target_kb * 1024),
            })

        if len(outputs) == 1:
            out_path = os.path.join(OUTPUT_DIR, f"{job_id}_{outputs[0][0]}")
            with open(out_path, "wb") as fh:
                fh.write(outputs[0][1])
        else:
            out_path = os.path.join(OUTPUT_DIR, f"{job_id}_compressed_images.zip")
            make_zip(out_path, outputs)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    jobs[job_id] = {"status": "done", "progress": "done", "file": out_path, "error": None}
    return jsonify({"job_id": job_id, "report": report})


# ---------------- Split a video into shareable clips ----------------

def split_once(in_path, out_pattern, seconds, job, tag):
    """Cut the file into fixed-length clips without re-encoding (no quality loss)."""
    cmd = [FFMPEG, "-y", "-hide_banner", "-i", in_path,
           "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy",
           "-f", "segment", "-segment_time", f"{seconds:.3f}",
           "-reset_timestamps", "1", "-segment_start_number", "1",
           "-progress", "pipe:1", "-nostats", out_pattern]
    run_ffmpeg(cmd, job, 0, f"{tag}splitting")


def split_worker(job_id, items, mode, limit_mb, clip_minutes):
    job = jobs[job_id]
    parts, errors = [], []
    for i, (in_path, orig, is_temp) in enumerate(items, 1):
        tag = f"[{i}/{len(items)}] " if len(items) > 1 else ""
        base = safe_base(orig, 40)
        ext = os.path.splitext(orig)[1].lower() or ".mp4"
        if ext not in (".mp4", ".mov", ".mkv", ".webm", ".avi"):
            ext = ".mp4"
        try:
            job["progress"] = f"{tag}analyzing video..."
            duration, _, _ = probe_media(in_path)
            total = os.path.getsize(in_path)
            if duration <= 0:
                raise RuntimeError("could not read the video's duration")

            if mode == "duration":
                seconds = max(1.0, clip_minutes * 60)
            else:
                limit = limit_mb * 1048576
                if total <= limit:
                    # already small enough: hand it back as a single clip
                    dest = os.path.join(OUTPUT_DIR, f"{job_id}_{i}_{base}{ext}")
                    shutil.copyfile(in_path, dest)
                    parts.append((f"{base}{ext}", dest))
                    continue
                seconds = duration * (limit / total) * 0.95   # 5% headroom

            made = []
            for attempt in range(3):
                for _, p in made:                    # clear the previous attempt
                    try:
                        os.remove(p)
                    except OSError:
                        pass
                pattern = os.path.join(OUTPUT_DIR, f"{job_id}_{i}_{base}_part%03d{ext}")
                split_once(in_path, pattern, seconds, job, tag)
                made = sorted((os.path.basename(f).split("_", 2)[2], os.path.join(OUTPUT_DIR, f))
                              for f in os.listdir(OUTPUT_DIR)
                              if f.startswith(f"{job_id}_{i}_{base}_part"))
                if mode == "duration" or not made:
                    break
                biggest = max(os.path.getsize(p) for _, p in made)
                if biggest <= limit_mb * 1048576:
                    break
                # a keyframe landed awkwardly and a clip came out too big: shorten and retry
                seconds *= (limit_mb * 1048576 / biggest) * 0.92
                job["progress"] = f"{tag}one clip was too big, retrying with shorter clips..."
            if not made:
                raise RuntimeError("ffmpeg produced no clips")
            parts.extend(made)
        except Exception as e:
            errors.append(f"{orig}: {str(e)[-300:]}")
        finally:
            if is_temp:       # never delete a file the user picked from this PC
                try:
                    os.remove(in_path)
                except OSError:
                    pass

    if not parts:
        job["status"] = "error"
        job["error"] = errors[0] if errors else "nothing was produced"
        return
    job["parts"] = parts
    job["file"] = parts[0][1]
    if errors:
        job["warning"] = f"{len(errors)} file(s) failed: " + "; ".join(errors)
    job["status"] = "done"
    job["progress"] = "done"


@app.route("/api/split", methods=["POST"])
def api_split():
    files = uploaded_files()
    mode = request.form.get("mode", "size")
    if not files:
        return jsonify({"error": "No video uploaded"}), 400
    try:
        limit_mb = float(request.form.get("limit_mb", 2000))
        clip_minutes = float(request.form.get("clip_minutes", 10))
    except ValueError:
        return jsonify({"error": "Size and length must be numbers"}), 400
    if mode == "size" and not 1 <= limit_mb <= 20000:
        return jsonify({"error": "Clip size must be between 1 MB and 20 GB"}), 400
    if mode == "duration" and not 0.1 <= clip_minutes <= 600:
        return jsonify({"error": "Clip length must be between 0.1 and 600 minutes"}), 400
    job_id = new_job()
    items = save_uploads(job_id, files)
    threading.Thread(target=split_worker, args=(job_id, items, mode, limit_mb, clip_minutes),
                     daemon=True).start()
    return jsonify({"job_id": job_id})


# ---------------- Job status / file fetch ----------------

@app.route("/api/status/<job_id>")
def api_status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "unknown job"}), 404
    resp = {"status": job["status"], "progress": job["progress"], "error": job["error"],
            "warning": job.get("warning")}
    if job.get("meta"):
        resp["meta"] = job["meta"]
    if job["status"] == "done" and job["file"]:
        resp["filename"] = os.path.basename(job["file"])[13:]  # strip "jobid_" prefix
    if job.get("parts"):   # split jobs return each clip separately, not as one ZIP
        resp["parts"] = [{"name": n, "mb": round(os.path.getsize(p) / 1048576, 1)}
                         for n, p in job["parts"] if os.path.exists(p)]
    return jsonify(resp)


@app.route("/api/file/<job_id>")
def api_file(job_id):
    job = jobs.get(job_id)
    if not job or job["status"] != "done" or not job["file"]:
        return jsonify({"error": "file not ready"}), 404
    name = os.path.basename(job["file"])[13:]
    return send_file(job["file"], as_attachment=True, download_name=name)


@app.route("/api/part/<job_id>/<int:index>")
def api_part(job_id, index):
    """Download one clip from a split job."""
    job = jobs.get(job_id)
    if not job or not job.get("parts") or not 0 <= index < len(job["parts"]):
        return jsonify({"error": "part not found"}), 404
    name, path = job["parts"][index]
    if not os.path.exists(path):
        return jsonify({"error": "file is gone"}), 404
    return send_file(path, as_attachment=True, download_name=name)


@app.route("/")
def index():
    return render_template("index.html")


if __name__ == "__main__":
    # Localhost only by default. Set "host": "0.0.0.0" in config.json (or HOST=0.0.0.0)
    # to reach it from your other devices — do that only on a trusted/private network
    # such as Tailscale, since the app has no password and can browse LIBRARY_ROOTS.
    host = os.environ.get("HOST") or CONFIG.get("host", "127.0.0.1")
    port = int(os.environ.get("PORT") or CONFIG.get("port", 5000))
    print(f"Open http://127.0.0.1:{port} in your browser")
    if host == "0.0.0.0":
        print(f"Also reachable from your other devices on this network, port {port}")
    app.run(host=host, port=port, debug=False, threaded=True)
