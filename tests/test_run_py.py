"""Tests del lanzador `run.py`: se ejecuta como lo haria el usuario, en otro proceso."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
RUN_PY = PROJECT / "run.py"


def run_py(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != "EEX_SCRAPER_CONFIG"}
    return subprocess.run(
        [sys.executable, str(RUN_PY), *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )


def test_run_py_shows_help(tmp_path):
    result = run_py("--help", cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert "curves" in result.stdout


def test_run_py_uses_the_config_next_to_it_from_any_folder(tmp_path):
    """Lanzado desde otra carpeta, sigue usando el config.toml del proyecto."""
    result = run_py("info", cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert str(PROJECT / "config.toml") in result.stdout


def test_run_py_writes_output_and_log_where_the_config_says(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text(
        '[output]\ndirectory = "datos"\n[logging]\ndirectory = "registros"\n',
        encoding="utf-8",
    )
    result = run_py("--config", str(config), "curves", cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "datos" / "inventory.csv").exists()
    logs = list((tmp_path / "registros").glob("*.log"))
    assert len(logs) == 1
    assert "=== fin: curves | codigo 0" in logs[0].read_text(encoding="utf-8")


def test_run_py_propagates_the_exit_code(tmp_path):
    result = run_py("--config", str(tmp_path / "no-existe.toml"), "info", cwd=tmp_path)
    assert result.returncode == 2
    assert "No existe" in result.stderr
