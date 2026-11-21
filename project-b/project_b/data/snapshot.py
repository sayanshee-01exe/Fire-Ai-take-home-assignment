"""The preserved raw snapshot: its committed manifest and its verification.

Every reported number comes from one download of a LIVE dataset: NYC Open Data keeps updating
2023 rows (late closures, status changes), so a later download differs. Reproducing the numbers
therefore needs that exact file, not "the same query". dataset_manifest.json (committed, at the
repository root) describes it: source, query, download time, file name, size, row count, schema
and SHA-256. verify() runs before any step reads the raw data (load.load_raw,
evaluate.ensure_derived, download.main) and stops the run when the file is missing or is not that
snapshot, instead of silently using different data.

The raw file itself (~150 MB) stays out of git (/data/ in .gitignore). README.md, section
"Dataset snapshot", says how a reviewer gets the exact file.

Usage:
    python -m project_b.data.snapshot                    verify data/raw/nyc311_2023.parquet
    python -m project_b.data.snapshot --write-manifest   describe the CURRENT file in dataset_manifest.json
                                                         (only when deliberately adopting a new snapshot)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

from project_b import config

ALLOW_ENV = "PROJECT_B_ALLOW_NEW_SNAPSHOT"  # "1": run on a non-matching file anyway, with a loud warning
_verified: dict[tuple, str] = {}  # (path, size, mtime) -> sha256, so one process hashes the file once


class SnapshotError(RuntimeError):
    """The raw snapshot is missing or is not the one dataset_manifest.json describes."""


def load_manifest(path: Path | None = None) -> dict:
    path = path or config.DATASET_MANIFEST
    if not path.exists():
        raise SnapshotError(f"{path.name} is missing: it is part of the repository and describes the snapshot every "
                            "number was computed from (restore it from git)")
    return json.loads(path.read_text(encoding="utf-8"))


def sha256(path: Path) -> str:
    stat = path.stat()
    key = (str(path.resolve()), stat.st_size, stat.st_mtime_ns)
    if key not in _verified:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        _verified[key] = h.hexdigest()
    return _verified[key]


def _how_to_obtain(manifest: dict) -> str:
    return " ".join(manifest.get("how_to_obtain") or [])


def verify(path: Path | None = None, manifest: dict | None = None) -> dict:
    """Check that `path` (default config.RAW_FILE) is the snapshot in dataset_manifest.json; return
    the manifest. Missing file, or a different file: SnapshotError (with ALLOW_ENV=1 a different
    file only warns, for someone who knowingly runs on new data)."""
    path = path or config.RAW_FILE
    manifest = manifest or load_manifest()
    expected = manifest["file"]
    if not path.exists():
        raise SnapshotError(f"the raw snapshot {expected['path']} is missing. {_how_to_obtain(manifest)}")
    size, digest = path.stat().st_size, sha256(path)
    if digest == expected["sha256"] and size == expected["size_bytes"]:
        return manifest
    problem = (f"{path.name} is not the snapshot the results were computed from: sha256 {digest[:16]}... "
               f"({size:,} bytes) instead of {expected['sha256'][:16]}... ({expected['size_bytes']:,} bytes) in "
               f"{config.DATASET_MANIFEST.name}. Every reported number depends on the exact snapshot.")
    if os.environ.get(ALLOW_ENV) == "1":
        print(f"WARNING: {problem} Continuing because {ALLOW_ENV}=1: results will differ from the reported ones "
              "and must not replace them.", file=sys.stderr, flush=True)
        return manifest
    raise SnapshotError(f"{problem} {_how_to_obtain(manifest)} To run on this file anyway, knowingly, set {ALLOW_ENV}=1.")


def build_manifest(path: Path | None = None, download_manifest: Path | None = None) -> dict:
    """dataset_manifest.json's content for the file at `path`, with the download facts recorded by
    download.py (data/raw/manifest.json: query, time, rows per month)."""
    import pyarrow.parquet as pq

    from project_b.data import download

    path = path or config.RAW_FILE
    dl = json.loads((download_manifest or config.RAW_MANIFEST).read_text(encoding="utf-8"))
    meta, schema = pq.read_metadata(path), pq.read_schema(path)
    pandas_meta = json.loads((schema.metadata or {}).get(b"pandas", b"{}"))
    return {
        "description": "The NYC 311 snapshot every number in this repository was computed from. Verified by "
                       "project_b/data/snapshot.py before any step reads it.",
        "dataset": dl["dataset"],
        "dataset_name": dl["dataset_name"],
        "publisher": "NYC Open Data (City of New York)",
        "landing_page": f"https://data.cityofnewyork.us/d/{dl['dataset']}",
        "source": config.SODA_CSV_URL,
        "api": {
            "rows_endpoint": config.SODA_CSV_URL,
            "count_endpoint": config.SODA_JSON_URL,
            "metadata_endpoint": config.SODA_META_URL,
            "request": "one query per created month, paged until a page is shorter than $limit",
            "parameters": {"$select": ",".join(config.COLUMNS),
                           "$where": "created_date >= '<month start>' AND created_date < '<next month start>'",
                           "$order": "unique_key", "$limit": download.PAGE_SIZE, "$offset": "0, 50000, 100000, ..."},
            "parsing": "CSV read with every value as a string; only empty fields become null (keep_default_na=False)",
        },
        "filter": dl["filter"],
        "date_filter": {"column": "created_date", "from_inclusive": config.YEAR_START, "to_exclusive": config.YEAR_END},
        "downloaded_at_utc": dl["downloaded_at_utc"],
        "source_rows_updated_at_utc": dl["source_rows_updated_at_utc"],
        "months": dl["months"],
        "total_rows": dl["total_rows"],
        "columns": dl["columns"],
        "sha256": sha256(path),
        "file": {
            "path": path.resolve().relative_to(config.ROOT).as_posix(),
            "filename": path.name,
            "size_bytes": path.stat().st_size,
            "sha256": sha256(path),
            "row_count": meta.num_rows,
            "row_groups": meta.num_row_groups,
            "format": "Apache Parquet, zstd compression",
            "written_by": f"{meta.created_by}; pandas {pandas_meta.get('pandas_version', '?')}",
            "row_order": "created_date, then unique_key",
        },
        "schema": [{"name": field.name, "type": str(field.type), "nullable": field.nullable} for field in schema],
        "how_to_obtain": [
            "Get the exact file from the repository's release asset (README.md, section 'Dataset snapshot'), e.g.",
            f"`gh release download data-snapshot-2023 --pattern {path.name} --dir data/raw`, then run again.",
            "`python -m project_b --download-fresh` downloads the live dataset from NYC Open Data instead;",
            "it is checked against this manifest too and will most likely differ, because the source is updated daily.",
        ],
        "note": dl.get("note"),
    }


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m project_b.data.snapshot", description=__doc__.splitlines()[0])
    ap.add_argument("--write-manifest", action="store_true", help="write dataset_manifest.json for the current file")
    args = ap.parse_args(argv)
    if args.write_manifest:
        manifest = build_manifest()
        config.DATASET_MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        print(f"[snapshot] wrote {config.DATASET_MANIFEST.name}: {manifest['file']['row_count']:,} rows, "
              f"sha256 {manifest['file']['sha256'][:16]}...")
        return
    manifest = verify()
    print(f"[snapshot] {manifest['file']['path']} verified: {manifest['file']['row_count']:,} rows, "
          f"{manifest['file']['size_bytes']:,} bytes, sha256 {manifest['file']['sha256'][:16]}...")


if __name__ == "__main__":
    main()
