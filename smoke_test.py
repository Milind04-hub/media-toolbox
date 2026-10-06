import io
import subprocess
import time

import imageio_ffmpeg
import requests
from PIL import Image

BASE = "http://127.0.0.1:5000"


def jpg_bytes(color="red", size=(200, 100)):
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "JPEG")
    buf.seek(0)
    return buf


# --- img2pdf with layout options ---
r = requests.post(f"{BASE}/api/img2pdf",
                  files=[("files", ("a.jpg", jpg_bytes("red"), "image/jpeg")),
                         ("files", ("b.jpg", jpg_bytes("blue"), "image/jpeg"))],
                  data={"page_size": "a4", "orientation": "landscape", "margin": "50",
                        "scale": "60", "align": "top-right"})
j = r.json()
assert "job_id" in j, j
pdf1 = requests.get(f"{BASE}/api/file/{j['job_id']}").content
assert pdf1[:4] == b"%PDF"
print("img2pdf (custom layout) OK:", len(pdf1), "bytes")

# second pdf, fit-to-image mode
r = requests.post(f"{BASE}/api/img2pdf",
                  files=[("files", ("c.jpg", jpg_bytes("green"), "image/jpeg"))],
                  data={"page_size": "fit"})
pdf2 = requests.get(f"{BASE}/api/file/{r.json()['job_id']}").content
assert pdf2[:4] == b"%PDF"
print("img2pdf (fit mode) OK:", len(pdf2), "bytes")

# --- merge PDFs ---
r = requests.post(f"{BASE}/api/pdf/merge",
                  files=[("files", ("one.pdf", io.BytesIO(pdf1), "application/pdf")),
                         ("files", ("two.pdf", io.BytesIO(pdf2), "application/pdf"))])
j = r.json()
assert "job_id" in j, j
merged = requests.get(f"{BASE}/api/file/{j['job_id']}").content
assert merged[:4] == b"%PDF" and len(merged) > len(pdf2)
print("merge OK:", len(merged), "bytes (3 pages expected)")

# --- pdf -> images (reverse) ---
r = requests.post(f"{BASE}/api/pdf2img",
                  files={"file": ("merged.pdf", io.BytesIO(merged), "application/pdf")},
                  data={"format": "png", "dpi": "96"})
j = r.json()
assert "job_id" in j, j
s = requests.get(f"{BASE}/api/status/{j['job_id']}").json()
zip_data = requests.get(f"{BASE}/api/file/{j['job_id']}").content
assert zip_data[:2] == b"PK", "expected zip for multi-page"
print("pdf2img OK:", s["filename"], len(zip_data), "bytes")

# single page pdf -> single png
r = requests.post(f"{BASE}/api/pdf2img",
                  files={"file": ("single.pdf", io.BytesIO(pdf2), "application/pdf")},
                  data={"format": "png", "dpi": "96"})
png = requests.get(f"{BASE}/api/file/{r.json()['job_id']}").content
assert png[:8] == b"\x89PNG\r\n\x1a\n"
print("pdf2img (single page -> png) OK:", len(png), "bytes")

# --- image format conversion (jpg -> png, then png -> webp) ---
r = requests.post(f"{BASE}/api/imgconvert",
                  files=[("files", ("a.jpg", jpg_bytes(), "image/jpeg"))],
                  data={"target": "png"})
png2 = requests.get(f"{BASE}/api/file/{r.json()['job_id']}").content
assert png2[:8] == b"\x89PNG\r\n\x1a\n"
r = requests.post(f"{BASE}/api/imgconvert",
                  files=[("files", ("a.png", io.BytesIO(png2), "image/png")),
                         ("files", ("b.png", io.BytesIO(png2), "image/png"))],
                  data={"target": "webp"})
zip2 = requests.get(f"{BASE}/api/file/{r.json()['job_id']}").content
assert zip2[:2] == b"PK", "expected zip for multiple outputs"
print("imgconvert OK: jpg->png and 2x png->webp (zip)", len(zip2), "bytes")

# --- A/V convert: generate 1s test video, mp4 -> mp3 ---
ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
subprocess.run([ffmpeg, "-y", "-f", "lavfi", "-i", "testsrc=duration=1:size=320x240:rate=10",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                "-c:v", "libx264", "-c:a", "aac", "-shortest", "test.mp4"],
               check=True, capture_output=True)
with open("test.mp4", "rb") as f:
    r = requests.post(f"{BASE}/api/convert", files={"file": ("test.mp4", f)}, data={"target": "mp3"})
j = r.json()
for _ in range(60):
    s = requests.get(f"{BASE}/api/status/{j['job_id']}").json()
    if s["status"] != "running":
        break
    time.sleep(1)
assert s["status"] == "done", s
print("convert mp4->mp3 OK:", s["filename"])

print("\nALL TESTS PASSED")
