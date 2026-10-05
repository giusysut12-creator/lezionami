import os
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests import mock_claude  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def mock_api():
    server = mock_claude.start()
    os.environ["ANTHROPIC_BASE_URL"] = f"http://127.0.0.1:{server.server_address[1]}"
    os.environ["ANTHROPIC_API_KEY"] = "chiave-finta-per-test"
    os.environ["SINGLE_PASS_MAX_CHARS"] = "60000"
    os.environ["SEGMENT_MAX_CHARS"] = "40000"
    yield server
    server.shutdown()


@pytest.fixture()
def mock_state(mock_api):
    mock_claude.STATE.reset()
    yield mock_claude.STATE
    mock_claude.STATE.reset()


@pytest.fixture(scope="session")
def app_url(mock_api):
    import uvicorn

    port = _free_port()
    config = uvicorn.Config("app.main:app", host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
