from __future__ import annotations

import pytest

from arcgis_inventory.config import load_config
from arcgis_inventory.errors import ConfigError

PORTAL = "https://northgate.example.gov/portal"


def test_portal_url_is_required() -> None:
    with pytest.raises(ConfigError, match="PORTAL_URL"):
        load_config(env={})


def test_trailing_slash_is_stripped() -> None:
    cfg = load_config(env={"ARCGIS_PORTAL_URL": PORTAL + "/", "ARCGIS_TOKEN": "t"})
    assert cfg.portal.url == PORTAL


def test_username_without_a_secret_is_refused_rather_than_falling_back_to_anonymous() -> None:
    with pytest.raises(ConfigError, match="anonymous"):
        load_config(env={"ARCGIS_PORTAL_URL": PORTAL, "ARCGIS_USERNAME": "svc_crawler"})


def test_password_without_a_username_is_refused() -> None:
    with pytest.raises(ConfigError, match="USERNAME"):
        load_config(env={"ARCGIS_PORTAL_URL": PORTAL, "ARCGIS_PASSWORD": "hunter2"})


def test_anonymous_is_allowed_when_nothing_is_configured() -> None:
    cfg = load_config(env={"ARCGIS_PORTAL_URL": PORTAL})
    assert cfg.portal.is_anonymous is True


def test_numeric_and_boolean_settings_are_parsed() -> None:
    cfg = load_config(
        env={
            "ARCGIS_PORTAL_URL": PORTAL,
            "ARCGIS_MAX_RPS": "2.5",
            "ARCGIS_PAGE_SIZE": "50",
            "ARCGIS_VERIFY_SSL": "false",
            "ARCGIS_PROBE_SERVICES": "yes",
        }
    )
    assert (cfg.max_rps, cfg.page_size) == (2.5, 50)
    assert cfg.portal.verify_ssl is False
    assert cfg.probe_services is True


@pytest.mark.parametrize(
    "env",
    [
        {"ARCGIS_PORTAL_URL": PORTAL, "ARCGIS_VERIFY_SSL": "maybe"},
        {"ARCGIS_PORTAL_URL": PORTAL, "ARCGIS_MAX_RPS": "fast"},
    ],
)
def test_unparseable_settings_raise(env: dict[str, str]) -> None:
    with pytest.raises(ConfigError):
        load_config(env=env)


def test_credentials_never_appear_in_a_repr() -> None:
    cfg = load_config(
        env={
            "ARCGIS_PORTAL_URL": PORTAL,
            "ARCGIS_USERNAME": "svc_crawler",
            "ARCGIS_PASSWORD": "hunter2",
        }
    )
    rendered = f"{cfg.portal!r} {cfg!r}"
    assert "hunter2" not in rendered
