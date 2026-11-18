"""Pack downloaded XYZ tiles into 8x8 mosaics (2048 px JPEGs) so the page stays under the host's file limit.
Usage: python3 build_atlas.py TILEDIR OUTDIR INDEXJSON"""
import json, os, sys
from collections import defaultdict
from PIL import Image

src, out, idx_path = sys.argv[1:4]
A = 8
os.makedirs(out, exist_ok=True)
index = {}
for layer in ("bm", "usgs"):
    groups, keys = defaultdict(list), []
    for root, _, files in os.walk(os.path.join(src, layer)):
        for f in files:
            p = os.path.join(root, f)
            if not f.endswith(".jpg") or os.path.getsize(p) < 1500:
                continue
            z, x = map(int, root.split(os.sep)[-2:])
            y = int(f[:-4])
            groups[(z, x // A, y // A)].append((x, y, p))
            keys.append(f"{z}/{x}/{y}")
    for (z, ax, ay), items in groups.items():
        xs = [x % A for x, _, _ in items]
        ys = [y % A for _, y, _ in items]
        im = Image.new("RGB", (A * 256, A * 256), (12, 20, 28))
        for x, y, p in items:
            im.paste(Image.open(p).convert("RGB"), ((x % A) * 256, (y % A) * 256))
        im.save(os.path.join(out, f"{layer}_{z}_{ax}_{ay}.jpg"), quality=86, optimize=True, progressive=True)
    index[layer] = sorted(keys)
    print(layer, len(keys), "tiles in", len(groups), "mosaics")
json.dump(index, open(idx_path, "w"), separators=(",", ":"))
