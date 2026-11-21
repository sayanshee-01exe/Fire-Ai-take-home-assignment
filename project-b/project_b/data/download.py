"""Download NYC 311 service requests created in calendar year 2023.

Source: NYC Open Data dataset erm2-nwe9 via the SODA API (the only data source
this project uses). We pull month by month and page by page, keep every value
exactly as delivered (all strings, empty -> null, so placeholders such as "N/A"
survive for profiling), and write one parquet file sorted by created_date.
A manifest records expected vs downloaded row counts so a partial download can
never silently pass.

The download is NOT how the reported numbers are reproduced: the source is live, so a new
download differs from the snapshot they came from. With that snapshot present, this step only
verifies it (snapshot.verify); without it, it stops and says where to get it. --fresh downloads
anyway, into a separate file that replaces the snapshot only if it matches dataset_manifest.json.

Usage:  python -m project_b.data.download [--fresh]
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pandas as pd
import requests

from project_b import config

PAGE_SIZE = 50_000
MAX_TRIES = 8


def _month_windows() -> list[tuple[int, str, str]]:
    windows = []
    for m in range(1, 13):
        start = f"2023-{m:02d}-01T00:00:00"
        end = f"2023-{m + 1:02d}-01T00:00:00" if m < 12 else config.YEAR_END
        windows.append((m, start, end))
    return windows


def _get(session: requests.Session, url: str, params: dict) -> requests.Response:
    """GET with exponential backoff on throttling / transient server errors."""
    for attempt in range(MAX_TRIES):
        try:
            resp = session.get(url, params=params, timeout=300)
            if resp.status_code == 200:
                return resp
            if resp.status_code not in (429, 500, 502, 503, 504):
                resp.raise_for_status()
        except (requests.ConnectionError, requests.Timeout):
            if attempt == MAX_TRIES - 1:
                raise
        time.sleep(min(120, 2 ** attempt * 3))
    raise RuntimeError(f"giving up on {url} {params}")


def _where(start: str, end: str) -> str:
    return f"created_date >= '{start}' AND created_date < '{end}'"


def expected_count(session: requests.Session, start: str, end: str) -> int:
    resp = _get(session, config.SODA_JSON_URL, {"$select": "count(*)", "$where": _where(start, end)})
    return int(resp.json()[0]["count"])


def download_month(month: int, start: str, end: str) -> dict:
    out = config.RAW_PARTS_DIR / f"2023-{month:02d}.parquet"
    session = requests.Session()
    expected = expected_count(session, start, end)
    if out.exists():
        have = len(pd.read_parquet(out, columns=["unique_key"]))
        if have == expected:
            return {"month": month, "expected": expected, "downloaded": have, "cached": True}
    frames, offset = [], 0
    while True:
        params = {
            "$select": ",".join(config.COLUMNS),
            "$where": _where(start, end),
            "$order": "unique_key",
            "$limit": PAGE_SIZE,
            "$offset": offset,
        }
        resp = _get(session, config.SODA_CSV_URL, params)
        page = pd.read_csv(
            io.StringIO(resp.content.decode("utf-8")),
            dtype=str,
            keep_default_na=False,  # keep literal "N/A", "NA", "NULL" as data
            na_values=[""],  # only truly empty fields become null
        )
        frames.append(page)
        print(f"  month {month:02d}: offset {offset:>7,} -> {len(page):,} rows", flush=True)
        if len(page) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    df = pd.concat(frames, ignore_index=True)
    df = df.reindex(columns=config.COLUMNS)  # stable column order even if a field is absent
    tmp = out.with_suffix(".tmp")
    df.to_parquet(tmp, index=False, compression="zstd")
    tmp.replace(out)
    return {"month": month, "expected": expected, "downloaded": len(df), "cached": False}


def _sha256(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main(fresh: bool = False) -> None:
    """Verify the preserved snapshot (default), or download the live dataset (fresh=True)."""
    from project_b.data import snapshot

    if not fresh:
        manifest = snapshot.verify(config.RAW_FILE)  # missing or different: SnapshotError with instructions
        print(f"[download] {config.RAW_FILE.name} verified against {config.DATASET_MANIFEST.name} "
              f"({manifest['file']['row_count']:,} rows, sha256 {manifest['file']['sha256'][:16]}...): nothing to download")
        return
    config.RAW_PARTS_DIR.mkdir(parents=True, exist_ok=True)
    target = config.RAW_FILE.with_name(config.RAW_FILE.stem + ".fresh.parquet")
    meta = requests.get(config.SODA_META_URL, timeout=60).json()
    with ThreadPoolExecutor(max_workers=4) as pool:
        months = list(pool.map(lambda w: download_month(*w), _month_windows()))
    for m in months:
        if m["expected"] != m["downloaded"]:
            raise RuntimeError(f"month {m['month']}: expected {m['expected']} rows, got {m['downloaded']}")
    parts = [pd.read_parquet(config.RAW_PARTS_DIR / f"2023-{m:02d}.parquet") for m, _, _ in _month_windows()]
    df = pd.concat(parts, ignore_index=True)
    # Arrival order = created_date order (ISO strings sort chronologically).
    df = df.sort_values(["created_date", "unique_key"], kind="stable").reset_index(drop=True)
    df.to_parquet(target, index=False, compression="zstd")
    manifest = {
        "dataset": config.DATASET_ID,
        "dataset_name": meta.get("name"),
        "source": config.SODA_CSV_URL,
        "filter": _where(config.YEAR_START, config.YEAR_END),
        "downloaded_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_rows_updated_at_utc": datetime.fromtimestamp(meta.get("rowsUpdatedAt", 0), timezone.utc).isoformat(),
        "months": months,
        "total_rows": int(len(df)),
        "columns": list(df.columns),
        "sha256": _sha256(target),
        "note": "The source is live; rows from 2023 can still be updated, so a later re-download may differ slightly.",
    }
    target.with_suffix(".manifest.json").write_text(json.dumps(manifest, indent=2))
    print(f"[download] wrote {len(df):,} rows -> {target}")
    snapshot.verify(target)  # a different snapshot stops here; the preserved file is left untouched
    target.replace(config.RAW_FILE)
    target.with_suffix(".manifest.json").replace(config.RAW_MANIFEST)
    print(f"[download] the fresh download matches {config.DATASET_MANIFEST.name}; it is now {config.RAW_FILE}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--fresh", action="store_true", help="download the live dataset (checked against dataset_manifest.json)")
    main(fresh=ap.parse_args().fresh)
