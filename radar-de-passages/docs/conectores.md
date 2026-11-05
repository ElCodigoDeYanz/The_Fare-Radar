# Conectores (fuentes de precios)

Un conector es una clase con cuatro atributos y dos métodos. El resto (historial, comparación entre tiendas, alertas por objetivo y por prioridad, intervalos aleatorios de 5–15 min, espaciado por sitio con AutoThrottle, detector de restricciones, incidentes, tope mensual) lo aporta el sistema.

## Contrato

```text
name          nombre que se elige al crear el seguimiento (campo «Fuente»)
kinds         tipos que atiende: "flight", "bus", "product"
live          True = fuente real: se aplica espaciado, backoff y circuito por dominio.
              False = sintética: sin esas restricciones y rotulada como demo.
min_interval  segundos mínimos entre dos consultas del mismo seguimiento
monthly_cap   tope mensual de llamadas (lo fija el registro desde el entorno); None = sin tope
domain(criteria)                 -> str, el sitio al que se aplicará el espaciado (p. ej. el host)
fetch(criteria, scenario, now)   -> list[Offer]
```

`fetch` debe lanzar el error **tipado** que corresponda, porque de eso depende la reacción del scheduler:

| Error (`radar.providers.base`) | Cuándo | Efecto |
|---|---|---|
| `BlockedError(msg, retry_after=None)` y sus subtipos `RateLimitedError` (429), `CaptchaError`, `AccessDeniedError` (403, «acceso denegado»), `SessionError` (pide iniciar sesión), `UnexpectedRedirectError` (redirige a un desafío o a un login) | La fuente **rechaza explícitamente** | Pausa de **todo el sitio** (15 min, 30, 60… hasta 6 h; respeta `Retry-After`); 5 seguidos abren el circuito y el sitio queda apagado hasta «Reanudar». Incidente registrado y, si es grave, aviso por Telegram. **Sin reintento por otra salida de red.** |
| `StructureError`, `GoneError` (404/410), `EmptyResponseError` | La página cambió, ya no existe o vino vacía | Se pausa **ese seguimiento** (estructura tras 5, inexistente tras 3) |
| `TransientError` (`TimeoutFailure`, `NetworkFailure`, 5xx) | Fallo pasajero | Reintento más tarde con *backoff*; tras 3 seguidos en un sitio, el sitio descansa (120 s, duplicando, tope 1 h). No cuenta como rechazo. |
| `EgressError` | La salida de red asignada al dominio está apagada | El dominio no recibe peticiones; **no** se prueba con otra salida |
| `ProviderError` | La fuente respondió pero sin precio utilizable | Se registra el fallo; *backoff* del seguimiento |
| `BudgetExhausted` | Lo lanza el sistema al agotar el tope mensual | Se detiene hasta el mes siguiente |

**Nunca intentes sortear un bloqueo** dentro de un conector (reintentos inmediatos, otras cabeceras, otra IP): lanza `BlockedError` y deja que el sistema espere.

### Reglas de datos de `Offer`

- `total_cents`: entero positivo en la moneda del seguimiento (`criteria.currency`). Sin conversiones; una oferta en otra moneda hace fallar la consulta completa.
- `provider_observation_id`: **hash del contenido** (criterios + precio + condiciones), *sin* la hora. Así el mismo precio visto otra vez es la misma observación y solo extiende su vigencia. Reutilizar una identidad con otro contenido es un error.
- `expires_at`: hasta cuándo el precio se considera vigente sin reconfirmarlo (≈ 20 min para tiendas; 2 h para tarifas en caché de vuelos).
- `product_ids` (opcional, solo productos): `gtin`, `mpn`, `brand`, `title`. Con ellos el sistema reconoce **el mismo producto en otra tienda** (GTIN válido, o marca + código de fabricante) para la comparación entre tiendas.
- `conditions`: lo que permite comparar. Lo que la fuente no diga debe ser `None` (no lo inventes): una condición `None` desactiva la comparación histórica. Campos requeridos para comparar: vuelo `flight_reference, fare_reference, checked_bags_per_person, cabin_bags_per_person, refundability, changes, cabin`; bus `trip_reference, operator, fare_class, refundability`; producto `product_ref, seller, condition, availability`.
- Productos: `availability` debe ser `in_stock` para que pueda alertar.

## Ejemplo mínimo (bus)

Este bloque se ejecuta en `tests/test_docs.py`; si el contrato cambia, la prueba falla y esta guía debe actualizarse.

```python
# conector-ejemplo
import hashlib
import httpx
from radar.providers.base import BlockedError, Offer, ProviderError, RateLimitedError, TransientError


class MiConectorBus:
    name = "mi-bus"
    kinds = ("bus",)
    live = True
    min_interval = 60
    monthly_cap = None

    def __init__(self, client):
        self.client = client

    def domain(self, criteria):
        return "api.mi-empresa.example"

    def fetch(self, criteria, scenario, now):
        try:
            r = self.client.get(
                "https://api.mi-empresa.example/precios",
                params={"from": criteria.origin, "to": criteria.destination, "date": criteria.departure},
            )
        except httpx.HTTPError as exc:
            raise TransientError("Error de red con la empresa.") from exc
        if r.status_code == 429:
            raise RateLimitedError("Límite de la empresa.", int(r.headers.get("retry-after", 0)) or None)
        if r.status_code == 403:
            raise BlockedError("La empresa rechazó la consulta.")
        if r.status_code != 200:
            raise ProviderError(f"Respuesta inesperada (HTTP {r.status_code}).")
        offers = []
        for item in r.json()["trips"]:
            cents = round(float(item["price"]) * 100)
            conditions = {
                "trip_reference": item["id"],
                "operator": item.get("operator"),      # None si no lo dice: no se compara
                "fare_class": item.get("class"),
                "refundability": item.get("refundable"),
            }
            identity = hashlib.sha256(f"{self.name}|{criteria.canonical()}|{cents}|{sorted(conditions.items())}".encode()).hexdigest()
            offers.append(Offer(self.name, identity, now, now + 1200, cents, criteria.currency,
                                criteria, conditions, "API de la empresa", False))
        if not offers:
            raise ProviderError("Sin viajes para esa fecha.")
        return offers
```

Para activarlo, añádelo en `radar/providers/registry.py` (`build_registry`), igual que `store` y `travelpayouts`: lee sus credenciales del entorno (envueltas en `Secret`) y, si cobra por consulta, exige `RADAR_BUDGET_<NOMBRE>_MONTHLY_CALLS`. Escribe siempre sus pruebas con `httpx.MockTransport`, como `tests/test_providers.py`.

## Conector `store` (páginas de producto y catálogos)

Lee precios de dominios de tu lista permitida (`RADAR_ALLOWED_STORE_DOMAINS` y las tiendas de `RADAR_ENABLED_SOURCES`) que publiquen el precio en **JSON-LD de schema.org** (`Product` → `Offer` con `price` y `priceCurrency`, o un `ItemList` de productos).

- Antes de cada página: dominio permitido → `robots.txt` (RFC 9309 con el analizador propio `radar/robots.py`: regla más larga gana, comodines `*` y `$`, `Crawl-delay`; 404/410 = sin reglas, 5xx = prohibido por ahora, **401/403/429 o página de desafío = rechazo**) → resolución DNS (se rechazan IPs privadas/locales/metadatos de la nube) → petición con User-Agent honesto, sin seguir redirecciones fuera de la lista permitida ni a `http`.
- Respuesta: solo tipos de contenido esperados, tope de 3 MB, y cualquier página de desafío (aunque llegue con un tipo de contenido inesperado) se clasifica como rechazo.
- Rechaza como ambiguo: un rango «desde» (`AggregateOffer` sin ofertas), varias ofertas distintas sin SKU, precio sin moneda, formatos de precio dudosos como `1.299,00`.
- Estado del vendedor/condición ausentes quedan como `store:<dominio>` y `unspecified`; nunca se asume «nuevo».
- **Descubrimiento por catálogo** (`radar/discovery.py`): en vez de un enlace por producto, indicas una tienda o una categoría. *Sitemap*: lee los `Sitemap:` del `robots.txt`, reconoce los sitemaps de producto por el nombre del archivo, ordena por `lastmod`, máx. 4 archivos por barrido con pausas, protecciones contra gzip/DTD/bombas. *Listado*: una sola petición a la página de categoría (JSON-LD `ItemList`); las páginas de producto se reconfirman 1–2 h después. Los seguimientos descubiertos son siempre de alerta **inteligente**.
- **JavaScript:** si el precio solo aparece tras ejecutar JavaScript, lista el dominio en `RADAR_RENDER_DOMAINS` (necesita Playwright; mismo `robots.txt` y mismo ritmo; sin modo sigiloso). Si prefieres no usar navegador, busca la API o el feed de afiliados de la tienda.

**Antes de añadir una tienda:** lee sus términos de uso y su `robots.txt`; las 91 tiendas del catálogo (`sources/stores.json`) están **sin verificar**. Usa `python -m radar.main --verify-sources --only <selector>` desde tu servidor.

## Conector `travelpayouts` (vuelos)

Ver [`vuelos.md`](vuelos.md): tarifas **en caché** de una API de datos autorizada (no en vivo). Es el ejemplo de un conector que **no rasca** nada.

## Aerolíneas, buses y agencias

`sources/travel.json` lista 67 empresas, todas «sin conector». Un conector nuevo para una de ellas solo tiene sentido si publica una API o un feed (o si un proveedor de datos con contrato la cubre). Si su sitio rechaza la automatización, no se hace nada más que anotarlo.

## Conector `demo`

Datos sintéticos (`demo/offers.json`) para vuelo, bus y producto. Escenarios: `auto` (precio que oscila por bloques de 15 min; lo usa el scheduler), `baseline`, `drop`, `failure`, `blocked` (activa el backoff sin tocar un sitio real), `expired`, `different_conditions`, `missing_conditions`, `out_of_stock` (solo productos).
