"""Validated read-only access to one published IPRATE static release."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any


class StaticAssetError(RuntimeError):
    """The selected static release is missing, corrupt, or internally inconsistent."""


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


def _sha256_prefix(root: Path, relative_path: str, length: int = 8) -> str:
    target = _safe_path(root, relative_path)
    digest = hashlib.sha256()
    try:
        with target.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise StaticAssetError(f"Cannot hash static asset: {relative_path}") from exc
    return digest.hexdigest()[:length]


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
    catalog = _read_json(root, "mcp/v0.1/catalog.json")
    if not isinstance(catalog, dict):
        raise StaticAssetError("MCP catalogue is not a JSON object")
    if catalog.get("schema_version") != "0.1":
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
    top_clients = value.get("top_clients") or []
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
            for item in top_clients[:5]
            if isinstance(item, dict) and item.get("display_name")
        ],
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
        "schema_version": "0.1",
        "release_id": manifest["release_id"],
        "generated_at": manifest.get("generated_at") or manifest.get("updated_at"),
        "source_checksums": {
            relative_path: declared_checksum(manifest, relative_path) for relative_path in index_paths
        },
        "representative_counts": actual_counts,
        "representatives": representatives,
    }
    target = Path(output_path) if output_path else root / "mcp/v0.1/catalog.json"
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
