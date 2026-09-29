"""Capa de red: httpx asincrono como via principal y Playwright como respaldo.

La API del hub (api.eex-group.com) es publica y no pide credenciales, asi que
httpx basta. El respaldo con navegador existe por si EEX mete un WAF delante:
en ese caso las mismas peticiones se lanzan con fetch() desde dentro de la
pagina real del hub, que es un origen que la API ya acepta.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Protocol
from urllib.parse import urlencode

import httpx

log = logging.getLogger(__name__)

# Codigos que delatan un bloqueo (WAF/bot check) y justifican cambiar de
# estrategia. Ojo: 429 NO entra aqui, es limitacion por ratio y se reintenta.
BLOCKING_STATUSES = frozenset({401, 403, 407})
RETRY_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})

# Espera minima tras un 429, por encima del backoff normal.
RATE_LIMIT_MIN_WAIT = 2.0

_PAGE_FETCH_JS = """
async ({ method, url, body }) => {
    const init = { method: method, headers: { 'Accept': 'application/json' } };
    if (body !== null) {
        init.body = body;
        init.headers['Content-Type'] = 'application/x-www-form-urlencoded';
    }
    const res = await fetch(url, init);
    return { status: res.status, text: await res.text() };
}
"""


class RateLimiter:
    """Token bucket compartido por todos los workers.

    La API del hub corta con 429 bastante pronto, asi que no basta con limitar
    la concurrencia: hay que limitar el ritmo. Ante un 429 se aplica una
    penalizacion global (nadie pide nada durante unos segundos), que es lo que
    evita la cascada de fallos cuando varios workers chocan a la vez.
    """

    def __init__(self, requests_per_second: float, burst: int) -> None:
        self.rate = max(0.01, float(requests_per_second))
        self.capacity = max(1, int(burst))
        self._tokens = float(self.capacity)
        self._updated = time.monotonic()
        self._penalty_until = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        while True:
            async with self._lock:
                now = time.monotonic()
                if now >= self._penalty_until:
                    elapsed = now - self._updated
                    self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
                    self._updated = now
                    if self._tokens >= 1.0:
                        self._tokens -= 1.0
                        return
                    wait = (1.0 - self._tokens) / self.rate
                else:
                    wait = self._penalty_until - now
            await asyncio.sleep(min(wait, 5.0))

    async def penalize(self, seconds: float) -> None:
        """Congela todas las peticiones durante `seconds` y vacia el bucket."""
        async with self._lock:
            now = time.monotonic()
            self._penalty_until = max(self._penalty_until, now + seconds)
            self._tokens = 0.0
            self._updated = now


class FetchError(RuntimeError):
    """Fallo de red o respuesta no utilizable."""


class HttpStatusError(FetchError):
    """La API respondio con un status de error."""

    def __init__(self, status: int, url: str, body: str = "") -> None:
        super().__init__(f"HTTP {status} en {url}: {body[:200]}")
        self.status = status
        self.url = url
        self.body = body


class BlockedError(FetchError):
    """Parece un bloqueo del lado del servidor; toca probar con navegador."""


class Fetcher(Protocol):
    async def get_json(self, url: str, params: dict[str, Any]) -> Any: ...

    async def post_form_json(
        self, url: str, params: dict[str, Any], data: dict[str, Any]
    ) -> Any: ...

    async def aclose(self) -> None: ...


class HttpxFetcher:
    """Cliente HTTP asincrono con reintentos y backoff exponencial."""

    def __init__(self, api_config, limiter: RateLimiter | None = None) -> None:
        self.cfg = api_config
        self.limiter = limiter
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(api_config.timeout_seconds),
            headers={
                "User-Agent": api_config.user_agent,
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Accept-Language": "en-GB,en;q=0.9,es;q=0.8",
                "Referer": api_config.referer,
                "Origin": api_config.referer.rstrip("/"),
            },
            follow_redirects=True,
        )

    async def get_json(self, url: str, params: dict[str, Any]) -> Any:
        return await self._request("GET", url, params=params)

    async def post_form_json(self, url: str, params: dict[str, Any], data: dict[str, Any]) -> Any:
        return await self._request("POST", url, params=params, data=data)

    async def _request(
        self,
        method: str,
        url: str,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
    ) -> Any:
        last_error: Exception | None = None
        for attempt in range(self.cfg.max_retries + 1):
            wait = self.cfg.backoff_seconds * (2**attempt)
            if self.limiter is not None:
                await self.limiter.acquire()
            try:
                response = await self._client.request(method, url, params=params, data=data)
            except httpx.HTTPError as exc:
                last_error = FetchError(f"Error de red en {url}: {exc}")
                log.debug("intento %d fallido (%s)", attempt + 1, exc)
            else:
                if response.is_success:
                    return _parse_json(response.text, url)
                status = response.status_code
                if status in BLOCKING_STATUSES:
                    raise BlockedError(f"HTTP {status} en {url}: posible bloqueo/WAF")
                if status not in RETRY_STATUSES:
                    raise HttpStatusError(status, url, response.text)
                if status == 429:
                    wait = max(wait, RATE_LIMIT_MIN_WAIT, _retry_after(response))
                    if self.limiter is not None:
                        # Freno global: el 429 es de toda la sesion, no de esta
                        # peticion, asi que paramos a todos los workers.
                        await self.limiter.penalize(wait)
                last_error = HttpStatusError(status, url, response.text)
                log.debug("intento %d devolvio HTTP %d", attempt + 1, status)

            if attempt < self.cfg.max_retries:
                await asyncio.sleep(wait)

        raise last_error or FetchError(f"Sin respuesta utilizable de {url}")

    async def aclose(self) -> None:
        await self._client.aclose()


class BrowserFetcher:
    """Respaldo con Playwright: lanza las peticiones desde la pagina del hub."""

    def __init__(self, browser_config, api_config) -> None:
        self.cfg = browser_config
        self.api_cfg = api_config
        self._playwright = None
        self._browser = None
        self._page = None
        self._lock = asyncio.Lock()

    async def _ensure_page(self):
        if self._page is not None:
            return self._page
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:  # pragma: no cover - depende del entorno
            raise FetchError(
                "Playwright no esta instalado. Ejecuta:\n"
                "  uv sync --extra browser\n"
                "  uv run playwright install chromium"
            ) from exc

        log.info("Arrancando navegador de respaldo (%s)...", self.cfg.browser)
        self._playwright = await async_playwright().start()
        launcher = getattr(self._playwright, self.cfg.browser)
        self._browser = await launcher.launch(headless=self.cfg.headless)
        context = await self._browser.new_context(user_agent=self.api_cfg.user_agent)
        self._page = await context.new_page()
        await self._page.goto(self.cfg.page_url, wait_until="domcontentloaded")
        return self._page

    async def get_json(self, url: str, params: dict[str, Any]) -> Any:
        return await self._fetch_in_page("GET", _with_params(url, params), None)

    async def post_form_json(self, url: str, params: dict[str, Any], data: dict[str, Any]) -> Any:
        return await self._fetch_in_page("POST", _with_params(url, params), urlencode(data))

    async def _fetch_in_page(self, method: str, url: str, body: str | None) -> Any:
        # Una sola pestana compartida: serializamos los accesos.
        async with self._lock:
            page = await self._ensure_page()
            result = await page.evaluate(
                _PAGE_FETCH_JS,
                {"method": method, "url": url, "body": body},
            )
        status = int(result["status"])
        if status >= 400:
            raise HttpStatusError(status, url, result.get("text", ""))
        return _parse_json(result.get("text", ""), url)

    async def aclose(self) -> None:
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None
        self._page = None


class ResilientFetcher:
    """httpx primero; al primer bloqueo, cambia a navegador para el resto."""

    def __init__(
        self,
        api_config,
        browser_config,
        force_browser: bool = False,
        limiter: RateLimiter | None = None,
    ) -> None:
        self.api_cfg = api_config
        self.browser_cfg = browser_config
        self._primary = None if force_browser else HttpxFetcher(api_config, limiter)
        self._browser: BrowserFetcher | None = None
        self._using_browser = force_browser
        self._warned_no_playwright = False
        if force_browser:
            self._browser = BrowserFetcher(browser_config, api_config)

    @property
    def using_browser(self) -> bool:
        return self._using_browser

    async def get_json(self, url: str, params: dict[str, Any]) -> Any:
        return await self._dispatch("get_json", url, params, None)

    async def post_form_json(self, url: str, params: dict[str, Any], data: dict[str, Any]) -> Any:
        return await self._dispatch("post_form_json", url, params, data)

    async def _dispatch(
        self, op: str, url: str, params: dict[str, Any], data: dict[str, Any] | None
    ) -> Any:
        args = (url, params) if data is None else (url, params, data)
        if not self._using_browser and self._primary is not None:
            try:
                return await getattr(self._primary, op)(*args)
            except BlockedError as exc:
                if not self.browser_cfg.fallback_enabled:
                    raise
                if not _playwright_available():
                    # Sin navegador instalado no hay respaldo posible: mejor
                    # decirlo una vez y seguir con httpx que fallar en bucle.
                    if not self._warned_no_playwright:
                        self._warned_no_playwright = True
                        log.warning(
                            "API bloqueada (%s) y Playwright no esta instalado. "
                            "Instala el respaldo con: uv sync --extra browser && "
                            "uv run playwright install chromium",
                            exc,
                        )
                    raise
                log.warning("API bloqueada (%s). Cambiando al respaldo con navegador.", exc)
                self._using_browser = True

        if self._browser is None:
            self._browser = BrowserFetcher(self.browser_cfg, self.api_cfg)
        return await getattr(self._browser, op)(*args)

    async def aclose(self) -> None:
        if self._primary is not None:
            await self._primary.aclose()
        if self._browser is not None:
            await self._browser.aclose()


def _retry_after(response: httpx.Response) -> float:
    """Segundos indicados por la cabecera Retry-After, si viene y es un numero."""
    raw = response.headers.get("Retry-After")
    try:
        return float(raw) if raw else 0.0
    except ValueError:
        return 0.0


def _playwright_available() -> bool:
    from importlib.util import find_spec

    return find_spec("playwright") is not None


def _with_params(url: str, params: dict[str, Any]) -> str:
    query = urlencode({k: ("" if v is None else v) for k, v in params.items()})
    return f"{url}?{query}" if query else url


def _parse_json(text: str, url: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise FetchError(f"Respuesta no-JSON desde {url}: {text[:200]}") from exc
