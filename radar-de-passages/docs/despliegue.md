# Despliegue en la nube

Objetivo: que el monitor corra 24/7 **fuera de tu PC**, en un servidor independiente. Es **un contenedor** (interfaz + API + scheduler) con **un volumen** para la base SQLite.

## Qué necesita la plataforma

- Proceso **siempre encendido** (el monitor trabaja aunque nadie abra la página).
- **Disco persistente** montado en `/data` (ahí vive todo el historial).
- **Una sola réplica.** SQLite es un archivo y el scheduler toma un *lease*; dos instancias no se pisan (hay una prueba que lo comprueba), pero tampoco hay alta disponibilidad.
- HTTPS delante (la app usa autenticación Basic: sin HTTPS la contraseña viaja en claro).
- Memoria: el servidor en reposo ocupa ≈ 53 MB y bajo carga simulada ≈ 70 MB (ver [`../evidence/benchmark.md`](../evidence/benchmark.md)). Una instancia de 256 MB alcanza para páginas normales; usa 512 MB si lees páginas grandes (el tope por página es 3 MB) y **≥ 1 GB si activas el navegador** (Playwright).

## Dónde NO correrlo

| Opción | Por qué no |
|---|---|
| **Tu PC** | Lo pediste: debe seguir funcionando con ella apagada. |
| **GitHub Actions como si fuera un servidor** | Los términos de GitHub para productos adicionales (versión vigente desde el 27-08-2026, consultada el 04-11-2026) limitan Actions con *runners* alojados por GitHub a actividad relacionada con producir, probar, desplegar o publicar el proyecto del repositorio, y prohíben usarlo como computación sin servidor o de forma desproporcionada. Un monitor de precios 24/7 que visita tiendas **no es eso** y arriesga el bloqueo de tu cuenta. GitHub se usa para el **código, el CI y la imagen**, no para ejecutar el monitor. |
| **Render, plan gratuito** | Se duerme a los 15 min sin tráfico y no tiene disco persistente (su documentación, revisada el 01-11-2026). |

## Opciones de alojamiento y costos

Cifras **verificadas** en la página oficial el 04-11-2026 salvo donde se indica. Confirma el precio y la disponibilidad el día que contrates.

| Proveedor | Plan | Costo | Estado de la verificación |
|---|---|---|---|
| **Fly.io** | `shared-cpu-1x`, 256 MB | US$ 2,19 / mes | Verificado (regiones `iad`/`ewr`; otras regiones, incluida `gru`, pueden costar distinto) |
| | `shared-cpu-1x`, 512 MB | US$ 3,69 / mes | Verificado |
| | `shared-cpu-1x`, 1 GB | US$ 6,70 / mes | Verificado |
| | Volumen persistente | US$ 0,15 / GB-mes | Verificado |
| | Salida de datos | US$ 0,02 / GB (Norteamérica y Europa) | Verificado. El tráfico del monitor es casi todo de **entrada** (descargar páginas): confirma en tu factura cómo cuenta la entrada. Una IPv4 dedicada, si la necesitas, tiene costo aparte (no verificado). |
| **Hetzner Cloud** | CX23 (x86) / CAX11 (Arm) | € 5,99 / € 6,49 al mes sin IVA, según su página | **Hoy aparecen como «no disponibles» para pedir** en la página oficial; un análisis de terceros (Bitdoze) reporta subidas de precio en 2026 y falta de stock de la línea CX hasta el 20-10-2026. Verifica antes de planificar con ellos. |
| **Oracle Cloud «Always Free»** | Instancias Ampere (2 OCPU / 12 GB según lo revisado en una sesión anterior) | US$ 0 | **No reverificado hoy.** Oracle puede reclamar instancias inactivas y la disponibilidad por región varía. |
| **DigitalOcean** | Droplets básicos | — | **No verificado.** |

### Estimación mensual (una instancia, sin navegador)

| Concepto | Costo |
|---|---|
| Fly.io, 512 MB + volumen de 1 GB | US$ 3,69 + 0,15 = **≈ US$ 3,84** (con 256 MB: ≈ US$ 2,34) |
| Fly.io con navegador (1 GB + volumen de 3 GB) | ≈ US$ 7,15 |
| Telegram (tus alertas) | US$ 0 (la API de bots es gratuita; no se verificó ningún límite) |
| Vuelos (Travelpayouts) | **No verificado**: confirma con ellos si el acceso a la API de datos es gratuito para tu cuenta |
| Proxy, rotación de IP, servicios de desbloqueo | US$ 0 (**no se usan**) |
| Dominio propio | Opcional; Fly da un subdominio `.fly.dev` |

**Total orientativo: US$ 2,5–4 al mes** para el caso base, **≈ US$ 7** si activas el navegador. Son los precios de lista del proveedor más barato verificado; no incluyen impuestos ni cambios de precio.

### Capacidad (medido, ver evidencia)

- **Por sitio:** con el espaciado por defecto (15–45 s, media 30 s) un sitio recibe **como máximo ≈ 2 800 páginas al día**. Con una revisión cada ~10 min eso son **≈ 20 seguimientos por tienda**; si pones más, cada uno se revisa más despacio (el panel «Estado del monitor» lo avisa) en vez de lanzar ráfagas.
- **CPU:** del orden de **1 % de un núcleo con 100 seguimientos, 4 % con 500 y 7 % con 1 000** (peor caso: el scheduler despierta cada segundo; el real duerme hasta 5 s cuando no hay nada vencido).
- **Disco:** entre 0,3 y 0,5 KB por consulta (estimación a partir de tres corridas; se usa la cota de 0,5 KB). Eso es del orden de **2,5 GB/año con 100 seguimientos, 12 GB/año con 500 y 24 GB/año con 1 000**. **El historial no se poda**: vigila el panel «Recursos» y dimensiona el volumen. Una política de retención segura exige un cambio de esquema (ver «Pendiente»).
- **Ancho de banda:** ≈ 1,2 GB/día con 100 seguimientos y páginas de 90 KB (tamaño **decodificado**, cota superior; con gzip se transfiere bastante menos). No se midió con páginas reales.

## 1. Subir el proyecto a GitHub

```bash
cd radar-de-passages
git init -b main
git add .
git status          # comprueba que NO aparecen .env, runtime/, *.sqlite3 ni node_modules
git commit -m "Radar de Passages v3"
git remote add origin git@github.com:TU-USUARIO/radar-de-passages.git
git push -u origin main
```

`.gitignore` excluye `.env`, `.env.*` (salvo el ejemplo), claves, bases de datos y `node_modules`. **Nunca subas contraseñas ni tokens**: van como variables o secretos del servidor.

Al subir se ejecuta `.github/workflows/ci.yml`: pruebas, comprobación de que no hay secretos, TypeScript (y que `static/app.js` coincide con su fuente), pruebas del navegador (Chromium real, sin red) y construcción de la imagen con una prueba de humo (autenticación, scheduler, catálogo de fuentes, usuario sin privilegios).

## 2. Opción A: servidor propio (VPS) con Docker Compose

En un servidor Ubuntu con Docker y Docker Compose:

```bash
git clone https://github.com/TU-USUARIO/radar-de-passages.git /opt/radar && cd /opt/radar
cp .env.example .env && nano .env     # mínimo: RADAR_ADMIN_PASSWORD (12+ caracteres) y RADAR_CONTACT
docker compose up -d --build          # escucha solo en 127.0.0.1:8003
```

HTTPS automático con Caddy (necesita un dominio apuntando al servidor y los puertos 80/443 abiertos):

```bash
RADAR_DOMAIN=radar.tudominio.com docker compose --profile tls up -d --build
```

Cierra el resto de puertos con el cortafuegos (solo 22, 80 y 443).

**Importante:** los comandos de mantenimiento se ejecutan **como el usuario `radar`** (`-u radar`), para que los archivos de la base no queden a nombre de root:

```bash
docker compose exec -u radar radar python -m radar.main --status
```

### Con navegador (tiendas que pintan el precio con JavaScript)

```bash
WITH_BROWSER=1 docker compose up -d --build     # la imagen crece varios cientos de MB
# y en .env:  RADAR_RENDER_DOMAINS=tienda-js.example   (necesita ≥ 1 GB de RAM)
```

El código del renderizador se probó contra un Chromium real, pero **esta imagen opcional no se construyó aquí**.

## 3. Opción B: Fly.io

```bash
fly launch --no-deploy --copy-config       # elige un nombre único y la región
fly volumes create radar_data --size 1     # disco persistente para SQLite
fly secrets set RADAR_ADMIN_PASSWORD='...' RADAR_CONTACT='tu@correo.example'
fly deploy
fly scale count 1                          # UNA sola máquina (SQLite + un scheduler)
```

`fly.toml` ya trae 512 MB, comprobación de salud y `auto_stop_machines = "off"`. **No se probó desde este entorno**: revísalo antes de usarlo.

## 4. Despliegue por imagen desde GitHub (opcional)

`.github/workflows/deploy.yml` construye la imagen y la publica en GitHub Container Registry (`ghcr.io/TU-USUARIO/radar-de-passages`) cuando subes una etiqueta `v*` (o a mano desde «Actions»). Si además defines estas **variables y secretos** del repositorio, despliega por SSH en tu VPS:

| Tipo | Nombre | Contenido |
|---|---|---|
| Variable | `DEPLOY_HOST` | IP o nombre del servidor (si falta, el despliegue por SSH se omite) |
| Variable | `DEPLOY_USER`, `DEPLOY_PATH` | Usuario SSH y carpeta con `docker-compose.yml` y `.env` (p. ej. `/opt/radar`) |
| Secreto | `DEPLOY_SSH_KEY` | Clave privada **solo para este fin** (sin contraseña de otros usos) |
| Secreto | `DEPLOY_KNOWN_HOSTS` | Salida de `ssh-keyscan -t ed25519 TU-HOST` (se verifica la huella: no se desactiva) |

En el servidor, una vez: `echo "RADAR_IMAGE=ghcr.io/tu-usuario/radar-de-passages:latest" >> .env` y `docker login ghcr.io` con un token personal de solo lectura de paquetes (el paquete es privado por defecto). Este flujo **no se ejecutó** desde aquí. Endurecimiento recomendado: fijar las acciones por *hash* de commit en vez de por etiqueta.

## 5. Activar tiendas (primero verificar, después activar)

Las 91 tiendas del catálogo están **sin verificar**: nadie las contactó. En orden:

```bash
docker compose exec -u radar radar python -m radar.main --list-sources
docker compose exec -u radar radar python -m radar.main --verify-sources --only country=PE --out /data/verify-pe.json
```

`--verify-sources` revisa con pausas largas el `robots.txt`, el sitemap y el JSON-LD de cada tienda **desde tu servidor** (no desde otro sitio) y emite un veredicto por tienda (`usable`, `moneda_distinta`, `precio_no_exacto`, `sin_json_ld`, `bloqueado`…). Lee los términos de uso de las que salgan `usable`, y solo entonces actívalas:

```bash
# en .env
RADAR_ENABLED_SOURCES=falabella-pe,wong-pe       # o country=PE | cat=tecnologia | all
docker compose up -d                              # al arrancar crea sus catálogos (idempotente)
```

Cada tienda queda con un catálogo por sitemap, tope de productos y alertas inteligentes. Desde la interfaz puedes pausar un catálogo o añadir uno por su enlace de categoría.

## 6. Operación

| Qué | Cómo |
|---|---|
| Estado, incidentes, recursos | Interfaz web, `GET /api/status`, `python -m radar.main --status`, `--incidents`, `--metrics` |
| Un sitio quedó pausado | Lee su incidente; si lo resolviste: botón «Reanudar» o `python -m radar.main --resume <dominio>`. **No lo reanudes sin entender el motivo.** |
| Copia de seguridad en caliente | `docker compose exec -u radar radar python -m radar.main --backup /data/backups/radar-$(date +%F).sqlite3` (cron diario recomendado; copia el archivo fuera del servidor) |
| Restaurar | Detén el contenedor, reemplaza `/data/radar.sqlite3` por la copia (y borra `-wal`/`-shm`), vuelve a arrancar |
| Actualizar | `git pull && docker compose up -d --build` (o `docker compose pull && docker compose up -d` con la imagen de GHCR). La migración de esquema es automática y transaccional |
| Registros | `docker compose logs -f radar` (sin tokens ni contraseñas) |
| Cambiar la contraseña | Edita `.env` y `docker compose up -d` |
| Disco | Panel «Recursos»; planifica el volumen (ver capacidad) |

## Pendiente y riesgos

- Nada de esto se ejecutó desde el entorno de desarrollo: ni la imagen de Docker, ni Fly.io, ni Caddy, ni los *workflows*.
- Los precios de proveedores cambian (Hetzner subió y se quedó sin stock en 2026): reverifica.
- Sin política de retención: el historial crece sin límite. Podar consultas antiguas alteraría la mediana ponderada de referencia salvo que se pliegue su conteo en las observaciones (cambio de esquema pendiente).
- El scheduler abre varias conexiones SQLite por ciclo: es la mayor parte de la CPU en reposo y una mejora pendiente (no afecta a la corrección).
- Una sola instancia: si cae, no se vigila hasta que vuelva.
- Revisa los términos de uso de cada tienda antes de activarla: el monitor respeta `robots.txt`, pero no puede leer sus condiciones por ti.
