import os

import fitz
from PIL import Image

BASE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(BASE, "static")

doc = fitz.open(os.path.join(STATIC, "icon.svg"))
page = doc[0]

pngs = []
for size in (16, 32, 48, 64, 128, 192, 256, 512):
    zoom = size / page.rect.width
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=True)
    path = os.path.join(STATIC, f"icon-{size}.png")
    pix.save(path)
    pngs.append(path)
doc.close()

# keep only the sizes we serve; build a multi-size .ico
img = Image.open(os.path.join(STATIC, "icon-256.png"))
img.save(os.path.join(STATIC, "favicon.ico"),
         sizes=[(16, 16), (32, 32), (48, 48), (64, 64)])
print("generated:", sorted(os.listdir(STATIC)))
