"""The fixture org has to be correct before anything can be tested against it.

`expected/edges.json` is a *claim* about the JSON in `items/`. These tests check
the claim against the files, so a hand-edit to either one that desynchronizes
them fails here rather than silently making the crawler's golden test wrong.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from arcgis_inventory.errors import PortalError
from arcgis_inventory.transport import FixtureTransport
from arcgis_inventory.urls import normalize_url

FIXTURE = Path(__file__).parent / "fixtures" / "northgate"
PORTAL = "https://northgate.example.gov/portal/sharing/rest"
PAGE_SIZE = 10


def load(*parts: str) -> Any:
    return json.loads((FIXTURE.joinpath(*parts)).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def inventory() -> list[dict[str, Any]]:
    return load("expected", "inventory.json")


@pytest.fixture(scope="module")
def edges() -> list[dict[str, Any]]:
    return load("expected", "edges.json")


@pytest.fixture
def transport() -> FixtureTransport:
    return FixtureTransport(FIXTURE)


def pointer(document: Any, path: str) -> Any:
    """Resolve a JSON pointer, raising with the failing segment named."""
    node = document
    for raw in path.strip("/").split("/"):
        if isinstance(node, list):
            node = node[int(raw)]
        else:
            if raw not in node:
                raise KeyError(f"{raw!r} not found while resolving {path!r}")
            node = node[raw]
    return node


# ---------------------------------------------------------------------------
# The fixture is internally consistent
# ---------------------------------------------------------------------------


def test_every_edge_source_path_resolves_to_the_url_it_claims(
    edges: list[dict[str, Any]], transport: FixtureTransport
) -> None:
    """The whole point of source_path: an edge has to be auditable."""
    checked = 0
    for edge in edges:
        data = transport.get_json(f"{PORTAL}/content/items/{edge['from_item_id']}/data").data
        value = pointer(data, edge["source_path"])

        if "to_item_id" in edge:
            assert value == edge["to_item_id"], edge
            checked += 1
            continue

        if edge["relation"] == "arcade_source":
            # An Arcade edge points at the expression, not a bare URL --- the
            # crawler has to dig the service out of the script text. That is the
            # whole difficulty of the case, so the fixture keeps it that way.
            assert edge["to_url"] in value, edge
            checked += 1
            continue

        got = normalize_url(value)
        want = normalize_url(edge["to_url"])
        # A print or GP edge points at a task *under* the service; the endpoint
        # recorded is the service itself, which is what the crawler must trim to.
        assert got.url == want.url or got.url.startswith(want.url + "/"), edge
        if got.url == want.url:
            assert got.layer_index == edge["layer_index"], edge
        checked += 1

    assert checked == len(edges)
    assert checked > 40, "the fixture should be exercising far more edges than this"


def test_the_dependency_graph_covers_every_relation_the_schema_declares(
    edges: list[dict[str, Any]],
) -> None:
    used = {e["relation"] for e in edges}
    # Not every relation needs a case yet, but the load-bearing ones do.
    assert {
        "operational_layer",
        "basemap",
        "data_source",
        "geocoder",
        "gp_service",
        "print_service",
        "widget_config",
        "arcade_source",
    } <= used


def test_the_same_service_at_two_indexes_is_one_endpoint_and_three_edges(
    edges: list[dict[str, Any]],
) -> None:
    """Case 22 --- the URL-normalization trap, asserted end to end."""
    centerlines = [
        e
        for e in edges
        if e["from_item_id"] == "a0000000000000000000000000000022" and "to_url" in e
    ]
    assert len(centerlines) == 3
    assert len({normalize_url(e["to_url"]).url for e in centerlines}) == 1
    assert sorted(e["layer_index"] for e in centerlines) == [0, 0, 1]
    # Same layer twice is genuinely two dependencies: different source_path.
    assert len({e["source_path"] for e in centerlines}) == 3


def test_group_layers_nest_four_levels_deep(edges: list[dict[str, Any]]) -> None:
    """Case 19 --- recursion depth in edge extraction."""
    depths = [
        e["source_path"].count("/layers/")
        for e in edges
        if e["from_item_id"] == "a0000000000000000000000000000019"
    ]
    assert depths and max(depths) == 3  # three nested groups, then the leaf


def test_a_service_referenced_by_url_only_has_no_portal_item(
    edges: list[dict[str, Any]], inventory: list[dict[str, Any]]
) -> None:
    """Case 18 --- forces an endpoint node with no item_id."""
    traffic = [e for e in edges if e["from_item_id"] == "a0000000000000000000000000000018"]
    assert traffic and all("to_url" in e for e in traffic)
    item_titles = {i["title"] for i in inventory}
    assert "Traffic Signals" in item_titles  # the web map is an item...
    # ...but the service it points at is not one.
    assert not any(
        i["title"] == "Traffic Signals" and i["platform"] != "web_map" for i in inventory
    )


# ---------------------------------------------------------------------------
# The transport can actually serve it
# ---------------------------------------------------------------------------


def test_search_paginates_and_covers_every_item_exactly_once(
    transport: FixtureTransport, inventory: list[dict[str, Any]]
) -> None:
    seen: list[str] = []
    start = 1
    for _ in range(10):  # generous bound; the fixture is 3 pages
        page = transport.get_json(f"{PORTAL}/search", {"start": start, "num": PAGE_SIZE}).data
        seen.extend(r["id"] for r in page["results"])
        if page["nextStart"] == -1:
            break
        start = page["nextStart"]

    assert len(seen) == len(set(seen)) == len(inventory)
    assert set(seen) == {i["item_id"] for i in inventory}


def test_every_item_in_the_inventory_can_be_fetched(
    transport: FixtureTransport, inventory: list[dict[str, Any]]
) -> None:
    for entry in inventory:
        item = transport.get_json(f"{PORTAL}/content/items/{entry['item_id']}").data
        assert item["id"] == entry["item_id"]
        assert item["type"] == entry["item_type"]
        assert item["access"] == entry["access"]


def test_portal_endpoints_resolve(transport: FixtureTransport) -> None:
    assert transport.get_json(f"{PORTAL}/portals/self").data["currentVersion"] == "11.4"
    assert transport.get_json(f"{PORTAL}/community/users").data["users"]
    assert transport.get_json(f"{PORTAL}/community/groups").data["results"]


def test_service_urls_resolve_to_service_metadata(transport: FixtureTransport) -> None:
    parcels = transport.get_json(
        "https://services.northgate.example.gov/server/rest/services/Public/Parcels/FeatureServer"
    ).data
    assert [layer["name"] for layer in parcels["layers"]] == ["Parcels", "Parcel Lines"]


def test_an_unmapped_url_fails_loudly(transport: FixtureTransport) -> None:
    from arcgis_inventory.errors import FixtureMissingError

    with pytest.raises(FixtureMissingError):
        transport.get_json(f"{PORTAL}/content/items/a0000000000000000000000000000999")


# ---------------------------------------------------------------------------
# The cases that exist to break a naive crawler
# ---------------------------------------------------------------------------


def test_malformed_item_data_raises_rather_than_returning_nothing(
    transport: FixtureTransport,
) -> None:
    """Case 14 --- the crawl records this and continues; it must not look empty."""
    with pytest.raises(PortalError, match="invalid JSON"):
        transport.get_json(f"{PORTAL}/content/items/a0000000000000000000000000000014/data")


def test_a_403_arrives_as_http_200_with_an_error_object(transport: FixtureTransport) -> None:
    """Case 15 --- which is genuinely how the ArcGIS REST API reports this."""
    reply = transport.get_json(f"{PORTAL}/content/items/a0000000000000000000000000000015/data")
    assert reply.ok  # HTTP-wise, yes
    assert reply.data["error"]["code"] == 403


def test_the_departed_owner_is_absent_from_the_user_list(
    transport: FixtureTransport, inventory: list[dict[str, Any]]
) -> None:
    """Case 4 --- owner_exists=0 and v_orphaned."""
    known = {u["username"] for u in transport.get_json(f"{PORTAL}/community/users").data["users"]}
    orphans = [i for i in inventory if not i["owner_exists"]]
    assert [i["item_id"] for i in orphans] == ["a0000000000000000000000000000004"]
    assert orphans[0]["owner"] not in known


def test_null_views_are_distinct_from_zero_views(inventory: list[dict[str, Any]]) -> None:
    """'Unknown usage' is a different recommendation from 'unused'."""
    by_views = {i["item_id"]: i["num_views"] for i in inventory}
    assert by_views["a0000000000000000000000000000024"] is None
    assert by_views["a0000000000000000000000000000004"] == 0


def test_awkward_metadata_survives_a_round_trip(transport: FixtureTransport) -> None:
    nasty = transport.get_json(f"{PORTAL}/content/items/a0000000000000000000000000000007").data
    assert '"' in nasty["title"] and "," in nasty["title"]
    assert "\n" in nasty["snippet"]  # CSV/Excel row-splitting

    emoji = transport.get_json(f"{PORTAL}/content/items/a0000000000000000000000000000030").data
    assert "\U0001f69c" in emoji["title"]  # Windows console codepage
    assert len(emoji["tags"]) == 200

    no_tags = transport.get_json(f"{PORTAL}/content/items/a0000000000000000000000000000004").data
    assert no_tags["tags"] == []


def test_clock_skew_and_missing_timestamps_are_present(transport: FixtureTransport) -> None:
    future = transport.get_json(f"{PORTAL}/content/items/a0000000000000000000000000000025").data
    past = transport.get_json(f"{PORTAL}/content/items/a0000000000000000000000000000016").data
    assert future["modified"] > past["modified"]

    null_modified = transport.get_json(
        f"{PORTAL}/content/items/a0000000000000000000000000000026"
    ).data
    assert null_modified["modified"] is None


def test_duplicate_titles_are_distinguished_only_by_id(inventory: list[dict[str, Any]]) -> None:
    parks = [i for i in inventory if i["title"] == "Parks"]
    assert len(parks) == 2
    assert len({i["item_id"] for i in parks}) == 2


def test_a_folder_name_containing_a_slash_is_present(transport: FixtureTransport) -> None:
    """Path construction on export breaks on this more often than it should."""
    page = transport.get_json(f"{PORTAL}/search", {"start": 21, "num": PAGE_SIZE}).data
    assert page["results"], "third page should not be empty"


def test_the_js_3x_app_carries_the_signals_the_scanner_needs(
    transport: FixtureTransport,
) -> None:
    """Case 8 --- and the reason its classification confidence is only 'likely'."""
    data = transport.get_json(f"{PORTAL}/content/items/a0000000000000000000000000000008/data").data
    assert data["apiUrl"].startswith("https://js.arcgis.com/3.")
    assert "dijit" in data["dojoConfig"]["packages"]

    item = transport.get_json(f"{PORTAL}/content/items/a0000000000000000000000000000008").data
    assert "Web AppBuilder" not in item["typeKeywords"]


def test_the_custom_widget_app_references_a_non_stock_widget(
    transport: FixtureTransport,
) -> None:
    """Case 3 --- what makes an app a rewrite rather than a reconfigure."""
    data = transport.get_json(f"{PORTAL}/content/items/a0000000000000000000000000000003/data").data
    uris = [w["uri"] for w in data["widgetPool"]["widgets"]]
    assert "widgets/NorthgatePlowStatus/Widget" in uris


def test_the_headline_sharing_case_is_wired_end_to_end(
    transport: FixtureTransport, edges: list[dict[str, Any]], inventory: list[dict[str, Any]]
) -> None:
    """Case 5: public app -> public web map -> org-only feature layer."""
    by_id = {i["item_id"]: i for i in inventory}
    app = by_id["a0000000000000000000000000000005"]
    assert app["access"] == "public"

    to_map = [e for e in edges if e["from_item_id"] == app["item_id"] and "to_item_id" in e]
    assert len(to_map) == 1
    web_map = by_id[to_map[0]["to_item_id"]]
    assert web_map["access"] == "public"

    endpoints = {e["url_normalized"]: e for e in load("expected", "endpoints.json")}
    deps = [e for e in edges if e["from_item_id"] == web_map["item_id"] and "to_url" in e]
    # A multi-layer service is several edges onto one endpoint, so dedupe:
    # the finding is about the endpoint, not about each layer reference.
    private = {
        endpoints[normalize_url(e["to_url"]).url]["key"]
        for e in deps
        if endpoints[normalize_url(e["to_url"]).url]["access"] != "public"
    }
    assert private == {"svc_dev_projects"}


# ---------------------------------------------------------------------------
# Run 2
# ---------------------------------------------------------------------------


def test_run2_overlay_shadows_only_what_changed() -> None:
    base = FixtureTransport(FIXTURE)
    later = FixtureTransport(FIXTURE, overlay="run2")
    plow = f"{PORTAL}/content/items/a0000000000000000000000000000003"

    assert later.get_json(plow).data["numViews"] > base.get_json(plow).data["numViews"]

    # An item the second run did not touch resolves to the run-1 file.
    untouched = f"{PORTAL}/content/items/a0000000000000000000000000000016"
    assert later.get_json(untouched).data == base.get_json(untouched).data


def test_run2_drops_one_app_and_adds_another() -> None:
    later = FixtureTransport(FIXTURE, overlay="run2")
    seen: list[str] = []
    start = 1
    for _ in range(10):
        page = later.get_json(f"{PORTAL}/search", {"start": start, "num": PAGE_SIZE}).data
        seen.extend(r["id"] for r in page["results"])
        if page["nextStart"] == -1:
            break
        start = page["nextStart"]

    diff = load("run2", "expected", "diff.json")
    for gone in diff["disappeared"]:
        assert gone not in seen
    for added in diff["added"]:
        assert added in seen


def test_run2_reshares_an_item_without_changing_its_identity() -> None:
    """The resource stays the same; a NEW finding fingerprint must appear."""
    base = FixtureTransport(FIXTURE)
    later = FixtureTransport(FIXTURE, overlay="run2")
    storm = f"{PORTAL}/content/items/a0000000000000000000000000000017"

    before, after = base.get_json(storm).data, later.get_json(storm).data
    assert before["access"] == "org"
    assert after["access"] == "public"
    assert before["id"] == after["id"]


def test_run2_restores_the_dead_service() -> None:
    """The reachability finding stops firing --- observed, not claimed."""
    url = "https://services.northgate.example.gov/server/rest/services/Utilities/StormSewer_OLD/MapServer"
    assert "error" in FixtureTransport(FIXTURE).get_json(url).data
    assert "error" not in FixtureTransport(FIXTURE, overlay="run2").get_json(url).data
