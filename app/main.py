"""TerraTruth: anti-greenwashing AI + Web3 oracle. FastAPI server."""
import json
import logging
import mimetypes
import threading
import traceback
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from web3 import Web3

from . import config, oracle, store
from .chain import Chain, evidence_hash, hx
from .satellite import DataError, make_grid

log = logging.getLogger("terratruth")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

mimetypes.add_type("application/manifest+json", ".webmanifest")
mimetypes.add_type("text/javascript", ".js")  # Windows registry sometimes maps .js to text/plain

app = FastAPI(title="TerraTruth Oracle", version="1.0")
STATIC = config.ROOT / "static"
REPORTS = config.DATA_DIR / "reports"
REPORTS.mkdir(exist_ok=True)
CHAIN: Chain = None


@app.on_event("startup")
def startup():
    global CHAIN
    store.init()
    CHAIN = Chain()
    log.info("Contract %s deployed/attached (%s chain, id %s)", CHAIN.address, CHAIN.mode, CHAIN.w3.eth.chain_id)
    if CHAIN.mode == "in-process":
        _replay()


def _replay():
    """The in-process chain lives in memory: rebuild it from the stored, hash-anchored evidence."""
    projects = sorted(store.list_projects(), key=lambda p: p["created_at"])
    for p in projects:
        try:
            store.update_project(p["id"], register_tx=CHAIN.register(p))
        except Exception as e:
            log.warning("replay register %s failed: %s", p["id"], e)
    for j in store.finished_jobs_in_order():
        try:
            res = CHAIN.submit(j["project_id"], j["report"])
            store.update_job(j["id"], chain=res)
        except Exception as e:
            log.warning("replay verification %s failed: %s", j["id"], e)
    if projects:
        log.info("Replayed %d project(s) onto the fresh in-process chain.", len(projects))


# ------------------------------------------------------------------ API ----
class ProjectIn(BaseModel):
    name: str
    developer: str
    geojson: dict
    planting_year: int
    claimed_trees: int
    claimed_tco2e: float


def _public(p):
    j = store.latest_job(p["id"])
    out = dict(p)
    try:
        out["onchain"] = CHAIN.onchain_project(p["id"])
    except Exception:
        out["onchain"] = None
    out["latest_job"] = {k: j[k] for k in ("id", "status", "progress")} if j else None
    return out


@app.get("/api/health")
def health():
    return {"ok": CHAIN is not None, "chain": CHAIN.mode if CHAIN else None}


@app.get("/api/chain")
def chain_info():
    return CHAIN.info()


@app.get("/api/ledger")
def ledger():
    return CHAIN.ledger()


@app.get("/api/projects")
def projects():
    return [_public(p) for p in store.list_projects()]


@app.post("/api/projects")
def create_project(body: ProjectIn):
    data = body.dict()
    if not Web3.is_address(data["developer"]):
        raise HTTPException(400, "Developer must be a valid 0x wallet address.")
    if data["claimed_tco2e"] <= 0 or data["claimed_trees"] <= 0:
        raise HTTPException(400, "Claims must be positive.")
    if not (1990 <= data["planting_year"] <= 2100):
        raise HTTPException(400, "Unrealistic planting year.")
    try:
        grid = make_grid(data["geojson"], buffer_m=0)
    except (DataError, KeyError, ValueError, TypeError) as e:
        raise HTTPException(400, f"Invalid site geometry: {e}")
    p = store.create_project(data)
    try:
        tx = CHAIN.register(p)
    except Exception as e:
        store.delete_project(p["id"])
        raise HTTPException(500, f"On-chain registration failed: {e}")
    store.update_project(p["id"], register_tx=tx)
    out = _public(store.get_project(p["id"]))
    out["site_ha"] = round(grid.site_ha, 2)
    return out


@app.get("/api/projects/{pid}")
def get_project(pid: str):
    p = store.get_project(pid)
    if not p:
        raise HTTPException(404, "project not found")
    out = _public(p)
    j = store.latest_job(pid, finished_only=True)
    out["report"] = j["report"] if j else None
    out["chain_result"] = j["chain"] if j else None
    out["job_id"] = j["id"] if j else None
    return out


def _run(jid, project, params, before, after):
    def logline(s):
        log.info("[%s] %s", jid, s)
        store.append_log(jid, s)
    try:
        report = oracle.run_verification(project, params["source"], params.get("season"), before, after,
                                         log=logline, progress=lambda p: store.update_job(jid, progress=p))
        report = json.loads(json.dumps(report, default=str))  # canonical form: identical after DB round-trip
        res = None
        if report["verdict"] in ("VERIFIED", "REJECTED"):
            logline("Submitting AI verdict to the smart contract...")
            res = CHAIN.submit(project["id"], report)
            logline(f"On-chain tx {res['tx']} (block {res['block']}): minted {res['minted_tco2e']:,.3f} vtCO2e.")
        (REPORTS / f"{jid}.json").write_text(json.dumps(report, sort_keys=True, separators=(",", ":"), default=str))
        store.update_job(jid, status="done", progress=100, report=report, chain=res)
        store.update_project(project["id"], status=report["verdict"])
    except Exception as e:
        logline(f"ERROR: {e}")
        log.error(traceback.format_exc())
        store.update_job(jid, status="error", error=str(e))


@app.post("/api/projects/{pid}/verify")
async def verify(pid: str, source: str = Form("sentinel-2"), season_start: Optional[int] = Form(None),
                 season_end: Optional[int] = Form(None), drone_before: Optional[UploadFile] = File(None),
                 drone_after: Optional[UploadFile] = File(None)):
    p = store.get_project(pid)
    if not p:
        raise HTTPException(404, "project not found")
    if source not in ("sentinel-2", "synthetic:honest", "synthetic:fraud"):
        raise HTTPException(400, "unknown data source")
    running = store.latest_job(pid)
    if running and running["status"] == "running":
        raise HTTPException(409, "an audit is already running for this project")
    season = None
    if season_start and season_end:
        if not (1 <= season_start <= season_end <= 12):
            raise HTTPException(400, "season months must satisfy 1 <= start <= end <= 12")
        season = [season_start, season_end]
    before = await drone_before.read() if drone_before and drone_before.filename else None
    after = await drone_after.read() if drone_after and drone_after.filename else None
    params = {"source": source, "season": season, "drone": bool(before or after)}
    jid = store.create_job(pid, params)
    threading.Thread(target=_run, args=(jid, p, params, before, after), daemon=True).start()
    return {"job_id": jid}


@app.get("/api/jobs/{jid}")
def job(jid: str):
    j = store.get_job(jid)
    if not j:
        raise HTTPException(404, "job not found")
    return j


@app.get("/api/jobs/{jid}/evidence.json")
def evidence(jid: str):
    f = REPORTS / f"{jid}.json"
    if not f.exists():
        raise HTTPException(404, "no evidence for this job")
    return FileResponse(f, media_type="application/json", filename=f"terratruth-evidence-{jid}.json")


@app.get("/api/jobs/{jid}/audit-hash")
def audit_hash(jid: str):
    """Anyone can recompute the evidence hash and compare it with the on-chain record."""
    j = store.get_job(jid)
    if not j or not j.get("report"):
        raise HTTPException(404, "no evidence for this job")
    recomputed = hx(evidence_hash(j["report"]))
    onchain = None
    for e in CHAIN.ledger(limit=1000):
        if e["event"] == "VerificationSubmitted" and e["args"].get("evidenceHash") == recomputed:
            onchain = e
    return {"recomputed_hash": recomputed, "found_on_chain": onchain is not None, "onchain_event": onchain}


class RetireIn(BaseModel):
    holder: str
    tonnes: float
    beneficiary: str


@app.post("/api/retire")
def retire(body: RetireIn):
    if body.tonnes <= 0:
        raise HTTPException(400, "amount must be positive")
    try:
        return {"tx": CHAIN.retire(body.holder, body.tonnes, body.beneficiary)}
    except Exception as e:
        raise HTTPException(400, str(e))


@app.exception_handler(DataError)
def data_error(_, exc):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
