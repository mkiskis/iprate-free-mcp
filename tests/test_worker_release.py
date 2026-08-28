from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

import pytest

from iprate.mcp.assets import StaticAssetError, clear_caches
from iprate.mcp.worker_release import POINTER_KEY, build_worker_artifacts, run_once

if TYPE_CHECKING:
    from pathlib import Path


class FakeStore:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.put_order: list[str] = []

    def get(self, key: str) -> bytes | None:
        return self.objects.get(key)

    def put(self, key: str, body: bytes, content_type: str = "application/json") -> None:
        self.objects[key] = body
        self.put_order.append(key)

    def list_keys(self, prefix: str) -> list[str]:
        return [key for key in self.objects if key.startswith(prefix)]

    def delete(self, key: str) -> None:
        self.objects.pop(key, None)


def _write_json(path: Path, payload: object) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    path.write_bytes(encoded)
    return hashlib.sha256(encoded).hexdigest()[:8]


def _cohort_payload(*, rank: int | None, score: float, client: str) -> dict[str, object]:
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
        "top_classes": [{"class_code": "09", "class_label": "Electronics", "share_pct": 20, "rank": 1}],
        "top_clients": [{"display_name": client, "share_pct": 15, "rank": 1}],
    }


def _build_live_tree(root: Path, *, release_id: str, applications_total: int = 777) -> None:
    _write_json(
        root / "firms" / "lt-example-ip.json",
        {
            "id": 1,
            "name": "Example IP",
            "slug": "lt-example-ip",
            "city": "Vilnius",
            "country_code": "LT",
            "cohorts": {
                "lt|tm|national|recent": _cohort_payload(rank=None, score=70.0, client="Beta Corp"),
                "lt|tm|national|long": _cohort_payload(rank=1, score=91.2, client="ACME Ltd"),
            },
        },
    )
    _write_json(
        root / "firms" / "lt-blocked-selffiler.json",
        {
            "id": 2,
            "name": "Blocked Self Filer",
            "slug": "lt-blocked-selffiler",
            "city": "Vilnius",
            "country_code": "LT",
            "cohorts": {"lt|tm|national|long": _cohort_payload(rank=2, score=80.0, client="ACME Ltd")},
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
            "cohorts": {"lt|tm|national|long": _cohort_payload(rank=None, score=82.4, client="ACME Ltd")},
        },
    )
    global_files = {
        "firms-index.json": _write_json(
            root / "firms-index.json", {"slugs": ["lt-blocked-selffiler", "lt-example-ip"]}
        ),
        "attorneys-index.json": _write_json(root / "attorneys-index.json", {"slugs": ["lt-example-person"]}),
        "analytics_stats.json": _write_json(root / "analytics_stats.json", {"total": {"records": 1000}}),
        "search-index.json": _write_json(
            root / "search-index.json",
            {
                "data": [
                    {"entity_type": "firm", "entity_id": 1, "slug": "lt-example-ip"},
                    {"entity_type": "attorney", "entity_id": 1, "slug": "lt-example-person"},
                ],
                "meta": {"count": 2},
            },
        ),
    }
    stats_path = "lt/tm/national/long/stats.json"
    firms_path = "lt/tm/national/long/firms.json"
    featured_path = "lt/tm/national/long/featured.json"
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
                        "slug": "lt-example-ip",
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
        featured_path: _write_json(root / featured_path, {"data": [], "meta": {"run_id": 32}}),
    }
    _write_json(
        root / "manifest.json",
        {
            "schema_version": 2,
            "release_id": release_id,
            "generated_at": "2026-08-27T12:00:00Z",
            "status": "ok",
            "degraded_reasons": [],
            "entity_counts": {"firms": 2, "attorneys": 1},
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
    clear_caches()


@pytest.fixture
def live_tree(tmp_path: Path) -> Path:
    root = tmp_path / "live"
    _build_live_tree(root, release_id="release-a")
    return root


def test_artifacts_cover_search_shards_and_assets(live_tree: Path) -> None:
    release_id, objects = build_worker_artifacts(live_tree)
    assert release_id == "release-a"

    search = json.loads(objects["search.json"])
    assert search["release_id"] == "release-a"
    rows = search["entities"]
    assert {(row[0], row[2]) for row in rows} == {("firm", "lt-example-ip"), ("attorney", "lt-example-person")}
    firm_row = next(row for row in rows if row[0] == "firm")
    _entity_type, entity_id, slug, name_key, country, shard, cohorts = firm_row
    assert (entity_id, name_key, country) == (1, "example ip", "LT")
    assert cohorts[0][:4] == ["LT", "tm", "national", "long"]  # priority-sorted: long first
    assert cohorts[0][6] == ["9"]  # class codes normalised, no leading zero
    assert cohorts[0][7] == ["acme ltd"]

    shard_doc = json.loads(objects[f"entities/{shard}.json"])
    record = shard_doc["entities"][f"firm:{slug}"]
    assert record["quoted_name"] == "Example IP"
    assert record["profile_url"] == "https://iprate.eu/firms/lt-example-ip/"
    assert record["cohorts"][0]["published_rating"]["rank"] == 1
    assert record["cohorts"][0]["leading_clients"][0]["quoted_name"] == "ACME Ltd"

    assert objects["assets/lt/tm/national/long/stats.json"] == (
        live_tree / "lt/tm/national/long/stats.json"
    ).read_bytes()
    assert "assets/lt/tm/national/long/featured.json" not in objects

    manifest = json.loads(objects["mcp-manifest.json"])
    assert manifest["release_id"] == "release-a"
    assert manifest["mcp_entity_counts"] == {"firms": 1, "attorneys": 1, "excluded": 1}
    cohort = manifest["cohorts"]["LT:tm:national"]
    assert cohort["run_id"] == 32
    assert cohort["windows"] == ["long"]
    assert cohort["files"]["long"]["stats"] == "lt/tm/national/long/stats.json"
    assert set(manifest["checksums"]) == set(objects) - {"mcp-manifest.json"}


def test_blocked_self_filer_is_excluded_everywhere(live_tree: Path) -> None:
    _release_id, objects = build_worker_artifacts(live_tree)
    for key, body in objects.items():
        assert b"lt-blocked-selffiler" not in body, key
        assert b"Blocked Self Filer" not in body, key


def test_run_once_activates_then_noops(live_tree: Path) -> None:
    store = FakeStore()
    assert run_once(live_tree, store, settle_seconds=0) == "release-a"
    pointer = json.loads(store.objects[POINTER_KEY])
    assert pointer["release_id"] == "release-a"
    prefix = pointer["prefix"]
    assert prefix.startswith("releases/release-a/")
    assert store.put_order[-1] == POINTER_KEY
    assert store.put_order[-2] == prefix + "mcp-manifest.json"
    assert prefix + "search.json" in store.objects

    puts_before = len(store.put_order)
    assert run_once(live_tree, store, settle_seconds=0) is None
    assert len(store.put_order) == puts_before


def test_release_bump_keeps_two_builds(live_tree: Path) -> None:
    store = FakeStore()
    assert run_once(live_tree, store, settle_seconds=0) == "release-a"
    _build_live_tree(live_tree, release_id="release-b", applications_total=888)
    assert run_once(live_tree, store, settle_seconds=0) == "release-b"
    _build_live_tree(live_tree, release_id="release-c")
    assert run_once(live_tree, store, settle_seconds=0) == "release-c"

    prefixes = {"/".join(key.split("/")[:3]) for key in store.objects if key.startswith("releases/")}
    assert len(prefixes) == 2
    assert not any(prefix.startswith("releases/release-a") for prefix in prefixes)
    pointer = json.loads(store.objects[POINTER_KEY])
    assert pointer["release_id"] == "release-c"
    current = json.loads(store.objects[pointer["prefix"] + "mcp-manifest.json"])
    assert current["release_id"] == "release-c"


def test_checksum_mismatch_aborts_without_uploading(live_tree: Path) -> None:
    store = FakeStore()
    assert run_once(live_tree, store, settle_seconds=0) == "release-a"
    keys_before = set(store.objects)

    stats = live_tree / "lt" / "tm" / "national" / "long" / "stats.json"
    stats.write_text(
        json.dumps({"data": {"applications_total": 999}, "meta": {"run_id": 32}}), encoding="utf-8"
    )
    manifest = json.loads((live_tree / "manifest.json").read_text(encoding="utf-8"))
    manifest["release_id"] = "release-corrupt"
    (live_tree / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    clear_caches()

    with pytest.raises(StaticAssetError):
        run_once(live_tree, store, settle_seconds=0)
    assert set(store.objects) == keys_before
    assert json.loads(store.objects[POINTER_KEY])["release_id"] == "release-a"
