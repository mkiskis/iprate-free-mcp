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


def _cohort_payload(*, rank: int | None, score: float, client: str, tier: str | None = "Q1") -> dict[str, object]:
    return {
        "scores": {
            "aggregate_score": score,
            "score_tier": tier,
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
        root / "firms" / "lt-unrated-shell.json",
        {
            "id": 2,
            "name": "Unrated Shell",
            "slug": "lt-unrated-shell",
            "city": "Vilnius",
            "country_code": "LT",
            "cohorts": {"lt|tm|national|long": _cohort_payload(rank=None, score=80.0, client="ACME Ltd", tier=None)},
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
        "firms-index.json": _write_json(root / "firms-index.json", {"slugs": ["lt-example-ip", "lt-unrated-shell"]}),
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
    assert manifest["mcp_entity_counts"] == {
        "firms": 1,
        "attorneys": 1,
        "excluded": 1,
        "folded": 0,
        "retired_aliases": 0,
    }
    cohort = manifest["cohorts"]["LT:tm:national"]
    assert cohort["run_id"] == 32
    assert cohort["windows"] == ["long"]
    assert cohort["files"]["long"]["stats"] == "lt/tm/national/long/stats.json"
    assert set(manifest["checksums"]) == set(objects) - {"mcp-manifest.json"}


def test_firm_the_site_search_omits_is_excluded_everywhere(live_tree: Path) -> None:
    _release_id, objects = build_worker_artifacts(live_tree)
    for key, body in objects.items():
        assert b"lt-unrated-shell" not in body, key
        assert b"Unrated Shell" not in body, key


def _replace_search_index(root: Path, rows: list[dict[str, object]]) -> None:
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    manifest["global_files"]["search-index.json"] = _write_json(
        root / "search-index.json", {"data": rows, "meta": {"count": len(rows)}}
    )
    _write_json(root / "manifest.json", manifest)
    clear_caches()


def test_rated_firm_is_listed_without_an_exported_search_row(live_tree: Path) -> None:
    # Solo practices are exported to search-index.json only as the person; the
    # site still lists the firm because its profile carries a rated cohort.
    _replace_search_index(live_tree, [{"entity_type": "attorney", "entity_id": 1, "slug": "lt-example-person"}])
    _release_id, objects = build_worker_artifacts(live_tree)
    rows = json.loads(objects["search.json"])["entities"]
    assert {(row[0], row[2]) for row in rows} == {("firm", "lt-example-ip"), ("attorney", "lt-example-person")}


def test_attorneys_still_follow_the_exported_search_rows(live_tree: Path) -> None:
    _replace_search_index(live_tree, [{"entity_type": "firm", "entity_id": 1, "slug": "lt-example-ip"}])
    _release_id, objects = build_worker_artifacts(live_tree)
    rows = json.loads(objects["search.json"])["entities"]
    assert {(row[0], row[2]) for row in rows} == {("firm", "lt-example-ip")}
    assert json.loads(objects["mcp-manifest.json"])["mcp_entity_counts"] == {
        "firms": 1,
        "attorneys": 0,
        "excluded": 2,
        "folded": 0,
        "retired_aliases": 0,
    }


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

    # force rebuilds the same release under a fresh build prefix
    assert run_once(live_tree, store, settle_seconds=0, force=True) == "release-a"
    new_pointer = json.loads(store.objects[POINTER_KEY])
    assert new_pointer["prefix"] != prefix
    assert new_pointer["release_id"] == "release-a"


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


def _firm_view(store: FakeStore) -> tuple[list, dict]:
    prefix = json.loads(store.objects[POINTER_KEY])["prefix"]
    rows = json.loads(store.objects[prefix + "search.json"])["entities"]
    row = next(row for row in rows if row[0] == "firm")
    record = json.loads(store.objects[f"{prefix}entities/{row[5]}.json"])["entities"][f"firm:{row[2]}"]
    return row, record


def test_firm_takes_the_site_public_slug_and_keeps_the_export_key(live_tree: Path, tmp_path: Path) -> None:
    registry = tmp_path / "public-url-registry.json"
    _write_json(registry, {"firms": {"1": {"slug": "lt-example", "aliases": ["lt-example-ip", "1"]}}})
    store = FakeStore()
    assert run_once(live_tree, store, settle_seconds=0, public_url_registry=registry) == "release-a"

    row, record = _firm_view(store)
    assert row[2] == "lt-example-ip"  # export key: shards and cohort files still join on it
    assert row[7] == ["lt-example"]
    assert record["slug"] == "lt-example"
    assert record["profile_url"] == "https://iprate.eu/firms/lt-example/"
    prefix = json.loads(store.objects[POINTER_KEY])["prefix"]
    attorney = next(r for r in json.loads(store.objects[prefix + "search.json"])["entities"] if r[0] == "attorney")
    assert len(attorney) == 7


def test_family_slug_wins_as_on_the_site(live_tree: Path, tmp_path: Path) -> None:
    registry = tmp_path / "public-url-registry.json"
    _write_json(
        registry,
        {
            "firms": {"1": {"slug": "lt-example", "aliases": []}},
            "families": {"1": {"slug": "lt-example-group", "members": [1]}},
        },
    )
    store = FakeStore()
    run_once(live_tree, store, settle_seconds=0, public_url_registry=registry)
    row, record = _firm_view(store)
    assert row[7] == ["lt-example-group"]
    assert record["profile_url"] == "https://iprate.eu/firms/lt-example-group/"


def test_registry_change_rebuilds_the_same_release(live_tree: Path, tmp_path: Path) -> None:
    registry = tmp_path / "public-url-registry.json"
    _write_json(registry, {"firms": {"1": {"slug": "lt-example-ip", "aliases": []}}})
    store = FakeStore()
    assert run_once(live_tree, store, settle_seconds=0, public_url_registry=registry) == "release-a"
    row, record = _firm_view(store)
    assert len(row) == 7 and record["slug"] == "lt-example-ip"
    assert run_once(live_tree, store, settle_seconds=0, public_url_registry=registry) is None

    # The site build allocated a shorter slug after the export: rebuild.
    _write_json(registry, {"firms": {"1": {"slug": "lt-example", "aliases": ["lt-example-ip"]}}})
    assert run_once(live_tree, store, settle_seconds=0, public_url_registry=registry) == "release-a"
    assert _firm_view(store)[1]["slug"] == "lt-example"


def test_unreadable_registry_keeps_the_export_slug(live_tree: Path, tmp_path: Path) -> None:
    store = FakeStore()
    missing = tmp_path / "absent.json"
    assert run_once(live_tree, store, settle_seconds=0, public_url_registry=missing) == "release-a"
    row, record = _firm_view(store)
    assert len(row) == 7
    assert record["profile_url"] == "https://iprate.eu/firms/lt-example-ip/"


def _make_solo(root: Path, *, in_profile: bool) -> None:
    profile = json.loads((root / "firms" / "lt-example-ip.json").read_text(encoding="utf-8"))
    if in_profile:
        profile["solo_attorney_id"] = 1
    _write_json(root / "firms" / "lt-example-ip.json", profile)
    rows = [
        {"entity_type": "firm", "entity_id": 1, "slug": "lt-example-ip", "solo_attorney_id": None if in_profile else 1},
        {"entity_type": "attorney", "entity_id": 1, "slug": "lt-example-person"},
    ]
    _replace_search_index(root, rows)


@pytest.mark.parametrize("in_profile", [True, False], ids=["profile-link", "exported-row-link"])
def test_solo_practice_is_one_firm_and_the_person_finds_it(live_tree: Path, in_profile: bool) -> None:
    _make_solo(live_tree, in_profile=in_profile)
    _release_id, objects = build_worker_artifacts(live_tree)
    rows = json.loads(objects["search.json"])["entities"]
    assert [(row[0], row[2]) for row in rows] == [("firm", "lt-example-ip")]
    assert rows[0][7] == ["lt-example-person"]
    counts = json.loads(objects["mcp-manifest.json"])["mcp_entity_counts"]
    assert counts == {"firms": 1, "attorneys": 0, "excluded": 1, "folded": 1, "retired_aliases": 0}
    for key, body in objects.items():
        assert b"attorney:lt-example-person" not in body, key


def test_solo_fold_keeps_the_public_slug_alias(live_tree: Path, tmp_path: Path) -> None:
    _make_solo(live_tree, in_profile=True)
    registry = tmp_path / "public-url-registry.json"
    _write_json(registry, {"firms": {"1": {"slug": "lt-example", "aliases": ["lt-example-ip"]}}})
    store = FakeStore()
    run_once(live_tree, store, settle_seconds=0, public_url_registry=registry)
    row, record = _firm_view(store)
    assert row[7] == ["lt-example", "lt-example-person"]
    assert record["profile_url"] == "https://iprate.eu/firms/lt-example/"


def test_folded_person_never_takes_another_firms_own_slug(live_tree: Path) -> None:
    # Solo twin: the person's slug is the other firm's own URL slug.
    _make_solo(live_tree, in_profile=True)
    twin = json.loads((live_tree / "firms" / "lt-example-ip.json").read_text(encoding="utf-8"))
    twin.update(id=3, slug="lt-example-person", solo_attorney_id=None)
    _write_json(live_tree / "firms" / "lt-example-person.json", twin)
    manifest = json.loads((live_tree / "manifest.json").read_text(encoding="utf-8"))
    manifest["global_files"]["firms-index.json"] = _write_json(
        live_tree / "firms-index.json", {"slugs": ["lt-example-ip", "lt-example-person", "lt-unrated-shell"]}
    )
    manifest["entity_counts"]["firms"] = 3
    _write_json(live_tree / "manifest.json", manifest)
    clear_caches()
    _release_id, objects = build_worker_artifacts(live_tree, {1: "lt-example"})
    rows = {row[1]: row for row in json.loads(objects["search.json"])["entities"] if row[0] == "firm"}
    assert rows[1][7] == ["lt-example"]  # the person's slug stays with firm 3, which owns it
    assert len(rows[3]) == 7


def _declare_attorney_aliases(root: Path, aliases: dict[str, str]) -> None:
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    body = {"aliases": aliases}
    manifest["global_files"]["attorney-aliases.json"] = _write_json(root / "attorney-aliases.json", body)
    _write_json(root / "manifest.json", manifest)
    clear_caches()


def test_merged_firm_slugs_find_the_survivor(live_tree: Path, tmp_path: Path) -> None:
    registry = tmp_path / "public-url-registry.json"
    _write_json(
        registry,
        {
            "firms": {
                "1": {"slug": "lt-example", "aliases": []},
                "9": {"slug": "lt-example-old", "aliases": ["lt-example-older", "9"], "merged_into": 8},
                "8": {"slug": "lt-example-mid", "aliases": [], "merged_into": 1},
            },
            "retired_aliases": {"lt-example-retired": 8, "lt-example-person": 1, "77": 1},
        },
    )
    store = FakeStore()
    run_once(live_tree, store, settle_seconds=0, public_url_registry=registry)
    row, _record = _firm_view(store)
    # Chains end at the survivor; numeric ids and slugs another record owns are skipped.
    assert set(row[7]) == {"lt-example", "lt-example-old", "lt-example-older", "lt-example-mid", "lt-example-retired"}
    prefix = json.loads(store.objects[POINTER_KEY])["prefix"]
    assert json.loads(store.objects[prefix + "mcp-manifest.json"])["mcp_entity_counts"]["retired_aliases"] == 4


def test_merged_attorney_slugs_find_the_survivor(live_tree: Path) -> None:
    _declare_attorney_aliases(live_tree, {"lt-example-person-old": "lt-example-person", "lt-unknown-old": "lt-nobody"})
    _release_id, objects = build_worker_artifacts(live_tree)
    rows = json.loads(objects["search.json"])["entities"]
    attorney = next(row for row in rows if row[0] == "attorney")
    assert attorney[7] == ["lt-example-person-old"]


def test_merged_attorney_of_a_solo_practice_finds_the_firm(live_tree: Path) -> None:
    _make_solo(live_tree, in_profile=True)
    _declare_attorney_aliases(live_tree, {"lt-example-person-old": "lt-example-person"})
    _release_id, objects = build_worker_artifacts(live_tree)
    rows = json.loads(objects["search.json"])["entities"]
    assert [(row[0], row[7]) for row in rows] == [("firm", ["lt-example-person", "lt-example-person-old"])]
