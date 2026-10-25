import os

import pytest


@pytest.fixture(autouse=True)
def _no_real_zetaris():
    """Tests must not pick up a developer's real Zetaris or state-database settings (load_dotenv reads .env into the
    environment), or they would need those services running."""
    mine = lambda k: k.startswith("ZETARIS_") or k == "OVERWATCH_STATE_URL"  # noqa: E731
    before = {k: v for k, v in os.environ.items() if mine(k)}
    for k in before:
        del os.environ[k]
    yield
    for k in [k for k in os.environ if mine(k)]:
        del os.environ[k]
    os.environ.update(before)
