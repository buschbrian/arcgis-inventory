"""URL normalization is fifty lines everything downstream depends on."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from arcgis_inventory.urls import normalize_url, same_service

BASE = "https://services.example.gov/server/rest/services/Public/Parcels/FeatureServer"


def test_scheme_and_host_are_lowercased_and_scheme_is_canonicalized() -> None:
    a = normalize_url(
        "HTTP://Services.Example.GOV/server/rest/services/Public/Parcels/FeatureServer"
    )
    assert a.url == BASE
    assert a.host == "services.example.gov"
    assert a.is_https is False


def test_https_is_recorded_but_does_not_split_the_node() -> None:
    over_http = normalize_url(BASE.replace("https://", "http://"))
    over_https = normalize_url(BASE)
    assert over_http.url == over_https.url
    assert (over_http.is_https, over_https.is_https) == (False, True)


def test_default_ports_are_stripped_but_others_are_kept() -> None:
    assert normalize_url("https://host.example.gov:443/rest/services/X/MapServer").url == (
        "https://host.example.gov/rest/services/X/MapServer"
    )
    assert normalize_url("http://host.example.gov:80/rest/services/X/MapServer").url == (
        "https://host.example.gov/rest/services/X/MapServer"
    )
    assert ":6443" in normalize_url("https://host.example.gov:6443/rest/services/X/MapServer").url


def test_case_is_preserved_after_rest_services() -> None:
    got = normalize_url("https://H.example.gov/SERVER/REST/SERVICES/Public/Storm_Drain/MapServer")
    assert got.url == "https://h.example.gov/server/rest/services/Public/Storm_Drain/MapServer"


def test_layer_index_is_split_off_not_kept_in_the_url() -> None:
    zero = normalize_url(f"{BASE}/0")
    three = normalize_url(f"{BASE}/3")
    assert zero.url == three.url == BASE
    assert (zero.layer_index, three.layer_index) == (0, 3)


def test_service_type_is_detected_and_case_canonicalized() -> None:
    got = normalize_url("https://h.example.gov/server/rest/services/Public/Addr/featureserver/2")
    assert got.service_type == "FeatureServer"
    assert got.url.endswith("/FeatureServer")


def test_query_and_fragment_are_not_part_of_identity() -> None:
    assert normalize_url(f"{BASE}/0?f=json&token=SECRET#frag").url == BASE


def test_trailing_slash_is_stripped() -> None:
    assert normalize_url(f"{BASE}/") == normalize_url(BASE)


def test_protocol_relative_urls_are_accepted() -> None:
    assert normalize_url("//h.example.gov/rest/services/X/MapServer").host == "h.example.gov"


def test_a_bare_numeric_path_is_not_mistaken_for_a_layer_index() -> None:
    got = normalize_url("https://h.example.gov/tiles/12")
    assert got.url == "https://h.example.gov/tiles/12"
    assert got.layer_index is None


@pytest.mark.parametrize(
    "bad", ["", "   ", "ftp://h.example.gov/x", "not a url", "/rest/services/X"]
)
def test_unusable_input_raises_rather_than_guessing(bad: str) -> None:
    with pytest.raises(ValueError):
        normalize_url(bad)


def test_same_service_ignores_scheme_and_layer() -> None:
    assert same_service(f"{BASE}/0", BASE.replace("https", "http") + "/7/")


@given(
    scheme=st.sampled_from(["http", "https", "HTTPS"]),
    host=st.sampled_from(["h.example.gov", "H.Example.Gov", "portal.example.com"]),
    folder=st.sampled_from(["Public", "public", "Storm_Drain"]),
    service=st.sampled_from(["FeatureServer", "MapServer", "ImageServer"]),
    layer=st.integers(min_value=0, max_value=99) | st.none(),
    slash=st.booleans(),
)
def test_normalization_is_idempotent(
    scheme: str, host: str, folder: str, service: str, layer: int | None, slash: bool
) -> None:
    url = f"{scheme}://{host}/server/rest/services/{folder}/Thing/{service}"
    if layer is not None:
        url += f"/{layer}"
    if slash:
        url += "/"

    once = normalize_url(url)
    twice = normalize_url(once.url)
    assert twice.url == once.url
    # Re-normalizing a canonical URL must not resurrect a layer index.
    assert twice.layer_index is None
    assert twice.service_type == once.service_type
