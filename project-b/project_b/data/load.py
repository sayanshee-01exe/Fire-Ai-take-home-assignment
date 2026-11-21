"""Load the raw 2023 snapshot and turn it into typed "feed" batches.

raw  : every value a string exactly as NYC Open Data delivered it (for profiling)
feed : numeric columns cast to the contract in config.FEED_DTYPES; dates stay ISO
       strings (that is how a JSON/CSV feed delivers them)
"""
from __future__ import annotations

import pandas as pd

from project_b import config


def load_raw(columns: list[str] | None = None) -> pd.DataFrame:
    """The preserved snapshot, after checking that it is the file dataset_manifest.json describes
    (snapshot.verify: a missing or different file stops here, it is never read silently)."""
    from project_b.data import snapshot

    snapshot.verify(config.RAW_FILE)
    return pd.read_parquet(config.RAW_FILE, columns=columns)


def to_feed(raw: pd.DataFrame) -> pd.DataFrame:
    """Cast numeric columns to the contracted dtypes. A value that is not a number
    becomes NaN here; the profiler counts such values on the raw strings first."""
    df = raw.copy(deep=False)
    for col, dtype in config.FEED_DTYPES.items():
        if col not in df.columns or dtype not in ("int64", "float64"):
            continue
        num = pd.to_numeric(df[col], errors="coerce")
        if dtype == "int64":
            bad = int(num.isna().sum())
            if bad:
                raise ValueError(f"{col}: {bad} values are not integers")
            df[col] = num.astype("int64")
        else:
            df[col] = num.astype("float64")
    return df


def load_feed(columns: list[str] | None = None) -> pd.DataFrame:
    return to_feed(load_raw(columns))


def split_reference_eval(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Reference period (fit / calibrate) = created before config.REFERENCE_END;
    evaluation period = the rest. ISO strings compare chronologically."""
    is_ref = df["created_date"] < config.REFERENCE_END
    return df[is_ref].reset_index(drop=True), df[~is_ref].reset_index(drop=True)


def make_batches(df: pd.DataFrame, batch_size: int = config.BATCH_SIZE) -> list[pd.DataFrame]:
    """Contiguous, arrival-ordered batches; a trailing partial batch is dropped."""
    return [df.iloc[i : i + batch_size].reset_index(drop=True) for i in range(0, len(df) - batch_size + 1, batch_size)]
