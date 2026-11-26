/* TerraTruth dashboard */
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const short = (h) => (h ? `${h.slice(0, 8)}…${h.slice(-6)}` : "");
const fmt = (n, d = 1) => (n == null ? "–" : Number(n).toLocaleString(undefined, { maximumFractionDigits: d, minimumFractionDigits: d }));

async function api(path, opts = {}) {
  const r = await fetch(path, opts);
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.detail || `HTTP ${r.status}`);
  return body;
}

// ---------------------------------------------------------------- map
const map = L.map("map", { zoomControl: true }).setView([-3.1, 37.35], 13);
const imagery = L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}", {
  maxZoom: 19, attribution: "Imagery © Esri, Maxar, Earthstar Geographics",
}).addTo(map);
const osm = L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom: 19, attribution: "© OpenStreetMap contributors" });
L.control.layers({ Satellite: imagery, Streets: osm }).addTo(map);

const drawn = new L.FeatureGroup().addTo(map);
const projectsLayer = new L.FeatureGroup().addTo(map);
map.addControl(new L.Control.Draw({
  edit: { featureGroup: drawn, remove: true },
  draw: {
    polygon: { allowIntersection: false, showArea: false, shapeOptions: { color: "#ffd400" } },
    rectangle: { showArea: false, shapeOptions: { color: "#ffd400" } },
    polyline: false, circle: false, marker: false, circlemarker: false,
  },
}));
let sitePolygon = null;
function updateSiteInfo() {
  const layers = drawn.getLayers();
  sitePolygon = layers.length ? layers[layers.length - 1] : null;
  if (!sitePolygon) { $("#siteInfo").textContent = "No site drawn yet."; return; }
  const ha = L.GeometryUtil.geodesicArea(sitePolygon.getLatLngs()[0]) / 10000;
  $("#siteInfo").innerHTML = `Site drawn: <b>${fmt(ha, 2)} ha</b> (≈${Math.round(ha * 100).toLocaleString()} Sentinel-2 pixels)`;
}
map.on(L.Draw.Event.CREATED, (e) => { drawn.clearLayers(); drawn.addLayer(e.layer); updateSiteInfo(); });
map.on(L.Draw.Event.EDITED, updateSiteInfo);
map.on(L.Draw.Event.DELETED, updateSiteInfo);

$("#gotoBtn").onclick = () => {
  const m = $("#gotoInput").value.split(",").map((s) => parseFloat(s.trim()));
  if (m.length === 2 && m.every((x) => Number.isFinite(x))) map.setView(m, 15);
};

// ---------------------------------------------------------------- chain
let chain = null;
async function refreshChain() {
  try {
    chain = await api("/api/chain");
    $("#chainPill").textContent = `${chain.mode} chain #${chain.chain_id} · contract ${short(chain.contract)} · block ${chain.block} · supply ${fmt(chain.total_supply_tco2e, 3)} ${chain.symbol} · retired ${fmt(chain.total_retired_tco2e, 3)}`;
    const sel = $("#devSelect");
    const prev = sel.value;
    sel.innerHTML = chain.wallets.map((w, i) => `<option value="${w.address}">Developer ${i + 1} · ${short(w.address)}</option>`).join("") + `<option value="custom">Custom address…</option>`;
    if (prev) sel.value = prev;
    $("#wallets").innerHTML = chain.wallets.length ? chain.wallets.map((w, i) => `
      <div class="wallet"><span>Dev ${i + 1} <span class="mono">${short(w.address)}</span></span>
      <span><b>${fmt(w.balance_tco2e, 3)}</b> ${w.balance_tco2e > 0 ? `<button data-retire="${w.address}">Retire</button>` : ""}</span></div>`).join("")
      : `<p class="hint">External network: balances are on your block explorer.</p>`;
  } catch (e) { $("#chainPill").textContent = "chain error: " + e.message; }
}
$("#devSelect").onchange = (e) => $("#devCustom").classList.toggle("hidden", e.target.value !== "custom");
$("#wallets").onclick = async (e) => {
  const addr = e.target.dataset.retire; if (!addr) return;
  const t = parseFloat(prompt("Tonnes of vtCO2e to retire (burn) as an offset claim:", "1"));
  if (!(t > 0)) return;
  const who = prompt("Beneficiary (who is claiming this offset?)", "Example Corp FY2026") || "unspecified";
  try { await api("/api/retire", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ holder: addr, tonnes: t, beneficiary: who }) }); }
  catch (err) { alert(err.message); }
  refreshAll();
};

async function refreshLedger() {
  const rows = await api("/api/ledger").catch(() => []);
  $("#ledger tbody").innerHTML = rows.length ? rows.map((r) => {
    const a = r.args; let d = "";
    if (r.event === "ProjectRegistered") d = `Claim: ${Number(a.claimedTrees).toLocaleString()} trees, ${fmt(a.claimedKgCO2e, 3)} tCO2e by ${short(a.developer)}`;
    if (r.event === "VerificationSubmitted") d = `<b class="${a.passed ? "" : "error-text"}">${a.passed ? "PASSED" : "REJECTED"}</b> · verified ${fmt(a.verifiedKgCO2e, 3)} / claimed ${fmt(a.claimedKgCO2e, 3)} t · <b>minted ${fmt(a.mintedKgCO2e, 3)}</b> · conf ${(a.confidenceBps / 100).toFixed(1)}% · evidence <span class="mono">${short(a.evidenceHash)}</span>`;
    if (r.event === "CreditsRetired") d = `${fmt(a.amountKg, 3)} t retired by ${short(a.holder)} for “${esc(a.beneficiary)}”`;
    return `<tr><td>${r.block}</td><td>${r.event}</td><td>${d}</td><td class="mono">${short(r.tx)}</td></tr>`;
  }).join("") : `<tr><td colspan="4" class="hint">No events yet.</td></tr>`;
}

// ---------------------------------------------------------------- projects
let projects = [];
let current = null;
const statusColor = { VERIFIED: "#2ecc71", REJECTED: "#ff4d4d", INCONCLUSIVE: "#f0b350", PENDING: "#5aa0ff" };

async function refreshProjects() {
  projects = await api("/api/projects");
  projectsLayer.clearLayers();
  $("#projectList").innerHTML = projects.length ? projects.map((p) => `
    <button class="project-item ${current === p.id ? "active" : ""}" data-id="${p.id}">
      <span>${esc(p.name)}<br><small class="hint">${p.planting_year} · ${fmt(p.claimed_tco2e, 0)} tCO2e claimed</small></span>
      <span class="badge ${p.status}">${p.status}</span></button>`).join("") : `<p class="hint">No projects yet.</p>`;
  for (const p of projects) {
    const lyr = L.geoJSON(p.geojson, { style: { color: statusColor[p.status] || "#5aa0ff", weight: 2, fillOpacity: 0.12 } });
    lyr.on("click", () => openProject(p.id));
    lyr.bindTooltip(`${esc(p.name)} · ${p.status}`);
    projectsLayer.addLayer(lyr);
  }
}
$("#projectList").onclick = (e) => { const b = e.target.closest("[data-id]"); if (b) openProject(b.dataset.id, true); };

$("#projectForm").onsubmit = async (e) => {
  e.preventDefault();
  if (!sitePolygon) { alert("Draw the project site on the map first (polygon or rectangle tool)."); return; }
  const f = new FormData(e.target);
  const dev = f.get("developer") === "custom" ? f.get("developerCustom").trim() : f.get("developer");
  const btn = e.target.querySelector("button"); btn.disabled = true;
  try {
    const p = await api("/api/projects", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: f.get("name"), developer: dev, geojson: sitePolygon.toGeoJSON(),
        planting_year: +f.get("planting_year"), claimed_trees: +f.get("claimed_trees"), claimed_tco2e: +f.get("claimed_tco2e"),
      }),
    });
    drawn.clearLayers(); updateSiteInfo(); e.target.reset();
    await refreshAll(); openProject(p.id, true);
  } catch (err) { alert(err.message); }
  btn.disabled = false;
};

async function openProject(id, zoom) {
  current = id;
  const p = await api(`/api/projects/${id}`);
  $("#auditPanel").classList.remove("hidden");
  $("#auditTitle").textContent = p.name;
  const oc = p.onchain;
  $("#auditMeta").innerHTML = `Developer <span class="mono">${short(p.developer)}</span> · planted ${p.planting_year} · claims ${Number(p.claimed_trees).toLocaleString()} trees & ${fmt(p.claimed_tco2e, 1)} tCO2e`
    + (oc ? ` · on-chain status <b>${oc.status}</b>, minted ${fmt(oc.minted_tco2e, 3)} t` : "")
    + (p.register_tx ? ` · reg tx <span class="mono">${short(p.register_tx)}</span>` : "");
  $("#statusBadge").className = `badge ${p.status}`; $("#statusBadge").textContent = p.status;
  if (zoom) map.fitBounds(L.geoJSON(p.geojson).getBounds(), { maxZoom: 16, padding: [40, 40] });
  document.querySelectorAll(".project-item").forEach((b) => b.classList.toggle("active", b.dataset.id === id));
  $("#progressWrap").classList.add("hidden");
  renderResult(p.report, p.chain_result, p.job_id);
  if (p.latest_job && p.latest_job.status === "running") pollJob(p.latest_job.id);
}

$("#verifyForm").onsubmit = async (e) => {
  e.preventDefault();
  if (!current) return;
  const fd = new FormData(e.target);
  for (const k of ["season_start", "season_end"]) if (!fd.get(k)) fd.delete(k);
  for (const k of ["drone_before", "drone_after"]) if (!fd.get(k) || !fd.get(k).size) fd.delete(k);
  try {
    const { job_id } = await api(`/api/projects/${current}/verify`, { method: "POST", body: fd });
    pollJob(job_id);
  } catch (err) { alert(err.message); }
};

let pollTimer = null;
async function pollJob(jid) {
  clearTimeout(pollTimer);
  $("#runBtn").disabled = true;
  $("#progressWrap").classList.remove("hidden");
  $("#result").innerHTML = "";
  const tick = async () => {
    const j = await api(`/api/jobs/${jid}`).catch(() => null);
    if (!j) { pollTimer = setTimeout(tick, 2000); return; }
    $("#progressBar").style.width = `${j.progress || 0}%`;
    $("#logBox").textContent = j.log || "Starting…"; $("#logBox").scrollTop = 1e9;
    if (j.status === "running") { pollTimer = setTimeout(tick, 1500); return; }
    $("#runBtn").disabled = false;
    if (j.status === "error") { $("#result").innerHTML = `<p class="error-text"><b>Audit failed:</b> ${esc(j.error)}</p>`; return; }
    await refreshAll();
    if (current === j.project_id) {
      const p = projects.find((x) => x.id === current);
      if (p) { $("#statusBadge").className = `badge ${p.status}`; $("#statusBadge").textContent = p.status; }
      renderResult(j.report, j.chain, j.id);
    }
  };
  tick();
}

// ---------------------------------------------------------------- results
let chartObj = null;
function renderResult(r, ch, jobId) {
  const box = $("#result");
  if (chartObj) { chartObj.destroy(); chartObj = null; }
  if (!r) { box.innerHTML = `<p class="hint" style="margin-top:12px">No audit yet. Choose an evidence source and run the AI audit.</p>`; return; }
  const m = r.measured || {};
  const ep = r.epochs || [];
  const flags = (r.flags || []).map((f) => `<li class="${f.level}"><b>${f.code}</b> · ${esc(f.msg)}</li>`).join("");
  const model = r.model || {};
  box.innerHTML = `
    <div class="verdict ${r.verdict}">
      <span class="big">${r.verdict}</span>
      <span>${r.verdict === "VERIFIED" ? `Smart contract minted <b>${fmt(ch?.minted_tco2e, 3)} vtCO2e</b> to the developer.` :
        r.verdict === "REJECTED" ? "Claim not supported by satellite evidence: <b>0 tokens minted</b>. The rejection is recorded on-chain." :
        "Not enough cloud-free imagery: nothing submitted on-chain."}</span>
      ${r.synthetic ? `<span class="badge INCONCLUSIVE">SYNTHETIC DATA</span>` : ""}
    </div>
    <div class="stats">
      <div class="stat"><b>${fmt(r.claim.tco2e, 1)}</b><span>tCO2e claimed</span></div>
      <div class="stat"><b>${fmt(r.verified_tco2e, 1)}</b><span>tCO2e verified (${r.baseline_year ?? "?"}→${r.latest_year ?? "?"})</span></div>
      <div class="stat"><b>${r.ratio == null ? "–" : Math.round(r.ratio * 100) + "%"}</b><span>evidence / claim</span></div>
      <div class="stat"><b>${m.canopy_gain_pts == null ? "–" : (m.canopy_gain_pts >= 0 ? "+" : "") + fmt(m.canopy_gain_pts, 1)} pts</b><span>canopy cover change</span></div>
      <div class="stat"><b>${Math.round((r.confidence || 0) * 100)}%</b><span>oracle confidence</span></div>
      <div class="stat"><b>${fmt(r.site.area_ha, 2)} ha</b><span>site area</span></div>
    </div>
    ${flags ? `<h3>Integrity checks</h3><ul class="flags">${flags}</ul>` : ""}
    <h3>Canopy &amp; carbon stock over time (inside the claimed polygon)</h3>
    <div class="chart-box"><canvas id="chart"></canvas></div>
    <h3>Satellite evidence per year <small class="hint">true colour · AI canopy probability (green = tree)</small></h3>
    <div class="epochs">${ep.map((e) => `
      <div class="epoch"><b>${e.year}</b> · ${e.metrics.canopy_pct ?? "–"}% canopy · ${Math.round((e.metrics.coverage || 0) * 100)}% clear
        <div class="imgs"><img src="${e.images.rgb}" alt="True colour ${e.year}"><img src="${e.images.canopy}" alt="Canopy map ${e.year}"></div>
        <div class="hint">${e.dates.join(", ")}</div></div>`).join("")}</div>
    ${r.drone && Object.keys(r.drone).length ? `<h3>Drone corroboration</h3><div class="epochs">
      ${r.drone.before_mask ? `<div class="epoch">Before: ${r.drone.before_canopy_pct}% canopy<div class="imgs"><img src="${r.drone.before_mask}" alt="Drone before mask"></div></div>` : ""}
      ${r.drone.after_mask ? `<div class="epoch">Now: ${r.drone.after_canopy_pct}% canopy (satellite Δ ${r.drone.satellite_vs_drone_diff_pts ?? "–"} pts)<div class="imgs"><img src="${r.drone.after_mask}" alt="Drone now mask"></div></div>` : ""}</div>` : ""}
    <h3>AI model</h3>
    <dl class="kv">
      <dt>Model</dt><dd>${esc(model.model)}</dd>
      ${model.accuracy != null ? `<dt>Held-out accuracy</dt><dd>${(model.accuracy * 100).toFixed(1)}% · F1 ${model.f1} · κ ${model.kappa} (${model.test_samples} px)</dd>
      <dt>Top features</dt><dd>${(model.top_features || []).map((f) => `${f[0]} ${f[1]}`).join(" · ")}</dd>` : ""}
      <dt>Training pixels</dt><dd>${model.train_tree_px ?? "–"} tree / ${model.train_other_px ?? "–"} other</dd>
      <dt>Data source</dt><dd>${esc(r.data_source)} · season months ${r.site.season_months.join("–")} · ${esc(r.site.crs)}</dd>
      <dt>Methodology</dt><dd class="hint">${esc(r.methodology)}</dd>
    </dl>
    ${ch ? `<h3>Oracle → smart contract receipt</h3>
    <dl class="kv">
      <dt>Transaction</dt><dd class="mono">${ch.tx} (block ${ch.block})</dd>
      <dt>Evidence hash</dt><dd class="mono">${ch.evidence_hash}</dd>
      <dt>Minted</dt><dd>${fmt(ch.minted_tco2e, 3)} vtCO2e</dd>
    </dl>
    <p><a href="/api/jobs/${jobId}/evidence.json" download>Download full evidence report (JSON)</a> ·
      <button type="button" id="auditHashBtn">Independently recompute evidence hash</button> <span id="auditHashOut" class="mono"></span></p>` : ""}
  `;
  const ab = $("#auditHashBtn");
  if (ab) ab.onclick = async () => {
    const h = await api(`/api/jobs/${jobId}/audit-hash`);
    $("#auditHashOut").textContent = h.found_on_chain ? `✔ ${short(h.recomputed_hash)} matches on-chain record` : `✘ ${short(h.recomputed_hash)} NOT found on-chain`;
  };
  const good = ep.filter((e) => e.metrics.canopy_pct != null);
  if (good.length) {
    const css = getComputedStyle(document.documentElement);
    const ink = css.getPropertyValue("--muted").trim(), grid = css.getPropertyValue("--line").trim();
    chartObj = new Chart($("#chart"), {
      data: {
        labels: good.map((e) => e.year),
        datasets: [
          { type: "line", label: "Canopy cover %", data: good.map((e) => e.metrics.canopy_pct), yAxisID: "y", borderColor: "#2e9e62", backgroundColor: "#2e9e62", tension: 0.25 },
          { type: "bar", label: "Carbon stock (tCO2e)", data: good.map((e) => e.metrics.carbon_stock_tco2e), yAxisID: "y2", backgroundColor: "rgba(120,140,200,.45)" },
          { type: "line", label: "Baseline + claim (tCO2e)", data: good.map((e) => e.year >= (r.latest_year || 0) ? (good[0].metrics.carbon_stock_tco2e + r.claim.tco2e) : null), yAxisID: "y2", borderColor: "#e0533d", backgroundColor: "#e0533d", pointStyle: "crossRot", pointRadius: 9, showLine: false },
        ],
      },
      options: {
        maintainAspectRatio: false, interaction: { mode: "index", intersect: false },
        plugins: { legend: { labels: { color: ink } } },
        scales: {
          x: { ticks: { color: ink }, grid: { color: grid } },
          y: { position: "left", min: 0, max: 100, title: { display: true, text: "canopy %", color: ink }, ticks: { color: ink }, grid: { color: grid } },
          y2: { position: "right", min: 0, title: { display: true, text: "tCO2e", color: ink }, ticks: { color: ink }, grid: { drawOnChartArea: false } },
        },
      },
    });
  }
}

// ---------------------------------------------------------------- installable app (PWA)
if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
let installPrompt = null;
window.addEventListener("beforeinstallprompt", (e) => {
  e.preventDefault(); installPrompt = e; $("#installBtn").classList.remove("hidden");
});
$("#installBtn").onclick = async () => {
  if (!installPrompt) return;
  installPrompt.prompt(); await installPrompt.userChoice; installPrompt = null;
  $("#installBtn").classList.add("hidden");
};
window.addEventListener("appinstalled", () => $("#installBtn").classList.add("hidden"));

async function refreshAll() { await Promise.all([refreshChain(), refreshProjects(), refreshLedger()]); }
refreshAll();
setInterval(refreshChain, 15000);
