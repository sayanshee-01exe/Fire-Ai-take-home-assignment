"""Tests for project_b/data/snapshot.py: the run must stop on a missing or different raw snapshot
instead of silently using other data (no network: files live in tmp_path)."""
from __future__ import annotations

import json

import pandas as pd
import pytest

from project_b import config
from project_b.data import download, load, snapshot


@pytest.fixture()
def snap(tmp_path, monkeypatch):
    """A tiny raw parquet, its download manifest, and a dataset_manifest.json describing it."""
    raw_dir = tmp_path / "data" / "raw"
    raw_dir.mkdir(parents=True)
    raw = raw_dir / "nyc311_2023.parquet"
    pd.DataFrame({"unique_key": ["1", "2"], "created_date": ["2023-01-01T00:00:00.000", "2023-01-02T00:00:00.000"]}).to_parquet(raw, index=False)
    (raw_dir / "manifest.json").write_text(json.dumps({
        "dataset": "erm2-nwe9", "dataset_name": "311", "filter": "f", "downloaded_at_utc": "t", "source_rows_updated_at_utc": "u",
        "months": [], "total_rows": 2, "columns": ["unique_key", "created_date"], "note": None}))
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setattr(config, "RAW_FILE", raw)
    monkeypatch.setattr(config, "RAW_MANIFEST", raw_dir / "manifest.json")
    monkeypatch.setattr(config, "DATASET_MANIFEST", tmp_path / "dataset_manifest.json")
    monkeypatch.delenv(snapshot.ALLOW_ENV, raising=False)
    snapshot._verified.clear()
    snapshot.main(["--write-manifest"])
    return raw


def test_the_manifest_describes_the_file(snap):
    m = snapshot.load_manifest()
    assert m["file"]["path"] == "data/raw/nyc311_2023.parquet" and m["file"]["row_count"] == 2
    assert m["file"]["size_bytes"] == snap.stat().st_size and m["file"]["sha256"] == m["sha256"] == snapshot.sha256(snap)
    assert [f["name"] for f in m["schema"]] == ["unique_key", "created_date"]
    assert m["date_filter"]["from_inclusive"] == config.YEAR_START and m["api"]["rows_endpoint"] == config.SODA_CSV_URL
    assert snapshot.verify() is not None and len(load.load_raw()) == 2  # the loader reads a verified file


def test_a_missing_snapshot_stops_with_instructions_and_downloads_nothing(snap, monkeypatch):
    snap.unlink()
    monkeypatch.setattr(download.requests, "get", lambda *a, **k: pytest.fail("must not download"))
    with pytest.raises(snapshot.SnapshotError, match="missing.*release"):
        download.main()
    with pytest.raises(snapshot.SnapshotError, match="missing"):
        load.load_raw()


def test_a_different_snapshot_stops_the_loader(snap):
    pd.DataFrame({"unique_key": ["1", "3"], "created_date": ["2023-01-01T00:00:00.000", "2023-01-03T00:00:00.000"]}).to_parquet(snap, index=False)
    with pytest.raises(snapshot.SnapshotError, match="not the snapshot the results were computed from"):
        load.load_raw()
    with pytest.raises(snapshot.SnapshotError):
        download.main()


def test_running_on_a_different_snapshot_needs_an_explicit_opt_in(snap, monkeypatch, capsys):
    pd.DataFrame({"unique_key": ["9"], "created_date": ["2023-05-01T00:00:00.000"]}).to_parquet(snap, index=False)
    monkeypatch.setenv(snapshot.ALLOW_ENV, "1")
    assert len(load.load_raw()) == 1
    assert "results will differ" in capsys.readouterr().err


def test_a_fresh_download_that_differs_never_replaces_the_preserved_snapshot(snap, monkeypatch):
    preserved = snap.read_bytes()
    live = pd.DataFrame({"unique_key": ["1", "2", "3"], "created_date": ["2023-01-01T00:00:00.000"] * 3})

    class Meta:
        @staticmethod
        def json():
            return {"name": "311", "rowsUpdatedAt": 0}

    monkeypatch.setattr(download.requests, "get", lambda *a, **k: Meta())
    monkeypatch.setattr(config, "RAW_PARTS_DIR", snap.parent / "parts")
    monkeypatch.setattr(download, "_month_windows", lambda: [(1, "a", "b")])

    def fake_month(month, start, end):
        live.to_parquet(config.RAW_PARTS_DIR / "2023-01.parquet", index=False)
        return {"month": month, "expected": 3, "downloaded": 3, "cached": False}

    monkeypatch.setattr(download, "download_month", fake_month)
    with pytest.raises(snapshot.SnapshotError, match="not the snapshot"):
        download.main(fresh=True)
    assert snap.read_bytes() == preserved  # the live data went to a separate file, not over the snapshot
    assert (snap.parent / "nyc311_2023.fresh.parquet").exists()
