# Plan por fases

Cada fase tiene un **criterio de salida**: no pases a la siguiente hasta cumplirlo.

| Fase | Qué se hace | Quién | Criterio de salida |
|---|---|---|---|
| **0. Hecho** | Núcleo con historial, scheduler 5–15 min, alertas por objetivo y por prioridad, comparación entre tiendas, descubrimiento por catálogo/sitemap, detector de restricciones, gestor de salida, 158 fuentes catalogadas, interfaz, documentos, 466 pruebas, 19 mutaciones, mediciones | Este proyecto | Entregado (ver [`../evidence/verification.md`](../evidence/verification.md)) |
| **1. Servidor** | Contratar un VPS o Fly.io, desplegar con `RADAR_ENABLE_DEMO=1` y sin tiendas, poner contraseña, HTTPS y `RADAR_CONTACT` | Tú | La interfaz abre por HTTPS, `/api/health` da `scheduler: true`, la copia de seguridad se ejecuta |
| **2. Verificar** | `--verify-sources --only country=XX` desde el servidor; leer los términos de las tiendas `usable`; (opcional) conectar Telegram | Tú | Una lista corta de tiendas con veredicto `usable` y términos revisados |
| **3. Activar poco a poco** | `RADAR_ENABLED_SOURCES` con 3–5 tiendas; observar **48 h** el panel de incidentes y el uso de recursos | Tú | 0 incidentes graves; el intervalo real por seguimiento está dentro de lo esperado; el disco crece según lo estimado |
| **4. Ampliar** | Más tiendas por país y categoría (siempre de a pocas); token de Travelpayouts y 3–5 rutas de vuelo | Tú | Sin incidentes graves nuevos durante una semana |
| **5. Viajes** | Conectores para buses, aerolíneas y agencias **solo** cuando exista una API, un feed de afiliados o un proveedor de datos con contrato ([`conectores.md`](conectores.md)) | Tú + este proyecto | Cada conector con sus pruebas simuladas y sus términos leídos |
| **6. Si hace falta** | Retención/compactación del historial (cambio de esquema), fechas flexibles y aeropuertos cercanos, reducir conexiones SQLite por ciclo, PostgreSQL si hubiera varias réplicas, revisión externa de seguridad | Este proyecto | Una necesidad real medida, no una suposición |

Si en cualquier fase un sitio **rechaza** la automatización, no se busca la manera de continuar: se detiene ese sitio, se registra y se busca una vía autorizada ([`arquitectura-red.md`](arquitectura-red.md)).
