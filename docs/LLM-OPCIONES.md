# LLM para `score`: opciones (investigado 2026-11-02)

Lo mantiene DECISIÓN. Precios por millón de tokens, en USD, de las páginas oficiales salvo Mistral (fuente secundaria). Caso: unos 3.000 tokens de entrada por oferta (2.000 se repiten: instrucciones y CVs), unos 500 de salida (razonamiento incluido) y 1.500 ofertas por mes.

| Modelo | Entrada / caché / salida | USD por mes | Salida JSON con esquema | Notas |
|---|---|---|---|---|
| OpenAI gpt-5-nano | 0,05 / 0,005 / 0,40 | ~0,40 | sí, estricta | el más barato; modelo de 2025 |
| OpenAI gpt-6-luna | 0,10 / 0,01 / 0,50 | ~0,55 | sí, estricta | generación actual |
| Anthropic Haiku 5.5 | 0,10 / 0,01 / 0,50 | ~0,55 | sí, estricta | sin modelo de respaldo si rechaza un pedido |
| Google Gemini 2.5 Flash-Lite | 0,10 / 0,01 / 0,40 | ~0,50 | sí | modelo de 2025; caché cobra almacenamiento por hora |
| Google Gemini 3.1 Flash-Lite | 0,25 / 0,025 / 1,50 | ~1,50 | sí | gratis con límites, pero en el nivel gratis Google usa los datos |
| DeepSeek deepseek-flash (V4.1) | 0,15 / 0,003 / 0,60 fuera de pico | ~0,45 a 0,90 | solo modo JSON, sin esquema | pico 01-04 y 06-10 UTC; servidores en China |
| Mistral Small 4 | 0,15 / 0,015 / 0,60 | ~0,90 | sí | precio sin confirmar en la página oficial |

Batch: 50% menos en OpenAI, Anthropic, Google y Mistral. Con este volumen ahorra centavos y agrega espera.

## Qué exige la tarea a la API

1. Salida JSON validada contra un esquema (puntaje 0-100, CV elegido, razones): si no, el programa tiene que reintentar o adivinar.
2. Buen español rioplatense y jerga de avisos.
3. Privacidad: el CV va en cada pedido. Mandar solo habilidades, experiencia y estudios, nunca nombre, email, teléfono ni DNI. Nada de niveles gratis que entrenan con los datos.
4. `usage` en la respuesta para registrar el costo (lo tienen todos).
5. Una sola dependencia nueva y un solo proveedor en producción.
6. Precio estable: Gemini 3.x Flash duplica precios el 2027-01-01; DeepSeek cambia según la hora.

## Conclusión

La diferencia de precio entre los 4 mejores es de centavos por mes, así que decide la calidad en ESTA tarea, no el precio. Los benchmarks públicos no comparan bien los modelos chicos ni miden puntuar avisos en español: hay que medir con ofertas reales.

Finalistas: gpt-6-luna, Haiku 5.5, gpt-5-nano. Descartados: DeepSeek (sin esquema estricto, datos en China), Gemini (Flash-Lite actual cuesta 3 veces más, el barato es viejo, precios inestables), Mistral (sin ventaja de precio ni calidad conocida).

Calibración propuesta: el usuario etiqueta 30 ofertas reales (postularía sí / no), los 3 finalistas las puntúan con el mismo prompt y gana el que más coincide. Costo estimado: menos de USD 0,10.

## Verificado en vivo (2026-11-02)

- `gpt-4o-mini` con Responses API: rechaza `reasoning.effort` (400 `unsupported_parameter`); sin ese parámetro acepta el JSON estricto del puntaje. Pedido mínimo: 127 tokens de entrada, 90 de salida.
