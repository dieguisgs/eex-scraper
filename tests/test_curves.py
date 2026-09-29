"""Tests de periodos de entrega, tenors y curvas por fecha de referencia."""

from __future__ import annotations

from datetime import date

import pytest

from eex_scraper import storage
from eex_scraper.config import Config
from eex_scraper.curves import build_curves, curves_dir
from eex_scraper.models import Contract
from eex_scraper.tenors import delivery_start, relative_tenor, tenor_label


def contract(maturity_type, maturity, **kwargs) -> Contract:
    base = {
        "short_code": "X",
        "maturity": maturity,
        "maturity_type": maturity_type,
        "commodity": "POWER",
        "pricing": "F",
        "area": "DE",
        "product": "Base",
    }
    base.update(kwargs)
    return Contract(**base)


# ------------------------------------------------------------ periodo de entrega


@pytest.mark.parametrize(
    ("c", "start", "label"),
    [
        (contract("Day", "202610", display_day=5), date(2026, 10, 5), "2026-10-05"),
        (contract("Week", "202610", display_week=41), date(2026, 10, 5), "2026-W41"),
        (contract("Weekend", "202610", display_week=41), date(2026, 10, 10), "WE 2026-W41"),
        (contract("Month", "202611"), date(2026, 11, 1), "2026-11"),
        (contract("Quarter", "202701"), date(2027, 1, 1), "2027-Q1"),
        (contract("Season", "202610"), date(2026, 10, 1), "Win-26/27"),
        (contract("Season", "202704"), date(2027, 4, 1), "Sum-27"),
        (contract("Year", "202801"), date(2028, 1, 1), "Cal-2028"),
    ],
)
def test_delivery_start_and_label(c, start, label):
    assert delivery_start(c) == start
    assert tenor_label(c.maturity_type, start) == label


def test_iso_week_one_in_december_belongs_to_next_year():
    # Un contrato con vencimiento 202612 y semana 1 es la primera semana ISO de 2027.
    c = contract("Week", "202612", display_week=1)
    assert delivery_start(c) == date.fromisocalendar(2027, 1, 1)


def test_contracts_without_maturity_have_no_tenor():
    c = contract(None, None, pricing="I")
    assert delivery_start(c) is None
    assert tenor_label(None, None) is None


def test_day_without_display_day_is_unknown_not_wrong():
    assert delivery_start(contract("Day", "202610")) is None


def test_expired_day_contract_takes_the_day_from_its_short_code():
    """Vencido, ya no esta en el catalogo: el dia sale de AB14 -> 14."""
    assert delivery_start(contract("Day", "202608", short_code="AB14")) == date(2026, 8, 14)


# ------------------------------------------------------------ tenor relativo


@pytest.mark.parametrize(
    ("maturity_type", "start", "trade", "expected"),
    [
        ("Month", date(2026, 10, 1), date(2026, 9, 29), "M+1"),
        ("Month", date(2026, 9, 1), date(2026, 9, 29), "M+0"),
        ("Quarter", date(2027, 1, 1), date(2026, 9, 29), "Q+2"),
        ("Year", date(2027, 1, 1), date(2026, 9, 29), "Y+1"),
        ("Season", date(2026, 10, 1), date(2026, 9, 29), "S+1"),
        ("Season", date(2027, 4, 1), date(2027, 2, 15), "S+1"),  # en invierno, el verano es S+1
        ("Week", date(2026, 10, 5), date(2026, 9, 29), "W+1"),
        ("Day", date(2026, 9, 30), date(2026, 9, 29), "D+1"),
    ],
)
def test_relative_tenor(maturity_type, start, trade, expected):
    assert relative_tenor(maturity_type, start, trade) == expected


# -------------------------------------------------------------------- curvas


def write_contract(cfg, c: Contract, rows: list[tuple[str, str]]):
    start = delivery_start(c)
    storage.write_csv(
        storage.contract_csv_path(cfg.output, c),
        [
            {
                "tradeDate": trade,
                "shortCode": c.short_code,
                "maturity": c.maturity,
                "maturityType": c.maturity_type,
                "deliveryStart": start.isoformat(),
                "tenor": tenor_label(c.maturity_type, start),
                "settlPx": px,
            }
            for trade, px in rows
        ],
        cfg.output,
    )


def test_curve_has_one_row_per_reference_date_and_one_column_per_tenor(tmp_path):
    cfg = Config()
    cfg.output.directory = str(tmp_path)
    write_contract(cfg, contract("Month", "202610", short_code="DEBM"), [("2026-09-28", "80")])
    write_contract(cfg, contract("Month", "202611", short_code="DEBM"), [("2026-09-28", "85")])
    write_contract(
        cfg,
        contract("Year", "202701", short_code="DEBY"),
        [("2026-09-25", "88"), ("2026-09-28", "90")],
    )

    report = build_curves(cfg.output)

    assert report.products == 1
    folder = curves_dir(cfg.output) / "POWER" / "DE"
    wide = storage.read_csv_rows(folder / "Base_wide.csv", cfg.output)
    assert list(wide[0]) == ["tradeDate", "M+1", "M+2", "Y+1"]
    assert wide[1] == {"tradeDate": "2026-09-28", "M+1": "80", "M+2": "85", "Y+1": "90"}
    assert wide[0]["M+1"] is None  # el 25 no habia mensual guardado

    long = storage.read_csv_rows(folder / "Base.csv", cfg.output)
    assert [(r["tradeDate"], r["tenor"], r["relativeTenor"]) for r in long] == [
        ("2026-09-25", "Cal-2027", "Y+1"),
        ("2026-09-28", "2026-10", "M+1"),
        ("2026-09-28", "2026-11", "M+2"),
        ("2026-09-28", "Cal-2027", "Y+1"),
    ]


def test_same_tenor_from_different_routes_is_not_mixed(tmp_path):
    """Freight: varias rutas comparten producto y tipo; cada una va en su columna."""
    cfg = Config()
    cfg.output.directory = str(tmp_path)
    for code, px in (("C5TM", "10"), ("C7EM", "20")):
        c = contract(
            "Month",
            "202610",
            short_code=code,
            commodity="FREIGHT",
            area="Freight",
            product="Capesize",
        )
        write_contract(cfg, c, [("2026-09-28", px)])

    build_curves(cfg.output)

    wide = storage.read_csv_rows(
        curves_dir(cfg.output) / "FREIGHT" / "Freight" / "Capesize_wide.csv", cfg.output
    )
    assert wide[0] == {"tradeDate": "2026-09-28", "C5TM M+1": "10", "C7EM M+1": "20"}


def test_spot_and_indices_do_not_produce_curves(tmp_path):
    cfg = Config()
    cfg.output.directory = str(tmp_path)
    c = contract(None, None, pricing="I", short_code="IDX")
    storage.write_csv(
        storage.contract_csv_path(cfg.output, c),
        [{"tradeDate": "2026-09-28", "settlPx": "5"}],
        cfg.output,
    )
    assert build_curves(cfg.output).products == 0
