# Evidencia y límites (v3, 05/11/2026)

Qué se comprobó de verdad, cómo, y qué **no**. Si una afirmación de otro documento no está aquí como «comprobado», trátala como no comprobada.

## Comprobado

- **466 pruebas**, todas sin red real, sin envíos reales, con reloj inyectado y bases temporales.
  - **Python 3.12** (la versión del Docker y del CI): **459 pasan y 1 se omite** (el módulo de navegador, porque Playwright no está en esa instalación). `tests.xml` es esta corrida.
  - **Python 3.13 + Playwright 1.63.0 + Chromium**: las **7 pruebas de navegador pasan** (renderizado opcional contra un Chromium real con rutas simuladas).
  - Reparto: núcleo y API originales (13, sin modificar), API v2 (19) y v3 (35), modelos y migraciones (29 + 7), scheduler (21), restricciones, salida de red y AutoThrottle (46), `robots.txt` (21), tiendas y proveedores (59), vuelos (57), descubrimiento por catálogo/sitemap (51), prioridades de alerta (27), verificador de fuentes (39), arranque automático de catálogos (9), entrega y CLI (20), documentos (6), navegador (7).
- **Prueba de mutación** (`mutations.md`): se rompió a propósito cada una de **19 salvaguardas** en una copia del proyecto y las pruebas fallaron en las 19. En la primera pasada una sobrevivió (el *lease* de instancia única): era un hueco real, se añadió la prueba y se repitió.
- **Migración** v1 → v2 → v3 partiendo de bases creadas con el esquema de cada versión anterior, con historial, y con *rollback* si algo falla (`tests/test_migration_v3.py`).
- **Interfaz** en Chromium real (Playwright): escritorio y 390 px de ancho, sin desborde horizontal ni errores de consola; seguimientos con modo de alerta, catálogos, comparación entre tiendas, incidentes, recursos y salidas de red, con datos sembrados. TypeScript estricto (`npm run check`), y el JavaScript compilado coincide con el código fuente.
- **Arranque de producción emulado** (Python 3.12, solo los archivos que copia el `Dockerfile`, con el `docker-entrypoint.sh` real): sin contraseña de 12+ caracteres **se niega a arrancar**; con ella, el entrypoint como root corrige el dueño del volumen y el servidor corre como **uid 10001**; `/api/health` responde 200 con `scheduler: true`, `/api/watches` da **401** sin credenciales y **200** con ellas, la cabecera HSTS está presente y la contraseña **no aparece en el registro**.
- **Mediciones** (`benchmark.md`): con red simulada, 100 / 500 / 1 000 seguimientos en 5 / 25 / 50 sitios usan 0,9 % / 3,9 % / 6,9 % de un núcleo y ≤ 70 MB de memoria; nunca hubo dos peticiones simultáneas a un sitio y el mínimo entre dos peticiones al mismo sitio fue 16 s (límite configurado: 15 s).
- **Documentos**: pruebas que comprueban que todo enlace relativo apunta a un archivo que existe, que cada variable de entorno que lee el código está en `.env.example` (y al revés), que el conector de vuelos retirado no queda mencionado fuera del registro de decisiones y que las cifras del catálogo citadas (158 fuentes, 91 tiendas, 67 de viajes) son ciertas.
- **Recorrido sintético** (`demo-results.json`, `scripts/demo.py`): cinco observaciones únicas y una alerta simulada, reproducible.

## Defectos reales hallados al verificar (y corregidos)

- `urllib.robotparser` no cumple el RFC 9309 (precedencia de la regla más larga, comodines, tratamiento de 4xx/5xx) → analizador propio con 21 pruebas.
- Un `NaN` o infinito en un precio rompía un conector → se rechaza con prueba de regresión.
- Ninguna prueba comprobaba que el scheduler respeta el *lease* (lo reveló la mutación M19) → prueba añadida.
- En la interfaz, al revisarla en Chromium: campo de enlace sin estilo, categoría por defecto equivocada en el formulario de catálogo, contador de seguimientos en 0 al iniciar, una columna que se partía en dos líneas, un selector truncado y una rejilla que no se apilaba en móvil → corregidos.
- Cifras de crecimiento de disco inconsistentes entre documentos → unificadas (0,3–0,5 KB por consulta) y protegidas por pruebas de documentos.
- Este mismo archivo (versión anterior) seguía nombrando el conector retirado: lo detectó la prueba de documentos y por eso se reescribió.
- De la v2: una redirección en `robots.txt` se trataba como «sin reglas» (se leía una página prohibida), un viaje con fecha pasada se consultaba cada hora para siempre (1 973 filas de error en 75 días simulados) y el prefijo NAT64 `64:ff9b::/96` evadía la comprobación de IP pública. Cada uno tiene prueba de regresión y mutación.

## Revisión

- Revisión **independiente por un segundo agente: NO realizada.** Se intentó y la cuenta alcanzó su límite de uso antes de que produjera hallazgos. Sí hubo revisión propia con sondeos ejecutables y la prueba de mutación. Una revisión externa de seguridad sigue siendo recomendable antes de exponer el servicio a Internet.

## No comprobado

- **Ninguna** tienda, aerolínea, empresa de bus ni la API de Travelpayouts fue contactada. Todo se probó con respuestas simuladas. Se ignora qué tiendas publican su precio de forma estructurada y qué dicen sus términos: eso lo decide `--verify-sources` **desde tu servidor** (y tu lectura de los términos), no este proyecto.
- La imagen Docker **no se construyó** (no hay daemon aquí); `docker-compose.yml`, `Caddyfile` y `fly.toml` no se ejecutaron, y los workflows de GitHub Actions (CI y despliegue) tampoco.
- **Telegram real**: la entrega nunca se probó contra Telegram.
- Rendimiento con **páginas reales** (tamaños, TLS, latencia) y el consumo del navegador con Playwright en un sitio real.
- Costos de proxies y de DigitalOcean (no verificados); Oracle no se volvió a verificar; los planes de Hetzner aparecían como no disponibles.
- **No implementado**: fechas flexibles y aeropuertos cercanos para vuelos; poda o compactación del historial (necesita un cambio de esquema); reducir las conexiones SQLite abiertas por ciclo del scheduler.
- No se contrató, desplegó ni publicó ningún servicio.

## Reproducir

```bash
pip install -r requirements-lock.txt      # incluye pytest
python -m pytest -q                       # 459 + 1 omitida en Python 3.12
pip install playwright==1.63.0 && playwright install chromium
python -m pytest -q tests/test_rendered.py   # las 7 pruebas de navegador
python scripts/mutations.py               # 19 mutaciones (varios minutos)
python scripts/benchmark.py --watches 100 --domains 5 --hours 2 --step 1.0
python scripts/demo.py
```
