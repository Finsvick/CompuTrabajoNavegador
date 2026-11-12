# Workspace: asistente de postulaciones para Computrabajo

Herramienta personal que busca y postula sola en `ar.computrabajo.com`, con reglas estrictas (puntaje mínimo, sin duplicados, tope diario, preguntas respondibles). Se construye con **dos chats de Claude Code** que se reparten el trabajo.

## Carpetas

```
00-LEEME.md            esta guía
prompts/
  REGLAS-DURAS.md      reglas que valen para los dos chats
  ROL-DECISION.md      instrucciones del chat que lee y decide
  ROL-PRODUCCION.md    instrucciones del chat que escribe código
  PROYECTO.md          objetivo, alcance, stack y fases (lo lee DECISIÓN)
docs/                  lo mantiene DECISIÓN
  ESTADO.md            dónde estamos (máx. 60 líneas)
  DECISIONES.md        registro de decisiones, una línea cada una
  FINDINGS.md          hechos verificados e incógnitas sobre Computrabajo
handoff/               el traspaso entre chats
  TAREA-NNN.md         la escribe DECISIÓN
  REPORTE-NNN.md       lo escribe PRODUCCIÓN
src/, tests/, ...      aparecen con la TAREA-001 (los crea PRODUCCIÓN)
```

## Cómo se trabaja

Abrí **dos sesiones** de Claude Code en esta carpeta. No se hablan entre sí: se pasan el trabajo por archivos y vos hacés de puente.

1. **DECISIÓN** decide qué sigue y escribe `handoff/TAREA-NNN.md`.
2. Vos abrís (o limpiás) el chat de **PRODUCCIÓN** y le decís: `Ejecutá handoff/TAREA-NNN.md`.
3. **PRODUCCIÓN** escribe el código y `handoff/REPORTE-NNN.md`.
4. Vos volvés al chat de **DECISIÓN**: `Revisá handoff/REPORTE-NNN.md`.
5. DECISIÓN acepta o devuelve, actualiza `ESTADO.md` y escribe la siguiente tarea.

Una tarea por sesión. Al terminar, cerrá el chat y abrí uno nuevo: el contexto vive en los archivos, no en el historial.

## Primer mensaje de cada chat

**DECISIÓN**

```
Leé prompts/ROL-DECISION.md, prompts/REGLAS-DURAS.md y docs/ESTADO.md. Después proponé la TAREA-001.
```

**PRODUCCIÓN**

```
Leé prompts/ROL-PRODUCCION.md y prompts/REGLAS-DURAS.md. Ejecutá handoff/TAREA-001.md.
```

## Quién escribe qué (para que no se pisen)

| Carpeta o archivo | DECISIÓN | PRODUCCIÓN | Usuario |
|---|---|---|---|
| `docs/*`, `handoff/TAREA-*` | escribe | no toca | lee |
| `handoff/REPORTE-*` | lee | escribe | lee |
| `src/`, `tests/`, dependencias | solo lee | escribe | lee |
| `prompts/*` | solo lee | solo lee | edita |
| `profile.yaml`, `.env`, `.browser-profile/` | no lee | no lee | crea y mantiene |

## Modelo sugerido (es una recomendación, no una regla)

- **DECISIÓN:** el modelo más capaz que tengas disponible y esfuerzo alto. Decide poco, pero cada decisión pesa.
- **PRODUCCIÓN:** un modelo intermedio (por ejemplo Sonnet 5.5) con esfuerzo medio. Ejecuta tareas bien acotadas.
- **Búsquedas amplias:** un subagente barato (por ejemplo Haiku 5.5) que devuelva 15 líneas o menos.

## Lo que tenés que aportar vos

- `profile.yaml` con tus datos reales (ver `prompts/PROYECTO.md`, sección 6). Nadie más lo lee.
- `.env` con la clave de API, si se usa el SDK de Anthropic.
- Haber leído los términos de uso de Computrabajo. El riesgo de bloqueo de cuenta es tuyo.
- Revisar cada variante de CV una vez, fijar umbral y tope diario, y leer el informe de cada corrida.
- El click real en "Postularme" en la Fase 0 (solo esa vez, para aprender el flujo).
- Python 3.12 y Playwright con Chromium instalados.
