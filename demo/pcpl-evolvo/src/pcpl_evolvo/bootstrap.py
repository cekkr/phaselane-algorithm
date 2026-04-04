"""Environment/bootstrap helpers for pcpl_evolvo."""

from __future__ import annotations

import runpy
import sys
from functools import lru_cache
from pathlib import Path
from typing import Dict


THIS_FILE = Path(__file__).resolve()
PROJECT_ROOT = THIS_FILE.parents[2]  # demo/pcpl-evolvo
DEMO_ROOT = THIS_FILE.parents[3]  # demo
REPO_ROOT = THIS_FILE.parents[4]
EVOLVO_SRC = PROJECT_ROOT / "evolvo" / "src"
REFERENCE_PCPL = DEMO_ROOT / "pcpl_cycle_test.py"


def ensure_evolvo_importable() -> None:
    """Make the vendored evolvo source importable without pip install."""
    path = str(EVOLVO_SRC)
    if path not in sys.path:
        sys.path.insert(0, path)


@lru_cache(maxsize=1)
def load_reference_pcpl() -> Dict[str, object]:
    """Load the existing deterministic PCPL implementation as the baseline model."""
    if not REFERENCE_PCPL.exists():
        raise FileNotFoundError(f"Reference PCPL script not found: {REFERENCE_PCPL}")
    return runpy.run_path(str(REFERENCE_PCPL))
