"""Central configuration for Project B (data-quality checks on NYC 311).

Every number that shapes an experiment lives here, so "change a requirement
live" in the interview is a one-line edit. Detector-specific thresholds live
at the top of each detector module, next to the code that uses them.
"""
from __future__ import annotations

import os
from pathlib import Path

# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent  # repository root
DATA_DIR = ROOT / "data"  # git-ignored: raw downloads + derived parquet
RAW_DIR = DATA_DIR / "raw"
RAW_PARTS_DIR = RAW_DIR / "nyc311_2023_parts"  # one parquet per month
RAW_FILE = RAW_DIR / "nyc311_2023.parquet"  # full year, values exactly as delivered
RAW_MANIFEST = RAW_DIR / "manifest.json"  # written by each download: expected vs downloaded counts, checksum
# Committed description of THE snapshot every number comes from; verified before the raw data is read.
DATASET_MANIFEST = ROOT / "dataset_manifest.json"
PKG_DIR = ROOT / "project_b"
RESULTS_DIR = PKG_DIR / "results"  # committed: every reported number comes from here
LLM_CACHE_DIR = PKG_DIR / "llm_cache"  # committed: Gemini responses for replay mode
ENV_FILE = ROOT / ".env"  # optional; holds GEMINI_API_KEY (never committed)

# --------------------------------------------------------------------------
# Source data: NYC Open Data, "311 Service Requests from 2020 to Present"
# --------------------------------------------------------------------------
DATASET_ID = "erm2-nwe9"
SODA_CSV_URL = f"https://data.cityofnewyork.us/resource/{DATASET_ID}.csv"
SODA_JSON_URL = f"https://data.cityofnewyork.us/resource/{DATASET_ID}.json"
SODA_META_URL = f"https://data.cityofnewyork.us/api/views/{DATASET_ID}.json"
YEAR_START = "2023-01-01T00:00:00"  # inclusive, on created_date
YEAR_END = "2024-01-01T00:00:00"  # exclusive

# Columns we keep (SODA field names). Display names in the brief map as:
# complaint_type = "Complaint Type", descriptor = "Descriptor",
# resolution_description = "Resolution Description", status = "Status".
COLUMNS = [
    "unique_key",
    "created_date",
    "closed_date",
    "agency",
    "agency_name",
    "complaint_type",
    "descriptor",
    "descriptor_2",
    "location_type",
    "incident_zip",
    "incident_address",
    "street_name",
    "city",
    "status",
    "due_date",
    "resolution_description",
    "resolution_action_updated_date",
    "community_board",
    "borough",
    "x_coordinate_state_plane",
    "y_coordinate_state_plane",
    "open_data_channel_type",
    "park_borough",
    "latitude",
    "longitude",
]

DATETIME_COLUMNS = ["created_date", "closed_date", "due_date", "resolution_action_updated_date"]
FREE_TEXT_COLUMNS = ["descriptor", "descriptor_2", "resolution_description"]

# --------------------------------------------------------------------------
# The "feed" contract: what a well-formed incoming batch looks like.
# Raw parquet stores every value as a string exactly as delivered; data.load.to_feed()
# converts to these dtypes. Dates intentionally stay strings: a real JSON/CSV feed
# delivers them as text, which is what makes a format switch possible (problem c).
# --------------------------------------------------------------------------
FEED_DTYPES: dict[str, str] = {c: "str" for c in COLUMNS}
FEED_DTYPES.update(
    {
        "unique_key": "int64",
        "latitude": "float64",
        "longitude": "float64",
        "x_coordinate_state_plane": "float64",
        "y_coordinate_state_plane": "float64",
    }
)
# Source timestamp format, e.g. 2023-07-01T23:59:55.000
SOURCE_DATETIME_FORMAT = "%Y-%m-%dT%H:%M:%S.%f"
SOURCE_DATETIME_REGEX = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}$"

BOROUGHS = ["MANHATTAN", "BRONX", "BROOKLYN", "QUEENS", "STATEN ISLAND"]
UNSPECIFIED_BOROUGH = "Unspecified"

# NYC bounding box (WGS84). Generous by ~1 km so shoreline points are inside.
NYC_LAT_MIN, NYC_LAT_MAX = 40.47, 40.93
NYC_LON_MIN, NYC_LON_MAX = -74.27, -73.68

# Status groups used by problem k (Resolution Description vs Status).
CLOSED_STATUSES = {"Closed"}
OPEN_STATUSES = {"Open", "In Progress", "Assigned", "Pending", "Started"}

# --------------------------------------------------------------------------
# Experimental design
# --------------------------------------------------------------------------
SEED = 2023
# Time split: detectors learn baselines / calibrate thresholds on the reference
# period only, and are evaluated on batches from the later evaluation period.
REFERENCE_END = "2023-09-01T00:00:00"  # reference = Jan-Aug, evaluation = Sep-Dec
BATCH_SIZE = 5_000  # rows per batch (~half a day of NYC 311 traffic)
N_CORRUPTED_BATCHES = 20  # per problem type, for rule + statistical detectors
N_CLEAN_BATCHES = 20  # clean evaluation batches for false-alarm measurement
ROW_INJECTION_RATE = 0.02  # share of rows corrupted for row-level problems
RATE_SWEEP = [0.001, 0.005, 0.02, 0.10]  # sensitivity sweep (rule + stat only)
TARGET_ROW_FPR = 0.001  # stat detectors calibrate thresholds to this on reference data
TARGET_BATCH_FAR = 0.05  # batch-level stat detectors: max share of clean batches alerted

# LLM evaluation sizes. Kept small enough for the Gemini free tier.
LLM_ROWS_PER_CALL = 25  # rows packed into one per-row review prompt
LLM_ROW_SAMPLE_INJECTED = 100  # injected rows reviewed per row-level problem
LLM_ROW_SAMPLE_CLEAN = 100  # clean rows reviewed per row-level problem
LLM_BATCH_SAMPLE_ROWS = 40  # rows shown to the LLM for a batch-sample review
LLM_N_BATCHES = 8  # corrupted (and, separately, clean) batches for batch-sample review
LLM_REPEATS = 3  # repeated identical calls to measure determinism

# Hybrid pipeline evaluation (Task 5, project_b/pipeline.py). Smaller than the detector
# benchmark because every corrupted row the cheap layer flags for j / k / l may go to the LLM.
PIPELINE_N_BATCHES = 10  # single-problem corrupted batches per problem (the benchmark's first ones)
PIPELINE_N_CLEAN = 10  # clean batches (the benchmark's first clean ones)
PIPELINE_N_MIXED = 20  # batches carrying several corruption types at once
PIPELINE_PROBLEMS_PER_MIXED = 3  # corruption types per mixed batch
PIPELINE_MAX_CONFIRM_ITEMS = 100  # distinct values one batch may send to the LLM per problem

# --------------------------------------------------------------------------
# Cost model
# --------------------------------------------------------------------------
# Gemini list prices in USD per 1M tokens (paid tier). The free tier costs $0 but
# is rate-limited; we report what the same traffic would cost on the paid tier.
# Thinking tokens are billed as output tokens.
# Source: Gemini Developer API pricing page (ai.google.dev/gemini-api/docs/pricing,
# now served from aistudio.google.com/docs/pricing), read 2026-11-20, "Standard" tier,
# text input. Free tier: "Free of charge" for all of these, but rate-limited.
LLM_PRICES_PER_MTOK: dict[str, dict[str, float]] = {
    "gemini-3.5-flash-lite": {"input": 0.30, "output": 2.50},
    "gemini-3.1-flash-lite": {"input": 0.25, "output": 1.50},
    "gemini-2.5-flash-lite": {"input": 0.10, "output": 0.40},
    "gemini-2.5-flash": {"input": 0.30, "output": 2.50},
    "gemini-3-flash-preview": {"input": 0.50, "output": 3.00},
    "gemini-3.6-flash": {"input": 0.75, "output": 3.75},  # introductory price until 2026-12-31
    "gemini-3.8-flash": {"input": 0.75, "output": 3.75},  # introductory price until 2026-12-31
}
LLM_PRICES_AS_OF = "2026-11-20, Gemini Developer API pricing page (Standard tier, text)"
# Rule/stat detectors cost only CPU. On-demand cloud vCPU-hour, rounded up.
COMPUTE_USD_PER_VCPU_HOUR = 0.05

# Gemini client defaults (overridable with env vars GEMINI_MODEL / GEMINI_RPM).
# gemini-3.5-flash-lite is Google's current low-cost model (gemini-2.5-flash-lite is
# "no longer available to new users" and redirects here) - the realistic choice for
# per-row review at volume. It is the only model this project calls.
GEMINI_DEFAULT_MODEL = "gemini-3.5-flash-lite"
GEMINI_RPM = 10  # requests per minute we allow ourselves (the free tier allows 15, see below)
GEMINI_TIMEOUT_S = 120
# Models that accept thinkingConfig.thinkingBudget=0 (checked live on 2026-11-20: 3.1-flash-lite accepts it,
# 3.5-flash-lite rejects it with HTTP 400 and does not think by default).
THINKING_BUDGET_MODEL_PREFIXES = ("gemini-2.5", "gemini-3.1")
# Free-tier limits for gemini-3.5-flash-lite as shown on the project's AI Studio "Rate limits"
# page (checked by the user, 2026-11-21): 15 requests/min, 250,000 tokens/min, 500 requests/day.
# The RPD figure only feeds the reported free-tier throughput ceiling; the run itself is never
# planned around it: a daily-quota error from the API stops further LLM calls (circuit breaker),
# and the next run resumes from the replay cache. Override with env GEMINI_FREE_RPD.
GEMINI_FREE_TIER_RPD = int(os.environ.get("GEMINI_FREE_RPD", "500"))
