"""Shared fixtures."""

import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from tests.fake_server import FakeLambdatronic, split_snapshot

FIXTURES = Path(__file__).parent / "fixtures"


def load_snapshot(name: str) -> dict[str, int]:
    data: dict[str, int] = json.loads((FIXTURES / name).read_text())
    return data


@pytest.fixture
def snapshot() -> dict[str, int]:
    return load_snapshot("snapshot_automatik.json")


@pytest.fixture
async def fake(snapshot: dict[str, int]) -> AsyncIterator[FakeLambdatronic]:
    inputs, holdings = split_snapshot(snapshot)
    server = FakeLambdatronic(inputs, holdings)
    await server.start()
    yield server
    await server.stop()


@pytest.fixture
async def fake_address(fake: FakeLambdatronic) -> tuple[str, int]:
    assert fake._server is not None
    host, port = fake._server.sockets[0].getsockname()[:2]
    return str(host), int(port)
