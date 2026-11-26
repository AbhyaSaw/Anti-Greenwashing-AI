"""End-to-end test: synthetic satellite -> AI -> oracle -> smart contract.

Run:  .venv\\Scripts\\python.exe -m tests.test_pipeline
"""
import json
import os
import tempfile

os.environ["TT_DATA_DIR"] = tempfile.mkdtemp()

from app import oracle  # noqa: E402
from app.chain import Chain  # noqa: E402

SITE = {"type": "Polygon", "coordinates": [[[37.340, -3.110], [37.352, -3.110], [37.352, -3.100],
                                            [37.340, -3.100], [37.340, -3.110]]]}


def project(pid, dev, claimed):
    return {"id": pid, "name": pid, "developer": dev, "geojson": SITE, "planting_year": 2019,
            "claimed_trees": 140_000, "claimed_tco2e": claimed}


def main():
    chain = Chain()
    dev = chain.dev_wallets[0]
    results = {}
    for scenario, claimed in [("honest", 2500), ("fraud", 2500)]:
        p = project(f"test-{scenario}", dev, claimed)
        chain.register(p)
        rep = oracle.run_verification(p, f"synthetic:{scenario}", log=lambda s: print("   ", s))
        rep = json.loads(json.dumps(rep, default=str))
        res = chain.submit(p["id"], rep)
        onchain = chain.onchain_project(p["id"])
        results[scenario] = (rep, res, onchain)
        print(f"== {scenario}: {rep['verdict']} verified={rep['verified_tco2e']} minted={res['minted_tco2e']} "
              f"onchain={onchain['status']}\n")

    h_rep, h_res, h_on = results["honest"]
    f_rep, f_res, f_on = results["fraud"]
    assert h_rep["verdict"] == "VERIFIED", h_rep["flags"]
    assert h_res["minted_tco2e"] > 0 and h_on["status"] == "Verified"
    assert h_res["minted_tco2e"] <= 2500 + 1e-6, "minting must be capped at the claim"
    assert f_rep["verdict"] == "REJECTED"
    assert f_res["minted_tco2e"] == 0 and f_on["status"] == "Rejected"
    assert any(f["code"] == "CLAIM_NOT_SUPPORTED" for f in f_rep["flags"])

    # Re-verifying the same evidence must not mint twice
    again = chain.submit("test-honest", h_rep)
    assert again["minted_tco2e"] == 0, "double minting!"

    # Non-oracle accounts cannot mint
    try:
        chain.c.functions.submitVerification(b"\x00" * 32, 10**9, 9000, True, b"\x00" * 32).transact(
            {"from": chain.dev_wallets[1]})
        raise AssertionError("non-oracle was able to submit a verification")
    except Exception as e:
        assert "not oracle" in str(e) or "revert" in str(e).lower(), e

    info = chain.info()
    assert abs(info["total_supply_tco2e"] - h_res["minted_tco2e"]) < 1e-6
    print("ALL CHECKS PASSED", json.dumps({k: v for k, v in info.items() if k != "wallets"}))


if __name__ == "__main__":
    main()
