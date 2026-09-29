"""Modelo de contrato y esquema de columnas del CSV de salida."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# Orden de columnas del CSV por contrato. Es la union de las dos cabeceras que
# devuelve /table-data: la de futuros (con open interest) y la de spot/indices
# (con deliveryDay), mas los metadatos del contrato, su periodo de entrega
# (solo futuros, ver tenors.py) y la marca de scrapeo.
CSV_COLUMNS: list[str] = [
    "tradeDate",
    "shortCode",
    "maturityDate",
    "maturity",
    "maturityType",
    "deliveryStart",
    "tenor",
    "commodity",
    "pricing",
    "area",
    "product",
    "productSpecific",
    "settlPx",
    "currency",
    "totVolTrdd",
    "uOM",
    "grossOpenInt",
    "grossOpenIntSz",
    "netOpenInt",
    "netOpenIntSz",
    "deliveryDay",
    "scrapedAt",
]

# Clave de deduplicacion dentro de un CSV de contrato.
DEDUP_KEYS: tuple[str, ...] = ("tradeDate", "deliveryDay")

# Columnas con datos de mercado. La API devuelve una fila por cada dia de
# negociacion de la ventana aunque el contrato no cotizase (todo a null): una
# fila sin ninguno de estos valores no aporta nada y no se guarda.
VALUE_COLUMNS: tuple[str, ...] = (
    "settlPx",
    "totVolTrdd",
    "grossOpenInt",
    "grossOpenIntSz",
    "netOpenInt",
    "netOpenIntSz",
)

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")

PRICING_LABELS = {
    "F": "Futures",
    "S": "Spot",
    "I": "Index",
    "A": "Auction",
    "O": "Option",
}

# Carpeta de cada tipo de precio dentro de <COMMODITY>/<AREA>/.
PRICING_FOLDERS = {
    "F": "Futures",
    "S": "Spot",
    "I": "Indices",
    "A": "Auctions",
    "O": "Options",
}


def sanitize(value: Any, fallback: str = "NA") -> str:
    """Convierte un valor en un fragmento de ruta seguro en Windows y Linux."""
    text = "" if value is None else str(value).strip()
    if not text:
        return fallback
    cleaned = _UNSAFE.sub("_", text).strip("._")
    return cleaned or fallback


@dataclass(frozen=True)
class Contract:
    """Una fila del catalogo del hub: un instrumento con su vencimiento."""

    short_code: str
    maturity: str | None
    maturity_type: str | None
    commodity: str
    pricing: str
    area: str
    product: str | None
    product_specific: str | None = None
    valuation_method: str | None = None
    display_year: int | None = None
    display_month: int | None = None
    display_quarter: int | None = None
    display_season: int | None = None
    display_week: int | None = None
    display_day: int | None = None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> Contract:
        return cls(
            short_code=str(row.get("shortCode") or "").strip(),
            maturity=_opt_str(row.get("maturity")),
            maturity_type=_opt_str(row.get("maturityType")),
            commodity=str(row.get("commodity") or "").strip(),
            pricing=str(row.get("pricing") or "").strip(),
            area=str(row.get("area") or "").strip(),
            product=_opt_str(row.get("product")),
            product_specific=_opt_str(row.get("productSpecific")),
            valuation_method=_opt_str(row.get("valuationMethod")),
            display_year=_opt_int(row.get("displayYear")),
            display_month=_opt_int(row.get("displayMonth")),
            display_quarter=_opt_int(row.get("displayQuarter")),
            display_season=_opt_int(row.get("displaySeason")),
            display_week=_opt_int(row.get("displayWeek")),
            display_day=_opt_int(row.get("displayDay")),
        )

    @property
    def key(self) -> str:
        """Identificador estable del contrato, usado en el fichero de estado."""
        return "|".join(
            [
                self.commodity,
                self.area,
                self.pricing,
                self.short_code,
                self.maturity or "-",
                self.maturity_type or "-",
            ]
        )

    @property
    def file_stem(self) -> str:
        """Nombre de fichero: <shortCode>_<maturity>, saneado."""
        code = sanitize(self.short_code, "UNKNOWN")
        if self.maturity:
            return f"{code}_{sanitize(self.maturity)}"
        # Spot, indices y subastas no tienen vencimiento: los distingue el pricing.
        return f"{code}_{PRICING_LABELS.get(self.pricing, self.pricing or 'NA').upper()}"

    @property
    def path_parts(self) -> tuple[str, ...]:
        """Ruta relativa del CSV dentro de la carpeta de datos.

        <COMMODITY>/<AREA>/<Futures|Spot|Indices|Auctions>/<producto>/[<vencimiento>/]<fichero>

        El nivel del tipo de vencimiento (Day, Month, Year...) solo existe para
        contratos con vencimiento, es decir, futuros.
        """
        parts = [
            sanitize(self.commodity, "UNKNOWN"),
            sanitize(self.area, "NA"),
            PRICING_FOLDERS.get(self.pricing, sanitize(self.pricing, "NA")),
            sanitize(self.product, "NA"),
        ]
        if self.maturity and self.maturity_type:
            parts.append(sanitize(self.maturity_type))
        return (*parts, f"{self.file_stem}.csv")

    @property
    def label(self) -> str:
        parts = [self.commodity, self.area, self.short_code]
        if self.maturity:
            parts.append(self.maturity)
        return " / ".join(p for p in parts if p)

    def to_catalog_row(self) -> dict[str, Any]:
        return {
            "shortCode": self.short_code,
            "maturity": self.maturity,
            "maturityType": self.maturity_type,
            "commodity": self.commodity,
            "pricing": self.pricing,
            "pricingLabel": PRICING_LABELS.get(self.pricing, self.pricing),
            "area": self.area,
            "product": self.product,
            "productSpecific": self.product_specific,
            "valuationMethod": self.valuation_method,
            "displayYear": self.display_year,
            "displayMonth": self.display_month,
            "displayQuarter": self.display_quarter,
            "displaySeason": self.display_season,
            "displayWeek": self.display_week,
            "displayDay": self.display_day,
            "csvPath": "/".join(self.path_parts),
        }


CATALOG_COLUMNS: list[str] = list(
    Contract(
        short_code="",
        maturity=None,
        maturity_type=None,
        commodity="",
        pricing="",
        area="",
        product=None,
    )
    .to_catalog_row()
    .keys()
)


def has_values(row: dict[str, Any]) -> bool:
    """True si la fila trae algun dato de mercado (precio, volumen u open interest)."""
    return any(row.get(c) not in (None, "") for c in VALUE_COLUMNS)


def _opt_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _opt_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
