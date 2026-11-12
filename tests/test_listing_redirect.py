"""TAREA-076: un listado que el sitio redirige (301/308) a su camino canónico se sigue una sola vez; cualquier otra redirección frena."""
import httpx
import pytest

from test_batch import listing_html
from test_source_truths import TRUTHS, closed, go, profile  # noqa: F401
from compuapply import apply, batch, scan

ASKED = "/trabajo-de-ayudante-de-cocina-en-capital-federal"
CANON = "/trabajo-de-ayudante-de-cocin-en-capital-federal"
HOST = "https://ar.computrabajo.com"


def client(routes, asked):
    """`routes[(camino, p)]` = (status, location) o HTML; lo que no está responde 404."""
    def handler(request):
        key = (request.url.path, int(request.url.params["p"]))
        asked.append(key)
        route = routes.get(key)
        if isinstance(route, tuple):
            return httpx.Response(route[0], headers={"location": route[1]} if route[1] else {})
        return httpx.Response(200, text=route) if route else httpx.Response(404)
    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(scan.time, "sleep", lambda s: None)


def collect(routes, pages=1, zones=("Capital Federal",)):
    asked = []
    out = scan.collect("ayudante de cocina", pages, client(routes, asked), zones=zones)
    return out, asked


def test_the_301_to_the_canonical_slug_is_followed_and_the_page_is_read():
    routes = {(ASKED, 1): (301, f"{CANON}?p=1"), (CANON, 1): listing_html(["Ayudante"] * 3, 1)}
    (offers, pages), asked = collect(routes)
    assert len(offers) == 3 and pages == 1 and asked == [(ASKED, 1), (CANON, 1)]


def test_a_308_is_followed_too():
    routes = {(ASKED, 1): (308, f"{HOST}{CANON}?p=1"), (CANON, 1): listing_html(["Ayudante"], 1)}
    (offers, _), _ = collect(routes)
    assert len(offers) == 1


def test_page_2_uses_the_canonical_slug_without_another_301():
    routes = {(ASKED, 1): (301, f"{CANON}?p=1"), (CANON, 1): listing_html(["A"] * 2, 1), (CANON, 2): listing_html(["B"] * 2, 10)}
    (offers, pages), asked = collect(routes, pages=2)
    assert len(offers) == 4 and pages == 2 and asked == [(ASKED, 1), (CANON, 1), (CANON, 2)]


@pytest.mark.parametrize("location", [
    "https://otro.example.com" + CANON + "?p=1",   # otro host
    "/login?p=1",                                   # login
    "/ofertas-de-trabajo/algo?p=1",                 # sin /trabajo-de-
    "/trabajo-de-ayudante-de-cocin?p=1",            # sin el sufijo de zona
    "/trabajo-de-ayudante-de-cocin-en-buenos-aires-gba?p=1",  # otra zona
    CANON,                                          # sin p=
    CANON + "?p=2",                                 # otra página
    "",                                             # sin Location
])
def test_other_redirects_stop(location):
    routes = {(ASKED, 1): (301, location), (CANON, 1): listing_html(["A"], 1)}
    with pytest.raises(RuntimeError, match="respondió 301"):
        collect(routes)


def test_two_301_in_a_row_stop():
    routes = {(ASKED, 1): (301, f"{CANON}?p=1"), (CANON, 1): (301, f"{CANON}-2?p=1")}
    with pytest.raises(RuntimeError, match="respondió 301"):
        collect(routes)


def test_a_302_stops():
    with pytest.raises(RuntimeError, match="respondió 302"):
        collect({(ASKED, 1): (302, f"{CANON}?p=1"), (CANON, 1): listing_html(["A"], 1)})


def test_a_national_search_follows_a_slug_without_zone_but_not_one_with_zone():
    asked = []
    national = "/trabajo-de-ayudante-de-cocina"
    routes = {(national, 1): (301, "/trabajo-de-ayudante-de-cocin?p=1"), ("/trabajo-de-ayudante-de-cocin", 1): listing_html(["A"], 1)}
    offers, _ = scan.collect("ayudante de cocina", 1, client(routes, asked), zones=())
    assert len(offers) == 1
    bad = {(national, 1): (301, "/trabajo-de-ayudante-de-cocin-en-capital-federal?p=1")}
    with pytest.raises(RuntimeError, match="respondió 301"):
        scan.collect("ayudante de cocina", 1, client(bad, []), zones=())


def test_canonical_path_and_site_slug():
    assert scan.canonical_path(f"{CANON}?p=3", 3, "capital-federal") == CANON
    assert scan.canonical_path(f"{CANON}?p=3", 1, "capital-federal") is None
    assert scan.site_slug(CANON, "capital-federal") == "ayudante-de-cocin" and scan.site_slug("/trabajo-de-mozo", None) == "mozo"


def test_the_report_row_says_which_slug_the_site_uses():
    row = {"keyword": "ayudante de cocina", "zone": "Capital Federal", "pages_read": 1, "found": 3, "new": 3, "seen": 0, "in_zone": 3,
           "matching": 3, "sent": 0, "reason": "sin más páginas", "site_slug": "ayudante-de-cocin"}
    text = "\n".join(batch._scan_table([row], False))
    assert "sin más páginas · el sitio usa `ayudante-de-cocin`" in text
    assert "el sitio usa" not in "\n".join(batch._scan_table([{**row, "site_slug": ""}], False))


# ── de paso (D-166) ──────────────────────────────────────────────────────────

def test_a_cv_answer_that_contradicts_a_truth_answers_with_the_truth():  # 085
    entry, _ = go(closed("¿Estás trabajando actualmente?"), profile(TRUTHS), {"option": 0, "source": "cv"})
    assert entry["source"] == "truths" and entry["option"] == 1 and entry["reason"] is None


def test_a_cv_answer_that_agrees_with_the_truth_is_left_alone():
    entry, _ = go(closed("¿Estás trabajando actualmente?"), profile(TRUTHS), {"option": 1, "source": "cv"})
    assert entry["option"] == 1 and entry["source"] in ("cv", "truths")


ADMIN = "¿Contás con experiencia previa realizando tareas administrativas?"


def test_experience_realizando_tareas_takes_the_task_word():
    assert apply._specific_experience({"text": ADMIN}) == ["administrativas"]
    assert apply._specific_experience({"text": "¿Tenés experiencia haciendo reparto de mercadería?"}) == ["reparto", "mercaderia"]


def test_a_yes_to_administrative_tasks_with_a_cv_without_them_becomes_no():
    entry, _ = go(closed(ADMIN, "SI", "NO"), profile(), {"option": 0, "source": "cv"})  # el CV de ejemplo trae «Administrator», no «administrativ»
    assert entry["source"] == "cv" and entry["option"] == 1


def test_with_a_cv_that_names_administrative_work_the_yes_stays():
    prof = profile()
    resume = dict(prof["variants"][0]["resume"])
    resume["experience_details"] = [{"position": "Asistente", "company": "X", "employment_period": "01/2024 - 06/2024",
                                     "key_responsibilities": [{"responsibility": "Tareas administrativas y de archivo"}], "skills_acquired": []}]
    prof["variants"][0] = {**prof["variants"][0], "resume": resume}
    entry, _ = go(closed(ADMIN, "SI", "NO"), prof, {"option": 0, "source": "cv"})
    assert entry["source"] == "cv" and entry["option"] == 0
