"""Tests for the report generator (never invents a number) and the single-command entry point."""
from __future__ import annotations

import json
import re

import pytest

import project_b.__main__ as entry
from project_b import evaluate, pipeline, report


def test_reports_say_not_measured_when_results_are_missing(tmp_path):
    report.main(["--results-dir", str(tmp_path), "--out-dir", str(tmp_path)])
    results = (tmp_path / "RESULTS_B.md").read_text(encoding="utf-8")
    matrix = (tmp_path / "DECISION_MATRIX.md").read_text(encoding="utf-8")
    assert "{{" not in results and "{{" not in matrix  # every block was filled
    assert results.count(report.PENDING) >= 3  # benchmark, background, pipeline, scale
    assert "| Detector benchmark (Tasks 3-4) | `" in results and "**not run yet**" in results
    assert "Plan source: **provisional**" in matrix and "design hypothesis" in matrix
    measured_sections = results.split("## 4.")[1].split("## 7.")[0]
    assert not re.search(r"\d+(\.\d+)?% \[", measured_sections)  # no measured rate (value [Wilson CI]) where nothing ran
    assert measured_sections.count(report.PENDING) >= 3  # detectors, background, pipeline


def test_closed_and_open_screen_counts_come_from_the_decision_file(tmp_path):
    report.main(["--results-dir", str(tmp_path), "--out-dir", str(tmp_path)])
    results = (tmp_path / "RESULTS_B.md").read_text(encoding="utf-8")
    assert "| open texts (may be put on a Closed row) | 29 | 23 | 6 | 0 |" in results
    assert "| closed texts (may stay on a reopened row) | 415 | 239 | 176 | 30 |" in results


def test_measured_plan_is_reported_with_its_numbers(tmp_path):
    summary = {"detectors": [
        {"problem": "d", "family": "rule", "detector": "rule_nyc_bbox", "status": "ok", "detection": {"value": 1.0}, "row_fpr": {"value": 0.0}, "n_batches": {"corrupted": 20, "clean": 20}},
        {"problem": "d", "family": "stat", "detector": "stat_coordinate_outlier", "status": "ok", "detection": {"value": 0.99}, "row_fpr": {"value": 0.0}, "n_batches": {"corrupted": 20, "clean": 20}},
    ], "same_rows": {}, "settings": {"llm_model": None}, "rate_sweep": [], "null_spike_sweep": [], "background": []}
    (tmp_path / evaluate.SUMMARY_FILE).write_text(json.dumps(summary))
    plan, source = pipeline.load_plan(tmp_path)
    assert source == "partly measured" and plan["d"].detectors == ("rule_nyc_bbox",)
    assert plan["d"].reason.startswith("measured: rule_nyc_bbox detection 100.00%")
    assert plan["a"].reason.startswith("provisional")  # no measurement for a: the default stays, and says so


def test_unknown_template_block_is_an_error():
    with pytest.raises(KeyError):
        report.render("{{no_such_block}}", report.Inputs(report.config.RESULTS_DIR))


def test_main_dry_run_runs_nothing(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(entry, "llm_mode", lambda requested: "replay")
    from project_b import profile
    monkeypatch.setattr(profile, "main", lambda *a, **k: calls.append("profile"))
    entry.main(["--dry-run"])
    out = capsys.readouterr().out
    assert "LLM mode: replay" in out and calls == []
    for step in entry.STEPS:
        assert f"- {step}:" in out


def test_main_runs_the_chosen_steps_in_order(monkeypatch, tmp_path):
    calls = []
    from project_b import scale
    monkeypatch.setattr(entry, "llm_mode", lambda requested: "replay")
    monkeypatch.setattr(scale, "main", lambda argv: calls.append(("scale", argv)))
    monkeypatch.setattr(report, "main", lambda argv: calls.append(("report", argv)))
    entry.main(["--steps", "scale,report", "--quick"])
    assert [c[0] for c in calls] == ["scale", "report"]
    assert all("partial" in " ".join(c[1]) for c in calls)  # --quick never writes over the full results


def test_llm_mode_defaults_to_replay_without_a_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setattr("project_b.llm._load_dotenv", lambda: None)
    assert entry.llm_mode(None) == "replay" and entry.llm_mode("live") == "live"


def test_unknown_step_is_rejected():
    with pytest.raises(SystemExit):
        entry.parse_args(["--steps", "benchmark"])
