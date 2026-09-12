import socket
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parents[1] / "research" / "fixtures"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Network is forbidden in offline tests")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)


@pytest.fixture
def initial():
    return (FIXTURES / "marathon-initial.html").read_bytes()


@pytest.fixture
def next_raw():
    return (FIXTURES / "marathon-next-question.json").read_bytes()
