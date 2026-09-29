"""Tests de la logica local: rutas, fusion de CSV, filtros y ventana de fechas."""

from __future__ import annotations

import asyncio
from datetime import date, timedelta

import pytest

from eex_scraper import storage
from eex_scraper.api import EexHubClient
from eex_scraper.config import Config
from eex_scraper.models import Contract
from eex_scraper.scraper import Mode, SelectionOverrides, _start_date, select_contracts
from eex_scraper.state import AlreadyRunningError, RunLock


def make_contract(**kwargs) -> Contract:
    base = {
        "short_code": "DEBY",
        "maturity": "202801",
        "maturity_type": "Year",
        "commodity": "POWER",
        "pricing": "F",
        "area": "DE",
        "product": "Base",
    }
    base.update(kwargs)
    return Contract(**base)


def row(trade_date: str, price: str | None = "10", delivery_day: str | None = None) -> dict:
    return {
        "tradeDate": trade_date,
        "shortCode": "DEBY",
        "settlPx": price,
        "deliveryDay": delivery_day,
        "scrapedAt": "2026-09-22T10:00:00+00:00",
    }


# --------------------------------------------------------------------- rutas


def test_csv_path_is_commodity_area_pricing_product_maturity_type(tmp_path):
    cfg = Config()
    cfg.output.directory = str(tmp_path)
    path = storage.contract_csv_path(cfg.output, make_contract())
    assert path.parts[-6:] == ("POWER", "DE", "Futures", "Base", "Year", "DEBY_202801.csv")


def test_index_path_has_no_maturity_level(tmp_path):
    cfg = Config()
    cfg.output.directory = str(tmp_path)
    contract = make_contract(
        short_code="EEX Weekly German Butter Index",
        maturity=None,
        maturity_type=None,
        pricing="I",
        commodity="AGRICULTURALS",
        product="Butter",
    )
    path = storage.contract_csv_path(cfg.output, contract)
    assert path.parts[-5:] == (
        "AGRICULTURALS",
        "DE",
        "Indices",
        "Butter",
        "EEX_Weekly_German_Butter_Index_INDEX.csv",
    )


def test_catalog_csv_path_matches_storage_path(tmp_path):
    cfg = Config()
    cfg.output.directory = str(tmp_path)
    contract = make_contract()
    relative = storage.contract_csv_path(cfg.output, contract).relative_to(cfg.output.table_dir)
    assert contract.to_catalog_row()["csvPath"] == relative.as_posix()


def test_short_codes_with_spaces_become_safe_filenames():
    contract = make_contract(
        short_code="EEX CBAM Reference Price AUD", maturity=None, maturity_type=None, pricing="I"
    )
    assert contract.file_stem == "EEX_CBAM_Reference_Price_AUD_INDEX"


def test_same_short_code_in_two_areas_does_not_collide(tmp_path):
    cfg = Config()
    cfg.output.directory = str(tmp_path)
    a = storage.contract_csv_path(
        cfg.output, make_contract(short_code="T3PA", maturity=None, area="EU")
    )
    b = storage.contract_csv_path(
        cfg.output, make_contract(short_code="T3PA", maturity=None, area="PL")
    )
    assert a != b


# --------------------------------------------------------------------- fusion


def test_merge_adds_new_dates_and_keeps_old_ones():
    existing = [row("2026-09-01"), row("2026-09-02")]
    outcome = storage.merge_rows(existing, [row("2026-09-03")])
    assert outcome.added == 1
    assert outcome.updated == 0
    assert [r["tradeDate"] for r in outcome.rows] == ["2026-09-01", "2026-09-02", "2026-09-03"]


def test_merge_overwrites_revised_settlement_price():
    outcome = storage.merge_rows([row("2026-09-01", "10")], [row("2026-09-01", "11")])
    assert (outcome.added, outcome.updated) == (0, 1)
    assert outcome.rows[0]["settlPx"] == "11"


def test_merge_ignores_scraped_at_when_comparing():
    old = row("2026-09-01")
    new = {**row("2026-09-01"), "scrapedAt": "2026-09-23T10:00:00+00:00"}
    outcome = storage.merge_rows([old], [new])
    assert outcome.changed is False


def test_spot_rows_dedup_by_trade_date_and_delivery_day():
    existing = [row("2026-09-01", delivery_day="2026-09-02")]
    incoming = [row("2026-09-01", delivery_day="2026-09-03")]
    outcome = storage.merge_rows(existing, incoming)
    assert outcome.added == 1
    assert len(outcome.rows) == 2


def test_rows_without_market_data_are_not_stored():
    """La API rellena la ventana con dias sin cotizacion; esas filas no entran."""
    empty = row("2026-09-02", price=None)
    outcome = storage.merge_rows([], [row("2026-09-01"), empty])
    assert [r["tradeDate"] for r in outcome.rows] == ["2026-09-01"]


def test_empty_row_never_overwrites_a_stored_price():
    outcome = storage.merge_rows([row("2026-09-01", "10")], [row("2026-09-01", None)])
    assert outcome.changed is False
    assert outcome.rows[0]["settlPx"] == "10"


def test_empty_rows_left_in_old_files_are_cleaned():
    outcome = storage.merge_rows([row("2026-09-01"), row("2026-09-02", None)], [])
    assert outcome.removed == 1
    assert outcome.changed is True
    assert len(outcome.rows) == 1


def test_csv_roundtrip_preserves_values(tmp_path):
    cfg = Config()
    cfg.output.directory = str(tmp_path)
    path = tmp_path / "x.csv"
    storage.write_csv(path, [row("2026-09-01", "12.5")], cfg.output)
    back = storage.read_csv_rows(path, cfg.output)
    assert back[0]["tradeDate"] == "2026-09-01"
    assert back[0]["settlPx"] == "12.5"
    assert back[0]["deliveryDay"] is None


# -------------------------------------------------------------------- filtros


def test_options_are_never_selected():
    contracts = [make_contract(pricing="O"), make_contract(pricing="F")]
    selected = select_contracts(contracts, Config())
    assert [c.pricing for c in selected] == ["F"]


def test_cli_filters_override_config():
    cfg = Config()
    cfg.filters.commodities = ["NATGAS"]
    contracts = [make_contract(commodity="POWER"), make_contract(commodity="NATGAS")]
    selected = select_contracts(contracts, cfg, SelectionOverrides(commodities=["POWER"]))
    assert [c.commodity for c in selected] == ["POWER"]


def test_filters_are_case_insensitive():
    selected = select_contracts([make_contract()], Config(), SelectionOverrides(areas=["de"]))
    assert len(selected) == 1


def test_exclusions_win_over_inclusions():
    cfg = Config()
    cfg.filters.exclude_areas = ["DE"]
    assert select_contracts([make_contract()], cfg, SelectionOverrides(areas=["DE"])) == []


def test_limit_truncates_selection():
    contracts = [make_contract(maturity=str(202800 + i)) for i in range(5)]
    assert len(select_contracts(contracts, Config(), SelectionOverrides(limit=2))) == 2


# ------------------------------------------------------------ ventana de fechas


def test_every_run_asks_for_the_whole_window():
    cfg = Config()
    today = date(2026, 9, 22)
    assert _start_date(cfg, today) == today - timedelta(days=cfg.scrape.max_lookback_days)


# ---------------------------------------------------------- flujo de un contrato


class TableFetcher:
    def __init__(self, data):
        self.data = data
        self.calls = 0

    async def get_json(self, url, params):
        self.calls += 1
        return {
            "header": ["shortCode", "tradeDate", "settlPx", "totVolTrdd"],
            "data": self.data,
            "currency": "EUR",
            "uOM": "MWh",
        }


def _scrape_one(tmp_path, fetcher, mode, state=None):
    from eex_scraper.scraper import _process_contract
    from eex_scraper.state import ScrapeState

    cfg = Config()
    cfg.output.directory = str(tmp_path)
    state = state or ScrapeState.load(cfg.output)
    client = EexHubClient(cfg, fetcher)
    outcome = asyncio.run(
        _process_contract(make_contract(), client, cfg, state, mode, date(2026, 9, 22))
    )
    return cfg, state, outcome


def test_second_run_with_same_data_does_not_rewrite(tmp_path):
    data = [["DEBY", "2026-09-18", 98.1, 100], ["DEBY", "2026-09-21", 97.5, 50]]
    _, state, first = _scrape_one(tmp_path, TableFetcher(data), Mode.FULL)
    assert (first.status, first.added) == ("written", 2)

    _, _, second = _scrape_one(tmp_path, TableFetcher(data), Mode.FULL, state)
    assert (second.status, second.added, second.updated) == ("unchanged", 0, 0)


def test_run_mode_skips_fresh_contracts_without_network(tmp_path):
    data = [["DEBY", "2026-09-18", 98.1, 100]]
    _, state, _ = _scrape_one(tmp_path, TableFetcher(data), Mode.FULL)
    fetcher = TableFetcher(data)
    _, _, outcome = _scrape_one(tmp_path, fetcher, Mode.RUN, state)
    assert outcome.status == "skipped"
    assert fetcher.calls == 0


def test_contract_that_never_traded_gets_no_csv(tmp_path):
    data = [["DEBY", "2026-09-18", None, None], ["DEBY", "2026-09-21", None, None]]
    cfg, _, outcome = _scrape_one(tmp_path, TableFetcher(data), Mode.FULL)
    assert outcome.status == "empty"
    assert not storage.contract_csv_path(cfg.output, make_contract()).exists()


def test_rows_carry_delivery_start_and_tenor(tmp_path):
    cfg, _, _ = _scrape_one(tmp_path, TableFetcher([["DEBY", "2026-09-18", 98.1, 1]]), Mode.FULL)
    rows = storage.read_csv_rows(storage.contract_csv_path(cfg.output, make_contract()), cfg.output)
    assert rows[0]["deliveryStart"] == "2028-01-01"
    assert rows[0]["tenor"] == "Cal-2028"


# --------------------------------------------------------------- migracion


def test_legacy_layout_is_migrated_and_cleaned(tmp_path):
    cfg = Config()
    cfg.output.directory = str(tmp_path)
    legacy = cfg.output.table_dir / "POWER" / "DE" / "DEBY_202801.csv"
    base = {
        "shortCode": "DEBY",
        "maturity": "202801",
        "maturityType": "Year",
        "commodity": "POWER",
        "pricing": "F",
        "area": "DE",
        "product": "Base",
    }
    storage.write_csv(
        legacy,
        [
            {**base, "tradeDate": "2026-09-18", "settlPx": "98"},
            {**base, "tradeDate": "2026-09-21"},  # fila vacia de la API
        ],
        cfg.output,
    )

    report = storage.migrate_legacy_layout(cfg.output)

    new = storage.contract_csv_path(cfg.output, make_contract())
    assert not legacy.exists()
    assert (report.moved, report.empty_rows_removed) == (1, 1)
    rows = storage.read_csv_rows(new, cfg.output)
    assert [r["tradeDate"] for r in rows] == ["2026-09-18"]
    assert rows[0]["tenor"] == "Cal-2028"
    # Idempotente: una segunda pasada no encuentra nada que mover.
    assert storage.migrate_legacy_layout(cfg.output).touched == 0


# ------------------------------------------------------------------ bloqueo


def test_second_run_on_same_output_is_refused(tmp_path):
    cfg = Config()
    cfg.output.directory = str(tmp_path)
    with RunLock(cfg.output), pytest.raises(AlreadyRunningError), RunLock(cfg.output):
        pass
    # Liberado el primero, se puede volver a coger.
    with RunLock(cfg.output):
        pass


# ------------------------------------------------------- parametros de la API


class FakeFetcher:
    def __init__(self, payload):
        self.payload = payload
        self.params = None

    async def get_json(self, url, params):
        self.params = params
        return self.payload


def test_table_request_sends_maturity_and_type():
    payload = {
        "header": ["shortCode", "tradeDate", "settlPx"],
        "data": [["DEBY", "2026-09-21", 97.47]],
        "currency": "EUR",
        "uOM": "MWh",
    }
    fetcher = FakeFetcher(payload)
    client = EexHubClient(Config(), fetcher)
    result = asyncio.run(
        client.fetch_table_data(make_contract(), date(2026, 1, 1), date(2026, 9, 22))
    )

    assert fetcher.params["maturity"] == "202801"
    assert fetcher.params["maturityType"] == "Year"
    assert fetcher.params["isRolling"] == "true"
    assert result.rows[0]["settlPx"] == 97.47
    assert result.rows[0]["currency"] == "EUR"
    assert result.last_trade_date == "2026-09-21"


def test_contracts_without_maturity_still_send_one():
    """Spot e indices no tienen vencimiento, pero la API exige el parametro."""
    fetcher = FakeFetcher({"header": [], "data": []})
    client = EexHubClient(Config(), fetcher)
    contract = make_contract(maturity=None, maturity_type=None, pricing="S")
    asyncio.run(client.fetch_table_data(contract, date(2026, 1, 1), date(2026, 9, 22)))

    assert fetcher.params["maturity"] == "20260922"
    assert fetcher.params["maturityType"] == "Day"


def test_options_are_rejected_before_hitting_the_network():
    client = EexHubClient(Config(), FakeFetcher({}))
    with pytest.raises(ValueError, match="table-data-option"):
        asyncio.run(
            client.fetch_table_data(make_contract(pricing="O"), date(2026, 1, 1), date(2026, 9, 22))
        )


# --------------------------------------------------- aislamiento de fallos


class BoomFetcher:
    """Fetcher que revienta con algo que el scraper no espera."""

    async def get_json(self, url, params):
        raise RuntimeError("bug inesperado")


def test_unexpected_error_is_recorded_not_propagated(tmp_path):
    """Un contrato roto se anota y el barrido sigue; no aborta las horas restantes."""
    from eex_scraper.scraper import _process_contract
    from eex_scraper.state import ScrapeState

    cfg = Config()
    cfg.output.directory = str(tmp_path)
    state = ScrapeState.load(cfg.output)
    client = EexHubClient(cfg, BoomFetcher())
    contract = make_contract()

    outcome = asyncio.run(
        _process_contract(contract, client, cfg, state, Mode.FULL, date(2026, 9, 22))
    )

    assert outcome.status == "error"
    assert "RuntimeError" in outcome.message
    assert state.entries[contract.key]["status"] == "error"


def test_keyboard_interrupt_still_stops_the_run(tmp_path):
    """Ctrl+C tiene que cortar de verdad, no quedar anotado como un error mas."""
    from eex_scraper import scraper as module
    from eex_scraper.scraper import _process_contract
    from eex_scraper.state import ScrapeState

    cfg = Config()
    cfg.output.directory = str(tmp_path)
    state = ScrapeState.load(cfg.output)

    async def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    original = module._scrape_contract
    module._scrape_contract = interrupted
    try:
        with pytest.raises(KeyboardInterrupt):
            asyncio.run(
                _process_contract(make_contract(), None, cfg, state, Mode.FULL, date(2026, 9, 22))
            )
    finally:
        module._scrape_contract = original
