"""Validated read-only access to one published IPRATE static release."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

CATALOG_PATH = "mcp/v0.1/catalog.json"
CATALOG_SCHEMA_VERSION = "0.2"


class StaticAssetError(RuntimeError):
    """The selected static release is missing, corrupt, or internally inconsistent."""


def normalise_text(value: str) -> str:
    """Casefolded, accent-stripped, token-joined key for matching released text."""
    decomposed = unicodedata.normalize("NFKD", value)
    without_marks = "".join(char for char in decomposed if not unicodedata.combining(char))
    return " ".join(re.findall(r"[\w]+", without_marks.casefold(), flags=re.UNICODE))


def asset_root(path: str | Path | None = None) -> Path:
    """Return the configured read-only `/data/v1` release directory."""
    configured = path or os.environ.get("IPRATE_MCP_ASSET_ROOT", "/data/v1")
    return Path(configured).resolve()


def _safe_path(root: Path, relative_path: str) -> Path:
    relative = Path(relative_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise StaticAssetError(f"Unsafe static asset path: {relative_path}")
    target = (root / relative).resolve()
    if target != root and root not in target.parents:
        raise StaticAssetError(f"Static asset escapes release root: {relative_path}")
    return target


@lru_cache(maxsize=16)
def _cached_json(path: str, modified_ns: int, size: int) -> Any:
    del modified_ns, size
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StaticAssetError(f"Cannot read static JSON asset: {path}") from exc


def _read_json(root: Path, relative_path: str) -> Any:
    target = _safe_path(root, relative_path)
    try:
        stat = target.stat()
    except OSError as exc:
        raise StaticAssetError(f"Required static asset is unavailable: {relative_path}") from exc
    if not target.is_file():
        raise StaticAssetError(f"Required static asset is not a file: {relative_path}")
    return _cached_json(str(target), stat.st_mtime_ns, stat.st_size)


_catalog_lock = threading.Lock()
_catalog_slot: tuple[tuple[str, int, int], Any] | None = None


def _read_catalog_json(root: Path) -> Any:
    """Read the MCP catalogue through a dedicated single-entry cache.

    The parsed catalogue holds every released representative — hundreds of
    megabytes at production scale. The shared lru cache would keep a
    superseded release's parse alive next to its replacement and overshoot
    the container memory limit, so the catalogue gets exactly one slot and
    the old parse is dropped before the new one is loaded.
    """
    global _catalog_slot
    target = _safe_path(root, CATALOG_PATH)
    try:
        stat = target.stat()
    except OSError as exc:
        raise StaticAssetError(f"Required static asset is unavailable: {CATALOG_PATH}") from exc
    key = (str(target), stat.st_mtime_ns, stat.st_size)
    with _catalog_lock:
        if _catalog_slot is not None and _catalog_slot[0] == key:
            return _catalog_slot[1]
        _catalog_slot = None
        try:
            parsed = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise StaticAssetError(f"Cannot read static JSON asset: {CATALOG_PATH}") from exc
        _catalog_slot = (key, parsed)
        return parsed


def clear_caches() -> None:
    """Drop every cached parse: small assets, file hashes, and the catalogue slot."""
    global _catalog_slot
    _cached_json.cache_clear()
    _hash_file.cache_clear()
    with _catalog_lock:
        _catalog_slot = None


@lru_cache(maxsize=256)
def _hash_file(path: str, modified_ns: int, size: int) -> str:
    del modified_ns, size
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise StaticAssetError(f"Cannot hash static asset: {path}") from exc
    return digest.hexdigest()


def _sha256_prefix(root: Path, relative_path: str, length: int = 8) -> str:
    target = _safe_path(root, relative_path)
    try:
        stat = target.stat()
    except OSError as exc:
        raise StaticAssetError(f"Cannot hash static asset: {relative_path}") from exc
    return _hash_file(str(target), stat.st_mtime_ns, stat.st_size)[:length]


def declared_checksum(manifest: dict[str, Any], relative_path: str) -> str | None:
    """Return the release-declared checksum for a global or cohort asset."""
    global_files = manifest.get("global_files") or {}
    if relative_path in global_files:
        return str(global_files[relative_path])
    for cohort in (manifest.get("cohorts") or {}).values():
        files = cohort.get("files") or {}
        if isinstance(files, dict) and relative_path in files:
            return str(files[relative_path])
    return None


def _validate_declared_asset(root: Path, manifest: dict[str, Any], relative_path: str) -> None:
    expected = declared_checksum(manifest, relative_path)
    if expected is None:
        raise StaticAssetError(f"Static asset is not declared by the release: {relative_path}")
    actual = _sha256_prefix(root, relative_path, len(expected))
    if actual != expected:
        raise StaticAssetError(
            f"Static asset checksum mismatch for {relative_path}: expected {expected}, got {actual}"
        )


@dataclass(frozen=True)
class ReleaseSnapshot:
    root: Path
    manifest: dict[str, Any]
    catalog: dict[str, Any]

    @property
    def release_id(self) -> str:
        return str(self.manifest["release_id"])

    @property
    def as_of(self) -> str | None:
        value = self.manifest.get("generated_at") or self.manifest.get("updated_at")
        return str(value) if value else None

    def read_json(self, relative_path: str, *, verify_declared: bool = False) -> Any:
        if verify_declared:
            _validate_declared_asset(self.root, self.manifest, relative_path)
        return _read_json(self.root, relative_path)


def load_release(path: str | Path | None = None) -> ReleaseSnapshot:
    """Load one internally consistent static release and its MCP catalogue."""
    root = asset_root(path)
    manifest = _read_json(root, "manifest.json")
    if not isinstance(manifest, dict) or not manifest.get("release_id"):
        raise StaticAssetError("manifest.json has no release_id")
    catalog = _read_catalog_json(root)
    if not isinstance(catalog, dict):
        raise StaticAssetError("MCP catalogue is not a JSON object")
    if catalog.get("schema_version") != CATALOG_SCHEMA_VERSION:
        raise StaticAssetError("Unsupported MCP catalogue schema")
    if catalog.get("release_id") != manifest.get("release_id"):
        raise StaticAssetError("MCP catalogue and manifest belong to different releases")

    for relative_path, expected in (catalog.get("source_checksums") or {}).items():
        current = declared_checksum(manifest, relative_path)
        if current != expected:
            raise StaticAssetError(f"MCP catalogue source changed: {relative_path}")
        _validate_declared_asset(root, manifest, relative_path)
    return ReleaseSnapshot(root=root, manifest=manifest, catalog=catalog)


def _cohort_entry(key: str, value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    parts = key.split("|")
    if len(parts) != 4:
        return None
    country, right_type, tier, window = parts
    scores = value.get("scores") or {}
    if not isinstance(scores, dict) or scores.get("score_tier") is None:
        return None
    denominators = scores.get("denominators") or {}
    top_classes = value.get("top_classes") or []
    clients = [
        item
        for item in (value.get("top_clients") or [])[:5]
        if isinstance(item, dict) and item.get("display_name")
    ]
    return {
        "jurisdiction": country.upper(),
        "right_type": right_type,
        "tier": tier,
        "window": "recent" if window in {"recent", "emerging"} else "long",
        "score": scores.get("aggregate_score"),
        "score_tier": scores.get("score_tier"),
        "confidence_grade": scores.get("confidence_grade"),
        "publication_rank": scores.get("publication_rank"),
        "case_units": denominators.get("case_units"),
        "volume_per_year": scores.get("volume_per_year"),
        "registration_rate": scores.get("registration_rate"),
        "time_to_grant_days": scores.get("time_to_grant_days"),
        "total_firms_filing": value.get("total_firms_filing"),
        "top_classes": [
            {
                "class_code": item.get("class_code"),
                "class_label": item.get("class_label"),
                "share_pct": item.get("share_pct"),
                "rank": item.get("rank"),
            }
            for item in top_classes[:10]
            if isinstance(item, dict) and item.get("class_code")
        ],
        "top_clients": [
            {
                "display_name": item.get("display_name"),
                "share_pct": item.get("share_pct"),
                "rank": item.get("rank"),
            }
            for item in clients
        ],
        "client_keys": [normalise_text(str(item.get("display_name") or "")) for item in clients],
    }


def _catalog_representative(root: Path, entity_type: str, slug: str) -> dict[str, Any]:
    relative_path = f"{'firms' if entity_type == 'firm' else 'attorneys'}/{slug}.json"
    profile = _read_json(root, relative_path)
    if not isinstance(profile, dict):
        raise StaticAssetError(f"Profile asset is not an object: {relative_path}")
    cohorts = [
        entry
        for key, value in (profile.get("cohorts") or {}).items()
        if (entry := _cohort_entry(str(key), value)) is not None
    ]
    return {
        "representative_type": entity_type,
        "representative_id": profile.get("id"),
        "name": profile.get("name"),
        "name_key": normalise_text(str(profile.get("name") or "")),
        "slug": profile.get("slug") or slug,
        "home_country_code": profile.get("country_code"),
        "city": profile.get("city"),
        "profile_asset": relative_path,
        "cohorts": cohorts,
    }


def build_catalog(
    release_root: str | Path,
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    """Build the MCP search catalogue exclusively from completed static JSON assets."""
    root = asset_root(release_root)
    manifest = _read_json(root, "manifest.json")
    if not isinstance(manifest, dict) or not manifest.get("release_id"):
        raise StaticAssetError("manifest.json has no release_id")

    index_paths = ("firms-index.json", "attorneys-index.json")
    for relative_path in index_paths:
        _validate_declared_asset(root, manifest, relative_path)

    representatives: list[dict[str, Any]] = []
    for entity_type, index_path in (("firm", index_paths[0]), ("attorney", index_paths[1])):
        index = _read_json(root, index_path)
        slugs = index.get("slugs") if isinstance(index, dict) else None
        if not isinstance(slugs, list):
            raise StaticAssetError(f"{index_path} has no slugs array")
        for slug in slugs:
            if not isinstance(slug, str) or not slug:
                raise StaticAssetError(f"{index_path} contains an invalid slug")
            representatives.append(_catalog_representative(root, entity_type, slug))

    expected_counts = manifest.get("entity_counts") or {}
    actual_counts = {
        "firms": sum(item["representative_type"] == "firm" for item in representatives),
        "attorneys": sum(item["representative_type"] == "attorney" for item in representatives),
    }
    for key, actual in actual_counts.items():
        expected = expected_counts.get(key)
        if expected is not None and int(expected) != actual:
            raise StaticAssetError(f"Static {key} count mismatch: expected {expected}, got {actual}")

    catalog = {
        "schema_version": CATALOG_SCHEMA_VERSION,
        "release_id": manifest["release_id"],
        "generated_at": manifest.get("generated_at") or manifest.get("updated_at"),
        "source_checksums": {
            relative_path: declared_checksum(manifest, relative_path) for relative_path in index_paths
        },
        "representative_counts": actual_counts,
        "representatives": representatives,
    }
    target = Path(output_path) if output_path else root / CATALOG_PATH
    target = target.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o644)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(catalog, handle, ensure_ascii=False, separators=(",", ":"))
        os.replace(temporary, target)
    except Exception:
        Path(temporary).unlink(missing_ok=True)
        raise
    return catalog
