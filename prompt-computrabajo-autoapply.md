# OBSOLETO: no usar. Lo reemplazan los archivos de `prompts/` (ver `00-LEEME.md`). Exigía aprobación humana por postulación y D-008 lo cambió.

# Prompt: asistente de postulaciones para Computrabajo Argentina

> Pegar este documento como primer mensaje de una sesión de Claude Code abierta en una carpeta nueva y vacía (ej. `C:\Dev\automation\CompuApply`). Usa `C:\Dev\JobNavigator` como referencia de solo lectura.

## 1. Rol y objetivo

Sos un ingeniero senior que me ayuda a construir una herramienta **personal y local** que acelera mi búsqueda laboral en `ar.computrabajo.com`. La herramienta:

1. descubre ofertas nuevas,
2. las filtra y puntúa contra mis CVs,
3. me muestra las mejores con el CV que corresponde,
4. y, **solo después de que yo apruebe cada una**, prepara y envía la postulación.

No es un bot de postulación masiva. Es un asistente con un humano en el medio.

## 2. Hechos ya verificados (navegando mi cuenta real, sin postular)

- Estoy logueado en Computrabajo. Home, búsqueda y detalle de oferta cargaron sin CAPTCHA ni desafío. No había scripts de Cloudflare, DataDome, PerimeterX, reCAPTCHA ni hCaptcha, solo analítica (GTM, Clarity, Piano) y telemetría propia (`api-collector.computrabajo.com`).
- Búsqueda: `https://ar.computrabajo.com/trabajo-de-{keyword}?p={n}`. Cada oferta es un `article[data-id]` con link al detalle. El detalle está renderizado en HTML y el texto completo es legible.
- El botón "Postularme" del detalle es un `<a>` **sin `href`**, con `data-href-offer-apply` (URL de postulación) y `data-href-access` (login). Un JS navega a `https://candidato.ar.computrabajo.com/candidate/apply/?oi=<32 hex>&...`. Hay que tratar esa navegación como una **acción que cambia estado**.
- Mi CV adjunto se administra en `https://candidato.ar.computrabajo.com/candidate/cv/uploadcv`: lista de PDF/Word, un radio "por defecto" que decide cuál se adjunta, opción "No adjuntar ningún CV", botón "Nuevo CV en Word/PDF" y ícono de borrar. Hoy hay un solo CV adjunto, y es de **Ventas**, mientras yo busco rol de programador.
- Mi perfil interno de Computrabajo (`/candidate/cv/edit/`) está al 76% y **sin experiencia laboral ni habilidades cargadas**.

## 3. Cosas que NO sé y debés verificar en la Fase 0

- Qué pasa exactamente al postular: ¿un clic y listo, o aparecen preguntas de filtro o más pantallas?
- ¿El CV adjunto es siempre el "por defecto" o se puede elegir al postular?
- ¿Hay CAPTCHA, límites diarios o bloqueos después de N postulaciones?
- Selector estable de la descripción en la página de detalle.
- Los Términos de Uso de Computrabajo respecto de la automatización. **Los lee el usuario, no vos**; vos solo dejás un recordatorio en el README.

## 4. Referencia: JobNavigator (solo lectura, no copiar su stack)

Repositorio en `C:\Dev\JobNavigator` (open source, Python/FastAPI/Postgres/React/Docker, demasiado pesado para este caso). Leé estos archivos antes de diseñar:

- `README.md` y `LEGAL_DISCLAIMER.md`: el flujo descubrir → filtrar → puntuar → adaptar CV → postular → trackear, y su regla de **no enviar postulaciones sin revisión humana**.
- `backend/scraper/sources/computrabajo.py`: ya descubre ofertas de Computrabajo con `httpx` + BeautifulSoup (`article[data-id]`, id de 16 hex para dedup, pausa de 0,7 s entre páginas, corta ante 403/429 sin reintentar, falla fuerte si la página 1 viene vacía). **No** trae descripción, **no** postula y **no** cambia de CV. Eso es lo que falta.
- `backend/tests/fixtures/computrabajo/` y `backend/tests/test_sources_computrabajo.py`: HTML guardado para testear el parser sin red.
- `backend/analyzer/cv_scorer.py`: puntuación con LLM contra varios CVs.
- `backend/analyzer/cover_letter_generator.py` y `backend/resume_templates/`: generación y plantillas; el PDF se exporta con Playwright.
- `backend/scraper/_shared/dedup.py` y `filters.py`: dedup y filtros por título/empresa.
- `extension/content_autofill.js`: patrón "overlay, nunca inyectar": el borrador del LLM se muestra aparte y el campo real no se toca hasta que el usuario acepta.
- `backend/api/routes_applications.py`: estados de una postulación (`applied`, `interview`, `offer`, `rejected`).

## 5. Alcance y stack (mínimo)

- Python 3.12, Windows. `httpx` + `beautifulsoup4` para listados y detalle. `playwright` solo para el tramo logueado (postular y cambiar CV). `sqlite3` de la stdlib. `jinja2` + Playwright para renderizar CVs a PDF. SDK de Anthropic para puntuar y adaptar (modelo barato para puntuar, uno mejor para adaptar CVs). CLI con `argparse`.
- Sin Docker, sin Postgres, sin frontend web, sin extensión de Chrome. La revisión es por terminal.
- Una sola fuente (Computrabajo AR). No generalizar a otras bolsas.

## 6. Reglas duras

1. **Aprobación humana por postulación.** Nada se envía sin que yo vea oferta, puntaje, CV elegido y respuestas, y confirme esa postulación puntual.
2. **Modo seco por defecto.** Todo lo que cambie estado (navegar a `candidate/apply`, cambiar el CV por defecto, subir un archivo) exige `--live` más la confirmación del punto 1.
3. **Sin evasión.** Si aparece CAPTCHA, un 403/429, una pantalla desconocida o un cambio de layout, **parás y me avisás**. No resolvés CAPTCHA, no rotás IP ni User-Agent, no reintentás un 429.
4. **Ritmo humano.** Pausas con variación entre páginas (mínimo 0,7 s) y entre postulaciones (varios segundos), y tope diario configurable (default 10).
5. **Credenciales.** Yo me logueo a mano una vez en un perfil persistente de Playwright. Nunca leés, guardás ni escribís mi contraseña. El perfil va en `.gitignore`.
6. **Cero invención.** El contenido de los CVs sale solo de `profile.yaml`, con los datos reales que yo te dé. Si falta un dato, **me lo pedís**. El LLM solo puede reordenar, reformular y priorizar lo que ya está, y yo reviso el diff antes de usar cualquier CV.
7. **Sin spam.** No postular a ofertas con puntaje bajo, repetidas ni ya postuladas (revisar la base local y "Mis postulaciones").
8. **Datos personales.** Mi email, teléfono y CVs no se loguean ni se suben al repo.

## 7. Fases (cada una termina con tests o una demo que yo pueda ver)

**Fase 0, spike supervisado (yo presente).** Proponé un guion y hacelo conmigo con **una** oferta que yo elija y con mi OK explícito para esa postulación. Respondé las preguntas de la sección 3. Entregable: `docs/FINDINGS.md` con pasos, selectores y comportamiento observado. Si el flujo exige algo que rompa las reglas de la sección 6, paramos y lo hablamos.

**Fase 1, descubrimiento.** Portá la lógica de `computrabajo.py` y agregá la descarga del detalle para obtener la descripción. Dedup por id de 16 hex. Persistencia en SQLite. Tests con HTML guardado (nada de red en los tests). Comando: `compuapply scan --keyword "programador" --pages 3`.

**Fase 2, filtro y puntaje.** Filtros por título y empresa, ubicación y modalidad. Puntaje 0 a 100 con razones, contra cada variante de CV, quedándose con la mejor. Cache por oferta y registro de costo. Comando: `compuapply score`.

**Fase 3, CVs por rol.** `profile.yaml` con mis datos reales, 2 o 3 variantes de rol (no una por oferta), plantillas Jinja, PDF con Playwright. La adaptación con LLM pasa por revisión de diff. Además, un asistente para completar mi perfil interno de Computrabajo (experiencia y habilidades), siempre con mi aprobación. Comando: `compuapply cv build`.

**Fase 4, cola de revisión y postulación.** Cola en terminal: oferta, puntaje, razones, CV propuesto, extracto de la descripción. Opciones: aprobar, saltar, salir. Al aprobar: asegurar que el CV por defecto sea el de la variante (verificándolo en `uploadcv`), postular, completar preguntas con borrador de LLM que yo edito, confirmación final, verificar que la oferta quedó como postulada y registrar. Comando: `compuapply review --live`.

**Fase 5, seguimiento.** Estados por postulación, notas y un reporte de embudo (vistas, postuladas, respuestas). Leer Gmail para detectar respuestas queda como mejora futura, inspirada en `email_monitor` de JobNavigator.

**Fase 6, robustez.** Detección de cambio de layout (fallar fuerte, como hace JobNavigator), logs claros en español y un README con el aviso sobre términos de uso y riesgo de bloqueo de cuenta.

## 8. Cómo trabajar

1. Leé la referencia (sección 4) y **no escribas código todavía**.
2. Respondeme con: estructura de carpetas propuesta, esquema de la base, el guion de la Fase 0 y la lista de datos que necesitás de mí para `profile.yaml`.
3. Esperá mi OK. Después avanzá fase por fase y frená al final de cada una con una demo corta.
4. Preferí el código más chico que funcione. Nada de abstracciones, dependencias ni configuración que no se necesiten hoy.
