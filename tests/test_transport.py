from __future__ import annotations

import json
from pathlib import Path

import pytest

from arcgis_inventory.errors import FixtureMissingError
from arcgis_inventory.transport import FixtureTransport, Transport


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "search").mkdir(parents=True)
    (tmp_path / "search" / "page-1.json").write_text(
        json.dumps({"total": 1, "start": 1, "num": 100, "results": []}), encoding="utf-8"
    )
    (tmp_path / "search" / "page-2.json").write_text(
        json.dumps({"total": 1, "start": 101, "num": 100, "results": []}), encoding="utf-8"
    )
    items = tmp_path / "northgate.example.gov" / "sharing" / "rest" / "content" / "items"
    items.mkdir(parents=True)
    (items / "a0000000000000000000000000000001.json").write_text(
        json.dumps({"id": "a0000000000000000000000000000001", "type": "Web Mapping Application"}),
        encoding="utf-8",
    )
    return tmp_path


def test_fixture_transport_satisfies_the_protocol(root: Path) -> None:
    assert isinstance(FixtureTransport(root), Transport)


def test_item_lookup(root: Path) -> None:
    t = FixtureTransport(root)
    reply = t.get_json(
        "https://northgate.example.gov/sharing/rest/content/items/a0000000000000000000000000000001"
    )
    assert reply.ok
    assert reply.data["type"] == "Web Mapping Application"


@pytest.mark.parametrize(("start", "page"), [(1, 1), (101, 2)])
def test_paging_maps_start_onto_page_files(root: Path, start: int, page: int) -> None:
    t = FixtureTransport(root)
    reply = t.get_json(
        "https://northgate.example.gov/sharing/rest/search", {"start": start, "num": 100}
    )
    assert reply.data["start"] == (1 if page == 1 else 101)


def test_an_unmapped_url_fails_loudly(root: Path) -> None:
    """A silently-empty response makes a broken crawler look like a clean org."""
    t = FixtureTransport(root)
    with pytest.raises(FixtureMissingError):
        t.get_json("https://northgate.example.gov/sharing/rest/content/items/nope")


def test_non_strict_mode_reports_404_rather_than_empty_success(root: Path) -> None:
    t = FixtureTransport(root, strict=False)
    reply = t.get_json("https://northgate.example.gov/sharing/rest/content/items/nope")
    assert reply.status == 404
    assert reply.ok is False
