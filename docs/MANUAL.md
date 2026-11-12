# compuapply

## 1. Qué hace

Busca ofertas en Computrabajo Argentina y las puntúa contra tus CVs con OpenAI.
Después elige a cuáles postularía, según reglas estrictas que fijás vos (puntaje mínimo, sin repetidas, tope por día).
Por defecto la postulación es en modo seco: te muestra qué haría y no envía nada. Con `apply --live` (o el botón «Postular (en vivo)») postula de verdad.

## 2. Aviso

Automatizar un sitio puede ir contra los términos de uso de Computrabajo y puede causar el bloqueo de tu cuenta.
El riesgo es tuyo: leé los términos antes de usar el programa.

El programa no evade defensas. Si aparece un CAPTCHA, un bloqueo o una pantalla que no reconoce, frena y te avisa.

## 3. Requisitos

- Windows y Python 3.13.
- Una clave de OpenAI, con un límite de gasto puesto en platform.openai.com.

## 4. Instalación

En la carpeta del proyecto, en PowerShell o CMD:

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

## 5. Configuración

Todos los comandos se corren desde la carpeta del proyecto.

**1. La clave de OpenAI.**

```bash
python -m compuapply setup env
```

Te pide la clave sin mostrarla (pegala y apretá Enter), comprueba gratis que sirve y que tu cuenta tiene acceso al modelo, y recién ahí crea el archivo `.env`.
La clave nunca se imprime. Si `.env` ya tiene una clave, no hace nada. En la interfaz es el botón «Cargar clave» (sección 6).

*A mano:* creá un archivo `.env` con estas dos líneas. `OPENAI_MODEL` es opcional: si no está, se usa `gpt-6-luna`.

```
OPENAI_API_KEY=pegá-acá-tu-clave
OPENAI_MODEL=gpt-6-luna
```

Modelos que acepta el programa: `gpt-6-luna`, `gpt-5-nano` (de razonamiento: se les pide esfuerzo bajo) y `gpt-4o-mini` (no es de razonamiento: anda sin eso).
Si OpenAI rechaza un pedido, el mensaje trae el código y el parámetro del error (por ejemplo «unsupported_parameter en reasoning.effort»), nunca el texto que devuelve la API.
Los montos se muestran con coma decimal («US$ 0,0004»).

**2. Tu perfil.** Tus CVs tienen que estar en formato `plain_text_resume.yaml` (el de AIHawk), cada uno en su propia subcarpeta de una carpeta común.
Primero mirá cuáles hay:

```bash
python -m compuapply setup profile --cv-dir C:\CVs
```

Lista los nombres de las subcarpetas que tienen un `plain_text_resume.yaml`. Elegí de 1 a 3 (una por rol) y creá el perfil:

```bash
python -m compuapply setup profile --cv-dir C:\CVs --variants backend,data
```

Crea `profile.yaml` (nunca pisa uno que ya exista), lo valida y muestra una línea por variante.
Después abrilo y completá las `keywords` de cada variante. El `pdf` lo podés dejar para más adelante.

*A mano:* copiá el ejemplo y completalo con tus datos:

```bash
copy profile.example.yaml profile.yaml
```

En `profile.yaml` van:

- `variants`: de 1 a 3 variantes de CV, cada una con:
  - `id`: un nombre corto, sin repetir.
  - `resume`: tu CV en formato `plain_text_resume.yaml` (el de AIHawk). **Qué secciones se usan:** al modelo del puntaje le llegan `experience_details`, `technical_skills` (una lista de textos; también se aceptan mapas `{nombre: nivel}` o `{categoría: [textos]}`), `projects`, `education_details`, `certifications`, `languages`, `interests` y `work_preferences`, nunca `personal_information` ni links. El CV a medida (`cv make`) elige entre experiencia, conocimientos técnicos, proyectos, logros, certificaciones e intereses, y siempre muestra tus datos personales, experiencias, estudios e idiomas.
  - `default_pdf_name` (opcional): el nombre del CV base de esa variante tal como aparece en la página de CV de Computrabajo (`uploadcv`). Con él, `apply --live` vuelve a marcar ese CV como el por defecto al terminar (ver «Postular de verdad»).
  - `pdf` (opcional): el PDF base de esa variante. `apply --live` no lo sube: adjunta el CV a medida de cada oferta (ver «Postular de verdad»).
  - `keywords`: palabras del rol (opcional).
- `exclude_companies` y `exclude_title_words`: empresas y palabras del título que descartan una oferta (opcionales).
- `zones` (opcional): las provincias a las que limitás las ofertas, escritas como las muestra Computrabajo (por ejemplo `Capital Federal` y `Buenos Aires-GBA`; en Parámetros, un cuadro «Zonas» con una por línea). La provincia de una oferta es lo que viene después de la última coma de su lugar («Villa Martelli, Buenos Aires-GBA» → `Buenos Aires-GBA`), sin importar mayúsculas, acentos ni espacios de más; una oferta sin lugar queda fuera. Con `zones`, las ofertas de otras provincias **no gastan nada**: no se les baja el detalle (tampoco en `scan`), no se puntúan, Calibrar no las ofrece y el plan las salta con «fuera de zona (<provincia>)», también las que ya tenían puntaje. El informe del plan y el de `run` dicen cuántas ofertas nuevas quedaron fuera. Sin `zones`, o con la lista vacía, no se filtra nada. Tiene que ser una lista: `zones: Capital Federal` (un texto) da un error.
- **Resultados y Calibrar en zona:** con `zones`, la pestaña Resultados lista solo las ofertas de esas provincias y dice cuántas quedaron afuera («N fuera de zona (no se muestran)»); Calibrar solo ofrece ofertas en zona, y `review threshold` (y «Calcular umbral») cuenta solo las etiquetas de ofertas en zona (`review threshold --profile` lee solo `zones`). Las etiquetas viejas de otras zonas no se borran. Sin `zones`, todo entra como siempre.
- `apply`: `threshold` (puntaje mínimo de 0 a 100, por defecto 75), `daily_cap` (máximo de postulaciones por día, por defecto 10), `company_daily_cap` (máximo de postulaciones por día a la misma empresa, por defecto 1: el nombre se compara sin mayúsculas, acentos ni espacios de más, las confidenciales no cuentan entre sí, y el plan salta las demás con «tope por empresa»; en Parámetros es «Tope diario por empresa») y `review_cv` (`true` por defecto: mientras sea `true`, `apply --live` no sube ningún CV a medida; ponelo en `false`, o desmarcá «Revisar cada CV antes de subirlo» en Parámetros, cuando quieras que el programa los suba sin que los revises antes).
- `answers` (opcional): tus respuestas para las preguntas de selección al postular. Cuatro claves fijas, que cargás en Parámetros → Respuestas: `salary` (sueldo pretendido, texto), `availability` (disponibilidad), `modality` (las modalidades que aceptás: una o más de `presencial`, `híbrido` y `remoto`, separadas por coma, barra, punto y coma, « y » u « o », sin importar mayúsculas ni acentos: «Presencial/Hibrido/Remoto» se guarda como «presencial, híbrido, remoto»; ante una pregunta por una modalidad de la lista el programa contesta sí, por una que no está, no, y con `modality` no contesta zona, traslado ni horario; si escribís otra cosa, Parámetros no guarda y dice «una o más de presencial, híbrido o remoto») y `start` (cuándo podés empezar); y respuestas a preguntas puntuales, con la pregunta sin mayúsculas, acentos ni signos como clave (las guarda la pestaña Preguntas). Son datos tuyos: no van al código ni a los tests.
- `run` (opcional): los valores por defecto del comando `run` y de la interfaz: `pages` (páginas por keyword, 2), `max_details` (detalles a bajar, 20), `score_limit` (ofertas a puntuar, 20), `max_cost` (gasto máximo de la corrida en USD, 0.50) y `max_cost_month` (tope de gasto en OpenAI por mes en USD, 2.00; ver «Frenos»). Un flag de la terminal gana sobre el perfil. Aparte, `max_account_pdfs` (por defecto 10, el límite de Computrabajo): con más PDFs que estos en la cuenta, `apply --live` no postula, y con tantos libera un lugar antes de subir (ver «CVs con el nombre de la postulación»). `apply.free_cv_slots` (`true` por defecto) apagado hace que frene en vez de borrar.

Las rutas de `resume` y `pdf` pueden ser absolutas o relativas a la carpeta de `profile.yaml`.
Guardá tus CVs reales en la carpeta `cv/`: git no la sube.

El programa no genera los PDF. Los hacés vos y **revisás cada uno una vez** antes de usarlo.
Mientras una variante no tenga `pdf`, `profile check` lo marca con «pdf: falta» y los informes de `apply` y `run` lo avisan en el encabezado.

Cuando termines, comprobá que todo carga bien:

```bash
python -m compuapply profile check
```

Imprime una línea por variante (cuántas experiencias y proyectos tiene, y el tamaño del PDF o «pdf: falta») y no muestra datos de tu CV.

## 6. Interfaz

Para no usar la terminal, hacé doble clic en `Iniciar.bat` (o corré `python -m compuapply.gui`). Se abre una ventana con cinco pestañas:

- **Estado:** una fila por punto, con ✔ o ✘ y su botón. La clave de OpenAI («Cargar clave» la pide sin mostrarla, la verifica y la guarda en `.env`; con la clave cargada, el botón pasa a «Reemplazar clave»: la nueva se verifica gratis y solo si anda reemplaza a la vieja, y si no, la vieja queda como estaba), el modelo, el perfil, el PDF de cada variante, el gasto del mes («Gasto del mes: US$ X de US$ Y», con ✘ cuando se llegó al tope mensual), la prueba de puntaje («Prueba de puntaje: sin hacer», o ✔ con el puntaje y el costo, o ✘ con el motivo; el botón «Probar el puntaje (menos de US$ 0,001)» queda activo solo con la clave y el perfil en ✔) y la sesión de Computrabajo: «Verificar» la comprueba e «Iniciar sesión» abre Chrome, donde iniciás sesión a mano y cerrás la ventana (con Chromium, sin Chrome instalado, tocás «Ya inicié sesión»). «Crear acceso directo en el escritorio» deja un `compuapply.lnk` que abre la ventana sin pasar por la terminal (si ya existía, lo reemplaza y lo avisa).
- **Parámetros:** el formulario de `profile.yaml` (variantes con su CV, keywords y PDF; empresas y palabras excluidas; `apply` y `run`). «Guardar» valida todo y recién ahí reemplaza el archivo: si hay un error, lo muestra y no toca nada. Si todavía no tenés `profile.yaml`, «Crear desde una carpeta de CV» lo arma con hasta 3 de tus CVs.
- **Correr:** «Corrida completa (modo seco)», «Puntuar pendientes» y «Plan de postulación», con el registro en vivo. Corre una acción a la vez. «Detener» crea el archivo `STOP` (el programa frena en el próximo control); si a los 30 segundos sigue en marcha, el botón pasa a «Forzar cierre». Al terminar dice «Terminó bien» o «Frenó: mirá el registro», y «Abrir informe» abre el informe de la corrida. Abajo está la **Corrida programada**: escribís la hora (`HH:MM`) y «Programar» deja una corrida diaria de `run` en modo seco; «Quitar» la saca (si no había ninguna, lo avisa). Muestra si hay una programada y cómo le fue la última (por ejemplo «Todos los días a las 09:00 · última: 03/11 09:00, terminó bien · próxima: 04/11 09:00»). No postula. La PC tiene que estar prendida y con tu sesión de Windows abierta; si estaba apagada, corre apenas se prende. «Programar» se niega, con el motivo, si el perfil no está en ✔ o si falta la clave y `run.score_limit` es mayor que 0 (con `score_limit` en 0, la corrida busca sin clave).
- **Calibrar:** lo mismo que `review label` y `review threshold`, con ventana. Te muestra de a una, en orden al azar, las ofertas puntuadas que todavía no etiquetaste: título, empresa, ubicación, tipo y los primeros 800 caracteres de la descripción, con «Abrir la oferta» para verla en el navegador. **No muestra el puntaje ni las razones del modelo**, para que tu respuesta no dependa de lo que opinó él. Contestá «Sí, me postularía», «No» o «Saltar» (o con las teclas `s`, `n` y espacio); cada respuesta se guarda al instante. Con 20 etiquetas o más, «Calcular umbral» muestra, para cada umbral de 30 a 95, cuántas ofertas enviaría, la precisión y la cobertura, y sugiere el más bajo con al menos 80% de precisión; «Usar el umbral sugerido» lo escribe como `apply.threshold` en `profile.yaml`, junto con `apply.calibrated_with` (con qué modelo y versión de prompt calibraste, por ejemplo `gpt-4o-mini v2`). Calibrar usa los mismos puntajes con los que decide el plan: los del modelo en uso y la versión de prompt actual, sin ofertas duplicadas. Si tenés etiquetas de ofertas que no tienen ese puntaje, lo avisa («N etiquetas sin puntaje de <modelo> <versión>: no cuentan para el umbral. Corré el puntaje primero.»).
- **Resultados:** la tabla de las ofertas puntuadas con el modelo en uso y la versión de prompt actual, sin duplicadas (de mayor a menor) con su variante, estado y fecha. «Solo las que pasan el umbral» filtra con el `threshold` de tu perfil. Al elegir una fila ves sus razones y lo que le falta; con doble clic se abre la oferta; «Armar CV» y «Abrir CV» trabajan con la fila elegida (ver «CV a medida de una oferta»). Con menos de 20 etiquetas aparece primero un aviso (ver los puntajes antes de calibrar sesga tus respuestas) y el botón «Mostrar igual». Debajo están los informes de `data/runs/` (el más nuevo primero, con «Abrir» y «Abrir carpeta») y el gasto en OpenAI de este mes y total. Es una estimación con la tabla de precios del programa y el mes se cuenta en UTC, como factura OpenAI: el gasto real está en platform.openai.com.

La interfaz no reimplementa nada: cada botón corre el mismo comando de la terminal (por eso comparten `profile.yaml` y `data/`). Todo es modo seco salvo el botón «Postular (en vivo)», que pide confirmación. La clave no se muestra nunca, ni en pantalla ni en el registro, y la interfaz no muestra el contenido de tus CVs.
Si Windows no encuentra `pythonw`, `Iniciar.bat` lo avisa: reinstalá Python marcando «Add python.exe to PATH».

## 7. Uso

Seguí este orden. Todo se guarda en la carpeta `data/`. Para ver las opciones de cualquier comando, agregale `--help`.

**Antes de empezar: probar el puntaje.** Con la clave cargada y el perfil armado (secciones 5 y 6), comprobá que el pedido real a OpenAI funciona antes de la primera corrida:

```bash
python -m compuapply score --probe
```

Puntúa una oferta de ejemplo fija ("Desarrollador/a Frontend React", inventada) contra tus variantes, con el mismo pedido que usa `score`, por menos de US$ 0,001. Dice «Prueba OK con <modelo>: puntaje N para <variante> · tokens: ... · costo: US$ X», o el motivo del error y sale con 1. No lee ni escribe la base. En la interfaz es la fila «Prueba de puntaje» de Estado, con el botón «Probar el puntaje (menos de US$ 0,001)» (activo con la clave y el perfil en ✔).

**1. Iniciar sesión (una sola vez).**

```bash
python -m compuapply login
```

Se abre **Google Chrome** (el instalado en `Program Files`, `Program Files (x86)` o `%LOCALAPPDATA%`) como un programa común, sin Playwright ni opciones de automatización: así Google deja iniciar sesión (con Google o con tu mail). Es otra instancia, con un perfil propio del programa en `.browser-profile/chrome/` y **sin extensiones** (`--disable-extensions`: ninguna extensión de otro programa de la PC corre en esa ventana); tu Chrome personal, su perfil y sus cookies no se tocan ni se leen.
Iniciá sesión en Computrabajo, tildá «Recordarme» si aparece y **cerrá esa ventana de Chrome**. El programa espera a que se cierre y recién ahí verifica la sesión (con Chrome abierto no se puede) y avisa «Sesión iniciada». No hace falta apretar Enter. En la interfaz, el diálogo dice lo mismo; si tocás «Ya inicié sesión» con Chrome todavía abierto, te pide que lo cierres.
Si Chrome no está instalado, usa Chromium (`python -m playwright install chromium`) con el perfil `.browser-profile/`, lo dice, y se inicia sesión como antes: ventana de Chromium y Enter.
El programa nunca lee tu contraseña.

**2. Comprobar que la sesión sigue activa.**

```bash
python -m compuapply session check
```

Abre la ventana, mira una página de tu cuenta sin cambiar nada y dice si la sesión está activa o vencida.

**3. Buscar ofertas.**

```bash
python -m compuapply scan --keyword programador --pages 3 --max-details 20
```

Lee hasta 3 páginas del listado, guarda las ofertas nuevas y baja la descripción de hasta 20 ofertas pendientes.
Con `--max-details 0` no baja descripciones.
**Reparto de detalles:** el límite se reparte entre las keywords que tienen ofertas pendientes, por turnos: una oferta de cada keyword por vuelta, y dentro de cada keyword la más nueva primero (así, con muchas búsquedas, los detalles no se agotan en las primeras). `run` dice «sin detalle todavía: N» (en la terminal y en el informe) cuando quedan ofertas sin detalle.

**4. Puntuar.**

```bash
python -m compuapply score --limit 20 --max-cost 0.50
```

Descarta las ofertas que coinciden con tus exclusiones y puntúa de 0 a 100 hasta 20 ofertas contra tus variantes de CV. **Un pedido por variante:** cada pedido a OpenAI lleva el CV de una sola variante (con varios CVs en el mismo pedido el modelo los mezclaba con la oferta y inflaba el puntaje). Se puntúan las variantes que tienen entre sus `keywords` la keyword con la que se encontró la oferta (sin distinguir mayúsculas ni acentos); si ninguna la tiene (la keyword ya no está en el perfil, o `scan --keyword`), se puntúan todas. Se guarda el puntaje más alto, con su variante, sus razones, faltantes y requisitos que el CV no muestra; el puntaje de cada variante pedida queda en `all_scores`, y el costo suma todos los pedidos de la oferta. El tope de gasto se controla antes de cada pedido: una oferta cortada a mitad queda sin puntaje (sigue en la cola) y lo que costaron sus pedidos pagos se anota en el gasto del mes. `score --probe` hace un pedido por variante y muestra el puntaje de cada una.
Se detiene si el gasto de la corrida llega a 0,50 dólares, o antes si lo que queda del tope mensual (`run.max_cost_month`, ver «Frenos») es menos. Al final muestra «Gasto del mes: US$ X de US$ Y». El costo que muestra es una estimación con la tabla de precios del programa: el gasto real está en platform.openai.com.
Si alguna oferta quedó con error, `python -m compuapply score --retry-errors` la vuelve a puntuar.
**Ofertas duplicadas:** si la misma oferta se publicó varias veces (misma empresa, mismo título y descripción idéntica; la empresa se compara sin mayúsculas ni espacios de más), se puntúa y se postula una sola vez: la más vieja queda como original y las otras pasan a `duplicate` (con `dup_of` apuntando a la original). No se puntúan, no entran en el plan, en Calibrar ni en Resultados, y el resumen dice «duplicadas: N» cuando las hay. Mismo título y empresa pero otra descripción no es una duplicada. Si la original desaparece o cambia, la duplicada vuelve a la cola.
**Requisitos que el CV no muestra:** el puntaje también guarda qué requisitos de la oferta no muestra tu CV (el modelo prioriza los que la oferta marca como excluyentes, obligatorios o «requisito indispensable», pero la lista puede traer más: tomala como una ayuda para revisar, no como un veredicto). **No cambia el puntaje ni frena nada**: se postula igual si el puntaje pasa el umbral; la lista se ve en Resultados (al elegir una fila), en el CSV de `review export` (columna `excluyentes`, rotulada «Requisitos que el CV no muestra») y sirve para revisar el CV a medida.
**Puntaje estable:** con los modelos sin razonamiento (`gpt-4o-mini`) el pedido lleva `temperature` 0, así el mismo texto da siempre el mismo puntaje. Los puntajes de antes (versiones de prompt `v1`, con azar, `v2`, sin tus conocimientos técnicos, `v3`, sin la lista de excluyentes, y `v4`, con todos los CVs en el mismo pedido) no se reutilizan: las ofertas se vuelven a puntuar y el plan solo usa los nuevos (`v5`).

**Banco de verdades (opcional).** Si hay algo cierto que tu CV no dice (por ejemplo, que usaste una tecnología en la facultad), escribilo vos en un YAML con una lista de textos cortos (ver `examples/truths.example.yaml`) y apuntalo desde la variante de `profile.yaml` con `truths: ruta/al/archivo.yaml`. Esas líneas valen como hechos de tu CV: el CV a medida puede usarlas como fuente y sus palabras pasan la verificación. Escribí solo lo que sea cierto: el programa no lo comprueba ni lo escribe, y la interfaz conserva el campo al guardar Parámetros.

**Banco propuesto por el programa (pestaña Banco).** No hace falta que escribas el banco: elegí la variante y tocá «Proponer». El programa lee tu CV base (sin datos personales ni links) y propone hechos que se siguen de lo que hiciste (por ejemplo, «usé React» ⇒ «usé JavaScript y npm»; una tarea que demuestra una habilidad blanda; lo que cubre una materia o una certificación): solo deducciones directas, en primera persona, sin años, números ni niveles. Cada propuesta muestra su motivo y la línea de tu CV de donde sale. **Aprobar es declarar que es cierto**: «Aprobar» o «Aprobar todas» pasan el texto al banco (`cv/truths-<variante>.yaml`, y si la variante no declara `truths` en `profile.yaml` se lo agrega, sin tocar lo demás); «Rechazar» la borra y no se vuelve a proponer (queda en `rechazadas` de `cv/truths-<variante>.propuestas.yaml`). Más abajo está el banco aprobado, con «Quitar» y un campo para agregar un hecho escrito por vos. Nada se aprueba solo. Cuesta centavos, se anota en el gasto del mes (`api_spend`, `kind = 'truths'`) y no se pide con el tope mensual lleno. Por consola: `python -m compuapply cv truths propose --variant <id>` (imprime cuántas propuso y el costo, nunca el texto) y `python -m compuapply cv truths list --variant <id>`.

**CV a medida de una oferta (solo para revisar).**

```bash
python -m compuapply cv make --id <id16>      # una oferta puntuada (acepta «ct-» adelante)
python -m compuapply cv make --top 3          # las 3 mejores ofertas nuevas que todavía no tienen CV (--max-cost 0.05 por defecto)
```

Arma un CV con la variante que mejor le encaja a la oferta y deja `data/cvs/<id16>-<variante>.html` y `.pdf`. **Se adapta a la oferta sin agregar hechos:** el modelo escribe un título (hasta 60 caracteres), un perfil (hasta 350) y de 1 a 5 viñetas (hasta 200 cada una) por experiencia relevante, reescritas con el vocabulario y las tareas de la oferta (no copias de tu CV), y cada viñeta indica de qué línea de tu CV (o de tu banco de verdades) sale; también elige y ordena por id conocimientos técnicos, proyectos, logros, certificaciones e intereses. El programa rechaza cualquier línea con una sigla, tecnología, número o palabra con mayúscula que no figure en tu CV base (todas las secciones menos los datos personales) ni en tu banco de verdades, y cualquier nivel (semi, senior, líder, jefe…) que no esté en algún puesto o estudio tuyo; junior, trainee, pasante, practicante y estudiante valen siempre. El control es justo con las palabras ciertas: «Desarrollador/a» y «y/o» valen si cada parte está en tu CV (sin contar las terminaciones a, o, as, os, es), «APIs» vale si está «API», «IA» vale si tu CV dice «Inteligencia Artificial» (las iniciales de 2 a 5 palabras seguidas de un mismo item, sin cruzar coma, punto, dos puntos, punto y coma ni salto de línea), y una mayúscula inicial sola («Automatización» en un título) vale si la palabra aparece en minúsculas en la oferta o en tu CV; en el título valen además las palabras de puesto del título de la oferta (desarrollador, developer, analista, soporte, atención al cliente… una lista fija) aunque tu CV no las tenga, pero la primera palabra del título también se controla (así «Python Developer» sin Python en tu CV se rechaza); si el modelo escribe ids del CV (E1.S1, T22…) dentro del título, el perfil o una viñeta, el programa los saca antes de controlar; siguen rechazadas las tecnologías con mayúscula que tu CV no tiene (Python, Docker…), las siglas, los números y los puntos o símbolos (Node.js, C#). Si una línea no pasa, o si todas las viñetas salen copiadas de tu CV, repite el pedido una vez con los motivos; si después del reintento fallan solo algunas líneas, reemplaza esas líneas y conserva el resto de la adaptación (el título inválido pasa a ser el puesto de tu experiencia relevante más reciente, tal cual está en tu CV, o queda sin título si no hay ninguna; el perfil inválido se saca; una viñeta inválida pasa a ser el texto de la línea de tu CV de donde sale, marcada «copia», o se saca si esa línea ya está en otra viñeta de la experiencia) y dice cuáles y por qué en la consola, en la ventana y en la revisión; si falla la estructura de la respuesta (ids que no existen, fuentes de otra experiencia, viñetas de más), arma el CV solo con ids de tu CV base (sin texto adaptado) y dice por qué; si la segunda respuesta sigue copiando pero no tiene otro problema, se usa igual (copiar no inventa nada) y la revisión lo avisa. Empresas, puestos, fechas, estudios e idiomas salen siempre de tu CV base, tal cual. **Dos bloques de experiencia:** el modelo marca cada experiencia como «relevante» (sirve para el puesto de la oferta) u «otra». El CV muestra primero «Experiencia relevante», con el puesto, la empresa y el período, las responsabilidades y las habilidades elegidas, y después «Otra experiencia», con el puesto y la meta y, si el modelo encontró una habilidad transferible para la oferta, una sola viñeta (con el mismo control y la fuente en esa experiencia); cada bloque en el orden de tu CV base. Todas tus experiencias salen siempre: la que el modelo no nombra, o marca «relevante» sin responsabilidades, va a «otra»; si ninguna es relevante, todas salen bajo «Experiencia». Estudios, idiomas y datos personales también salen siempre, completos. Dentro de cada bloque las experiencias van de la más nueva a la más vieja (por la fecha de inicio de `employment_period`; sin fecha legible, al final y en el orden de tu CV base) y los estudios por `year_of_completion`, de más nuevo a más viejo; un estudio que termina en un año futuro dice « (en curso)» y los niveles de idioma salen en español (Native → Nativo, Fluent → Fluido, …). Tu CV base no se modifica. Al modelo no le llega ningún dato personal (ni nombre, ni email, ni teléfono, ni dirección, ni links). La oferta tiene que tener puntaje del modelo en uso y no ser duplicada; cuenta para el tope mensual (el costo sale en «Gasto del mes»): cada respuesta de OpenAI se paga y se anota (tabla `api_spend`), sirva o no, y rearmar un CV suma otro costo.
**Cómo revisarlo:** junto al PDF queda `data/cvs/<id16>-<variante>.revision.html`: cada línea generada junto a la línea de tu CV de donde sale (las viñetas marcadas «adaptada» o «copia»), el título y el perfil con sus fuentes, los requisitos que el CV no muestra y, si el CV salió solo con ids, el motivo. Leé cada línea contra su fuente y abrí el PDF; si no te gusta, volvé a correr el mismo comando para armar otro. El PDF se genera con Chromium en una ventana oculta, sin tu sesión de Computrabajo, y no se sube a ningún lado. En la interfaz: pestaña Resultados, elegí una fila y «Armar CV»; «Abrir CV» lo abre y «Abrir revisión» abre la página de revisión.

**5. Elegir el puntaje mínimo con tus propias respuestas.**

En la interfaz es la pestaña Calibrar (sección 6). En la terminal:

```bash
python -m compuapply review label --limit 30
```

Te muestra, de a una y en orden al azar, ofertas ya puntuadas: título, empresa, ubicación, tipo de contrato, los primeros 800 caracteres de la descripción y el link.
**No te muestra el puntaje ni las razones del modelo**, para que tu respuesta no dependa de lo que opinó él.
Para cada una contestá `s` (sí te postularías) o `n` (no). Enter la salta y `q` (o Ctrl+C) termina. Cada respuesta se guarda al instante, así que podés cortar cuando quieras y seguir otro día: las ya etiquetadas no vuelven a salir.
Etiquetá al menos 20 ofertas en total.

```bash
python -m compuapply review threshold
```

Con tus etiquetas muestra, para cada umbral de 30 a 95, cuántas ofertas enviaría, qué porcentaje de ellas habías marcado con `s` (precisión) y qué porcentaje de tus `s` alcanzaría (cobertura).
Sugiere el umbral más bajo con al menos 80% de precisión.
Poné ese número como `threshold` en `profile.yaml` (la ventana también anota `calibrated_with`). Cuenta solo los puntajes del modelo en uso (`OPENAI_MODEL`) y de la versión de prompt actual, sin duplicadas, y avisa si hay etiquetas sin ese puntaje.

**Umbral con origen (`calibrated_with`).** `apply.calibrated_with: "gpt-4o-mini v5"` dice con qué modelo y versión se calibró el umbral. Si falta, o no coincide con el modelo y la versión que se usan ahora, `apply` y `run` lo avisan en la terminal y en el informe, y Estado lo marca con ✘: un umbral calibrado con otra escala puede postular de más o de menos. Guardar Parámetros con el mismo umbral conserva `calibrated_with`; con otro umbral lo borra, porque ya no viene de la calibración.

*Alternativa con Excel (muestra el puntaje, así que tiende a darle la razón al modelo):*

```bash
python -m compuapply review export
```

Crea `data/review.csv` con las ofertas puntuadas con el modelo en uso y la versión de prompt actual (sin duplicadas), de mayor a menor puntaje.

1. Abrilo en Excel y completá la columna `postularia` con `s` o `n`. Etiquetá al menos 20 ofertas.
2. No cambies la columna `id16`.
3. Guardalo con el mismo nombre como **CSV UTF-8**. Si lo guardás como "CSV" a secas, o con comas en vez de punto y coma, el programa te lo va a decir.

Para calcular el umbral con ese archivo en vez de con la base: `python -m compuapply review threshold --csv data/review.csv`.
Si volvés a correr `review export`, tus etiquetas del CSV se conservan.

**6. Ver a qué ofertas postularía (modo seco).**

```bash
python -m compuapply apply
```

No envía nada y no toca Computrabajo. Muestra cuántas postularía y cuántas saltaría, con el motivo, y guarda el informe en `data/runs/AAAAMMDD-HHMMSS-apply-seco.md`. Si todavía no hay ofertas puntuadas con el modelo en uso, lo avisa («Todavía no hay ofertas puntuadas con <modelo>: puntuá primero ...»), también en `run`.
**Postular de verdad (`apply --live`).**

```bash
python -m compuapply apply --live
```

Postula a las ofertas que el plan manda a «Enviaría», una por una, con el navegador **visible** (Chrome con el perfil de `.browser-profile/chrome/`, o Chromium con `.browser-profile/` si Chrome no está instalado: hace falta haber iniciado sesión con `login`; si la sesión venció, sale con un error y no postula nada). Por cada oferta abre el detalle; si ya dice «Ya aplicaste» (a la vista), la marca como postulada y sigue. Si no, toca «Postularme». Si el sitio muestra preguntas de selección, el programa las contesta **solo con datos de tu CV o con tus `answers`** (un pedido a OpenAI con el CV sin datos personales, tus respuestas y las preguntas; las cifras, como el sueldo, solo pueden salir de tus `answers`) y envía; el CV adjunto es el a medida de esa oferta (ver abajo). Si una pregunta no se puede contestar con certeza (por ejemplo, el sueldo sin `salary` cargado), la oferta **no se envía**: queda «pregunta sin respuesta» y la pregunta aparece en la pestaña Preguntas; al contestarla, la oferta vuelve a la cola. Nunca inventa una respuesta.
Respeta el tope diario, el archivo `STOP` y una pausa de varios segundos entre postulaciones. **Frena toda la corrida** ante un CAPTCHA, un 403 o 429, la falta de sesión, una pantalla que no conoce, un error del navegador o 3 respuestas inválidas del modelo seguidas; lo que ya se envió queda. Cada postulación queda en la tabla `applications` (variante, fecha, preguntas y respuestas, resultado) y el informe `data/runs/AAAAMMDD-HHMMSS-apply.md` lista las enviadas y las saltadas con su motivo (sin las respuestas). Contestar preguntas cuesta centavos y cuenta en el gasto del mes (`api_spend`, `kind = 'answers'`). Sin `--live` no se abre el navegador.
**CVs con el nombre de la postulación y el límite de 10.** Cada CV se sube como `CV-<Nombre>-<Apellido>-<Puesto de la oferta>-<Empresa>.pdf` (el título y la empresa de la oferta, «Confidencial» si no tiene; sin acentos, hasta 80 caracteres, recortando primero el puesto); si la cuenta ya tiene un CV con ese nombre para otra oferta se suman los 4 últimos caracteres del `id16`, y un CV ya subido de la oferta conserva su nombre. Cada PDF que se sube también se copia a `cv/postulaciones/` con el mismo nombre y al empezar cada corrida (también en seco) se borran de esa carpeta, y solo de ella, los PDF con fecha de modificación de más de 30 días; el informe dice cuántos. Computrabajo admite 10 CVs por cuenta (`run.max_account_pdfs`, por defecto 10; «No adjuntar» no cuenta): con 10, antes de subir el programa libera un lugar borrando, en este orden, (1) una copia repetida del archivo de una oferta que no se envió (queda al menos una) o (2) el CV de una postulación del programa en «Proceso finalizado» y, si no hay, en «CV Visto» (la más vieja primero), solo si ninguna postulación en «Postulado» ni oferta pendiente usa ese archivo. Si no hay ninguno de esos, borra el CV de una postulación del programa de más de 30 días (en cualquier estado, la más vieja primero), solo si ninguna oferta pendiente usa ese archivo ni ninguna postulación de menos de 30 días. Nunca borra el CV por defecto, el `default_pdf_name`, el PDF de una variante ni un CV que no figure en la base. Hace un clic en la papelera de esa fila (la elige por su `data-filecandidateid`), espera el cuadro del sitio `#frmDelete` («¿Estás seguro que deseás eliminar este currículum?») y hace clic en su `#btDelete` visible; si el cuadro no aparece o dice otra cosa, lo cierra con `#btCancelDelete` y frena con la huella. Un `confirm` nativo, si lo hubiera, se acepta. Después recarga y exige que ese `filecandidateid` ya no esté (nunca por nombre: puede haber copias con el mismo). Si no hay ninguno que se pueda borrar, o con `apply.free_cv_slots: false`, frena con «La cuenta llegó a 10 CVs (límite de Computrabajo)…» sin borrar ni subir; si el sitio avisa «número máximo de documentos», frena con ese texto. Una pregunta abierta de sueldo, remuneración o salario se contesta con `salary` tal cual, sin pasar por el modelo. **Lugares por subida.** Antes de cada subida que no es un reuso se lee la cuenta (`uploadcv`) y, si tiene `run.max_account_pdfs` CVs, se libera un lugar; no hay cálculos acumulados, así que una corrida puede borrar y subir varios CVs. Si después de elegir el archivo la página dice «número máximo de documentos», frena con «El sitio dice que la cuenta llegó al máximo de documentos». **Verificación final.** Al terminar una corrida con envíos (también si frena después de alguno, salvo CAPTCHA, 403/429 o sin sesión) se relee «Mis postulaciones» (solo lectura) y cada enviada que no figura (por `im`, o por título y empresa) queda en el informe como «enviada, sin confirmar en Mis postulaciones»; el estado no cambia. El informe lista las preguntas pendientes con su motivo («el modelo dijo unknown» o «el control: …», sin valores) y «CVs borrados en esta corrida: N» con el nombre y el motivo de cada uno.

**CVs subidos antes de guardar el archivo.** Al empezar `apply --live`, una fila de `applications` en `cv_uploaded` o `pending_question` sin `cv_file` se completa con el nombre viejo (`CV-<N>-<A>-<titular del CV guardado>.pdf`) si la oferta tiene un CV guardado de esa variante y versión y la cuenta tiene un PDF con ese nombre; si dos ofertas dan el mismo nombre no se completa ninguna. Así el reuso y el borrado de copias las reconocen. También al empezar, una oferta en `pending_question` cuyas preguntas pendientes ya se pueden contestar (una de sueldo con `salary`, o una cuya clave normalizada está en `answers`) vuelve a la cola. El informe cuenta las filas completadas y las ofertas devueltas.

**Viñetas en estilo nominal.** El CV a medida pide viñetas y perfil con sustantivos de acción («Control y reposición de…»), sin verbos conjugados en primera persona, y un solo tiempo verbal si hace falta un verbo; el código marca como problema una viñeta que mezcla pretérito en «é» con imperfecto en «aba»/«ía» («Controlé y reponía»), sin contar sustantivos como «categoría» o «mensajería», y sigue el mismo camino que los demás controles (un reintento y, si sigue mal, la línea se reemplaza por la del CV base).

**Con el CV a medida de cada oferta.** El programa **nunca postula para comprobar algo**. Al empezar lee una sola vez «Mis postulaciones» (`candidato.ar.computrabajo.com/candidate/match/`, hasta 3 páginas; solo lectura): de cada tarjeta toma el título (`h1`), la empresa (`p.fc_base.mt5`), el lugar (`p.fc_base` sin `mt5`) y el estado (`p.fwB`); las ofertas viejas o cerradas no traen empresa ni lugar, «Importante empresa del sector» (y «Confidencial») cuenta como sin empresa, y una tarjeta sin título o sin estado frena la corrida porque el sitio cambió. Con eso arma un índice con (título, empresa, lugar) sin mayúsculas, acentos ni espacios de más (sin empresa, la confidencial: título y lugar). Si una oferta del plan figura ahí, la marca como postulada, guarda su `im` y la salta, sin armar ni subir nada ni abrirla; un falso positivo (dos ofertas iguales) solo hace que se saltee una. Si no figura: arma el CV a medida (el `c8` vigente de la oferta, o uno nuevo con `cv make`; si sale solo con ids, `c4`, se usa igual y el informe lo dice), copia el PDF con el nombre `CV-<Nombre>-<Apellido>-<Titulo>.pdf` (nombre y apellido de `personal_information`, título = el titular del CV; solo letras sin acentos, dígitos y guiones, hasta 80 caracteres; la copia se borra al terminar), lo sube en la página de CV (`uploadcv`: el archivo se manda solo y el programa espera a que la página llegue a `candidate/cv/edit`) y **verifica que quedó marcado como el por defecto, con ese nombre**; si no, frena. El PDF del CV se imprime con el mismo Playwright que ya tiene abierto `apply --live` (un Chromium headless aparte, en un contexto nuevo): abrir otro `sync_playwright` adentro del primero falla. **Recién entonces** abre el detalle y toca «Postularme»: la empresa recibe desde el primer momento el CV correcto. Si el sitio igual dice «Ya te postulaste», la marca como postulada (el CV subido queda en la cuenta). En la pantalla final verifica que **el archivo adjunto es el subido**; si es otro, lo repara desde esa pantalla («Editar» y «Actualizar»), lo verifica y el informe lo dice; si la reparación no lo deja bien, frena. La fila de `applications` guarda el nombre del archivo, el `im` de la pantalla final y la versión del CV.
**Revisión (regla 6).** Mientras `apply.review_cv` sea `true` (el valor por defecto), `apply --live` frena antes de armar o subir nada y dice que hay que revisar los CVs de `data/cvs/` (cada uno tiene su `.revision.html`) o apagar la revisión.
**Los CV de la cuenta.** Cada PDF de `uploadcv` es una fila `ul.fila_tabla`: su nombre es el texto de la fila y su radio `rbPrincipal` va solo, sin `label`; «No adjuntar» (valor 0) no cuenta. Marcar ese radio recarga la página (y la recarga puede terminar en `uploadcv` o en `candidate/cv/edit`, como la subida) antes de que Playwright confirme el tilde, así que un «Element is not attached to the DOM» (o un contexto destruido o una navegación interrumpida) mientras la página cambia no frena: el programa espera la recarga y **recién entonces vuelve a cargar la página y exige que el CV pedido haya quedado marcado**; si no quedó, frena. **Al empezar**, antes de la primera oferta, si el CV por defecto de la cuenta no es el `default_pdf_name` de la primera variante que lo declara, lo restaura y el informe lo dice («estaba … se restauró …»); si no puede, frena sin subir ni postular nada.
**Reintentos sin CVs repetidos.** Apenas un CV se sube, la fila de la oferta en `applications` guarda el nombre del archivo (`cv_file`) y la versión (`cv_version`): también si la oferta queda `pending_question`, si el sitio dice «Ya te postulaste» o si la corrida frena después de subir (esa fila tiene el estado `cv_uploaded`, no cuenta como postulada y la oferta sigue en la cola). Cuando la oferta vuelve a la cola, antes de subir el programa **reusa** el CV de la cuenta si se cumplen las tres: una fila de **esa misma oferta** tiene ese `cv_file` y la misma `cv_version`; `uploadcv` tiene un PDF con ese nombre; y **ninguna otra oferta** tiene ese mismo `cv_file`. Si se reusa, lo marca como por defecto y lo verifica como siempre, sin subir otra copia; el informe dice «CV reusado» (y esas ofertas no suman en «CVs subidos»). Si no se cumple alguna, sube como antes. Cuando una espera se agota, «La navegación no terminó» dice la última pantalla vista (host y ruta, sin query).
**La cuenta queda como estaba.** Al terminar, aunque la corrida frene, vuelve a marcar como el por defecto el CV base de la **primera variante del perfil** que declare `default_pdf_name` (no el de la última oferta) y lo verifica; el informe dice cuál quedó. Si ninguna lo declara, el informe avisa que quedó por defecto el último CV subido. Una oferta con una pregunta sin respuesta ya subió su CV: no pasa nada, el informe lo cuenta. El informe dice cuántos PDFs hay en la cuenta y, si pasan de `run.max_account_pdfs`, la corrida no postula y avisa que tenés que limpiar a mano; con la cuenta en el máximo, el programa solo borra un CV en los casos seguros que se describen abajo. El informe también dice si al postular el sitio pidió `setalert.ashx` («Avísame si hay nuevas ofertas»); el programa no toca la configuración de alertas.
**Respuestas desde el CV y claves fijas nuevas (057).** `answers` suma cuatro claves fijas opcionales (también en Parámetros y en `profile.example.yaml`): `location` (barrio y localidad), `mobility` (movilidad o transporte), `salary_part_time` (pretensión de media jornada) y `salary_daily` (pretensión por día); se mandan tal cual. Una abierta de dónde vivís y/o movilidad se contesta con esos textos. La pretensión se elige según la jornada de la pregunta: «por día», «diario» o «por jornada» sin franja → `salary_daily`; medio tiempo, media jornada, part time o una franja de 6 horas o menos → `salary_part_time`; jornada completa o sin indicación → `salary`; «por hora» queda pendiente. Si falta la clave que corresponde, el motivo es «falta <clave> en Parámetros». Las preguntas se contestan con el texto del CV a medida de la postulación (el de `cvs`, sin nombre ni contacto; si no hay, el base), también en negativo («No tengo experiencia en call center; sí en …»); `unknown` queda para datos personales que no están en el CV ni en `answers`.

**Cuánta experiencia (057).** En una cerrada de experiencia el modelo solo devuelve en `jobs` los índices de los puestos del CV base que cuentan (o una lista vacía); el código suma los meses con `employment_period` (sin contar dos veces los superpuestos; «Actualidad» llega hasta hoy; un período sin mes no se lee) y elige la opción por su rango («más de N», «menos de N», «entre N y M», «N a M», «no tengo» si la suma es 0). Lo que el modelo diga de la opción no manda; si no se leen las fechas o los rangos, queda pendiente. Las duraciones por puesto se le pasan al modelo y valen como cifras permitidas en las abiertas. `pending_questions` guarda la `ANSWER_PROMPT_VERSION` (`a3`): al empezar, una pendiente con otra versión vuelve a `new`.

**Respuestas desde el CV, encabezado y zona (060).** En las abiertas valen como cifras las del CV de la postulación y las fechas de `employment_period` del CV base (la fecha «02/2024 a 08/2024» pasa; «3 años en Coto», que el CV no tiene, no). Si el modelo deja vacía una abierta con `source` «cv» o «answers», se le pide una vez más solo esas; si vuelve vacía queda pendiente con «el modelo no escribió respuesta». El texto de cada pregunta se lee sin el encabezado del formulario («Preguntas de selección», «Ya casi estamos…») ni códigos sueltos como `HH119719`. Si la oferta está en `zones`, el pedido se lo dice al modelo y una pregunta de disponibilidad para trabajar presencial en la sede se contesta con `modality` (sí si incluye presencial); si pide mudarse o viajar a otra ciudad, no. `ANSWER_PROMPT_VERSION` pasó a `a3`. El informe suma la sección «Respuestas» con, por cada oferta con preguntas (enviada o pendiente), la pregunta, la respuesta, la fuente y el motivo si quedó pendiente; lo que sale tal cual de `answers` (sueldo, dónde vive, movilidad, una respuesta declarada) se muestra como «(respuesta fija: <clave>)», sin el valor.

**Cuenta llena: CV base (061).** Si la cuenta tiene 10 CVs y `choose_deletion` no encuentra ninguno que se pueda borrar sin riesgo (o con `apply.free_cv_slots: false`), la oferta se envía con el PDF base de su variante que ya está en la cuenta (su `default_pdf_name` o el nombre de su `pdf`), marcado como por defecto, sin subir ni borrar nada; las preguntas se contestan con el CV base, `applications.cv_version` queda en «base» y el informe suma «Enviadas con el CV base (cuenta llena): N» con cada oferta. Si el PDF base no está en la cuenta, frena como antes.

## Tanda

`python -m compuapply tanda --variant ventas --keywords "Atención al cliente, Mozo" --minutes 60 [--max N|all] [--afin-puntaje] [--por-empresa] [--dry]`: el usuario elige un CV (variante) y las búsquedas, y el programa postula solo, con ese CV, a las ofertas que cumplen las reglas duras. Toma las búsquedas en el orden en que se escribieron y, para cada una, lee el listado página por página en cada zona de `zones` (`/trabajo-de-<búsqueda>-en-capital-federal?p=N` y `-en-buenos-aires-gba`; sin zonas, o con una sin slug conocido, la búsqueda nacional; también `scan` y `run` buscan por zona). Por cada página baja los detalles de sus ofertas, las puntúa (la variante elegida siempre), arma el plan restringido a ellas con el puntaje de esa variante y postula antes de leer la siguiente. Pasa a la siguiente búsqueda tras 3 ofertas seguidas, en el orden del listado, cuyo título no es de la búsqueda (contiene sus palabras, sin tildes ni mayúsculas ni palabras cortas como «al», «de» o «y»), o cuando no hay más páginas; cada zona lleva su racha. `--afin-puntaje` (apagado por defecto): también cuenta como de la búsqueda la oferta con puntaje igual o mayor al umbral. `--por-empresa` (apagado por defecto): aplica `apply.company_daily_cap`; apagado, la tanda no limita por empresa (las duplicadas siguen fuera). Termina cuando se acaba la lista, el tiempo (`--minutes`: no empieza otra oferta, la que está en curso termina), el tope o un freno. `--max` reemplaza al tope diario; sin `--max` se usa `apply.daily_cap`, `--max 0` es 0 y `--max all` es sin tope (manda solo el tiempo). `--dry` hace todo menos el envío. Al final se relee «Mis postulaciones» (hasta 10 páginas) para confirmar cada enviada. Un solo informe (`data/runs/…-tanda.md`): una fila por búsqueda y zona (páginas leídas, en zona, de la búsqueda, enviadas y por qué pasó a la siguiente: «3 seguidas de otra cosa», «sin más páginas», «tiempo», «tope»), minutos usados, cómo terminó, enviadas, saltadas con su motivo y respuestas. Las ofertas cuyo detalle responde 301/302 quedan `gone`. En la pestaña Correr, el bloque «Tanda» (CV, búsquedas, minutos, tope —vacío es sin tope—, «Afín por puntaje», «Tope por empresa»; «Plan de la tanda» y «Postular la tanda», que pide confirmación) corre lo mismo; la última tanda queda en `data/last_batch.json` (el programa nunca reescribe `profile.yaml`) y completa el bloque la próxima vez.

**Interés y disponibilidad (065).** Una pregunta cerrada de si le interesa la vacante, el puesto, la modalidad o el contrato de la oferta se contesta siempre «Sí» (si nombra una modalidad que `modality` no incluye, queda pendiente; nunca «No»). Las preguntas de disponibilidad (turnos rotativos, fines de semana, feriados, full o part time, horarios, sábados, presencial) salen de una sola regla con `availability` y `modality`: «Sí» si lo declarado lo cubre, «No» solo si lo niega (o `modality` no incluye presencial) y, si las claves no alcanzan, pendiente con «disponibilidad: falta en tus respuestas». Con opciones «Sí…/No…» se elige la que empieza con esa palabra, sin importar tildes ni mayúsculas. «Mis postulaciones» se pagina con `nav.pag_numeric .b_next[data-path]`; la tanda la lee una vez al principio y una al final, y una búsqueda cuyo listado dice «Parece que no hay ofertas…» pasa a la siguiente («sin ofertas»).

**Rotación de CVs (062).** Con la cuenta llena, el programa borra, en este orden: (1) una copia repetida del CV de una oferta no enviada, (2) el CV de una oferta pendiente que no se envió y (3) el CV de la postulación enviada más vieja (`applied_at`), en cualquier estado (también «Postulado» de menos de 30 días; si un archivo lo usan varias postulaciones, cuenta la más nueva). Motivo en el informe: «rotación: el CV de la postulación más vieja (<empresa>, <fecha>)». Nunca borra el CV por defecto, los PDF que declara una variante, un CV que no figura en `applications` ni el de la oferta en curso; la copia de `cv/postulaciones/` queda 30 días. El CV base de la 061 se usa solo si no hay nada para rotar y su PDF no lo declara otra variante; si no, la oferta se salta con «cuenta llena y sin CV para rotar» (en una tanda se sigue; en `apply --live` la corrida frena como antes).

**Lugar en la cuenta con ofertas pendientes (054).** Entre la copia repetida y los «Proceso finalizado»/«CV Visto», `choose_deletion` puede borrar de la cuenta el CV de una oferta `pending_question` o `cv_uploaded` que nunca se envió (la más vieja primero; solo si ninguna postulación enviada usa ese archivo ni la oferta que se está por postular; motivo «CV de una oferta pendiente que no se envió»); la copia de `cv/postulaciones/` queda y la fila conserva su `cv_file`.

**Reintento sin el PDF en la cuenta (054).** Si la oferta tiene `cv_file` y ese PDF ya no está en la cuenta, `make_cv_provider` lo vuelve a subir desde `cv/postulaciones/` (si la versión es la actual), sin llamar al modelo; si la copia no existe, arma el CV de nuevo.

**Sueldo (054).** «Pretensión» cuenta como pregunta de sueldo. Si la pregunta dice «por jornada», «por día», «por hora», «medio tiempo», «media jornada», «part time» o trae una franja horaria, no se usa `salary` (es mensual): queda pendiente con «sueldo de otra jornada: contestalo en Preguntas», salvo que su clave normalizada esté en `answers`. Si el modelo devuelve un texto vacío, el motivo es «el modelo no escribió respuesta». El informe dice «declarado en `default_pdf_name`» en vez de nombrar la variante.

**Preguntas de selección.** En `candidate/kq` el programa espera hasta unos 20 segundos a que aparezcan los campos `KillerQuestions[…]` (los que no son ocultos) antes de leerlos. Si no llegan, o hay un campo que no es de pregunta, frena y el mensaje (y el informe) trae la **huella** de la pantalla: la ruta sin query y los `input`, `select`, `textarea` y `button` del formulario como `tag[tipo] nombre` (hasta 40, de 80 caracteres como máximo). Cada campo suma «oculto» si no se ve y «label» si tiene un `label[for]`. Nunca lleva valores ni textos, así que no tiene datos personales.
**Las respuestas cerradas no inventan cifras (regla 6).** Si la opción que elige el modelo trae una cifra (dígitos, o un número en palabras junto a una unidad, ver abajo), o si la pregunta es de cuánta experiencia hay (habla de años o meses junto a «experiencia», en la pregunta o en alguna opción, aunque la opción elegida sea «No tengo experiencia»), el código la acepta **solo si el modelo dice que sale de tus respuestas declaradas (`source` «answers») y declaraste la respuesta a esa misma pregunta** (la clave es la pregunta normalizada, como la guarda la pestaña Preguntas) **o** la pregunta es de sueldo, salario o remuneración y cargaste `salary`. El CV no alcanza: un «Sí, más de 2 años en ventas» elegido por lo que dice el CV se descarta. Si no se cumple, la pregunta queda sin respuesta: la oferta queda `pending_question`, la pregunta aparece en Preguntas y la oferta espera. Lo que contestes ahí sirve para **todas** las ofertas que hagan esa misma pregunta. Una cerrada sin cifras ni duración (por ejemplo «¿Tenés secundario completo?») se acepta como siempre.
**Cifras sin falsas alarmas, y la opción declarada manda.** Un número en palabras (un, uno, una, medio, media, dos …) cuenta como cifra solo si la misma opción trae una unidad (año, mes, semana, día, hora u «hs», vez, peso, «$», mil, millón): «Sí, tengo un título» se acepta; «Más de un año» y «Año y medio» esperan tu respuesta. Los dígitos siempre cuentan. **Turno:** una pregunta de turno, horario, jornada o franja que no es de experiencia se acepta con `source` «answers» y `availability` cargada, sin respuesta declarada, si las cifras de la opción elegida son solo franjas horarias («9 a 13», «de 9 a 13 hs», «9-13», «09:00 a 13:00»); una hora suelta («4 hs») sigue siendo cifra y espera en Preguntas. **La opción declarada manda:** si tu respuesta a una pregunta cerrada es igual al texto de una de sus opciones (sin mayúsculas, acentos ni signos), se envía esa opción, elija lo que elija el modelo; si no es igual a ninguna, se trata como antes. Esa opción se envía aunque el modelo diga `unknown`, devuelva una opción que no existe o no traiga la pregunta: así la pregunta no vuelve a Preguntas en bucle. En la pestaña Preguntas, una pregunta cerrada se contesta eligiendo una de sus opciones de un desplegable (que también acepta texto libre); se guarda el texto exacto de la opción.
**Marcar las respuestas, como una persona.** Muchos sitios esconden el radio con CSS y muestran su `label` o su recuadro. Para una pregunta cerrada, si el radio se ve el programa usa `check()` con una espera corta de 5 s; si no se ve, o si `check()` se agota, hace clic en lo que vería una persona: el `label[for]` de la opción si se ve y, si no, el recuadro de la opción si se ve. Después exige que el radio haya quedado marcado: si no quedó, o no hay nada visible para hacer clic, frena con «No se pudo marcar la opción N de la pregunta M» y la huella de la pantalla, y no envía nada. Una respuesta abierta se escribe con la misma espera corta; si el campo no se ve, frena igual. El programa nunca fuerza un clic ni marca con JavaScript (regla 3): el sitio recibe lo mismo que si lo hiciera una persona. **Turnos:** si una pregunta cerrada pide elegir un turno u horario (mañana o tarde, una franja de horas), el modelo elige la primera opción que entre en tu `availability` (por ejemplo «Mañana/Tarde»); si entran varias, la primera de la lista; si no entra ninguna, la pregunta queda sin respuesta. Nunca inventa una disponibilidad que no declaraste. **CVs subidos:** el informe cuenta cada CV en cuanto el sitio lo recibe, aunque después la oferta frene. **Errores:** un error inesperado del navegador aparece en el informe como «clase: primera línea», sin query strings, y el costo suma todo lo que quedó en `api_spend` durante la corrida, también los CV que fallaron.
En la ventana, Correr → «Postular (en vivo)» pide una confirmación que dice cuántas ofertas va a enviar; con la revisión activada no lanza nada y lo explica en el registro.

**7. Todo junto: una corrida completa (modo seco).**

```bash
python -m compuapply run
```

Usa los valores de la sección `run` de `profile.yaml` (por defecto, 2 páginas, 20 detalles, 20 ofertas a puntuar y US$ 0,50). Cada flag (`--pages`, `--max-details`, `--score-limit`, `--max-cost`) gana sobre el perfil. El informe suma «Gasto del mes: US$ X de US$ Y».
Hace los pasos 3, 4 y 6 de una vez: busca con todas las `keywords` de tus variantes (sin repetir), baja las descripciones pendientes, puntúa y arma el plan de `apply`.
Deja un único informe en `data/runs/AAAAMMDD-HHMMSS-run.md`, con una sección por etapa y las tablas de lo que enviaría y saltaría.
Necesita un `profile.yaml` con al menos una `keyword`. No abre ningún navegador, así que no hace falta haber iniciado sesión.
La clave de OpenAI hace falta solo si la etapa de puntaje va a llamar a la API (`score_limit` mayor que 0 y queda presupuesto del mes). Sin clave, `run` busca y baja las descripciones igual, pero frena en el puntaje («Falta la clave de OpenAI: cargala en la pestaña Estado (o con python -m compuapply setup env)»): escribe el informe con su `Freno` y sale con 1. Con `--score-limit 0` (o `score_limit: 0` en el perfil) no pide la clave ni llama a OpenAI: busca, baja las descripciones y arma el plan con los puntajes que ya hay; el informe dice «Puntaje: no se pidió (score_limit 0)» y sale con 0. Con el tope mensual lleno tampoco hace falta. `score` (el comando solo) sí exige la clave.
`python -m compuapply run --live` todavía no está implementado y sale con un error: para postular usá `apply --live`.

**8. Programar una corrida diaria (modo seco).**

```bash
python -m compuapply schedule set --at 09:00
python -m compuapply schedule show
python -m compuapply schedule off
```

`schedule set` crea una tarea de Windows llamada `compuapply-run` que todos los días a esa hora corre `run` (siempre en modo seco: no postula y no existe un `--live` para esto). Sin permisos de administrador. Si la PC estaba apagada a esa hora, corre apenas se prende; arranca también si la notebook está a batería; hace falta tu sesión de Windows abierta. Si ya había una, la reemplaza. `schedule show` dice la hora, cuándo corrió por última vez y cómo le fue, y cuándo corre la próxima (o «No programada»); `schedule off` la quita (si no había ninguna, dice «No había ninguna corrida programada.» y sale bien). En la interfaz es la sección «Corrida programada» de la pestaña Correr.
Cada corrida deja su informe en `data/runs/` y respeta el tope mensual de gasto.

## 8. Frenos

Para parar el programa:

- Creá un archivo vacío llamado `STOP` (sin extensión) en la carpeta del proyecto. `scan`, `score` y `run` lo revisan antes de cada página, detalle, oferta o keyword. Para volver a correr, borralo. En la interfaz, «Detener» lo crea por vos y la próxima acción lo borra.
- O apretá Ctrl+C.

```powershell
New-Item STOP -ItemType File
Remove-Item STOP
```

El programa también frena solo, sin reintentar, en estos casos:

- **403, 429 u otra respuesta inesperada de Computrabajo:** el sitio te bloqueó o te limitó. Esperá un rato antes de volver a correr.
- **CAPTCHA o pantalla desconocida:** pasa en `login` y `session check`. Revisá la ventana a mano. El programa no resuelve CAPTCHA.
- **3 errores seguidos:** en `scan`, 3 ofertas seguidas con una dirección inválida (puede haber cambiado el sitio); en `score`, 3 respuestas inválidas seguidas del modelo.
- **3 ofertas seguidas sin descripción:** en `scan` y `run`, si el detalle de 3 ofertas seguidas no trae la descripción (posible cambio de layout). Una oferta suelta sin descripción no frena: queda marcada y se sigue, y el resumen muestra cuántas fueron.
- **Detalle vencido (301/302):** en `scan` y `run`, un 301 o 302 en el detalle marca la oferta `gone` (queda fuera del plan, del puntaje y de Calibrar), no cuenta para el límite y se sigue. 3 seguidas frenan con «posible bloqueo o cambio del sitio».
- **Tope de gasto:** `score` corta al llegar a `--max-cost`. No es un error: lo ya puntuado queda guardado y podés volver a correrlo.
- **Tope mensual:** `run.max_cost_month` (por defecto US$ 2,00) es lo máximo que se gasta en OpenAI por mes; el mes es el de la fecha UTC, como factura OpenAI. El presupuesto de puntaje de cada corrida es el menor entre `--max-cost` y lo que queda del mes. Si ya no queda nada, `score` y `run` no llaman a la API: dicen «Tope mensual alcanzado: US$ X de US$ Y» y terminan bien (`run` arma el plan igual, con lo ya puntuado). Si no, suman «Gasto del mes: US$ X de US$ Y». La estimación usa la tabla de precios del programa.

Qué queda guardado cuando algo frena:

- `scan`: si frena mientras lee el listado, esa corrida no guarda ofertas. Si frena bajando descripciones, lo ya bajado queda.
- `score`: lo ya puntuado queda guardado.
- `run`: hace las dos cosas de arriba y **escribe el informe igual**. Si una etapa frena, no corre las siguientes; el informe muestra lo hecho hasta ahí y una sección «Freno» con la etapa y el motivo, y el comando termina con error.

## 9. Privacidad

A OpenAI se envía el texto de cada oferta y, de tus CVs, solo esto: experiencia, proyectos, estudios, certificaciones, idiomas, intereses y modalidad de trabajo (remoto o presencial).
Nunca se envían tus datos de contacto, tu identidad, tu salario ni links (como tu GitHub).

Estos archivos y carpetas están en `.gitignore`, así que git nunca los sube: `.env`, `profile.yaml`, `data/`, `cv/`, `.browser-profile/` y `STOP`.
