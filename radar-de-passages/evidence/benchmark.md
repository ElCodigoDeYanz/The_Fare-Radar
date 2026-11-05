# Mediciones de recursos (benchmark local, red simulada)

`python scripts/benchmark.py --watches N --domains D --hours H --step 1.0`

Qué es: el **scheduler, el espaciado por sitio (15–45 s), el AutoThrottle, el lector JSON-LD, la base SQLite y las reglas de alerta reales**, con un reloj simulado y una «red» que es un *stub* en memoria que responde `robots.txt` y una página de producto de 90 KB con JSON-LD. Nada sale de la máquina. Cada seguimiento es un producto distinto repartido en D sitios; el 2 % de las respuestas trae un precio distinto.

Qué **no** es: no mide latencia de red real, TLS, tamaños reales de página ni compresión (los bytes son los **decodificados**, una cota superior de lo transferido), ni sitios con JavaScript. La CPU incluye el coste de despertar el scheduler **cada segundo simulado** (peor caso; el bucle real duerme hasta 5 s cuando no hay nada vencido).

Entorno: sandbox de 2 vCPU, Python 3.13 (la imagen usa 3.12). Una corrida por escenario (seed 1); no hay barras de error: son órdenes de magnitud.

## Resultados

| | 100 seguimientos / 5 sitios | 500 / 25 sitios | 1 000 / 50 sitios |
|---|---|---|---|
| Horas simuladas | 2 | 1 | 0,75 |
| Consultas de página | 1 100 | 2 746 | 4 127 |
| Intervalo medio real por seguimiento | 654 s | 655 s | 654 s |
| CPU total | 65 s | 141 s | 187 s |
| **CPU si corriera en tiempo real** (% de un núcleo) | **0,9 %** | **3,9 %** | **6,9 %** |
| Memoria máxima del proceso | 66 MB | 70 MB | 70 MB |
| Descargado (cuerpos decodificados) | 97 MB | 241 MB | 362 MB |
| → por día, a 90 KB/página | 1,2 GB | 5,8 GB | 11,6 GB |
| Mínimo entre dos consultas al mismo sitio | 16 s | 16 s | 16 s |
| Máximo de consultas simultáneas a un sitio | 1 | 1 | 1 |
| Máximo de páginas por sitio y día (extrapolado) | 2 664 | 2 760 | 2 816 |
| Alertas / incidentes | 15 / 0 | 0 / 0 | 0 / 0 |
| Tamaño de la base al final | 0,75 MB | 1,9 MB | 3,2 MB |

(Las 15 alertas del primer escenario vienen de las bajadas de precio sintéticas del 2 % de las respuestas.)

## Lo que dicen

- **Las invariantes de cortesía se mantienen bajo carga**: nunca dos peticiones simultáneas a un sitio, y el mínimo entre dos peticiones nunca bajó de los 15 s configurados (la resolución del reloj simulado es 1 s).
- **Capacidad por sitio ≈ 2 800 páginas/día** (un sitio cada 30 s de media). Con 20 seguimientos por sitio el intervalo medio sale ≈ 650 s, dentro de la ventana 5–15 min; con más, cada uno se revisa más despacio, no en ráfagas.
- **Memoria**: el servidor completo en reposo (API + scheduler, sin seguimientos) ocupa ≈ 53 MB y el proceso de prueba bajo carga ≈ 70 MB: una máquina de 256 MB alcanza; se recomienda 512 MB por las páginas grandes (tope 3 MB).
- **CPU**: pequeña. Casi toda es el coste de cada ciclo del scheduler (varias conexiones SQLite por ciclo), no del análisis de las páginas: hay margen de mejora y no afecta a la corrección.
- **Disco**: entre **0,3 y 0,5 KB por consulta** (el ajuste exacto de las tres corridas da ≈ 0,3 KB por consulta y ≈ 1,9 KB por seguimiento; se toma 0,5 KB como cota prudente, y con tres puntos el ajuste es ruidoso). Con una consulta por seguimiento cada ~11 min (≈ 132 al día), la cota prudente da **≈ 2,5 GB/año con 100 seguimientos, ≈ 12 GB/año con 500 y ≈ 24 GB/año con 1 000**. **El historial no se poda.**
- **Ancho de banda**: las cifras por día son cotas superiores (decodificadas, 90 KB). Con compresión real y páginas más pequeñas o más grandes variarán; mídelo en tu servidor con el panel «Recursos» o `python -m radar.main --metrics`.

## Qué falta medir

CPU y memoria con **páginas reales**, TLS y latencia real, el navegador (Playwright), y cuánto del tráfico facturan los proveedores como entrada o salida.
