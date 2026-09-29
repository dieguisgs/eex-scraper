"""Tests de configuracion, fichero de estado y limitador de ritmo."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from eex_scraper.config import Config, ConfigError
from eex_scraper.fetchers import RateLimiter
from eex_scraper.state import ScrapeState

# ------------------------------------------------------------- configuracion


def write_config(tmp_path, text: str):
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_valid_config_overrides_defaults(tmp_path):
    path = write_config(tmp_path, '[scrape]\nconcurrency = 16\n[filters]\nareas = ["DE"]\n')
    cfg = Config.load(path)
    assert cfg.scrape.concurrency == 16
    assert cfg.filters.areas == ["DE"]
    # Lo no declarado conserva el valor por defecto.
    assert cfg.scrape.requests_per_second == Config().scrape.requests_per_second


def test_unknown_key_is_reported_with_a_suggestion(tmp_path):
    path = write_config(tmp_path, "[scrape]\nconcurrenci = 4\n")
    with pytest.raises(ConfigError, match="concurrency"):
        Config.load(path)


def test_unknown_section_is_reported(tmp_path):
    path = write_config(tmp_path, "[scrapping]\nx = 1\n")
    with pytest.raises(ConfigError, match="Secciones desconocidas"):
        Config.load(path)


def test_scalar_where_a_list_is_expected_is_reported(tmp_path):
    path = write_config(tmp_path, '[filters]\ncommodities = "POWER"\n')
    with pytest.raises(ConfigError, match="tiene que ser una lista"):
        Config.load(path)


def test_invalid_toml_is_reported(tmp_path):
    path = write_config(tmp_path, "[scrape\nx = 1\n")
    with pytest.raises(ConfigError, match="no es un TOML valido"):
        Config.load(path)


def test_missing_config_file_is_reported(tmp_path):
    with pytest.raises(ConfigError, match="No existe"):
        Config.load(tmp_path / "no-existe.toml")


def test_output_path_is_relative_to_the_config_file(tmp_path):
    path = write_config(tmp_path, '[output]\ndirectory = "datos"\n')
    cfg = Config.load(path)
    assert cfg.output.root == (tmp_path / "datos").resolve()


# -------------------------------------------------------------------- estado


def test_fresh_entry_is_skipped_and_stale_one_is_not(tmp_path):
    cfg = Config()
    cfg.output.directory = str(tmp_path)
    state = ScrapeState.load(cfg.output)
    state.record_success(
        "K", csv_path="a.csv", rows=5, added=5, updated=0, last_trade_date="2026-09-21"
    )
    assert state.is_fresh("K", 12.0) is True
    assert state.is_fresh("K", 0.0) is False  # 0 horas = no saltar nunca
    assert state.is_fresh("DESCONOCIDO", 12.0) is False


def test_old_entry_is_not_fresh(tmp_path):
    cfg = Config()
    cfg.output.directory = str(tmp_path)
    state = ScrapeState.load(cfg.output)
    old = (datetime.now(UTC) - timedelta(hours=30)).isoformat()
    state.entries["K"] = {"status": "ok", "lastScrapedAt": old}
    assert state.is_fresh("K", 12.0) is False


def test_entry_with_error_is_always_retried(tmp_path):
    cfg = Config()
    cfg.output.directory = str(tmp_path)
    state = ScrapeState.load(cfg.output)
    state.record_error("K", "boom")
    assert state.is_fresh("K", 12.0) is False


def test_state_survives_a_roundtrip(tmp_path):
    cfg = Config()
    cfg.output.directory = str(tmp_path)
    state = ScrapeState.load(cfg.output)
    state.record_success(
        "K", csv_path="a.csv", rows=3, added=3, updated=0, last_trade_date="2026-09-21"
    )
    state.save()
    assert ScrapeState.load(cfg.output).last_trade_date("K") == "2026-09-21"


def test_corrupt_state_file_does_not_crash(tmp_path):
    cfg = Config()
    cfg.output.directory = str(tmp_path)
    (tmp_path / cfg.output.state_dirname).mkdir(parents=True)
    (tmp_path / cfg.output.state_dirname / "scrape_state.json").write_text(
        "{ roto", encoding="utf-8"
    )
    assert ScrapeState.load(cfg.output).entries == {}


def test_state_is_written_atomically(tmp_path):
    cfg = Config()
    cfg.output.directory = str(tmp_path)
    state = ScrapeState.load(cfg.output)
    state.record_success("K", csv_path=None, rows=0, added=0, updated=0, last_trade_date=None)
    state.save()
    written = json.loads(state.path.read_text(encoding="utf-8"))
    assert written["version"] == 1
    assert not list(state.path.parent.glob("*.tmp"))  # sin temporales huerfanos


# ----------------------------------------------------------- limitador de ritmo


def test_limiter_spends_the_burst_then_throttles():
    async def run():
        limiter = RateLimiter(requests_per_second=100.0, burst=3)
        start = time.monotonic()
        for _ in range(3):  # el burst sale sin esperar
            await limiter.acquire()
        burst_elapsed = time.monotonic() - start
        await limiter.acquire()  # la cuarta ya paga el ritmo
        return burst_elapsed, time.monotonic() - start

    burst_elapsed, total = asyncio.run(run())
    assert burst_elapsed < 0.05
    assert total >= 0.01


def test_penalty_blocks_every_caller():
    async def run():
        limiter = RateLimiter(requests_per_second=1000.0, burst=50)
        await limiter.penalize(0.2)
        start = time.monotonic()
        await asyncio.gather(*(limiter.acquire() for _ in range(5)))
        return time.monotonic() - start

    assert asyncio.run(run()) >= 0.15


def test_penalty_empties_the_bucket():
    async def run():
        limiter = RateLimiter(requests_per_second=1000.0, burst=50)
        await limiter.penalize(0.0)
        return limiter._tokens

    assert asyncio.run(run()) == 0.0


# ---------------------------------------------------------------- ruta base


def test_output_directory_expands_environment_variables(tmp_path, monkeypatch):
    monkeypatch.setenv("EEX_TEST_BASE", str(tmp_path / "base"))
    path = write_config(tmp_path, '[output]\ndirectory = "$EEX_TEST_BASE/eex"\n')
    assert Config.load(path).output.root == (tmp_path / "base" / "eex").resolve()


def test_absolute_output_directory_is_kept(tmp_path):
    target = (tmp_path / "otra" / "sitio").as_posix()
    path = write_config(tmp_path, f'[output]\ndirectory = "{target}"\n')
    assert Config.load(path).output.root == (tmp_path / "otra" / "sitio").resolve()


def test_config_can_come_from_environment_variable(tmp_path, monkeypatch):
    path = write_config(tmp_path, '[output]\ndirectory = "desde_env"\n')
    monkeypatch.setenv("EEX_SCRAPER_CONFIG", str(path))
    monkeypatch.chdir(tmp_path.parent)  # lejos del config: no se encontraria buscando
    assert Config.load().output.root == (tmp_path / "desde_env").resolve()


def test_explicit_path_wins_over_environment_variable(tmp_path, monkeypatch):
    env_cfg = tmp_path / "env"
    env_cfg.mkdir()
    monkeypatch.setenv("EEX_SCRAPER_CONFIG", str(write_config(env_cfg, "")))
    explicit = write_config(tmp_path, '[output]\ndirectory = "explicito"\n')
    assert Config.load(explicit).output.root == (tmp_path / "explicito").resolve()


# --------------------------------------------------------------------- logs


def test_logs_default_to_the_base_directory(tmp_path):
    path = write_config(tmp_path, '[output]\ndirectory = "datos"\n')
    cfg = Config.load(path)
    assert cfg.log_dir == (tmp_path / "datos" / "_logs").resolve()


def test_log_directory_follows_output_override(tmp_path):
    cfg = Config.load(write_config(tmp_path, ""))
    cfg.output.set_directory(tmp_path / "otra", tmp_path)
    assert cfg.log_dir == (tmp_path / "otra" / "_logs").resolve()


def test_log_directory_can_be_its_own_path(tmp_path):
    path = write_config(tmp_path, '[logging]\ndirectory = "mis_logs"\n')
    assert Config.load(path).log_dir == (tmp_path / "mis_logs").resolve()


def test_absolute_log_directory_is_kept(tmp_path):
    target = (tmp_path / "abs" / "logs").as_posix()
    path = write_config(tmp_path, f'[logging]\ndirectory = "{target}"\n')
    assert Config.load(path).log_dir == (tmp_path / "abs" / "logs").resolve()


def test_log_file_name_has_the_date(tmp_path):
    path = write_config(tmp_path, '[logging]\nfilename = "eex_{date}.txt"\n')
    cfg = Config.load(path)
    assert cfg.log_file(date(2026, 9, 29)).name == "eex_2026-09-29.txt"


def test_invalid_log_level_is_reported(tmp_path):
    path = write_config(tmp_path, '[logging]\nlevel = "VERBOSE"\n')
    with pytest.raises(ConfigError, match="level"):
        Config.load(path)


def test_log_filename_cannot_contain_a_folder(tmp_path):
    path = write_config(tmp_path, '[logging]\nfilename = "sub/eex_{date}.log"\n')
    with pytest.raises(ConfigError, match="filename"):
        Config.load(path)


def test_project_config_toml_is_valid():
    """El config.toml que se distribuye tiene que cargar sin errores."""
    project_config = Path(__file__).resolve().parent.parent / "config.toml"
    cfg = Config.load(project_config)
    assert cfg.logging.directory == "{output}/_logs"
