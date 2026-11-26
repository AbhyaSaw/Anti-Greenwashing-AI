"""Web3 layer: compiles and deploys VerifiedCarbonCredit, then acts as the oracle.

Default mode runs an in-process Ethereum chain (eth-tester + py-evm) so the app
works with zero setup. Set CHAIN_RPC_URL + ORACLE_PRIVATE_KEY (and optionally
CONTRACT_ADDRESS) to use a real network such as Sepolia, Polygon Amoy or Anvil.
"""
import json
import threading

from web3 import Web3

from . import config

KG = 1000  # contract stores kg CO2e; 3 decimals => 1 token = 1 tonne


def hx(b) -> str:
    s = b.hex() if hasattr(b, "hex") else str(b)
    return s if s.startswith("0x") else "0x" + s


def _compile():
    out = config.BUILD_DIR / "VerifiedCarbonCredit.json"
    src = config.CONTRACT_SRC.read_text()
    if out.exists():
        art = json.loads(out.read_text())
        if art.get("source_hash") == hx(Web3.keccak(text=src)):
            return art
    import solcx
    if config.SOLC_VERSION not in [str(v) for v in solcx.get_installed_solc_versions()]:
        import time
        for attempt in range(6):  # flaky networks: the binary is only downloaded once
            try:
                solcx.install_solc(config.SOLC_VERSION)
                break
            except Exception:
                if attempt == 5:
                    raise
                time.sleep(2 + attempt * 2)
    res = solcx.compile_standard({
        "language": "Solidity",
        "sources": {"VerifiedCarbonCredit.sol": {"content": src}},
        "settings": {"optimizer": {"enabled": True, "runs": 200}, "evmVersion": "paris",
                     "outputSelection": {"*": {"*": ["abi", "evm.bytecode.object"]}}},
    }, solc_version=config.SOLC_VERSION)
    c = res["contracts"]["VerifiedCarbonCredit.sol"]["VerifiedCarbonCredit"]
    art = {"abi": c["abi"], "bytecode": c["evm"]["bytecode"]["object"],
           "source_hash": hx(Web3.keccak(text=src)), "solc": config.SOLC_VERSION}
    config.BUILD_DIR.mkdir(exist_ok=True)
    out.write_text(json.dumps(art, indent=1))
    return art


def project_key(project_id: str) -> bytes:
    return Web3.keccak(text=f"terratruth:{project_id}")


def site_hash(geojson) -> bytes:
    return Web3.keccak(text=json.dumps(geojson, sort_keys=True, separators=(",", ":")))


def evidence_hash(report) -> bytes:
    """Hash of the full evidence report, images included, so evidence can't be swapped later."""
    return Web3.keccak(text=json.dumps(report, sort_keys=True, separators=(",", ":"), default=str))


class Chain:
    def __init__(self):
        self.lock = threading.RLock()
        art = _compile()
        self.abi = art["abi"]
        if config.CHAIN_RPC_URL:
            from eth_account import Account
            self.mode = "rpc"
            self.w3 = Web3(Web3.HTTPProvider(config.CHAIN_RPC_URL))
            if not config.ORACLE_PRIVATE_KEY:
                raise RuntimeError("ORACLE_PRIVATE_KEY is required with CHAIN_RPC_URL")
            self.acct = Account.from_key(config.ORACLE_PRIVATE_KEY)
            self.oracle = self.acct.address
            self.dev_wallets = []
        else:
            from web3 import EthereumTesterProvider
            self.mode = "in-process"
            self.w3 = Web3(EthereumTesterProvider())
            self.acct = None
            self.oracle = self.w3.eth.accounts[0]
            self.dev_wallets = list(self.w3.eth.accounts[1:6])
        if config.CONTRACT_ADDRESS:
            self.address = Web3.to_checksum_address(config.CONTRACT_ADDRESS)
        else:
            Factory = self.w3.eth.contract(abi=self.abi, bytecode=art["bytecode"])
            rcpt = self._send(Factory.constructor())
            self.address = rcpt["contractAddress"]
        self.c = self.w3.eth.contract(address=self.address, abi=self.abi)

    # -------------------------------------------------------------- tx ----
    def _send(self, fn, sender=None):
        with self.lock:
            if self.mode == "in-process":
                tx_hash = fn.transact({"from": sender or self.oracle})
            else:
                tx = fn.build_transaction({
                    "from": self.oracle,
                    "nonce": self.w3.eth.get_transaction_count(self.oracle, "pending"),
                    "chainId": self.w3.eth.chain_id,
                })
                signed = self.acct.sign_transaction(tx)
                raw = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction")
                tx_hash = self.w3.eth.send_raw_transaction(raw)
            rcpt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
            if rcpt["status"] != 1:
                raise RuntimeError(f"transaction reverted: {hx(tx_hash)}")
            return rcpt

    def register(self, project):
        dev = Web3.to_checksum_address(project["developer"])
        sender = dev if self.mode == "in-process" and dev in self.dev_wallets else self.oracle
        rcpt = self._send(self.c.functions.registerProject(
            project_key(project["id"]), dev, int(project["planting_year"]), int(project["claimed_trees"]),
            int(round(float(project["claimed_tco2e"]) * KG)), site_hash(project["geojson"])), sender)
        return hx(rcpt["transactionHash"])

    def submit(self, project_id, report):
        ev = evidence_hash(report)
        passed = report["verdict"] == "VERIFIED"
        rcpt = self._send(self.c.functions.submitVerification(
            project_key(project_id), int(round(report["verified_tco2e"] * KG)),
            int(round(report["confidence"] * 10000)), passed, ev))
        logs = self.c.events.VerificationSubmitted().process_receipt(rcpt)
        minted = logs[0]["args"]["mintedKgCO2e"] / KG if logs else 0
        return {"tx": hx(rcpt["transactionHash"]), "evidence_hash": hx(ev),
                "minted_tco2e": minted, "block": rcpt["blockNumber"]}

    def retire(self, holder, tonnes, beneficiary):
        holder = Web3.to_checksum_address(holder)
        if self.mode != "in-process":
            raise RuntimeError("Retirement must be signed by the holder's own wallet on a public network.")
        rcpt = self._send(self.c.functions.retire(int(round(tonnes * KG)), beneficiary), holder)
        return hx(rcpt["transactionHash"])

    # ----------------------------------------------------------- reads ----
    def onchain_project(self, project_id):
        p = self.c.functions.projects(project_key(project_id)).call()
        status = ["None", "Pending", "Verified", "Rejected"][p[6]]
        return {"developer": p[0], "planting_year": p[1], "claimed_trees": p[2], "claimed_tco2e": p[3] / KG,
                "minted_tco2e": p[4] / KG, "site_hash": hx(p[5]), "status": status,
                "verifications": p[7]}

    def info(self):
        f = self.c.functions
        wallets = [{"address": a, "balance_tco2e": f.balanceOf(a).call() / KG} for a in self.dev_wallets]
        return {"mode": self.mode, "chain_id": self.w3.eth.chain_id, "contract": self.address,
                "oracle": self.oracle, "block": self.w3.eth.block_number,
                "total_supply_tco2e": f.totalSupply().call() / KG,
                "total_retired_tco2e": f.totalRetired().call() / KG,
                "symbol": f.symbol().call(), "wallets": wallets}

    def ledger(self, limit=50):
        out = []
        for name in ["ProjectRegistered", "VerificationSubmitted", "CreditsRetired"]:
            ev = getattr(self.c.events, name)
            try:
                logs = ev.get_logs(from_block=0) if self._v7 else ev.get_logs(fromBlock=0)
            except Exception:
                logs = []
            for lg in logs:
                a = dict(lg["args"])
                for k, v in a.items():
                    if isinstance(v, (bytes, bytearray)):
                        a[k] = hx(v)
                    elif k.endswith("KgCO2e") or k == "amountKg":
                        a[k] = v / KG
                out.append({"event": name, "block": lg["blockNumber"], "tx": hx(lg["transactionHash"]),
                            "args": a})
        out.sort(key=lambda e: -e["block"])
        return out[:limit]

    @property
    def _v7(self):
        import web3
        return int(web3.__version__.split(".")[0]) >= 7
