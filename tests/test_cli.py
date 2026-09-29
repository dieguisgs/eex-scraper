"""Tests de la linea de comandos y del log: comandos, rutas, niveles, rotacion y fallos.

Ninguno toca la red: `scrape` y la descarga del catalogo se sustituyen por
versiones falsas.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from eex_scraper import cli
from eex_scraper.config import Config
from eex_scraper.models import Contract
from eex_scraper.scraper import Mode, ScrapeStats
from eex_scraper.state import RunLock

runner = CliRunner()


def make_config(tmp_path: Path, extra: str = "") -> Path:
    path = tmp_path / "config.toml"
    path.write_text('[output]\ndirectory = "datos"\n' + extra, encoding="utf-8")
    return path


def invoke(config: Path, *args: str):
    return runner.invoke(cli.app, ["--config", str(config), *args])


def only_log(folder: Path) -> str:
    logs = sorted(folder.glob("*.log"))
    assert len(logs) == 1, logs
    return logs[0].read_text(encoding="utf-8")


@pytest.fixture
def fake_scrape(monkeypatch):
    """Sustituye el scrapeo real; guarda con que modo se llamo."""
    calls: list[Mode] = []

    async def fake(cfg, mode, **kwargs):
        calls.append(mode)
        return ScrapeStats(mode=mode, total=3, written=2, unchanged=1, rows_added=10)

    monkeypatch.setattr(cli, "scrape", fake)
    return calls


# ------------------------------------------------------------------ comandos


def test_help_lists_all_commands(tmp_path):
    result = invoke(make_config(tmp_path), "--help")
    assert result.exit_code == 0
    for command in ("run", "full", "catalog", "curves", "migrate", "info"):
        assert command in result.output


def test_no_command_means_run(tmp_path, fake_scrape):
    result = invoke(make_config(tmp_path))
    assert result.exit_code == 0, result.output
    assert fake_scrape == [Mode.RUN]


def test_full_uses_full_mode(tmp_path, fake_scrape):
    assert invoke(make_config(tmp_path), "full").exit_code == 0
    assert fake_scrape == [Mode.FULL]


def test_run_prints_summary(tmp_path, fake_scrape):
    result = invoke(make_config(tmp_path), "run")
    assert "filas nuevas            : 10" in result.output


def test_info_shows_where_data_and_logs_are(tmp_path):
    result = invoke(make_config(tmp_path, '[logging]\ndirectory = "L"\n'), "info")
    assert result.exit_code == 0
    assert str((tmp_path / "datos").resolve()) in result.output
    assert str((tmp_path / "L").resolve()) in result.output


# ----------------------------------------------------------------------- log


def test_run_log_has_start_summary_and_end(tmp_path, fake_scrape):
    invoke(make_config(tmp_path), "run")
    log = only_log(tmp_path / "datos" / "_logs")
    assert "=== inicio: run" in log
    assert "filas nuevas            : 10" in log
    assert "=== fin: run | codigo 0" in log


def test_log_file_is_named_by_day(tmp_path, fake_scrape):
    invoke(make_config(tmp_path), "run")
    (log,) = (tmp_path / "datos" / "_logs").glob("*.log")
    assert log.name == f"eex-scraper_{time.strftime('%Y-%m-%d')}.log"


def test_log_goes_to_the_directory_in_the_config(tmp_path):
    config = make_config(tmp_path, '[logging]\ndirectory = "logs_aparte"\n')
    assert invoke(config, "curves").exit_code == 0
    assert "=== inicio: curves" in only_log(tmp_path / "logs_aparte")
    assert not (tmp_path / "datos" / "_logs").exists()


def test_output_override_moves_data_and_logs(tmp_path):
    config = make_config(tmp_path)
    other = tmp_path / "otra"
    result = runner.invoke(cli.app, ["--config", str(config), "-o", str(other), "curves"])
    assert result.exit_code == 0, result.output
    assert (other / "inventory.csv").exists()
    assert "=== inicio: curves" in only_log(other / "_logs")
    assert not (tmp_path / "datos").exists()


def test_every_working_command_logs(tmp_path, monkeypatch):
    async def fake_catalog(cfg, *, force_browser):
        return [Contract("DEBY", "202801", "Year", "POWER", "F", "DE", "Base")]

    monkeypatch.setattr(cli, "_load_catalog_only", fake_catalog)
    config = make_config(tmp_path)
    for command in ("catalog", "curves", "migrate"):
        assert invoke(config, command).exit_code == 0

    log = only_log(tmp_path / "datos" / "_logs")
    for command in ("catalog", "curves", "migrate"):
        assert f"=== fin: {command} | codigo 0" in log
    assert "Catalogo: 1 contratos" in log


def test_info_does_not_write_a_log(tmp_path):
    invoke(make_config(tmp_path), "info")
    assert not (tmp_path / "datos" / "_logs").exists()


def test_warning_level_skips_routine_messages(tmp_path, fake_scrape):
    invoke(make_config(tmp_path, '[logging]\nlevel = "WARNING"\n'), "run")
    log = only_log(tmp_path / "datos" / "_logs")
    assert "inicio" not in log
    assert "filas nuevas" not in log


def test_crash_is_logged_with_its_traceback(tmp_path, monkeypatch):
    async def broken(cfg, mode, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(cli, "scrape", broken)
    result = invoke(make_config(tmp_path), "run")
    assert result.exit_code != 0
    log = only_log(tmp_path / "datos" / "_logs")
    assert "Traceback" in log
    assert "RuntimeError: boom" in log
    assert "=== fin: run | codigo 1" in log


def test_second_run_is_refused_and_logged(tmp_path, fake_scrape):
    config = make_config(tmp_path)
    with RunLock(Config.load(config).output):
        result = invoke(config, "curves")
    assert result.exit_code == 3
    log = only_log(tmp_path / "datos" / "_logs")
    assert "Ya hay otra ejecucion en marcha" in log
    assert "codigo 3" in log


# ---------------------------------------------------------- rotacion y fallos


def _age(path: Path, days: int) -> None:
    old = time.time() - days * 86400
    os.utime(path, (old, old))


def test_old_logs_are_deleted_and_the_rest_kept(tmp_path):
    logs = tmp_path / "datos" / "_logs"
    logs.mkdir(parents=True)
    old = logs / "eex-scraper_2020-01-01.log"
    old_pid = logs / "eex-scraper_2020-01-01_1234.log"
    recent = logs / "eex-scraper_2026-09-01.log"
    unrelated = logs / "notas.txt"
    for f in (old, old_pid, recent, unrelated):
        f.write_text("x", encoding="utf-8")
    for f in (old, old_pid, unrelated):
        _age(f, 90)
    _age(recent, 5)

    invoke(make_config(tmp_path, "[logging]\nkeep_days = 60\n"), "curves")

    assert not old.exists()
    assert not old_pid.exists()
    assert recent.exists()
    assert unrelated.exists()  # no es un log del scraper


def test_keep_days_zero_never_deletes(tmp_path):
    logs = tmp_path / "datos" / "_logs"
    logs.mkdir(parents=True)
    old = logs / "eex-scraper_2020-01-01.log"
    old.write_text("x", encoding="utf-8")
    _age(old, 400)
    invoke(make_config(tmp_path, "[logging]\nkeep_days = 0\n"), "curves")
    assert old.exists()


def test_busy_log_falls_back_to_a_file_of_its_own(tmp_path, monkeypatch):
    """Si otro proceso tiene el log del dia bloqueado, se usa uno con el PID."""
    real = logging.FileHandler

    def picky(path, *args, **kwargs):
        if str(os.getpid()) not in Path(path).name:
            raise PermissionError("bloqueado")
        return real(path, *args, **kwargs)

    monkeypatch.setattr(cli.logging, "FileHandler", picky)
    assert invoke(make_config(tmp_path), "curves").exit_code == 0
    (log,) = (tmp_path / "datos" / "_logs").glob("*.log")
    assert str(os.getpid()) in log.name


def test_command_still_works_when_no_log_can_be_opened(tmp_path, monkeypatch):
    def never(*args, **kwargs):
        raise PermissionError("sin permiso")

    monkeypatch.setattr(cli.logging, "FileHandler", never)
    result = invoke(make_config(tmp_path), "curves")
    assert result.exit_code == 0
    assert (tmp_path / "datos" / "inventory.csv").exists()


def test_log_handlers_are_removed_after_each_command(tmp_path, fake_scrape):
    before = list(logging.getLogger().handlers)
    invoke(make_config(tmp_path), "run")
    after = [h for h in logging.getLogger().handlers if h not in before]
    assert not [h for h in after if isinstance(h, logging.FileHandler)]
