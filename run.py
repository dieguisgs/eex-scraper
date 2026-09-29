"""Lanzador del scraper: `python run.py` hace la descarga de cada dia.

    python run.py                      descarga todo, fusiona con lo guardado y
                                       regenera curvas (= python run.py run)
    python run.py info                 que hay descargado
    python run.py run -C POWER         cualquier comando y opcion (ver --help)
    python run.py -o D:/datos/eex      otra ruta base solo para esta ejecucion

La ruta base de salida se configura en config.toml ([output] directory). Este
fichero usa siempre el config.toml que tiene al lado, se lance desde donde se
lance (salvo que se pase --config o exista EEX_SCRAPER_CONFIG).

Si el Python con el que se lanza no tiene las dependencias instaladas, se
relanza solo con `uv run`, que usa el entorno del proyecto (.venv).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent


def main() -> int:
    args = sys.argv[1:]
    try:
        import httpx  # noqa: F401
        import typer  # noqa: F401
    except ImportError:
        return _relaunch_with_uv(args)

    sys.path.insert(0, str(PROJECT / "src"))
    os.environ.setdefault("EEX_SCRAPER_CONFIG", str(PROJECT / "config.toml"))

    from eex_scraper.cli import app

    try:
        app(args=args, prog_name="run.py")
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    return 0


def _relaunch_with_uv(args: list[str]) -> int:
    if os.environ.get("EEX_SCRAPER_RELAUNCHED"):
        print("Faltan dependencias incluso dentro de `uv run`; prueba `uv sync`.", file=sys.stderr)
        return 1
    uv = shutil.which("uv") or str(Path.home() / ".local" / "bin" / "uv.exe")
    if not Path(uv).exists() and not shutil.which(uv):
        print(
            "Este Python no tiene las dependencias y no encuentro `uv`.\n"
            "Instala uv (https://docs.astral.sh/uv/) o ejecuta: uv run python run.py",
            file=sys.stderr,
        )
        return 1
    env = {**os.environ, "EEX_SCRAPER_RELAUNCHED": "1"}
    cmd = [uv, "run", "--project", str(PROJECT), "python", str(Path(__file__).resolve()), *args]
    return subprocess.call(cmd, env=env)


if __name__ == "__main__":
    sys.exit(main())
