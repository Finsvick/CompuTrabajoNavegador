"""Sesión de Computrabajo en un perfil persistente de Google Chrome (Playwright) y postulación de una oferta (`apply_one`).

El usuario inicia sesión a mano, en una ventana de Chrome común que abre `start_chrome` con un perfil propio del programa: este
módulo no ve su contraseña ni guarda nada de la sesión. Sin Chrome instalado se usa Chromium, como antes.
"""
import json
import os
import re
import subprocess
import time
import unicodedata
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout

# Página de solo lectura: sin sesión, el sitio termina en el portal de acceso (otro host).
CHECK_URL = "https://candidato.ar.computrabajo.com/candidate/cv/uploadcv"
CHECK_HOST = "candidato.ar.computrabajo.com"
LOGIN_HOSTS = ("secure.computrabajo.com",)
PROFILE_DIR = Path(".browser-profile")  # el perfil de Chromium (sin Chrome instalado)
CHROME_PROFILE = PROFILE_DIR / "chrome"  # el perfil de Chrome: Chrome no abre un perfil que escribió un Chromium más nuevo
CHROME_DIRS = ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")
CHALLENGE_SELECTORS = (
    'iframe[src*="recaptcha"]',
    'iframe[src*="hcaptcha"]',
    'iframe[src*="challenges.cloudflare.com"]',
)
HALT = "pantalla desconocida o desafío: revisá la ventana a mano"
LOGIN_PROMPT = "Iniciá sesión en la ventana de Chromium. Cuando termines, volvé acá y apretá Enter."
CHROME_PROMPT = ('Se abrió Chrome. Iniciá sesión en Computrabajo (con Google o con tu mail), tildá "Recordarme" si aparece y '
                 "cerrá esa ventana de Chrome.")
NO_CHROME = "No se encontró Chrome: se usa Chromium, con el perfil .browser-profile/."


def find_chrome() -> Path | None:
    """`chrome.exe` de Program Files, Program Files (x86) o %LOCALAPPDATA%; None si Chrome no está instalado."""
    for variable in CHROME_DIRS:
        base = os.environ.get(variable)
        if base and (path := Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe").is_file():
            return path
    return None


def browser_name() -> str:
    """«Chrome» si está instalado y «Chromium» si no: el navegador que abren `login`, `session check` y `apply --live`."""
    return "Chrome" if find_chrome() else "Chromium"


def profile_dir() -> Path:
    """El perfil persistente del navegador que se usa."""
    return CHROME_PROFILE if find_chrome() else PROFILE_DIR


def start_chrome(chrome: Path, profile: Path = CHROME_PROFILE):
    """Abre `chrome.exe` como un proceso común (otra instancia: el Chrome personal no se toca), con el perfil propio del programa
    y la página de verificación. Sin Playwright ni nada que oculte la ventana; devuelve el `Popen`."""
    command = [str(chrome), f"--user-data-dir={Path(profile).resolve()}", "--no-first-run", "--no-default-browser-check",
               "--disable-extensions", CHECK_URL]  # sin extensiones: Chrome ofrecía una de otro programa de la PC, con acceso a todos los sitios
    return subprocess.Popen(command)


def open_context(pw, profile: Path | None = None):
    """Abre Chrome (o Chromium si no está instalado) con el perfil persistente, con ventana visible y el navegador tal como viene."""
    if find_chrome():
        return pw.chromium.launch_persistent_context(str(profile or CHROME_PROFILE), channel="chrome", headless=False, locale="es-AR")
    return pw.chromium.launch_persistent_context(str(profile or PROFILE_DIR), headless=False, locale="es-AR")


def is_logged_in(page) -> bool:
    """Carga la página de verificación y dice si hay sesión.

    En el portal de acceso (`LOGIN_HOSTS`) devuelve False sin buscar CAPTCHA: ahí actúa el usuario a mano.
    Lanza RuntimeError (frena la corrida) ante un HTTP >= 400, un desafío en el sitio o un host inesperado.
    """
    response = page.goto(CHECK_URL, wait_until="domcontentloaded")
    status = getattr(response, "status", None)
    if status is not None and status >= 400:
        raise RuntimeError(f"Computrabajo respondió {status}: {HALT}.")
    url = urlsplit(page.url)
    if url.hostname in LOGIN_HOSTS:
        return False
    if any(page.query_selector(selector) for selector in CHALLENGE_SELECTORS):
        raise RuntimeError(f"Apareció un desafío (CAPTCHA): {HALT}.")
    if url.hostname != CHECK_HOST:
        # Solo host y path: la query y el fragmento llevan ids y tokens.
        where = f"{url.hostname}{url.path}" if url.hostname else "sin host"
        raise RuntimeError(f"La página terminó en otro sitio ({where}): {HALT}.")
    return not url.path.startswith("/acceso")


def _page(context):
    return context.pages[0] if context.pages else context.new_page()


def login(context, wait=input) -> bool:
    """Verifica la sesión; si no hay, espera a que el usuario inicie sesión a mano y verifica una vez más."""
    page = _page(context)
    if is_logged_in(page):
        return True
    print(LOGIN_PROMPT)
    wait()
    return is_logged_in(page)


def session_check(context) -> bool:
    """True si el perfil tiene una sesión activa."""
    return is_logged_in(_page(context))


# ── postular una oferta ──────────────────────────────────────────────────────

class Halt(RuntimeError):
    """Una pantalla que el programa no conoce, un desafío, un 403/429 o la falta de sesión: se frena toda la corrida (regla 3)."""


class Unconfirmed(RuntimeError):
    """Se tocó el envío pero la pantalla final no confirma esta oferta: queda «sin confirmar», no enviada, y la tanda sigue (no es un `Halt`).
    `text` es el texto visible de la pantalla (crudo: quien lo guarda le saca los datos personales) y `blocks` los bloques con «postul»
    como `(largo, comienzo)`."""

    def __init__(self, message: str, text: str = "", blocks: list | None = None):
        super().__init__(message)
        self.text, self.blocks = text, blocks or []


DETAIL_HOST = "ar.computrabajo.com"
APPLY_HOST = "candidato.ar.computrabajo.com"
SITE_SUFFIX = "computrabajo.com"
APPLY_BUTTON = "a[data-href-offer-apply]"  # «Postularme»: un <a> sin href que un JS lleva a `match/`
ALREADY_BOX = "div.box_ok"  # siempre está en el HTML, oculto: solo cuenta si se ve
ALREADY_TEXT = "Ya aplicaste"
QUESTION_FIELDS = '[name^="KillerQuestions["]'
FIELD_NAME = re.compile(r"KillerQuestions\[(\d+)\]\.(ClosedQuestion|OpenQuestion)$")
SUBMIT_BUTTON = 'form[action*="kq"] [type="submit"]'  # «Enviar mi CV»
SUCCESS_TEXT = "Te postulaste correctamente"
OPEN_MAX = 500
SETTLE_POLLS = 40  # 40 x 0,5 s: lo que se espera a que una navegación llegue a una pantalla conocida
CLIMB = 6  # niveles que se sube desde un campo para encontrar el recuadro de su pregunta
UPLOAD_URL = CHECK_URL  # «Mis CV adjuntos»: lista de PDF con un radio «por defecto»
UPLOAD_INPUT = "input#ContainerGeneralOverWrite_ContainerGeneral_fuUploadCv"  # manda el archivo solo al elegirlo
CV_ROW = "ul.fila_tabla"  # un PDF de la cuenta en `uploadcv`: su texto es el nombre del archivo
CV_RADIO = 'input[name="rbPrincipal"]'  # el radio «por defecto» de la fila (value = id del archivo; «No adjuntar» vale 0)
CV_LINK = "a.it-file"  # el nombre del archivo de la fila; `data-filecandidateid` es su id
CV_TRASH = "span.icon.papelera"  # la papelera de la fila, con el mismo `data-filecandidateid`
CV_MODAL = '[role="dialog"], [role="alertdialog"], .modal, .ui-dialog, .popup'  # un cuadro que el programa no conoce
DELETE_CONFIRM, DELETE_CANCEL = "input#btDelete", "input#btCancelDelete"  # el cuadro blockUI `#frmDelete` del sitio: confirmar y cancelar
DELETE_TEXT = "eliminar este curriculum"  # lo que dice ese cuadro (sin acentos, para comparar)
LIMIT_TEXT = "numero maximo de documentos"  # «Se ha alcanzado el número máximo de documentos permitido» (sin acentos, para comparar)
LIMIT_MESSAGE = "Se ha alcanzado el número máximo de documentos permitido"
# Lo que dice un error de Playwright cuando la página cambió debajo: contexto destruido, navegación interrumpida o el elemento ya
# no está en el DOM (marcar el radio de la página de CV la recarga antes de que Playwright confirme el tilde)
NAVIGATING = ("execution context was destroyed", "is interrupted by another navigation", "navigation interrupted",
              "is not attached to the dom")
FIELD_WAIT = 5000  # ms que se espera a que un campo de pregunta se vea antes de hacer lo que haría una persona o frenar
FINGERPRINT_TAGS = ("input", "select", "textarea", "button")
FINGERPRINT_MAX, FINGERPRINT_ENTRY = 40, 80  # entradas de una huella y largo de cada una
ALREADY_APPLIED_TEXT = "Ya te postulaste a esta oferta"  # lo dice `match/?oi=…` de una oferta ya postulada
EDIT_INPUT = "input#fuUploadCv"  # en `postapply`: cambiar el CV adjunto de la postulación
EDIT_APPLY = "a#updateAttached"
APPLICATIONS_URL = "https://candidato.ar.computrabajo.com/candidate/match/"  # «Mis postulaciones»: solo se lee
APPLICATION_CARD = "div[data-match][data-targeturl]"
NEXT_PAGE = 'nav.pag_numeric .b_next[data-path], a[rel="next"]'  # «Siguiente» de «Mis postulaciones»: un span con la URL en data-path
CARD_TITLE, CARD_COMPANY, CARD_LOCATION, CARD_STATUS = "h1", "p.fc_base.mt5", "p.fc_base:not(.mt5)", "p.fwB"
CONFIDENTIAL = {"confidencial", "empresa confidencial", "importante empresa del sector"}  # lo que el sitio muestra en vez de la empresa
RATING_SUFFIX = re.compile(r"\s+\d[.,]\d\s*$")  # la nota de la empresa (4,4) al final de su nombre en «Mis postulaciones»
ATTACHED = re.compile(r"Hemos adjuntado\s+(.+?)\s+a tu postulaci", re.S)


def _halt(message: str) -> Halt:
    return Halt(f"{message}: {HALT}.")


def _check_site(page, statuses: list) -> None:
    """Frena ante un HTTP >= 400 de una página del sitio, un desafío (CAPTCHA) o el portal de acceso (sin sesión)."""
    if statuses:
        raise _halt(f"Computrabajo respondió {statuses[0]}")
    url = urlsplit(page.url)
    if url.hostname in LOGIN_HOSTS:
        raise Halt("Sin sesión: la sesión venció. Corré python -m compuapply login e iniciá sesión a mano.")
    if any(page.query_selector(selector) for selector in CHALLENGE_SELECTORS):
        raise _halt("Apareció un desafío (CAPTCHA)")


def _screen(page) -> str:
    """Qué pantalla es la actual: `detail`, `transit` (match/apply), `applied` (match/ dice «Ya te postulaste»), `kq`, `postapply`,
    `uploadcv`, `cvedit`, `applications` («Mis postulaciones») o `unknown` (por host y path; solo en `match/` se mira el texto)."""
    url = urlsplit(page.url)
    if url.hostname == DETAIL_HOST and url.path.startswith("/ofertas-de-trabajo/"):
        return "detail"
    if url.hostname == APPLY_HOST:
        if url.path.startswith("/match/") and ALREADY_APPLIED_TEXT in (page.inner_text("body") or ""):
            return "applied"
        for name, prefix in (("transit", "/match/"), ("transit", "/apply/"), ("kq", "/candidate/kq"), ("postapply", "/candidate/postapply"),
                             ("uploadcv", "/candidate/cv/uploadcv"), ("cvedit", "/candidate/cv/edit"), ("applications", "/candidate/match")):
            if url.path.startswith(prefix):
                return name
    return "unknown"


def _navigating(error: Exception) -> bool:
    """Si un error de Playwright dice que la página estaba cambiando (contexto destruido, navegación interrumpida): se vuelve a mirar."""
    return isinstance(error, PlaywrightError) and any(text in str(error).lower() for text in NAVIGATING)


def _settle(page, statuses: list, wanted: tuple) -> str:
    """Espera a que la navegación llegue a una de las pantallas `wanted`; cualquier otra pantalla frena.

    Mientras la página navega, un `Error` de Playwright por contexto destruido o navegación interrumpida cuenta como «todavía
    navegando» y se vuelve a mirar; cualquier otro error frena.
    """
    for _ in range(SETTLE_POLLS):
        try:
            _check_site(page, statuses)
            screen = _screen(page)
        except PlaywrightError as e:
            if not _navigating(e):
                raise
            page.wait_for_timeout(500)
            continue
        if screen in wanted:
            return screen
        if screen == "unknown":
            url = urlsplit(page.url)
            raise _halt(f"Pantalla desconocida ({url.hostname}{url.path})")
        page.wait_for_timeout(500)  # una pantalla conocida que todavía no es la esperada (la navegación sigue)
    url = urlsplit(page.url)
    raise _halt(f"La navegación no terminó (última pantalla: {url.hostname}{url.path})")  # sin query: lleva ids y tokens


def _up(element):
    return element.query_selector("xpath=..")


def _label(page, field) -> str:
    """El texto de una opción: su `label[for]`, o el texto de su recuadro, o su valor."""
    field_id = field.get_attribute("id")
    label = page.query_selector(f'label[for="{field_id}"]') if field_id else None
    text = " ".join((label.inner_text() if label else "").split())
    if not text:
        parent = _up(field)
        text = " ".join((parent.inner_text() if parent else "").split())
        text = text if len(text) <= 200 else ""
    return text or (field.get_attribute("value") or "")


def _index_of(field) -> int | None:
    found = FIELD_NAME.match(field.get_attribute("name") or "")
    return int(found.group(1)) if found else None


def _question_text(page, first, options: list[str], index: int) -> str:
    """El texto de la pregunta: lo que queda del recuadro más chico que tiene sus campos y ningún campo de otra pregunta."""
    box = first
    for _ in range(CLIMB):
        parent = _up(box)
        if parent is None or parent.query_selector(SUBMIT_BUTTON):
            break
        if any(_index_of(other) not in (index, None) for other in parent.query_selector_all(QUESTION_FIELDS)):
            break
        box = parent
    lines = [" ".join(line.split()) for line in box.inner_text().splitlines()]
    return _without_header(" ".join(line for line in lines if line and line not in options))


FORM_HEADER = re.compile(r"preguntas de selecci[oó]n|ya casi estamos,?\s*pero antes contest[aá] estas preguntas\.?|(?-i:(?<!\w)[A-Z]{1,4}\d{4,}(?!\w))",
                         re.IGNORECASE)


def _without_header(text: str) -> str:
    """El texto de la pregunta sin el encabezado del formulario («Preguntas de selección», «Ya casi estamos…») ni códigos sueltos
    como «HH119719». Si no queda nada, devuelve el texto como estaba."""
    cleaned = " ".join(FORM_HEADER.sub(" ", text).split())
    return cleaned or text


def fingerprint(page, within=None) -> str:
    """La huella de una pantalla que el programa no entiende: la ruta sin query y los `input`, `select`, `textarea` y `button` del
    `form` de las preguntas (o del primer `form`, o de la página si no hay) como `tag[type] name`, más «oculto» si el campo no se
    ve y «label» si tiene un `label[for]`. Hasta 40 entradas de 80 caracteres; nunca valores ni textos, así que no lleva datos
    personales."""
    scope = within or page.query_selector('form[action*="kq"]') or page.query_selector("form") or page
    entries = []
    for tag in FINGERPRINT_TAGS:
        for field in scope.query_selector_all(tag):
            kind, name, field_id = field.get_attribute("type"), field.get_attribute("name"), field.get_attribute("id")
            hidden = " oculto" if not field.is_visible() else ""
            labelled = " label" if field_id and page.query_selector(f'label[for="{field_id}"]') is not None else ""
            entries.append(f"{tag}{f'[{kind}]' if kind else ''}{f' {name}' if name else ''}{hidden}{labelled}"[:FINGERPRINT_ENTRY])
    shown = ", ".join(entries[:FINGERPRINT_MAX]) + (" …" if len(entries) > FINGERPRINT_MAX else "")
    return f"{urlsplit(page.url).path} · campos: {shown or 'ninguno'}"


def _question_fields(page) -> list:
    """Los campos `KillerQuestions[…]` de la pantalla. Si todavía no hay ninguno que no sea oculto, espera hasta `SETTLE_POLLS`
    (los campos pueden llegar después de la URL), tolerando los errores de navegación; devuelve lo último que vio."""
    fields: list = []
    for _ in range(SETTLE_POLLS):
        try:
            fields = page.query_selector_all(QUESTION_FIELDS)
            if any(field.get_attribute("type") != "hidden" for field in fields):
                return fields
        except PlaywrightError as e:
            if not _navigating(e):
                raise
        page.wait_for_timeout(500)
    return fields


def read_questions(page) -> list[dict]:
    """Las preguntas de selección de la pantalla `candidate/kq`, sin leer ningún valor ya cargado.

    Cada una: `{"index", "kind": "closed" | "open", "text", "options": [texto], "fields": [campos]}`. Espera a que lleguen los
    campos. Una pregunta sin texto, un campo que no es de pregunta ni oculto, o una pantalla sin preguntas frena (`Halt`); los dos
    últimos con la `fingerprint` de la pantalla en el mensaje.
    """
    groups: dict[int, list] = {}
    for field in _question_fields(page):
        index = _index_of(field)
        if index is None:
            if field.get_attribute("type") == "hidden":
                continue
            raise _halt(f"Un campo de las preguntas de selección no es el esperado ({fingerprint(page)})")
        groups.setdefault(index, []).append(field)
    if not groups:
        raise _halt(f"La pantalla de preguntas de selección no trae preguntas ({fingerprint(page)})")
    questions = []
    for index in sorted(groups):
        fields = groups[index]
        kinds = {FIELD_NAME.match(f.get_attribute("name")).group(2) for f in fields}
        if len(kinds) != 1:
            raise _halt("Una pregunta mezcla campos cerrados y abiertos")
        closed = kinds == {"ClosedQuestion"}
        options = [_label(page, f) for f in fields] if closed else []
        text = _question_text(page, fields[0], options, index)
        if not text or (closed and not all(options)):
            raise _halt("No se pudo leer el texto de una pregunta de selección")
        questions.append({"index": index, "kind": "closed" if closed else "open", "text": text, "options": options, "fields": fields})
    return questions


def _click_target(page, field):
    """Lo que vería una persona para marcar una opción: su `label[for]` si se ve; si no, su recuadro si se ve; si no, nada."""
    field_id = field.get_attribute("id")
    label = page.query_selector(f'label[for="{field_id}"]') if field_id else None
    if label is not None and label.is_visible():
        return label
    box = _up(field)
    return box if box is not None and box.is_visible() else None


def _mark(page, question: dict, option: int) -> None:
    """Marca una opción cerrada como lo haría una persona.

    Si el radio se ve, `check()` con una espera corta. Si no se ve (el CSS lo oculta y deja visible su `label` o su recuadro), o si
    `check()` se agota, un clic en lo que se ve. Después exige `is_checked()`: si no quedó marcado, o no hay nada visible para
    hacer clic, frena con la huella de la pantalla. Sin `force` ni JavaScript: el sitio toma un clic como el de una persona.
    """
    field = question["fields"][option]
    if field.is_visible():
        try:
            field.check(timeout=FIELD_WAIT)
        except PlaywrightTimeout:
            pass  # se agotó la espera: se prueba con el clic de una persona
    if not field.is_checked():
        target = _click_target(page, field)
        if target is not None:
            target.click(timeout=FIELD_WAIT)
    if not field.is_checked():
        raise _halt(f"No se pudo marcar la opción {option + 1} de la pregunta {question['index'] + 1} ({fingerprint(page)})")


def _fill(page, questions: list[dict], answers: list[dict]) -> None:
    """Contesta cada pregunta: la opción elegida (cerradas, con `_mark`) o el texto (abiertas, hasta 500 caracteres)."""
    for question, answer in zip(questions, answers):
        if question["kind"] == "closed":
            _mark(page, question, answer["option"])
        else:
            if len(answer["text"]) > OPEN_MAX:
                raise _halt("Una respuesta abierta pasa de 500 caracteres")
            field = question["fields"][0]
            try:
                if not field.is_visible():
                    raise PlaywrightTimeout("campo oculto")
                field.fill(answer["text"], timeout=FIELD_WAIT)
            except PlaywrightTimeout:
                raise _halt(f"No se pudo escribir la respuesta de la pregunta {question['index'] + 1} ({fingerprint(page)})") from None


def _plain(text: str) -> str:
    """En minúsculas, sin acentos y con los espacios colapsados."""
    folded = "".join(ch for ch in unicodedata.normalize("NFD", text) if not unicodedata.combining(ch))
    return " ".join(folded.casefold().split())


def _text(card, selector: str) -> str:
    """El texto, con los espacios colapsados, del primer elemento de la tarjeta que calza con el selector ("" si no hay)."""
    element = card.query_selector(selector)
    return " ".join(element.inner_text().split()) if element else ""


def _application(card) -> dict:
    """Una tarjeta de «Mis postulaciones»: cada campo sale de su selector; el `im` de `data-match`.

    Las ofertas viejas o cerradas no traen empresa ni lugar ("") y una empresa confidencial queda en "". Sin título o sin estado
    el sitio cambió: `Halt`. Solo lee.
    """
    title, status = _text(card, CARD_TITLE), _text(card, CARD_STATUS)
    if not title or not status:
        raise _halt("Una tarjeta de «Mis postulaciones» no tiene título o estado: el sitio cambió")
    company = RATING_SUFFIX.sub("", _text(card, CARD_COMPANY))  # «Randstad Argentina 4,4»: la nota de la empresa viene pegada
    return {"title": title, "company": "" if _plain(company) in CONFIDENTIAL else company, "location": _text(card, CARD_LOCATION),
            "status": status, "im": card.get_attribute("data-match") or ""}


def read_applications(page, pages: int = 3, want=None) -> list[dict]:
    """Lee «Mis postulaciones» (de 1 a `pages` páginas, siguiendo el enlace a la siguiente si hay) y devuelve `[{title, company,
    location, status, im}]` sin repetir. Solo lee: no toca ningún botón. Sin sesión, con un desafío o en otra pantalla, `Halt`.
    La siguiente página es `nav.pag_numeric .b_next[data-path]` (su `data-path` es la URL) o un `a[rel=next]`. Con `want` (una función que
    recibe lo leído hasta ahora y dice si ya está todo lo que se buscaba) corta apenas devuelve True."""
    found: dict[str, dict] = {}
    url = APPLICATIONS_URL
    with _watch(page) as (statuses, _):
        for _page_number in range(max(pages, 1)):
            _goto(page, url, statuses)
            if _screen(page) != "applications":
                raise _halt("«Mis postulaciones» terminó en otra pantalla")
            cards = page.query_selector_all(APPLICATION_CARD)
            for card in cards:
                application = _application(card)
                found.setdefault(application["im"] or f"{len(found)}", application)
            if want is not None and want(list(found.values())):
                break
            following = page.query_selector(NEXT_PAGE)
            href = following and (following.get_attribute("data-path") or following.get_attribute("href"))
            target = urlsplit(urljoin(page.url, href)) if href else None
            stay = target is None or target.hostname != APPLY_HOST or not target.path.startswith("/candidate/match")
            if not cards or stay or target.geturl() == page.url:  # sin siguiente, a otro sitio o a la misma página: se termina
                break
            url = target.geturl()
    return list(found.values())


def _param_names(raw) -> list[str]:
    """Los nombres (nunca los valores) de los parámetros de un cuerpo de pedido: formulario o JSON. Vacío si no hay o no se lee."""
    if not raw:
        return []
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    text = str(raw).strip()
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return sorted(str(k) for k in data)
    except ValueError:
        pass
    return sorted(parse_qs(text, keep_blank_values=True))


def alert_note(response) -> dict:
    """De un pedido a `setalert.ashx`: el método, el status y los nombres de los parámetros de la URL y del cuerpo. Los valores no se anotan."""
    request = getattr(response, "request", None)
    try:
        body = request.post_data
    except Exception:  # el cuerpo puede no poder leerse (binario)
        body = None
    return {"method": str(getattr(request, "method", "") or "GET").upper(), "status": getattr(response, "status", 0),
            "url_params": sorted(parse_qs(urlsplit(response.url).query, keep_blank_values=True)), "body_params": _param_names(body)}


@contextmanager
def _watch(page):
    """Anota los HTTP >= 400 de páginas del sitio y los pedidos a `setalert.ashx` mientras dura el bloque: (statuses, alerts). Cada alerta es
    `alert_note` (método, status, nombres de parámetros)."""
    statuses: list = []
    alerts: list = []

    def on_response(response) -> None:
        if "setalert.ashx" in response.url:
            alerts.append(alert_note(response))
            return
        host = urlsplit(response.url).hostname or ""
        if response.status >= 400 and host.endswith(SITE_SUFFIX) and response.request.resource_type == "document":
            statuses.append(response.status)

    page.on("response", on_response)
    try:
        yield statuses, alerts
    finally:
        page.remove_listener("response", on_response)


def _goto(page, url: str, statuses: list) -> None:
    for attempt in range(3):
        try:
            response = page.goto(url, wait_until="domcontentloaded")
            break
        except PlaywrightError as e:  # una recarga de la misma página (p. ej. al marcar un radio) puede interrumpir el `goto`
            if attempt == 2 or not _navigating(e):
                raise
            page.wait_for_timeout(500)
    if getattr(response, "status", 200) >= 400:
        statuses.append(response.status)
    _check_site(page, statuses)


def _norm_file(text: str) -> str:
    """Un nombre de archivo para comparar: Computrabajo le saca los espacios."""
    return "".join(text.split()).casefold()


def same_file(a: str, b: str) -> bool:
    """Si dos nombres de archivo son el mismo, sin mirar espacios ni mayúsculas."""
    return _norm_file(a) == _norm_file(b)


def same_name(wanted: str, shown: str) -> bool:
    """Si `shown` (el texto de una fila de la página de CV o de la pantalla final) contiene el archivo `wanted`, sin mirar espacios ni mayúsculas."""
    return _norm_file(wanted) in _norm_file(shown)


def _rows(page, statuses: list) -> list[dict]:
    """Abre la página de CV de la cuenta y devuelve, de cada PDF, `name`, `checked` (es el por defecto), `radio`, `id` y `row`.

    Cada PDF es una fila `ul.fila_tabla`: su nombre es el texto de la fila y su radio `rbPrincipal` está solo en la fila, sin
    `label`. El `id` es el `data-filecandidateid` de `a.it-file` (o el valor del radio si la fila no lo trae): dos copias con el
    mismo nombre tienen ids distintos. «No adjuntar» (valor 0) no cuenta.
    """
    _goto(page, UPLOAD_URL, statuses)
    if _screen(page) != "uploadcv":
        raise _halt("La página de CV de la cuenta terminó en otra pantalla")
    found = []
    for row in page.query_selector_all(CV_ROW):
        radio = row.query_selector(CV_RADIO)
        if radio is not None and radio.get_attribute("value") != "0":
            link = row.query_selector(CV_LINK)
            file_id = (link.get_attribute("data-filecandidateid") if link is not None else None) or radio.get_attribute("value") or ""
            found.append({"name": " ".join(row.inner_text().split()), "checked": radio.is_checked(), "radio": radio, "id": file_id, "row": row})
    return found


def _radios(page, statuses: list) -> list[tuple[str, bool, object]]:
    """(nombre, es el por defecto, radio) de cada PDF de la cuenta (ver `_rows`)."""
    return [(row["name"], row["checked"], row["radio"]) for row in _rows(page, statuses)]


def account_cvs(page) -> list[dict]:
    """Los PDF que hay en la cuenta (`uploadcv`): `[{"name", "default", "id"}]`. Solo lee."""
    with _watch(page) as (statuses, _):
        return [{"name": row["name"], "default": row["checked"], "id": row["id"]} for row in _rows(page, statuses)]


def delete_cv(page, file_id: str, name: str) -> None:
    """Borra de la cuenta el PDF `name` con ese `data-filecandidateid`: un clic en la papelera de su fila, como una persona.

    El sitio abre un cuadro blockUI (`#frmDelete`: «¿Estás seguro que deseás eliminar este currículum?»): se espera a que se vea un
    `#btDelete` (blockUI puede clonar el cuadro, así que cuenta el visible) cuyo cuadro tenga ese texto y se hace clic en él. Si el
    cuadro no aparece o dice otra cosa, se toca `#btCancelDelete` (si se ve) y se frena con la huella, sin tocar nada más. Un `confirm`
    nativo, si lo hubiera, se acepta. Después recarga la página de CV y exige que ese `data-filecandidateid` ya no esté (nunca por
    nombre: puede haber otras copias). Sin `force` ni JavaScript.
    """
    with _watch(page) as (statuses, _):
        row = next((r for r in _rows(page, statuses) if r["id"] == file_id and same_file(name, r["name"])), None)
        if row is None:
            raise _halt(f"El CV «{name}» no está en la cuenta")
        trash = row["row"].query_selector(CV_TRASH)
        if trash is None:
            raise _halt(f"La fila del CV «{name}» no tiene papelera")

        native = []

        def accept(dialog) -> None:
            native.append(True)
            dialog.accept()

        page.on("dialog", accept)
        try:
            try:
                trash.click(timeout=FIELD_WAIT)
            except PlaywrightError as e:  # el borrado puede recargar la página antes de que Playwright confirme el clic
                if not _navigating(e):
                    raise
            if not native:
                _confirm_delete(page, file_id, native)
        finally:
            page.remove_listener("dialog", accept)
        page.wait_for_timeout(1000)  # la recarga o el pedido que sigue al clic
        if any(r["id"] == file_id for r in _rows(page, statuses)):
            raise _halt(f"El CV «{name}» sigue en la cuenta después de borrarlo")


def _visible(page, selector: str) -> list:
    """Los elementos visibles que calzan con el selector; si la página está navegando, ninguno (se vuelve a mirar)."""
    try:
        return [e for e in page.query_selector_all(selector) if e.is_visible()]
    except PlaywrightError as e:
        if not _navigating(e):
            raise
        return []


def _dialog_text(button) -> str:
    """El texto del cuadro al que pertenece un botón: sube por los ancestros (hasta 5 niveles) y devuelve el texto del primero que diga
    «eliminar este curriculum». Se detiene en `#frmDelete` (lo incluye) y no pasa del contenedor de blockUI. Si no lo encuentra, ""."""
    box = _up(button)
    for _ in range(5):
        if box is None:
            break
        classes = (box.get_attribute("class") or "").split()
        if "blockUI" in classes or "blockMsg" in classes:
            break
        text = _plain(box.inner_text() or "")
        if DELETE_TEXT in text:
            return text
        if box.get_attribute("id") == "frmDelete":
            break
        box = _up(box)
    return ""


def _confirm_delete(page, file_id: str, native: list) -> None:
    """Espera el cuadro de confirmación del borrado y hace clic en su `#btDelete` visible. Sin el cuadro, o con otro texto, cierra con
    `#btCancelDelete` (si se ve) y frena con la huella."""
    shown = None
    for _ in range(SETTLE_POLLS):
        if native:  # un `confirm` nativo ya se aceptó: no hay cuadro que tocar
            return
        buttons = _visible(page, DELETE_CONFIRM)
        if buttons:
            shown = buttons[0]
            break
        page.wait_for_timeout(500)
    if shown is not None:
        text = _dialog_text(shown)
        wrong = shown.get_attribute("data-filecandidateid")
        if DELETE_TEXT in text and (not wrong or wrong == file_id):
            try:
                shown.click(timeout=FIELD_WAIT)  # como una persona: sin force ni JavaScript
            except PlaywrightError as e:  # el borrado recarga la página
                if not _navigating(e):
                    raise
            return
    other = shown.query_selector("xpath=..") if shown is not None else next(iter(_visible(page, CV_MODAL)), None)
    shape = fingerprint(page, other)  # antes de cerrar el cuadro: después ya no está
    for cancel in _visible(page, DELETE_CANCEL)[:1]:  # lo cierra y no toca nada más
        try:
            cancel.click(timeout=FIELD_WAIT)
        except PlaywrightError as e:
            if not _navigating(e):
                raise
    what = "apareció un cuadro distinto del de confirmación" if shown is not None else "no apareció el cuadro de confirmación"
    raise _halt(f"Al borrar un CV {what} ({shape})")


def _verify_default(page, statuses: list, name: str) -> None:
    """Recarga la página de CV y exige que el PDF por defecto sea `name` (sin mirar espacios ni mayúsculas)."""
    for label, checked, _ in _radios(page, statuses):
        if checked:
            if same_name(name, label):
                return
            raise _halt(f"El CV por defecto de la cuenta no es «{name}»")
    raise _halt(f"Ningún CV figura como por defecto, y se esperaba «{name}»")


def set_default(page, name: str) -> None:
    """Deja `name` como el CV por defecto de la cuenta y lo verifica recargando; si no está o no quedó, `Halt`."""
    with _watch(page) as (statuses, _):
        for label, checked, radio in _radios(page, statuses):
            if same_name(name, label):
                if not checked:
                    try:
                        radio.check()
                    except PlaywrightError as e:  # marcar el radio recarga la página antes de que Playwright confirme el tilde
                        if not _navigating(e):
                            raise
                        _settle(page, statuses, ("uploadcv", "cvedit"))  # la recarga termina en `uploadcv` o en `candidate/cv/edit`, como la subida
                    page.wait_for_timeout(1000)  # y recién entonces `_verify_default` recarga y exige que haya quedado marcado
                break
        else:
            raise _halt(f"El CV «{name}» no está en la cuenta")
        _verify_default(page, statuses, name)


def upload_cv(page, path: Path, name: str, statuses: list, on_uploaded=None) -> None:
    """Sube `path` en la página de CV (el archivo se manda solo al elegirlo) y verifica que quedó como el por defecto, con `name`.

    `on_uploaded()` avisa en cuanto el sitio terminó de recibir el archivo (llegó a `candidate/cv/edit`), antes de verificar.
    """
    _goto(page, UPLOAD_URL, statuses)
    field = page.query_selector(UPLOAD_INPUT) if _screen(page) == "uploadcv" else None
    if field is None:
        raise _halt("No hay campo para subir el CV en la página de CV de la cuenta")
    try:
        field.set_input_files(str(path))  # el formulario se manda solo: la página navega hasta `candidate/cv/edit`
    except PlaywrightError as e:
        if not _navigating(e):
            raise
    _check_limit(page)  # el aviso puede aparecer sin que la página navegue
    try:
        _settle(page, statuses, ("cvedit",))  # se espera esa navegación antes de leer la página
    except Halt:
        _check_limit(page, in_html=True)
        raise
    if on_uploaded is not None:
        on_uploaded()
    _verify_default(page, statuses, name)


def _check_limit(page, in_html: bool = False) -> None:
    """Si la página dice «número máximo de documentos» (texto visible; con `in_html`, también en el HTML), el sitio no recibió el
    archivo: frena con ese motivo."""
    html = ""
    if in_html:
        try:
            html = page.content() or ""
        except Exception:
            pass
    if LIMIT_TEXT in _plain(_body(page)) or LIMIT_TEXT in _plain(html):
        raise Halt("El sitio dice que la cuenta llegó al máximo de documentos "
                   f"(«{LIMIT_MESSAGE}»): no recibió el CV. Borrá un CV a mano en la página de CV y volvé a correr.")


def _body(page) -> str:
    try:
        return page.inner_text("body") or ""
    except PlaywrightError:
        return ""


def _shown_file(page) -> str:
    found = ATTACHED.search(page.inner_text("body") or "")
    return " ".join(found.group(1).split()) if found else ""


def _verify_attached(page, statuses: list, name: str) -> None:
    """En `postapply`, exige que el archivo adjunto sea `name`; si es otro, `Halt` con el que se ve."""
    for _ in range(SETTLE_POLLS):
        try:
            _check_site(page, statuses)
            body = page.inner_text("body") or ""
        except PlaywrightError as e:  # «Actualizar» recarga la pantalla: se vuelve a mirar
            if not _navigating(e):
                raise
            page.wait_for_timeout(500)
            continue
        if same_name(name, body):
            return
        page.wait_for_timeout(500)
    shown = _shown_file(page)
    raise _halt(f"El archivo adjunto a la postulación no es el subido (se ve «{shown or 'ninguno'}», se esperaba «{name}»)")


def attach_after_posting(page, path: Path, name: str, statuses: list) -> None:
    """En `postapply`, cambia el CV adjunto por `path` («Editar» → archivo → «Actualizar») y verifica el nombre."""
    field, update = page.query_selector(EDIT_INPUT), page.query_selector(EDIT_APPLY)
    if field is None or update is None:
        raise _halt("No hay forma de cambiar el CV adjunto en la pantalla final")
    field.set_input_files(str(path))
    update.click()
    _verify_attached(page, statuses, name)


CONFIRM_BLOCKS = "h1, h2, h3, h4, p, strong, b, span, li, div, section"  # candidatos a «bloque de confirmación» de la pantalla final
CONFIRM_MAX = 300  # un bloque de confirmación es corto: un contenedor grande que trae el texto entre otras cosas no cuenta


def final_print(page) -> str:
    """La huella de una pantalla final: su URL sin parámetros y el título de la página (sin datos personales)."""
    url = urlsplit(page.url)
    try:
        title_tag = page.query_selector("title")
        title = " ".join(((title_tag.text_content() if title_tag else "") or "").split())[:80]
    except PlaywrightError:
        title = ""
    return f"{url.hostname or ''}{url.path}" + (f" · título «{title}»" if title else "")


def _confirmation_block(page) -> bool:
    """Si la pantalla final tiene, **visible**, un bloque corto cuyo texto dice «Te postulaste correctamente»."""
    for element in page.query_selector_all(CONFIRM_BLOCKS):
        text = " ".join((element.text_content() or "").split())
        if SUCCESS_TEXT in text and len(text) <= CONFIRM_MAX and element.is_visible():
            return True
    return False


def _names_offer(page, offer: dict) -> bool:
    """Si la URL final (`oi=`) o un enlace de la página nombran la oferta enviada: su id de 32 hex (el final de la URL del detalle) o su `id16`."""
    tail = re.search(r"[0-9A-Fa-f]{16,32}$", urlsplit(offer.get("url") or "").path)
    ids = {tail.group(0).casefold()} if tail else set()
    if offer.get("id16"):
        ids.add(str(offer["id16"]).casefold())
    oi = (parse_qs(urlsplit(page.url).query).get("oi") or [""])[0].casefold()
    if len(oi) >= 4 and any(i.startswith(oi) or oi.startswith(i) for i in ids):
        return True
    links = " ".join((a.get_attribute("href") or "") for a in page.query_selector_all("a[href]")).casefold()
    return any(i in links for i in ids)


SUCCESS_WAIT = 10.0  # segundos que se reintenta el chequeo de la pantalla final antes de dar `Unconfirmed`
SUCCESS_POLL_MS = 500
DUMP_RAW_MAX = 20000
_now = time.monotonic  # el reloj de la espera; los tests lo cambian


def _success_problem(page, offer: dict | None) -> str | None:
    """Por qué la pantalla final todavía no confirma esta oferta (None si la confirma). Una página que está navegando cuenta como «todavía»."""
    try:
        if not _confirmation_block(page):
            return "La pantalla final no confirma la postulación"
        if offer is not None and not _names_offer(page, offer):
            return "La pantalla final no nombra la oferta enviada"
    except PlaywrightError as e:
        if not _navigating(e):
            raise
        return "La pantalla final todavía estaba cargando"
    return None


def page_dump(page) -> tuple[str, list]:
    """El texto visible de la pantalla (hasta `DUMP_RAW_MAX` caracteres) y los bloques que contienen «postul» con su largo. Solo texto: nunca el
    HTML ni datos de la sesión."""
    try:
        text = (page.inner_text("body") or "")[:DUMP_RAW_MAX]
        blocks = []
        for element in page.query_selector_all(CONFIRM_BLOCKS):
            shown = " ".join((element.text_content() or "").split())
            if "postul" in shown.casefold() and element.is_visible():
                blocks.append((len(shown), shown[:80]))
        return text, blocks
    except PlaywrightError:
        return "", []


def _require_success(page, offer: dict | None = None) -> None:
    """Exige que la pantalla final confirme **esta** oferta: el texto de éxito en un bloque de confirmación visible y la URL o la página
    con el id de la oferta. Reintenta hasta `SUCCESS_WAIT` segundos (el texto puede tardar en aparecer). Si no, `Unconfirmed` con la huella
    (URL sin parámetros y título) y el texto de la pantalla: la oferta no cuenta como enviada."""
    limit = _now() + SUCCESS_WAIT
    while (problem := _success_problem(page, offer)) is not None:
        if _now() >= limit:
            text, blocks = page_dump(page)
            raise Unconfirmed(f"{problem} ({final_print(page)})", text, blocks)
        page.wait_for_timeout(SUCCESS_POLL_MS)


def _posted(page, qa: list, cv: dict | None, alerts: list, before: int = 0, seconds: float = 0.0) -> dict:
    """El resultado de una postulación enviada: el `im` de la URL de `postapply`, si el sitio pidió `setalert.ashx` (`alerts`: método, status y
    nombres de parámetros, con `when` = «antes» o «después» de que se confirmara la pantalla de éxito; `before` es cuántos llegaron antes),
    la huella de la pantalla final y los segundos que pasaron en `postapply`."""
    im = (parse_qs(urlsplit(page.url).query).get("im") or [""])[0]
    noted = [{**a, "when": "antes" if n < before else "después"} for n, a in enumerate(alerts)]
    return {"status": "applied", "qa": qa, "cv": cv, "im": im, "alert": bool(alerts), "alerts": noted, "final": final_print(page),
            "postapply_s": round(seconds, 1)}


def _ensure_attached(page, statuses: list, cv: dict) -> bool:
    """En `postapply`, comprueba que el archivo adjunto es el subido; si no, lo repara desde la misma pantalla y lo verifica.

    Devuelve si hubo que repararlo. Si la reparación no deja el adjunto como el subido, `Halt`.
    """
    for _ in range(2):
        try:
            _check_site(page, statuses)
            if same_name(cv["name"], page.inner_text("body") or ""):
                return False
        except PlaywrightError as e:
            if not _navigating(e):
                raise
        page.wait_for_timeout(500)
    attach_after_posting(page, cv["path"], cv["name"], statuses)
    return True


def apply_one(page, offer: dict, answers_fn, cv_fn=None) -> dict:
    """Postula a una oferta (`offer`: `id16`, `url`) con el navegador de `page` y devuelve `{"status", "qa", "cv", "im", "alert", "repaired"}`.

    Abre el detalle. Si «Ya aplicaste» **se ve** (el texto está siempre en el HTML, oculto), devuelve `already_applied` sin
    tocar «Postularme». Con `cv_fn` (devuelve `{"path", "name"}`: el CV a medida ya copiado con su nombre), arma el CV, lo sube en
    la página de CV (verificando que queda por defecto), vuelve al detalle y **recién entonces** toca «Postularme»: la empresa
    recibe desde el primer momento el CV correcto. Si el sitio dice «Ya te postulaste», devuelve `already_applied`. Sin sesión
    (portal de acceso), con un desafío, un 403/429 o una pantalla desconocida lanza `Halt`. En `candidate/kq` lee las preguntas y
    llama a `answers_fn(questions)` (cada una con `text`, `kind`, `options`): si devuelve `None` (hay una sin respuesta) no
    envía nada y devuelve `pending_question` (el CV ya quedó subido); si no, recibe una respuesta por pregunta (`option` o
    `text`), las carga y envía. Sin pantalla de preguntas también vale. En `postapply` exige «Te postulaste correctamente» y,
    con `cv_fn`, que el archivo adjunto sea el subido; si es otro lo repara desde `postapply` (`repaired`) y, si no puede, `Halt`.
    """
    url = urlsplit(offer["url"])
    if url.hostname != DETAIL_HOST or not url.path.startswith("/ofertas-de-trabajo/"):
        raise _halt("La URL de la oferta no es de un detalle de Computrabajo")
    with _watch(page) as (statuses, alerts):
        _goto(page, offer["url"], statuses)
        if _screen(page) != "detail":
            raise _halt("El detalle de la oferta terminó en otra pantalla")
        box = page.query_selector(ALREADY_BOX)
        if box is not None and box.is_visible() and ALREADY_TEXT in (box.text_content() or ""):
            return {"status": "already_applied", "qa": [], "cv": None, "im": "", "alert": False, "repaired": False}
        button = page.query_selector(APPLY_BUTTON)
        if button is None:
            raise _halt("No hay botón «Postularme» en el detalle")
        cv = None
        if cv_fn is not None:  # el CV a medida queda subido y por defecto ANTES de tocar «Postularme»
            cv = cv_fn()
            if cv.get("reuse"):  # ese PDF ya está en la cuenta (un intento anterior de esta misma oferta): se marca, sin subir otro
                set_default(page, cv["name"])
            else:
                upload_cv(page, cv["path"], cv["name"], statuses, cv.get("uploaded"))  # `uploaded` cuenta el CV apenas se sube
            _goto(page, offer["url"], statuses)
            button = page.query_selector(APPLY_BUTTON) if _screen(page) == "detail" else None
            if button is None:
                raise _halt("No hay botón «Postularme» en el detalle")
        button.click()
        screen = _settle(page, statuses, ("kq", "postapply", "applied"))
        if screen == "applied":
            return {"status": "already_applied", "qa": [], "cv": cv, "im": "", "alert": False, "repaired": False}
        arrived = _now() if screen == "postapply" else None  # desde que se llega a `postapply` se miden los segundos que se queda
        qa = []
        if screen == "kq":
            questions = read_questions(page)
            answers = answers_fn([{key: q[key] for key in ("text", "kind", "options")} for q in questions])
            if answers is None:
                return {"status": "pending_question", "qa": [], "cv": cv, "im": "", "alert": False, "repaired": False}
            if len(answers) != len(questions):
                raise _halt("Las respuestas no coinciden con las preguntas")
            _fill(page, questions, answers)
            submit = page.query_selector(SUBMIT_BUTTON)
            if submit is None:
                raise _halt("No hay botón para enviar las respuestas")
            qa = [{"question": q["text"], "answer": a["options"][a["option"]] if q["kind"] == "closed" else a["text"]}
                  for q, a in zip(questions, answers)]
            submit.click()
            _settle(page, statuses, ("postapply",))
            arrived = _now()
        _check_site(page, statuses)
        _require_success(page, offer)
        before = len(alerts)  # las alertas que llegaron hasta que se confirmó la pantalla de éxito
        repaired = False
        if cv is not None:
            repaired = _ensure_attached(page, statuses, cv)  # la reparación ya verifica el adjunto
        return {**_posted(page, qa, cv, alerts, before, _now() - (arrived if arrived is not None else _now())), "repaired": repaired}
