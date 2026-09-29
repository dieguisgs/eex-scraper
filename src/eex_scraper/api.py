"""Cliente de la API publica del EEX Market Data Hub.

Endpoints usados (los mismos que el widget de www.eex.com):

  POST {customise_url}/filter-data-with-scope
      Catalogo completo de instrumentos: shortCode, commodity, pricing, area,
      product, maturity, maturityType... El cuerpo es un JSON en base64.

  GET  {market_data_url}/table-data
      La tabla del hub para un contrato y un rango de fechas.

Ojo con el limite del lado servidor: los futuros solo devuelven ~31 dias de
negociacion y los indices ~1 ano, aunque pidas un rango mayor. El historico
completo es producto de pago (EEX Group DataSource).
"""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from .models import CSV_COLUMNS, Contract, has_values
from .tenors import delivery_start, tenor_label

log = logging.getLogger(__name__)

# Alcance "todo" para el catalogo, tal y como lo manda el widget publico.
FULL_SCOPE: list[dict[str, str]] = [
    {
        "commodity": "All",
        "pricing": "All",
        "area": "All",
        "product": "All",
        "productSpecific": "All",
        "maturityType": "All",
    }
]

# Pricings que el endpoint /table-data no acepta (las opciones tienen su propia
# tabla en el hub, /table-data-option).
UNSUPPORTED_PRICINGS = frozenset({"O"})


@dataclass
class TableResult:
    """Resultado de una consulta a /table-data para un contrato."""

    contract: Contract
    rows: list[dict[str, Any]] = field(default_factory=list)
    currency: str | None = None
    uom: str | None = None

    @property
    def is_empty(self) -> bool:
        return not self.rows

    @property
    def last_trade_date(self) -> str | None:
        dates = [r["tradeDate"] for r in self.rows if r.get("tradeDate")]
        return max(dates) if dates else None


class EexHubClient:
    """Traduce contratos a peticiones de la API y respuestas a filas de CSV."""

    def __init__(self, config, fetcher) -> None:
        self.config = config
        self.fetcher = fetcher

    # ------------------------------------------------------------------ catalogo

    async def fetch_catalog(self, scope: list[dict[str, str]] | None = None) -> list[Contract]:
        payload = base64.b64encode(
            json.dumps(scope or FULL_SCOPE, separators=(",", ":")).encode("utf-8")
        ).decode("ascii")
        url = f"{self.config.api.customise_url}/filter-data-with-scope"
        obj = await self.fetcher.post_form_json(url, {"data": payload}, {"data": payload})

        header = obj.get("header") or []
        data = obj.get("data") or []
        if not header:
            raise ValueError("El catalogo llego sin cabecera; la API ha cambiado de formato")

        contracts = [Contract.from_row(dict(zip(header, row, strict=False))) for row in data]
        log.info("Catalogo: %d contratos en bruto", len(contracts))
        return [c for c in contracts if c.short_code and c.commodity]

    # ---------------------------------------------------------------- tabla

    async def fetch_table_data(
        self, contract: Contract, start_date: date, end_date: date
    ) -> TableResult:
        if contract.pricing in UNSUPPORTED_PRICINGS:
            raise ValueError(
                f"pricing={contract.pricing!r} no lo sirve /table-data "
                "(las opciones usan /table-data-option)"
            )

        params = {
            "shortCode": contract.short_code,
            "commodity": contract.commodity,
            "pricing": contract.pricing,
            "area": contract.area,
            "product": contract.product or "",
            # El widget manda el vencimiento en el parametro `maturity` y el tipo
            # en `maturityType`. Spot/indices/subastas no tienen vencimiento: la
            # API exige igualmente ambos parametros, pero ignora su valor y
            # devuelve su propia ventana movil.
            "maturity": contract.maturity or end_date.strftime("%Y%m%d"),
            "maturityType": contract.maturity_type or "Day",
            "startDate": start_date.isoformat(),
            "endDate": end_date.isoformat(),
            "isRolling": "true",
        }
        url = f"{self.config.api.market_data_url}/table-data"
        obj = await self.fetcher.get_json(url, params)
        return self._to_result(contract, obj)

    def _to_result(self, contract: Contract, obj: dict[str, Any]) -> TableResult:
        header = obj.get("header") or []
        data = obj.get("data") or []
        currency = obj.get("currency")
        uom = obj.get("uOM")
        scraped_at = datetime.now(UTC).replace(microsecond=0).isoformat()
        start = delivery_start(contract)

        rows: list[dict[str, Any]] = []
        for raw in data:
            # strict=False: si EEX anade columnas, leemos las que conocemos
            # en vez de romper la ejecucion entera.
            record = dict(zip(header, raw, strict=False))
            # La API rellena cada dia de la ventana aunque no hubiera cotizacion;
            # esas filas vacias se descartan.
            if not record.get("tradeDate") or not has_values(record):
                continue
            row = dict.fromkeys(CSV_COLUMNS)
            row.update({k: v for k, v in record.items() if k in row})
            row.update(
                {
                    "shortCode": record.get("shortCode") or contract.short_code,
                    "maturity": contract.maturity,
                    "maturityType": contract.maturity_type,
                    "deliveryStart": start.isoformat() if start else None,
                    "tenor": tenor_label(contract.maturity_type, start),
                    "commodity": contract.commodity,
                    "pricing": contract.pricing,
                    "area": contract.area,
                    "product": contract.product,
                    "productSpecific": contract.product_specific,
                    "currency": currency,
                    "uOM": uom,
                    "scrapedAt": scraped_at,
                }
            )
            rows.append(row)

        rows.sort(key=lambda r: (str(r.get("tradeDate") or ""), str(r.get("deliveryDay") or "")))
        return TableResult(contract=contract, rows=rows, currency=currency, uom=uom)
