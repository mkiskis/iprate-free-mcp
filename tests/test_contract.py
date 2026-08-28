from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from iprate.mcp.assets import _cached_json, build_catalog
from iprate.mcp.server import mcp, mcp_http_app
from iprate.mcp.service import (
    find_ip_representatives_result,
    get_ip_market_snapshot_result,
    get_ip_representative_profile_result,
    get_iprate_coverage_result,
)


def _write_json(path: Path, payload: object) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    path.write_bytes(encoded)
    return hashlib.sha256(encoded).hexdigest()[:8]


def _cohort(*, rank: int | None, score: float, client: str) -> dict[str, object]:
    return {
        "scores": {
            "aggregate_score": score,
            "score_tier": "Q1",
            "confidence_grade": "A",
            "publication_rank": rank,
            "denominators": {"case_units": 123},
            "volume_per_year": 12.5,
            "registration_rate": 0.9,
            "time_to_grant_days": 180,
        },
        "total_firms_filing": 40,
        "top_classes": [{"class_code": "9", "class_label": "Electronics", "share_pct": 20, "rank": 1}],
        "top_clients": [{"display_name": client, "share_pct": 15, "rank": 1}],
    }


@pytest.fixture
def static_release(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "data" / "v1"
    firm_slug = "lt-example-ip"
    attorney_slug = "lt-example-person"
    _write_json(
        root / "firms" / f"{firm_slug}.json",
        {
            "meta": {"generated_at": "2026-08-27T12:00:00Z"},
            "id": 1,
            "name": "Example IP",
            "slug": firm_slug,
            "city": "Vilnius",
            "country_code": "LT",
            "cohorts": {"lt|tm|national|long": _cohort(rank=1, score=91.2, client="ACME Ltd")},
        },
    )
    _write_json(
        root / "attorneys" / f"{attorney_slug}.json",
        {
            "meta": {"generated_at": "2026-08-27T12:00:00Z"},
            "id": 1,
            "name": "Example Person",
            "slug": attorney_slug,
            "city": "Kaunas",
            "country_code": "LT",
            "cohorts": {"lt|tm|national|long": _cohort(rank=None, score=82.4, client="ACME Ltd")},
        },
    )
    global_files = {
        "firms-index.json": _write_json(root / "firms-index.json", {"slugs": [firm_slug]}),
        "attorneys-index.json": _write_json(root / "attorneys-index.json", {"slugs": [attorney_slug]}),
        "analytics_stats.json": _write_json(
            root / "analytics_stats.json",
            {
                "by_vertical": {"tm": {"records": 1000, "jurisdictions": 1}},
                "total": {"records": 1000, "jurisdictions": 1},
                "generated_at": "2026-08-27T12:00:00Z",
            },
        ),
        "countries.json": _write_json(root / "countries.json", {"data": [], "meta": {}}),
    }
    stats_path = "lt/tm/national/long/stats.json"
    firms_path = "lt/tm/national/long/firms.json"
    cohort_files = {
        stats_path: _write_json(
            root / stats_path,
            {
                "data": {
                    "country_code": "LT",
                    "vertical": "tm",
                    "tier": "national",
                    "window_kind": "long",
                    "applications_total": 777,
                    "rated_firms": 1,
                },
                "meta": {"run_id": 32, "generated_at": "2026-08-27T12:00:00Z"},
            },
        ),
        firms_path: _write_json(
            root / firms_path,
            {
                "data": [
                    {
                        "id": 1,
                        "name": "Example IP",
                        "slug": firm_slug,
                        "city": "Vilnius",
                        "country_code": "LT",
                        "score_tier": "Q1",
                        "confidence_grade": "A",
                        "volume_per_year": 12.5,
                        "registration_rate": 0.9,
                        "time_to_grant_days": 180,
                        "top_class": "9",
                    }
                ],
                "meta": {"run_id": 32, "count": 1},
            },
        ),
    }
    _write_json(
        root / "manifest.json",
        {
            "schema_version": 2,
            "release_id": "test-release",
            "generated_at": "2026-08-27T12:00:00Z",
            "status": "ok",
            "degraded_reasons": [],
            "entity_counts": {"firms": 1, "attorneys": 1},
            "global_files": global_files,
            "cohorts": {
                "LT:tm:national": {
                    "run_id": 32,
                    "published_at": "2026-08-27T12:00:00Z",
                    "files": cohort_files,
                    "degraded_reasons": [],
                }
            },
        },
    )
    build_catalog(root)
    _cached_json.cache_clear()
    monkeypatch.setenv("IPRATE_MCP_ASSET_ROOT", str(root))
    monkeypatch.setenv("IPRATE_MCP_ASSET_BASE_URL", "https://assets.example/data/v1")
    return root


@pytest.mark.asyncio
async def test_protocol_lists_exactly_four_static_read_only_tools() -> None:
    tools = await mcp.list_tools()
    assert {tool.name for tool in tools} == {
        "find_ip_representatives",
        "get_ip_representative_profile",
        "get_ip_market_snapshot",
        "get_iprate_coverage",
    }
    for tool in tools:
        assert tool.annotations.read_only_hint is True
        assert tool.annotations.destructive_hint is False
        assert tool.annotations.idempotent_hint is True
        properties = tool.input_schema.get("properties", {})
        assert "activity_from" not in properties
        assert "activity_to" not in properties


def test_unfiltered_enumeration_is_rejected() -> None:
    assert find_ip_representatives_result().status == "invalid_request"


def test_static_catalog_search_preserves_released_rating(static_release: Path) -> None:
    result = find_ip_representatives_result(
        jurisdiction="LT",
        right_type="trademark",
        nice_classes=["9"],
        client_name="ACME",
        tier="national",
        window="long",
    )
    assert result.status == "ok"
    assert result.release_id == "test-release"
    item = result.data["items"][0]
    assert item["quoted_name"] == "Example IP"
    assert item["matching_cohort"]["published_rating"]["score"] == 91.2
    assert item["matching_cohort"]["released_activity"]["case_units"] == 123
    assert result.coverage["static_assets"] == ["mcp/v0.1/catalog.json"]


def test_profile_identifier_collision_requires_type(static_release: Path) -> None:
    ambiguous = get_ip_representative_profile_result(representative_id=1)
    assert ambiguous.status == "ambiguous"
    result = get_ip_representative_profile_result(representative_id=1, representative_type="attorney")
    assert result.status == "ok"
    assert result.data["quoted_name"] == "Example Person"
    assert len(result.data["released_cohorts"]) == 1


def test_market_snapshot_copies_static_values(static_release: Path) -> None:
    result = get_ip_market_snapshot_result(
        jurisdiction="LT",
        right_type="trademark",
        tier="national",
        window="long",
    )
    assert result.status == "ok"
    assert result.data["released_statistics"]["applications_total"] == 777
    assert result.data["leading_representatives"][0]["volume_per_year"] == 12.5
    assert len(result.data["leading_representatives"]) <= 5


def test_coverage_comes_from_manifest_and_analytics(static_release: Path) -> None:
    result = get_iprate_coverage_result(jurisdiction="LT", right_type="trademark", tier="national")
    assert result.status == "ok"
    assert result.data["hold_state"] == "held"
    assert result.data["holdings"]["total"]["records"] == 1000
    assert result.data["cohorts"][0]["run_id"] == 32


def test_checksum_mismatch_fails_closed(static_release: Path) -> None:
    path = static_release / "lt" / "tm" / "national" / "long" / "stats.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["data"]["applications_total"] = 778
    _write_json(path, payload)
    _cached_json.cache_clear()
    result = get_ip_market_snapshot_result(
        jurisdiction="LT",
        right_type="trademark",
        tier="national",
        window="long",
    )
    assert result.status == "source_unavailable"
    assert result.coverage["error_type"] == "StaticAssetError"


def test_mixed_release_catalog_fails_closed(static_release: Path) -> None:
    path = static_release / "mcp" / "v0.1" / "catalog.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["release_id"] = "different-release"
    _write_json(path, payload)
    _cached_json.cache_clear()
    result = get_iprate_coverage_result()
    assert result.status == "source_unavailable"


def test_http_transport_initializes(static_release: Path) -> None:
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "contract-test", "version": "1.0"},
        },
    }
    with TestClient(mcp_http_app) as client:
        response = client.post(
            "/mcp",
            json=request,
            headers={"Accept": "application/json, text/event-stream"},
        )
    assert response.status_code == 200
    assert response.json()["result"]["serverInfo"]["name"] == "eu.iprate/ip-analytics"


def test_package_has_no_database_or_private_iprate_dependency() -> None:
    source_root = Path(__file__).parents[1] / "src"
    combined = "\n".join(path.read_text(encoding="utf-8") for path in source_root.rglob("*.py"))
    forbidden = ["sqlalchemy", "psycopg", "sqlite3", "DATABASE_URL", "iprate.pipeline", "iprate.models", "iprate.api"]
    assert not [token for token in forbidden if token in combined]
