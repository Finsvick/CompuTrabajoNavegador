"""Tests de los arreglos de la primera corrida en vivo (TAREA-040). Playwright, sitio y cliente falsos: nunca un navegador real."""
import shutil
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from playwright.sync_api import Error as PlaywrightError

import fakesite
from fakesite import APPLY_URL, BASE_CV, CHECK_URL, DETAIL_URL, MATCH_URL, POSTAPPLY_URL, FakePage, fixture, standard_site
from test_apply_live import (CV_NAME, MODEL, TODAY, FakeClient, add_offer, conn, known_reply, live_env, make_profile, rows,  # noqa: F401
                             status_of, stub_cv, write_profile_with)
from compuapply import apply, browser, cli, cv, db, profile, ui_logic

NO_QUESTIONS = (MATCH_URL, APPLY_URL, POSTAPPLY_URL)
CONTEXT_DESTROYED = "Page.evaluate: Execution context was destroyed, most likely because of a navigation"


def site(**over):
    return standard_site(after_apply=NO_QUESTIONS, **over)


def run(conn, tmp_path, page_site, *, prof=None, client=None, provider=None, pw=None, page=None):
    page = page or FakePage(page_site)
    result = apply.run_live(conn, prof or make_profile(), MODEL, client, page, TODAY, stop_file=tmp_path / "STOP", pause=lambda: None,
                            cv_provider=provider or stub_cv(), pw=pw)
    return result, page


# ── PDF con el Playwright que ya está abierto ────────────────────────────────

class FakePlaywright:
    """Un Playwright abierto: `chromium.launch(headless=True)` da un navegador que imprime un PDF mínimo."""

    def __init__(self):
        self.launches, self.pdfs, self.closed = [], [], 0
        self.chromium = SimpleNamespace(launch=self.launch)

    def launch(self, **kwargs):
        self.launches.append(kwargs)
        outer = self

        class Page:
            def goto(self, uri):
                self.uri = uri

            def pdf(self, path, **kwargs):
                Path(path).write_bytes(b"%PDF-" + b"0" * 100)
                outer.pdfs.append(path)

        class Context:
            routed = []

            def route(self, pattern, handler):
                self.routed.append(pattern)

            def new_page(self):
                return Page()

        return SimpleNamespace(new_context=Context, close=lambda: setattr(outer, "closed", outer.closed + 1))


@pytest.fixture
def opened(monkeypatch):
    """Un `sync_playwright` ya abierto: abrir otro desde `cv` falla como en Playwright («Sync API inside the asyncio loop»)."""
    state = {"active": 0, "opened": 0}
    pw = FakePlaywright()

    @contextmanager
    def sync_playwright():
        if state["active"]:
            raise RuntimeError("It looks like you are using Playwright Sync API inside the asyncio loop. Please use the Async API instead.")
        state["active"] += 1
        state["opened"] += 1
        try:
            yield pw
        finally:
            state["active"] -= 1

    monkeypatch.setattr(cv, "sync_playwright", sync_playwright)
    with sync_playwright() as outer:  # el de `apply --live`
        yield SimpleNamespace(pw=outer, state=state)


def fake_cv_make(monkeypatch, written=None):
    """`cv.make` falso que imprime el PDF con el `render` que le pasan, o con `cv.render_pdf` (el real) si no le pasan ninguno."""
    def make(connection, prof, client, model, wanted, out_dir=None, stop_file=None, render=None):
        html = Path(out_dir) / f"{wanted}.html"
        html.parent.mkdir(parents=True, exist_ok=True)
        html.write_text('<div class="headline">Desarrollador de pruebas</div>', encoding="utf-8")
        pdf = html.with_suffix(".pdf")
        (render or cv.render_pdf)(html, pdf)
        if written is not None:
            written.append(pdf)
        return {"html": html, "pdf": pdf, "version": "c8", "cost_usd": 0.0}

    monkeypatch.setattr(cv, "make", make)


def test_run_live_prints_the_cv_pdf_with_the_playwright_already_open_and_reaches_the_upload(conn, tmp_path, monkeypatch, opened):
    add_offer(conn)
    written = []
    fake_cv_make(monkeypatch, written)
    the_site = site()
    result = apply.run_live(conn, make_profile(), MODEL, FakeClient(known_reply()), FakePage(the_site), TODAY, stop_file=tmp_path / "STOP",
                            pause=lambda: None, cv_dir=tmp_path / "cvs", pw=opened.pw)
    assert result["halted"] is None and [i["id16"] for i in result["sent"]] == [f"{1:016X}"]
    assert the_site.uploads == ["CV-Giovanni-Bianchi-X.pdf"]  # llegó a la subida, con el puesto genérico (la búsqueda)
    assert written and opened.state["opened"] == 1  # nadie abrió otro sync_playwright
    assert opened.pw.launches == [{"headless": True}] and opened.pw.pdfs and opened.pw.closed == 1


def test_without_the_open_playwright_the_pdf_would_open_another_one_and_fail(conn, tmp_path, monkeypatch, opened):
    add_offer(conn)
    fake_cv_make(monkeypatch)
    the_site = site()
    result = apply.run_live(conn, make_profile(), MODEL, FakeClient(known_reply()), FakePage(the_site), TODAY, stop_file=tmp_path / "STOP",
                            pause=lambda: None, cv_dir=tmp_path / "cvs")  # como antes: sin `pw`
    assert "Sync API inside the asyncio loop" in result["halted"] and the_site.uploads == [] and result["sent"] == []


def test_render_pdf_without_pw_still_opens_its_own_playwright(tmp_path, monkeypatch):
    pw, opened = FakePlaywright(), []

    @contextmanager
    def own():
        opened.append(1)
        yield pw

    html = tmp_path / "a.html"
    html.write_text("<p>x</p>", encoding="utf-8")
    cv.render_pdf(html, tmp_path / "a.pdf", playwright=own)
    assert opened == [1] and pw.launches == [{"headless": True}] and (tmp_path / "a.pdf").is_file()
    cv.render_pdf(html, tmp_path / "b.pdf", pw=pw, playwright=lambda: pytest.fail("no tenía que abrir otro Playwright"))
    assert (tmp_path / "b.pdf").is_file() and opened == [1]


def test_make_cv_provider_hands_the_open_playwright_to_cv_make_only_when_it_has_one(conn, tmp_path, monkeypatch):
    id16 = add_offer(conn)
    seen = []

    def make(connection, prof, client, model, wanted, out_dir=None, stop_file=None, **kwargs):
        seen.append(kwargs)
        html = Path(out_dir) / "x.html"
        html.parent.mkdir(parents=True, exist_ok=True)
        html.write_text('<div class="headline">Analista</div>', encoding="utf-8")
        pdf = html.with_suffix(".pdf")
        pdf.write_bytes(b"%PDF-" + b"0" * 100)
        return {"html": html, "pdf": pdf, "version": "c8", "cost_usd": 0.0}

    monkeypatch.setattr(cv, "make", make)
    for pw in (None, FakePlaywright()):
        info = apply.make_cv_provider(conn, make_profile(), FakeClient(), MODEL, tmp_path / "salida", pw=pw)({"id16": id16, "variant": "dev"})
        shutil.rmtree(info["dir"])
    assert seen[0] == {} and list(seen[1]) == ["render"]


def test_cli_apply_live_gives_run_live_the_playwright_it_opened(live_env, tmp_path, monkeypatch):
    seen = {}
    real = apply.run_live

    def spy(*args, **kwargs):
        seen["pw"] = kwargs.get("pw")
        return real(*args, **kwargs)

    monkeypatch.setattr(apply, "run_live", spy)
    add_offer(live_env.conn)
    assert cli.main(["apply", "--live"]) == 0
    assert seen["pw"] is not None and hasattr(seen["pw"].chromium, "launch_persistent_context")


# ── nombres en uploadcv ──────────────────────────────────────────────────────

def test_account_cvs_gives_the_names_and_file_ids_of_the_rows_and_never_the_no_attach_option():
    the_site = standard_site(cvs=(BASE_CV, CV_NAME), default=CV_NAME)
    page = FakePage(the_site)
    assert browser.account_cvs(page) == [{"name": BASE_CV, "default": False, "id": "fc0001"}, {"name": CV_NAME, "default": True, "id": "fc0002"}]
    html = fixture("apply_uploadcv.html")
    assert 'class="fila_tabla"' in html and 'id="rbNoCV"' in html and 'name="rbPrincipal"' in html and "<label" not in html


def test_the_names_come_from_the_text_of_the_row_even_if_the_row_has_more_cells():
    the_site = standard_site(cvs=("CV-Uno.pdf",), default="CV-Uno.pdf")
    original = the_site.html_for
    the_site.html_for = lambda url, keep=False: original(url, keep).replace("CV-Uno.pdf</li>", "CV-Uno.pdf</li><li>03/11/2026</li>")
    page = FakePage(the_site)
    [found] = browser.account_cvs(page)
    assert "CV-Uno.pdf" in found["name"] and found["default"] is True
    browser.set_default(page, "cv- uno.PDF")  # sin mirar espacios ni mayúsculas


def test_set_default_checks_the_asked_radio_and_verify_default_accepts_the_uploaded_one():
    the_site = standard_site(cvs=(BASE_CV, "CV-Otro.pdf"), default="CV-Otro.pdf")
    page = FakePage(the_site)
    browser.set_default(page, BASE_CV)
    assert the_site.default == BASE_CV and ("default", f"{1:032x}") in page.log
    with browser._watch(page) as (statuses, _):
        browser._verify_default(page, statuses, BASE_CV)
        with pytest.raises(browser.Halt, match="no es «CV-Otro.pdf»"):
            browser._verify_default(page, statuses, "CV-Otro.pdf")
    with pytest.raises(browser.Halt, match="no está en la cuenta"):
        browser.set_default(page, "CV-No-Existe.pdf")


def test_upload_cv_accepts_the_uploaded_name_as_the_default(tmp_path):
    the_site = standard_site()
    page = FakePage(the_site)
    pdf = tmp_path / CV_NAME
    pdf.write_bytes(b"%PDF-" + b"0" * 100)
    with browser._watch(page) as (statuses, _):
        browser.upload_cv(page, pdf, CV_NAME, statuses)
    assert the_site.default == CV_NAME and the_site.uploads == [CV_NAME]


# ── espera de la subida ──────────────────────────────────────────────────────

def test_a_context_destroyed_error_while_waiting_for_the_upload_does_not_halt(tmp_path):
    the_site = standard_site()
    page = FakePage(the_site)
    page.navigation_errors = 3  # las lecturas fallan mientras la página navega hacia candidate/cv/edit
    pdf = tmp_path / CV_NAME
    pdf.write_bytes(b"%PDF-" + b"0" * 100)
    with browser._watch(page) as (statuses, _):
        browser.upload_cv(page, pdf, CV_NAME, statuses)
    assert page.navigation_errors < 3 and the_site.default == CV_NAME  # al menos un error se tragó y la subida terminó bien


def test_run_live_gets_through_a_destroyed_context_during_the_upload_and_posts(conn, tmp_path):
    add_offer(conn)
    page = FakePage(site())
    original = page.wait_for_timeout

    def navigating(ms):  # arma los errores cuando empieza la subida, no antes
        if page.log and page.log[-1][0] == "upload":
            page.navigation_errors = 2
        original(ms)

    page.wait_for_timeout = navigating
    page.navigation_errors = 0
    result, _ = run(conn, tmp_path, None, page=page)
    assert result["halted"] is None and len(result["sent"]) == 1


@pytest.mark.parametrize("message", ["Page.evaluate: Navigation to \"https://x/\" is interrupted by another navigation to \"https://y/\"",
                                     "Navigation interrupted"])
def test_an_interrupted_navigation_error_also_counts_as_still_navigating(message):
    assert browser._navigating(PlaywrightError(message)) and browser._navigating(PlaywrightError(CONTEXT_DESTROYED))


@pytest.mark.parametrize("error", [PlaywrightError("Target page, context or browser has been closed"), PlaywrightError("Timeout 30000ms exceeded"),
                                   ValueError("Execution context was destroyed")])
def test_any_other_error_is_not_still_navigating(error):
    assert not browser._navigating(error)


def test_another_playwright_error_while_waiting_halts_the_wait():
    page = FakePage(standard_site())
    page.url = fakesite.CV_EDIT_URL

    def broken(selector):
        raise PlaywrightError("Target page, context or browser has been closed")

    page.query_selector = broken
    with pytest.raises(PlaywrightError, match="has been closed"):
        browser._settle(page, [], ("cvedit",))


def test_a_destroyed_context_when_checking_the_radio_is_not_an_error_the_reload_decides():
    the_site = standard_site(cvs=(BASE_CV, "CV-Otro.pdf"), default="CV-Otro.pdf")
    page = FakePage(the_site)
    page.check_error = PlaywrightError(CONTEXT_DESTROYED)
    browser.set_default(page, BASE_CV)
    assert the_site.default == BASE_CV
    page.check_error = PlaywrightError("otra cosa")
    the_site.default = "CV-Otro.pdf"
    with pytest.raises(PlaywrightError, match="otra cosa"):
        browser.set_default(page, BASE_CV)


# ── errores en el informe ────────────────────────────────────────────────────

def report_text(result, tmp_path, meta=None):
    meta = meta or {"now": datetime(2026, 11, 4, 10, 0, 0), "threshold": 70, "daily_cap": 10, "applied_today": 0, "model": MODEL}
    return apply.write_live_report(result, meta, tmp_path / "runs").read_text(encoding="utf-8")


def test_an_unexpected_error_outside_a_wait_halts_and_the_report_shows_its_first_line(conn, tmp_path):
    add_offer(conn)
    page = FakePage(site())
    page.goto_errors[DETAIL_URL] = PlaywrightError(f"Page.goto: net::ERR_FAILED at https://ar.computrabajo.com/x?token=SECRETO\nCall log:\n  - muchas líneas")
    result, _ = run(conn, tmp_path, None, page=page)
    assert result["sent"] == [] and "error inesperado del navegador (Error: Page.goto: net::ERR_FAILED at https://ar.computrabajo.com/x)" in result["halted"]
    assert "SECRETO" not in result["halted"] and "Call log" not in result["halted"]
    assert "error inesperado del navegador (Error: Page.goto: net::ERR_FAILED" in report_text(result, tmp_path)


def test_the_error_of_the_restoration_shows_its_class_and_first_line_too(conn, tmp_path):
    add_offer(conn)
    the_site = site()
    page = FakePage(the_site)
    page.check_error = PlaywrightError("radio roto https://x/y?q=1\nsegunda línea")
    result, _ = run(conn, tmp_path, None, page=page, prof=make_profile(default_pdf_name=BASE_CV))
    assert len(result["sent"]) == 1 and result["restore"]["ok"] is False
    assert result["restore"]["message"] == "error inesperado del navegador (Error: radio roto https://x/y)"
    assert "radio roto https://x/y)" in result["halted"] and "segunda" not in result["halted"] and "q=1" not in result["halted"]


def test_describe_error_has_the_class_the_first_line_and_no_query_strings():
    assert apply.describe_error(ValueError("uno ?x=1\ndos")) == "ValueError: uno ?x=1"
    assert apply.describe_error(ValueError("ve https://a.b/c?d=e#f ya")) == "ValueError: ve https://a.b/c ya"
    assert apply.describe_error(KeyError()) == "KeyError" and apply.describe_error(RuntimeError("  ")) == "RuntimeError"
    assert len(apply.describe_error(ValueError("x" * 500))) <= len("ValueError: ") + 200


def test_the_error_of_reading_the_account_before_starting_is_reported_not_raised(conn, tmp_path):
    add_offer(conn)
    page = FakePage(site())
    page.goto_errors[fakesite.CHECK_URL] = PlaywrightError("Page.goto: Timeout 30000ms exceeded.\nCall log:\n  - x")
    result, _ = run(conn, tmp_path, None, page=page)
    assert "Error: Page.goto: Timeout 30000ms exceeded." in result["halted"] and result["sent"] == [] and "Call log" not in result["halted"]


# ── costo ────────────────────────────────────────────────────────────────────

def test_the_cost_adds_the_cvs_that_failed_from_api_spend(conn, tmp_path):
    id16 = add_offer(conn)

    def failing(item):
        db.add_spend(conn, "cv", item["id16"], MODEL, 0.0008, 800, 100, False)  # dos pedidos pagos del CV que no sirvieron
        db.add_spend(conn, "cv", item["id16"], MODEL, 0.0005, 500, 100, False)
        raise RuntimeError("El CV no pasó los controles.")

    result, _ = run(conn, tmp_path, site(), provider=failing)
    assert result["sent"] == [] and result["halted"] == "El CV no pasó los controles."
    assert result["cost_usd"] == pytest.approx(0.0013)
    assert "- Costo (respuestas y CVs armados, también los que fallaron): US$ 0,0013" in report_text(result, tmp_path)
    assert status_of(conn, id16) == "new"


def test_the_cost_does_not_count_what_was_spent_before_the_run(conn, tmp_path):
    db.add_spend(conn, "cv", "ABC", MODEL, 0.5, 1, 1, True)
    add_offer(conn)
    result, _ = run(conn, tmp_path, site())
    assert result["cost_usd"] == 0.0


# ── CV por defecto al empezar ────────────────────────────────────────────────

def test_with_the_wrong_default_at_the_start_it_is_restored_before_the_first_offer(conn, tmp_path):
    add_offer(conn)
    the_site = site(cvs=(BASE_CV, "CV-De-Una-Oferta-Vieja.pdf"), default="CV-De-Una-Oferta-Vieja.pdf")
    result, page = run(conn, tmp_path, the_site, prof=make_profile(default_pdf_name=BASE_CV))
    assert result["halted"] is None and len(result["sent"]) == 1
    first_default = next(n for n, entry in enumerate(page.log) if entry[0] == "default")
    first_upload = next(n for n, entry in enumerate(page.log) if entry[0] == "upload")
    assert first_default < first_upload  # restaurado antes de subir el CV de la primera oferta
    assert page.visited.index(CHECK_URL) < page.visited.index(DETAIL_URL)
    start = result["start_default"]
    assert (start["name"], start["was"], start["restored"], start["ok"]) == (BASE_CV, "CV-De-Una-Oferta-Vieja.pdf", True, True)
    text = report_text(result, tmp_path)
    assert f"estaba «CV-De-Una-Oferta-Vieja.pdf»; se restauró «{BASE_CV}» antes de la primera oferta" in text
    assert the_site.default == BASE_CV  # y al terminar vuelve a quedar el base


def test_with_the_right_default_at_the_start_nothing_is_changed_and_the_report_says_so(conn, tmp_path):
    add_offer(conn)
    result, page = run(conn, tmp_path, site(), prof=make_profile(default_pdf_name="cv- base.PDF"))
    assert result["start_default"]["restored"] is False and result["start_default"]["ok"] is True
    assert all(entry[0] != "default" for entry in page.log[:page.log.index(("upload", CV_NAME))])  # no se tocó ningún radio antes de subir
    assert f"ya era «cv- base.PDF» (declarado en `default_pdf_name`)" in report_text(result, tmp_path)


def test_when_the_default_cannot_be_restored_it_halts_without_posting_or_uploading(conn, tmp_path):
    id16 = add_offer(conn)
    the_site = site(cvs=(BASE_CV, "CV-Otro.pdf"), default="CV-Otro.pdf", check_changes_default=False)
    result, page = run(conn, tmp_path, the_site, prof=make_profile(default_pdf_name=BASE_CV))
    assert "No se pudo dejar «CV-Base.pdf» como CV por defecto antes de empezar" in result["halted"] and "No se postuló a nada" in result["halted"]
    assert result["sent"] == [] and the_site.uploads == [] and DETAIL_URL not in page.visited and page.clicks() == []
    assert [i["reason"] for i in result["skipped"]] == ["sin procesar: no se pudo dejar el CV por defecto de la cuenta"]
    assert result["start_default"]["ok"] is False and result["restore"] is None and status_of(conn, id16) == "new"
    assert "NO se pudo restaurar «CV-Base.pdf»" in report_text(result, tmp_path)


def test_when_the_declared_cv_is_not_in_the_account_it_halts_without_posting(conn, tmp_path):
    add_offer(conn)
    result, page = run(conn, tmp_path, site(cvs=("CV-Otro.pdf",), default="CV-Otro.pdf"), prof=make_profile(default_pdf_name=BASE_CV))
    assert "no está en la cuenta" in result["halted"] and result["sent"] == [] and DETAIL_URL not in page.visited


def test_without_a_declared_default_the_start_is_not_checked_and_the_report_says_so(conn, tmp_path):
    add_offer(conn)
    result, page = run(conn, tmp_path, site(default="CV-Otro.pdf", cvs=(BASE_CV, "CV-Otro.pdf")))
    assert result["start_default"] is None and len(result["sent"]) == 1
    assert "no se comprobó, porque ninguna variante declara default_pdf_name" in report_text(result, tmp_path)


def test_nothing_to_send_does_not_touch_the_account(conn, tmp_path):
    result, page = run(conn, tmp_path, site(default="CV-Otro.pdf", cvs=(BASE_CV, "CV-Otro.pdf")), prof=make_profile(default_pdf_name=BASE_CV))
    assert page.visited == [] and result["start_default"] is None


# ── tope por empresa ─────────────────────────────────────────────────────────

def add_company(conn, n, company, points=90, location="Rosario"):
    return add_offer(conn, n, points, company=company, location=location)


def plan_with(conn, cap=None, **kwargs):
    prof = make_profile(**kwargs)
    if cap is not None:
        prof["apply"]["company_daily_cap"] = cap
    return apply.plan(conn, prof, MODEL, TODAY)


def test_two_offers_of_the_same_company_with_the_cap_at_1_send_only_the_first(conn):
    first, second, other = add_company(conn, 1, "Acme SA", 95), add_company(conn, 2, "Acme SA", 90), add_company(conn, 3, "Otra SRL", 85)
    plan = plan_with(conn, 1)
    assert [i["id16"] for i in plan["send"]] == [first, other]
    assert [(i["id16"], i["reason"]) for i in plan["skip"]] == [(second, "tope por empresa")]
    assert "tope por empresa: 1" in apply.summary(plan)


def test_the_default_company_cap_is_1(conn):
    add_company(conn, 1, "Acme SA"), add_company(conn, 2, "Acme SA")
    assert len(plan_with(conn)["send"]) == 1  # make_profile no declara el tope: vale el de por defecto


def test_a_bigger_cap_lets_more_through(conn):
    for n in (1, 2, 3):
        add_company(conn, n, "Acme SA", 95 - n)
    plan = plan_with(conn, 2)
    assert len(plan["send"]) == 2 and [i["reason"] for i in plan["skip"]] == ["tope por empresa"]


def test_the_company_name_is_compared_without_case_accents_or_extra_spaces(conn):
    add_company(conn, 1, "Cañuelas  Móvil SA", 95)
    second = add_company(conn, 2, " CANUELAS movil sa ", 90)
    assert [(i["id16"], i["reason"]) for i in plan_with(conn, 1)["skip"]] == [(second, "tope por empresa")]


def test_confidential_companies_do_not_count_among_themselves(conn):
    for n, company in enumerate(["", "", "Importante empresa del sector", "Empresa confidencial", "confidencial", None], 1):
        add_company(conn, n, company)
    plan = plan_with(conn, 1)
    assert len(plan["send"]) == 6 and plan["skip"] == []


def test_the_postings_of_today_count_toward_the_company_cap(conn):
    done = add_company(conn, 1, "Acme SA")
    db.mark_status(conn, done, "applied")
    db.save_application(conn, done, "dev", 90, "applied", "ok", [])
    same = add_company(conn, 2, "ACME SA")
    other = add_company(conn, 3, "Otra SRL")
    plan = plan_with(conn, 1)
    assert [i["id16"] for i in plan["send"]] == [other] and [(i["id16"], i["reason"]) for i in plan["skip"]] == [(same, "tope por empresa")]
    assert [i["id16"] for i in plan_with(conn, 2)["send"]] == [same, other]


def test_a_posting_of_another_day_does_not_count(conn):
    done = add_company(conn, 1, "Acme SA")
    db.mark_status(conn, done, "applied")
    conn.execute("INSERT INTO applications (id16, variant_id, score, status, reason, applied_at) VALUES (?, 'dev', 90, 'applied', '', ?)",
                 (done, datetime(2020, 1, 1, 10, 0).astimezone().isoformat()))
    conn.commit()
    same_company = add_company(conn, 2, "Acme SA")
    assert [i["id16"] for i in plan_with(conn, 1)["send"]] == [same_company]


def test_an_offer_skipped_by_the_company_cap_does_not_use_the_daily_cap(conn):
    add_company(conn, 1, "Acme SA", 95), add_company(conn, 2, "Acme SA", 94)
    last = add_company(conn, 3, "Otra SRL", 80)
    plan = plan_with(conn, 1, daily_cap=2)
    assert [i["id16"] for i in plan["send"]][-1] == last and len(plan["send"]) == 2


def test_the_company_cap_goes_before_the_daily_cap_and_after_the_lower_reasons(conn):
    add_company(conn, 1, "Acme SA", 95)
    capped = add_company(conn, 2, "Acme SA", 90)
    low = add_company(conn, 3, "Acme SA", 10)
    reasons = {i["id16"]: i["reason"] for i in plan_with(conn, 1, daily_cap=1)["skip"]}
    assert reasons == {capped: "tope por empresa", low: "puntaje bajo (10 < 70)"}


def test_the_report_says_the_company_cap(conn, tmp_path):
    meta = {"now": datetime(2026, 11, 4, 9, 0), "threshold": 70, "daily_cap": 10, "company_daily_cap": 1, "applied_today": 0, "model": MODEL}
    text = apply.write_report(plan_with(conn, 1), meta, tmp_path).read_text(encoding="utf-8")
    assert "- Tope por empresa: 1 por día a la misma empresa" in text


def test_the_profile_loads_company_daily_cap_with_default_1_and_validates_it(tmp_path):
    assert profile.load_profile(write_profile_with(tmp_path))["apply"]["company_daily_cap"] == 1
    assert profile.load_profile(write_profile_with(tmp_path, apply={"company_daily_cap": 3}))["apply"]["company_daily_cap"] == 3
    for bad in (0, -1, 2.5, "2", True, []):
        with pytest.raises(RuntimeError, match="'apply.company_daily_cap' en .* entero de 1 o más"):
            profile.load_profile(write_profile_with(tmp_path, apply={"company_daily_cap": bad}))


def test_parameters_saves_and_reloads_the_company_cap(tmp_path):
    path = write_profile_with(tmp_path)
    form = ui_logic.profile_to_form(path)
    assert form["apply"]["company_daily_cap"] == "1"
    form["apply"]["company_daily_cap"] = "2"
    ui_logic.save_profile(ui_logic.form_to_profile(form), path)
    assert profile.load_profile(path)["apply"]["company_daily_cap"] == 2 and ui_logic.profile_to_form(path)["apply"]["company_daily_cap"] == "2"
    form["apply"]["company_daily_cap"] = ""
    with pytest.raises(RuntimeError, match="«Tope diario por empresa» tiene que ser un número entero"):
        ui_logic.form_to_profile(form)
    form["apply"]["company_daily_cap"] = "0"
    with pytest.raises(RuntimeError, match="company_daily_cap"):
        ui_logic.save_profile(ui_logic.form_to_profile(form), path)


# ── profile check ────────────────────────────────────────────────────────────

def test_profile_check_does_not_say_the_pdf_is_needed_to_apply(tmp_path, capsys):
    assert cli.main(["profile", "check", "--profile", str(write_profile_with(tmp_path))]) == 0
    out = capsys.readouterr().out
    assert "pdf: falta" in out and "hace falta para postular" not in out
