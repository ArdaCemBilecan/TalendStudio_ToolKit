from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples" / "talend"


@pytest.fixture(scope="session")
def samples() -> Path:
    return SAMPLES
