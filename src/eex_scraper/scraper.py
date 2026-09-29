"""Orquestacion del scrapeo: seleccion de contratos, concurrencia y escritura.

Cada ejecucion pide a cada contrato toda la ventana que la API publica deja
ver (~31 sesiones en futuros, ~1 ano en indices; pedir mas no da mas) y la
contrasta con lo ya guardado: solo entra lo nuevo o lo que EEX ha corregido.
Pedir la ventana entera cuesta lo mismo que pedir un dia (una peticion por
contrato) y asi se cubren huecos y revisiones sin pensar.

Dos modos, que solo difieren en que contratos se saltan:

  run   Se salta los scrapeados con exito hace menos de
        `scrape.min_refresh_hours`. Si una ejecucion se corta, relanzarla
        continua donde se quedo. Es el que se programa a diario.

  full  No se salta ninguno.

Como la API solo expone una ventana movil corta, ejecutarlo a diario es lo que
acaba construyendo el historico largo.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import StrEnum

from . import storage
from .api import UNSUPPORTED_PRICINGS, EexHubClient
from .config import Config
from .curves import CurvesReport, build_curves
from .fetchers import FetchError, RateLimiter, ResilientFetcher
from .models import Contract
from .state import RunLock, ScrapeState

log = logging.getLogger(__name__)

# Cada cuantos contratos se vuelca el estado a disco (por si se corta a mitad).
STATE_FLUSH_EVERY = 100


class Mode(StrEnum):
    RUN = "run"
    FULL = "full"


@dataclass
class ContractOutcome:
    contract: Contract
    status: str  # skipped | written | unchanged | empty | error
    rows: int = 0
    added: int = 0
    updated: int = 0
    message: str | None = None


@dataclass
class ScrapeStats:
    mode: Mode
    total: int = 0
    processed: int = 0
    skipped: int = 0
    written: int = 0
    unchanged: int = 0
    empty: int = 0
    errors: int = 0
    rows_added: int = 0
    rows_updated: int = 0
    used_browser: bool = False
    migration: storage.MigrationReport | None = None
    curves: CurvesReport | None = None
    started_at: datetime = field(default_factory=datetime.now)

    @property
    def elapsed_seconds(self) -> float:
        return (datetime.now() - self.started_at).total_seconds()

    def register(self, outcome: ContractOutcome) -> None:
        self.processed += 1
        self.rows_added += outcome.added
        self.rows_updated += outcome.updated
        match outcome.status:
            case "skipped":
                self.skipped += 1
            case "written":
                self.written += 1
            case "unchanged":
                self.unchanged += 1
            case "empty":
                self.empty += 1
            case "error":
                self.errors += 1


ProgressCallback = Callable[[ScrapeStats, ContractOutcome], None]


@dataclass
class SelectionOverrides:
    """Filtros puntuales de la linea de comandos; pisan a los del config.toml."""

    commodities: Sequence[str] | None = None
    areas: Sequence[str] | None = None
    products: Sequence[str] | None = None
    pricings: Sequence[str] | None = None
    maturity_types: Sequence[str] | None = None
    short_codes: Sequence[str] | None = None
    limit: int | None = None


def select_contracts(
    contracts: Iterable[Contract],
    config: Config,
    overrides: SelectionOverrides | None = None,
) -> list[Contract]:
    overrides = overrides or SelectionOverrides()
    f = config.filters

    commodities = _norm(overrides.commodities if overrides.commodities else f.commodities)
    areas = _norm(overrides.areas if overrides.areas else f.areas)
    products = _norm(overrides.products if overrides.products else f.products)
    pricings = _norm(overrides.pricings if overrides.pricings else f.pricings)
    maturity_types = _norm(
        overrides.maturity_types if overrides.maturity_types else f.maturity_types
    )
    short_codes = _norm(overrides.short_codes if overrides.short_codes else f.short_codes)
    excluded_commodities = _norm(f.exclude_commodities)
    excluded_areas = _norm(f.exclude_areas)

    selected: list[Contract] = []
    for contract in contracts:
        if contract.pricing in UNSUPPORTED_PRICINGS:
            continue
        if commodities and contract.commodity.casefold() not in commodities:
            continue
        if areas and contract.area.casefold() not in areas:
            continue
        if products and (contract.product or "").casefold() not in products:
            continue
        if pricings and contract.pricing.casefold() not in pricings:
            continue
        if maturity_types and (contract.maturity_type or "").casefold() not in maturity_types:
            continue
        if short_codes and contract.short_code.casefold() not in short_codes:
            continue
        if contract.commodity.casefold() in excluded_commodities:
            continue
        if contract.area.casefold() in excluded_areas:
            continue
        selected.append(contract)

    selected.sort(key=lambda c: (c.commodity, c.area, c.short_code, c.maturity or ""))
    if overrides.limit:
        selected = selected[: overrides.limit]
    return selected


async def load_catalog(
    client: EexHubClient, config: Config, *, refresh: bool = True
) -> list[Contract]:
    """Descarga el catalogo (y lo guarda) o reutiliza el CSV ya guardado."""
    if not refresh:
        cached = storage.read_catalog(config.output)
        if cached:
            log.info("Catalogo reutilizado del CSV local: %d contratos", len(cached))
            return cached
        log.info("No hay catalogo local; se descarga.")

    contracts = await client.fetch_catalog()
    path = storage.write_catalog(contracts, config.output)
    log.info("Catalogo guardado en %s (%d contratos)", path, len(contracts))
    return contracts


async def scrape(
    config: Config,
    mode: Mode,
    *,
    overrides: SelectionOverrides | None = None,
    refresh_catalog: bool = True,
    force_browser: bool = False,
    dry_run: bool = False,
    progress: ProgressCallback | None = None,
) -> ScrapeStats:
    if dry_run:
        return await _scrape(
            config, mode, overrides, refresh_catalog, force_browser, True, progress
        )
    with RunLock(config.output):
        return await _scrape(
            config, mode, overrides, refresh_catalog, force_browser, False, progress
        )


async def _scrape(
    config: Config,
    mode: Mode,
    overrides: SelectionOverrides | None,
    refresh_catalog: bool,
    force_browser: bool,
    dry_run: bool,
    progress: ProgressCallback | None,
) -> ScrapeStats:
    stats = ScrapeStats(mode=mode)
    limiter = RateLimiter(config.scrape.requests_per_second, config.scrape.burst)
    fetcher = ResilientFetcher(
        config.api, config.browser, force_browser=force_browser, limiter=limiter
    )
    client = EexHubClient(config, fetcher)
    state = ScrapeState.load(config.output)

    try:
        catalog = await load_catalog(client, config, refresh=refresh_catalog)
        if not dry_run:
            stats.migration = migrate(config, catalog, state)
        contracts = select_contracts(catalog, config, overrides)
        stats.total = len(contracts)
        log.info("Seleccionados %d contratos de %d en el catalogo", len(contracts), len(catalog))

        if dry_run:
            for contract in contracts:
                outcome = ContractOutcome(contract=contract, status="skipped", message="dry-run")
                stats.register(outcome)
                if progress:
                    progress(stats, outcome)
            return stats

        semaphore = asyncio.Semaphore(max(1, config.scrape.concurrency))
        today = date.today()
        lock = asyncio.Lock()

        async def worker(contract: Contract) -> None:
            async with semaphore:
                outcome = await _process_contract(contract, client, config, state, mode, today)
                if config.scrape.request_delay_seconds > 0:
                    await asyncio.sleep(config.scrape.request_delay_seconds)
            async with lock:
                stats.register(outcome)
                if progress:
                    progress(stats, outcome)
                if stats.processed % STATE_FLUSH_EVERY == 0:
                    state.save()

        await asyncio.gather(*(worker(c) for c in contracts))
        stats.used_browser = fetcher.using_browser
        state.save()

        log.info("Regenerando inventario y curvas...")
        storage.write_inventory(config.output)
        stats.curves = build_curves(config.output)
        return stats
    finally:
        state.save()
        await fetcher.aclose()


def migrate(config: Config, catalog: list[Contract], state: ScrapeState) -> storage.MigrationReport:
    """Pasa los CSV del layout antiguo al nuevo y apunta el estado a las rutas nuevas."""
    report = storage.migrate_legacy_layout(config.output, catalog)
    if report.touched:
        for entry in state.entries.values():
            if entry.get("csv") in report.paths:
                entry["csv"] = report.paths[entry["csv"]]
        state.save()
        log.info(
            "Reorganizados %d CSV al nuevo layout (%d fusionados, %d sin datos borrados, "
            "%d filas vacias quitadas)",
            report.touched,
            report.merged,
            report.dropped,
            report.empty_rows_removed,
        )
    return report


async def _process_contract(
    contract: Contract,
    client: EexHubClient,
    config: Config,
    state: ScrapeState,
    mode: Mode,
    today: date,
) -> ContractOutcome:
    """Aisla el fallo de un contrato para que no tumbe el barrido entero.

    Un `full` son horas y miles de contratos: que un error inesperado (disco
    lleno, CSV ilegible, JSON raro, un bug) aborte los 7.000 restantes seria
    peor que anotarlo y seguir. KeyboardInterrupt y CancelledError no heredan
    de Exception, asi que siguen cortando la ejecucion como deben.
    """
    try:
        return await _scrape_contract(contract, client, config, state, mode, today)
    except (FetchError, ValueError) as exc:
        state.record_error(contract.key, str(exc))
        log.debug("Error en %s: %s", contract.label, exc)
        return ContractOutcome(contract=contract, status="error", message=str(exc))
    except Exception as exc:  # noqa: BLE001 - ver docstring
        message = f"{type(exc).__name__}: {exc}"
        state.record_error(contract.key, message)
        log.warning("Error inesperado en %s: %s", contract.label, message)
        return ContractOutcome(contract=contract, status="error", message=message)


async def _scrape_contract(
    contract: Contract,
    client: EexHubClient,
    config: Config,
    state: ScrapeState,
    mode: Mode,
    today: date,
) -> ContractOutcome:
    path = storage.contract_csv_path(config.output, contract)

    if mode is Mode.RUN and state.is_fresh(contract.key, config.scrape.min_refresh_hours):
        return ContractOutcome(contract=contract, status="skipped", message="ya scrapeado")

    existing = storage.read_csv_rows(path, config.output)
    result = await client.fetch_table_data(contract, _start_date(config, today), today)

    if result.is_empty and not existing and config.scrape.skip_empty:
        state.record_success(
            contract.key, csv_path=None, rows=0, added=0, updated=0, last_trade_date=None
        )
        return ContractOutcome(contract=contract, status="empty")

    merge = storage.merge_rows(existing, result.rows)
    if merge.changed or not path.exists():
        storage.write_csv(path, merge.rows, config.output)
        status = "written"
    else:
        status = "unchanged"

    state.record_success(
        contract.key,
        csv_path=storage.relative_csv_path(config.output, contract),
        rows=len(merge.rows),
        added=merge.added,
        updated=merge.updated,
        last_trade_date=storage.last_trade_date(merge.rows),
    )
    return ContractOutcome(
        contract=contract,
        status=status,
        rows=len(merge.rows),
        added=merge.added,
        updated=merge.updated,
    )


def _start_date(config: Config, today: date) -> date:
    """Siempre la ventana entera: la API recorta sola y el coste es el mismo."""
    return today - timedelta(days=config.scrape.max_lookback_days)


def _norm(values: Sequence[str] | None) -> set[str]:
    return {str(v).strip().casefold() for v in values if str(v).strip()} if values else set()
