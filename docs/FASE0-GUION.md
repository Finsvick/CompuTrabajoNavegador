# FASE 0: guion de la primera postulación supervisada

Se hace en el navegador integrado de Claude (D-032), con el usuario y DECISIÓN juntos. Dura unos 15 minutos. Su resultado es `docs/FINDINGS.md` actualizado y la TAREA de la Fase 4 (`apply --live`).

El clic en «Postularme» **envía una postulación real y no se puede deshacer**. Por eso no empieza nada hasta que el usuario diga «arrancamos».

## Reglas de esta sesión

- El usuario escribe su contraseña, hace el clic y completa los formularios. DECISIÓN solo lee (reglas 5, 8 y 10).
- DECISIÓN no abre por URL `candidate/apply` ni `match/`. La página de «Mis postulaciones» la abre el usuario desde el menú.
- Nada de capturas durante el login ni en pantallas con formularios (muestran datos personales). Si hace falta una, escala 0,5.
- En `docs/` no se escribe ningún valor que el usuario haya cargado (nombre, teléfono, email, respuestas, nombres de archivos de CV; regla 12). Solo etiquetas, tipos de campo y rutas sin ids.
- DECISIÓN no pide el cuerpo de ninguna request: puede llevar las respuestas del formulario o tokens. Solo método, ruta, estado y redirecciones.
- Una sola postulación. Ante un CAPTCHA, un bloqueo o una pantalla rara, se frena y se avisa (regla 3).

## Antes de arrancar: dos decisiones del usuario

1. **Qué oferta.** Una a la que realmente quieras postularte, porque se va a enviar.
2. **Qué CV sale.** El 2026-11-01 el único CV de la cuenta era el de Ventas. Si el sitio manda el CV por defecto, saldría ese. Dos caminos:
   - **A.** Elegir una oferta en la que ese CV sirva.
   - **B.** Antes de empezar, subir a mano en `uploadcv` el PDF de la variante (ya revisado) y marcarlo «por defecto». Recomendado si el PDF ya está listo: además prueba el paso que la Fase 4 va a automatizar. DECISIÓN lo mira sin tocar nada.

**Elegido el 2026-11-02 (D-054): camino A**, con el CV de Ventas y una oferta de ventas que el usuario quiera. Elegila directamente en el navegador; no se escanean ofertas de ventas con el programa.

## Pasos

| # | Quién | Qué |
|---|---|---|
| 1 | DECISIÓN | Abre `https://ar.computrabajo.com/` en el navegador integrado. Si la app pide permiso para el sitio, lo aceptás vos. |
| 2 | Usuario | Inicia sesión en esa pestaña. DECISIÓN no mira la pantalla. Avisá «logueado». |
| 3 | DECISIÓN | Confirma la sesión leyendo el texto del home (menú «Mi área»). Lee `…/candidate/cv/uploadcv` (solo lectura): cuántos CV hay y cuál está marcado por defecto, sin nombres de archivo. |
| 4 | Usuario | Busca la oferta y abre su detalle en la misma pestaña. Avisá «oferta abierta». |
| 5 | DECISIÓN | Lectura previa, sin clic: texto de la oferta, atributos del botón «Postularme» (con el `oi` tapado), formularios de la página y una primera consulta de red como base. |
| 6 | Usuario | Un solo clic en «Postularme». Si pide algo, respondé con tus datos reales como harías siempre. En **cada pantalla nueva** escribí «pantalla» y esperá el «leída» antes de seguir. No recargues, no vuelvas atrás, no abras otra pestaña. |
| 7 | DECISIÓN | En cada pantalla: URL, texto, campos del formulario sin valores y requests nuevas (ver «Cómo se lee»). |
| 8 | DECISIÓN | Al terminar: pantalla de confirmación, request que envió la postulación, el detalle de la misma oferta otra vez (¿cambió el botón?) y `uploadcv` otra vez. |
| 9 | Usuario | Abre «Mis postulaciones» desde el menú. DECISIÓN lee cómo se lista cada postulación. |
| 10 | DECISIÓN | Escribe `docs/FINDINGS.md`, la decisión en `docs/DECISIONES.md`, reescribe `docs/ESTADO.md` y arma la TAREA de la Fase 4. |

No se hace una segunda postulación «para probar», ni favoritos, seguir empresas o alertas, ni se borra ni sube nada que no esté acordado en la decisión 2.

## Cómo se lee (solo inspección)

Campos de un formulario, sin valores:

```js
[...document.querySelectorAll('form input, form select, form textarea')].map(e => ({
  tag: e.tagName.toLowerCase(), type: e.type, name: e.name || e.id, required: e.required,
  label: (e.labels?.[0]?.innerText || e.getAttribute('aria-label') || e.placeholder || '').trim().slice(0, 120),
  options: e.tagName === 'SELECT' ? [...e.options].map(o => o.text.trim()).slice(0, 15) : undefined }))
```

Botón «Postularme», con el id tapado:

```js
(b => b && ({ text: b.innerText.trim(), tag: b.tagName, href: b.getAttribute('href'), attrs: [...b.attributes].map(a => a.name),
  apply: (b.dataset.hrefOfferApply || '').replace(/oi=[0-9A-Fa-f]+/, 'oi=<id>') }))(document.querySelector('a.b_primary.big, [data-href-offer-apply]'))
```

Red: `read_network_requests` con `urlPattern` (`candidate`, `apply`, `match`, `postul`), una consulta de base en el paso 5 y otra tras cada pantalla. Se anota método, ruta sin ids, estado y redirecciones.

## Qué tiene que quedar respondido en FINDINGS

1. ¿Postular es un clic o hay pantallas y preguntas? Cuántas pantallas y de qué tipo.
2. ¿El CV se elige al postular (aparece un selector) o va el marcado por defecto? ¿Se puede subir un archivo dentro del propio flujo de postulación, sin pasar por `uploadcv`? (Define si un CV por oferta es viable.)
3. Qué dice la confirmación, a qué URL (sin ids) llega y cómo queda la oferta (botón y texto).
4. La request que envía: método, ruta, estado, redirecciones; si es navegación o XHR.
5. Preguntas del formulario: dónde aparecen (ventana emergente o página), texto, tipo, obligatoria, opciones (sin respuestas), si alguna pide adjuntar algo y si se pueden responder con datos de `profile.yaml`.
6. «Mis postulaciones»: URL, cómo se identifica cada postulación (para el dedup), selectores y paginación.
7. ¿Apareció algún CAPTCHA o desafío? Una sola postulación no prueba el límite diario: queda como incógnita para las primeras corridas reales.
8. Si se hizo el camino B: ¿cambiar el CV por defecto es inmediato? ¿hay límite de archivos?
9. Qué CV se adjuntó realmente (posición en la lista, tipo), sin nombres.
