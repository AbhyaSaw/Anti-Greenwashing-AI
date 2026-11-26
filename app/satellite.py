"""Satellite data access.

* ``Sentinel2Source`` – real, free Copernicus Sentinel-2 L2A imagery streamed as
  Cloud-Optimised GeoTIFF windows from Microsoft Planetary Computer (no API key),
  plus ESA WorldCover 10 m land-cover used as training labels for the AI model.
* ``SyntheticSource`` – physically plausible simulated imagery for offline demos
  and tests (an honest project and a fraudulent one). Always flagged as synthetic.

Both return the same structures, so the AI pipeline cannot tell them apart.
"""
import calendar
import math
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

os.environ.setdefault("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")
os.environ.setdefault("CPL_VSIL_CURL_ALLOWED_EXTENSIONS", ".tif,.TIF,.tiff")
os.environ.setdefault("GDAL_HTTP_MAX_RETRY", "5")
os.environ.setdefault("GDAL_HTTP_RETRY_DELAY", "2")
os.environ.setdefault("GDAL_HTTP_MULTIPLEX", "YES")
os.environ.setdefault("GDAL_HTTP_CONNECTTIMEOUT", "20")
os.environ.setdefault("GDAL_HTTP_TIMEOUT", "90")
os.environ.setdefault("VSI_CACHE", "TRUE")

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.features import rasterize
from rasterio.transform import from_origin
from rasterio.vrt import WarpedVRT
from rasterio.warp import transform_geom

from . import config

BANDS = ["B02", "B03", "B04", "B08", "B11", "B12"]  # blue, green, red, NIR, SWIR1, SWIR2
SCL_VALID = {4, 5, 6, 7, 11}  # vegetation, bare, water, unclassified, snow (clouds/shadows excluded)


class DataError(RuntimeError):
    pass


# ---------------------------------------------------------------- geometry ----
def _coords(geom):
    t = geom["type"]
    if t == "Polygon":
        for ring in geom["coordinates"]:
            yield from ring
    elif t == "MultiPolygon":
        for poly in geom["coordinates"]:
            for ring in poly:
                yield from ring
    else:
        raise DataError(f"Unsupported geometry type {t}; draw a polygon or rectangle.")


def geometry_of(geojson):
    if geojson.get("type") == "FeatureCollection":
        geojson = geojson["features"][0]
    if geojson.get("type") == "Feature":
        geojson = geojson["geometry"]
    return geojson


@dataclass
class Grid:
    crs: str
    transform: object
    width: int
    height: int
    mask: np.ndarray        # True inside the claimed site
    bbox_ll: list           # lon/lat bbox of the whole analysis window
    centroid: tuple         # lon, lat

    @property
    def site_ha(self):
        return float(self.mask.sum()) * 100 / 10_000  # 10 m pixels


def make_grid(geojson, buffer_m=None, res=10.0) -> Grid:
    geom = geometry_of(geojson)
    pts = list(_coords(geom))
    lon = sum(p[0] for p in pts) / len(pts)
    lat = sum(p[1] for p in pts) / len(pts)
    zone = int((lon + 180) // 6) + 1
    crs = f"EPSG:{(32600 if lat >= 0 else 32700) + zone}"
    g = transform_geom("EPSG:4326", crs, geom)
    xs, ys = zip(*_coords(g))
    buf = config.CONTEXT_BUFFER_M if buffer_m is None else buffer_m
    minx = math.floor((min(xs) - buf) / res) * res
    maxx = math.ceil((max(xs) + buf) / res) * res
    miny = math.floor((min(ys) - buf) / res) * res
    maxy = math.ceil((max(ys) + buf) / res) * res
    w, h = int((maxx - minx) / res), int((maxy - miny) / res)
    if max(w, h) > config.MAX_GRID_PX:
        raise DataError(f"Site too large for this demo oracle (window {w}x{h}px). Max ~{config.MAX_SITE_KM2:.0f} km².")
    transform = from_origin(minx, maxy, res, res)
    mask = rasterize([(g, 1)], out_shape=(h, w), transform=transform, fill=0, dtype="uint8").astype(bool)
    if mask.sum() < 4:
        raise DataError("Site polygon is smaller than a few Sentinel-2 pixels (10 m).")
    if mask.sum() * 100 / 1e6 > config.MAX_SITE_KM2:
        raise DataError(f"Site is larger than {config.MAX_SITE_KM2:.0f} km².")
    bb = transform_geom(crs, "EPSG:4326", {
        "type": "Polygon",
        "coordinates": [[(minx, miny), (maxx, miny), (maxx, maxy), (minx, maxy), (minx, miny)]],
    })
    bx, by = zip(*bb["coordinates"][0])
    return Grid(crs, transform, w, h, mask, [min(bx), min(by), max(bx), max(by)], (lon, lat))


def default_season(lat):
    """Peak-greenness months: avoids scoring deciduous forest in leaf-off winter."""
    if lat > 23.5:
        return (6, 9)
    if lat < -23.5:
        return (1, 3)
    return (1, 12)  # tropics: take the clearest scenes of the year


# ----------------------------------------------------------- Sentinel-2 ----
class Sentinel2Source:
    name = "sentinel-2"
    synthetic = False

    def __init__(self):
        import planetary_computer
        import pystac_client
        self.catalog = pystac_client.Client.open(config.STAC_URL, modifier=planetary_computer.sign_inplace)

    def _search(self, **kw):
        s = self.catalog.search(**kw)
        return list(s.items() if hasattr(s, "items") else s.get_items())

    def _read(self, href, grid, resampling, attempts=4):
        import time
        for k in range(attempts):  # unreliable networks: retry each window read with backoff
            try:
                with rasterio.open(href) as src:
                    with WarpedVRT(src, crs=grid.crs, transform=grid.transform, width=grid.width,
                                   height=grid.height, resampling=resampling, src_nodata=0, nodata=0) as vrt:
                        return vrt.read(1)
            except rasterio.errors.RasterioIOError:
                if k == attempts - 1:
                    raise
                time.sleep(2 * (k + 1))

    def composite(self, grid: Grid, year: int, season=None, log=print):
        """Cloud-masked median reflectance composite for one year's growing season."""
        m0, m1 = season or default_season(grid.centroid[1])
        dt = f"{year}-{m0:02d}-01/{year}-{m1:02d}-{calendar.monthrange(year, m1)[1]}"
        items = self._search(collections=[config.S2_COLLECTION], bbox=grid.bbox_ll, datetime=dt,
                             query={"eo:cloud_cover": {"lt": config.MAX_CLOUD_PCT}}, max_items=100)
        if not items:
            raise DataError(f"No Sentinel-2 scenes under {config.MAX_CLOUD_PCT:.0f}% cloud for {dt}.")
        items.sort(key=lambda it: it.properties.get("eo:cloud_cover", 100))
        items = items[: config.SCENES_PER_YEAR]
        log(f"{year}: compositing {len(items)} scene(s): " + ", ".join(
            f"{it.datetime:%Y-%m-%d} ({it.properties.get('eo:cloud_cover', 0):.0f}% cloud)" for it in items))

        jobs = [(i, b) for i in range(len(items)) for b in BANDS + ["SCL"]]

        def fetch(job):
            i, b = job
            rs = Resampling.nearest if b == "SCL" else Resampling.bilinear
            try:
                return job, self._read(items[i].assets[b].href, grid, rs)
            except Exception as e:
                return job, e

        with ThreadPoolExecutor(max_workers=4) as ex:
            raw = dict(ex.map(fetch, jobs))

        bad = {i for (i, _), v in raw.items() if isinstance(v, Exception)}
        for i in sorted(bad):
            log(f"{year}: dropped scene {items[i].datetime:%Y-%m-%d} (download failed)")
        if len(bad) == len(items):
            raise DataError(f"all {len(items)} scene downloads failed for {year}")
        items = [it if i not in bad else None for i, it in enumerate(items)]

        stack = {b: [] for b in BANDS}
        for i, it in enumerate(items):
            if it is None:
                continue
            baseline = float(it.properties.get("s2:processing_baseline", "0") or 0)
            offset = 1000.0 if baseline >= 4.0 else 0.0  # ESA added a +1000 DN offset from PB 04.00 (Jan 2022)
            scl = raw[(i, "SCL")]
            ok = np.isin(scl, list(SCL_VALID))
            for b in BANDS:
                dn = raw[(i, b)].astype("float32")
                refl = (dn - offset) / 10000.0
                refl[~ok | (dn == 0)] = np.nan
                stack[b].append(refl)
        with np.errstate(all="ignore"):
            import warnings
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)
                comp = {b: np.nanmedian(np.stack(v), axis=0) for b, v in stack.items()}
        comp["dates"] = [f"{it.datetime:%Y-%m-%d}" for it in items if it is not None]
        comp["scene_ids"] = [it.id for it in items if it is not None]
        return comp

    def landcover_labels(self, grid: Grid, log=print):
        items = self._search(collections=[config.LANDCOVER_COLLECTION], bbox=grid.bbox_ll,
                             datetime=f"{config.LANDCOVER_YEAR}-01-01/{config.LANDCOVER_YEAR}-12-31")
        if not items:
            raise DataError("No ESA WorldCover tiles for this site.")
        out = np.zeros((grid.height, grid.width), dtype="uint8")
        for it in items:
            arr = self._read(it.assets["map"].href, grid, Resampling.nearest)
            out = np.where(out == 0, arr, out)
        log(f"Loaded ESA WorldCover {config.LANDCOVER_YEAR} labels ({len(items)} tile(s)).")
        return out


# ------------------------------------------------------------ synthetic ----
# Endmember reflectances [B02, B03, B04, B08, B11, B12]
_TREE = np.array([0.025, 0.055, 0.028, 0.38, 0.17, 0.075], dtype="float32")
_GRASS = np.array([0.045, 0.085, 0.075, 0.27, 0.27, 0.16], dtype="float32")
_SOIL = np.array([0.090, 0.130, 0.170, 0.24, 0.33, 0.27], dtype="float32")


class SyntheticSource:
    """Simulated Sentinel-2 for offline demos. scenario: 'honest' | 'fraud'."""
    synthetic = True

    def __init__(self, scenario="honest", planting_year=2019, seed=7):
        self.scenario = scenario
        self.planting_year = planting_year
        self.seed = seed
        self.name = f"synthetic-{scenario}"

    def _landscape(self, grid):
        from scipy.ndimage import gaussian_filter
        rng = np.random.default_rng(self.seed)
        n = gaussian_filter(rng.random((grid.height, grid.width)), 6)
        n = (n - n.min()) / (np.ptp(n) + 1e-9)
        forest = (n > 0.62).astype("float32")            # existing natural forest patches
        soil = gaussian_filter(rng.random(n.shape), 3)
        soil = (soil - soil.min()) / (np.ptp(soil) + 1e-9)
        return forest, soil

    def _site_canopy(self, year):
        age = year - self.planting_year
        if self.scenario == "honest":
            return 0.03 + 0.72 / (1 + math.exp(-1.1 * (age - 3.0))) if age >= 0 else 0.03
        return 0.03 + 0.01 * max(age, 0) ** 0.5  # "trees planted" on paper; field stays bare

    def composite(self, grid, year, season=None, log=print):
        rng = np.random.default_rng(self.seed * 1000 + year)
        forest, soil = self._landscape(grid)
        canopy = forest * 0.9
        site = grid.mask
        frac = self._site_canopy(year)
        # canopy grows in planted rows/clumps, not uniformly
        clumps = np.clip(frac + rng.normal(0, 0.12, canopy.shape), 0, 1)
        canopy = np.where(site, clumps, canopy).astype("float32")
        bare = (1 - canopy) * (0.35 + 0.5 * soil)
        grass = 1 - canopy - bare
        cube = (canopy[..., None] * _TREE + grass[..., None] * _GRASS + bare[..., None] * _SOIL)
        cube += rng.normal(0, 0.006, cube.shape).astype("float32")
        cloud = rng.random(canopy.shape) < 0.03
        comp = {}
        for k, b in enumerate(BANDS):
            arr = cube[..., k].astype("float32")
            arr[cloud] = np.nan
            comp[b] = arr
        comp["dates"] = [f"{year}-07-15"]
        comp["scene_ids"] = [f"SYNTHETIC_{self.scenario.upper()}_{year}"]
        log(f"{year}: generated synthetic composite (scenario={self.scenario}).")
        return comp

    def landcover_labels(self, grid, log=print):
        forest, _ = self._landscape(grid)
        labels = np.where(forest > 0, 10, 30).astype("uint8")  # 10 tree cover, 30 grassland
        canopy_2021 = self._site_canopy(config.LANDCOVER_YEAR)
        labels[grid.mask] = 10 if canopy_2021 > 0.5 else 30
        log("Generated synthetic land-cover labels.")
        return labels


def get_source(kind, planting_year):
    if kind.startswith("synthetic"):
        return SyntheticSource(kind.split(":", 1)[1] if ":" in kind else "honest", planting_year)
    return Sentinel2Source()
