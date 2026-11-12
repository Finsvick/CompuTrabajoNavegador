# Reglas duras

Las leen los dos chats antes de empezar. Valen para el código y para el trabajo de ambos. Si una tarea las contradice, no se ejecuta: se frena y se avisa al usuario.

## Producto

1. **Postulación automática, gobernada por reglas.** El programa postula solo, sin pedir confirmación por oferta. Envía únicamente si se cumplen todas estas condiciones: puntaje igual o mayor al umbral (configurable, provisional 75), la oferta es de **Capital Federal o Buenos Aires-GBA** (las búsquedas se hacen en esas dos zonas, con `-en-capital-federal` y `-en-buenos-aires-gba`), la oferta no fue postulada antes (base local y "Mis postulaciones"), ni la empresa ni el título están excluidos, no se superó el tope diario, y todas las preguntas del formulario se pueden responder con datos de `profile.yaml`. Si una condición falla, **salta la oferta** y la deja marcada para que el usuario la mire cuando quiera. Nunca inventa una respuesta.
2. **Modo seco por defecto.** Todo lo que cambie estado en Computrabajo (navegar a `candidate/apply`, cambiar el CV por defecto, subir o borrar un archivo) exige el flag `--live`. Sin él, el programa hace todo menos enviar y muestra qué habría hecho.
3. **Sin evasión y con frenos.** Ante un CAPTCHA, un 403 o 429, una pantalla desconocida o un cambio de layout, el programa **frena toda la corrida y avisa**. No resuelve CAPTCHA, no rota IP ni User-Agent, no reintenta un 429. También frena tras 3 fallos seguidos, y se puede detener a mano en cualquier momento (archivo `STOP` en la carpeta del proyecto, o Ctrl+C): lo revisa antes de cada oferta.
4. **Ritmo humano.** Pausas con variación entre páginas (mínimo 0,7 s) y entre postulaciones (varios segundos), y un tope configurable (diario, o de la tanda; en la tanda puede quedar vacío y entonces manda solo el tiempo elegido). El usuario supervisa las corridas, así que el tope es un ajuste suyo y no una traba: no hay un límite obligatorio para la primera corrida.
5. **Credenciales.** El usuario se loguea a mano, una vez, en un perfil persistente de Playwright. Ni el programa ni los chats leen, guardan o escriben su contraseña. Si la sesión vence, el programa frena y avisa.
6. **Cero invención.** El contenido de los CVs sale solo de los CVs base que el usuario declara en `profile.yaml` y de su banco de verdades aprobado. El LLM puede reformular, ordenar y priorizar; el código rechaza tecnologías, números, títulos, niveles y empresas que no estén en esas fuentes. Cada CV generado queda guardado, con su revisión, y listado en el informe de la corrida, y el programa puede subirlo y usarlo sin revisión previa. Si falta un dato, se le pide al usuario.
7. **Sin spam y con rastro.** No se postula a ofertas con puntaje bajo, repetidas ni ya postuladas. Cada corrida termina con un informe de lo enviado y de lo saltado (oferta, empresa, puntaje, CV usado, motivo) para que el usuario audite. El navegador corre visible (no headless) por defecto, para que pueda mirar la corrida.

## Sitio en vivo

8. **Ningún chat ejecuta acciones que cambien estado en Computrabajo**: postular, cambiar el CV por defecto, subir o borrar archivos, seguir empresas, guardar favoritos, crear alertas. Las hace el programa con `--live`, o el usuario.
9. **PRODUCCIÓN no abre Computrabajo** ni usa herramientas de navegador. Sus tests usan HTML guardado.
10. **DECISIÓN solo navega para leer.** No hace click en "Postularme" ni en ningún botón de envío, y no abre la URL `candidate/apply` (navegar a ella puede postular).

## Datos

11. **Archivos que ningún chat lee, imprime ni sube:** `.env`, `.browser-profile/`, `profile.yaml`, `data/`, los PDF reales en `cv/`. Los tests y ejemplos usan datos falsos (`profile.example.yaml`).
12. No se escriben datos personales reales (email, teléfono, DNI, dirección) en código, tests, logs ni documentos.
