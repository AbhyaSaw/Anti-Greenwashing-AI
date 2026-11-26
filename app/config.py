"""Central configuration. Every value can be overridden with an environment variable."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("TT_DATA_DIR", ROOT / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "terratruth.db"
BUILD_DIR = ROOT / "build"
CONTRACT_SRC = ROOT / "contracts" / "VerifiedCarbonCredit.sol"


def _f(name, default):
    return float(os.getenv(name, default))


# --- Satellite --------------------------------------------------------------
STAC_URL = os.getenv("TT_STAC_URL", "https://planetarycomputer.microsoft.com/api/stac/v1")
S2_COLLECTION = "sentinel-2-l2a"
LANDCOVER_COLLECTION = "esa-worldcover"   # ESA WorldCover 10 m: training labels
LANDCOVER_YEAR = 2021
MAX_CLOUD_PCT = _f("TT_MAX_CLOUD", 30)
SCENES_PER_YEAR = int(os.getenv("TT_SCENES_PER_YEAR", 3))
CONTEXT_BUFFER_M = _f("TT_CONTEXT_BUFFER_M", 1500)  # surrounding area used to train the model
MAX_SITE_KM2 = _f("TT_MAX_SITE_KM2", 50)
MAX_GRID_PX = int(os.getenv("TT_MAX_GRID_PX", 1600))

# --- Biomass / carbon model (IPCC defaults; calibrate per biome with field plots) ---
AGB_MAX_T_HA = _f("TT_AGB_MAX_T_HA", 150)        # above-ground biomass of closed mature canopy
ROOT_SHOOT_RATIO = _f("TT_ROOT_SHOOT", 0.24)      # IPCC 2006 Vol.4 Table 4.4 (typical)
CARBON_FRACTION = _f("TT_CARBON_FRACTION", 0.47)  # IPCC default carbon fraction of dry matter
CO2_PER_C = 44.0 / 12.0
CROWN_AREA_M2 = _f("TT_CROWN_AREA_M2", 12)        # rough crown of a 3-8 yr old planted tree

# --- Oracle decision policy -------------------------------------------------
PASS_RATIO = _f("TT_PASS_RATIO", 0.80)       # verified/claimed needed to mint
MIN_COVERAGE = _f("TT_MIN_COVERAGE", 0.50)   # min cloud-free fraction of the site per epoch
MAX_DENSITY_TREES_HA = _f("TT_MAX_DENSITY", 5000)
BASELINE_FOREST_FLAG = _f("TT_BASELINE_FOREST_FLAG", 40)  # % canopy at baseline => additionality risk

# --- Chain ------------------------------------------------------------------
CHAIN_RPC_URL = os.getenv("CHAIN_RPC_URL")          # e.g. Sepolia / Polygon Amoy / local anvil
ORACLE_PRIVATE_KEY = os.getenv("ORACLE_PRIVATE_KEY")
CONTRACT_ADDRESS = os.getenv("CONTRACT_ADDRESS")    # reuse an existing deployment
SOLC_VERSION = os.getenv("TT_SOLC_VERSION", "0.8.24")
