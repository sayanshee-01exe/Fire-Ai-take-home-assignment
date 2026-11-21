"""Evaluation harnesses. Results go to RESULTS_DIR: eval/results/, or $PROJECT_A_RESULTS_DIR if set
(reproduce.py sets it so a replay run never touches the committed results)."""
import os
from pathlib import Path

RESULTS_DIR = Path(os.environ.get("PROJECT_A_RESULTS_DIR") or Path(__file__).resolve().parent / "results")
