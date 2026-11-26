# TerraTruth Oracle: an anti-greenwashing AI + Web3 verification layer

**Problem:** in today's carbon markets, data that goes in wrong comes out wrong. If a developer lies about planting
100,000 trees, a blockchain faithfully records the lie forever.

**TerraTruth** puts an AI auditor between the claim and the token. A developer can *register* a claim,
but **no carbon token can be minted until satellite evidence measures real biomass growth**. The smart
contract only accepts mint instructions from the oracle, and it never mints more than the evidence supports.

```
 Developer claim ──► VerifiedCarbonCredit.registerProject()   (on-chain, mints 0)
        │
        ▼
 Oracle job ──► Sentinel-2 L2A (free, Planetary Computer) ──► cloud mask + yearly median composites
        │      ESA WorldCover 2021 labels ──► per-site Random-Forest canopy model (held-out accuracy reported)
        │      canopy probability × NDVI ──► biomass ──► IPCC factors ──► tCO2e stock per year
        │      optional drone photos ──► Excess-Green + Otsu canopy ──► cross-check vs satellite
        ▼
 Verdict + integrity flags ──► keccak256(evidence report) ──► submitVerification()
        │
        ├─ PASSED  (verified ≥ 80% of claim) ──► mint min(verified, claimed) vtCO2e to developer
        └─ REJECTED ──► 0 minted, rejection permanently recorded on-chain
```

## Run it

```powershell
cd terratruth
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --only-binary=:all: -r requirements.txt
.\run.ps1            # then open http://127.0.0.1:8000
```

On first start, the app downloads the Solidity compiler (`solc 0.8.24`) once, compiles the contract into
`build/`, and deploys it to an in-process EVM chain. It needs no wallet, no API key and no test ETH.

Tests (synthetic honest vs fraudulent project, double-mint protection, oracle-only minting):

```powershell
.\.venv\Scripts\python.exe -m tests.test_pipeline
```

## Using the dashboard

1. Use **Jump to lat, lon** or pan the map, then draw the site with the polygon or rectangle tool (top left of the map).
2. Fill in the developer's claim: planting year, number of trees and tCO2e. Then click **Register claim on-chain**.
3. In the audit panel, pick the evidence source:
   * **Sentinel-2 satellite**: real imagery. It takes about 1–5 minutes, depending on site size and network.
   * **Synthetic demo honest / fraudulent**: instant offline demonstrations, clearly labelled as simulations.
4. You can optionally upload drone photos (before and now). They can only *lower* confidence or raise flags,
   never increase the credited amount, because the developer supplies them.
5. Review the verdict, the canopy and carbon chart, the yearly satellite and AI canopy images, the integrity flags,
   the model accuracy and the on-chain receipt. **Recompute evidence hash** proves that the downloadable
   evidence JSON matches what was anchored on-chain.
6. **Retire** credits from a wallet to burn them as an offset claim.

## Integrity checks (fraud flags)

| Code | Meaning |
|---|---|
| `CLAIM_NOT_SUPPORTED` | Measured sequestration is below 80% of the claim, so nothing is minted |
| `IMPOSSIBLE_DENSITY` | More trees claimed per hectare than physically plausible (above 5,000/ha), so the claim is rejected |
| `ADDITIONALITY` | The site was already forested at baseline (cutting old forest to "replant" earns nothing) |
| `TREE_COUNT` | The claimed tree count is more than 3× what the observed canopy could contain after 3 or more years |
| `CANOPY_LOSS` | Canopy dropped more than 10 points from its peak (clearing, fire or die-off) |
| `DRONE_MISMATCH` | The developer's drone footage disagrees with the satellite by more than 25 points |
| `INSUFFICIENT_DATA` | Fewer than two cloud-free epochs, so the verdict is INCONCLUSIVE and nothing is submitted |

## Deploying to a real network (Sepolia, Polygon Amoy, Anvil…)

```powershell
$env:CHAIN_RPC_URL = "https://<your-rpc>"
$env:ORACLE_PRIVATE_KEY = "0x..."       # funded oracle account; never commit this
# $env:CONTRACT_ADDRESS = "0x..."       # optional: reuse an existing deployment
.\run.ps1
```

In this mode, developers enter their own wallet address, and the oracle registers the claim on their behalf.
Retirements must be signed by the holder's wallet.

## Configuration

Every parameter in [app/config.py](app/config.py) can be overridden with an environment variable. Examples:
`TT_PASS_RATIO` (default 0.8), `TT_AGB_MAX_T_HA` (150), `TT_MAX_CLOUD` (30), `TT_SCENES_PER_YEAR` (3),
and `TT_MAX_SITE_KM2` (50).

## Honest limitations (read before using for real credits)

* **The biomass model is a proxy.** Canopy probability × NDVI → above-ground biomass uses IPCC default
  factors. NDVI saturates in dense forest. For production, calibrate `TT_AGB_MAX_T_HA` per biome with field plots,
  or replace the model with one trained on GEDI or LiDAR canopy height.
* **Sentinel-2 resolution is 10 m.** Seedlings in their first one or two years are mostly invisible, so young
  projects should expect low verified amounts until the canopy closes. This conservative behaviour is intentional.
* **The training labels (ESA WorldCover 2021) contain errors**, and some of those errors carry over into the model.
  Held-out accuracy is reported with every audit so that the error rate is visible.
* **The oracle is a single trusted signer.** Production systems should use several independent oracle nodes
  (for example, Chainlink Functions or an M-of-N multisig) and pin the evidence JSON to IPFS.
* **The in-process demo chain lives in memory.** On restart, it is rebuilt from stored registrations and
  verifications, but retirements are not replayed.

## Project layout

```
contracts/VerifiedCarbonCredit.sol   ERC-20 + oracle-gated minting, claims, rejections, retirement
app/satellite.py                     Sentinel-2 / WorldCover access (STAC + COG windows), synthetic source
app/vision.py                        Random-Forest canopy model, biomass model, drone Excess-Green analysis
app/oracle.py                        Verification pipeline, integrity flags, verdict
app/chain.py                         Compile / deploy / oracle transactions (web3.py)
app/main.py                          FastAPI server + background audit jobs
static/                              Leaflet + Chart.js dashboard
tests/test_pipeline.py               End-to-end test
```
