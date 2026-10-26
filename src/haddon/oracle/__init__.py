"""The independent oracle (chunk C2b): pinned source -> store -> reparsed export, compared three ways.

The oracle is a failing command, not a report that prints mismatches and exits successfully.  ``run_oracle``
returns a report whose ``ok`` is false unless every required gate passed, ``bin/haddon-oracle`` exits nonzero on
any disagreement, and ``tests/test_oracle.py`` asserts the same inside the suite.
"""

from haddon.oracle.report import Gate, OracleReport
from haddon.oracle.runner import Baseline, load_baselines, run_oracle

__all__ = ["load_baselines", "Baseline", "Gate", "OracleReport", "run_oracle"]
