"""Curvas forward: por producto, la cotizacion de todos sus tenors en cada fecha.

Se generan a partir de los CSV por contrato de table_data/ (que siguen siendo
la fuente de verdad) y se reescriben enteras en cada ejecucion:

    curves/<COMMODITY>/<AREA>/<producto>.csv
        Formato largo: una fila por (fecha de referencia, contrato). Lleva el
        tenor absoluto (2027-Q1, Cal-2028...), el relativo (Q+1, Y+2...),
        precio, volumen y open interest.

    curves/<COMMODITY>/<AREA>/<producto>_wide.csv
        Formato ancho: una fila por fecha de referencia y una columna por
        tenor relativo (D+1 ... M+1, M+2 ... Q+1 ... Y+1), con el precio de
        liquidacion. Cada columna es una serie continua aunque los contratos
        vayan rotando.

Solo aplica a futuros: spot e indices no tienen tenors y ya son una serie por
fichero en table_data/.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from . import storage
from .models import PRICING_FOLDERS, has_values
from .tenors import MATURITY_ORDER, relative_sort_key, relative_tenor

CURVES_DIRNAME = "curves"

LONG_COLUMNS: list[str] = [
    "tradeDate",
    "relativeTenor",
    "tenor",
    "maturityType",
    "deliveryStart",
    "shortCode",
    "maturity",
    "settlPx",
    "totVolTrdd",
    "grossOpenInt",
    "netOpenInt",
    "currency",
    "uOM",
]


@dataclass
class CurvesReport:
    products: int = 0
    rows: int = 0
    directory: Path | None = None


def curves_dir(output_cfg) -> Path:
    return output_cfg.root / CURVES_DIRNAME


def build_curves(output_cfg) -> CurvesReport:
    report = CurvesReport(directory=curves_dir(output_cfg))
    futures = PRICING_FOLDERS["F"]
    for product_dir in sorted(output_cfg.table_dir.glob(f"*/*/{futures}/*")):
        if not product_dir.is_dir():
            continue
        rows = _load_product(product_dir, output_cfg)
        if not rows:
            continue
        commodity, area = product_dir.parts[-4], product_dir.parts[-3]
        target = curves_dir(output_cfg) / commodity / area
        storage.write_csv(target / f"{product_dir.name}.csv", rows, output_cfg, LONG_COLUMNS)
        header, wide = _pivot(rows)
        storage.write_csv(target / f"{product_dir.name}_wide.csv", wide, output_cfg, header)
        report.products += 1
        report.rows += len(rows)
    return report


def _load_product(product_dir: Path, output_cfg) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in product_dir.rglob("*.csv"):
        for record in storage.read_csv_rows(path, output_cfg):
            if not record.get("tradeDate") or not has_values(record):
                continue
            row = {c: record.get(c) for c in LONG_COLUMNS}
            row["relativeTenor"] = _relative(record)
            rows.append(row)
    rows.sort(
        key=lambda r: (
            r["tradeDate"],
            MATURITY_ORDER.get(r.get("maturityType") or "", 99),
            r.get("deliveryStart") or "",
            r.get("shortCode") or "",
        )
    )
    return rows


def _relative(record: dict[str, Any]) -> str | None:
    try:
        start = date.fromisoformat(str(record.get("deliveryStart")))
        trade = date.fromisoformat(str(record.get("tradeDate")))
    except ValueError:
        return None
    return relative_tenor(record.get("maturityType"), start, trade)


def _pivot(rows: list[dict[str, Any]]) -> tuple[list[str], list[dict[str, Any]]]:
    """Filas = fecha de referencia, columnas = tenor relativo, valor = settlPx.

    Si en un mismo producto hay varios contratos con el mismo tenor el mismo
    dia (p. ej. las rutas de freight: C5TM y C7EM son ambos Capesize mensual),
    esas columnas llevan delante el shortCode para no mezclarlos.
    """
    seen: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    for r in rows:
        if r["relativeTenor"]:
            seen[(r["tradeDate"], r["maturityType"] or "", r["relativeTenor"])].add(
                r["shortCode"] or ""
            )
    ambiguous = {mt for (_, mt, _), codes in seen.items() if len(codes) > 1}

    table: dict[str, dict[str, Any]] = {}
    columns: set[str] = set()
    for r in rows:
        if not r["relativeTenor"] or r.get("settlPx") in (None, ""):
            continue
        column = r["relativeTenor"]
        if (r["maturityType"] or "") in ambiguous:
            column = f"{r['shortCode']} {column}"
        columns.add(column)
        table.setdefault(r["tradeDate"], {"tradeDate": r["tradeDate"]})[column] = r["settlPx"]

    def order(column: str) -> tuple[Any, ...]:
        code, _, tenor = column.rpartition(" ")
        kind, n = relative_sort_key(tenor)
        return kind, code, n

    header = ["tradeDate", *sorted(columns, key=order)]
    return header, [table[d] for d in sorted(table)]
