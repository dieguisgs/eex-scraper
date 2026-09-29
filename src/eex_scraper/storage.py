"""Escritura de los CSV de salida.

Layout:

    output/
      catalog/contracts.csv     <- catalogo completo del hub
      inventory.csv             <- una fila por fichero de datos: fechas, filas, ultimo precio
      table_data/<COMMODITY>/<AREA>/<Futures|Spot|Indices|Auctions>/<producto>/[<vencimiento>/]<CONTRATO>.csv
      curves/...                <- vista por producto y fecha de referencia (curves.py)
      _state/scrape_state.json  <- que se scrapeo y cuando

Cada CSV de contrato lleva una fila por fecha de negociacion (tradeDate) con
las columnas de la tabla. Al volver a scrapear, las filas nuevas se fusionan
con las existentes deduplicando por (tradeDate, deliveryDay): gana siempre la
lectura mas reciente, porque EEX revisa precios de liquidacion. Las filas sin
ningun dato de mercado no se guardan.
"""

from __future__ import annotations

import csv
import os
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .models import CATALOG_COLUMNS, CSV_COLUMNS, DEDUP_KEYS, Contract, has_values
from .tenors import delivery_start, tenor_label

INVENTORY_FILENAME = "inventory.csv"
INVENTORY_COLUMNS: list[str] = [
    "csvPath",
    "commodity",
    "area",
    "pricing",
    "product",
    "maturityType",
    "tenor",
    "shortCode",
    "maturity",
    "rows",
    "firstTradeDate",
    "lastTradeDate",
    "lastSettlPx",
]


@dataclass
class MergeOutcome:
    """Que cambio al fusionar filas nuevas con un CSV existente."""

    rows: list[dict[str, Any]]
    added: int
    updated: int
    existing: int
    removed: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.added or self.updated or self.removed)


@dataclass
class MigrationReport:
    """Resultado de pasar los CSV del layout antiguo (<COMMODITY>/<AREA>/x.csv) al nuevo."""

    moved: int = 0
    merged: int = 0
    dropped: int = 0
    empty_rows_removed: int = 0
    paths: dict[str, str | None] = field(default_factory=dict)  # antigua -> nueva (o None)

    @property
    def touched(self) -> int:
        return self.moved + self.merged + self.dropped


def contract_csv_path(output_cfg, contract: Contract) -> Path:
    return output_cfg.table_dir.joinpath(*contract.path_parts)


def relative_csv_path(output_cfg, contract: Contract) -> str:
    return contract_csv_path(output_cfg, contract).relative_to(output_cfg.root).as_posix()


def read_csv_rows(path: Path, output_cfg) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open("r", encoding=output_cfg.encoding, newline="") as fh:
        reader = csv.DictReader(fh, delimiter=output_cfg.delimiter)
        return [{k: (v if v != "" else None) for k, v in row.items()} for row in reader]


def merge_rows(existing: list[dict[str, Any]], incoming: Iterable[dict[str, Any]]) -> MergeOutcome:
    """Fusiona filas nuevas con las guardadas; gana la lectura mas reciente.

    Las filas sin datos de mercado se descartan en los dos lados: una fila
    vacia nunca pisa un precio ya guardado, y las que quedaron en CSV antiguos
    se limpian la proxima vez que se toca el fichero.
    """
    kept = [r for r in existing if has_values(r)]
    merged: dict[tuple[str, ...], dict[str, Any]] = {_dedup_key(r): r for r in kept}
    added = updated = 0

    for row in incoming:
        if not has_values(row):
            continue
        key = _dedup_key(row)
        previous = merged.get(key)
        if previous is None:
            added += 1
        elif _differs(previous, row):
            updated += 1
        else:
            continue
        merged[key] = row

    rows = sorted(merged.values(), key=lambda r: _dedup_key(r))
    return MergeOutcome(
        rows=rows,
        added=added,
        updated=updated,
        existing=len(existing),
        removed=len(existing) - len(kept),
    )


def write_csv(
    path: Path, rows: list[dict[str, Any]], output_cfg, columns: list[str] | None = None
) -> None:
    """Escritura atomica: se genera un temporal y se reemplaza el destino."""
    columns = columns or CSV_COLUMNS
    path.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        with tmp_path.open("w", encoding=output_cfg.encoding, newline="") as fh:
            writer = csv.DictWriter(
                fh, fieldnames=columns, delimiter=output_cfg.delimiter, extrasaction="ignore"
            )
            writer.writeheader()
            for row in rows:
                writer.writerow({c: _cell(row.get(c)) for c in columns})
        tmp_path.replace(path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


def catalog_path(output_cfg) -> Path:
    return output_cfg.catalog_dir / "contracts.csv"


def write_catalog(contracts: list[Contract], output_cfg) -> Path:
    path = catalog_path(output_cfg)
    rows = [c.to_catalog_row() for c in contracts]
    rows.sort(key=lambda r: (r["commodity"], r["area"], r["shortCode"], str(r["maturity"] or "")))
    write_csv(path, rows, output_cfg, columns=CATALOG_COLUMNS)
    return path


def read_catalog(output_cfg) -> list[Contract]:
    path = catalog_path(output_cfg)
    return [Contract.from_row(row) for row in read_csv_rows(path, output_cfg)]


def migrate_legacy_layout(output_cfg, catalog: Iterable[Contract] = ()) -> MigrationReport:
    """Mueve los CSV del layout antiguo <COMMODITY>/<AREA>/<fichero>.csv al nuevo.

    El contrato se reconstruye desde las columnas del propio CSV, asi que
    funciona tambien con contratos vencidos que ya no estan en el catalogo;
    cuando esta, el catalogo aporta el dia/semana de entrega para el tenor. Por
    el camino se quitan las filas vacias y se rellenan deliveryStart/tenor. Si
    el destino ya existe se fusionan (gana el destino, que es mas reciente).
    """
    report = MigrationReport()
    table_dir = output_cfg.table_dir
    if not table_dir.exists():
        return report

    by_key = {c.key: c for c in catalog}
    for old in sorted(table_dir.glob("*/*/*.csv")):
        old_rel = old.relative_to(output_cfg.root).as_posix()
        rows = read_csv_rows(old, output_cfg)
        data_rows = [r for r in rows if has_values(r)]
        report.empty_rows_removed += len(rows) - len(data_rows)
        if not data_rows:
            old.unlink()
            report.dropped += 1
            report.paths[old_rel] = None
            continue

        contract = Contract.from_row(data_rows[0])
        contract = by_key.get(contract.key, contract)
        start = delivery_start(contract)
        label = tenor_label(contract.maturity_type, start)
        for row in data_rows:
            if not row.get("deliveryStart") and start:
                row["deliveryStart"] = start.isoformat()
            if not row.get("tenor") and label:
                row["tenor"] = label

        new = contract_csv_path(output_cfg, contract)
        if new.exists():
            data_rows = merge_rows(data_rows, read_csv_rows(new, output_cfg)).rows
            report.merged += 1
        else:
            report.moved += 1
        write_csv(new, data_rows, output_cfg)
        old.unlink()
        report.paths[old_rel] = new.relative_to(output_cfg.root).as_posix()

    return report


def inventory_path(output_cfg) -> Path:
    return output_cfg.root / INVENTORY_FILENAME


def write_inventory(output_cfg) -> Path:
    """Un CSV con una fila por fichero de datos: que es, cuantas filas y que fechas cubre."""
    entries: list[dict[str, Any]] = []
    if output_cfg.table_dir.exists():
        for path in sorted(output_cfg.table_dir.rglob("*.csv")):
            rows = read_csv_rows(path, output_cfg)
            if not rows:
                continue
            first, last = rows[0], rows[-1]
            dates = [str(r["tradeDate"]) for r in rows if r.get("tradeDate")]
            last_px = next((r["settlPx"] for r in reversed(rows) if r.get("settlPx")), None)
            entries.append(
                {
                    "csvPath": path.relative_to(output_cfg.root).as_posix(),
                    "commodity": first.get("commodity"),
                    "area": first.get("area"),
                    "pricing": first.get("pricing"),
                    "product": first.get("product"),
                    "maturityType": first.get("maturityType"),
                    "tenor": last.get("tenor"),
                    "shortCode": first.get("shortCode"),
                    "maturity": first.get("maturity"),
                    "rows": len(rows),
                    "firstTradeDate": min(dates) if dates else None,
                    "lastTradeDate": max(dates) if dates else None,
                    "lastSettlPx": last_px,
                }
            )
    path = inventory_path(output_cfg)
    write_csv(path, entries, output_cfg, columns=INVENTORY_COLUMNS)
    return path


def last_trade_date(rows: list[dict[str, Any]]) -> str | None:
    dates = [str(r["tradeDate"]) for r in rows if r.get("tradeDate")]
    return max(dates) if dates else None


def _dedup_key(row: dict[str, Any]) -> tuple[str, ...]:
    return tuple(str(row.get(k) or "") for k in DEDUP_KEYS)


def _differs(previous: dict[str, Any], row: dict[str, Any]) -> bool:
    """Compara ignorando la marca de scrapeo, que cambia en cada ejecucion."""
    return any(_cell(previous.get(c)) != _cell(row.get(c)) for c in CSV_COLUMNS if c != "scrapedAt")


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)
