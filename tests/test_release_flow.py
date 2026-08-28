from __future__ import annotations

import hashlib
import json
import os
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from iprate.mcp.assets import CATALOG_SCHEMA_VERSION, StaticAssetError, clear_caches
from iprate.mcp.refresh import snapshot_release
from iprate.mcp.service import get_ip_market_snapshot_result


def _write_json(path: Path, payload: object) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    path.write_bytes(encoded)
    return hashlib.sha256(encoded).hexdigest()[:8]


def _cohort_payload() -> dict[str, object]:
    return {
        "scores": {
            "aggregate_score": 91.2,
            "score_tier": "Q1",
            "confidence_grade": "A",
            "publication_rank": 1,
            "denominators": {"case_units": 123},
            "volume_per_year": 12.5,
            "registration_rate": 0.9,
            "time_to_grant_days": 180,
        },
        "total_firms_filing": 40,
        "top_classes": [{"class_code": "9", "class_label": "Electronics", "share_pct": 20, "rank": 1}],
        "top_clients": [{"display_name": "ACME Ltd", "share_pct": 15, "rank": 1}],
    }


def _build_live_tree(root: Path, *, release_id: str, applications_total: int = 777) -> None:
    firm_slug = "lt-example-ip"
    _write_json(
        root / "firms" / f"{firm_slug}.json",
        {
            "id": 1,
            "name": "Example IP",
            "slug": firm_slug,
            "city": "Vilnius",
            "country_code": "LT",
            "cohorts": {"lt|tm|national|long": _cohort_payload()},
        },
    )
    _write_json(
        root / "attorneys" / "lt-example-person.json",
        {
            "id": 1,
            "name": "Example Person",
            "slug": "lt-example-person",
            "city": "Kaunas",
            "country_code": "LT",
            "cohorts": {"lt|tm|national|long": _cohort_payload()},
        },
    )
    global_files = {
        "firms-index.json": _write_json(root / "firms-index.json", {"slugs": [firm_slug]}),
        "attorneys-index.json": _write_json(root / "attorneys-index.json", {"slugs": ["lt-example-person"]}),
        "analytics_stats.json": _write_json(root / "analytics_stats.json", {"total": {"records": 1000}}),
    }
    stats_path = "lt/tm/national/long/stats.json"
    firms_path = "lt/tm/national/long/firms.json"
    files = {
        stats_path: _write_json(
            root / stats_path,
            {"data": {"applications_total": applications_total}, "meta": {"run_id": 32}},
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
            "release_id": release_id,
            "generated_at": "2026-08-27T12:00:00Z",
            "status": "ok",
            "degraded_reasons": [],
            "entity_counts": {"firms": 1, "attorneys": 1},
            "global_files": global_files,
            "cohorts": {
                "LT:tm:national": {
                    "run_id": 32,
                    "published_at": "2026-08-27T12:00:00Z",
                    "files": files,
                    "degraded_reasons": [],
                }
            },
        },
    )


def _symlinks_supported(base: Path) -> bool:
    probe_target = base / "probe-target"
    probe_target.mkdir(exist_ok=True)
    probe = base / "probe-link"
    try:
        os.symlink(probe_target.name, probe, target_is_directory=True)
    except OSError:
        return False
    probe.unlink()
    return True


@pytest.fixture
def release_dirs(tmp_path: Path) -> tuple[Path, Path]:
    if not _symlinks_supported(tmp_path):
        pytest.skip("symlinks unavailable on this platform/user")
    live = tmp_path / "live"
    _build_live_tree(live, release_id="release-a")
    return live, tmp_path / "releases"


def test_snapshot_creates_consistent_release(
    release_dirs: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    live, releases = release_dirs
    assert snapshot_release(live, releases, settle_seconds=0) == "release-a"
    current = releases / "current"
    assert (current / "manifest.json").read_bytes() == (live / "manifest.json").read_bytes()
    catalog = json.loads((current / "mcp" / "v0.1" / "catalog.json").read_text(encoding="utf-8"))
    assert catalog["release_id"] == "release-a"
    assert catalog["schema_version"] == CATALOG_SCHEMA_VERSION
    assert not (current / "firms").exists()  # profile files stay out of the snapshot

    monkeypatch.setenv("IPRATE_MCP_ASSET_ROOT", str(current))
    monkeypatch.setenv("IPRATE_MCP_ASSET_BASE_URL", "https://assets.example/data/v1")
    clear_caches()
    result = get_ip_market_snapshot_result(
        jurisdiction="LT", right_type="trademark", tier="national", window="long"
    )
    assert result.status == "ok"
    assert result.release_id == "release-a"
    assert result.data["released_statistics"]["applications_total"] == 777


def test_snapshot_noop_until_release_changes(release_dirs: tuple[Path, Path]) -> None:
    live, releases = release_dirs
    assert snapshot_release(live, releases, settle_seconds=0) == "release-a"
    assert snapshot_release(live, releases, settle_seconds=0) is None

    _build_live_tree(live, release_id="release-b", applications_total=888)
    assert snapshot_release(live, releases, settle_seconds=0) == "release-b"
    names = {entry.name for entry in releases.iterdir()}
    assert {"release-a", "release-b", "current"} <= names  # keep=2 retains the previous release

    _build_live_tree(live, release_id="release-c")
    assert snapshot_release(live, releases, settle_seconds=0) == "release-c"
    names = {entry.name for entry in releases.iterdir()}
    assert "release-a" not in names  # pruned beyond keep
    assert "release-b" in names
    assert (releases / "current").resolve().name == "release-c"


def test_snapshot_aborts_on_checksum_mismatch(release_dirs: tuple[Path, Path]) -> None:
    live, releases = release_dirs
    assert snapshot_release(live, releases, settle_seconds=0) == "release-a"

    # A declared cohort file changes without its manifest checksum: the pass
    # must abandon the copy and keep the previous snapshot active.
    stats = live / "lt" / "tm" / "national" / "long" / "stats.json"
    stats.write_text(
        json.dumps({"data": {"applications_total": 999}, "meta": {"run_id": 32}}), encoding="utf-8"
    )
    manifest = json.loads((live / "manifest.json").read_text(encoding="utf-8"))
    manifest["release_id"] = "release-corrupt"
    (live / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(StaticAssetError):
        snapshot_release(live, releases, settle_seconds=0)
    assert (releases / "current").resolve().name == "release-a"
    assert not list(releases.glob(".staging-*"))
