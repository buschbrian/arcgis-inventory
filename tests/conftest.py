"""Test isolation from the developer's own machine.

This exists because of a real incident: adding `.env` support meant a developer
with a `.env` in the repo root had their *test suite* pick up a live portal URL
and start crawling production. The tests that assert "refuses to run without a
portal" were the ones that did it, because unsetting the environment variable
no longer removed the configuration.

Two guarantees, applied to every test automatically:

* no ``ARCGIS_*`` variable from the developer's shell is visible
* the working directory is an empty temporary one, so no ambient ``.env``,
  ``output/``, or database is found

Tests that care about either --- the `.env` parsing tests, for instance --- set
up their own, and still work because they chdir and write files themselves.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_from_the_local_machine(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[None]:
    for name in list(dict(__import__("os").environ)):
        if name.startswith("ARCGIS_"):
            monkeypatch.delenv(name, raising=False)

    sandbox = tmp_path_factory.mktemp("cwd")
    monkeypatch.chdir(sandbox)
    yield


@pytest.fixture(autouse=True)
def _no_real_http(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> None:
    """Fail loudly if a test opens a real connection.

    A test that quietly reaches the network is slow, flaky, and --- for this
    tool specifically --- capable of crawling somebody's production portal.
    Mark a test `@pytest.mark.network` if it genuinely needs one; nothing in
    the suite does today, and CI never runs them.
    """
    if request.node.get_closest_marker("network"):
        return

    import httpx

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError(
            "a test tried to open a real HTTP connection. Use FixtureTransport, "
            "or mark the test with @pytest.mark.network if it truly needs one."
        )

    monkeypatch.setattr(httpx.Client, "send", refuse)


@pytest.fixture
def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent
