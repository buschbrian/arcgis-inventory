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


@pytest.mark.parametrize(
    "command",
    ["inventory", "dependencies", "scan", "audit-sharing", "recommend", "report", "reprocess"],
)
def test_unimplemented_commands_fail_rather_than_quietly_succeeding(command: str) -> None:
    """A crawl command that silently does nothing is how you conclude an org is clean."""
    result = runner.invoke(app, [command])
    assert result.exit_code != 0
    assert isinstance(result.exception, NotImplementedError)
