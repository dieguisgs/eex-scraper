"""Periodo de entrega y tenor de un futuro.

Cada futuro entrega en un periodo concreto (un dia, una semana, un mes...).
Aqui se calcula:

  deliveryStart   primer dia de entrega, en ISO (2027-01-01)
  tenor           etiqueta absoluta del periodo: 2026-10-05, 2026-W41,
                  WE 2026-W41, 2026-10, 2026-Q4, Win-26/27, Cal-2027
  relativeTenor   distancia al dia de negociacion: D+3, W+1, WE+1, M+1, Q+2,
                  S+1, Y+1. Es lo que permite leer la curva como series
                  estables ("el Cal+1 de cada dia") aunque los contratos roten.

El catalogo da el vencimiento como AAAAMM y, segun el tipo, el dia
(displayDay) o la semana ISO (displayWeek). Las temporadas empiezan en abril
(verano) u octubre (invierno).
"""

from __future__ import annotations

from datetime import date

from .models import Contract

# Orden de los tipos de vencimiento en la curva: de lo mas corto a lo mas largo.
MATURITY_ORDER: dict[str, int] = {
    "Day": 0,
    "Weekend": 1,
    "Week": 2,
    "Month": 3,
    "Quarter": 4,
    "Season": 5,
    "Year": 6,
}

_RELATIVE_PREFIX = {
    "Day": "D",
    "Weekend": "WE",
    "Week": "W",
    "Month": "M",
    "Quarter": "Q",
    "Season": "S",
    "Year": "Y",
}


def delivery_start(contract: Contract) -> date | None:
    """Primer dia de entrega del contrato, o None si no se puede deducir."""
    ym = _year_month(contract.maturity)
    if ym is None:
        return None
    year, month = ym
    mt = contract.maturity_type

    try:
        if mt == "Day":
            day = contract.display_day or _day_from_short_code(contract.short_code)
            return date(year, month, day) if day else None
        if mt in ("Week", "Weekend"):
            week = contract.display_week
            if not week:
                return None
            iso_year = year
            # La semana ISO 1 puede caer en diciembre y la 52/53 en enero.
            if week == 1 and month == 12:
                iso_year += 1
            elif week >= 52 and month == 1:
                iso_year -= 1
            return date.fromisocalendar(iso_year, week, 6 if mt == "Weekend" else 1)
        if mt in ("Month", "Quarter", "Season", "Year"):
            return date(year, month, 1)
    except ValueError:
        return None
    return None


def tenor_label(maturity_type: str | None, start: date | None) -> str | None:
    """Etiqueta absoluta del periodo de entrega."""
    if start is None or not maturity_type:
        return None
    match maturity_type:
        case "Day":
            return start.isoformat()
        case "Week":
            y, w, _ = start.isocalendar()
            return f"{y}-W{w:02d}"
        case "Weekend":
            y, w, _ = start.isocalendar()
            return f"WE {y}-W{w:02d}"
        case "Month":
            return f"{start.year}-{start.month:02d}"
        case "Quarter":
            return f"{start.year}-Q{(start.month - 1) // 3 + 1}"
        case "Season":
            if start.month >= 10:
                return f"Win-{start.year % 100:02d}/{(start.year + 1) % 100:02d}"
            return f"Sum-{start.year % 100:02d}"
        case "Year":
            return f"Cal-{start.year}"
    return None


def relative_tenor(maturity_type: str | None, start: date | None, trade_date: date) -> str | None:
    """Tenor relativo al dia de negociacion: M+1 es el mes que viene, etc."""
    if start is None or maturity_type not in _RELATIVE_PREFIX:
        return None
    match maturity_type:
        case "Day":
            n = (start - trade_date).days
        case "Week" | "Weekend":
            n = (_monday(start) - _monday(trade_date)).days // 7
        case "Month":
            n = _month_index(start) - _month_index(trade_date)
        case "Quarter":
            n = _month_index(start) // 3 - _month_index(trade_date) // 3
        case "Season":
            n = _season_index(start) - _season_index(trade_date)
        case "Year":
            n = start.year - trade_date.year
        case _:  # pragma: no cover - cubierto por el guard de arriba
            return None
    sign = "+" if n >= 0 else "-"
    return f"{_RELATIVE_PREFIX[maturity_type]}{sign}{abs(n)}"


def relative_sort_key(label: str) -> tuple[int, int]:
    """Ordena columnas relativas: D+1 < WE+1 < W+1 < M+1 < ... < Y+1."""
    for mt, prefix in sorted(_RELATIVE_PREFIX.items(), key=lambda kv: -len(kv[1])):
        if label.startswith(prefix) and label[len(prefix) : len(prefix) + 1] in "+-":
            try:
                n = int(label[len(prefix) :])
            except ValueError:
                break
            return MATURITY_ORDER[mt], n
    return len(MATURITY_ORDER), 0


def _day_from_short_code(short_code: str) -> int | None:
    """Los futuros diarios llevan el dia en los dos ultimos digitos (DB14, G301...).

    Comprobado en los 1.798 diarios del catalogo. Sirve para contratos vencidos
    que ya no estan en el catalogo y por tanto no traen displayDay.
    """
    tail = (short_code or "")[-2:]
    return int(tail) if tail.isdigit() and 1 <= int(tail) <= 31 else None


def _year_month(maturity: str | None) -> tuple[int, int] | None:
    text = (maturity or "").strip()
    if len(text) < 6 or not text[:6].isdigit():
        return None
    year, month = int(text[:4]), int(text[4:6])
    if not 1 <= month <= 12:
        return None
    return year, month


def _month_index(d: date) -> int:
    return d.year * 12 + d.month - 1


def _season_index(d: date) -> int:
    """Verano (abr-sep) del ano Y = 2Y; invierno (oct-mar) que empieza en Y = 2Y+1."""
    if d.month >= 10:
        return 2 * d.year + 1
    if d.month >= 4:
        return 2 * d.year
    return 2 * (d.year - 1) + 1


def _monday(d: date) -> date:
    return date.fromordinal(d.toordinal() - d.weekday())
