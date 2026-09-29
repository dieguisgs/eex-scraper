"""Carga de configuracion desde config.toml."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import date
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_FILENAME = "config.toml"
CONFIG_ENV_VAR = "EEX_SCRAPER_CONFIG"


class ConfigError(Exception):
    """El fichero de configuracion tiene algo que no cuadra."""


@dataclass
class ApiConfig:
    market_data_url: str = "https://api.eex-group.com/pub/market-data"
    customise_url: str = "https://api.eex-group.com/pub/customise-widget"
    referer: str = "https://www.eex.com/"
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    )
    timeout_seconds: float = 45.0
    max_retries: int = 4
    backoff_seconds: float = 1.5


@dataclass
class ScrapeConfig:
    concurrency: int = 4
    # La API corta en ~60 peticiones/minuto (medido). Por encima de 1/s empiezan
    # los 429, asi que el ritmo -no la concurrencia- es el freno real.
    requests_per_second: float = 0.9
    burst: int = 20
    request_delay_seconds: float = 0.0
    max_lookback_days: int = 420
    min_refresh_hours: float = 12.0
    skip_empty: bool = True


@dataclass
class FilterConfig:
    commodities: list[str] = field(default_factory=list)
    areas: list[str] = field(default_factory=list)
    products: list[str] = field(default_factory=list)
    maturity_types: list[str] = field(default_factory=list)
    short_codes: list[str] = field(default_factory=list)
    pricings: list[str] = field(default_factory=lambda: ["F", "S", "I", "A"])
    exclude_commodities: list[str] = field(default_factory=list)
    exclude_areas: list[str] = field(default_factory=list)


@dataclass
class OutputConfig:
    directory: str = "output"
    table_dirname: str = "table_data"
    catalog_dirname: str = "catalog"
    state_dirname: str = "_state"
    delimiter: str = ","
    encoding: str = "utf-8-sig"

    @property
    def root(self) -> Path:
        return Path(self.directory)

    @property
    def table_dir(self) -> Path:
        return self.root / self.table_dirname

    @property
    def catalog_dir(self) -> Path:
        return self.root / self.catalog_dirname

    @property
    def state_dir(self) -> Path:
        return self.root / self.state_dirname

    def set_directory(self, directory: str | Path, base: Path) -> None:
        """Fija la ruta base: expande ~ y variables y resuelve relativas contra `base`."""
        self.directory = str(resolve_path(directory, base))


LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")


@dataclass
class LoggingConfig:
    # Carpeta de los logs. `{output}` se sustituye por la ruta base de salida,
    # asi que por defecto los logs siguen a los datos (tambien con -o).
    directory: str = "{output}/_logs"
    # Un fichero por dia; `{date}` es AAAA-MM-DD.
    filename: str = "eex-scraper_{date}.log"
    level: str = "INFO"
    keep_days: int = 60

    def validate(self) -> None:
        if self.level.upper() not in LOG_LEVELS:
            raise ConfigError(
                f"[logging].level tiene que ser uno de {', '.join(LOG_LEVELS)}, no {self.level!r}"
            )
        if any(sep in self.filename for sep in ("/", "\\")):
            raise ConfigError(
                "[logging].filename es solo el nombre del fichero; la carpeta va en directory"
            )
        if self.keep_days < 0:
            raise ConfigError("[logging].keep_days no puede ser negativo (0 = no borrar nunca)")


def resolve_path(value: str | Path, base: Path) -> Path:
    """Expande ~ y variables de entorno; una ruta relativa cuelga de `base`."""
    expanded = Path(os.path.expanduser(os.path.expandvars(str(value))))
    if not expanded.is_absolute():
        expanded = base / expanded
    return expanded.resolve()


@dataclass
class BrowserConfig:
    fallback_enabled: bool = True
    headless: bool = True
    browser: str = "chromium"
    page_url: str = "https://www.eex.com/en/market-data/market-data-hub"


@dataclass
class Config:
    api: ApiConfig = field(default_factory=ApiConfig)
    scrape: ScrapeConfig = field(default_factory=ScrapeConfig)
    filters: FilterConfig = field(default_factory=FilterConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    browser: BrowserConfig = field(default_factory=BrowserConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    source_path: Path | None = None

    @property
    def base_dir(self) -> Path:
        """Contra que se resuelven las rutas relativas: la carpeta del config (o cwd)."""
        return self.source_path.parent.resolve() if self.source_path else Path.cwd().resolve()

    @property
    def log_dir(self) -> Path:
        template = self.logging.directory.replace("{output}", str(self.output.root))
        return resolve_path(template, self.base_dir)

    def log_file(self, day: date) -> Path:
        return self.log_dir / self.logging.filename.replace("{date}", day.isoformat())

    @classmethod
    def load(cls, path: str | Path | None = None) -> Config:
        """Lee el config: la ruta dada, la de EEX_SCRAPER_CONFIG o un config.toml
        buscado hacia arriba desde la carpeta actual."""
        env = os.environ.get(CONFIG_ENV_VAR)
        if path:
            resolved = Path(path)
        elif env:
            resolved = Path(os.path.expanduser(os.path.expandvars(env)))
        else:
            resolved = _find_default_config()
        if resolved is None:
            cfg = cls()
            cfg.output.set_directory(cfg.output.directory, Path.cwd())
            return cfg
        if not resolved.exists():
            raise ConfigError(f"No existe el fichero de configuracion: {resolved}")

        try:
            with resolved.open("rb") as fh:
                raw: dict[str, Any] = tomllib.load(fh)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{resolved} no es un TOML valido: {exc}") from exc

        unknown_sections = set(raw) - {"api", "scrape", "filters", "output", "browser", "logging"}
        if unknown_sections:
            raise ConfigError(
                f"Secciones desconocidas en {resolved.name}: {', '.join(sorted(unknown_sections))}"
            )

        cfg = cls(
            api=_build(ApiConfig, raw.get("api"), "api"),
            scrape=_build(ScrapeConfig, raw.get("scrape"), "scrape"),
            filters=_build(FilterConfig, raw.get("filters"), "filters"),
            output=_build(OutputConfig, raw.get("output"), "output"),
            browser=_build(BrowserConfig, raw.get("browser"), "browser"),
            logging=_build(LoggingConfig, raw.get("logging"), "logging"),
            source_path=resolved,
        )
        cfg.logging.validate()
        # Rutas de salida relativas al directorio del config, no al cwd.
        cfg.output.set_directory(cfg.output.directory, resolved.parent.resolve())
        return cfg


def _find_default_config() -> Path | None:
    here = Path.cwd().resolve()
    for folder in [here, *here.parents]:
        candidate = folder / DEFAULT_CONFIG_FILENAME
        if candidate.exists():
            return candidate
    return None


def _build(cls: type, data: dict[str, Any] | None, section: str):
    """Instancia un dataclass validando las claves que trae el TOML."""
    if not is_dataclass(cls):  # pragma: no cover - guarda de programacion
        raise TypeError(f"{cls!r} no es un dataclass")
    if not data:
        return cls()

    known = {f.name: f for f in fields(cls)}
    unknown = set(data) - set(known)
    if unknown:
        cercanas = _suggest(sorted(unknown)[0], known)
        pista = f" (quiza quisiste decir: {cercanas})" if cercanas else ""
        raise ConfigError(
            f"Claves desconocidas en [{section}]: {', '.join(sorted(unknown))}{pista}"
        )

    # Las listas de filtros son faciles de escribir mal ("POWER" en vez de
    # ["POWER"]); mejor decirlo aqui que fallar en medio del scrapeo.
    for name, value in data.items():
        expected = known[name].type
        if "list" in str(expected) and not isinstance(value, list):
            raise ConfigError(
                f"[{section}].{name} tiene que ser una lista, no {type(value).__name__}"
            )

    return cls(**data)


def _suggest(word: str, candidates) -> str:
    from difflib import get_close_matches

    return ", ".join(get_close_matches(word, list(candidates), n=2, cutoff=0.6))
