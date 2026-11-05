# Fuente de vuelos: revisión documental y decisión

Consulta documental: 29/10/2026. Fuente candidata, no integración implementada: **Amadeus Self-Service / Flight Offers Search**.

Fuentes primarias revisadas:

- [Test Data Collection](https://amadeus4dev.github.io/developer-guides/test-data/): test con datos limitados/cacheados, destinado a desarrollo; no equivalen a precios actuales disponibles.
- [Pricing](https://admin.developers.amadeus.com/self-service/apis-docs/guides/developer-guides/pricing/): cuotas gratuitas mensuales en test; producción puede facturar llamadas adicionales.
- [FAQ](https://admin.developers.amadeus.com/self-service/apis-docs/guides/developer-guides/faq/): limitaciones de cobertura y condiciones de acceso/datos. El catálogo no equivale a todo el mercado.

## Lo comprobado y lo pendiente

La documentación confirma la existencia de un entorno de prueba. No se ha creado cuenta, usado una clave, hecho una consulta autenticada ni aceptado un contrato. El precio por operación, la cuota exacta vigente de Flight Offers Search, el acceso regional y la autorización para conservar/mostrar datos **no se consideran verificados para una cuenta concreta**. No se promete una conexión real sin resolverlos. Una disponibilidad documental no demuestra que la ruta deseada devuelva resultados.

Antes de implementar el adaptador real: comprobar estado actual del programa y del endpoint; registro gratuito y límites de la cuenta; condiciones de conservación/publicación de observaciones; coste y bloqueo de gasto; cobertura de rutas; campos realmente disponibles de equipaje y tarifa. Después, probar una consulta autorizada y conservar evidencia sin credenciales. No activar producción ni costes sin autorización expresa.

## Decisión de esta versión

Implementar `DemoProvider`, sin conexión externa. Datos rotulados como sintéticos en archivo, pantalla, API, alertas y documentación. La interfaz `Provider` permite incorporar otra fuente sin contaminar el dominio. Los campos desconocidos se conservan como desconocidos y no se usan para afirmar equivalencia. Nunca usar un «precio desde» genérico de una ruta como si correspondiera a fecha/pasajeros concretos.

Referencia de transporte opcional: [Telegram sendMessage](https://core.telegram.org/bots/api#sendmessage). Los parámetros documentados no incluyen una clave de idempotencia; por ello el adaptador no reintenta automáticamente una entrega sin confirmar. Esto es una decisión de ingeniería a partir del contrato, no una garantía del proveedor.
