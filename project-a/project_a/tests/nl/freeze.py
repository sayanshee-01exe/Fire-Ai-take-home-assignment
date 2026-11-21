"""Freezes the NL query set: records the sha256 of queries.jsonl in MANIFEST.json, frozen=true.

Run:  python3 -m project_a.tests.nl.freeze

Once frozen, test_nl_set.py fails if queries.jsonl no longer matches the recorded hash.
Refuses to re-freeze a frozen set whose file has changed (unfreeze by editing MANIFEST.json).
"""
import hashlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
QUERIES = HERE / "queries.jsonl"
MANIFEST = HERE / "MANIFEST.json"


def sha256_of(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_manifest(queries=QUERIES, manifest=MANIFEST, frozen=False):
    data = {"file": Path(queries).name, "sha256": sha256_of(queries), "frozen": frozen}
    Path(manifest).write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return data


def check(queries=QUERIES, manifest=MANIFEST):
    """Returns None if unfrozen or the hash matches; otherwise an error message."""
    data = json.loads(Path(manifest).read_text(encoding="utf-8"))
    if data["frozen"] and data["sha256"] != sha256_of(queries):
        return ("%s changed after it was frozen: manifest %s, file %s"
                % (Path(queries).name, data["sha256"], sha256_of(queries)))
    return None


def freeze(queries=QUERIES, manifest=MANIFEST):
    if Path(manifest).exists():
        err = check(queries, manifest)
        if err:
            raise SystemExit("refusing to re-freeze: " + err)
    return write_manifest(queries, manifest, frozen=True)


if __name__ == "__main__":
    print(json.dumps(freeze(), indent=2))
    sys.exit(0)
