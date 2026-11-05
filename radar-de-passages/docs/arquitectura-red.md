# Arquitectura de red y de IP

Cómo sale el monitor a Internet, qué hace cuando un sitio le dice «no» y qué se evaluó (y se descartó) para evitar bloqueos.

## Principio

> Si una fuente rechaza la automatización, el monitor **detiene** las solicitudes a esa fuente, lo registra y te lo dice. No intenta pasar por otro lado.

Por eso el diseño es de **cortesía**, no de evasión: pocas peticiones, espaciadas, con identidad honesta, respetando `robots.txt`, y parada total ante un rechazo explícito.

## Dónde corre

En un **servidor independiente y siempre encendido** (un VPS o un contenedor en la nube con disco persistente), nunca en tu PC. Un solo proceso hace de interfaz web, API y scheduler; la base es un archivo SQLite en un volumen. Opciones y costos en [`despliegue.md`](despliegue.md).

```
 Internet ──HTTPS──► Caddy (opcional) ──► radar (UI + API + scheduler) ──► SQLite (/data)
                                                │
                                                ├─ DomainGate: una petición a la vez por sitio, 15–45 s entre peticiones, AutoThrottle
                                                ├─ Detector de restricciones ──► incidentes ──► Telegram (opcional)
                                                └─ Gestor de salida (egress) ──► «direct» (por defecto) o un proxy autorizado por dominio
```

## Gestor de salida (egress)

- **Un perfil `direct`** siempre existe: la petición sale desde la IP del servidor. Es el valor por defecto y el recomendado.
- Puedes declarar **proxies autorizados** (por ejemplo, uno que te entregue la propia tienda o un proveedor de datos con el que tengas contrato) y asignarlos a dominios concretos:

  ```bash
  RADAR_EGRESS_PROFILES=proxy_a
  RADAR_EGRESS_PROXY_A_URL=http://usuario:clave@host:puerto     # se guarda como Secret
  RADAR_EGRESS_PROXY_A_ENABLED=1
  RADAR_EGRESS_MAP=tienda.example=proxy_a,*.ejemplo.cl=proxy_a
  RADAR_EGRESS_DEFAULT=direct
  ```

- **Cada dominio usa una sola salida** (coincidencia exacta o por sufijo). No hay lista de salidas «de repuesto».
- **No hay conmutación automática.** Si una salida se apaga, sus dominios dejan de recibir peticiones (**no** caen a `direct`); si un sitio bloquea, **no** se prueba con otra salida.
- Toda petición queda **trazada**: dominio, estado HTTP, latencia, bytes, tipo de contenido y nombre de la salida (nunca la URL del proxy ni sus credenciales).
- Desde la interfaz («Recursos y salidas de red») puedes apagar o activar cada salida. La URL del proxy no sale nunca del servidor: la API solo expone nombre, tipo, estado y `host:puerto`.

Pruebas que lo garantizan: `test_each_domain_has_one_exit_exact_or_by_suffix_and_the_default_is_direct`, `test_a_block_pauses_the_domain_and_never_moves_it_to_another_exit`, `test_a_disabled_exit_refuses_its_domains_and_never_falls_back_to_direct`, `test_proxy_credentials_never_appear_in_the_public_description_or_repr`.

## Qué pasa cuando un sitio dice «no»

Un único detector clasifica cada respuesta o error. Solo cuentan como **rechazo** las negativas explícitas (un `reCAPTCHA` incrustado en una página que sí trae precio no es un bloqueo).

| Qué se detecta | Se trata como | Reacción |
|---|---|---|
| 403, «acceso denegado», 429, CAPTCHA o página de verificación, pedido de inicio de sesión, redirección a un desafío o a un login | **Rechazo** (grave) | Se **pausa todo el sitio**: 15 min, luego 30, 60… hasta 6 h (respeta `Retry-After`). Tras 5 seguidos el **circuito se abre** y el sitio queda apagado hasta que tú lo reanudes. Se registra el incidente y se avisa por Telegram (1 mensaje por sitio y hora). |
| La página dejó de traer precio (estructura cambiada), 404/410, respuesta vacía | Problema **del seguimiento** | Se pausa solo ese seguimiento (estructura tras 5, inexistente tras 3). |
| Timeout, error de red, 5xx | **Transitorio** | Tras 3 seguidos en un sitio, descansa 120 s (duplicando, tope 1 h). No se cuenta como rechazo. |
| Cualquier tipo de fallo repetido | — | Un seguimiento con 8 fallos seguidos queda pausado hasta que lo revises. |

Los reintentos son acotados y **nunca inmediatos**. Para reanudar un sitio tras revisarlo: botón «Reanudar» en la interfaz o `python -m radar.main --resume <dominio>`. El diario de incidentes es de solo-añadir (`python -m radar.main --incidents`) y no guarda credenciales.

Pruebas: `test_a_captcha_pauses_the_domain_logs_the_incident_and_stops_the_requests` (un dominio pausado **no recibe más peticiones**), `test_every_restriction_kind_pauses_the_whole_domain_not_just_one_watch`, `test_circuit_opens_after_repeated_rejections_and_only_a_human_resumes_it`, `test_transient_failures_across_a_domain_make_it_cool_down_instead_of_being_hammered`.

## Sobre las IP de centro de datos

Tienes razón en que las IP de proveedores de nube se reconocen fácilmente y algunos sitios las bloquean sin importar tu ritmo. El proyecto **no** lo esquiva. Lo que hace es:

1. Mantener un ritmo bajo y humano (5–15 min por seguimiento, 15–45 s entre peticiones a un sitio).
2. Detectar el bloqueo, parar y dejar constancia.
3. Decirte cuál es el sitio afectado, para que busques una vía autorizada.

Vías legítimas, en orden de preferencia: **API o feed oficial de la tienda** (muchas tienen programa de afiliados con feed de productos y precios), **un proveedor de datos con contrato**, **pedir permiso** al sitio (a veces te dan una clave o una lista blanca de IP), o **retirar ese sitio** de la lista.

## Qué se evaluó y por qué no se adoptó

| Opción | Decisión | Motivo |
|---|---|---|
| **Rotación de IP** | No | Cambiar de IP al recibir un bloqueo es evadir un control de acceso, justo lo que pediste evitar. Tampoco se reparte la carga entre IP para saltarse límites por IP. |
| **Tor** | No | Las salidas de Tor son públicas y muchos sitios las bloquean de entrada; es lento; y usarlo para continuar tras un rechazo es evasión. |
| **Proxies residenciales** | No por defecto | Solo tendrían sentido si los términos del sitio lo permiten y con un proveedor que puedas verificar (cómo obtiene sus IP y con qué consentimiento). Las IP residenciales no son una manera de «seguir» después de un bloqueo. No se verificó ningún proveedor ni su precio. |
| **Proxy autorizado por dominio** | Sí, opcional | Ya está soportado con el gestor de salida: una salida fija por dominio, sin conmutación. |
| **Scrapy + AutoThrottle** | AutoThrottle sí (propio); Scrapy no | Ver [`auditoria.md`](auditoria.md). |
| **Playwright** | Opcional por dominio | Solo si el precio se pinta con JavaScript; mismo `robots.txt`, mismo ritmo, User-Agent honesto, sin ocultar que es un navegador automatizado. |

## Identidad y cortesía

- User-Agent honesto con un contacto que **tú** defines en `RADAR_CONTACT` (es el único origen del contacto; ningún otro dato personal se envía).
- `robots.txt` según RFC 9309 (analizador propio), incluido `Crawl-delay`. Si falta (404/410), no hay reglas; si responde 5xx, se presume **prohibido por ahora** y se reintenta más tarde; si responde **401, 403, 429 o una página de desafío**, se trata como un **rechazo explícito** (el RFC permitiría seguir tras un 4xx, pero este proyecto es más estricto a propósito) y se pausa el sitio.
- Solo dominios de tu lista permitida, solo HTTPS, redirecciones revalidadas, sin IP privadas (protección SSRF), tope de 3 MB por página y lista de tipos de contenido.
- Nada de cookies de sesión, inicio de sesión ni formularios: si un sitio lo exige, se detiene.

## Seguridad y privacidad

- Secretos solo por variables de entorno, envueltos en `Secret`; los registros pasan por un filtro que borra tokens de Telegram, `user:clave@`, `Authorization`/`Bearer` y cualquier secreto declarado. El registro HTTP de `httpx` se silenció porque la URL de Telegram lleva el token.
- El diario de incidentes y las trazas de petición guardan las URL **sin credenciales**.
- En producción la app no arranca sin contraseña de 12+ caracteres y toda la API la exige (salvo `/api/health`).
- `.env` nunca va a Git (`.gitignore`); la imagen no copia `.env`, pruebas ni documentos.
- No hay datos personales de terceros: solo precios públicos y tus propios ajustes.

## Riesgos que quedan

- Un sitio puede cambiar sus términos o su estructura: el monitor lo notará como «estructura cambiada» o como un rechazo, pero no puede leer los términos por ti.
- Los precios de JSON-LD pueden estar desactualizados o ser incorrectos: por eso hay vigencia de 20 minutos, reconfirmación y el marcador de «probable error de precio».
- Una sola instancia: sin alta disponibilidad.
