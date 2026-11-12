# compuapply

Postulador automático para Computrabajo Argentina (`ar.computrabajo.com`), escrito en Python para Windows.

Busca ofertas, las puntúa contra tu CV con un modelo de OpenAI y arma un CV a medida para cada oferta. Después postula con un navegador real (Playwright) y contesta las preguntas de selección con tu CV, tus respuestas declaradas y un banco de hechos que escribís vos. Lo que no puede contestar sin inventar lo deja pendiente para que lo contestes una vez.

> **Aviso.** Automatizar un sitio puede ir contra los términos de uso de Computrabajo y puede causar el bloqueo de la cuenta. El programa no evade defensas: ante un CAPTCHA, un bloqueo o una pantalla que no reconoce, frena y avisa. Usalo bajo tu responsabilidad.

## Cómo funciona

1. **`scan`**: lee los listados de las búsquedas, guarda las ofertas en SQLite (`data/`) y baja el detalle de cada una. Filtra por zona, empresa y palabras del título.
2. **`score`**: el modelo puntúa cada oferta de 0 a 100 contra cada variante de CV y explica por qué. El gasto queda registrado y tiene tope por corrida y por mes.
3. **`cv`**: arma un CV a medida de la oferta, en PDF, con datos de tu CV base y del banco de verdades. Un control rechaza tecnologías, cifras o títulos que no estén en tus datos.
4. **`apply --live` / `tanda`**: postula con ese CV. Contesta las preguntas así:
   - Lo declarado en `answers` (sueldo, disponibilidad, localidad, modalidad) se contesta con reglas del código.
   - Lo que respalda el CV o el banco de verdades lo contesta el modelo, y el código lo verifica.
   - El modelo escribe las preguntas de opinión («¿por qué te gustaría…?»), con un control de estilo para que no suenen a plantilla.
   - Lo demás queda **pendiente**: la oferta no se envía.
5. **`pending --answer`**: lista las preguntas pendientes sin repetir. Cada respuesta tuya se guarda como un hecho en el banco de verdades y la oferta vuelve a la cola. CUIL, DNI, teléfono y dirección no se guardan nunca.

Al terminar, cada corrida escribe un informe en `data/runs/` y vuelve a dejar como CV por defecto el que declaraste. También verifica en «Mis postulaciones» que cada envío figure.

Hay una interfaz gráfica (`Iniciar.bat` o `python -m compuapply.gui`) que corre los mismos comandos. El manual completo, con cada comando, opción y freno, está en [`docs/MANUAL.md`](docs/MANUAL.md).

## Lo que se quitó de este repositorio y tenés que agregar

Esta copia no tiene datos personales. Para que funcione en otra computadora hay que crear lo siguiente, y nada de eso se sube a git (`.gitignore` ya lo excluye):

| Qué | Dónde | Cómo |
|---|---|---|
| Clave de OpenAI | `.env` | `python -m compuapply setup env`, o a mano: `OPENAI_API_KEY=...` y opcional `OPENAI_MODEL=gpt-4o-mini`. Poné un límite de gasto en platform.openai.com. |
| Perfil | `profile.yaml` | Copiá `profile.example.yaml` y completalo, o usá `python -m compuapply setup profile --cv-dir <carpeta>`. Lleva variantes, zonas, topes, umbral y `answers`. |
| CV base | `cv/` (o la ruta que pongas en `resume`) | Tu CV en formato `plain_text_resume.yaml` (el de AIHawk). Ejemplo: `examples/resume.example.yaml`. |
| PDF base | `cv/` (campo `pdf`) | El PDF de tu CV, generado y revisado por vos. |
| Banco de verdades | un YAML en `cv/`, apuntado por `truths:` en la variante (fuera de `cv/` git no lo ignora) | Lista de hechos cortos y ciertos sobre vos. Ejemplo: `examples/truths.example.yaml`. `pending --answer` le agrega líneas. |
| CV por defecto en la cuenta | `default_pdf_name` en la variante | El nombre exacto del CV base tal como figura en la página de CVs de Computrabajo. |
| Sesión de Computrabajo | `.browser-profile/` | `python -m compuapply login`: se abre Chromium y te logueás a mano una vez. El programa nunca lee ni guarda tu contraseña. |
| Base de datos e informes | `data/` | Se crean solos en la primera corrida. |

Las rutas de `resume`, `pdf` y `truths` pueden ser absolutas o relativas a la carpeta de `profile.yaml`. El código no tiene rutas de ninguna máquina en particular.

## Instalación

Requisitos: Windows, Python 3.13 y una cuenta de Computrabajo Argentina con al menos un CV subido.

```bash
pip install -r requirements.txt
python -m playwright install chromium
python -m compuapply setup env
copy profile.example.yaml profile.yaml
python -m compuapply profile check
python -m compuapply login
```

Primera prueba, en modo seco (no envía nada):

```bash
python -m compuapply run
```

Postular de verdad a las ofertas de unas búsquedas, durante 60 minutos como máximo:

```bash
python -m compuapply tanda --variant <id> --keywords "vendedor,atencion al cliente" --minutes 60
```

Para frenar cualquier corrida, creá un archivo `STOP` en la carpeta del proyecto (o tocá «Detener» en la interfaz).

Tests (no usan red ni OpenAI):

```bash
python -m pytest -q
```

## Estado

Funciona de punta a punta y ya hizo cientos de postulaciones reales. La calidad de las respuestas a las preguntas de selección es lo que más se trabajó y lo que más falta.

## Lo que le falta para estar terminado

**Respuestas a preguntas**
- Las preguntas nuevas aparecen todo el tiempo y cada familia (residencia, cercanía, rangos de años, listas, actitud, opinión) necesitó reglas propias. Cada tanda real todavía muestra casos mal contestados o pendientes de más.
- El control de «cero invención» se endureció después de que el modelo afirmara cosas que no estaban en los datos. Hay que confirmar con tandas cortas y revisadas que no vuelva a pasar antes de correr tandas largas.
- El control de estilo trabaja con una lista de frases y patrones. Falta uno más general.
- Las preguntas de cercanía o distancia («¿vivís cerca de X?», «¿a cuántos km?») quedan pendientes porque el programa no sabe geografía. Falta un dato de distancia o un mapa de localidades.

**Postulación**
- Algunas postulaciones llegan a la pantalla de éxito pero no aparecen en «Mis postulaciones». La causa no está clara: quizás ofertas cerradas o que ya llenaron el cupo.
- No hay forma automática de retirar una postulación enviada por error.
- El umbral de puntaje no está calibrado con etiquetas (`review label` / Calibrar).

**Alcance**
- Solo Computrabajo Argentina y solo Windows (el programador de tareas y el acceso directo son de Windows).
- Seguimiento (fase 5 de `prompts/PROYECTO.md`): estados por postulación, notas, embudo y lectura del mail para detectar respuestas de las empresas.
- Robustez (fase 6): detectar cambios de diseño del sitio y fallar con un mensaje claro en vez de un timeout.

## Cómo se construyó

El proyecto lo desarrollaron dos chats de Claude Code con roles separados: uno decide y revisa, el otro escribe código y tests. Las instrucciones están en `prompts/` y `00-LEEME.md`. Las tareas, reportes y bitácoras de decisiones (`handoff/`, `docs/ESTADO.md`, `docs/DECISIONES.md`, `docs/FINDINGS.md`) no están en esta copia porque tenían datos personales.

## Licencia

Sin licencia por ahora: todos los derechos reservados.
