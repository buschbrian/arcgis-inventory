from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from arcgis_inventory.cli import app

runner = CliRunner()


def test_help_states_what_the_tool_does_not_do() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    # The README and the help text both lead with this because it is the first
    # question every ArcGIS admin asks, and the answer disappoints them.
    assert "does NOT convert" in result.output


def test_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "arcgis-inventory" in result.output


def test_init_db_creates_a_database(tmp_path: Path) -> None:
    db = tmp_path / "inv.sqlite"
    result = runner.invoke(app, ["init-db", "--db", str(db)])
    assert result.exit_code == 0, result.output
    assert db.exists()

    again = runner.invoke(app, ["init-db", "--db", str(db)])
    assert again.exit_code == 0
    assert "verified" in again.output


def test_doctor_reports_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARCGIS_PORTAL_URL", "https://northgate.example.gov/portal")
    monkeypatch.setenv("ARCGIS_TOKEN", "fixture-token")
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "northgate.example.gov" in result.output
    assert "fixture-token" not in result.output


def test_doctor_fails_loudly_without_a_portal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ARCGIS_PORTAL_URL", raising=False)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 2


def test_inventory_crawls_a_fixture_without_network_or_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ARCGIS_PORTAL_URL", raising=False)
    db = tmp_path / "inv.sqlite"
    fixture = Path(__file__).parent / "fixtures" / "northgate"

    result = runner.invoke(app, ["inventory", "--fixture", str(fixture), "--db", str(db)])
    assert result.exit_code == 0, result.output
    assert "30 items" in result.output
    assert "web_appbuilder" in result.output
    assert db.exists()


def test_reprocess_reports_that_nothing_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ARCGIS_PORTAL_URL", raising=False)
    db = tmp_path / "inv.sqlite"
    fixture = Path(__file__).parent / "fixtures" / "northgate"
    runner.invoke(app, ["inventory", "--fixture", str(fixture), "--db", str(db)])

    result = runner.invoke(app, ["reprocess", "--db", str(db)])
    assert result.exit_code == 0, result.output
    assert "30 items" in result.output
    assert "0" in result.output  # 0 classifications changed


def test_dependencies_builds_the_graph_after_a_crawl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ARCGIS_PORTAL_URL", raising=False)
    db = tmp_path / "inv.sqlite"
    fixture = Path(__file__).parent / "fixtures" / "northgate"
    runner.invoke(app, ["inventory", "--fixture", str(fixture), "--db", str(db)])

    result = runner.invoke(app, ["dependencies", "--db", str(db)])
    assert result.exit_code == 0, result.output
    assert "63 dependencies" in result.output
    assert "operational_layer" in result.output


def test_dependencies_before_any_crawl_fails_clearly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ARCGIS_PORTAL_URL", raising=False)
    result = runner.invoke(app, ["dependencies", "--db", str(tmp_path / "missing.sqlite")])
    assert result.exit_code == 2


def test_reprocess_before_any_crawl_fails_clearly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ARCGIS_PORTAL_URL", raising=False)
    result = runner.invoke(app, ["reprocess", "--db", str(tmp_path / "missing.sqlite")])
    assert result.exit_code == 2
    assert "inventory" in result.output.lower()


def test_inventory_without_a_portal_fails_rather_than_crawling_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ARCGIS_PORTAL_URL", raising=False)
    result = runner.invoke(app, ["inventory", "--db", str(tmp_path / "x.sqlite")])
    assert result.exit_code == 2


@pytest.mark.parametrize(
    "command",
    ["scan", "audit-sharing", "recommend", "report"],
)
def test_unimplemented_commands_fail_rather_than_quietly_succeeding(command: str) -> None:
    """A crawl command that silently does nothing is how you conclude an org is clean."""
    result = runner.invoke(app, [command])
    assert result.exit_code != 0
    assert isinstance(result.exception, NotImplementedError)
