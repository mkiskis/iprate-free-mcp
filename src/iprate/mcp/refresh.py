"""Snapshot a completed static release for the MCP and derive its catalogue.

Reads the live public export tree (static JSON only), copies exactly the
manifest-declared assets into an immutable per-release snapshot, builds the
MCP catalogue inside that snapshot, and atomically repoints the ``current``
symlink. The adapter keeps serving one internally consistent release until
the next completed one is selected; a release that changes while it is being
snapshotted is abandoned and retried on the next pass. Static files in,
static files out — no database, pipeline, model, or API import.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import time
from pathlib import Path
from typing import Any

from iprate.mcp.assets import CATALOG_PATH, CATALOG_SCHEMA_VERSION, StaticAssetError, build_catalog

SNAPSHOT_MARKER = ".iprate-mcp-snapshot.json"
CURRENT_LINK = "current"


def _log(event: str, **fields: Any) -> None:
    print(json.dumps({"event": event, **fields}, ensure_ascii=False), flush=True)


def _read_manifest_bytes(live_root: Path) -> bytes:
    try:
        return (live_root / "manifest.json").read_bytes()
    except OSError as exc:
        raise StaticAssetError(f"Cannot read live manifest: {exc}") from exc


def _parse_manifest(raw: bytes) -> dict[str, Any]:
    try:
        manifest = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StaticAssetError("Live manifest is not valid JSON") from exc
    if not isinstance(manifest, dict) or not manifest.get("release_id"):
        raise StaticAssetError("Live manifest has no release_id")
    return manifest


def _declared_files(manifest: dict[str, Any]) -> dict[str, str | None]:
    """Return {relative_path: checksum_prefix} for every manifest-declared asset."""
    files: dict[str, str | None] = {}
    for name, checksum in (manifest.get("global_files") or {}).items():
        files[str(name)] = str(checksum) if checksum else None
    for cohort in (manifest.get("cohorts") or {}).values():
        cohort_files = cohort.get("files") or {}
        if isinstance(cohort_files, dict):
            for name, checksum in cohort_files.items():
                files[str(name)] = str(checksum) if checksum else None
        else:
            for name in cohort_files:
                files[str(name)] = None
    return files


def _copy_declared_file(live_root: Path, staging: Path, relative_path: str, expected: str | None) -> None:
    relative = Path(relative_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise StaticAssetError(f"Unsafe declared asset path: {relative_path}")
    source = live_root / relative
    try:
        payload = source.read_bytes()
    except OSError as exc:
        raise StaticAssetError(f"Declared asset is unavailable: {relative_path}") from exc
    if expected:
        actual = hashlib.sha256(payload).hexdigest()[: len(expected)]
        if actual != expected:
            raise StaticAssetError(
                f"Declared asset checksum mismatch for {relative_path}: expected {expected}, got {actual}"
            )
    target = staging / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    os.chmod(target, 0o644)


def _current_marker(releases_root: Path) -> dict[str, Any] | None:
    marker = releases_root / CURRENT_LINK / SNAPSHOT_MARKER
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _snapshot_dirname(releases_root: Path, release_id: str) -> str:
    candidate = release_id
    suffix = 1
    while (releases_root / candidate).exists():
        suffix += 1
        candidate = f"{release_id}-r{suffix}"
    return candidate


def _activate(releases_root: Path, dirname: str) -> None:
    temporary = releases_root / f".{CURRENT_LINK}.tmp"
    temporary.unlink(missing_ok=True)
    os.symlink(dirname, temporary, target_is_directory=True)
    os.replace(temporary, releases_root / CURRENT_LINK)


def _prune(releases_root: Path, keep: int) -> list[str]:
    try:
        active = (releases_root / CURRENT_LINK).resolve().name
    except OSError:
        active = ""
    snapshots: list[tuple[int, Path]] = []
    for entry in releases_root.iterdir():
        if entry.name.startswith(".") or entry.name == CURRENT_LINK or not entry.is_dir():
            continue
        marker = entry / SNAPSHOT_MARKER
        if not marker.is_file():
            continue
        snapshots.append((marker.stat().st_mtime_ns, entry))
    snapshots.sort(key=lambda item: item[0], reverse=True)
    removed: list[str] = []
    for index, (_mtime, entry) in enumerate(snapshots):
        if index < keep or entry.name == active:
            continue
        shutil.rmtree(entry, ignore_errors=True)
        removed.append(entry.name)
    return removed


def snapshot_release(
    live_root: str | Path,
    releases_root: str | Path,
    *,
    keep: int = 2,
    settle_seconds: float = 5.0,
) -> str | None:
    """Create and activate one immutable snapshot of the live release.

    Returns the activated release_id, or None when the active snapshot is
    already current. Raises StaticAssetError when the live tree is missing,
    inconsistent, or changed while being copied — the previously activated
    snapshot stays in service in every failure case.
    """
    live = Path(live_root).resolve()
    releases = Path(releases_root).resolve()
    releases.mkdir(parents=True, exist_ok=True)

    initial = _read_manifest_bytes(live)
    manifest = _parse_manifest(initial)
    release_id = str(manifest["release_id"])

    marker = _current_marker(releases)
    if (
        marker is not None
        and marker.get("release_id") == release_id
        and marker.get("catalog_schema") == CATALOG_SCHEMA_VERSION
    ):
        return None

    if settle_seconds > 0:
        time.sleep(settle_seconds)
        if _read_manifest_bytes(live) != initial:
            raise StaticAssetError("Live release changed while settling; retrying later")

    declared = _declared_files(manifest)
    dirname = _snapshot_dirname(releases, release_id)
    staging = releases / f".staging-{dirname}"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    try:
        manifest_copy = staging / "manifest.json"
        manifest_copy.write_bytes(initial)
        os.chmod(manifest_copy, 0o644)
        for relative_path, expected in sorted(declared.items()):
            _copy_declared_file(live, staging, relative_path, expected)
        catalog = build_catalog(live, staging / CATALOG_PATH)
        if catalog.get("release_id") != release_id:
            raise StaticAssetError("Live release changed during catalogue build; retrying later")
        if _read_manifest_bytes(live) != initial:
            raise StaticAssetError("Live release changed while copying; retrying later")
        marker_path = staging / SNAPSHOT_MARKER
        marker_path.write_text(
            json.dumps(
                {
                    "release_id": release_id,
                    "catalog_schema": CATALOG_SCHEMA_VERSION,
                    "declared_files": len(declared),
                    "representatives": catalog.get("representative_counts") or {},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        os.chmod(marker_path, 0o644)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    staging.rename(releases / dirname)
    _activate(releases, dirname)
    removed = _prune(releases, keep)
    _log(
        "snapshot_activated",
        release_id=release_id,
        directory=dirname,
        declared_files=len(declared),
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
    parser.add_argument(
        "--releases-root",
        type=Path,
        default=Path(os.environ.get("IPRATE_MCP_RELEASES_ROOT", "/data")),
        help="Directory that holds immutable snapshots and the `current` symlink",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=float(os.environ.get("IPRATE_MCP_REFRESH_INTERVAL", "300")),
        help="Seconds between passes; 0 runs one pass and exits",
    )
    parser.add_argument("--keep", type=int, default=2, help="Completed snapshots to retain")
    parser.add_argument("--settle-seconds", type=float, default=5.0)
    args = parser.parse_args()
    while True:
        try:
            if snapshot_release(
                args.live_root,
                args.releases_root,
                keep=args.keep,
                settle_seconds=args.settle_seconds,
            ) is None:
                _log("snapshot_current")
        except StaticAssetError as exc:
            _log("snapshot_retry", reason=str(exc))
        except OSError as exc:
            _log("snapshot_error", error=type(exc).__name__, detail=str(exc))
        if args.interval <= 0:
            return
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
