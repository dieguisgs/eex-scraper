"""Estado de scrapeo: que contrato se bajo, cuando y con que resultado.

Es lo que permite al modo incremental saltarse lo ya scrapeado sin tocar la
red. Vive en output/_state/scrape_state.json y se puede borrar sin perder
datos: los CSV siguen siendo la fuente de verdad.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

STATE_FILENAME = "scrape_state.json"
STATE_VERSION = 1


class ScrapeState:
    def __init__(self, path: Path, entries: dict[str, dict[str, Any]] | None = None) -> None:
        self.path = path
        self.entries: dict[str, dict[str, Any]] = entries or {}

    # ------------------------------------------------------------ carga/guardado

    @classmethod
    def load(cls, output_cfg) -> ScrapeState:
        path = output_cfg.state_dir / STATE_FILENAME
        if not path.exists():
            return cls(path)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # Un estado corrupto no debe romper la ejecucion: se re-scrapea todo.
            return cls(path)
        return cls(path, raw.get("contracts", {}))

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": STATE_VERSION,
            "updatedAt": _now_iso(),
            "contracts": self.entries,
        }
        fd, tmp_name = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        os.close(fd)
        tmp_path = Path(tmp_name)
        try:
            tmp_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
            tmp_path.replace(self.path)
        finally:
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)

    # ------------------------------------------------------------------ consulta

    def get(self, key: str) -> dict[str, Any] | None:
        return self.entries.get(key)

    def is_fresh(self, key: str, min_refresh_hours: float) -> bool:
        """True si el contrato se scrapeo con exito hace menos de N horas."""
        if min_refresh_hours <= 0:
            return False
        entry = self.entries.get(key)
        if not entry or entry.get("status") == "error":
            return False
        stamp = _parse_iso(entry.get("lastScrapedAt"))
        if stamp is None:
            return False
        return datetime.now(UTC) - stamp < timedelta(hours=min_refresh_hours)

    def last_trade_date(self, key: str) -> str | None:
        entry = self.entries.get(key)
        return entry.get("lastTradeDate") if entry else None

    # ---------------------------------------------------------------- escritura

    def record_success(
        self,
        key: str,
        *,
        csv_path: str | None,
        rows: int,
        added: int,
        updated: int,
        last_trade_date: str | None,
    ) -> None:
        self.entries[key] = {
            "status": "empty" if rows == 0 else "ok",
            "lastScrapedAt": _now_iso(),
            "lastTradeDate": last_trade_date,
            "rows": rows,
            "lastAdded": added,
            "lastUpdated": updated,
            "csv": csv_path,
        }

    def record_error(self, key: str, message: str) -> None:
        previous = self.entries.get(key, {})
        self.entries[key] = {
            **previous,
            "status": "error",
            "lastErrorAt": _now_iso(),
            "lastError": message[:500],
        }


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


LOCK_FILENAME = "run.lock"


class AlreadyRunningError(RuntimeError):
    """Otra ejecucion del scraper tiene el bloqueo de esta carpeta de salida."""


class RunLock:
    """Impide que dos ejecuciones escriban a la vez en la misma carpeta de salida.

    Usa un bloqueo del sistema operativo sobre output/_state/run.lock, no la
    mera existencia del fichero: si el proceso muere (corte de luz, kill), el
    sistema libera el bloqueo solo y la siguiente ejecucion arranca sin mas.
    """

    def __init__(self, output_cfg) -> None:
        self.path = output_cfg.state_dir / LOCK_FILENAME
        self._fh = None

    def __enter__(self) -> RunLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = self.path.open("a+b")
        try:
            _lock(fh)
        except OSError:
            fh.close()
            raise AlreadyRunningError(
                f"Ya hay otra ejecucion en marcha sobre {self.path.parent.parent}"
            ) from None
        self._fh = fh
        return self

    def __exit__(self, *exc) -> None:
        if self._fh is not None:
            try:
                _unlock(self._fh)
            finally:
                self._fh.close()
                self._fh = None


if os.name == "nt":
    import msvcrt

    def _lock(fh) -> None:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock(fh) -> None:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _lock(fh) -> None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fh) -> None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
