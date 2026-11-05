# Radar de Passages

Monitor de precios con historial verificable y alertas, pensado para correr **24/7 en un servidor independiente** (no en tu PC). Vigila:

- **Productos de tienda**, por **tienda o categoría completa** (sitemap o listado), no un enlace por producto: alimentos, ropa, juguetes, electrodomésticos y tecnología (laptops, TV, consolas, componentes de PC, celulares, monitores…).
- **Pasajes aéreos** con una API de datos autorizada (tarifas en caché, no en vivo) y **buses** con los conectores que añadas.

Cada seguimiento se revisa solo, en un momento **aleatorio entre 5 y 15 minutos**, con espaciado por sitio, y el sistema **se detiene** si un sitio rechaza la automatización.

Caso demostrativo propio de **Yanz Studio Systèmes / El Código de Yanz**. Sin reservas ni pagos.

## Qué incluye

- **Escaneo por enlace de tienda o categoría** (modo *sitemap* y modo *listado*) con tope de productos por catálogo y por tienda.
- **Alertas inteligentes por prioridad** (baja, media, alta y **máxima** para lo extraordinario), calculadas contra el historial propio y **contra otras tiendas** (mismo producto por GTIN o marca + código de fabricante). El precio objetivo sigue disponible.
- **Detector de restricciones**: clasifica 403/429/CAPTCHA/«acceso denegado»/inicio de sesión/redirecciones sospechosas/estructura cambiada/vacío/timeouts, pausa el sitio, registra el incidente y avisa por Telegram. **Sin rotación de IP ni evasión.**
- **Gestor de salida de red** con trazabilidad por dominio; proxies **autorizados** opcionales, sin conmutación automática.
- **158 fuentes catalogadas** (91 tiendas en 8 países, 40 aerolíneas, 17 empresas de bus, 10 agencias) y un **verificador** que las revisa con cortesía desde tu servidor. Todas están **sin verificar**.
- **Medición de recursos** (CPU, memoria, disco, ancho de banda) en la interfaz y por línea de comandos; renderizado opcional con navegador solo para los dominios que lo necesiten.
- Interfaz web en español (modo inteligente, prioridades, catálogos, comparación entre tiendas, incidentes, recursos y salidas de red).

## Estado real: qué está comprobado y qué no

| Pieza | Estado |
|---|---|
| Núcleo (historial inmutable, comparación solo con condiciones iguales, alertas únicas, cooldown), API, interfaz | Probado con la suite y en un Chromium real (sin errores de consola) |
| Scheduler 5–15 min, espaciado por sitio, AutoThrottle, backoff, circuito abierto, lease de instancia única | Probado con reloj simulado. **19 salvaguardas se rompieron a propósito y las pruebas las detectaron** ([`evidence/mutations.md`](evidence/mutations.md)); una sobrevivió a la primera pasada, era un hueco real y se cerró |
| Alertas por prioridad y comparación entre tiendas | Probado con historiales y tiendas simuladas |
| Detector de restricciones, incidentes, salida de red | Probado con respuestas simuladas, incluido que **un sitio pausado no recibe más peticiones** y que una salida apagada nunca cae a otra |
| `robots.txt` (RFC 9309, analizador propio), anti-SSRF, topes y tipos de contenido | Probado con respuestas simuladas |
| Descubrimiento por sitemap y por listado | Probado con sitios simulados (incluidos gzip, índices y bombas) |
| Renderizado con navegador (opcional) | Probado contra un Chromium real con rutas simuladas; la imagen Docker con navegador **no se construyó** |
| Migración v1→v2→v3 | Probada desde bases creadas con cada esquema anterior |
| Medición de recursos | Medido con red simulada ([`evidence/benchmark.md`](evidence/benchmark.md)); **no** con páginas reales |
| **Ninguna tienda, aerolínea, empresa de bus ni la API de Travelpayouts fue contactada** | Todo se probó con respuestas simuladas. Se ignora qué tiendas publican precio exacto en JSON-LD, qué token de vuelos funciona y qué cobertura tiene |
| Buses, aerolíneas y agencias | Catalogadas, **sin conector**: no se rasca ninguna |
| Docker, Fly.io, Caddy, workflows de GitHub, Telegram real | Escritos y revisados, **no ejecutados** desde el entorno de desarrollo |
| Revisión independiente por un segundo agente | **No realizada**: se intentó y la cuenta alcanzó su límite de uso antes de producir hallazgos. Se hizo revisión propia con pruebas y mutaciones; una revisión externa sigue siendo recomendable antes de exponerlo a Internet |

Los datos de la fuente **sintética** (`demo`) son valores de prueba, no precios de mercado, y la interfaz y las alertas los rotulan así.

## Ejecutar en local

Python 3.12+:

```bash
python3 -m venv .venv && source .venv/bin/activate
python -m pip install -r requirements-lock.txt
RADAR_SCHEDULER=1 python run.py        # http://127.0.0.1:8003
```

Sin `RADAR_SCHEDULER=1` las consultas son manuales (botón «Consultar»). El JavaScript compilado ya está incluido; si cambias `frontend/app.ts`: `npm ci && npm run build`. En local, sin `RADAR_ADMIN_PASSWORD`, la app queda abierta: úsala solo en `127.0.0.1`.

## Llevarlo a un servidor

Guía completa, con costos verificados y qué **no** hacer: [`docs/despliegue.md`](docs/despliegue.md). Resumen:

```bash
cp .env.example .env          # pon RADAR_ADMIN_PASSWORD (12+ caracteres) y RADAR_CONTACT
docker compose up -d --build  # http://SERVIDOR:8003 (HTTPS: docker compose --profile tls)
```

- **GitHub es para el código, el CI y la imagen, no para ejecutar el monitor**: los términos de GitHub limitan Actions a actividad del propio proyecto. El monitor corre en un VPS o en Fly.io (≈ US$ 3–4 al mes con los precios verificados; ver la guía).
- Con `RADAR_ENV=production` (valor por defecto de la imagen) **la app no arranca sin contraseña**. Usa siempre HTTPS.
- **Una sola réplica** (SQLite + lease). Copia de seguridad en caliente: `python -m radar.main --backup /data/backups/radar-AAAAMMDD.sqlite3`.
- Los comandos de mantenimiento se ejecutan como `radar`: `docker compose exec -u radar radar python -m radar.main --status`.

## Cómo se programan las revisiones (y qué NO hace)

1. **Intervalo aleatorio por seguimiento.** Tras cada revisión, el siguiente momento se sortea en `[RADAR_MIN_INTERVAL_S, RADAR_MAX_INTERVAL_S]` (300–900 s). Sin ritmo fijo ni seguimientos sincronizados; los nuevos se revisan a los 5–60 s.
2. **Espaciado por sitio** (lo que más reduce bloqueos): entre dos peticiones al mismo dominio pasan 15–45 s aleatorios, nunca hay dos simultáneas, y **AutoThrottle** adapta el retardo a la latencia del sitio (solo puede frenar). Un sitio recibe como máximo ≈ 2 800 páginas al día; con más seguimientos de los que eso permite, **cada uno se revisa más despacio** en vez de lanzar ráfagas, y el panel lo avisa.
3. **Buenos modales:** `robots.txt` según el RFC 9309 (incluido `Crawl-delay`), User-Agent que dice quién eres (`RADAR_CONTACT`), solo dominios de tu lista permitida, solo HTTPS.
4. **Tope y reparto del gasto** para las fuentes que cobran por consulta.

## Alertas por prioridad

| Nivel | Condición (precio por debajo de la referencia) |
|---|---|
| **baja** | ≥ 8 % |
| **media** | ≥ 15 % |
| **alta** | ≥ 25 % |
| **máxima** | ≥ 40 %, **y** el precio es el mínimo histórico, **y** hay ≥ 6 observaciones en ≥ 1 h |

La **referencia** es la mediana del historial del propio producto, en la misma tienda y con condiciones equivalentes, donde cada precio pesa por las veces que se vio. Además:

- Una caída de **70 % o más** se marca como **probable error de precio**: puede desaparecer en minutos o no honrarse.
- Con **otras tiendas** vendiendo el mismo producto (GTIN o marca + código de fabricante, misma moneda, con stock y precio vigente), una tienda ≥ 5 % más barata **baja un nivel** la alerta, y con ≥ 2 tiendas pares la alerta no pasa de «alta»: la máxima es para lo que no tiene explicación en el mercado.
- Enfriamiento de 30 min por seguimiento; **una prioridad mayor lo rompe**. Telegram entrega primero la más alta.
- Un precio agotado o con stock desconocido **nunca** alerta. No hay predicción ni IA.

Al crear un seguimiento a mano eliges «Precio objetivo» (tu precio), «Inteligente (por prioridad)» o «Ambos»; los descubiertos por catálogo son siempre inteligentes. Los umbrales se ajustan con `RADAR_TIER_*`.

## Vigilar tiendas por catálogo

```bash
# 1) ver el catálogo y verificarlo DESDE TU SERVIDOR (pausas largas, sin ráfagas)
python -m radar.main --list-sources
python -m radar.main --verify-sources --only country=PE --out verify-pe.json
# 2) leer los términos de las tiendas «usable» y activarlas en .env
RADAR_ENABLED_SOURCES=falabella-pe,wong-pe          # o country=PE | cat=tecnologia | all
```

Al arrancar, el monitor crea el catálogo de cada tienda listada (idempotente); también puedes pegar el enlace de una categoría en la interfaz. Los veredictos del verificador son: `usable`, `moneda_distinta`, `precio_no_exacto`, `sin_json_ld`, `sin_productos_reconocibles`, `bloqueado`, `no_verificable`. Las tiendas que pintan el precio con JavaScript necesitan el renderizado opcional (`RADAR_RENDER_DOMAINS`) o su API oficial.

## Cuando un sitio dice «no»

Un 403, 429, CAPTCHA, «acceso denegado», pedido de inicio de sesión o redirección a un desafío **pausa todo el sitio** (15 min, 30, 60… hasta 6 h, respetando `Retry-After`); tras 5 seguidos el sitio queda apagado hasta que lo reanudes (`--resume <dominio>` o el botón). Cada incidente queda en un diario de solo-añadir y los graves llegan a Telegram. **No se cambia de IP, de salida de red ni de identidad para continuar**: se detiene y se busca una vía autorizada (API, feed, proveedor de datos). Detalles y alternativas evaluadas (rotación, Tor, proxies residenciales) en [`docs/arquitectura-red.md`](docs/arquitectura-red.md).

## Fuentes (proveedores)

| Proveedor | Tipo | Se activa con |
|---|---|---|
| `demo` | vuelo, bus, producto (**sintético**) | activo por defecto; `RADAR_ENABLE_DEMO=0` lo oculta |
| `store` | producto | `RADAR_ENABLED_SOURCES` y/o `RADAR_ALLOWED_STORE_DOMAINS` |
| `travelpayouts` | vuelo (**tarifas en caché**, no en vivo) | `RADAR_TRAVELPAYOUTS_TOKEN` (+ tope mensual recomendado) |

Detalles y límites de la fuente de vuelos, y lo que está pendiente (fechas flexibles, aeropuertos cercanos): [`docs/vuelos.md`](docs/vuelos.md). Cómo añadir una fuente: [`docs/conectores.md`](docs/conectores.md).

## Configuración

Todo por variables de entorno; la plantilla comentada con todas ellas y sus valores reales es [`.env.example`](.env.example): acceso, ventana de intervalos, espaciado, AutoThrottle y *backoff*, fuentes, renderizado, salidas de red, umbrales de prioridad, límite de seguimientos (`RADAR_MAX_WATCHES`, 1 000 por defecto) y Telegram.

## Modelo y decisiones

- **Condiciones inmutables** del seguimiento: vuelo (origen/destino IATA, fechas, viajeros, cabina, moneda, escalas, aerolíneas), bus (origen/destino, fechas, pasajeros, empresas, moneda) o producto (URL https, SKU opcional, moneda). La misma condición con distinta fuente son seguimientos distintos.
- **Comparación histórica solo entre observaciones equivalentes**: una condición desconocida se conserva como desconocida y desactiva la comparación; «unspecified» significa «la página no lo dice» y nunca equivale a «nuevo». No se convierten monedas.
- **Un mismo precio es una sola observación.** Ver el mismo precio otra vez solo extiende su vigencia (20 min en tiendas); un precio distinto es otra observación. Nada se sobrescribe ni se borra.
- Los **fallos** tienen su propio registro y nunca reciben un precio. Una página con varias ofertas distintas o solo un «desde» se rechaza como ambigua.
- Base SQLite (WAL, claves foráneas), esquema v3, migración automática y transaccional desde la v1 y la v2 sin perder historial.

## Telegram

Opt-in, para tu propio chat: `RADAR_TELEGRAM_ENABLED=1`, `RADAR_TELEGRAM_TOKEN`, `RADAR_TELEGRAM_CHAT_ID`. Con el scheduler activo, las alertas se entregan solas tras cada ciclo (**las de mayor prioridad primero**) y los incidentes graves de acceso se avisan (como máximo uno por sitio y hora). Telegram no ofrece clave de idempotencia: si la entrega es ambigua (timeout, 5xx, proceso interrumpido) la alerta queda `uncertain` y **no se reintenta**. Ningún registro contiene el token. Nunca pongas secretos en Git ni en capturas.

## Pruebas y evidencia

```bash
python -m pytest -q                      # 466 pruebas (algunas simulan horas con reloj inyectado); las 7 de navegador se omiten sin Playwright
npm ci && npm run check                  # TypeScript estricto
python scripts/mutations.py              # rompe 19 salvaguardas a propósito y comprueba que las pruebas lo notan
python scripts/benchmark.py --watches 500 --domains 25 --hours 1   # CPU/memoria/ancho de banda con red simulada
python scripts/demo.py                   # recorrido sintético reproducible → evidence/demo-results.json
python -m radar.main --status            # estado del scheduler, dominios y consumo
```

Resultados y límites: [`evidence/verification.md`](evidence/verification.md), [`evidence/benchmark.md`](evidence/benchmark.md), [`evidence/mutations.md`](evidence/mutations.md).

## Límites conocidos

- Una instancia, un archivo SQLite: sin alta disponibilidad ni escalado horizontal. El historial **no se poda** (0,3–0,5 KB por consulta: ver el cálculo de disco en la guía de despliegue).
- Solo precios que la página publique como JSON-LD de schema.org (o que un navegador pinte, con el renderizado opcional).
- Los vuelos son tarifas en caché de una API de datos: pueden haber desaparecido. Sin fechas flexibles ni aeropuertos cercanos todavía.
- **Revisa los términos de uso de cada sitio antes de activarlo**: el monitor respeta `robots.txt`, pero no puede leer sus condiciones.
- Hosts resueltos por DNS: se rechazan IP no públicas, pero no hay fijación de IP entre la comprobación y la conexión (riesgo residual bajo: solo se contactan dominios de tu lista).
- Autenticación Basic de un solo usuario; sin roles ni auditoría de accesos.

## Documentos

| Documento | Contenido |
|---|---|
| [`docs/auditoria.md`](docs/auditoria.md) | Qué había, qué se conservó, ajustó, creó o descartó; comparación con `polite-web-crawler`; Scrapy, Tor, Playwright, PostgreSQL |
| [`docs/arquitectura-red.md`](docs/arquitectura-red.md) | Salida de red, qué pasa ante un bloqueo, IP de centro de datos, rotación/Tor/proxies, seguridad |
| [`docs/despliegue.md`](docs/despliegue.md) | Dónde correrlo, costos verificados, GitHub y CI, operación, riesgos |
| [`docs/vuelos.md`](docs/vuelos.md) | Fuente de vuelos, límites y pendientes |
| [`docs/conectores.md`](docs/conectores.md) | Contrato de un conector y cómo añadir fuentes |
| [`docs/plan-por-fases.md`](docs/plan-por-fases.md) | Plan por fases con criterios de salida |
| [`docs/caso-de-estudio.md`](docs/caso-de-estudio.md) | Resumen del caso |
