"""Oracle decision engine: satellite evidence -> AI measurements -> verdict.

The developer's claim is NEVER an input to the measurement, only to the final
comparison. Drone footage is supplied by the developer, so it can corroborate or
contradict, but it can never on its own raise the verified amount.
"""
import datetime as dt

from . import config, render
from .satellite import DataError, default_season, get_source, make_grid
from .vision import CanopyModel, drone_canopy, site_metrics

METHODOLOGY = (
    "Sentinel-2 L2A cloud-masked (SCL) median composites per growing season; per-site Random-Forest "
    "canopy classifier trained on ESA WorldCover 2021 tree-cover labels from the surrounding landscape; "
    "AGB = canopy probability x NDVI-scaled biomass (max {agb} t/ha); total biomass with root:shoot {rs}; "
    "carbon fraction {cf}; CO2e = C x 44/12. Verified credit = stock(latest) - stock(baseline). "
    "Biomass coefficients are IPCC-default placeholders and must be calibrated with field plots or "
    "GEDI/LiDAR for production use."
).format(agb=config.AGB_MAX_T_HA, rs=config.ROOT_SHOOT_RATIO, cf=config.CARBON_FRACTION)


def _years(planting_year, season):
    today = dt.date.today()
    latest = today.year if today.month > season[1] else today.year - 1
    baseline = max(int(planting_year), 2017)  # Sentinel-2 L2A is reliably global from 2017
    latest = max(latest, baseline + 1)
    years = list(range(baseline, latest + 1))
    if len(years) > 9:  # keep runtime bounded: baseline, latest and evenly spaced in between
        step = (len(years) - 1) / 8
        years = sorted({years[round(i * step)] for i in range(9)})
    return years


def run_verification(project, source_kind="sentinel-2", season=None, drone_before=None, drone_after=None,
                     log=print, progress=lambda p: None):
    flags = []

    def flag(level, code, msg):
        flags.append({"level": level, "code": code, "msg": msg})
        log(f"[{level}] {msg}")

    grid = make_grid(project["geojson"])
    season = tuple(season) if season else default_season(grid.centroid[1])
    source = get_source(source_kind, project["planting_year"])
    if source.synthetic:
        flag("info", "SYNTHETIC", "Synthetic demo imagery: this verdict is a simulation, not real evidence.")
    log(f"Site {grid.site_ha:.2f} ha; analysis window {grid.width}x{grid.height} px in {grid.crs}; "
        f"season months {season[0]}-{season[1]}.")
    if int(project["planting_year"]) < 2017:
        flag("warn", "PRE_SENTINEL", "Planting predates reliable Sentinel-2 coverage; growth is measured from 2017.")
    progress(5)

    # 1) Train the canopy model on the landscape around the site
    labels = source.landcover_labels(grid, log=log)
    progress(12)
    comps = {}
    years = _years(project["planting_year"], season)
    train_year = config.LANDCOVER_YEAR
    try:
        comps[train_year] = source.composite(grid, train_year, season, log=log)
    except DataError as e:
        raise DataError(f"Could not build the {train_year} training composite: {e}")
    model = CanopyModel().fit(comps[train_year], labels, log=log)
    progress(25)

    # 2) Measure every epoch
    epochs = []
    for i, y in enumerate(years):
        try:
            comp = comps.get(y) or source.composite(grid, y, season, log=log)
        except DataError as e:
            log(f"{y}: skipped ({e})")
            continue
        except Exception as e:  # network hiccup on one epoch should not kill the audit
            log(f"{y}: skipped (read error: {e})")
            continue
        prob, ndvi = model.predict(comp)
        m = site_metrics(prob, ndvi, grid.mask)
        epochs.append({
            "year": y, "dates": comp["dates"], "scene_ids": comp["scene_ids"], "metrics": m,
            "images": {"rgb": render.true_color(comp, grid.mask), "canopy": render.canopy_map(prob, grid.mask)},
        })
        if m.get("coverage", 0) < config.MIN_COVERAGE:
            log(f"{y}: only {m.get('coverage', 0):.0%} of site cloud-free; epoch unreliable.")
        else:
            log(f"{y}: canopy {m['canopy_pct']:.1f}% | NDVI {m['mean_ndvi']:.2f} | "
                f"stock {m['carbon_stock_tco2e']:.1f} tCO2e")
        progress(25 + int(60 * (i + 1) / len(years)))

    usable = [e for e in epochs if e["metrics"].get("coverage", 0) >= config.MIN_COVERAGE]
    claim_t = float(project["claimed_tco2e"])
    report = {
        "project_id": project["id"], "project_name": project["name"],
        "generated_at": dt.datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "data_source": source.name, "synthetic": source.synthetic,
        "site": {"area_ha": round(grid.site_ha, 3), "centroid": [round(c, 6) for c in grid.centroid],
                 "crs": grid.crs, "season_months": list(season)},
        "model": model.metrics, "epochs": epochs,
        "claim": {"trees": int(project["claimed_trees"]), "tco2e": claim_t,
                  "planting_year": int(project["planting_year"])},
        "methodology": METHODOLOGY, "flags": flags,
    }

    if len(usable) < 2:
        flag("error", "INSUFFICIENT_DATA", "Fewer than two cloud-free epochs; cannot measure growth.")
        report.update(verdict="INCONCLUSIVE", ratio=None, confidence=0.0, verified_tco2e=0.0, credits_tco2e=0.0)
        progress(100)
        return report

    base, last = usable[0], usable[-1]
    if base["year"] > max(int(project["planting_year"]), 2017) + 1:
        flag("warn", "LATE_BASELINE", f"No clean imagery near planting; baseline moved to {base['year']}.")
    bm, lm = base["metrics"], last["metrics"]
    verified = max(0.0, lm["carbon_stock_tco2e"] - bm["carbon_stock_tco2e"])
    gain_pts = lm["canopy_pct"] - bm["canopy_pct"]
    ratio = verified / claim_t if claim_t > 0 else 0.0

    # 3) Fraud / integrity checks
    density = project["claimed_trees"] / grid.site_ha if grid.site_ha else 0
    if density > config.MAX_DENSITY_TREES_HA:
        flag("error", "IMPOSSIBLE_DENSITY",
             f"Claimed {density:,.0f} trees/ha exceeds the plausible maximum of {config.MAX_DENSITY_TREES_HA:,.0f}.")
    if bm["canopy_pct"] >= config.BASELINE_FOREST_FLAG:
        flag("warn", "ADDITIONALITY",
             f"Site already had {bm['canopy_pct']:.0f}% canopy at baseline ({base['year']}): existing forest "
             "is not new sequestration.")
    implied_trees = lm["canopy_ha"] * 10_000 / config.CROWN_AREA_M2
    age = last["year"] - int(project["planting_year"])
    if age >= 3 and project["claimed_trees"] > 3 * max(implied_trees, 1):
        flag("warn", "TREE_COUNT", f"Claim of {project['claimed_trees']:,} trees is >3x what the observed canopy "
             f"can hold (~{implied_trees:,.0f} trees of {config.CROWN_AREA_M2:.0f} m² crown) after {age} years.")
    peak = max(usable, key=lambda e: e["metrics"]["canopy_pct"])
    if peak is not last and peak["metrics"]["canopy_pct"] - lm["canopy_pct"] > 10:
        flag("warn", "CANOPY_LOSS", f"Canopy fell from {peak['metrics']['canopy_pct']:.0f}% ({peak['year']}) to "
             f"{lm['canopy_pct']:.0f}% ({last['year']}): possible clearing, fire or die-off.")
    if ratio > 3:
        flag("info", "UNDERCLAIMED", "Measured growth far exceeds the claim; credits are capped at the claim.")

    # 4) Drone corroboration (developer-supplied => can only lower confidence)
    drone = {}
    agreement = None
    if drone_after:
        d = drone_canopy(drone_after)
        drone["after_canopy_pct"] = d["canopy_pct"]
        drone["after_mask"] = render.mask_png(d["mask"])
        diff = abs(d["canopy_pct"] - lm["canopy_pct"])
        agreement = max(0.0, 1 - diff / 100)
        drone["satellite_vs_drone_diff_pts"] = round(diff, 1)
        if diff > 25:
            flag("warn", "DRONE_MISMATCH", f"Drone shows {d['canopy_pct']:.0f}% canopy but satellite shows "
                 f"{lm['canopy_pct']:.0f}%: footage may be from a different site or cherry-picked.")
    if drone_before:
        d = drone_canopy(drone_before)
        drone["before_canopy_pct"] = d["canopy_pct"]
        drone["before_mask"] = render.mask_png(d["mask"])
        if "after_canopy_pct" in drone:
            drone["drone_gain_pts"] = round(drone["after_canopy_pct"] - d["canopy_pct"], 1)

    # 5) Verdict
    skill = model.metrics.get("f1", 0.6)
    cov = min(bm["coverage"], lm["coverage"])
    confidence = 0.55 * skill + 0.30 * cov + 0.15 * (agreement if agreement is not None else 0.7)
    hard_fail = any(f["level"] == "error" for f in flags)
    passed = ratio >= config.PASS_RATIO and not hard_fail
    if not passed and not hard_fail:
        flag("error", "CLAIM_NOT_SUPPORTED", f"Satellite-verified sequestration ({verified:,.1f} tCO2e) is "
             f"{ratio:.0%} of the claim ({claim_t:,.1f} tCO2e); {config.PASS_RATIO:.0%} required.")
    credits = min(verified, claim_t) if passed else 0.0
    log(f"VERDICT: {'VERIFIED' if passed else 'REJECTED'} | verified {verified:,.1f} / claimed {claim_t:,.1f} "
        f"tCO2e ({ratio:.0%}) | confidence {confidence:.0%}")

    report.update({
        "baseline_year": base["year"], "latest_year": last["year"],
        "measured": {"verified_tco2e": round(verified, 3), "canopy_gain_pts": round(gain_pts, 2),
                     "canopy_implied_trees": int(implied_trees), "claimed_density_trees_ha": round(density, 1)},
        "drone": drone, "verdict": "VERIFIED" if passed else "REJECTED", "ratio": round(ratio, 4),
        "confidence": round(confidence, 4), "verified_tco2e": round(verified, 3), "credits_tco2e": round(credits, 3),
    })
    progress(95)
    return report
