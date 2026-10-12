"""The ChatGPT plugin package must stay importable and consistent with the Worker.

Limits follow OpenAI's plugin submission reference (Agent Plugins format).
"""

import json
import re
import struct
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugin"
MANIFEST = json.loads((PLUGIN / "plugin.json").read_text(encoding="utf-8"))
OPENAI = MANIFEST["extensions"]["com.openai"]
INTERFACE = OPENAI["interface"]


def _worker_tool_names() -> set[str]:
    protocol = (ROOT / "worker" / "src" / "protocol.ts").read_text(encoding="utf-8")
    tools_block = protocol.split("const TOOLS: ToolDefinition[] = [", 1)[1].split("\n];", 1)[0]
    return set(re.findall(r'^    name: "([a-z_]+)",$', tools_block, flags=re.MULTILINE))


def _keys(value) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {key for child in value.values() for key in _keys(child)}
    if isinstance(value, list):
        return {key for child in value for key in _keys(child)}
    return set()


def test_manifest_identity_and_listing_limits():
    assert MANIFEST["$schema"] == "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
    assert re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", MANIFEST["name"]) and len(MANIFEST["name"]) <= 64
    assert re.fullmatch(r"\d+\.\d+\.\d+", MANIFEST["version"])
    assert len(INTERFACE["displayName"]) <= 30
    assert len(INTERFACE["shortDescription"]) <= 30
    assert len(INTERFACE["longDescription"]) <= 4000
    assert len(INTERFACE["developerName"]) <= 80
    assert len(INTERFACE["capabilities"]) <= 20
    assert all(len(label) <= 120 for label in INTERFACE["capabilities"])
    prompts = INTERFACE["defaultPrompt"]
    assert len(prompts) <= 3 and len(set(prompts)) == len(prompts)
    assert all(len(prompt) <= 128 and "@" not in prompt for prompt in prompts)
    for field in ("websiteURL", "supportURL", "privacyPolicyURL", "termsOfServiceURL"):
        assert INTERFACE[field].startswith("https://") and len(INTERFACE[field]) <= 1024
    assert re.fullmatch(r"#[0-9A-Fa-f]{6}", INTERFACE["brandColor"])


def test_listing_text_avoids_pricing_and_promotion():
    listing = " ".join([INTERFACE["shortDescription"], INTERFACE["longDescription"], *INTERFACE["defaultPrompt"]])
    for word in ("free", "price", "pricing", "discount", "trial", "subscription", "€", "$"):
        assert word not in listing.lower(), word


def test_package_omits_fields_the_zip_import_rejects():
    keys = _keys(MANIFEST)
    for forbidden in ("apps", "hooks", "test_credentials", "reviewer_instructions"):
        assert forbidden not in keys
    assert not (PLUGIN / ".app.json").exists()
    assert not (PLUGIN / "hooks").exists()


def test_review_cases_match_the_worker_tools():
    cases = OPENAI["review"]["test_cases"]
    assert len(cases["positive"]) == 5
    assert len(cases["negative"]) == 3
    tools = _worker_tool_names()
    assert tools == {
        "find_ip_representatives",
        "get_ip_representative_profile",
        "get_ip_market_snapshot",
        "get_iprate_coverage",
        "search",
        "fetch",
    }
    covered = set()
    for case in cases["positive"]:
        assert case["description"] and case["prompt"] and case["expected_behavior"]
        triggered = {name.strip() for name in case["tools_triggered"].split(",")}
        assert triggered <= tools
        covered |= triggered
    assert covered == tools
    for case in cases["negative"]:
        assert case["description"] and case["prompt"]
        assert "tools_triggered" not in case
    assert OPENAI["review"]["commerce"] is False


def test_mcp_config_points_at_the_registered_endpoint():
    mcp = json.loads((PLUGIN / "mcp.json").read_text(encoding="utf-8"))
    server = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))
    assert mcp["$schema"] == "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"
    assert len(mcp["mcpServers"]) == 1
    (entry,) = mcp["mcpServers"].values()
    assert entry == {"type": "streamable-http", "url": server["remotes"][0]["url"]}


def test_skills_have_matching_names_and_fit_import_limits():
    skills = sorted((PLUGIN / "skills").glob("*/SKILL.md"))
    assert skills
    for skill in skills:
        raw = skill.read_bytes()
        assert len(raw) <= 256 * 1024
        text = raw.decode("utf-8")
        front = text.split("---", 2)[1]
        fields = dict(line.split(": ", 1) for line in front.strip().splitlines())
        assert fields["name"] == skill.parent.name
        assert len(fields["description"]) > 40


def test_logo_is_square_and_carries_no_metadata():
    for field in ("logo", "composerIcon"):
        path = PLUGIN / INTERFACE[field].removeprefix("./")
        data = path.read_bytes()
        assert data[:8] == b"\x89PNG\r\n\x1a\n"
        width, height = struct.unpack(">II", data[16:24])
        assert width == height >= 48 and width <= 4096
        chunks, position = [], 8
        while position < len(data):
            (length,) = struct.unpack(">I", data[position : position + 4])
            chunks.append(data[position + 4 : position + 8].decode("ascii"))
            position += 12 + length
        assert set(chunks) <= {"IHDR", "PLTE", "tRNS", "IDAT", "IEND"}, chunks
