"""Tests for the scale projection (project_b/scale.py): it never invents a measurement."""
from __future__ import annotations

import json

import pytest

from project_b import config, pipeline, scale


def test_without_measurements_everything_measured_is_pending():
    rows = scale.project(None)
    assert rows["rows per second, daily mean"]["value"] == pytest.approx(50_000_000 / 86_400)
    for name in ("local throughput, one laptop core", "cores needed at peak", "compute cost per day", "LLM cost per day", "LLM values per day"):
        assert rows[name]["value"] is None and rows[name]["source"] == "pending", name
    assert "pending" in scale.render(rows, None)


def test_projection_uses_normal_traffic_only():
    pipe = {"settings": {"llm_model": config.GEMINI_DEFAULT_MODEL},
            "throughput": {"rows_per_s_excluding_llm_wait": 10_000.0},
            "llm_by_kind": {
                "clean": {"rows": 500_000, "values_asked": 50, "llm_calls": 2, "input_tokens": 5_000, "output_tokens": 1_000, "llm_latency_s": 4.0},
                "raw_background": {"rows": 500_000, "values_asked": 50, "llm_calls": 2, "input_tokens": 5_000, "output_tokens": 1_000, "llm_latency_s": 4.0},
                "single": {"rows": 1_000_000, "values_asked": 99_999, "llm_calls": 999, "input_tokens": 1, "output_tokens": 1, "llm_latency_s": 1.0}}}
    rows = scale.project(pipe)
    assert rows["LLM values escalated per 1M rows (clean + real batches)"]["value"] == pytest.approx(100)  # 100 values / 1M rows
    assert rows["LLM values per day"]["value"] == pytest.approx(100 * 50)
    assert rows["LLM requests per day"]["value"] == pytest.approx(5_000 * 4 / 100)
    price = config.LLM_PRICES_PER_MTOK[config.GEMINI_DEFAULT_MODEL]
    assert rows["LLM cost per day"]["value"] == pytest.approx(5_000 * (100 * price["input"] + 20 * price["output"]) / 1e6)
    assert rows["cores needed at peak"]["value"] == -(-50_000_000 / 86_400 * scale.PEAK_TO_MEAN * scale.HEADROOM // 10_000)
    assert rows["local throughput, one laptop core"]["source"] == "measured"


def test_main_writes_outputs(tmp_path):
    rows = scale.main(["--results-dir", str(tmp_path)])
    assert (tmp_path / "scale.json").exists() and (tmp_path / "scale_tables.md").exists()
    assert json.loads((tmp_path / "scale.json").read_text())["assumptions_and_projection"].keys() == rows.keys()
    assert not (tmp_path / pipeline.SUMMARY_FILE).exists()


def test_a_plan_without_llm_requests_has_no_llm_wait():
    """0 requests is a measured zero, not a pending measurement (regression: it showed as pending)."""
    zero = {"rows": 50_000, "values_asked": 0, "llm_calls": 0, "input_tokens": 0, "output_tokens": 0, "llm_latency_s": 0.0}
    pipe = {"settings": {"llm_model": config.GEMINI_DEFAULT_MODEL}, "throughput": {"rows_per_s_excluding_llm_wait": 4_000.0},
            "llm_by_kind": {"clean": zero, "raw_background": zero}}
    row = scale.project(pipe)["LLM wait per batch at peak, largest customer"]
    assert row["value"] == 0.0 and row["source"] == "projection" and "no LLM request" in row["how"]
