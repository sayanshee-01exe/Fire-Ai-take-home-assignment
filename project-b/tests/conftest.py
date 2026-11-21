"""Shared pytest fixtures.

tests/data holds two small slices of the REAL 2023 data (committed, ~3 MB) so the
tests run in seconds without the 3.2 M-row download:
  reference_raw.parquet : 15,000 rows sampled across Jan-Aug 2023 (stands in for the reference period)
  batch_raw.parquet     : 5,000 contiguous rows from mid-October 2023 (one realistic batch)
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from project_b.data.load import to_feed  # noqa: E402

DATA = Path(__file__).parent / "data"


@pytest.fixture(scope="session")
def reference_raw() -> pd.DataFrame:
    return pd.read_parquet(DATA / "reference_raw.parquet")


@pytest.fixture(scope="session")
def batch_raw() -> pd.DataFrame:
    return pd.read_parquet(DATA / "batch_raw.parquet")


@pytest.fixture(scope="session")
def reference_feed(reference_raw) -> pd.DataFrame:
    return to_feed(reference_raw)


@pytest.fixture()
def batch_feed(batch_raw) -> pd.DataFrame:
    """Fresh copy per test so a test that corrupts it cannot leak into another."""
    return to_feed(batch_raw).copy()
