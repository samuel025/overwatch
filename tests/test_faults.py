"""The fault catalog must grade its own ground truth as correct, or the eval is meaningless."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "examples" / "demo-system" / "scripts"))
sys.path.insert(0, str(ROOT / "eval"))

from faults import FAULTS  # noqa: E402
from run_eval import grade  # noqa: E402


@pytest.mark.parametrize("name", list(FAULTS))
def test_truth_passes_its_own_grading(name):
    ok, missing = grade(FAULTS[name], FAULTS[name].truth)
    assert ok, f"{name}: truth text misses {missing}"


@pytest.mark.parametrize("name", list(FAULTS))
def test_report_faults_have_a_report(name):
    f = FAULTS[name]
    assert f.trigger in ("watcher", "report")
    assert bool(f.report) == (f.trigger == "report")


def test_vague_answer_fails():
    ok, _ = grade(FAULTS["lock_contention"], "payments service is slow, consider restarting it")
    assert not ok
