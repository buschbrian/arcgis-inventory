"""Fingerprint stability protects the most important property in the schema:
authored triage state survives a re-crawl.
"""

from __future__ import annotations

import pytest

from arcgis_inventory.fingerprint import finding_fingerprint

ITEM = "a0000000000000000000000000000005"
DEP = "https://services.example.gov/server/rest/services/Parcels/FeatureServer"


def test_same_inputs_give_the_same_fingerprint() -> None:
    a = finding_fingerprint("public-app-private-dep", ITEM, evidence={"dep": DEP})
    b = finding_fingerprint("public-app-private-dep", ITEM, evidence={"dep": DEP})
    assert a == b


def test_evidence_key_order_does_not_matter() -> None:
    a = finding_fingerprint("r", ITEM, evidence={"dep": "x", "relation": "basemap"})
    b = finding_fingerprint("r", ITEM, evidence={"relation": "basemap", "dep": "x"})
    assert a == b


def test_list_order_in_evidence_does_not_matter() -> None:
    a = finding_fingerprint("r", ITEM, evidence={"layers": ["b", "a"]})
    b = finding_fingerprint("r", ITEM, evidence={"layers": ["a", "b"]})
    assert a == b


def test_different_rule_resource_or_evidence_gives_a_different_fingerprint() -> None:
    base = finding_fingerprint("r", ITEM, evidence={"dep": "x"})
    assert base != finding_fingerprint("other-rule", ITEM, evidence={"dep": "x"})
    assert base != finding_fingerprint("r", ITEM.replace("5", "6"), evidence={"dep": "x"})
    assert base != finding_fingerprint("r", ITEM, evidence={"dep": "y"})


def test_no_evidence_is_distinct_from_empty_evidence_is_not() -> None:
    assert finding_fingerprint("r", ITEM) == finding_fingerprint("r", ITEM, evidence={})


def test_fingerprint_shape() -> None:
    fp = finding_fingerprint("r", ITEM)
    assert len(fp) == 32
    assert all(c in "0123456789abcdef" for c in fp)


@pytest.mark.parametrize(
    ("rule", "identity"),
    [("", ITEM), ("r", "")],
)
def test_missing_identity_is_an_error_not_a_hash_of_nothing(rule: str, identity: str) -> None:
    with pytest.raises(ValueError):
        finding_fingerprint(rule, identity)
