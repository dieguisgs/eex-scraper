# eex-scraper

Scraper del [EEX Market Data Hub](https://www.eex.com/en/market-data/market-data-hub):
power, natural gas, environmentals, freight, agriculturals, guarantees of origin
e hidrógeno.

> **Guía completa — cómo funciona todo y cómo se ejecuta: [GUIA.md](GUIA.md).**
> Lo mínimo: `uv sync` una vez, ajustar la ruta en `config.toml` y
> `python run.py`.

## Cómo saca los datos

La tabla del hub **no está en el HTML**. La página monta un widget que consume
una API JSON pública de EEX, y eso es lo que usa este scraper:

| Endpoint | Para qué |
|---|---|
| `POST api.eex-group.com/pub/customise-widget/filter-data-with-scope` | Catálogo completo de contratos (~7.800: shortCode, commodity, área, producto, vencimiento…) |
| `GET api.eex-group.com/pub/market-data/table-data` | La tabla: precio de liquidación, volumen, open interest, por fecha de negociación |

Ir contra la API en vez de contra el DOM es más rápido, más estable y devuelve
exactamente los mismos números que ves en la web. Playwright queda como
**respaldo**: si EEX pusiera un WAF delante, el scraper abre el hub real en un
navegador y lanza los mismos `fetch()` desde dentro de la página.

## Límite importante: cuánta historia hay

La API pública sirve una **ventana móvil**, no el histórico completo:

- **Futuros**: ~31 días de negociación (unas 6 semanas).
- **Spot**: ~40 días.
- **Índices**: ~1 año.

Pedir un rango mayor no da error, simplemente devuelve lo mismo (comprobado
pidiendo desde 2015). El histórico largo es producto de pago (EEX Group
DataSource, con API key).

Por eso cada ejecución pide **la ventana entera** y la contrasta con lo que ya
hay: **ejecutándolo a diario, la ruta de salida va acumulando el histórico** que la API
sola no te da. Si pasan más de ~6 semanas sin ejecutarlo, se pierden días de
futuros.

## Límite de ratio: cuánto tarda

La API corta con HTTP 429 sobre las **60 peticiones/minuto** (medido). El
scraper lleva un limitador de ritmo compartido (token bucket) y, ante un 429,
frena a todos los workers y reintenta, así que no hace falta tocar nada. Pero
marca el tiempo de un barrido completo, a una petición por contrato:

| Selección | Contratos | Tiempo aprox. |
|---|---:|---:|
| Todo (sin opciones) | 7.323 | ~2 h 15 min |
| POWER | 3.890 | ~72 min |
| NATGAS | 2.226 | ~41 min |
| FREIGHT | 972 | ~18 min |
| AGRICULTURALS | 106 | ~2 min |
| ENVIRONMENTALS | 92 | ~2 min |
| GO | 36 | ~1 min |

Si solo te interesan power, gas y emisiones, `-C POWER -C NATGAS -C
ENVIRONMENTALS` son ~1 h 55 min. Déjalo corriendo de fondo: si se corta,
volver a lanzar `run` continúa donde se quedó (se salta lo hecho en las
últimas `min_refresh_hours`).

## Instalación

Es un proyecto [uv](https://docs.astral.sh/uv/) para ejecutar, no para
instalar (`pyproject.toml` + `uv.lock` solo fijan Python y dependencias):

```bash
uv sync                       # crea .venv con las dependencias
uv sync --extra browser       # opcional: respaldo con Playwright
uv run playwright install chromium
```

## Ruta de salida

Todo se escribe bajo **una ruta base**, que se fija en `config.toml`:

```toml
[output]
directory = "D:/datos/eex"     # absoluta
# directory = "output"         # o relativa a la carpeta del config.toml
# directory = "%USERPROFILE%/eex"   # admite ~ y variables de entorno
```

Para una ejecución puntual se puede pisar sin tocar el config:
`python run.py -o otra/ruta`.

Qué `config.toml` se usa: el de `--config`, si no el de la variable de entorno
`EEX_SCRAPER_CONFIG`, y si no un `config.toml` en la carpeta actual o en una
superior. `run.py` usa siempre el que tiene al lado.

## Uso

```bash
python run.py                 # lo de cada día: descarga todo, fusiona, regenera curvas
python run.py info            # qué hay ya descargado
```

`run.py` funciona con cualquier Python: si el que lo lanza no tiene las
dependencias, se relanza solo con `uv run` usando el `.venv` del proyecto. Da
igual desde qué carpeta se ejecute. Todos los comandos y opciones van detrás:
`python run.py run -C POWER`, `python run.py --help`...

No es un paquete instalable: `pyproject.toml` solo declara Python y
dependencias para que `uv sync` monte el entorno. El código se usa
directamente desde `src/`.

Cada ejecución:

1. Baja el catálogo de contratos del hub.
2. Pide a cada contrato **toda la ventana** que la API deja ver.
3. La **contrasta con lo guardado**: solo entran filas nuevas o precios que EEX
   ha corregido; lo idéntico no se toca y los ficheros sin cambios no se
   reescriben. Las filas sin ningún dato (la API rellena con vacíos los días
   sin cotización) no se guardan.
4. Regenera `curves/` e `inventory.csv`.

Otros comandos:

```bash
python run.py run [filtros]   # lo mismo que sin comando, con filtros
python run.py full            # como run, sin saltarse los que ya están al día
python run.py curves          # regenera curvas e inventario sin tocar la red
python run.py catalog         # solo el catálogo
```

Cada comando que hace algo (todos menos `info`) deja log en la ruta de
`[logging]` del config; por defecto `<ruta base>/_logs/eex-scraper_AAAA-MM-DD.log`.
Lleva inicio, progreso, resumen, avisos, el error completo si algo falla y una
línea de fin con el código de salida. Se guardan `keep_days` días (60).

Dos ejecuciones sobre la misma ruta base no pueden solaparse: la segunda avisa y
sale (bloqueo en `_state/run.lock`, que se libera solo si el proceso muere).

### Filtros

Los mismos en `run` y `full`, y pisan a los de `config.toml`:

```bash
python run.py run -C POWER -C NATGAS          # solo power y gas
python run.py run -C ENVIRONMENTALS -A EU     # emisiones de la zona EU
python run.py run -A DE -A ES -P Base         # base alemán y español
python run.py run -s DEBY -s FEU2             # contratos concretos
python run.py run --pricing I                 # solo índices
python run.py run -n 50 --dry-run             # ver qué haría, sin red
```

Otras opciones útiles:

- `--browser` fuerza el scrapeo vía Playwright.
- `--no-catalog-refresh` reutiliza el catálogo ya guardado.
- `-c ruta/config.toml` usa otro fichero de configuración.
- `-v` más detalle por pantalla.

## Salida

```
<ruta base>/
  catalog/contracts.csv           catálogo completo del hub
  inventory.csv                   un fichero de datos por fila: fechas, nº de filas, último precio
  table_data/                     UN CSV POR CONTRATO (fuente de verdad)
    <COMMODITY>/<ÁREA>/<Futures|Spot|Indices|Auctions>/<producto>/[<vencimiento>/]<contrato>.csv
    POWER/DE/Futures/Base/Year/DEBY_202801.csv
    POWER/DE/Futures/Peak/Month/DEPM_202611.csv
    NATGAS/TTF/Futures/Physical/Month/G3BM_202612.csv
    ENVIRONMENTALS/EU/Indices/EUA/EEX_ECarbix_Month_Index_INDEX.csv
  curves/                         CURVAS POR PRODUCTO Y FECHA DE REFERENCIA
    POWER/DE/Base.csv             formato largo
    POWER/DE/Base_wide.csv        formato ancho
  _state/scrape_state.json        qué se scrapeó, cuándo y con qué resultado
  _logs/eex-scraper_AAAA-MM-DD.log   log del día (ruta configurable en [logging])
```

### Curvas (`curves/`)

Para cada producto de futuros (p. ej. POWER DE Base, que junta diarios,
semanas, meses, trimestres y años), la cotización de todos sus tenors en cada
fecha de referencia. Se regeneran enteras en cada ejecución desde `table_data/`.

`Base_wide.csv`: una fila por fecha de referencia, una columna por **tenor
relativo** y el precio de liquidación. Cada columna es una serie continua
aunque los contratos roten (el `Y+1` de hoy es Cal-2027; en enero pasará a ser
Cal-2028):

```csv
tradeDate,...,M+1,M+2,...,Q+1,Q+2,...,Y+1,Y+2,Y+3,...
2026-09-28,...,165.23,170.69,...,167.25,163.25,...,129.28,99.35,87.77,...
```

`Base.csv`: formato largo, una fila por (fecha de referencia, contrato), con
tenor absoluto y relativo, precio, volumen y open interest. Es el que conviene
para filtrar en Excel o pandas:

```csv
tradeDate,relativeTenor,tenor,maturityType,deliveryStart,shortCode,maturity,settlPx,totVolTrdd,grossOpenInt,netOpenInt,currency,uOM
2026-09-28,M+1,2026-10,Month,2026-10-01,DEBM,202610,165.23,3409865,339784,,EUR,MWh
2026-09-28,Q+1,2026-Q4,Quarter,2026-10-01,DEBQ,202610,167.25,994050,250418,,EUR,MWh
2026-09-28,Y+1,Cal-2027,Year,2027-01-01,DEBY,202701,129.28,5072040,109145,,EUR,MWh
2026-09-28,Y+2,Cal-2028,Year,2028-01-01,DEBY,202801,99.35,1510848,30111,,EUR,MWh
```

Tenors: `D` día, `WE` fin de semana, `W` semana, `M` mes, `Q` trimestre,
`S` temporada (verano abr-sep, invierno oct-mar), `Y` año. `M+0` es el mes en
curso. Cuando un producto tiene varios contratos con el mismo tenor (las rutas
de freight), la columna lleva delante el código: `C5TM M+1`, `C7EM M+1`.

Spot e índices no tienen tenors: su serie está directamente en su CSV de
`table_data/`.

### CSV por contrato (`table_data/`)

Una fila por fecha de negociación:

```csv
tradeDate,shortCode,maturityDate,maturity,maturityType,deliveryStart,tenor,commodity,pricing,area,product,productSpecific,settlPx,currency,totVolTrdd,uOM,grossOpenInt,grossOpenIntSz,netOpenInt,netOpenIntSz,deliveryDay,scrapedAt
2026-09-21,DEBY,202801,202801,Year,2028-01-01,Cal-2028,POWER,F,DE,Base,,97.47,EUR,1967616,MWh,29546,259532064,,,,2026-09-22T13:40:11+00:00
```

Columnas: `settlPx` precio de liquidación, `totVolTrdd` volumen negociado,
`grossOpenInt` / `netOpenInt` open interest (en lotes) y sus `*Sz` en unidades
físicas, `deliveryStart` / `tenor` periodo de entrega (futuros), `deliveryDay`
día de entrega (solo spot e índices).

### Cómo se fusionan las ejecuciones

Al volver a scrapear un contrato, lo nuevo se fusiona con lo que ya había,
deduplicando por `(tradeDate, deliveryDay)`. **Gana siempre la lectura más
reciente**, porque EEX revisa precios de liquidación ya publicados, salvo que
venga vacía: una fila sin datos nunca pisa un precio guardado. Nunca se pierde
una fecha ya guardada, aunque la API deje de servirla.

La escritura es atómica (fichero temporal + reemplazo), así que un corte a
mitad no deja CSV truncados.

## Ejecución diaria

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1          # cada día a las 20:00
powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1 -At 21:30
powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1 -Remove  # quitarla
```

Registra la tarea «EEX Scraper» en el Programador de tareas de Windows, que
lanza `scripts\run_daily.ps1`, es decir, `run.py`. El log queda en la ruta
de `[logging]`. Si el equipo está apagado a esa hora, corre al
encenderlo. Necesita la sesión de usuario iniciada.

`run` se salta los contratos scrapeados hace menos de `min_refresh_hours` (4 en config.toml);
`full` no se salta ninguno. Borrar `<ruta base>/_state/scrape_state.json` no pierde
datos: solo hace que la siguiente ejecución vuelva a pedir todos los contratos.

### Migración desde el layout antiguo

Los CSV de versiones anteriores (`table_data/<COMMODITY>/<ÁREA>/<contrato>.csv`)
se mueven solos a la estructura nueva en la primera ejecución, quitando las
filas vacías y rellenando el tenor. También a mano, sin red:
`python run.py migrate`.

## Configuración

Todo se ajusta en `config.toml` (concurrencia, reintentos, ventana de fechas,
filtros por defecto, delimitador y codificación del CSV, respaldo con
navegador). Cada opción está comentada en el propio fichero.

Tres que conviene mirar:

- `scrape.requests_per_second` (0.9): el freno real. Subirlo solo consigue más
  429 y más esperas.
- `scrape.min_refresh_hours` (4): cuánto tiene que pasar para que `run`
  vuelva a mirar un contrato. A 0, no se salta ninguno.
- `output.encoding` (`utf-8-sig`): abre bien en Excel. Pon `utf-8` si prefieres
  el fichero limpio.

## Tests

```bash
uv run pytest        # 104 tests, sin red
uv run ruff check src tests run.py
uv run ruff format --check src tests run.py
```

Cubren rutas de fichero, fusión y deduplicación de CSV (incluidas las filas
vacías), filtros, parámetros que se mandan a la API, periodos de entrega y
tenors, curvas, migración del layout antiguo, bloqueo entre ejecuciones,
validación de `config.toml` (ruta base y del log), el log (contenido, nivel,
rotación, fallos), cada comando de la CLI y `run.py` lanzado como proceso.
Detalle por fichero en [GUIA.md](GUIA.md#25-tests).

## ¿De verdad son los datos de la web?

Comprobado abriendo el hub en un navegador real y comparando:

1. La web hace exactamente estas llamadas (capturadas de su tráfico de red):
   `POST /pub/customise-widget/filter-data-with-scope` y
   `GET /pub/market-data/table-data?shortCode=DEBY&commodity=POWER&pricing=F&area=DE&product=Base&maturity=202701&maturityType=Year&isRolling=true`
   — los mismos parámetros que manda este scraper.
2. La fila que la web pinta para DEBY 2027 el 2026-09-21 es
   `vol=5.974.320  OI=107.118  settlPx=127,62`, idéntica a la del CSV
   generado.
3. El HTML servido por `eex.com` no contiene ningún dato: sus únicos `<table>`
   son los del aviso de cookies. La tabla la monta JavaScript desde esa API.

Los dos caminos del scraper (httpx y Playwright) producen datos idénticos:
scrapeando los mismos 11 contratos por navegador y luego por httpx, el
resultado es 11 «sin cambios», 0 filas actualizadas.

## Notas

- Las **opciones** (`pricing = "O"`, 469 contratos) viven en otra tabla del hub
  (`/table-data-option`, con strikes y call/put) y el endpoint principal las
  rechaza con HTTP 400, así que quedan fuera por defecto.
- Bastantes contratos no tienen **ningún dato** (vencimientos lejanos sin
  negociar, índices descatalogados): la API devuelve sus días con todo vacío.
  No se les crea CSV; quedan anotados como `empty` en el fichero de estado.
- Los contratos **vencidos** desaparecen del catálogo y dejan de pedirse, pero
  su CSV se conserva con todo lo acumulado.
- Los datos son de EEX y están sujetos a sus condiciones de licencia; esto es
  para uso propio, no para redistribuir.
