# Proyecto: postulador automático para Computrabajo Argentina

Lo lee el chat de DECISIÓN, por sección y cuando lo necesita. PRODUCCIÓN no lo lee: recibe lo que necesita dentro de cada TAREA.

## 1. Objetivo

Una herramienta **personal y local** que busca trabajo por mí en `ar.computrabajo.com`:

1. descubre ofertas nuevas,
2. las filtra y puntúa contra mis CVs,
3. elige el CV que corresponde,
4. y **postula sola**, sin que yo apruebe cada oferta, siempre que se cumplan reglas automáticas (puntaje mínimo, sin duplicados, tope diario, preguntas respondibles con mis datos).

No es un bot de postulación masiva: es un postulador automático con filtros estrictos, así que postula poco y a lo relevante.

Por qué selectivo: los datos de la industria, aunque vengan sobre todo de vendors, apuntan a que un CV adaptado convierte aproximadamente el doble en entrevistas, y a que los reclutadores ya sufren el volumen de postulaciones automáticas. Las reglas del punto 1 de `REGLAS-DURAS.md` son lo que reemplaza a mi aprobación manual.

## 2. Hechos y dudas sobre Computrabajo

Viven en `docs/FINDINGS.md` (única fuente). No los copies acá.

## 3. Referencia: JobNavigator (solo lectura, no copiar su stack)

Repositorio en `C:\Dev\JobNavigator` (open source: Python, FastAPI, Postgres, React, Docker; demasiado pesado para este caso). Su disclaimer pide revisión humana antes de enviar; este proyecto decidió otra cosa (D-008) y la compensa con reglas. Archivos útiles:

- `README.md` y `LEGAL_DISCLAIMER.md`: el flujo descubrir, filtrar, puntuar, adaptar CV, postular y seguir.
- `backend/scraper/sources/computrabajo.py`: ya descubre ofertas con `httpx` y BeautifulSoup (`article[data-id]`, id de 16 hex para dedup, pausa de 0,7 s entre páginas, corta ante 403/429 sin reintentar, falla fuerte si la página 1 viene vacía). **No** trae descripción, **no** postula y **no** cambia de CV: eso es lo que falta.
- `backend/tests/fixtures/computrabajo/` y `backend/tests/test_sources_computrabajo.py`: HTML guardado (listado, listado vacío, tarjeta con rating, detalle) para testear sin red.
- `backend/analyzer/cv_scorer.py`: puntuación con LLM contra varios CVs.
- `backend/analyzer/cover_letter_generator.py` y `backend/resume_templates/`: generación y plantillas; el PDF se exporta con Playwright.
- `backend/scraper/_shared/dedup.py` y `filters.py`: dedup y filtros por título y empresa.
- `extension/content_autofill.js`: cómo se proyectan los datos del perfil a campos de formulario (útil para responder preguntas solo con datos reales).
- `backend/api/routes_applications.py`: estados de una postulación (`applied`, `interview`, `offer`, `rejected`).

## 4. Alcance y stack (decisiones D-001 a D-008)

- Python 3.12 en Windows. `httpx` y `beautifulsoup4` para listados y detalle. `playwright` solo para el tramo logueado (postular y cambiar el CV por defecto). `sqlite3` de la stdlib. `jinja2` más Playwright para renderizar CVs a PDF. SDK de Anthropic para puntuar y adaptar. CLI con `argparse`.
- Sin Docker, sin Postgres, sin frontend web, sin extensión de Chrome. El informe de cada corrida sale por terminal y queda guardado en `data/`.
- Una sola fuente: Computrabajo AR.
- Estructura de carpetas, nombres de módulos y esquema de la base: **los propone DECISIÓN** en la TAREA-001 y los registra en `DECISIONES.md`.

## 5. Fases (cada una termina con tests o una demo)

**Fase 0, reconocimiento supervisado (DECISIÓN más el usuario).** Responder las dudas de `docs/FINDINGS.md`. Es lo único supervisado: sirve para aprender el flujo antes de programarlo, no es la forma de operar. Guion: el usuario elige **una** oferta; DECISIÓN la abre en modo lectura, registra atributos del botón y arranca la captura de red; el usuario hace el click en "Postularme" y completa lo que pida; DECISIÓN registra pantallas, preguntas, requests y cómo queda la oferta después. Entregable: `FINDINGS.md` actualizado. Si el flujo exige algo que rompa las reglas duras, se frena y se habla.

**Fase 1, descubrimiento.** Portar la lógica de `computrabajo.py` y agregar la descarga del detalle para tener la descripción. Dedup por id de 16 hex. Persistencia en SQLite. Tests con HTML guardado. Comando: `scan`.

**Fase 2, filtro y puntaje.** Filtros por título, empresa, ubicación y modalidad. Puntaje 0 a 100 con razones, contra cada variante de CV, quedándose con la mejor. Cache por oferta y registro del costo en tokens. DECISIÓN fija el umbral inicial con puntajes reales y se lo propone al usuario. Comando: `score`.

**Fase 3, CVs por rol.** `profile.yaml` con datos reales, 2 o 3 variantes de rol (no una por oferta), plantillas Jinja, PDF con Playwright. El usuario revisa cada variante una vez, y el diff de cualquier adaptación con LLM, antes de usarla. Asistente para completar el perfil interno de Computrabajo (experiencia y habilidades), siempre con aprobación del usuario. Comando: `cv build`.

**Fase 4, postulación automática.** Para cada oferta que pasa las reglas (`REGLAS-DURAS.md`, punto 1): agrupar por variante de CV para cambiar el CV por defecto lo menos posible (verificándolo en `uploadcv`), postular, responder preguntas solo con datos de `profile.yaml` (si alguna no se puede responder, saltar la oferta y marcarla), verificar que la oferta quedó postulada y registrar. Corre sola, a mano o programada, y termina con el informe de la corrida. El navegador corre visible para que el usuario pueda supervisar, y el tope diario lo ajusta él. Comando: `apply --live`.

**Fase 5, seguimiento.** Estados por postulación, notas y reporte de embudo. Leer Gmail para detectar respuestas queda como mejora futura (ver `email_monitor` en JobNavigator).

**Fase 6, robustez.** Detección de cambio de layout (fallar fuerte), logs en español y un README con el aviso sobre términos de uso y riesgo de bloqueo de cuenta.

**Terminado v1:** `scan`, `score`, `cv build` y `apply` funcionan en modo seco, y una corrida real, supervisada por el usuario, postuló bien, con informe correcto y sin duplicados.

## 6. Qué tiene que aportar el usuario

- `profile.yaml` con datos **reales**: roles que busca, ubicación y modalidad, experiencia, proyectos (con links), tecnologías con nivel real, estudios, idiomas, disponibilidad, y respuestas para las preguntas de filtro típicas (pretensión salarial, disponibilidad, etc.). Hoy su perfil de Computrabajo no tiene experiencia ni habilidades cargadas. Lo crea y lo mantiene solo el usuario; ningún chat lo lee.
- `.env` con la clave de la API (si se usa el SDK de Anthropic). Ningún chat lo lee.
- Haber leído los términos de uso de Computrabajo (el riesgo de bloqueo de cuenta es suyo, y postular sin intervención lo aumenta).
- Revisar una vez cada variante de CV, fijar umbral y tope diario, y leer el informe de cada corrida.
- Python 3.12 y Playwright con Chromium instalados.
