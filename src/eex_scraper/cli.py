"""Interfaz de linea de comandos.

Se lanza con `python run.py [comando]`:

python run.py           Lo mismo que `python run.py run`
python run.py run       Descarga todo lo disponible y lo fusiona con lo guardado
python run.py full      Igual, pero sin saltarse los contratos ya al dia
python run.py catalog   Descarga el catalogo de contratos del hub
python run.py curves    Regenera curvas e inventario desde lo guardado (sin red)
python run.py migrate   Reorganiza CSV del layout antiguo (sin red)
python run.py info      Resumen de lo que ya hay en la ruta de salida

Todos los comandos salvo `info` dejan log en la ruta de [logging] del config.
"""

from __future__ import annotations

import asyncio
import csv
import logging
import os
import sys
import time
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path

import typer

from . import storage
from .config import Config, ConfigError
from .curves import build_curves, curves_dir
from .models import PRICING_LABELS
from .scraper import (
    ContractOutcome,
    Mode,
    ScrapeStats,
    SelectionOverrides,
    load_catalog,
    migrate,
    scrape,
)
from .state import AlreadyRunningError, RunLock, ScrapeState

app = typer.Typer(
    add_completion=False,
    invoke_without_command=True,
    help=(
        "Scraper del EEX Market Data Hub (power, gas, environmentals y demas). "
        "Sin comando, ejecuta `run`."
    ),
)

_CONFIG_OPTION = typer.Option(
    None,
    "--config",
    "-c",
    help="Ruta a config.toml (si no, EEX_SCRAPER_CONFIG o config.toml en la carpeta actual).",
)
_OUTPUT_OPTION = typer.Option(
    None,
    "--output",
    "-o",
    help="Ruta base de salida para esta ejecucion; pisa [output].directory del config.",
)
_VERBOSE_OPTION = typer.Option(False, "--verbose", "-v", help="Log detallado.")
_BROWSER_OPTION = typer.Option(
    False, "--browser", help="Forzar el scrapeo via navegador (Playwright)."
)

# Opciones de filtro compartidas por `full` y `update`.
_COMMODITY = typer.Option(None, "--commodity", "-C", help="Filtra por commodity (repetible).")
_AREA = typer.Option(None, "--area", "-A", help="Filtra por area/zona (repetible).")
_PRODUCT = typer.Option(None, "--product", "-P", help="Filtra por producto (repetible).")
_PRICING = typer.Option(None, "--pricing", help="F=futuros, S=spot, I=indices, A=subastas.")
_MATURITY = typer.Option(None, "--maturity-type", help="Day, Week, Month, Quarter, Season, Year.")
_SHORT_CODE = typer.Option(None, "--short-code", "-s", help="Filtra por shortCode (repetible).")
_LIMIT = typer.Option(None, "--limit", "-n", help="Procesa como mucho N contratos.")
_DRY_RUN = typer.Option(False, "--dry-run", help="Lista lo que haria, sin pedir datos.")
_NO_CATALOG_REFRESH = typer.Option(
    False, "--no-catalog-refresh", help="Usa el catalogo ya guardado en vez de bajarlo."
)


@app.callback()
def main(
    ctx: typer.Context,
    config: Path | None = _CONFIG_OPTION,
    output: Path | None = _OUTPUT_OPTION,
    verbose: bool = _VERBOSE_OPTION,
) -> None:
    try:
        ctx.obj = Config.load(config)
    except ConfigError as exc:
        typer.secho(f"Error de configuracion: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from None
    if output:
        ctx.obj.output.set_directory(output, Path.cwd())
    _setup_console_logging(ctx.obj, verbose)

    if ctx.invoked_subcommand is None:
        _run(
            ctx.obj,
            Mode.RUN,
            overrides=SelectionOverrides(),
            refresh_catalog=True,
            force_browser=False,
            dry_run=False,
        )


@app.command()
def catalog(
    ctx: typer.Context,
    browser: bool = _BROWSER_OPTION,
) -> None:
    """Descarga el catalogo completo de contratos a output/catalog/contracts.csv."""
    cfg: Config = ctx.obj
    with _command_log(cfg, "catalog"):
        contracts = asyncio.run(_load_catalog_only(cfg, force_browser=browser))
        _report(f"\nCatalogo: {len(contracts)} contratos")
        _report(f"  -> {storage.catalog_path(cfg.output)}")
        _print_breakdown(contracts)


@app.command()
def run(
    ctx: typer.Context,
    commodity: list[str] | None = _COMMODITY,
    area: list[str] | None = _AREA,
    product: list[str] | None = _PRODUCT,
    pricing: list[str] | None = _PRICING,
    maturity_type: list[str] | None = _MATURITY,
    short_code: list[str] | None = _SHORT_CODE,
    limit: int | None = _LIMIT,
    dry_run: bool = _DRY_RUN,
    no_catalog_refresh: bool = _NO_CATALOG_REFRESH,
    browser: bool = _BROWSER_OPTION,
) -> None:
    """Descarga todo lo disponible y lo contrasta con lo guardado (el de cada dia).

    Se salta los contratos ya scrapeados hace menos de `min_refresh_hours`, asi
    que si se corta, relanzarlo continua donde se quedo.
    """
    _run(
        ctx.obj,
        Mode.RUN,
        overrides=_overrides(commodity, area, product, pricing, maturity_type, short_code, limit),
        refresh_catalog=not no_catalog_refresh,
        force_browser=browser,
        dry_run=dry_run,
    )


@app.command()
def full(
    ctx: typer.Context,
    commodity: list[str] | None = _COMMODITY,
    area: list[str] | None = _AREA,
    product: list[str] | None = _PRODUCT,
    pricing: list[str] | None = _PRICING,
    maturity_type: list[str] | None = _MATURITY,
    short_code: list[str] | None = _SHORT_CODE,
    limit: int | None = _LIMIT,
    dry_run: bool = _DRY_RUN,
    no_catalog_refresh: bool = _NO_CATALOG_REFRESH,
    browser: bool = _BROWSER_OPTION,
) -> None:
    """Como `run`, pero vuelve a pedir todos los contratos aunque esten al dia."""
    _run(
        ctx.obj,
        Mode.FULL,
        overrides=_overrides(commodity, area, product, pricing, maturity_type, short_code, limit),
        refresh_catalog=not no_catalog_refresh,
        force_browser=browser,
        dry_run=dry_run,
    )


# Alias de compatibilidad: antes `update` era el modo incremental.
app.command("update", hidden=True)(run)


@app.command()
def curves(ctx: typer.Context) -> None:
    """Regenera curves/ e inventory.csv a partir de los CSV guardados (sin red)."""
    cfg: Config = ctx.obj
    with _command_log(cfg, "curves"), _locked(cfg):
        inventory = storage.write_inventory(cfg.output)
        report = build_curves(cfg.output)
        _report(f"Curvas: {report.products} productos, {report.rows} filas -> {report.directory}")
        _report(f"Inventario -> {inventory}")


@app.command(name="migrate")
def migrate_cmd(ctx: typer.Context) -> None:
    """Reorganiza los CSV del layout antiguo al nuevo (sin red; `run` lo hace solo)."""
    cfg: Config = ctx.obj
    with _command_log(cfg, "migrate"), _locked(cfg):
        state = ScrapeState.load(cfg.output)
        report = migrate(cfg, storage.read_catalog(cfg.output), state)
        _report(
            f"Movidos {report.moved}, fusionados {report.merged}, "
            f"borrados sin datos {report.dropped}, filas vacias quitadas {report.empty_rows_removed}"
        )


@app.command()
def info(ctx: typer.Context) -> None:
    """Resume que datos hay ya descargados en output/."""
    cfg: Config = ctx.obj
    root = cfg.output.root
    typer.echo(f"Configuracion: {cfg.source_path or '(valores por defecto)'}")
    typer.echo(f"Salida:        {root}")
    typer.echo(f"Logs:          {cfg.log_dir}")

    if not cfg.output.table_dir.exists():
        typer.echo("\nTodavia no hay datos. Empieza con:  python run.py")
        raise typer.Exit()

    files = sorted(cfg.output.table_dir.rglob("*.csv"))
    per_commodity: dict[str, dict[str, object]] = {}
    total_rows = 0
    global_min: str | None = None
    global_max: str | None = None

    for path in files:
        commodity = path.relative_to(cfg.output.table_dir).parts[0]
        bucket = per_commodity.setdefault(commodity, {"files": 0, "rows": 0})
        bucket["files"] = int(bucket["files"]) + 1
        rows, first, last = _scan_csv(path, cfg.output)
        bucket["rows"] = int(bucket["rows"]) + rows
        total_rows += rows
        if first and (global_min is None or first < global_min):
            global_min = first
        if last and (global_max is None or last > global_max):
            global_max = last

    typer.echo(f"\n{len(files)} ficheros CSV, {total_rows} filas")
    typer.echo(f"Inventario: {storage.inventory_path(cfg.output)}")
    typer.echo(f"Curvas:     {curves_dir(cfg.output)}")
    if global_min and global_max:
        typer.echo(f"Rango de fechas de negociacion: {global_min} -> {global_max}")

    typer.echo("\nPor commodity:")
    for commodity, bucket in sorted(per_commodity.items()):
        typer.echo(f"  {commodity:<16} {bucket['files']:>5} ficheros  {bucket['rows']:>8} filas")

    state = ScrapeState.load(cfg.output)
    if state.entries:
        statuses: dict[str, int] = {}
        for entry in state.entries.values():
            key = str(entry.get("status", "?"))
            statuses[key] = statuses.get(key, 0) + 1
        resumen = ", ".join(f"{k}={v}" for k, v in sorted(statuses.items()))
        typer.echo(f"\nEstado ({len(state.entries)} contratos): {resumen}")


# --------------------------------------------------------------------- helpers


def _run(
    cfg: Config,
    mode: Mode,
    *,
    overrides: SelectionOverrides,
    refresh_catalog: bool,
    force_browser: bool,
    dry_run: bool,
) -> None:
    with _command_log(cfg, mode.value + (" --dry-run" if dry_run else "")):
        _report(f"Modo: {mode.value}   salida: {cfg.output.root}")
        try:
            stats = asyncio.run(
                scrape(
                    cfg,
                    mode,
                    overrides=overrides,
                    refresh_catalog=refresh_catalog,
                    force_browser=force_browser,
                    dry_run=dry_run,
                    progress=_progress_printer(),
                )
            )
        except KeyboardInterrupt:  # pragma: no cover - interaccion manual
            _report("\nInterrumpido. El estado y los CSV ya escritos se conservan.")
            raise typer.Exit(code=130) from None
        except AlreadyRunningError as exc:
            _warn(str(exc))
            raise typer.Exit(code=3) from None

        _print_summary(stats)
        if stats.errors and stats.written == 0:
            raise typer.Exit(code=1)


# ------------------------------------------------------------------------ log
#
# Dos destinos:
#   consola  mensajes INFO de los modulos (o DEBUG con -v)
#   fichero  el log del dia en [logging].directory, al nivel de [logging].level:
#            los mensajes de los modulos + lo que el comando ensena por consola
#            (resumen, progreso), que va por _REPORT_LOG solo al fichero para no
#            salir dos veces por pantalla.

_LOG_FORMAT = "%(asctime)s %(levelname)-7s %(message)s"
_REPORT_LOG = logging.getLogger("eex_scraper.report")
_REPORT_LOG.propagate = False
_REPORT_LOG.setLevel(logging.DEBUG)
_console_handler: logging.Handler | None = None


class _StderrHandler(logging.Handler):
    """Escribe en el sys.stderr de cada momento, no en el que habia al crearlo."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            sys.stderr.write(self.format(record) + "\n")
        except Exception:  # noqa: BLE001 - politica estandar de logging
            self.handleError(record)


def _setup_console_logging(cfg: Config, verbose: bool) -> None:
    global _console_handler
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)  # filtran los handlers, no el logger
    if _console_handler is None:
        _console_handler = _StderrHandler()
        _console_handler.setFormatter(logging.Formatter("%(levelname)-7s %(message)s"))
        root.addHandler(_console_handler)
    _console_handler.setLevel(logging.DEBUG if verbose else logging.INFO)
    # httpx registra cada peticion en INFO: solo si se pide detalle.
    detailed = verbose or cfg.logging.level.upper() == "DEBUG"
    logging.getLogger("httpx").setLevel(logging.INFO if detailed else logging.WARNING)


def _report(line: str) -> None:
    typer.echo(line)
    _REPORT_LOG.info("%s", line.strip("\n"))


def _warn(line: str) -> None:
    typer.secho(line, fg=typer.colors.YELLOW, err=True)
    _REPORT_LOG.warning("%s", line)


@contextmanager
def _command_log(cfg: Config, command: str):
    """Abre el log del dia para un comando y deja constancia de inicio y final.

    Si el comando falla con una excepcion, el traceback queda en el log: en una
    tarea programada no hay consola y el log es lo unico que queda.
    """
    handler = _open_log(cfg)
    root = logging.getLogger()
    if handler is not None:
        root.addHandler(handler)
        _REPORT_LOG.addHandler(handler)

    started = time.monotonic()
    _REPORT_LOG.info(
        "=== inicio: %s | config: %s | salida: %s",
        command,
        cfg.source_path or "(valores por defecto)",
        cfg.output.root,
    )
    code = 0
    try:
        yield
    except typer.Exit as exc:
        code = exc.exit_code
        raise
    except Exception:
        code = 1
        _REPORT_LOG.exception("El comando %s ha fallado", command)
        raise
    except BaseException:  # KeyboardInterrupt y compania
        code = 130
        raise
    finally:
        _REPORT_LOG.info(
            "=== fin: %s | codigo %s | %s",
            command,
            code,
            _duration(time.monotonic() - started),
        )
        if handler is not None:
            root.removeHandler(handler)
            _REPORT_LOG.removeHandler(handler)
            handler.close()


def _open_log(cfg: Config) -> logging.Handler | None:
    """Abre el log del dia; si otro proceso lo tiene bloqueado, uno propio por PID.

    Quedarse sin log nunca debe impedir que el comando haga su trabajo.
    """
    target = cfg.log_file(date.today())
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        typer.secho(f"No se pudo crear la carpeta de logs {target.parent}: {exc}", err=True)
        return None
    _prune_logs(cfg)

    for path in (target, target.with_name(f"{target.stem}_{os.getpid()}{target.suffix}")):
        try:
            handler = logging.FileHandler(path, encoding="utf-8")
        except OSError:
            continue
        handler.setFormatter(logging.Formatter(_LOG_FORMAT))
        handler.setLevel(cfg.logging.level.upper())
        return handler
    typer.secho(f"No se pudo abrir el log en {target.parent}; sigo sin log.", err=True)
    return None


def _prune_logs(cfg: Config) -> None:
    """Borra los logs de este scraper con mas de keep_days dias (0 = nunca)."""
    if cfg.logging.keep_days <= 0:
        return
    cutoff = datetime.now().timestamp() - cfg.logging.keep_days * 86400
    pattern = cfg.logging.filename.replace("{date}", "*")
    stem, dot, suffix = pattern.rpartition(".")
    patterns = {pattern, f"{stem}_*{dot}{suffix}"} if dot else {pattern}
    for glob in patterns:
        for old in cfg.log_dir.glob(glob):
            try:
                if old.is_file() and old.stat().st_mtime < cutoff:
                    old.unlink()
            except OSError:
                pass


def _overrides(commodity, area, product, pricing, maturity_type, short_code, limit):
    return SelectionOverrides(
        commodities=commodity,
        areas=area,
        products=product,
        pricings=pricing,
        maturity_types=maturity_type,
        short_codes=short_code,
        limit=limit,
    )


class _locked:
    """RunLock con salida limpia si ya hay otra ejecucion en marcha."""

    def __init__(self, cfg: Config) -> None:
        self.lock = RunLock(cfg.output)

    def __enter__(self):
        try:
            return self.lock.__enter__()
        except AlreadyRunningError as exc:
            _warn(str(exc))
            raise typer.Exit(code=3) from None

    def __exit__(self, *exc):
        return self.lock.__exit__(*exc)


def _progress_printer():
    # En consola, una linea que se reescribe; en un log (tarea programada),
    # una linea nueva cada cierto numero de contratos.
    interactive = sys.stderr.isatty()
    every = 25 if interactive else 250

    def printer(stats: ScrapeStats, outcome: ContractOutcome) -> None:
        if stats.processed % every == 0 or stats.processed == stats.total:
            done = stats.processed
            pct = (done / stats.total * 100) if stats.total else 100.0
            line = (
                f"  {done}/{stats.total} ({pct:5.1f}%)  "
                f"escritos={stats.written} sin-cambios={stats.unchanged} "
                f"vacios={stats.empty} saltados={stats.skipped} errores={stats.errors}"
            )
            if interactive:
                sys.stderr.write(f"\r{line}   ")
                if done == stats.total:
                    sys.stderr.write("\n")
            else:
                sys.stderr.write(f"{line}\n")
            sys.stderr.flush()
            if done % 250 == 0 or done == stats.total:
                _REPORT_LOG.info("%s", line.strip())

    return printer


def _print_summary(stats: ScrapeStats) -> None:
    _report("\nResumen")
    _report(f"  contratos seleccionados : {stats.total}")
    _report(f"  CSV escritos            : {stats.written}")
    _report(f"  sin cambios             : {stats.unchanged}")
    _report(f"  vacios (sin datos)      : {stats.empty}")
    _report(f"  saltados (ya al dia)    : {stats.skipped}")
    _report(f"  errores                 : {stats.errors}")
    _report(f"  filas nuevas            : {stats.rows_added}")
    _report(f"  filas actualizadas      : {stats.rows_updated}")
    _report(f"  tiempo                  : {_duration(stats.elapsed_seconds)}")
    if stats.migration and stats.migration.touched:
        m = stats.migration
        _report(
            f"  reorganizados           : {m.touched} CSV al nuevo layout "
            f"({m.empty_rows_removed} filas vacias quitadas)"
        )
    if stats.curves:
        _report(
            f"  curvas                  : {stats.curves.products} productos -> "
            f"{stats.curves.directory}"
        )
    if stats.used_browser:
        _report("  (se uso el respaldo con navegador)")


def _duration(seconds: float) -> str:
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours} h {minutes} min"
    if minutes:
        return f"{minutes} min {secs} s"
    return f"{seconds:.1f} s"


def _print_breakdown(contracts) -> None:
    by_commodity: dict[str, int] = {}
    by_pricing: dict[str, int] = {}
    for contract in contracts:
        by_commodity[contract.commodity] = by_commodity.get(contract.commodity, 0) + 1
        by_pricing[contract.pricing] = by_pricing.get(contract.pricing, 0) + 1

    _report("\nPor commodity:")
    for name, count in sorted(by_commodity.items(), key=lambda kv: -kv[1]):
        _report(f"  {name:<16} {count:>6}")

    _report("\nPor tipo de precio:")
    for code, count in sorted(by_pricing.items(), key=lambda kv: -kv[1]):
        _report(f"  {code} ({PRICING_LABELS.get(code, '?'):<8}) {count:>6}")


async def _load_catalog_only(cfg: Config, *, force_browser: bool):
    from .api import EexHubClient
    from .fetchers import RateLimiter, ResilientFetcher

    fetcher = ResilientFetcher(
        cfg.api,
        cfg.browser,
        force_browser=force_browser,
        limiter=RateLimiter(cfg.scrape.requests_per_second, cfg.scrape.burst),
    )
    try:
        client = EexHubClient(cfg, fetcher)
        return await load_catalog(client, cfg, refresh=True)
    finally:
        await fetcher.aclose()


def _scan_csv(path: Path, output_cfg) -> tuple[int, str | None, str | None]:
    """Cuenta filas y rango de fechas sin cargar el CSV entero en memoria."""
    rows = 0
    first: str | None = None
    last: str | None = None
    try:
        with path.open("r", encoding=output_cfg.encoding, newline="") as fh:
            for record in csv.DictReader(fh, delimiter=output_cfg.delimiter):
                rows += 1
                trade_date = record.get("tradeDate")
                if not trade_date:
                    continue
                if first is None or trade_date < first:
                    first = trade_date
                if last is None or trade_date > last:
                    last = trade_date
    except OSError:
        return 0, None, None
    return rows, first, last


if __name__ == "__main__":  # pragma: no cover
    app()
