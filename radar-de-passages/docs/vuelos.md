# Vuelos

Los precios de vuelos vienen de **una API de datos autorizada**, no de rascar los sitios de las aerolíneas ni de los buscadores. Es una decisión deliberada: esos sitios usan sistemas anti-bots, sus términos casi siempre prohíben la automatización, y el proyecto no esquiva bloqueos.

## Qué fuente se usa

`travelpayouts`: la API de datos de Aviasales (`prices_for_dates`, servicio de Travelpayouts). Se activa con:

```bash
RADAR_TRAVELPAYOUTS_TOKEN=...                      # lo pides tú a Travelpayouts
RADAR_BUDGET_TRAVELPAYOUTS_MONTHLY_CALLS=3000      # tope mensual (recomendado)
```

## Fuentes revisadas

- **Amadeus Self-Service**: descartada, [el portal de autoservicio cerró](https://www.phocuswire.com/amadeus-shut-down-self-service-apis-portal-developers) (claves desactivadas el 17-07-2026); sus APIs Enterprise requieren contrato comercial.
- **Kiwi.com Tequila y Skyscanner Travel API**: un artículo de terceros afirma que son solo por invitación o comerciales; **no lo verifiqué en una fuente primaria**.
- **Busbud** (buses): solo se obtiene la API mediante un formulario de alianza, sin plan público ni precios; no confirma cobertura de Perú.
- **Travelpayouts / Aviasales Data API**: la que se implementó, con las cautelas que siguen.

Un precio de vuelo guarda siempre sus condiciones desconocidas como desconocidas y nunca usa un «precio desde» como precio de una fecha y unos viajeros concretos.

## Qué es (y qué NO es) un precio de esta fuente

| Es | No es |
|---|---|
| La tarifa **más barata guardada en caché** para esa ruta y esas fechas, a partir de búsquedas reales de usuarios de Aviasales (hasta 7 días de antigüedad). | Una tarifa **en vivo**: puede haber desaparecido cuando la ves. |
| Un precio por **un adulto en económica**. | Un precio con equipaje, cambios o reembolso: no los publica, así que quedan como `unspecified` (comparables solo consigo mismos, nunca «incluido»). |
| Útil para **detectar tendencias y caídas fuertes** de una ruta. | Una confirmación de disponibilidad: el mensaje de cada alerta dice «verifica en la aerolínea antes de decidir». |

Un precio se muestra como vigente durante 2 horas desde que se vio. Las alertas de vuelos usan el modo `target` por defecto (tu precio objetivo).

## Qué debes confirmar tú

- Si el acceso es gratuito para tu cuenta, sus **límites de consultas** y sus **términos de uso**. La documentación pública consultada (01-11-2026) no los indica; el proyecto no los asume. Por eso se recomienda el tope mensual.
- Que el token se pide a Travelpayouts y se guarda solo como variable de entorno (o secreto del servidor). El código lo envuelve en `Secret` y nunca lo escribe en los registros.

## Estado de la verificación

El contrato (endpoint, parámetros, campos) se tomó de su documentación pública y se probó con **respuestas simuladas** (57 pruebas en `tests/test_flights.py`: precios, monedas, caché vencida, `NaN`/infinitos, 401/403/429/5xx, tope mensual, ida y vuelta, vuelos directos). **No se hizo ninguna llamada real**: nadie ha comprobado que tu token funcione, qué rutas de Latinoamérica devuelven datos ni su cobertura de aerolíneas.

## Pendiente (no implementado)

- **Fechas flexibles** (ventana de ±N días, «cualquier día del mes», fin de semana): hoy cada seguimiento vigila una fecha exacta.
- **Aeropuertos cercanos** (p. ej. salir desde Lima o desde otro aeropuerto cercano): la API ofrece una consulta de lugares cercanos, pero no se integró. Mientras tanto se puede crear un seguimiento por aeropuerto.
- **Varios pasajeros, cabinas distintas a económica y escalas máximas distintas de «directo / con escalas»**: la fuente no publica esos datos.
- **Vuelos con aerolíneas que no están en la caché de Aviasales** (por ejemplo, tarifas exclusivas de la web de una aerolínea): requieren una API oficial de esa aerolínea o un proveedor de datos con contrato.

## Aerolíneas, agencias y buses

`sources/travel.json` lista 67 empresas (40 aerolíneas, 17 de buses y 10 agencias) con su país y dominio, todas marcadas **«sin conector»**. No se rasca ninguna. Si alguna publica una API o un feed de afiliados, escribe un conector siguiendo [`conectores.md`](conectores.md); si rechaza la automatización, el sistema debe detenerse y buscar esa alternativa autorizada.
