"""Build one immutable Worker release in R2 from the completed static tree.

Reads the live public export tree (static JSON only), derives the compact
search index and bounded entity shards the Cloudflare Worker serves, copies
the released cohort statistics byte-for-byte (checksum-verified against the
release manifest), uploads everything under an immutable
``releases/{release_id}/{build_id}/`` prefix, and atomically activates it by
rewriting the single ``current.json`` pointer object. A release that changes
while it is being built is abandoned and retried on the next cron pass.
Static files in, static objects out — no database, pipeline, model, or API
import.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Protocol

from iprate.mcp.assets import (
    StaticAssetError,
    _cohort_entry,
    _read_json,
    _validate_declared_asset,
    declared_checksum,
    normalise_text,
)
from iprate.mcp.service import _cohort_priority, _profile_url, _public_cohort, _safe_text

WORKER_SCHEMA = 1
POINTER_KEY = "current.json"
RELEASES_PREFIX = "releases/"


class ObjectStore(Protocol):
    """Minimal object-store surface the builder needs (R2 in production)."""

    def get(self, key: str) -> bytes | None: ...

    def put(self, key: str, body: bytes, content_type: str = "application/json") -> None: ...

    def list_keys(self, prefix: str) -> list[str]: ...

    def delete(self, key: str) -> None: ...


class R2Client:
    """S3-API client for one R2 bucket, configured from the environment."""

    def __init__(self) -> None:
        import boto3  # deferred so the server install never needs it

        endpoint = os.environ["IPRATE_MCP_R2_ENDPOINT"]
        self.bucket = os.environ["IPRATE_MCP_R2_BUCKET"]
        self._s3 = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=os.environ["IPRATE_MCP_R2_ACCESS_KEY_ID"],
            aws_secret_access_key=os.environ["IPRATE_MCP_R2_SECRET_ACCESS_KEY"],
            region_name="auto",
        )

    def get(self, key: str) -> bytes | None:
        try:
            return self._s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        except self._s3.exceptions.NoSuchKey:
            return None
        except self._s3.exceptions.ClientError as exc:  # pragma: no cover - defensive
            if exc.response.get("Error", {}).get("Code") in {"NoSuchKey", "404"}:
                return None
            raise

    def put(self, key: str, body: bytes, content_type: str = "application/json") -> None:
        self._s3.put_object(Bucket=self.bucket, Key=key, Body=body, ContentType=content_type)

    def list_keys(self, prefix: str) -> list[str]:
        keys: list[str] = []
        token: str | None = None
        while True:
            kwargs: dict[str, Any] = {"Bucket": self.bucket, "Prefix": prefix, "MaxKeys": 1000}
            if token:
                kwargs["ContinuationToken"] = token
            page = self._s3.list_objects_v2(**kwargs)
            keys.extend(item["Key"] for item in page.get("Contents") or [])
            if not page.get("IsTruncated"):
                return keys
            token = page.get("NextContinuationToken")

    def delete(self, key: str) -> None:
        self._s3.delete_object(Bucket=self.bucket, Key=key)


def _log(event: str, **fields: Any) -> None:
    print(json.dumps({"event": event, **fields}, ensure_ascii=False), flush=True)


def _dump(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _read_manifest_bytes(live_root: Path) -> bytes:
    try:
        return (live_root / "manifest.json").read_bytes()
    except OSError as exc:
        raise StaticAssetError(f"Cannot read live manifest: {exc}") from exc


def _validated_bytes(live_root: Path, manifest: dict[str, Any], relative_path: str) -> bytes:
    expected = declared_checksum(manifest, relative_path)
    if expected is None:
        raise StaticAssetError(f"Static asset is not declared by the release: {relative_path}")
    try:
        payload = (live_root / relative_path).read_bytes()
    except OSError as exc:
        raise StaticAssetError(f"Declared asset is unavailable: {relative_path}") from exc
    actual = hashlib.sha256(payload).hexdigest()[: len(expected)]
    if actual != expected:
        raise StaticAssetError(
            f"Declared asset checksum mismatch for {relative_path}: expected {expected}, got {actual}"
        )
    return payload


def _search_allowlist(live_root: Path, manifest: dict[str, Any]) -> set[tuple[str, int]]:
    """Published-search allowlist: the same exclusions the public site search applies."""
    payload = json.loads(_validated_bytes(live_root, manifest, "search-index.json"))
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise StaticAssetError("search-index.json has no data array")
    allowed: set[tuple[str, int]] = set()
    for row in rows:
        if isinstance(row, dict) and row.get("entity_type") and row.get("entity_id") is not None:
            allowed.add((str(row["entity_type"]), int(row["entity_id"])))
    if not allowed:
        raise StaticAssetError("search-index.json allowlist is empty")
    return allowed


def _shard_id(entity_type: str, slug: str) -> str:
    return hashlib.sha256(f"{entity_type}:{slug}".encode()).hexdigest()[:2]


def _normalise_class(value: Any) -> str:
    return str(value).strip().lstrip("0") or "0"


def _scan_cohort(cohort: dict[str, Any]) -> list[Any]:
    return [
        cohort.get("jurisdiction"),
        cohort.get("right_type"),
        cohort.get("tier"),
        cohort.get("window"),
        cohort.get("publication_rank"),
        cohort.get("score"),
        [_normalise_class(item.get("class_code")) for item in cohort.get("top_classes") or []][:5],
        (cohort.get("client_keys") or [])[:5],
    ]


def _entity_records(
    live_root: Path,
    manifest: dict[str, Any],
    allowed: set[tuple[str, int]],
) -> tuple[list[list[Any]], dict[str, dict[str, Any]], dict[str, int]]:
    scan_rows: list[list[Any]] = []
    shard_records: dict[str, dict[str, Any]] = {}
    counts = {"firms": 0, "attorneys": 0, "excluded": 0}
    expected_counts = manifest.get("entity_counts") or {}
    for entity_type, index_path, plural in (
        ("firm", "firms-index.json", "firms"),
        ("attorney", "attorneys-index.json", "attorneys"),
    ):
        type_total = 0
        _validate_declared_asset(live_root, manifest, index_path)
        index = _read_json(live_root, index_path)
        slugs = index.get("slugs") if isinstance(index, dict) else None
        if not isinstance(slugs, list):
            raise StaticAssetError(f"{index_path} has no slugs array")
        for slug in slugs:
            if not isinstance(slug, str) or not slug:
                raise StaticAssetError(f"{index_path} contains an invalid slug")
            type_total += 1
            relative_path = f"{plural}/{slug}.json"
            profile = _read_json(live_root, relative_path)
            if not isinstance(profile, dict):
                raise StaticAssetError(f"Profile asset is not an object: {relative_path}")
            entity_id = profile.get("id")
            if entity_id is None or (str(entity_type), int(entity_id)) not in allowed:
                counts["excluded"] += 1
                continue
            cohorts = [
                entry
                for key, value in (profile.get("cohorts") or {}).items()
                if (entry := _cohort_entry(str(key), value)) is not None
            ]
            cohorts.sort(key=_cohort_priority)
            shard = _shard_id(entity_type, slug)
            name_key = normalise_text(str(profile.get("name") or ""))
            scan_rows.append(
                [
                    entity_type,
                    int(entity_id),
                    slug,
                    name_key,
                    profile.get("country_code"),
                    shard,
                    [_scan_cohort(cohort) for cohort in cohorts],
                ]
            )
            record_key = f"{entity_type}:{slug.casefold()}"
            shard_records.setdefault(shard, {})[record_key] = {
                "representative_type": entity_type,
                "representative_id": int(entity_id),
                "quoted_name": _safe_text(profile.get("name")),
                "slug": slug,
                "home_country_code": profile.get("country_code"),
                "city": _safe_text(profile.get("city"), limit=256),
                "cohorts": [_public_cohort(cohort) for cohort in cohorts],
                "profile_url": _profile_url(entity_type, slug),
                "text_provenance": "quoted_untrusted_register_data",
            }
            counts[plural] += 1
        expected = expected_counts.get(plural)
        if expected is not None and int(expected) != type_total:
            raise StaticAssetError(f"Static {plural} count mismatch: expected {expected}, got {type_total}")
    return scan_rows, shard_records, counts


def _cohort_windows(files: Any) -> dict[str, dict[str, str]]:
    """Map window -> {stats: path, firms: path} from a manifest cohort file dict."""
    names = files.keys() if isinstance(files, dict) else (files or [])
    windows: dict[str, dict[str, str]] = {}
    for path in names:
        window = "recent" if "/emerging/" in path else "long"
        if path.endswith("/stats.json"):
            windows.setdefault(window, {})["stats"] = path
        elif path.endswith("/firms.json"):
            windows.setdefault(window, {})["firms"] = path
    return {window: paths for window, paths in windows.items() if "stats" in paths}


def build_worker_artifacts(live_root: str | Path) -> tuple[str, dict[str, bytes]]:
    """Return (release_id, {relative object key: body}) for one consistent release.

    Keys are relative to the ``releases/{release_id}/{build_id}/`` prefix;
    ``mcp-manifest.json`` must be uploaded last within the prefix.
    """
    live = Path(live_root).resolve()
    manifest = _read_json(live, "manifest.json")
    if not isinstance(manifest, dict) or not manifest.get("release_id"):
        raise StaticAssetError("manifest.json has no release_id")
    release_id = str(manifest["release_id"])

    allowed = _search_allowlist(live, manifest)
    scan_rows, shard_records, counts = _entity_records(live, manifest, allowed)

    objects: dict[str, bytes] = {}
    checksums: dict[str, str] = {}

    def add(key: str, body: bytes) -> None:
        objects[key] = body
        checksums[key] = hashlib.sha256(body).hexdigest()[:16]

    add("search.json", _dump({"schema": WORKER_SCHEMA, "release_id": release_id, "entities": scan_rows}))
    for shard, records in sorted(shard_records.items()):
        add(f"entities/{shard}.json", _dump({"release_id": release_id, "entities": records}))

    cohort_meta: dict[str, Any] = {}
    for cohort_key, cohort in (manifest.get("cohorts") or {}).items():
        if not isinstance(cohort, dict):
            continue
        windows = _cohort_windows(cohort.get("files"))
        if not windows:
            continue
        files_meta: dict[str, dict[str, str]] = {}
        for window, paths in windows.items():
            entry: dict[str, str] = {}
            for kind, relative_path in paths.items():
                asset_key = f"assets/{relative_path}"
                if asset_key not in objects:
                    add(asset_key, _validated_bytes(live, manifest, relative_path))
                entry[kind] = relative_path
            files_meta[window] = entry
        cohort_meta[str(cohort_key)] = {
            "run_id": cohort.get("run_id"),
            "published_at": cohort.get("published_at"),
            "degraded_reasons": cohort.get("degraded_reasons") or [],
            "windows": sorted(files_meta),
            "files": files_meta,
        }

    add("assets/analytics_stats.json", _validated_bytes(live, manifest, "analytics_stats.json"))

    add(
        "mcp-manifest.json",
        _dump(
            {
                "worker_schema": WORKER_SCHEMA,
                "release_id": release_id,
                "generated_at": manifest.get("generated_at") or manifest.get("updated_at"),
                "status": manifest.get("status"),
                "degraded_reasons": manifest.get("degraded_reasons") or [],
                "entity_counts": manifest.get("entity_counts") or {},
                "mcp_entity_counts": counts,
                "cohorts": cohort_meta,
                "checksums": checksums,
            }
        ),
    )
    return release_id, objects


def _pointer(store: ObjectStore) -> dict[str, Any] | None:
    raw = store.get(POINTER_KEY)
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _prune(store: ObjectStore, keep_prefixes: set[str]) -> list[str]:
    """Delete release build prefixes that are neither current nor the newest spare."""
    grouped: dict[str, list[str]] = {}
    for key in store.list_keys(RELEASES_PREFIX):
        parts = key.split("/")
        if len(parts) >= 4:
            grouped.setdefault("/".join(parts[:3]) + "/", []).append(key)
    complete = sorted(
        (prefix for prefix, keys in grouped.items() if f"{prefix}mcp-manifest.json" in keys),
        reverse=True,
    )
    for prefix in complete:
        if len(keep_prefixes) >= 2:
            break
        keep_prefixes.add(prefix)
    removed: list[str] = []
    for prefix, keys in grouped.items():
        if prefix in keep_prefixes:
            continue
        for key in keys:
            store.delete(key)
        removed.append(prefix)
    return removed


def run_once(
    live_root: str | Path,
    store: ObjectStore,
    *,
    settle_seconds: float = 5.0,
    force: bool = False,
) -> str | None:
    """Build, upload, and activate one release. Silent no-op when current.

    Returns the activated release_id, or None when the pointer already names
    this release at the current worker schema (``force`` rebuilds anyway —
    for entity re-exports that regenerate assets without a new release_id).
    Raises StaticAssetError when the live tree is missing, inconsistent, or
    changed while being built — the previously activated release keeps
    serving in every failure case.
    """
    live = Path(live_root).resolve()
    initial = _read_manifest_bytes(live)
    try:
        release_id = str(json.loads(initial)["release_id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise StaticAssetError("Live manifest has no release_id") from exc

    pointer = _pointer(store)
    if (
        not force
        and pointer is not None
        and pointer.get("release_id") == release_id
        and pointer.get("worker_schema") == WORKER_SCHEMA
    ):
        return None

    if settle_seconds > 0:
        time.sleep(settle_seconds)
        if _read_manifest_bytes(live) != initial:
            raise StaticAssetError("Live release changed while settling; retrying later")

    built_release, objects = build_worker_artifacts(live)
    if built_release != release_id or _read_manifest_bytes(live) != initial:
        raise StaticAssetError("Live release changed during build; retrying later")

    build_id = uuid.uuid4().hex[:8]
    prefix = f"{RELEASES_PREFIX}{release_id}/{build_id}/"
    manifest_body = objects.pop("mcp-manifest.json")
    for key in sorted(objects):
        store.put(prefix + key, objects[key])
    store.put(prefix + "mcp-manifest.json", manifest_body)
    store.put(
        POINTER_KEY,
        _dump(
            {
                "release_id": release_id,
                "worker_schema": WORKER_SCHEMA,
                "build_id": build_id,
                "prefix": prefix,
            }
        ),
    )
    removed = _prune(store, {prefix})
    _log(
        "worker_release_activated",
        release_id=release_id,
        build_id=build_id,
        objects=len(objects) + 1,
        pruned=removed,
    )
    return release_id


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--live-root",
        type=Path,
        default=Path(os.environ.get("IPRATE_MCP_LIVE_ROOT", "/live/data/v1")),
        help="Completed live /data/v1 export tree (read-only input)",
    )
    parser.add_argument("--settle-seconds", type=float, default=5.0)
    parser.add_argument(
        "--force",
        action="store_true",
        help="Rebuild even when the pointer already names the live release_id",
    )
    args = parser.parse_args()
    try:
        run_once(args.live_root, R2Client(), settle_seconds=args.settle_seconds, force=args.force)
    except StaticAssetError as exc:
        _log("worker_release_retry", reason=str(exc))
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
