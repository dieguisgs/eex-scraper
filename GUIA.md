# EEX Scraper — Guía completa

Descarga los datos del [EEX Market Data Hub](https://www.eex.com/en/market-data/market-data-hub)
(electricidad, gas, emisiones, fletes, agrícolas, garantías de origen e
hidrógeno), los acumula día a día sin duplicar nada y construye las **curvas
forward** de cada producto por fecha de referencia.

- [Parte 1 — Cómo funciona](#parte-1--cómo-funciona)
  1. [Qué hace, en una frase por paso](#11-qué-hace-en-una-frase-por-paso)
  2. [De dónde salen los datos](#12-de-dónde-salen-los-datos)
  3. [Límites de la API](#13-límites-de-la-api)
  4. [Qué pasa en cada ejecución](#14-qué-pasa-en-cada-ejecución)
  5. [Cómo se evita duplicar](#15-cómo-se-evita-duplicar)
  6. [Qué se genera y dónde](#16-qué-se-genera-y-dónde)
  7. [Curvas y tenors](#17-curvas-y-tenors)
  8. [Estado, reanudación y bloqueo](#18-estado-reanudación-y-bloqueo)
  9. [El log](#19-el-log)
  10. [Mapa del código](#110-mapa-del-código)
- [Parte 2 — Cómo se ejecuta](#parte-2--cómo-se-ejecuta)
  1. [Instalación](#21-instalación)
  2. [Configuración](#22-configuración)
  3. [Ejecutar](#23-ejecutar)
  4. [Ejecución diaria automática](#24-ejecución-diaria-automática)
  5. [Tests](#25-tests)
  6. [Problemas frecuentes](#26-problemas-frecuentes)

---

# Parte 1 — Cómo funciona

## 1.1 Qué hace, en una frase por paso

1. Baja el **catálogo** de contratos del hub (~7.900).
2. A cada contrato le pide **toda la historia que la API deja ver**.
3. La **compara con lo que ya hay guardado**: solo entra lo nuevo o lo que EEX
   ha corregido.
4. Guarda **un CSV por contrato**, ordenado por commodity, zona, tipo de
   precio, producto y vencimiento.
5. Regenera las **curvas** (todos los tenors de cada producto, por fecha de
   referencia) y un **inventario** de lo descargado.
6. Deja constancia de todo en un **log** diario.

Ejecutándolo cada día, la carpeta de salida va acumulando un histórico que la
API por sí sola no da.

## 1.2 De dónde salen los datos

La tabla del hub **no está en el HTML** de eex.com: la página monta un widget
que consume una API JSON pública. El scraper llama a esa misma API, con los
mismos parámetros que la web (capturados de su tráfico de red):

| Llamada | Para qué |
|---|---|
| `POST api.eex-group.com/pub/customise-widget/filter-data-with-scope` | Catálogo: shortCode, commodity, zona, producto, tipo de precio, vencimiento, día/semana de entrega |
| `GET api.eex-group.com/pub/market-data/table-data` | Datos de un contrato: una fila por día de negociación |

Cada fila trae:

| Columna | Qué es |
|---|---|
| `settlPx` | Precio de liquidación |
| `totVolTrdd` | Volumen negociado |
| `grossOpenInt`, `netOpenInt` | Open interest en lotes (`*Sz`: en unidades físicas) |
| `deliveryDay` | Día de entrega (solo spot e índices) |
| `currency`, `uOM` | Moneda y unidad (EUR, MWh, t...) |

Comprobado contra la web: la fila que la web pinta para DEBY 2027 el
2026-09-21 (`vol 5.974.320 · OI 107.118 · settlPx 127,62`) es idéntica a la del
CSV.

Si EEX pusiera un bloqueo (403/Cloudflare) delante de la API, el scraper tiene
un **respaldo con navegador** (Playwright): abre el hub real y lanza las mismas
peticiones desde dentro de la página.

## 1.3 Límites de la API

**Historia disponible — ventana móvil, no histórico completo:**

| Tipo | Cuánto devuelve |
|---|---|
| Futuros | últimas ~31 sesiones (~6 semanas) |
| Spot | ~40 días |
| Índices | ~1 año |

Pedir un rango mayor no da error, simplemente devuelve lo mismo (comprobado
pidiendo desde 2015). El histórico largo es un producto de pago de EEX. Por
eso el scraper está pensado para ejecutarse **a diario**: si pasan más de ~6
semanas sin ejecutarlo, se pierden días de futuros que ya no se pueden
recuperar.

**Ritmo — ~60 peticiones por minuto:** por encima, la API responde HTTP 429.
El scraper va a 0,9 peticiones/segundo con un limitador compartido por todas
las peticiones; si aun así llega un 429, frena a todos y reintenta. Una pasada
completa es una petición por contrato:

| Selección | Contratos | Tiempo |
|---|---:|---:|
| Todo (sin opciones) | ~7.440 | ~2 h 15 min |
| POWER | ~3.970 | ~72 min |
| NATGAS | ~2.260 | ~41 min |
| FREIGHT | 972 | ~18 min |
| AGRICULTURALS / ENVIRONMENTALS / GO | ~100 cada uno | 1–2 min |

**Opciones** (479 contratos): viven en otra tabla del hub (con strikes y
call/put) que este endpoint no sirve, así que quedan fuera.

## 1.4 Qué pasa en cada ejecución

```
python run.py
 │
 ├─ 1. Lee config.toml                       (ruta base, log, filtros, ritmo...)
 ├─ 2. Abre el log del día                   ([logging].directory)
 ├─ 3. Coge el bloqueo de la ruta base       (si otra ejecución lo tiene: avisa y sale)
 ├─ 4. Descarga el catálogo                  → catalog/contracts.csv
 ├─ 5. Reorganiza CSV de versiones antiguas  (solo si los hay)
 ├─ 6. Selecciona contratos                  (filtros del config o de la línea de comandos)
 ├─ 7. Por cada contrato, con 4 en paralelo y a ~1 petición/s:
 │      ├─ ¿scrapeado con éxito hace < min_refresh_hours?  → se salta (solo en `run`)
 │      ├─ pide toda la ventana a la API
 │      ├─ descarta los días sin ningún dato
 │      ├─ fusiona con el CSV existente (ver 1.5)
 │      ├─ escribe el CSV solo si algo cambió
 │      └─ anota el resultado en _state/scrape_state.json (se guarda cada 100)
 ├─ 8. Regenera inventory.csv y curves/
 └─ 9. Resumen por pantalla y al log; suelta el bloqueo
```

Si un contrato falla (red, respuesta rara...), se anota como error y el resto
sigue: un fallo suelto no tumba una pasada de dos horas. En la siguiente
ejecución se reintenta.

## 1.5 Cómo se evita duplicar

Cada CSV de contrato se **fusiona** con lo nuevo, nunca se reescribe a ciegas:

- **Clave:** `(tradeDate, deliveryDay)`. Una fila por día de negociación (y por
  día de entrega en spot/índices).
- **Fila que no existía** → se añade.
- **Fila idéntica** → no se toca (la marca `scrapedAt` no cuenta como cambio).
- **Fila distinta** → gana la lectura nueva, porque EEX corrige precios de
  liquidación ya publicados.
- **Fila vacía** (la API rellena con vacíos los días sin cotización) → no se
  guarda nunca, y **nunca pisa un precio ya guardado**.
- Una fecha guardada **no se pierde nunca**, aunque la API deje de servirla.
- Si nada cambió, el fichero ni se reescribe.
- La escritura es atómica (fichero temporal + reemplazo): un corte a mitad no
  deja un CSV roto.
- Un contrato que nunca ha cotizado no genera CSV (queda como `empty` en el
  estado).

## 1.6 Qué se genera y dónde

Todo cuelga de la **ruta base** (`[output].directory` del config):

```
<ruta base>/
├─ table_data/                         UN CSV POR CONTRATO (la fuente de verdad)
│   └─ <COMMODITY>/<ZONA>/<Futures|Spot|Indices|Auctions>/<producto>/[<vencimiento>/]<contrato>.csv
│       POWER/DE/Futures/Base/Year/DEBY_202801.csv
│       POWER/DE/Futures/Peak/Month/DEPM_202611.csv
│       NATGAS/TTF/Futures/Physical/Season/G3BS_202704.csv
│       AGRICULTURALS/DE/Indices/Butter/EEX_Weekly_German_Butter_Index_INDEX.csv
├─ curves/                             CURVAS POR PRODUCTO (se regeneran cada vez)
│   └─ POWER/DE/Base.csv  ·  POWER/DE/Base_wide.csv
├─ inventory.csv                       un fichero de datos por fila
├─ catalog/contracts.csv               catálogo completo del hub
├─ _state/scrape_state.json            qué se descargó, cuándo y con qué resultado
├─ _state/run.lock                     bloqueo entre ejecuciones
└─ _logs/eex-scraper_AAAA-MM-DD.log    log del día (si [logging].directory no se cambia)
```

**CSV por contrato** — una fila por día de negociación:

```csv
tradeDate,shortCode,maturityDate,maturity,maturityType,deliveryStart,tenor,commodity,pricing,area,product,productSpecific,settlPx,currency,totVolTrdd,uOM,grossOpenInt,grossOpenIntSz,netOpenInt,netOpenIntSz,deliveryDay,scrapedAt
2026-09-21,DEBY,202801,202801,Year,2028-01-01,Cal-2028,POWER,F,DE,Base,,97.47,EUR,1967616,MWh,29546,259532064,,,,2026-09-22T13:40:11+00:00
```

**`inventory.csv`** — para ver de un vistazo qué hay: ruta, commodity, zona,
producto, tenor, nº de filas, primera y última fecha y último precio de cada
fichero.

**Contratos vencidos**: desaparecen del catálogo y dejan de pedirse, pero su
CSV se conserva con todo lo acumulado.

## 1.7 Curvas y tenors

Para cada **producto de futuros** (p. ej. POWER DE Base, que junta diarios,
fines de semana, semanas, meses, trimestres, temporadas y años) se genera la
cotización de todos sus tenors en cada **fecha de referencia** (día de
negociación). Hay dos ficheros por producto en `curves/<COMMODITY>/<ZONA>/`:

**`<producto>_wide.csv`** — una fila por fecha de referencia, una columna por
**tenor relativo**, valor = precio de liquidación. Se abre tal cual en Excel.
Cada columna es una serie continua aunque los contratos vayan venciendo: el
`Y+1` de hoy es Cal-2027; en enero pasará a ser Cal-2028.

```csv
tradeDate,...,M+1,M+2,...,Q+1,Q+2,...,Y+1,Y+2,Y+3,...
2026-09-28,...,165.23,170.69,...,167.25,163.25,...,129.28,99.35,87.77,...
```

**`<producto>.csv`** — formato largo, una fila por (fecha de referencia,
contrato), con tenor absoluto y relativo, precio, volumen y open interest.
Es el cómodo para filtrar en Excel o pandas:

```csv
tradeDate,relativeTenor,tenor,maturityType,deliveryStart,shortCode,maturity,settlPx,totVolTrdd,grossOpenInt,netOpenInt,currency,uOM
2026-09-28,M+1,2026-10,Month,2026-10-01,DEBM,202610,165.23,3409865,339784,,EUR,MWh
2026-09-28,Q+1,2026-Q4,Quarter,2026-10-01,DEBQ,202610,167.25,994050,250418,,EUR,MWh
2026-09-28,Y+1,Cal-2027,Year,2027-01-01,DEBY,202701,129.28,5072040,109145,,EUR,MWh
2026-09-28,Y+2,Cal-2028,Year,2028-01-01,DEBY,202801,99.35,1510848,30111,,EUR,MWh
```

**Etiquetas de tenor:**

| Tipo | Tenor absoluto (`tenor`) | Tenor relativo (`relativeTenor`) |
|---|---|---|
| Day | `2026-10-05` | `D+n` — días naturales |
| Weekend | `WE 2026-W41` | `WE+n` — semanas |
| Week | `2026-W41` (semana ISO) | `W+n` |
| Month | `2026-10` | `M+n` — `M+0` es el mes en curso |
| Quarter | `2026-Q4` | `Q+n` |
| Season | `Sum-27` (abr–sep), `Win-26/27` (oct–mar) | `S+n` |
| Year | `Cal-2027` | `Y+n` |

- Un tenor negativo (`D-2`, `W-1`) es un contrato cuyo periodo de entrega ya
  empezó pero que sigue publicando liquidación.
- Si un producto tiene varios contratos con el mismo tenor el mismo día (las
  rutas de freight: `C5TM`, `C7EM`... son todas Capesize mensual), la columna
  lleva delante el código: `C5TM M+1`, `C7EM M+1`.
- Spot e índices no tienen tenors: su serie está directamente en su CSV de
  `table_data/`.

El periodo de entrega de cada contrato sale del catálogo (año y mes del
vencimiento, más el día o la semana ISO). Para diarios ya vencidos que no están
en el catálogo, el día se toma de los dos últimos dígitos del código (`AB14` →
día 14; comprobado en los 1.798 diarios del catálogo).

## 1.8 Estado, reanudación y bloqueo

- **`_state/scrape_state.json`** guarda, por contrato, cuándo se descargó, con
  qué resultado (`ok`, `empty`, `error`), cuántas filas tiene y su última fecha.
  Se vuelca cada 100 contratos y al terminar.
- **Reanudación:** `run` se salta los contratos descargados con éxito hace
  menos de `min_refresh_hours` (4 h). Si una pasada se corta, relanzarla
  continúa donde se quedó. `full` no se salta ninguno.
- Borrar el fichero de estado **no pierde datos** (la fuente de verdad son los
  CSV): solo hace que la siguiente ejecución vuelva a pedirlo todo.
- **Bloqueo:** dos ejecuciones sobre la misma ruta base no pueden solaparse
  (p. ej. la tarea programada y una manual). La segunda avisa, lo anota en el
  log y sale con código 3. El bloqueo es del sistema operativo: si el proceso
  muere, se libera solo.

## 1.9 El log

Cada comando que hace algo (`run`, `full`, `catalog`, `curves`, `migrate`)
escribe en el **log del día**. `info` no escribe log.

- **Dónde:** `[logging].directory` + `[logging].filename` del config. Por
  defecto `<ruta base>/_logs/eex-scraper_AAAA-MM-DD.log`.
- **Qué contiene:** una línea de inicio (comando, config y ruta de salida),
  los mensajes del proceso (catálogo, contratos seleccionados, avisos), el
  progreso cada 250 contratos, el resumen final y una línea de fin con el
  código de salida y la duración. Si algo falla, **el error completo con su
  traceback** — en una tarea programada no hay consola y el log es lo único
  que queda.
- **Nivel:** `[logging].level`. `INFO` por defecto; `DEBUG` registra además
  cada petición HTTP; `WARNING` solo avisos y errores.
- **Varias ejecuciones el mismo día** se añaden al mismo fichero, cada una
  entre sus líneas `=== inicio` y `=== fin`.
- **Rotación:** los logs con más de `keep_days` días (60) se borran solos al
  arrancar. Solo se tocan ficheros con el patrón del log; nada más de esa
  carpeta.
- **Robusto:** si otro programa tiene el log del día bloqueado, se escribe en
  uno propio con el número de proceso (`eex-scraper_AAAA-MM-DD_1234.log`); si
  no se puede abrir ninguno, el comando sigue sin log. Quedarse sin log nunca
  impide la descarga.

Log real de `python run.py -o <carpeta> run -s DEBY -n 2` (rutas acortadas):

```
2026-09-29 14:34:13,129 INFO    === inicio: run | config: C:\...\eex_scraper\config.toml | salida: C:\...\o3
2026-09-29 14:34:13,141 INFO    Modo: run   salida: C:\...\o3
2026-09-29 14:34:13,609 INFO    Catalogo: 7919 contratos en bruto
2026-09-29 14:34:13,695 INFO    Catalogo guardado en C:\...\o3\catalog\contracts.csv (7919 contratos)
2026-09-29 14:34:13,699 INFO    Seleccionados 2 contratos de 7919 en el catalogo
2026-09-29 14:34:13,855 INFO    2/2 (100.0%)  escritos=2 sin-cambios=0 vacios=0 saltados=0 errores=0
2026-09-29 14:34:13,857 INFO    Regenerando inventario y curvas...
2026-09-29 14:34:13,871 INFO    Resumen
2026-09-29 14:34:13,871 INFO      contratos seleccionados : 2
2026-09-29 14:34:13,871 INFO      CSV escritos            : 2
2026-09-29 14:34:13,871 INFO      sin cambios             : 0
2026-09-29 14:34:13,871 INFO      vacios (sin datos)      : 0
2026-09-29 14:34:13,871 INFO      saltados (ya al dia)    : 0
2026-09-29 14:34:13,871 INFO      errores                 : 0
2026-09-29 14:34:13,871 INFO      filas nuevas            : 62
2026-09-29 14:34:13,871 INFO      filas actualizadas      : 0
2026-09-29 14:34:13,871 INFO      tiempo                  : 0.7 s
2026-09-29 14:34:13,871 INFO      curvas                  : 1 productos -> C:\...\o3\curves
2026-09-29 14:34:13,871 INFO    === fin: run | codigo 0 | 0.8 s
```

En una pasada completa, entre la selección y el resumen aparece una línea de
progreso cada 250 contratos.

## 1.10 Mapa del código

| Fichero | Qué hace |
|---|---|
| `run.py` | **El punto de entrada**: `python run.py`. Añade `src/` al path, usa el `config.toml` de su carpeta y, si el Python no tiene las dependencias, se relanza con `uv run`. |
| `pyproject.toml` / `uv.lock` | Versión de Python y dependencias para `uv sync`, y ajustes de pytest y ruff. No hay build. |
| `src/eex_scraper/cli.py` | Comandos (`run`, `full`, `catalog`, `curves`, `migrate`, `info`), opciones, log y resumen. |
| `src/eex_scraper/config.py` | Lee y valida `config.toml`; resuelve la ruta base y la del log. |
| `src/eex_scraper/scraper.py` | Orquesta una ejecución: catálogo, selección, paralelismo, estado, curvas. |
| `src/eex_scraper/api.py` | Traduce contratos a peticiones de la API y respuestas a filas. |
| `src/eex_scraper/fetchers.py` | HTTP con reintentos, limitador de ritmo y respaldo con navegador. |
| `src/eex_scraper/storage.py` | CSV: rutas, fusión sin duplicados, escritura atómica, inventario, migración. |
| `src/eex_scraper/tenors.py` | Periodo de entrega y tenors (absoluto y relativo) de cada futuro. |
| `src/eex_scraper/curves.py` | Construye `curves/` a partir de `table_data/`. |
| `src/eex_scraper/models.py` | Modelo de contrato, columnas del CSV y estructura de carpetas. |
| `src/eex_scraper/state.py` | Fichero de estado y bloqueo entre ejecuciones. |
| `scripts/run_daily.ps1` | Lo que lanza la tarea programada (= `run.py`). |
| `scripts/install_task.ps1` | Registra o quita la tarea diaria en Windows. |

---

# Parte 2 — Cómo se ejecuta

## 2.1 Instalación

Requisitos: Windows, Linux o macOS con [uv](https://docs.astral.sh/uv/)
instalado. uv se encarga de Python (≥ 3.11) y de las dependencias.

Es un proyecto **para ejecutar, no para instalar**: no se construye ningún
paquete. `pyproject.toml` y `uv.lock` solo fijan la versión de Python y las
dependencias; `uv sync` monta con ellas el entorno `.venv`, y el código se usa
tal cual desde `src/` a través de `run.py`.

```bash
cd eex_scraper
uv sync                               # crea .venv con httpx y typer (una vez)
python run.py                         # y a ejecutar
```

Opcional, solo si algún día EEX bloquea la API y hace falta el navegador:

```bash
uv sync --extra browser
uv run playwright install chromium
```

## 2.2 Configuración

Todo se ajusta en **`config.toml`**, en la raíz del proyecto. Cada opción está
comentada en el propio fichero. Lo que no se declare toma el valor por
defecto; una clave mal escrita da un error claro (con sugerencia) en vez de
ignorarse.

### Ruta base de salida — `[output]`

```toml
[output]
directory = "output"              # relativa a la carpeta del config.toml
# directory = "D:/datos/eex"      # absoluta
# directory = 'D:\datos\eex'      # con barras de Windows: comillas simples
# directory = "%USERPROFILE%/eex" # admite ~ y variables de entorno
```

| Clave | Por defecto | Qué es |
|---|---|---|
| `directory` | `"output"` | **Ruta base**: aquí se escribe todo |
| `table_dirname` | `"table_data"` | Subcarpeta de los CSV por contrato |
| `catalog_dirname` | `"catalog"` | Subcarpeta del catálogo |
| `state_dirname` | `"_state"` | Subcarpeta del estado y el bloqueo |
| `delimiter` | `","` | Separador de los CSV |
| `encoding` | `"utf-8-sig"` | Codificación (`utf-8-sig` abre bien en Excel) |

> Si cambias `directory`, **mueve antes** el contenido de la ruta anterior: la
> siguiente ejecución empezaría con la ruta nueva vacía y lo guardado no
> aparecería allí.

### Ruta del log — `[logging]`

```toml
[logging]
directory = "{output}/_logs"          # {output} = la ruta base de [output]
filename  = "eex-scraper_{date}.log"  # {date} = AAAA-MM-DD
level     = "INFO"                    # DEBUG, INFO, WARNING, ERROR
keep_days = 60                        # 0 = no borrar nunca
```

`directory` admite lo mismo que la ruta base (absoluta, relativa al config,
`~`, variables). Con `{output}` los logs siguen a los datos, también con `-o`.

### Descarga — `[scrape]`

| Clave | Por defecto | Qué es |
|---|---|---|
| `requests_per_second` | `0.9` | Ritmo máximo. **El freno real**: subirlo solo trae más 429 |
| `burst` | `20` | Peticiones que salen de golpe al arrancar |
| `concurrency` | `4` | Peticiones simultáneas |
| `request_delay_seconds` | `0.0` | Pausa extra tras cada petición |
| `max_lookback_days` | `420` | Ventana pedida (la API recorta sola; no hace falta tocarlo) |
| `min_refresh_hours` | `4.0` | `run` se salta lo descargado hace menos de esto; `0` = nunca saltar |
| `skip_empty` | `true` | No crear CSV para contratos sin ningún dato |

### Qué se descarga — `[filters]`

Listas vacías = todo. Los valores posibles están en `catalog/contracts.csv`.

| Clave | Ejemplo |
|---|---|
| `commodities` | `["POWER", "NATGAS"]` — POWER, NATGAS, ENVIRONMENTALS, FREIGHT, AGRICULTURALS, GO, HYDROGEN |
| `areas` | `["DE", "ES", "TTF"]` |
| `products` | `["Base", "Peak"]` |
| `maturity_types` | `["Month", "Quarter", "Year"]` |
| `short_codes` | `["DEBY", "FEU2"]` |
| `pricings` | `["F", "S", "I", "A"]` — Futuros, Spot, Índices, Subastas (las opciones `O` no se soportan) |
| `exclude_commodities`, `exclude_areas` | Nunca se descargan, aunque estén arriba |

### Otras secciones

- `[api]`: direcciones de la API, timeout (45 s) y reintentos (4, con espera
  creciente desde 1,5 s). No hace falta tocarlas.
- `[browser]`: respaldo con Playwright (activado; solo actúa si la API
  devuelve 403/Cloudflare).

### Qué config se usa

1. El de `--config ruta/config.toml`, si se pasa.
2. Si no, el de la variable de entorno `EEX_SCRAPER_CONFIG`.
3. Si no, un `config.toml` en la carpeta actual o en una superior.

`python run.py` usa siempre el `config.toml` que tiene al lado, se lance desde
donde se lance.

## 2.3 Ejecutar

### Lo de cada día

```bash
python run.py
```

Descarga todo, lo contrasta con lo guardado, regenera curvas e inventario y
deja el log. Funciona con cualquier Python: si el que lo lanza no tiene las
dependencias, se relanza solo con `uv run` usando el `.venv` del proyecto.
Equivalente: `uv run python run.py`.

### Comandos

| Comando | Qué hace | Red | Log |
|---|---|:-:|:-:|
| `python run.py` | = `run` | sí | sí |
| `python run.py run [filtros]` | Descarga, fusiona y regenera curvas; se salta lo reciente | sí | sí |
| `python run.py full [filtros]` | Igual, sin saltarse nada | sí | sí |
| `python run.py catalog` | Solo el catálogo | sí | sí |
| `python run.py curves` | Regenera `curves/` e `inventory.csv` desde lo guardado | no | sí |
| `python run.py migrate` | Reorganiza CSV del formato antiguo (`run` ya lo hace solo) | no | sí |
| `python run.py info` | Qué hay descargado, dónde están los datos y los logs | no | no |

### Opciones generales (van antes del comando)

| Opción | Qué hace |
|---|---|
| `-c, --config RUTA` | Usa otro config |
| `-o, --output RUTA` | Otra ruta base solo para esta ejecución |
| `-v, --verbose` | Más detalle por pantalla |
| `--help` | Ayuda (también `python run.py run --help`) |

### Filtros de `run` y `full` (pisan a los del config)

| Opción | Ejemplo |
|---|---|
| `-C, --commodity` | `-C POWER -C NATGAS` |
| `-A, --area` | `-A DE -A ES` |
| `-P, --product` | `-P Base` |
| `--pricing` | `--pricing I` (solo índices) |
| `--maturity-type` | `--maturity-type Month --maturity-type Year` |
| `-s, --short-code` | `-s DEBY -s FEU2` |
| `-n, --limit` | `-n 50` (como mucho 50 contratos) |
| `--dry-run` | Lista lo que haría, sin descargar |
| `--no-catalog-refresh` | Reutiliza el catálogo guardado |
| `--browser` | Fuerza el respaldo con navegador |

### Ejemplos

```bash
python run.py                                   # todo
python run.py run -C POWER -C NATGAS            # solo electricidad y gas (~1 h 50 min)
python run.py run -A DE -P Base                 # alemán base: todos sus tenors
python run.py run -s DEBY -s FEU2               # contratos concretos
python run.py run -C POWER -n 20 --dry-run      # ver qué haría, sin red
python run.py -o D:/pruebas/eex run -A ES       # a otra carpeta, solo esta vez
python run.py curves                            # rehacer curvas sin descargar
python run.py info                              # qué hay
```

### Códigos de salida

| Código | Significado |
|---:|---|
| 0 | Bien |
| 1 | Hubo errores y no se escribió nada, o el comando falló (detalle en el log) |
| 2 | Error en el config (el mensaje dice qué clave) |
| 3 | Ya había otra ejecución en marcha sobre la misma ruta base |
| 130 | Interrumpido con Ctrl+C (lo ya escrito se conserva) |

## 2.4 Ejecución diaria automática

En Windows, con el Programador de tareas:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1            # cada día a las 20:00
powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1 -At 21:30  # a otra hora
powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1 -Remove    # quitarla
```

- Registra la tarea **«EEX Scraper»**, que lanza `scripts\run_daily.ps1` →
  `run.py`.
- 20:00 porque EEX publica las liquidaciones por la tarde.
- Si el equipo está apagado a esa hora, corre en cuanto se enciende.
- Necesita la sesión de usuario iniciada (no guarda contraseña).
- Si la anterior aún no ha terminado, no arranca otra.
- Resultado: en el log del día (`[logging].directory`). Si fallase antes de
  arrancar Python (uv no encontrado...), la salida cruda queda en
  `%TEMP%\eex_scraper_task.log`.

Comprobar que está registrada y cuándo corre:

```powershell
Get-ScheduledTaskInfo -TaskName "EEX Scraper" | Format-List LastRunTime,LastTaskResult,NextRunTime
```

En Linux/macOS, una línea de cron equivalente:

```
0 20 * * *  cd /ruta/eex_scraper && uv run python run.py
```

## 2.5 Tests

```bash
uv run pytest                         # 104 tests, sin red, ~4 s
uv run ruff check src tests run.py    # estilo y errores
uv run ruff format --check src tests run.py
```

Ningún test toca la red ni tus datos: trabajan en carpetas temporales y
sustituyen la descarga por versiones falsas.

| Fichero | Tests | Qué cubre |
|---|---:|---|
| `tests/test_scraper.py` | 30 | Rutas de fichero, fusión sin duplicados, filas vacías, filtros, ventana de fechas, parámetros enviados a la API, flujo de un contrato (escribir, no reescribir, saltar lo reciente, contrato sin datos), migración del formato antiguo, bloqueo, aislamiento de fallos |
| `tests/test_curves.py` | 23 | Periodo de entrega y tenor de cada tipo (día, semana ISO, mes, trimestre, temporada, año), tenors relativos, curvas ancha y larga, rutas de freight, spot/índices sin curva |
| `tests/test_config_state.py` | 28 | Validación del config, ruta base (relativa, absoluta, variables, `EEX_SCRAPER_CONFIG`), ruta y nombre del log, niveles, el `config.toml` del proyecto carga bien, fichero de estado, limitador de ritmo |
| `tests/test_cli.py` | 19 | Cada comando, `-o`, log (inicio/resumen/fin, carpeta del config, nivel, traceback si falla, bloqueo, rotación, log ocupado, sin permiso para escribir log) |
| `tests/test_run_py.py` | 4 | `run.py` en un proceso aparte: ayuda, usa su config desde cualquier carpeta, escribe datos y log donde dice el config, códigos de salida |

## 2.6 Problemas frecuentes

**«Ya hay otra ejecución en marcha»** — otra ejecución (quizá la tarea
programada) está trabajando sobre la misma ruta base. Espera a que termine.
Si el proceso murió, el bloqueo ya está libre y basta con relanzar.

**Muchos «saltados»** — `run` se salta lo descargado hace menos de
`min_refresh_hours`. Para forzar: `python run.py full`.

**Muchos «vacíos»** — normal: vencimientos lejanos que aún no cotizan e índices
descatalogados. No generan CSV.

**Va lento** — es el límite de EEX (~1 petición/s); una pasada completa son
~2 h 15 min. Subir `requests_per_second` solo trae rechazos. Para ir más rápido,
limita con `[filters]` a lo que uses.

**Quiero cambiar la ruta de salida** — mueve la carpeta actual a la nueva
ubicación y después cambia `[output].directory`.

**Quiero empezar de cero sin perder nada** — borra solo
`<ruta base>/_state/scrape_state.json`: la siguiente ejecución volverá a pedir
todos los contratos y los fusionará con lo guardado.

**¿Qué pasó anoche?** — abre el log del día en `[logging].directory` y busca
la línea `=== fin`: el código 0 es que fue bien. Si no es 0, justo encima está
el motivo.

**Los datos son de EEX** y están sujetos a sus condiciones de licencia: uso
propio, no redistribución.
