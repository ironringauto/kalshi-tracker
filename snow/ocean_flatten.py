"""One-time imagery step after build_atlas.py: evens out ocean color in the Blue Marble mosaics (removes dark bathymetry blocks)."""
import sys, os, numpy as np
from PIL import Image
from scipy.ndimage import gaussian_filter, binary_erosion
SRC, DST = "/home/claude/atlas", "/home/claude/atlas_v2"
TARGET = np.array([22, 52, 98], "float32")  # deep ocean navy
def fix(path, out):
    im = np.asarray(Image.open(path).convert("RGB")).astype("float32")
    r, g, b = im[..., 0], im[..., 1], im[..., 2]
    water = (b > r * 1.3 + 6) & (b >= g * 0.95) & (r < 120)
    water = binary_erosion(water, iterations=1, border_value=1)
    m = gaussian_filter(water.astype("float32"), 1.5)
    # broad tone of the water only (no bathymetry ridges or tile-edge steps)
    wsum = gaussian_filter(im * water[..., None], (24, 24, 0))
    wcnt = gaussian_filter(water.astype("float32"), 24)[..., None]
    broad = np.where(wcnt > 1e-3, wsum / np.maximum(wcnt, 1e-3), TARGET)
    # shallow shelf near the coast stays a little lighter; deep water goes to one even navy
    lum = broad.mean(axis=2, keepdims=True)
    shelf = np.clip((lum - 60) / 60, 0, 1)
    new = TARGET * (1 - 0.55 * shelf) + broad * 0.55 * shelf
    outim = im * (1 - m[..., None]) + new * m[..., None]
    Image.fromarray(np.clip(outim, 0, 255).astype("uint8")).save(out, "JPEG", quality=84, optimize=True, progressive=True)
for f in sorted(os.listdir(SRC)):
    if f.startswith("bm_") and (len(sys.argv) < 2 or f in sys.argv[1:]):
        fix(os.path.join(SRC, f), os.path.join(DST, f))
        print(f, os.path.getsize(os.path.join(DST, f)))
