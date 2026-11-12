"""Tests de TAREA-043: respuestas cerradas con radios ocultos por CSS (clic en el label o en el recuadro) y la huella con «oculto» y «label»."""
from datetime import datetime
from pathlib import Path

import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeout

import fakesite
from fakesite import KQ_URL, FakeElement, FakePage, fixture, standard_site
from test_apply_live import MODEL, SALARY, TODAY, FakeClient, add_offer, conn, known_reply, make_profile, reply, status_of, stub_cv  # noqa: F401
from compuapply import apply, browser, profile

BROWSER_SOURCE = Path(browser.__file__)
YEARS_QUESTION = "¿Cuántos años de experiencia tenés con atención al cliente?"  # sus opciones tienen cifras: hace falta la respuesta declarada
TWO_CLOSED = reply({"option": 1, "source": "answers"}, {"option": 0, "source": "answers"})  # años: «Entre 1 y 3»; disponibilidad: «Sí»
CHOSEN = {"KillerQuestions[0].ClosedQuestion": "2", "KillerQuestions[1].ClosedQuestion": "1"}


def run(conn, tmp_path, kq_html, *, client=None, page=None):
    page = page or FakePage(standard_site(pages={KQ_URL: kq_html}))
    result = apply.run_live(conn, make_profile({"salary": SALARY, "availability": "Lunes a viernes", profile.normalize_question(YEARS_QUESTION): "Entre 1 y 3 años"}), MODEL, client or FakeClient(TWO_CLOSED),
                            page, TODAY, stop_file=tmp_path / "STOP", pause=lambda: None, cv_provider=stub_cv())
    return result, page


def report_text(result, tmp_path):
    meta = {"now": datetime(2026, 11, 5, 10, 0, 0), "threshold": 70, "daily_cap": 10, "applied_today": 0, "model": MODEL}
    return apply.write_live_report(result, meta, tmp_path / "runs").read_text(encoding="utf-8")


def of(page, action):
    return [call for call in page.calls if call[0] == action]


# ── radios ocultos con label ─────────────────────────────────────────────────

def test_a_hidden_radio_with_a_visible_label_is_marked_by_clicking_the_label_and_the_offer_is_sent(conn, tmp_path):
    id16 = add_offer(conn)
    result, page = run(conn, tmp_path, fixture("apply_kq_hidden_labels.html"))
    assert result["halted"] is None and [i["id16"] for i in result["sent"]] == [id16] and status_of(conn, id16) == "applied"
    assert page.site.submitted[0]["checked"] == CHOSEN  # quedaron marcadas las opciones elegidas
    labels = [entry for entry in page.log if entry[0] == "click" and entry[1] == "label"]
    assert [entry[2] for entry in labels] == ["Entre 1 y 3 años", "Sí"]
    assert of(page, "check") == []  # un radio oculto no se espera: Playwright nunca lo vería
    assert [call for call in of(page, "click") if call[1] == browser.FIELD_WAIT] == [("click", browser.FIELD_WAIT)] * 2  # los dos clics en labels, con espera corta


def test_a_hidden_radio_without_a_label_but_with_a_visible_box_is_marked_by_clicking_the_box(conn, tmp_path):
    id16 = add_offer(conn)
    result, page = run(conn, tmp_path, fixture("apply_kq_hidden_boxes.html"))
    assert result["halted"] is None and [i["id16"] for i in result["sent"]] == [id16]
    assert page.site.submitted[0]["checked"] == CHOSEN
    boxes = [entry for entry in page.log if entry[0] == "click" and entry[1] == "div"]
    assert [entry[2] for entry in boxes] == ["Entre 1 y 3 años", "Sí"] and of(page, "check") == []


def test_a_visible_radio_is_marked_with_check_and_a_short_wait_and_nothing_is_clicked(conn, tmp_path):
    add_offer(conn)
    result, page = run(conn, tmp_path, fixture("apply_kq.html"), client=FakeClient(known_reply()))
    assert result["halted"] is None and len(result["sent"]) == 1
    assert of(page, "check") == [("check", browser.FIELD_WAIT)] * 2
    assert not [entry for entry in page.log if entry[0] == "click" and entry[1] in ("label", "div")]
    assert of(page, "fill") == [("fill", browser.FIELD_WAIT)]  # la abierta también con la espera corta


def test_a_visible_radio_whose_check_times_out_falls_back_to_the_click_of_a_person(conn, tmp_path, monkeypatch):
    add_offer(conn)
    real = FakeElement.check

    def times_out(self, timeout=None, **kwargs):
        if "ClosedQuestion" in (self.tag.get("name") or ""):
            raise PlaywrightTimeout(f"ElementHandle.check: Timeout {timeout}ms exceeded.")
        return real(self, timeout, **kwargs)

    monkeypatch.setattr(FakeElement, "check", times_out)
    result, page = run(conn, tmp_path, fixture("apply_kq.html"), client=FakeClient(known_reply()))
    assert result["halted"] is None and len(result["sent"]) == 1
    assert [entry[2] for entry in page.log if entry[0] == "click" and entry[1] == "label"] == ["Entre 1 y 3 años", "Sí"]


def test_another_error_from_check_is_not_hidden_by_the_fallback(conn, tmp_path, monkeypatch):
    add_offer(conn)

    def broken(self, timeout=None, **kwargs):
        from playwright.sync_api import Error
        raise Error("ElementHandle.check: Target closed")

    monkeypatch.setattr(FakeElement, "check", broken)
    result, _ = run(conn, tmp_path, fixture("apply_kq.html"), client=FakeClient(known_reply()))
    assert result["sent"] == [] and "Error: ElementHandle.check: Target closed" in result["halted"]


# ── lo que no se puede marcar frena ──────────────────────────────────────────

def test_if_the_radio_is_not_checked_after_the_click_it_halts_with_the_fingerprint_and_sends_nothing(conn, tmp_path):
    id16 = add_offer(conn)
    page = FakePage(standard_site(pages={KQ_URL: fixture("apply_kq_hidden_labels.html")}))
    page.clicks_do_not_tick = True  # el sitio ignora el clic
    result, _ = run(conn, tmp_path, None, page=page)
    assert result["sent"] == [] and page.site.submitted == [] and status_of(conn, id16) == "new"
    assert "No se pudo marcar la opción 2 de la pregunta 1 (/candidate/kq · campos: " in result["halted"]
    assert "input[radio] KillerQuestions[0].ClosedQuestion oculto label" in result["halted"]
    assert not any(entry[0] == "click" and entry[1] == "button" for entry in page.log)  # no tocó «Enviar»
    text = report_text(result, tmp_path)
    assert "No se pudo marcar la opción 2 de la pregunta 1" in text and "oculto label" in text
    assert result["pdfs"]["uploaded"] == 1  # el CV ya estaba subido


def test_if_nothing_is_visible_to_click_it_halts_without_clicking():
    page = FakePage(standard_site(pages={KQ_URL: fixture("apply_kq_hidden_boxes.html")}))
    page.goto(KQ_URL)
    questions = browser.read_questions(page)
    for tag in page.soup.select("div.opt"):
        tag["class"] = ["opt", "hide"]  # el recuadro también se esconde
    with pytest.raises(browser.Halt, match=r"No se pudo marcar la opción 2 de la pregunta 1 \(/candidate/kq · campos: .*oculto"):
        browser._fill(page, questions, [{"option": 1}, {"option": 0}])
    assert of(page, "click") == [] and of(page, "check") == []


def test_a_closed_question_with_a_radio_that_does_not_become_checked_after_check_also_halts(monkeypatch):
    page = FakePage(standard_site(pages={KQ_URL: fixture("apply_kq.html")}))
    page.goto(KQ_URL)
    questions = browser.read_questions(page)
    monkeypatch.setattr(FakeElement, "check", lambda self, timeout=None, **kw: None)  # `check` vuelve sin marcar nada
    page.clicks_do_not_tick = True
    with pytest.raises(browser.Halt, match="No se pudo marcar la opción 3 de la pregunta 1"):
        browser._fill(page, questions, [{"option": 2}, {"text": "x"}, {"option": 0}])


def test_a_hidden_open_field_halts_with_the_fingerprint(conn, tmp_path):
    add_offer(conn)
    html = fixture("apply_kq.html").replace('<textarea name="KillerQuestions[1].OpenQuestion" maxlength="500">',
                                            '<textarea name="KillerQuestions[1].OpenQuestion" maxlength="500" style="display:none">')
    result, page = run(conn, tmp_path, html, client=FakeClient(known_reply()))
    assert result["sent"] == [] and page.site.submitted == []
    assert "No se pudo escribir la respuesta de la pregunta 2 (/candidate/kq · campos: " in result["halted"]
    assert "textarea KillerQuestions[1].OpenQuestion oculto" in result["halted"] and of(page, "fill") == []  # ni lo intentó: no se ve


def test_an_open_field_that_times_out_halts_with_the_fingerprint(conn, tmp_path, monkeypatch):
    add_offer(conn)

    def times_out(self, text, timeout=None, **kwargs):
        raise PlaywrightTimeout(f"ElementHandle.fill: Timeout {timeout}ms exceeded.")

    monkeypatch.setattr(FakeElement, "fill", times_out)
    result, _ = run(conn, tmp_path, fixture("apply_kq.html"), client=FakeClient(known_reply()))
    assert result["sent"] == [] and "No se pudo escribir la respuesta de la pregunta 2" in result["halted"]


# ── sin force ni JavaScript ──────────────────────────────────────────────────

def test_browser_never_forces_a_click_nor_marks_with_javascript():
    source = BROWSER_SOURCE.read_text(encoding="utf-8")
    for forbidden in ("force=True", "force=true", "force =", ".evaluate(", "evaluate_handle", "dispatch_event", "eval_on_selector",
                      ".set_checked(", "execute_script"):
        assert forbidden not in source, forbidden
    assert "FIELD_WAIT = 5000" in source


def test_the_fake_rejects_force_so_a_forced_click_would_fail_a_test():
    page = FakePage(standard_site(pages={KQ_URL: fixture("apply_kq.html")}))
    page.goto(KQ_URL)
    with pytest.raises(AssertionError, match="force"):
        page.query_selector("label").click(force=True)


# ── huella con «oculto» y «label» ────────────────────────────────────────────

def test_the_fingerprint_marks_hidden_and_label_without_any_value():
    page = FakePage(standard_site(pages={KQ_URL: fixture("apply_kq_hidden_labels.html")}))
    page.goto(KQ_URL)
    text = browser.fingerprint(page)
    assert text.startswith("/candidate/kq · campos: ")
    assert text.count("input[radio] KillerQuestions[0].ClosedQuestion oculto label") == 3
    assert text.count("input[radio] KillerQuestions[1].ClosedQuestion oculto label") == 2
    assert "input[hidden] KillerQuestions[0].Id oculto" in text and text.endswith("button[submit]")
    for secret in ("value", "años", "Sí", "201", "202", "oi=AAAA"):
        assert secret not in text, secret


def test_the_fingerprint_of_visible_fields_has_no_oculto_and_marks_only_the_ones_with_a_label():
    page = FakePage(standard_site(pages={KQ_URL: fixture("apply_kq.html")}))
    page.goto(KQ_URL)
    text = browser.fingerprint(page)
    assert "textarea KillerQuestions[1].OpenQuestion" in text and "textarea KillerQuestions[1].OpenQuestion oculto" not in text
    assert "input[radio] KillerQuestions[0].ClosedQuestion label" in text and "ClosedQuestion oculto" not in text
    boxes = FakePage(standard_site(pages={KQ_URL: fixture("apply_kq_hidden_boxes.html")}))
    boxes.goto(KQ_URL)
    assert "input[radio] KillerQuestions[0].ClosedQuestion oculto," in browser.fingerprint(boxes)  # oculto y sin label
