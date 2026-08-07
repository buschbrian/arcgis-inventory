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
    assert "71 dependencies" in result.output
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


def test_audit_sharing_stays_silent_about_exposure_without_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ARCGIS_PORTAL_URL", raising=False)
    db = tmp_path / "inv.sqlite"
    fixture = Path(__file__).parent / "fixtures" / "northgate"
    runner.invoke(app, ["inventory", "--fixture", str(fixture), "--db", str(db)])
    runner.invoke(app, ["dependencies", "--db", str(db)])

    result = runner.invoke(app, ["audit-sharing", "--db", str(db)])
    assert result.exit_code == 0, result.output
    assert "public-app-private-dep" not in result.output
    assert "--probe" in result.output  # and it says how to find out


def test_audit_sharing_with_probe_finds_public_exposure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ARCGIS_PORTAL_URL", raising=False)
    db = tmp_path / "inv.sqlite"
    fixture = Path(__file__).parent / "fixtures" / "northgate"
    runner.invoke(app, ["inventory", "--fixture", str(fixture), "--db", str(db)])
    runner.invoke(app, ["dependencies", "--db", str(db)])

    result = runner.invoke(
        app, ["audit-sharing", "--db", str(db), "--probe", "--fixture", str(fixture)]
    )
    assert result.exit_code == 0, result.output
    assert "public-app-private-dep" in result.output
    assert "restricted" in result.output


def test_scan_reports_deprecated_technology(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ARCGIS_PORTAL_URL", raising=False)
    db = tmp_path / "inv.sqlite"
    fixture = Path(__file__).parent / "fixtures" / "northgate"
    runner.invoke(app, ["inventory", "--fixture", str(fixture), "--db", str(db)])

    result = runner.invoke(app, ["scan", "--db", str(db)])
    assert result.exit_code == 0, result.output
    assert "web-appbuilder-retiring" in result.output
    assert "critical" in result.output


def test_scan_can_filter_by_severity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ARCGIS_PORTAL_URL", raising=False)
    db = tmp_path / "inv.sqlite"
    fixture = Path(__file__).parent / "fixtures" / "northgate"
    runner.invoke(app, ["inventory", "--fixture", str(fixture), "--db", str(db)])

    result = runner.invoke(app, ["scan", "--db", str(db), "--severity", "critical"])
    assert result.exit_code == 0, result.output
    assert "web-appbuilder-retiring" in result.output
    assert "unused-and-stale" not in result.output  # low severity, filtered out


def test_recommend_reports_targets_with_reasoning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ARCGIS_PORTAL_URL", raising=False)
    db = tmp_path / "inv.sqlite"
    fixture = Path(__file__).parent / "fixtures" / "northgate"
    runner.invoke(app, ["inventory", "--fixture", str(fixture), "--db", str(db)])
    runner.invoke(app, ["dependencies", "--db", str(db)])

    result = runner.invoke(app, ["recommend", "--db", str(db), "--show", "3"])
    assert result.exit_code == 0, result.output
    assert "instant_app" in result.output
    assert "15 applications" in result.output
    # The reasoning, not just the label.
    assert "widgets" in result.output


def test_recommend_warns_when_the_graph_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ARCGIS_PORTAL_URL", raising=False)
    db = tmp_path / "inv.sqlite"
    fixture = Path(__file__).parent / "fixtures" / "northgate"
    runner.invoke(app, ["inventory", "--fixture", str(fixture), "--db", str(db)])

    result = runner.invoke(app, ["recommend", "--db", str(db)])
    assert result.exit_code == 0, result.output
    assert "dependencies" in result.output


def test_report_writes_both_formats(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ARCGIS_PORTAL_URL", raising=False)
    db = tmp_path / "inv.sqlite"
    out = tmp_path / "out"
    fixture = Path(__file__).parent / "fixtures" / "northgate"
    runner.invoke(app, ["inventory", "--fixture", str(fixture), "--db", str(db)])

    result = runner.invoke(app, ["report", "--db", str(db), "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert (out / "inventory-report.md").exists()
    assert (out / "inventory-report.html").exists()
    assert "9 Web AppBuilder apps" in result.output


def test_report_honours_the_format_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ARCGIS_PORTAL_URL", raising=False)
    db = tmp_path / "inv.sqlite"
    out = tmp_path / "out"
    fixture = Path(__file__).parent / "fixtures" / "northgate"
    runner.invoke(app, ["inventory", "--fixture", str(fixture), "--db", str(db)])

    result = runner.invoke(
        app, ["report", "--db", str(db), "--out", str(out), "--format", "markdown"]
    )
    assert result.exit_code == 0, result.output
    assert (out / "inventory-report.md").exists()
    assert not (out / "inventory-report.html").exists()


def test_report_rejects_an_unknown_format(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ARCGIS_PORTAL_URL", raising=False)
    db = tmp_path / "inv.sqlite"
    fixture = Path(__file__).parent / "fixtures" / "northgate"
    runner.invoke(app, ["inventory", "--fixture", str(fixture), "--db", str(db)])

    result = runner.invoke(app, ["report", "--db", str(db), "--format", "pdf"])
    assert result.exit_code == 2


def test_report_warns_about_its_own_gaps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """It gets forwarded to people who will not run the tool themselves."""
    monkeypatch.delenv("ARCGIS_PORTAL_URL", raising=False)
    db = tmp_path / "inv.sqlite"
    out = tmp_path / "out"
    fixture = Path(__file__).parent / "fixtures" / "northgate"
    runner.invoke(app, ["inventory", "--fixture", str(fixture), "--db", str(db)])

    result = runner.invoke(app, ["report", "--db", str(db), "--out", str(out)])
    assert "does not know" in result.output


def test_wab_export_writes_documentation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ARCGIS_PORTAL_URL", raising=False)
    db = tmp_path / "inv.sqlite"
    out = tmp_path / "wab"
    fixture = Path(__file__).parent / "fixtures" / "northgate"
    runner.invoke(app, ["inventory", "--fixture", str(fixture), "--db", str(db)])

    result = runner.invoke(app, ["wab-export", "--db", str(db), "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert "7" in result.output
    assert (out / "manifest.json").exists()
    # The two unreadable apps are surfaced, not silently missing.
    assert "unreadable" in result.output


def test_every_subcommand_is_implemented() -> None:
    """The placeholder era is over: nothing raises NotImplementedError.

    Kept as a guard --- a subcommand that silently does nothing is how you end
    up believing an org is clean, so any future addition must either work or
    fail loudly.
    """
    commands = [
        "init-db",
        "doctor",
        "inventory",
        "reprocess",
        "dependencies",
        "audit-sharing",
        "scan",
        "recommend",
        "report",
        "wab-export",
    ]
    listed = runner.invoke(app, ["--help"]).output
    for command in commands:
        assert command in listed
        result = runner.invoke(app, [command, "--help"])
        assert result.exit_code == 0, command
        assert not isinstance(result.exception, NotImplementedError)
