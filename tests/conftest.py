import os

import pytest


@pytest.fixture(autouse=True)
def _no_real_zetaris():
    """Tests must not pick up a developer's real Zetaris settings (load_dotenv reads .env into the environment)."""
    before = {k: v for k, v in os.environ.items() if k.startswith("ZETARIS_")}
    for k in before:
        del os.environ[k]
    yield
    for k in [k for k in os.environ if k.startswith("ZETARIS_")]:
        del os.environ[k]
    os.environ.update(before)
