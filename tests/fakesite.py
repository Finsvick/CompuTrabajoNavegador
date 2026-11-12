"""Sitio y página falsos para probar la postulación sin navegador: HTML de tests/fixtures/ con BeautifulSoup.

`FakePage` imita solo lo que usa `browser`. El sitio sigue un guion: tocar «Postularme» o el botón de enviar encadena las URLs
que se le indican, una por cada espera (`wait_for_timeout`), como las redirecciones reales. Además lleva la cuenta: los CV
que hay en `uploadcv`, cuál es el por defecto y qué archivo muestra `postapply`.
"""
from pathlib import Path
from types import SimpleNamespace

from bs4 import BeautifulSoup
from playwright.sync_api import TimeoutError as PlaywrightTimeout

FIXTURES = Path(__file__).parent / "fixtures"
DETAIL_URL = "https://ar.computrabajo.com/ofertas-de-trabajo/oferta-de-prueba-AAAAAAAAAAAAAAAA0000000000000000"
MATCH_URL = "https://candidato.ar.computrabajo.com/match/?oi=AAAA"
KQ_URL = "https://candidato.ar.computrabajo.com/candidate/kq?oi=AAAA"
APPLY_URL = "https://candidato.ar.computrabajo.com/apply/?oi=AAAA&idcandidate=1"
POSTAPPLY_URL = "https://candidato.ar.computrabajo.com/candidate/postapply?oi=AAAA&im=IM123&new_user=False"
LOGIN_URL = "https://secure.computrabajo.com/acceso/?rfl=abc"
CHECK_URL = "https://candidato.ar.computrabajo.com/candidate/cv/uploadcv"
CV_EDIT_URL = "https://candidato.ar.computrabajo.com/candidate/cv/edit"
OTHER_URL = "https://candidato.ar.computrabajo.com/candidate/otra"
APPLICATIONS_URL = "https://candidato.ar.computrabajo.com/candidate/match/"
SETALERT_URL = "https://candidato.ar.computrabajo.com/ajax/setalert.ashx"
BASE_CV = "CV-Base.pdf"
UPLOAD_FIELD = "ContainerGeneralOverWrite_ContainerGeneral_fuUploadCv"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def visible(tag) -> bool:
    """Oculto si él o un ancestro tiene la clase `hide`, `hidden` o `display:none`; un `input type="hidden"` tampoco se ve."""
    if (getattr(tag, "attrs", None) or {}).get("type") == "hidden":
        return False
    for node in [tag, *tag.parents]:
        attrs = getattr(node, "attrs", None) or {}
        if "hide" in (attrs.get("class") or []) or "hidden" in attrs or "display:none" in (attrs.get("style") or "").replace(" ", ""):
            return False
    return True


class FakeElement:
    def __init__(self, page, tag):
        self.page, self.tag = page, tag

    def get_attribute(self, name):
        value = self.tag.get(name)
        return " ".join(value) if isinstance(value, list) else value

    def inner_text(self):
        return self.tag.get_text("\n") if visible(self.tag) else ""

    def text_content(self):
        return self.tag.get_text()

    def is_visible(self):
        return visible(self.tag)

    def is_checked(self):
        name = self.tag.get("name")
        if name in self.page.checked:  # lo marcado durante la prueba manda sobre el atributo del HTML
            return self.page.checked[name] == self.tag.get("value")
        return self.tag.has_attr("checked")

    def _needs_to_be_seen(self, action, timeout):
        """Como Playwright: un campo oculto por CSS nunca se ve y la acción espera hasta agotar el tiempo."""
        self.page.calls.append((action, timeout))
        if not visible(self.tag):
            raise PlaywrightTimeout(f"ElementHandle.{action}: Timeout {timeout}ms exceeded.")

    def _tick(self, radio):
        """El radio queda marcado por el clic de una persona (en su label o en su recuadro), salvo que la página lo ignore."""
        if not self.page.clicks_do_not_tick and radio is not None:
            self.page.checked[radio.get("name")] = radio.get("value")
            self.page.log.append(("tick", radio.get("name"), radio.get("value")))

    def query_selector(self, selector):
        if selector == "xpath=..":
            parent = self.tag.parent
            return FakeElement(self.page, parent) if parent is not None and parent.name not in (None, "[document]") else None
        found = self.tag.select_one(selector)
        return FakeElement(self.page, found) if found else None

    def query_selector_all(self, selector):
        return [FakeElement(self.page, tag) for tag in self.tag.select(selector)]

    def check(self, timeout=None, **kwargs):
        assert not kwargs, f"check no lleva {kwargs}"  # nada de force ni de otras opciones
        if self.tag.get("name") != "rbPrincipal":
            self._needs_to_be_seen("check", timeout)
        if self.tag.get("name") == "rbPrincipal":  # los radios de la página de CV
            self.page.site.default_radio(int(self.tag.get("value"), 16))
            self.page.log.append(("default", self.tag.get("value")))
            self.page.site.radio_reload(self.page)
            if self.page.check_error is not None:
                raise self.page.check_error
            return
        self.page.checked[self.tag.get("name")] = self.tag.get("value")
        self.page.log.append(("check", self.tag.get("name"), self.tag.get("value")))

    def fill(self, text, timeout=None, **kwargs):
        assert not kwargs, f"fill no lleva {kwargs}"
        self._needs_to_be_seen("fill", timeout)
        self.page.filled[self.tag.get("name")] = text
        self.page.log.append(("fill", self.tag.get("name"), text))

    def set_input_files(self, path):
        assert Path(path).is_file(), f"el archivo a subir no existe: {path}"
        self.page.log.append(("upload", Path(path).name))
        self.page.site.file_chosen(self.page, self.tag.get("id"), Path(path).name)

    def click(self, timeout=None, **kwargs):
        assert not kwargs, f"click no lleva {kwargs}"  # nada de force
        self._needs_to_be_seen("click", timeout)
        self.page.log.append(("click", self.tag.name, self.tag.get_text(strip=True)))
        if self.tag.name == "label" and self.tag.get("for"):
            self._tick(self.page.soup.find(id=self.tag.get("for")))
        elif self.tag.name == "div" and self.tag.find("input", attrs={"type": "radio"}) is not None:  # el recuadro de una opción
            self._tick(self.tag.find("input", attrs={"type": "radio"}))
        self.page.site.clicked(self.page, self.tag)


class FakeSite:
    """`pages`: URL -> HTML. `after_apply` / `after_submit`: las URLs que se encadenan al tocar «Postularme» / enviar.

    La cuenta: `cvs` (los PDF de `uploadcv`), `default`, `uploads` (los nombres que se subieron) y `attached` (lo que se ve en
    `postapply`). Con `check_changes_default=False` marcar un radio no cambia el por defecto; con `edit_changes=False`,
    «Actualizar» en `postapply` no cambia el adjunto; `attached_override` fuerza el archivo que muestra `postapply`.
    """

    def __init__(self, pages: dict, after_apply=(MATCH_URL, KQ_URL), after_submit=(APPLY_URL, POSTAPPLY_URL), statuses=None,
                 cvs=(BASE_CV,), default=BASE_CV, check_changes_default=True, edit_changes=True, attached_override=None, setalert=True,
                 upload_makes_default=True, max_docs=None, delete_mode="blockui", lists_posted=True):
        self.pages, self.after_apply, self.after_submit, self.statuses = dict(pages), list(after_apply), list(after_submit), statuses or {}
        # `goto(match/)` (la comprobación previa) sigue las mismas redirecciones que el botón «Postularme»
        self.chain = list(after_apply[1:]) if after_apply and after_apply[0] == MATCH_URL else list(after_apply)
        self.cvs, self.default = list(cvs), default
        self.check_changes_default, self.edit_changes, self.attached_override, self.setalert = check_changes_default, edit_changes, attached_override, setalert
        self.upload_makes_default = upload_makes_default
        self.alert_status = 200
        self.alert_url, self.alert_method, self.alert_body = SETALERT_URL + "?lang=es&t=1", "GET", None  # el pedido a setalert.ashx (con 2 parámetros en la URL)
        self.lists_posted, self.posted = lists_posted, False  # como el sitio real: «Mis postulaciones» lista lo que se postuló (im de POSTAPPLY_URL)
        self.radio_reloads_to_cvedit = False
        self.queue: list[str] = []
        self.submitted: list[dict] = []
        self.uploads: list[str] = []
        self.pending_attach = None
        self.attached = None
        # `max_docs`: cuántos PDF admite la cuenta (con más, el sitio avisa y no recibe el archivo). `delete_mode`: qué pasa al tocar la
        # papelera: "blockui" (el cuadro `#frmDelete` del sitio; borra al tocar `#btDelete`), "blockui_other" (un cuadro con otro texto), "blockui_stays"
        # (el cuadro correcto, pero `#btDelete` no borra), "confirm" (un confirm nativo), "silent" (borra sin preguntar), "modal" (un cuadro
        # desconocido, no borra) o "stays" (no pasa nada).
        self.max_docs, self.delete_mode, self.limit_hit = max_docs, delete_mode, False
        self.file_ids: list[str] = []  # el data-filecandidateid de cada PDF de `cvs` (copias con el mismo nombre tienen ids distintos)
        self.next_id = 1
        self.deleted: list[str] = []
        self.cancelled = 0  # cuántas veces se tocó `#btCancelDelete`

    def _sync_ids(self):
        while len(self.file_ids) < len(self.cvs):
            self.file_ids.append(f"fc{self.next_id:04d}")
            self.next_id += 1

    def delete_cv(self, file_id):
        self._sync_ids()
        at = self.file_ids.index(file_id)
        name = self.cvs.pop(at)
        self.file_ids.pop(at)
        self.deleted.append(name)
        if self.default == name and name not in self.cvs:
            self.default = None

    def open_box(self, page, file_id, text="¿Estás seguro que deseás eliminar este currículum?"):
        """Como blockUI: un clon visible del cuadro `#frmDelete` (el original sigue oculto en la página), con el id de la fila."""
        page.soup.body.append(BeautifulSoup(
            f'<div class="blockUI blockMsg"><div id="frmDelete" class="popup_gestof popup_gestcan">'
            '<div class="md-modal md-effect-3 md-show"><div class="md-content"><div class="modal-p"><h3>Eliminando curriculum...</h3>'
            f'<div class="triangulo_inf"></div><p>{text}</p><div class="submitbtn">'
            f'<input type="button" id="btDelete" class="submit_n" data-filecandidateid="{file_id}" value="Eliminar">'
            '<input type="button" id="btCancelDelete" class="cancelar_link" value="Cancelar"></div></div></div></div></div></div>', "html.parser"))

    def close_box(self, page):
        for box in page.soup.select(".blockMsg"):
            box.decompose()

    def html_for(self, url, keep=False):
        if url == CHECK_URL:
            self._sync_ids()
            rows = []
            for n, name in enumerate(self.cvs, 1):  # una fila por PDF: el radio solo, sin label, y el nombre como texto de la fila
                fid = self.file_ids[n - 1]
                rows.append(f'<ul class="fila_tabla"><li class="tableof_col1"><input type="radio" id="rb{n}" name="rbPrincipal" '
                            f'value="{n:032x}"{" checked" if name == self.default else ""}></li>'
                            f'<li class="tableof_col2"><a class="it-file" data-filecandidateid="{fid}">{name}</a></li>'
                            f'<li class="tableof_col3"><span class="icon papelera" id="it-delete" data-filecandidateid="{fid}" '
                            f'title="Eliminar la postulación"></span></li></ul>')
            page = fixture("apply_uploadcv.html").replace("{{ROWS}}", "".join(rows))
            if self.limit_hit:
                page = page.replace("</form>", '<div class="alert">Se ha alcanzado el número máximo de documentos permitido</div></form>')
            return page
        page = self.pages.get(url, "<html><body></body></html>")
        if url == APPLICATIONS_URL and self.posted and self.lists_posted:
            page = page.replace("</body>", '<div class="box_border" data-match="IM123" data-targeturl="/candidate/postapply?oi=AAAA&im=IM123">'
                                '<h1>Postulada por el programa</h1><p class="fc_base mt5">Empresa del programa</p><p class="fwB">Postulado</p></div></body>')
        if url == POSTAPPLY_URL:
            if not keep:  # al llegar a la pantalla final se adjunta el CV por defecto del momento
                self.attached = self.default
            page = page.replace("{{FILE}}", self.attached_override or self.attached)
        return page

    def load(self, page, url, keep=False):
        if url == POSTAPPLY_URL:
            self.posted = True
        page.url = url
        page.soup = BeautifulSoup(self.html_for(url, keep), "html.parser")
        if url == MATCH_URL and not self.queue:
            self.queue = list(self.chain)
        status = self.statuses.get(url, 200)
        responses = [(url, status, "document", "GET", None)]
        if url == POSTAPPLY_URL and self.setalert:
            responses.append((self.alert_url, self.alert_status, "xhr", self.alert_method, self.alert_body))
        for address, code, kind, method, body in responses:
            for handler in list(page.handlers):
                handler(SimpleNamespace(url=address, status=code,
                                        request=SimpleNamespace(resource_type=kind, method=method, post_data=body)))
        return SimpleNamespace(status=status)

    def default_radio(self, number):
        if self.check_changes_default:
            self.default = self.cvs[number - 1]

    def radio_reload(self, page):
        """Marcar un radio recarga la página y termina en `candidate/cv/edit` (con `radio_reloads_to_cvedit`)."""
        if self.radio_reloads_to_cvedit:
            self.load(page, CV_EDIT_URL)

    def file_chosen(self, page, field_id, name):
        if field_id == UPLOAD_FIELD and self.max_docs is not None and len(self.cvs) >= self.max_docs:
            self.limit_hit = True  # el sitio no recibe el archivo: la página de CV sigue ahí, con el aviso
            self.load(page, CHECK_URL)
        elif field_id == UPLOAD_FIELD:  # la página de CV manda el archivo solo y redirige a candidate/cv/edit
            self.uploads.append(name)
            self.cvs.append(name)
            if self.upload_makes_default:
                self.default = name
            self.queue = [CV_EDIT_URL]
        else:  # `fuUploadCv` de postapply: el archivo espera a «Actualizar»
            self.pending_attach = name

    def clicked(self, page, tag):
        if "papelera" in (tag.get("class") or []):
            if self.delete_mode == "modal":
                page.soup.body.append(BeautifulSoup('<div class="modal"><p>Texto del cuadro</p><button type="button">Aceptar</button></div>',
                                                    "html.parser"))
            elif self.delete_mode in ("blockui", "blockui_stays"):
                self.open_box(page, tag.get("data-filecandidateid"))
            elif self.delete_mode == "blockui_other":
                self.open_box(page, tag.get("data-filecandidateid"), "¿Querés cerrar la sesión?")
            elif self.delete_mode == "confirm":
                accepted = []
                for handler in list(page.dialog_handlers):
                    handler(SimpleNamespace(accept=lambda: accepted.append(True)))
                if accepted:
                    self.delete_cv(tag.get("data-filecandidateid"))
            elif self.delete_mode == "silent":
                self.delete_cv(tag.get("data-filecandidateid"))
        elif tag.get("id") == "btDelete":
            if self.delete_mode != "blockui_stays":
                self.delete_cv(tag.get("data-filecandidateid"))
            self.close_box(page)
        elif tag.get("id") == "btCancelDelete":
            self.cancelled += 1
            self.close_box(page)
        elif tag.get("data-href-offer-apply") is not None:
            self.queue = list(self.after_apply)
        elif tag.get("id") == "updateAttached" and self.edit_changes and self.pending_attach:
            self.uploads.append(self.pending_attach)
            self.cvs.append(self.pending_attach)
            self.default = self.attached = self.pending_attach
            self.attached_override = None
            self.pending_attach = None
            self.load(page, page.url, keep=True)
        elif tag.get("type") == "submit":
            self.submitted.append({"checked": dict(page.checked), "filled": dict(page.filled)})
            self.queue = list(self.after_submit)


NAVIGATING_ERROR = "Page.query_selector: Execution context was destroyed, most likely because of a navigation"


class FakeClock:
    """El reloj de los tests: avanza solo con `wait_for_timeout` de la página falsa (nunca se espera de verdad)."""

    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t


CLOCK = FakeClock()


class FakePage:
    """`navigation_errors`: cuántas lecturas fallan, como en Playwright, mientras la página está navegando (`queue` con pasos);
    `goto_errors`: URL -> excepción que lanza `goto`; `check_error`: lo que lanza `check()` de un radio después de marcarlo."""

    def __init__(self, site: FakeSite):
        self.site, self.url, self.soup = site, "about:blank", BeautifulSoup("", "html.parser")
        self.handlers, self.log, self.checked, self.filled, self.visited = [], [], {}, {}, []
        self.dialog_handlers = []  # los `page.on("dialog", …)
        self.navigation_errors, self.goto_errors, self.check_error = 0, {}, None
        self.calls = []  # (acción, timeout) de cada check, fill y click sobre un campo
        self.clicks_do_not_tick = False  # el clic en un label o recuadro no marca el radio
        self.goto_interruptions = 0  # cuántos `goto` seguidos interrumpe otra navegación antes de que uno funcione

    def _reading(self):
        if self.navigation_errors and self.site.queue:
            self.navigation_errors -= 1
            from playwright.sync_api import Error
            raise Error(NAVIGATING_ERROR)

    def goto(self, url, wait_until=None):
        self.visited.append(url)
        if url in self.goto_errors:
            raise self.goto_errors[url]
        if self.goto_interruptions:
            self.goto_interruptions -= 1
            from playwright.sync_api import Error
            raise Error(f'Page.goto: Navigation to "{url}" is interrupted by another navigation to "{url}"')
        return self.site.load(self, url)

    def on(self, event, handler):
        (self.dialog_handlers if event == "dialog" else self.handlers).append(handler)

    def remove_listener(self, event, handler):
        (self.dialog_handlers if event == "dialog" else self.handlers).remove(handler)

    def wait_for_timeout(self, ms):
        CLOCK.t += ms / 1000  # el reloj falso avanza con cada espera
        if self.site.queue:
            self.site.load(self, self.site.queue.pop(0))

    def query_selector(self, selector):
        self._reading()
        tag = self.soup.select_one(selector)
        return FakeElement(self, tag) if tag else None

    def query_selector_all(self, selector):
        self._reading()
        return [FakeElement(self, tag) for tag in self.soup.select(selector)]

    def inner_text(self, selector):
        self._reading()
        tag = self.soup.select_one(selector)
        return tag.get_text("\n") if tag else ""

    def clicks(self) -> list:
        return [entry for entry in self.log if entry[0] == "click"]


def standard_site(**over) -> FakeSite:
    pages = {DETAIL_URL: fixture("apply_detail.html"), KQ_URL: fixture("apply_kq.html"), POSTAPPLY_URL: fixture("apply_postapply.html"),
             MATCH_URL: "<html><body>redirigiendo</body></html>", APPLY_URL: "<html><body>enviando</body></html>",
             CV_EDIT_URL: "<html><body>Mi currículum</body></html>", OTHER_URL: fixture("apply_other.html"),
             APPLICATIONS_URL: fixture("apply_applications.html")}
    pages.update(over.pop("pages", {}))
    return FakeSite(pages, **over)
