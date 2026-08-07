"""Transport-level behavior.

Resolution rules and failure modes are tested here; the fixture org's *content*
is tested in test_fixture.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from arcgis_inventory.errors import FixtureMissingError
from arcgis_inventory.transport import FixtureTransport, Response, Transport

FIXTURE = Path(__file__).parent / "fixtures" / "northgate"
PORTAL = "https://northgate.example.gov/portal/sharing/rest"
SERVICE = "https://services.northgate.example.gov/server/rest/services/Public/Parcels/FeatureServer"


@pytest.fixture
def transport() -> FixtureTransport:
    return FixtureTransport(FIXTURE)


def test_fixture_transport_satisfies_the_protocol(transport: FixtureTransport) -> None:
    assert isinstance(transport, Transport)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (f"{PORTAL}/portals/self", "portal/self.json"),
        (f"{PORTAL}/community/users", "portal/users.json"),
        (f"{PORTAL}/community/groups", "portal/groups.json"),
        (
            f"{PORTAL}/content/items/a0000000000000000000000000000001",
            "items/a0000000000000000000000000000001.json",
        ),
        (
            f"{PORTAL}/content/items/a0000000000000000000000000000001/data",
            "items/a0000000000000000000000000000001.data.json",
        ),
        (
            SERVICE,
            "services/services.northgate.example.gov/"
            "server__rest__services__Public__Parcels__FeatureServer.json",
        ),
    ],
)
def test_urls_map_onto_the_documented_layout(
    transport: FixtureTransport, url: str, expected: str
) -> None:
    resolved = transport.resolve(url)
    assert resolved is not None
    assert resolved.relative_to(FIXTURE).as_posix() == expected


def test_a_service_url_is_not_mistaken_for_a_portal_url(transport: FixtureTransport) -> None:
    """Both contain `/rest/`; only one is followed by `services`."""
    service = transport.resolve(SERVICE)
    portal = transport.resolve(f"{PORTAL}/portals/self")
    assert service is not None and portal is not None
    assert service.parent.parent.name == "services"
    assert portal.parent.name == "portal"


def test_a_pathological_service_path_stays_within_the_windows_limit() -> None:
    """A deep REST path must not produce a file git cannot open on Windows.

    Real portals have paths like this, and `MAX_PATH` is 260 characters --- at
    which point git reports "Filename too long" and silently skips the file.
    """
    deep = (
        "https://services.northgate.example.gov/server/rest/services/Internal/Engineering/"
        "CapitalImprovements/StormwaterManagement/DetentionBasins/InspectionHistory/"
        "2019Through2026/DetentionBasinInspectionHistoryArchive/FeatureServer"
    )
    resolved = FixtureTransport(FIXTURE).resolve(deep)
    assert resolved is not None
    assert resolved.is_file(), "the fixture must actually ship this service"
    assert len(str(resolved.resolve())) < 260
    # Flattened to one component, so no directory tree gets deep either.
    assert resolved.parent.name == "services.northgate.example.gov"


@pytest.mark.parametrize(("start", "page"), [(1, 1), (11, 2), (21, 3)])
def test_paging_maps_start_onto_page_files(
    transport: FixtureTransport, start: int, page: int
) -> None:
    resolved = transport.resolve(f"{PORTAL}/search", {"start": start, "num": 10})
    assert resolved is not None
    assert resolved.name == f"page-{page}.json"


def test_requests_are_recorded_for_assertions(transport: FixtureTransport) -> None:
    transport.get_json(f"{PORTAL}/portals/self")
    assert transport.requested == [f"{PORTAL}/portals/self"]


def test_an_unmapped_url_fails_loudly(transport: FixtureTransport) -> None:
    """A silently-empty response makes a broken crawler look like a clean org."""
    with pytest.raises(FixtureMissingError, match="no fixture for"):
        transport.get_json(f"{PORTAL}/content/items/a0000000000000000000000000000999")


def test_non_strict_mode_reports_404_rather_than_empty_success() -> None:
    t = FixtureTransport(FIXTURE, strict=False)
    reply = t.get_json(f"{PORTAL}/content/items/a0000000000000000000000000000999")
    assert reply.status == 404
    assert reply.ok is False


def test_an_unrecognized_portal_endpoint_is_unmapped(transport: FixtureTransport) -> None:
    assert transport.resolve(f"{PORTAL}/content/users/kmartinez_ng/items") is None


def test_response_ok_reflects_http_status_only() -> None:
    assert Response(url="x", status=200, data={"error": {"code": 403}}).ok is True
    assert Response(url="x", status=403, data=None).ok is False
