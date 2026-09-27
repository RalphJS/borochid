from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
EXAMPLES = ROOT / "examples" / "packages"


@pytest.fixture(scope="session")
def examples() -> Path:
    return EXAMPLES
