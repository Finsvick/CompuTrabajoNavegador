"""Tests de TAREA-041: marcar el CV por defecto (la página se recarga), esperar los campos de kq, la huella y el contador de CVs subidos."""
from datetime import datetime

import pytest
from playwright.sync_api import Error as PlaywrightError

import fakesite
from fakesite import BASE_CV, CHECK_URL, DETAIL_URL, KQ_URL, FakePage, fixture, standard_site
from test_apply_live import (CV_NAME, MODEL, SALARY, TODAY, FakeClient, add_offer, conn, known_reply, make_profile, status_of, stub_cv)  # noqa: F401
from compuapply import apply, browser

NOT_ATTACHED = "ElementHandle.check: Element is not attached to the DOM"
CONTEXT_DESTROYED = "Page.query_selector_all: Execution context was destroyed, most likely because of a navigation"


def run(conn, tmp_path, the_site=None, *, page=None, prof=None, client=None):
    page = page or FakePage(the_site or standard_site())
    result = apply.run_live(conn, prof or make_profile({"salary": SALARY}), MODEL, client or FakeClient(known_reply()), page, TODAY,
                            stop_file=tmp_path / "STOP", pause=lambda: None, cv_provider=stub_cv())
    return result, page


def report_text(result, tmp_path):
    meta = {"now": datetime(2026, 11, 4, 10, 0, 0), "threshold": 70, "daily_cap": 10, "applied_today": 0, "model": MODEL}
    return apply.write_live_report(result, meta, tmp_path / "runs").read_text(encoding="utf-8")


# ── set_default: marcar el radio recarga la página ───────────────────────────

def two_cvs(**over):
    return standard_site(cvs=(BASE_CV, "CV-Otro.pdf"), default="CV-Otro.pdf", **over)


def test_a_radio_whose_check_says_not_attached_to_the_dom_but_stays_checked_does_not_halt():
    the_site = two_cvs()
    page = FakePage(the_site)
    page.check_error = PlaywrightError(NOT_ATTACHED)
    browser.set_default(page, BASE_CV)
    assert the_site.default == BASE_CV and ("default", f"{1:032x}") in page.log


def test_a_radio_that_says_not_attached_and_does_not_stay_checked_halts():
    the_site = two_cvs(check_changes_default=False)
    page = FakePage(the_site)
    page.check_error = PlaywrightError(NOT_ATTACHED)
    with pytest.raises(browser.Halt, match="El CV por defecto de la cuenta no es «CV-Base.pdf»"):
        browser.set_default(page, BASE_CV)
    assert the_site.default == "CV-Otro.pdf"


def test_not_attached_to_the_dom_counts_as_navigating_but_other_check_errors_still_halt():
    assert browser._navigating(PlaywrightError(NOT_ATTACHED)) and browser._navigating(PlaywrightError("Element is not attached to the DOM"))
    page = FakePage(two_cvs())
    page.check_error = PlaywrightError("Timeout 30000ms exceeded.")
    with pytest.raises(PlaywrightError, match="Timeout"):
        browser.set_default(page, BASE_CV)


def test_a_goto_interrupted_by_the_same_reload_does_not_halt():
    the_site = two_cvs()
    page = FakePage(the_site)
    page.check_error = PlaywrightError(NOT_ATTACHED)
    page.goto_interruptions = 1
    browser.set_default(page, BASE_CV)
    assert the_site.default == BASE_CV and len(page.visited) >= 3  # el primer `goto` se repitió


def test_a_goto_interrupted_again_and_again_ends_up_halting():
    page = FakePage(two_cvs())
    page.goto_interruptions = 5
    with pytest.raises(PlaywrightError, match="interrupted by another navigation"):
        browser.account_cvs(page)
    assert len(page.visited) == 3


def test_a_goto_with_another_error_is_not_repeated():
    page = FakePage(two_cvs())
    page.goto_errors[CHECK_URL] = PlaywrightError("Page.goto: net::ERR_FAILED")
    with pytest.raises(PlaywrightError, match="ERR_FAILED"):
        browser.account_cvs(page)
    assert page.visited == [CHECK_URL]


def test_the_start_restoration_goes_on_when_the_radio_reloads_the_page(conn, tmp_path):
    add_offer(conn)
    the_site = standard_site(after_apply=(fakesite.MATCH_URL, fakesite.APPLY_URL, fakesite.POSTAPPLY_URL),
                             cvs=(BASE_CV, "CV-De-APEX.pdf"), default="CV-De-APEX.pdf")
    page = FakePage(the_site)
    page.check_error = PlaywrightError(NOT_ATTACHED)
    result, _ = run(conn, tmp_path, page=page, prof=make_profile(default_pdf_name=BASE_CV))
    assert result["halted"] is None and len(result["sent"]) == 1
    assert result["start_default"]["restored"] is True and result["start_default"]["ok"] is True
    assert result["restore"]["ok"] is True and the_site.default == BASE_CV


# ── kq: los campos pueden llegar después de la URL ───────────────────────────

def late_fields(page, empty_reads: int, error=None):
    """Que `query_selector_all(KillerQuestions…)` devuelva (o lance) esto las primeras `empty_reads` veces."""
    real, reads = page.query_selector_all, []

    def reading(selector):
        if selector == browser.QUESTION_FIELDS:
            reads.append(1)
            if len(reads) <= empty_reads:
                if error is not None:
                    raise error
                return []
        return real(selector)

    page.query_selector_all = reading
    return reads


def test_kq_fields_that_show_up_after_two_reads_are_answered_and_sent(conn, tmp_path):
    id16 = add_offer(conn)
    page = FakePage(standard_site())
    reads = late_fields(page, 2)
    result, _ = run(conn, tmp_path, page=page)
    assert result["halted"] is None and [i["id16"] for i in result["sent"]] == [id16] and len(reads) >= 3
    [submitted] = page.site.submitted
    assert submitted["checked"] and len(submitted["filled"]) == 1


def test_a_destroyed_context_while_waiting_for_the_kq_fields_does_not_halt(conn, tmp_path):
    id16 = add_offer(conn)
    page = FakePage(standard_site())
    late_fields(page, 2, error=PlaywrightError(CONTEXT_DESTROYED))
    result, _ = run(conn, tmp_path, page=page)
    assert result["halted"] is None and [i["id16"] for i in result["sent"]] == [id16]


def test_another_error_while_waiting_for_the_kq_fields_halts(conn, tmp_path):
    add_offer(conn)
    page = FakePage(standard_site())
    late_fields(page, 1, error=PlaywrightError("Target page, context or browser has been closed\nmás líneas"))
    result, _ = run(conn, tmp_path, page=page)
    assert result["sent"] == [] and "Error: Target page, context or browser has been closed" in result["halted"] and "más líneas" not in result["halted"]


def test_hidden_fields_alone_are_not_questions_and_the_wait_ends_at_the_limit():
    page = FakePage(standard_site(pages={KQ_URL: '<html><body><form action="/candidate/kq"><input type="hidden" name="KillerQuestions[0].Id" value="7">'
                                                  '<button type="submit">Enviar</button></form></body></html>'}))
    page.goto(KQ_URL)
    waits = []
    page.wait_for_timeout = lambda ms: waits.append(ms)
    with pytest.raises(browser.Halt, match="no trae preguntas"):
        browser.read_questions(page)
    assert len(waits) == browser.SETTLE_POLLS


# ── huella ───────────────────────────────────────────────────────────────────

EMPTY_KQ = ("/candidate/kq · campos: input[hidden] __RequestVerificationToken oculto, input[hidden] Oi oculto, input[text] Email, "
            "select Pais, textarea Cover, button[submit]")


def test_a_kq_without_question_fields_halts_and_the_report_has_the_fingerprint_without_values(conn, tmp_path):
    add_offer(conn)
    result, page = run(conn, tmp_path, standard_site(pages={KQ_URL: fixture("apply_kq_empty.html")}))
    assert result["sent"] == [] and f"La pantalla de preguntas de selección no trae preguntas ({EMPTY_KQ})" in result["halted"]
    text = report_text(result, tmp_path)
    assert EMPTY_KQ in text
    for secret in ("SECRETO", "alguien.falso", "Texto personal", "Argentina", "busqueda", "value", "oi=AAAA"):
        assert secret not in result["halted"] and secret not in text, secret
    assert page.visited.count(KQ_URL) == 0 or KQ_URL in page.visited  # llegó a kq por el botón


def test_the_fingerprint_also_goes_with_a_field_that_is_not_the_expected_one(conn, tmp_path):
    add_offer(conn)
    result, _ = run(conn, tmp_path, standard_site(pages={KQ_URL: fixture("apply_kq_unknown_field.html")}))
    assert "no es el esperado (/candidate/kq · campos: input[checkbox] KillerQuestions[0].Terms label, button[submit])" in result["halted"]


def test_the_fingerprint_uses_the_whole_page_without_a_form_caps_the_entries_and_cuts_each_one():
    long_name = "n" * 120
    inputs = "".join(f'<input type="text" name="campo{n}" value="v{n}">' for n in range(50))
    page = FakePage(standard_site(pages={KQ_URL: f'<html><body>{inputs}<input type="radio" name="{long_name}"></body></html>'}))
    page.goto(KQ_URL)
    text = browser.fingerprint(page)
    entries = text.split("campos: ", 1)[1].removesuffix(" …").split(", ")
    assert text.startswith("/candidate/kq · campos: input[text] campo0, ") and text.endswith(" …") and len(entries) == 40
    assert all(len(entry) <= 80 for entry in entries) and "v0" not in text and "value" not in text
    page.goto(fakesite.OTHER_URL)
    assert browser.fingerprint(page).startswith("/candidate/otra · campos: ")
    page.soup = type(page.soup)("<html><body><p>nada</p></body></html>", "html.parser")
    assert browser.fingerprint(page) == "/candidate/otra · campos: ninguno"


def test_the_fingerprint_prefers_the_kq_form_over_other_forms():
    page = FakePage(standard_site(pages={KQ_URL: fixture("apply_kq_empty.html")}))
    page.goto(KQ_URL)
    assert "q" not in {entry.split(" ")[-1] for entry in browser.fingerprint(page).split("campos: ")[1].split(", ")}


# ── contador de CVs subidos ──────────────────────────────────────────────────

def test_an_offer_that_halts_after_uploading_the_cv_counts_it(conn, tmp_path):
    add_offer(conn)
    the_site = standard_site(pages={KQ_URL: fixture("apply_kq_empty.html")})
    result, _ = run(conn, tmp_path, the_site)
    assert result["halted"] and the_site.uploads == [CV_NAME] and result["pdfs"]["uploaded"] == 1
    assert "CVs subidos en esta corrida: 1." in report_text(result, tmp_path)


def test_a_cv_whose_upload_never_finishes_is_not_counted(conn, tmp_path):
    add_offer(conn)
    page = FakePage(standard_site())
    page.site.file_chosen = lambda page_, field_id, name: None  # el sitio no recibe el archivo: nunca llega a candidate/cv/edit
    result, _ = run(conn, tmp_path, page=page)
    assert result["halted"] and result["pdfs"]["uploaded"] == 0


def test_a_cv_that_uploads_but_does_not_become_the_default_is_counted_and_the_run_halts(conn, tmp_path):
    add_offer(conn)
    the_site = standard_site(upload_makes_default=False)
    result, _ = run(conn, tmp_path, the_site)
    assert "no es" in result["halted"] and the_site.uploads == [CV_NAME] and result["pdfs"]["uploaded"] == 1


def test_a_normal_run_counts_one_per_posting(conn, tmp_path):
    for n in (1, 2):
        add_offer(conn, n)
    result, _ = run(conn, tmp_path, standard_site(after_apply=(fakesite.MATCH_URL, fakesite.APPLY_URL, fakesite.POSTAPPLY_URL)))
    assert len(result["sent"]) == 2 and result["pdfs"]["uploaded"] == 2
    assert status_of(conn, f"{1:016X}") == "applied"
