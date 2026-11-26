"""AI computer-vision layer.

1. ``CanopyModel`` – a Random-Forest pixel classifier trained *per site* on the
   surrounding landscape: Sentinel-2 spectral features (6 bands + 4 indices) as
   inputs, ESA WorldCover "tree cover" as labels. Training locally adapts the
   model to the biome, soil and sensor conditions of each project, and the
   held-out accuracy is reported as part of the evidence.
2. Biomass model – canopy probability x NDVI-scaled above-ground biomass,
   expanded with IPCC root:shoot, carbon fraction and CO2/C ratio.
3. Drone analysis – Excess-Green index + Otsu thresholding on RGB orthophotos.
"""
import io

import numpy as np

from . import config
from .satellite import BANDS

FEATURES = BANDS + ["NDVI", "EVI", "NDMI", "NBR"]


def indices(c):
    with np.errstate(all="ignore"):
        b, g, r, n, s1, s2 = (c[k] for k in BANDS)
        ndvi = (n - r) / (n + r)
        evi = 2.5 * (n - r) / (n + 6 * r - 7.5 * b + 1)
        ndmi = (n - s1) / (n + s1)
        nbr = (n - s2) / (n + s2)
    return {"NDVI": ndvi, "EVI": np.clip(evi, -1, 2), "NDMI": ndmi, "NBR": nbr}


def feature_stack(c):
    idx = indices(c)
    arrs = [c[k] for k in BANDS] + [idx[k] for k in ["NDVI", "EVI", "NDMI", "NBR"]]
    X = np.stack(arrs, axis=-1).astype("float32")
    valid = np.isfinite(X).all(axis=-1)
    return X, valid, idx["NDVI"]


class CanopyModel:
    def __init__(self):
        self.clf = None
        self.metrics = {}
        self.kind = None

    def fit(self, composite, labels, log=print, seed=0):
        from sklearn.ensemble import RandomForestClassifier
        from sklearn.metrics import accuracy_score, cohen_kappa_score, f1_score, precision_score, recall_score
        from sklearn.model_selection import train_test_split

        X, valid, ndvi = feature_stack(composite)
        known = valid & (labels > 0) & (labels != 80)  # drop nodata and open water from training
        y = labels[known] == 10                        # ESA WorldCover class 10 = tree cover
        Xk = X[known]
        n_tree, n_other = int(y.sum()), int((~y).sum())
        if n_tree < 40 or n_other < 40:
            self.kind = "ndvi-threshold"
            self.metrics = {"model": "NDVI>0.6 rule (too few labelled tree/non-tree pixels nearby to train AI)",
                            "train_tree_px": n_tree, "train_other_px": n_other}
            log("Not enough labelled pixels to train; falling back to NDVI threshold rule.")
            return self

        rng = np.random.default_rng(seed)
        per_class = 5000
        ti = rng.choice(np.flatnonzero(y), min(per_class, n_tree), replace=False)
        oi = rng.choice(np.flatnonzero(~y), min(per_class, n_other), replace=False)
        sel = np.concatenate([ti, oi])
        Xtr, Xte, ytr, yte = train_test_split(Xk[sel], y[sel], test_size=0.3, random_state=seed, stratify=y[sel])
        self.clf = RandomForestClassifier(n_estimators=120, max_depth=14, min_samples_leaf=2,
                                          class_weight="balanced", n_jobs=-1, random_state=seed)
        self.clf.fit(Xtr, ytr)
        pred = self.clf.predict(Xte)
        imp = sorted(zip(FEATURES, self.clf.feature_importances_), key=lambda t: -t[1])
        self.kind = "random-forest"
        self.metrics = {
            "model": "RandomForest(120 trees) per-site, labels = ESA WorldCover 2021 tree cover",
            "train_tree_px": n_tree, "train_other_px": n_other,
            "test_samples": int(len(yte)),
            "accuracy": round(float(accuracy_score(yte, pred)), 4),
            "precision": round(float(precision_score(yte, pred)), 4),
            "recall": round(float(recall_score(yte, pred)), 4),
            "f1": round(float(f1_score(yte, pred)), 4),
            "kappa": round(float(cohen_kappa_score(yte, pred)), 4),
            "top_features": [[k, round(float(v), 3)] for k, v in imp[:5]],
        }
        log(f"Canopy model trained: accuracy {self.metrics['accuracy']:.1%}, F1 {self.metrics['f1']:.2f} "
            f"on {len(yte)} held-out pixels.")
        return self

    def predict(self, composite):
        """Returns per-pixel tree-canopy probability (NaN where cloudy/no data) and NDVI."""
        X, valid, ndvi = feature_stack(composite)
        prob = np.full(valid.shape, np.nan, dtype="float32")
        if self.kind == "random-forest":
            if valid.any():
                prob[valid] = self.clf.predict_proba(X[valid])[:, 1]
        else:
            prob[valid] = np.clip((ndvi[valid] - 0.45) / 0.3, 0, 1)
        return prob, ndvi


def site_metrics(prob, ndvi, mask):
    """Canopy and carbon stock inside the claimed polygon for one epoch."""
    site_px = int(mask.sum())
    v = mask & np.isfinite(prob)
    cov = float(v.sum()) / site_px if site_px else 0.0
    if v.sum() == 0:
        return {"coverage": 0.0}
    p = prob[v]
    nd = np.clip(np.nan_to_num(ndvi[v]), 0, 1)
    canopy_frac = float((p >= 0.5).mean())
    # AGB density per pixel: canopy probability x greenness-scaled biomass (NDVI 0.3 -> 0, 0.9 -> max)
    vigor = np.clip((nd - 0.3) / 0.6, 0, 1) ** 1.5
    agb_density = config.AGB_MAX_T_HA * p * vigor            # t dry matter / ha
    agb_mean = float(agb_density.mean())                      # extrapolate valid pixels to whole site
    site_ha = site_px * 100 / 10_000
    agb_t = agb_mean * site_ha
    total_biomass = agb_t * (1 + config.ROOT_SHOOT_RATIO)
    tco2e = total_biomass * config.CARBON_FRACTION * config.CO2_PER_C
    return {
        "coverage": round(cov, 4),
        "canopy_pct": round(canopy_frac * 100, 2),
        "canopy_ha": round(canopy_frac * site_ha, 3),
        "mean_ndvi": round(float(np.nanmean(ndvi[v])), 4),
        "agb_t_ha": round(agb_mean, 2),
        "agb_t": round(agb_t, 2),
        "carbon_stock_tco2e": round(tco2e, 2),
    }


# ------------------------------------------------------------------ drone ----
def _otsu(values, bins=256):
    hist, edges = np.histogram(values, bins=bins)
    centers = (edges[:-1] + edges[1:]) / 2
    w = np.cumsum(hist)
    w2 = w[-1] - w
    m = np.cumsum(hist * centers)
    mu1 = m / np.maximum(w, 1)
    mu2 = (m[-1] - m) / np.maximum(w2, 1)
    var = w * w2 * (mu1 - mu2) ** 2
    return float(centers[np.argmax(var)])


def drone_canopy(image_bytes):
    """Canopy cover % from an RGB drone orthophoto using Excess-Green + Otsu."""
    from PIL import Image
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    img.thumbnail((1024, 1024))
    a = np.asarray(img).astype("float32")
    s = a.sum(axis=-1) + 1e-6
    r, g, b = a[..., 0] / s, a[..., 1] / s, a[..., 2] / s
    exg = 2 * g - r - b
    thr = max(_otsu(exg.ravel()), 0.05)  # never call grey/brown soil "vegetation"
    veg = exg > thr
    return {"canopy_pct": round(float(veg.mean()) * 100, 2), "exg_threshold": round(thr, 4),
            "size": list(img.size), "mask": veg}
