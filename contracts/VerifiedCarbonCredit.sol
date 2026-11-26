// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/// @title TerraTruth Verified Carbon Credit
/// @notice ERC-20 carbon credit that can ONLY be minted by an authorised AI oracle
///         after satellite evidence verifies real biomass growth. Developers can
///         register claims, but a claim alone never creates tokens ("no garbage in").
/// @dev    1 token unit = 1 kg CO2e (decimals = 3, so 1.000 token = 1 tonne CO2e).
contract VerifiedCarbonCredit {
    // ---------------------------------------------------------------- ERC-20
    string public constant name = "TerraTruth Verified Carbon";
    string public constant symbol = "vtCO2e";
    uint8 public constant decimals = 3;
    uint256 public totalSupply;
    uint256 public totalRetired;
    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;

    event Transfer(address indexed from, address indexed to, uint256 value);
    event Approval(address indexed owner, address indexed spender, uint256 value);

    // ------------------------------------------------------------ Governance
    address public owner;
    mapping(address => bool) public isOracle;

    event OracleUpdated(address indexed oracle, bool enabled);

    modifier onlyOwner() {
        require(msg.sender == owner, "not owner");
        _;
    }

    modifier onlyOracle() {
        require(isOracle[msg.sender], "not oracle");
        _;
    }

    // -------------------------------------------------------------- Projects
    enum Status { None, Pending, Verified, Rejected }

    struct Project {
        address developer;
        uint64 plantingYear;
        uint256 claimedTrees;
        uint256 claimedKgCO2e;   // what the developer SAYS was sequestered
        uint256 mintedKgCO2e;    // what the oracle has actually allowed to exist
        bytes32 siteHash;        // keccak256 of the site GeoJSON
        Status status;
        uint32 verifications;
    }

    struct Verification {
        uint256 verifiedKgCO2e;  // satellite-measured sequestration
        uint16 confidenceBps;    // 0..10000
        bool passed;
        bytes32 evidenceHash;    // keccak256 of the full AI evidence report
        uint64 timestamp;
    }

    mapping(bytes32 => Project) public projects;
    mapping(bytes32 => Verification[]) private _history;
    bytes32[] public projectIds;

    event ProjectRegistered(
        bytes32 indexed projectId,
        address indexed developer,
        uint64 plantingYear,
        uint256 claimedTrees,
        uint256 claimedKgCO2e,
        bytes32 siteHash
    );
    event VerificationSubmitted(
        bytes32 indexed projectId,
        address indexed oracle,
        bool passed,
        uint256 claimedKgCO2e,
        uint256 verifiedKgCO2e,
        uint256 mintedKgCO2e,
        uint16 confidenceBps,
        bytes32 evidenceHash
    );
    event CreditsRetired(address indexed holder, uint256 amountKg, string beneficiary);

    constructor() {
        owner = msg.sender;
        isOracle[msg.sender] = true;
        emit OracleUpdated(msg.sender, true);
    }

    function setOracle(address oracle, bool enabled) external onlyOwner {
        isOracle[oracle] = enabled;
        emit OracleUpdated(oracle, enabled);
    }

    /// @notice Register a reforestation claim. Callable by the developer themself
    ///         or by an oracle acting as registrar. Registration mints NOTHING.
    function registerProject(
        bytes32 projectId,
        address developer,
        uint64 plantingYear,
        uint256 claimedTrees,
        uint256 claimedKgCO2e,
        bytes32 siteHash
    ) external {
        require(developer != address(0), "zero developer");
        require(msg.sender == developer || isOracle[msg.sender], "not developer/registrar");
        require(projects[projectId].status == Status.None, "project exists");
        require(claimedKgCO2e > 0, "empty claim");

        projects[projectId] = Project({
            developer: developer,
            plantingYear: plantingYear,
            claimedTrees: claimedTrees,
            claimedKgCO2e: claimedKgCO2e,
            mintedKgCO2e: 0,
            siteHash: siteHash,
            status: Status.Pending,
            verifications: 0
        });
        projectIds.push(projectId);
        emit ProjectRegistered(projectId, developer, plantingYear, claimedTrees, claimedKgCO2e, siteHash);
    }

    /// @notice Oracle posts the AI verdict. If it passed, credits are minted for the
    ///         smaller of (claimed, verified) minus anything already minted, so a
    ///         project can never be over-credited and re-verification only tops up
    ///         genuine new growth.
    function submitVerification(
        bytes32 projectId,
        uint256 verifiedKgCO2e,
        uint16 confidenceBps,
        bool passed,
        bytes32 evidenceHash
    ) external onlyOracle returns (uint256 minted) {
        Project storage p = projects[projectId];
        require(p.status != Status.None, "unknown project");
        require(confidenceBps <= 10000, "bad confidence");

        _history[projectId].push(Verification({
            verifiedKgCO2e: verifiedKgCO2e,
            confidenceBps: confidenceBps,
            passed: passed,
            evidenceHash: evidenceHash,
            timestamp: uint64(block.timestamp)
        }));
        p.verifications += 1;

        if (passed) {
            uint256 creditable = verifiedKgCO2e < p.claimedKgCO2e ? verifiedKgCO2e : p.claimedKgCO2e;
            if (creditable > p.mintedKgCO2e) {
                minted = creditable - p.mintedKgCO2e;
                p.mintedKgCO2e = creditable;
                _mint(p.developer, minted);
            }
            p.status = Status.Verified;
        } else if (p.mintedKgCO2e == 0) {
            // Publicly and permanently flags the claim as unsupported by evidence.
            p.status = Status.Rejected;
        }

        emit VerificationSubmitted(
            projectId, msg.sender, passed, p.claimedKgCO2e, verifiedKgCO2e, minted, confidenceBps, evidenceHash
        );
    }

    function verificationCount(bytes32 projectId) external view returns (uint256) {
        return _history[projectId].length;
    }

    function getVerification(bytes32 projectId, uint256 index) external view returns (Verification memory) {
        return _history[projectId][index];
    }

    function projectCount() external view returns (uint256) {
        return projectIds.length;
    }

    /// @notice Permanently retire (burn) credits to claim the offset.
    function retire(uint256 amountKg, string calldata beneficiary) external {
        require(balanceOf[msg.sender] >= amountKg, "insufficient balance");
        balanceOf[msg.sender] -= amountKg;
        totalSupply -= amountKg;
        totalRetired += amountKg;
        emit Transfer(msg.sender, address(0), amountKg);
        emit CreditsRetired(msg.sender, amountKg, beneficiary);
    }

    // ------------------------------------------------------- ERC-20 plumbing
    function transfer(address to, uint256 value) external returns (bool) {
        _transfer(msg.sender, to, value);
        return true;
    }

    function approve(address spender, uint256 value) external returns (bool) {
        allowance[msg.sender][spender] = value;
        emit Approval(msg.sender, spender, value);
        return true;
    }

    function transferFrom(address from, address to, uint256 value) external returns (bool) {
        uint256 allowed = allowance[from][msg.sender];
        require(allowed >= value, "allowance");
        if (allowed != type(uint256).max) {
            allowance[from][msg.sender] = allowed - value;
        }
        _transfer(from, to, value);
        return true;
    }

    function _transfer(address from, address to, uint256 value) internal {
        require(to != address(0), "zero to");
        require(balanceOf[from] >= value, "insufficient balance");
        balanceOf[from] -= value;
        balanceOf[to] += value;
        emit Transfer(from, to, value);
    }

    function _mint(address to, uint256 value) internal {
        totalSupply += value;
        balanceOf[to] += value;
        emit Transfer(address(0), to, value);
    }
}
