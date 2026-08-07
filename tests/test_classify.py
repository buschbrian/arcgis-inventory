"""Classification rules in isolation.

Most classification is covered end to end by the crawl's golden test against the
fixture org. This file holds the cases learned from a **real** portal, where the
fixture's thirty hand-built items had nothing to say.
"""

from __future__ import annotations

import pytest

from arcgis_inventory.classify import classify, is_data_bearing
from arcgis_inventory.vocabulary import PLATFORMS


def item(item_type: str, **extra: object) -> dict:
    return {"id": "a" * 32, "type": item_type, "typeKeywords": [], **extra}


# ---------------------------------------------------------------------------
# Learned from a real portal
# ---------------------------------------------------------------------------


def test_a_survey123_form_is_not_asked_for_its_data() -> None:
    """A Form's /data redirects to a .zip. Requesting it produced one failed
    fetch per form on the first real crawl --- 34 of them --- and turned an
    otherwise clean run `partial` for no reason at all."""
    assert is_data_bearing("Form") is False


def test_a_form_still_classifies_confidently_without_its_data() -> None:
    """Which is why not asking costs nothing."""
    result = classify(item("Form"))
    assert result.platform == "form"
    assert result.confidence == "certain"


@pytest.mark.parametrize(
    "item_type",
    [
        "PDF",
        "Image",
        "Shapefile",
        "File Geodatabase",
        "Map Package",
        "KML",
        "GeoJson",
        "Service Definition",
        "CSV",
        "Microsoft Word",
    ],
)
def test_uploaded_files_are_recognized_rather_than_dumped_in_other(item_type: str) -> None:
    """`other` has to mean "this tool does not know what this is". A bucket that
    quietly contains every PDF in the organization cannot carry that meaning,
    and the number stops being a useful signal that something needs attention.
    """
    result = classify(item(item_type))
    assert result.platform == "file"
    assert result.confidence == "certain"


@pytest.mark.parametrize("item_type", ["Hub Site Application", "Hub Page", "Hub Initiative"])
def test_hub_content_is_recognized(item_type: str) -> None:
    assert classify(item(item_type)).platform == "hub_site"


def test_genuinely_unknown_types_still_land_in_other() -> None:
    """The bucket must keep working for what it is actually for."""
    result = classify(item("Insights Workbook"))
    assert result.platform == "other"
    assert result.confidence == "guess"
    assert result.evidence["item_type"] == "Insights Workbook"


# ---------------------------------------------------------------------------
# Invariants
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "item_type",
    [
        "Web Map",
        "Web Scene",
        "Dashboard",
        "StoryMap",
        "Form",
        "PDF",
        "Hub Page",
        "Web Experience",
        "Code Attachment",
        "Anything At All",
    ],
)
def test_every_verdict_uses_the_declared_vocabulary(item_type: str) -> None:
    assert classify(item(item_type)).platform in PLATFORMS


def test_every_data_bearing_type_really_returns_json() -> None:
    """A type on this list is one the crawler will spend a request on, and a
    failure there is recorded as a crawl error. Keep it to types whose /data is
    genuinely a JSON document."""
    assert is_data_bearing("Web Map")
    assert is_data_bearing("Web Mapping Application")
    for binary in ("Form", "PDF", "Shapefile", "Code Attachment", "Service Definition"):
        assert not is_data_bearing(binary), binary
