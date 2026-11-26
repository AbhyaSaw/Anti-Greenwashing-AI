"""Small PNG renderings embedded in the evidence report (true colour + AI canopy map)."""
import base64
import io

import numpy as np
from PIL import Image


def _png(arr):
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, format="PNG", optimize=True)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def _outline(mask):
    from scipy.ndimage import binary_erosion
    return mask & ~binary_erosion(mask, iterations=1)


def _upscale(arr, max_side=420):
    h, w = arr.shape[:2]
    k = max(1, int(max_side // max(h, w)))
    return np.repeat(np.repeat(arr, k, axis=0), k, axis=1) if k > 1 else arr


def true_color(comp, mask):
    rgb = np.stack([comp["B04"], comp["B03"], comp["B02"]], axis=-1)
    rgb = np.nan_to_num(rgb, nan=0.0)
    rgb = np.clip(rgb / 0.18, 0, 1) ** 0.8
    out = (rgb * 255).astype("uint8")
    out[_outline(mask)] = (255, 220, 0)
    return _png(_upscale(out))


def canopy_map(prob, mask):
    p = np.nan_to_num(prob, nan=-1)
    out = np.zeros(p.shape + (3,), dtype="uint8")
    out[...] = (60, 60, 60)                                         # no data
    t = np.clip(p, 0, 1)[..., None]
    green = np.array([20, 140, 60]); tan = np.array([225, 210, 170])
    col = (tan * (1 - t) + green * t).astype("uint8")
    out = np.where((p >= 0)[..., None], col, out)
    out[_outline(mask)] = (255, 60, 60)
    return _png(_upscale(out))


def mask_png(veg):
    out = np.where(veg[..., None], np.array([20, 140, 60], "uint8"), np.array([225, 210, 170], "uint8"))
    return _png(out.astype("uint8"))
