# Auditoría del proyecto

Qué se encontró al recibir el proyecto, qué se conservó, qué se ajustó, qué se implementó y qué se descartó, y por qué. Cada componente tiene una de estas etiquetas:

- **Correcto**: se conserva tal cual, con sus pruebas.
- **Ajustado**: funcionaba, pero necesitaba cambios (se indica cuáles).
- **Nuevo**: no existía y se implementó.
- **Descartado**: se retiró o se reemplazó (se indica el motivo).
- **Decisión tuya**: no se puede resolver desde el código; depende de un contrato, un término de uso o un gasto.

No se reescribió nada que funcionara sin justificarlo, y no se añadió ninguna dependencia nueva obligatoria: sigue siendo FastAPI + httpx + SQLite.

## Componentes

| Componente | Estado | Qué se hizo y por qué |
|---|---|---|
| Base SQLite, historial inmutable, observaciones y consultas | Correcto | Se mantuvo. Un mismo precio es una sola observación; nada se sobrescribe. La migración a la v3 es **aditiva y transaccional** (v1→v2→v3, con *rollback* si algo falla) y se probó partiendo de bases creadas con el esquema de cada versión anterior (`tests/test_migration_v3.py`). |
| Modelo de seguimientos (vuelo, bus, producto) | Ajustado | El precio objetivo pasó a ser **opcional**: un seguimiento descubierto por catálogo no tiene objetivo propio. Hay tres modos de alerta: `target`, `smart` y `both`. |
| Alerta por precio objetivo | Correcto | Intacta; es el modo por defecto de vuelos y buses. |
| Alertas inteligentes por prioridad | Nuevo | Cuatro niveles (baja ≥ 8 %, media ≥ 15 %, alta ≥ 25 %, máxima ≥ 40 % bajo la referencia) contra la **mediana ponderada** del historial propio y contra otras tiendas. La máxima exige precio ≤ mínimo histórico y ≥ 6 observaciones en ≥ 1 h; una caída ≥ 70 % se marca como **probable error de precio**. Ver README. |
| Comparación entre tiendas | Nuevo | Solo el mismo producto (GTIN válido, o marca + código de fabricante), misma moneda y condición, con stock y precio vigente. Una tienda ≥ 5 % más barata baja la alerta un nivel; con ≥ 2 pares, la alerta no pasa de «alta». |
| Scheduler (lease, ciclo aleatorio, backoff) | Ajustado | Ventana de **5–15 min** (antes 1–10), espaciado por sitio con **AutoThrottle propio** (retardo = (anterior + latencia/objetivo)/2, tope 60 s; una respuesta que no sea 200 nunca lo acelera) y reacción a las restricciones del detector. El lease de instancia única y el reparto del tope mensual se mantuvieron. |
| Control de ritmo por dominio (`DomainGate`) | Ajustado | Se le añadió el **circuito abierto** (5 rechazos seguidos = el sitio queda apagado hasta que lo reanudes), `Retry-After`, y el descanso por fallos transitorios (timeouts/5xx: tras 3 seguidos, 120 s duplicándose, tope 1 h). |
| Lector de tiendas (`store`, JSON-LD de schema.org) | Ajustado | Se conservó la idea (leer solo precios que la página publica de forma estructurada). Se añadió: lista de tipos de contenido permitidos, tope de 3 MB, **clasificación de páginas de desafío** aunque lleguen con un tipo de contenido inesperado, y renderizado opcional por dominio. |
| `robots.txt` con `urllib.robotparser` | **Descartado** | La biblioteca estándar **no implementa el RFC 9309** (precedencia de la regla más larga, comodines `*` y `$`, tratamiento de 4xx/5xx/401/403). Se reemplazó por un analizador propio (`radar/robots.py`, 21 pruebas). 401/403 sobre `robots.txt` o una página de desafío en su lugar se tratan como **rechazo**, no como «sin reglas». |
| Conector de vuelos Duffel | **Descartado** | Lo pediste (cobra por búsqueda y su cobertura de aerolíneas locales no estaba confirmada). Se eliminó del código, la configuración, la interfaz y los demás documentos —este registro de decisiones es el único lugar donde se nombra—; una prueba verifica que no queda rastro y que la variable antigua no hace nada. |
| Amadeus Self-Service | **Descartado** (ya no existe) | El servicio cerró el 17-07-2026. |
| Vuelos reales | Nuevo / **Decisión tuya** | Conector `travelpayouts` (API de datos de Aviasales, tarifas **en caché** de búsquedas de usuarios, hasta 7 días; no son en vivo ni verificadas). Necesita un token y que confirmes sus condiciones. Ver [`vuelos.md`](vuelos.md). |
| Descubrimiento por catálogo / sitemap | Nuevo | En vez de un enlace por producto: indicas la tienda (o su categoría) y el monitor crea los seguimientos con tope por catálogo y por tienda. Modo *sitemap* (lee los `Sitemap:` del `robots.txt`) y modo *listado* (precios del JSON-LD `ItemList`, una sola petición; las páginas de producto se reconfirman 1–2 h después). Siempre en modo `smart`. |
| Catálogo de fuentes (158) y verificador | Nuevo | `sources/stores.json` (91 tiendas en 8 países), `sources/travel.json` (67: 40 aerolíneas, 17 buses, 10 agencias) y `sources/categories.json` (35 plantillas). **Todas están «sin verificar»** y las de viajes están «sin conector»; el verificador (`--verify-sources`) revisa `robots.txt`, sitemap y JSON-LD con pausas largas desde tu servidor. |
| Detector de restricciones, incidentes y avisos | Nuevo | Un único punto clasifica 403/429/CAPTCHA/«acceso denegado»/inicio de sesión/redirecciones sospechosas/estructura cambiada/respuesta vacía/timeouts. Cada incidente queda en un registro de solo-añadir y los graves llegan a Telegram (1 por sitio y hora). Sin reintento a otra salida de red. |
| Gestor de salida de red (*egress*) | Nuevo | Perfil `direct` por defecto. Proxies **autorizados** solo si los configuras (`RADAR_EGRESS_*`), cada dominio con **una** salida, trazabilidad por petición, credenciales en `Secret`. Si una salida se apaga o recibe un bloqueo, sus dominios se detienen: **no hay conmutación automática**. Ver [`arquitectura-red.md`](arquitectura-red.md). |
| Renderizado con navegador (Playwright) | Nuevo, opcional | Solo para los dominios que listes en `RADAR_RENDER_DOMAINS`, con el mismo `robots.txt`, el mismo ritmo y un User-Agent honesto; sin «modo sigiloso». Probado contra un Chromium real con rutas simuladas. No viene instalado por defecto. |
| Métricas de recursos | Nuevo | CPU, memoria, tamaño de la base, disco libre, peticiones y bytes por dominio, en la UI, `/api/status` y `--metrics`. Mediciones de referencia en [`../evidence/benchmark.md`](../evidence/benchmark.md). |
| Telegram | Ajustado | Entrega por prioridad (la más alta primero), avisos de incidentes graves, sin reintento tras envíos ambiguos y sin el *token* en ningún registro (se silenció el registro HTTP, donde iba en la URL). La entrega real sigue sin probarse. |
| API y UI | Ajustado | Modo inteligente, etiquetas de prioridad, catálogos, comparación, incidentes, recursos y salidas de red. Verificado en Chromium real, sin errores de consola. |
| Autenticación | Correcto, con límite | Usuario único con Basic; en producción no arranca sin contraseña de 12+ caracteres. Sin roles ni auditoría de acceso. |
| Docker / Compose / Fly.io / CI | Ajustado | Se agregaron `sources/` a la imagen, el arranque automático de catálogos, el despliegue por imagen (GHCR) y un CI que construye, prueba y comprueba que el JavaScript compilado coincide con el TypeScript. La imagen **no se construyó aquí** (sin Docker). |
| Fuente `demo` | Correcto | Sintética y rotulada como tal en la UI y en las alertas. |
| Buses, aerolíneas y agencias directas | **Decisión tuya** | 67 entradas sin conector. Cada empresa necesita una API o un acuerdo; rascar sus sitios no es una opción de este proyecto. Ver [`conectores.md`](conectores.md). |
| Rotación de IP, Tor, proxies residenciales | **Descartado** | Diseñado para eludir límites, no para respetarlos; va contra lo que pediste. Ver [`arquitectura-red.md`](arquitectura-red.md). |

## Comparación con `polite-web-crawler`

Se analizó el repositorio de referencia (`mojtaba-py-code/polite-web-crawler`, paquete `politecrawl`, ~1 100 líneas de código). Se trató como **código no confiable**: se leyó en un directorio aparte, no se ejecutó y **no se añadió como dependencia**.

| Aspecto | `politecrawl` | Este proyecto |
|---|---|---|
| Objetivo | Rastrear un sitio y guardar sus páginas | Vigilar precios con historial y alertas, durante semanas |
| `robots.txt` | `urllib.robotparser` | Analizador propio conforme al RFC 9309 |
| Protección SSRF | Sí (resuelve todas las direcciones, rechaza no públicas) | Sí, equivalente, más lista blanca de dominios |
| Redirecciones | Manuales, cada salto revalidado | Igual, con tope de saltos y sin salir de la lista blanca |
| Tope de tamaño / tipos de contenido | Sí | Sí (3 MB, lista de tipos), más protección contra bombas de descompresión en sitemaps |
| Ritmo por sitio | Mínimo fijo (1 s) | Aleatorio 15–45 s + AutoThrottle + `Crawl-delay`, persistido en la base |
| Reacción a bloqueos | Reintentos con *backoff* ante 5xx | Detector central, pausa de todo el sitio, circuito abierto, incidentes, avisos |
| Estado | En memoria | SQLite (sobrevive a reinicios) |
| Credenciales / sesión | Admite *token* y *cookie* | **No** se admiten: si un sitio pide iniciar sesión, se detiene y se registra |

Qué se tomó prestado como **práctica** (no como código): el envoltorio `Secret`, el filtro que borra credenciales de los registros y la limpieza de texto no confiable antes de registrarlo (`radar/security.py`). Lo demás ya existía o era insuficiente para esta tarea.

## Tecnologías evaluadas

| Tecnología | Decisión | Motivo |
|---|---|---|
| **Scrapy** | No adoptada | Es un *framework* de rastreo masivo con su propio bucle (Twisted); duplicaría el scheduler, el estado por dominio y la base que ya existen y están probados, y añadiría una dependencia grande. De Scrapy se tomó la **idea** de AutoThrottle, implementada en unas 20 líneas dentro de `DomainGate`. |
| **AutoThrottle** | Implementado (propio) | Ajusta el retardo a la latencia real del sitio y solo puede frenar, nunca acelerar tras un error. |
| **Playwright** | Opcional, por dominio | Solo hace falta cuando el precio aparece tras ejecutar JavaScript. Pesa (cientos de MB de disco; planifica ≥ 1 GB de RAM, cifra orientativa que no se midió aquí), así que no es el camino por defecto. |
| **Tor** | No adoptado | Las salidas de Tor son públicas y conocidas, muchos sitios las bloquean de entrada, es lento y su uso para esquivar un bloqueo es justo lo que pediste evitar. |
| **Rotación de IP / proxies residenciales** | No adoptada | Ver [`arquitectura-red.md`](arquitectura-red.md). Solo existe la posibilidad de **asignar** un proxy autorizado a un dominio cuyos términos lo permitan. |
| **PostgreSQL** | Opcional a futuro | Con una sola instancia, SQLite en WAL alcanza para el volumen medido (ver [`../evidence/benchmark.md`](../evidence/benchmark.md)). Solo tendría sentido con varias réplicas o alta disponibilidad. |

## Qué sigue abierto

- Ninguna tienda, aerolínea, empresa de buses ni la API de Travelpayouts fue contactada realmente. Todo se probó con respuestas simuladas.
- Fechas flexibles y aeropuertos cercanos para vuelos **no están implementados** (ver [`vuelos.md`](vuelos.md)).
- Telegram real, la imagen de Docker, Fly.io, Caddy y el CI de GitHub **no se ejecutaron** desde este entorno.
- Los costos de proxies y de DigitalOcean **no se verificaron**; los de Hetzner, Fly.io y GitHub sí (ver [`despliegue.md`](despliegue.md)).
