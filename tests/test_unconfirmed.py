"""Tests de TAREA-066: envíos sin confirmar (pantalla final y «Mis postulaciones») y preguntas de horario con opciones. Todo falso."""
from datetime import datetime

import pytest

import fakesite
from fakesite import APPLICATIONS_URL, DETAIL_URL, MATCH_URL, POSTAPPLY_URL, APPLY_URL, FakePage, fixture, standard_site
from test_apply_live import (MODEL, SALARY, TODAY, FakeClient, add_offer, conn, known_reply, make_profile, rows,  # noqa: F401
                             status_of, stub_cv)
from pathlib import Path

from compuapply import apply, browser, db

NO_QUESTIONS = (MATCH_URL, APPLY_URL, POSTAPPLY_URL)
OFFER = {"id16": "0000000000000001", "url": DETAIL_URL, "title": "Oferta 1"}
OTHER_OFFER = {"id16": "0000000000000002", "url": "https://ar.computrabajo.com/ofertas-de-trabajo/otra-oferta-BBBBBBBBBBBBBBBB1111111111111111",
               "title": "Otra"}
POSTAPPLY = fixture("apply_postapply.html")


def run(conn, tmp_path, site, **kw):
    page = FakePage(site)
    result = apply.run_live(conn, make_profile({"salary": SALARY}), MODEL, FakeClient(known_reply()), page, TODAY,
                            stop_file=tmp_path / "STOP", pause=lambda: None, cv_provider=stub_cv(), runs_dir=tmp_path / "runs", **kw)
    return result, page


def unconfirmed_pages():
    return {
        "text hidden": POSTAPPLY.replace("<h1>Te postulaste correctamente</h1>", '<h1 class="hide">Te postulaste correctamente</h1>'),
        "text only in a big container": POSTAPPLY.replace("<h1>Te postulaste correctamente</h1>",
                                                          "<div>" + "Texto de la página. " * 40 + "Te postulaste correctamente</div>"),
        "no text": POSTAPPLY.replace("Te postulaste correctamente", "Gracias"),
    }


@pytest.mark.parametrize("kind", list(unconfirmed_pages()))
def test_a_final_page_with_the_success_text_outside_the_confirmation_block_is_not_a_posting(kind):
    site = standard_site(pages={POSTAPPLY_URL: unconfirmed_pages()[kind]}, after_apply=NO_QUESTIONS)
    with pytest.raises(browser.Unconfirmed) as err:
        browser.apply_one(FakePage(site), OFFER, lambda questions: [])
    message = str(err.value)
    assert "no confirma la postulación" in message and "/candidate/postapply" in message and "título" in message
    assert "?" not in message.split(":")[0] and "oi=" not in message  # la huella no lleva parámetros


def test_a_final_page_that_does_not_name_the_sent_offer_is_not_a_posting():
    other_url = DETAIL_URL.replace("AAAAAAAAAAAAAAAA0000000000000000", "BBBBBBBBBBBBBBBB1111111111111111")
    site = standard_site(pages={other_url: fixture("apply_detail.html")}, after_apply=NO_QUESTIONS)  # la pantalla final es de `oi=AAAA`
    with pytest.raises(browser.Unconfirmed, match="no nombra la oferta enviada"):
        browser.apply_one(FakePage(site), {**OTHER_OFFER, "url": other_url}, lambda questions: [])


def test_a_final_page_with_the_block_and_the_offer_counts_and_leaves_its_print():
    site = standard_site(after_apply=NO_QUESTIONS)
    outcome = browser.apply_one(FakePage(site), OFFER, lambda questions: [])
    assert outcome["status"] == "applied" and outcome["final"].startswith("candidato.ar.computrabajo.com/candidate/postapply")
    assert "?" not in outcome["final"] and "oi=" not in outcome["final"]


def test_run_live_marks_an_unconfirmed_posting_and_a_new_run_does_not_post_it_again(conn, tmp_path):
    id16 = add_offer(conn)
    bad = standard_site(pages={POSTAPPLY_URL: unconfirmed_pages()["text hidden"]}, after_apply=NO_QUESTIONS)
    result, _ = run(conn, tmp_path, bad)
    assert result["sent"] == [] and result["halted"] is None  # una sola sin confirmar no frena
    assert status_of(conn, id16) == "unconfirmed"
    assert rows(conn, "SELECT status FROM applications") == [("unconfirmed",)]
    assert [i["id16"] for i in result["unconfirmed_now"]] == [id16]
    again, page = run(conn, tmp_path, standard_site(after_apply=NO_QUESTIONS))
    assert again["plan"]["send"] == [] and again["sent"] == [] and page.clicks() == [] and page.visited == []


def test_a_sent_offer_missing_in_the_final_check_is_unconfirmed_and_listed_with_its_url(conn, tmp_path):
    id16 = add_offer(conn, 1)
    site = standard_site(after_apply=NO_QUESTIONS, lists_posted=False)  # «Mis postulaciones» no la lista
    result, _ = run(conn, tmp_path, site)
    assert [i["id16"] for i in result["sent"]] == [id16] and result["halted"] is None
    assert [i["id16"] for i in result["verify"]["unconfirmed"]] == [id16]
    assert status_of(conn, id16) == "unconfirmed" and rows(conn, "SELECT status FROM applications") == [("unconfirmed",)]
    text = apply.write_live_report(result, {"now": datetime(2026, 11, 6, 20, 0, 0), "threshold": 70, "daily_cap": 10, "applied_today": 0,
                                            "model": MODEL}, tmp_path / "runs").read_text(encoding="utf-8")
    assert "sin confirmar en Mis postulaciones" in text and DETAIL_URL in text and "| pantalla final |" in text
    assert "candidato.ar.computrabajo.com/candidate/postapply" in text  # la huella de cada enviada
    again, page = run(conn, tmp_path, standard_site(after_apply=NO_QUESTIONS))
    assert again["plan"]["send"] == [] and again["sent"] == [] and page.clicks() == []


def test_a_sent_offer_listed_in_my_applications_stays_applied(conn, tmp_path):
    id16 = add_offer(conn)
    result, _ = run(conn, tmp_path, standard_site(after_apply=NO_QUESTIONS))
    assert result["verify"]["unconfirmed"] == [] and status_of(conn, id16) == "applied"


CARD = ('<div data-match="IMX1" data-targeturl="/candidate/postapply?oi=AAAA&im=IMX1"><h1>{title}</h1><p class="fc_base mt5">{company}</p>'
        '<p class="fc_base">Rosario</p><p class="fwB">Postulado</p></div>')


def applications_page(company):
    return standard_site(pages={APPLICATIONS_URL: "<html><body>" + CARD.format(title="Cajero", company=company) + "</body></html>"})


@pytest.mark.parametrize("shown", ["Randstad Argentina 4,4", "Randstad Argentina 4.4", "Randstad Argentina", "Randstad Argentina  5,0 "])
def test_the_company_in_my_applications_loses_its_rating(shown):
    found = browser.read_applications(FakePage(applications_page(shown)), pages=1)
    assert found[0]["company"] == "Randstad Argentina"


def test_a_company_that_ends_in_a_plain_number_keeps_it():
    assert browser.read_applications(FakePage(applications_page("Banco 2")), pages=1)[0]["company"] == "Banco 2"


def test_the_final_check_matches_title_and_company_without_the_rating():
    page = FakePage(applications_page("Randstad Argentina 4,4"))
    sent = [{"id16": "1", "title": "Cajero", "company": "Randstad Argentina", "im": ""}]
    assert apply._verify_sent(page, sent)["unconfirmed"] == []


# ── horario con opciones ─────────────────────────────────────────────────────

SCHEDULE = "¿Cuál es tu disponibilidad horaria?"


def schedule(options):
    return {"text": SCHEDULE, "kind": "closed", "options": options}


@pytest.mark.parametrize("declared", ["Disponibilidad total", "Amplia, de lunes a domingo", "Cualquier horario"])
def test_with_total_availability_the_widest_option_is_chosen(declared):
    answer = apply.availability_answer(schedule(["Por la Mañana", "Por la Tarde", "Indistinto"]), {"availability": declared})
    assert answer["option"] == 2


@pytest.mark.parametrize("options, wanted", [(["Mañana", "Tarde", "Cualquiera"], 2), (["Mañana", "Full time"], 1),
                                             (["Turno mañana", "Rotativo"], 1), (["Mañana", "Todos los turnos"], 1)])
def test_other_wide_options_also_count(options, wanted):
    assert apply.availability_answer(schedule(options), {"availability": "Disponibilidad total"})["option"] == wanted


def test_with_total_availability_and_only_one_shift_options_the_question_stays_pending():
    answer = apply.availability_answer(schedule(["Por la Mañana", "Por la Tarde"]), {"availability": "Disponibilidad total"})
    assert answer == {"pending": "disponibilidad: ninguna opción cubre lo declarado"}


def test_without_total_availability_the_rule_does_not_choose_a_shift():
    assert apply.availability_answer(schedule(["Por la Mañana", "Indistinto"]), {"availability": "Lunes a viernes"}) is None
    assert apply.availability_answer(schedule(["Por la Mañana", "Indistinto"]), {}) is None


def test_answer_questions_uses_the_widest_option_without_asking_the_model():
    profile = make_profile({"availability": "Disponibilidad total"})
    entries = apply.answer_questions([schedule(["Por la Mañana", "Por la Tarde", "Indistinto"])], profile, None, MODEL, "dev")  # sin cliente
    assert entries[0]["option"] == 2 and entries[0]["source"] == "answers"


def test_the_report_shows_the_options_of_a_closed_question_as_offer_text(conn, tmp_path):
    result = {"id16": "x", "title": "T", "company": "C", "entries": [
        {"question": SCHEDULE, "kind": "closed", "options": ["Por la Mañana", "Indistinto"], "option": 1, "source": "answers",
         "fixed": "availability + modality"}]}
    lines = apply.answer_section({"answers": [result], "sent": []})
    assert any("opciones (texto de la oferta): Por la Mañana / Indistinto" in line for line in lines)


# ── TAREA-067: espera, pantalla guardada, racha, reconciliación y tope ───────

PERSONAL = ("Giovanni", "Bianchi", "giovanni.bianchi@example.com", "3345678901")
LEAKY = ("Hola Giovanni Bianchi, escribinos a giovanni.bianchi@example.com o al +39 3345678901. Gracias.</h1>"
         "<p>Mirá tus postulaciones en el portal</p><h1 class=\"hide\">")


def sequence(site, kinds):
    """La pantalla final de la oferta n-ésima sale según `kinds[n]`: «ok» (la de siempre) o «bad» (sin el bloque de éxito y con datos del perfil)."""
    original, state = site.html_for, {"n": 0}

    def html_for(url, keep=False):
        page = original(url, keep)
        if url == POSTAPPLY_URL and not keep:
            kind = kinds[min(state["n"], len(kinds) - 1)]
            state["n"] += 1
            if kind == "bad":
                page = page.replace("<h1>Te postulaste correctamente</h1>", "<h1>" + LEAKY + "x</h1>")
        return page

    site.html_for = html_for
    return site


class SlowPage(FakePage):
    """La pantalla final llega sin el texto de éxito y lo muestra a los `after` segundos (del reloj falso)."""

    def __init__(self, site, after):
        super().__init__(site)
        self.after, self.arrived, self.good = after, None, None

    def wait_for_timeout(self, ms):
        super().wait_for_timeout(ms)
        if self.url == POSTAPPLY_URL and self.arrived is None:  # recién llegó a la pantalla final: todavía sin el texto de éxito
            self.arrived, self.good = fakesite.CLOCK.t, self.soup
            self.soup = type(self.soup)(str(self.soup).replace("Te postulaste correctamente", "Un momento"), "html.parser")
        elif self.arrived is not None and fakesite.CLOCK.t - self.arrived >= self.after:
            self.soup = self.good


def test_the_success_text_that_shows_up_after_3_seconds_still_counts():
    page = SlowPage(standard_site(after_apply=NO_QUESTIONS), after=3)
    outcome = browser.apply_one(page, OFFER, lambda questions: [])
    assert outcome["status"] == "applied" and 3 <= fakesite.CLOCK.t - page.arrived < 10


def test_a_success_text_that_never_shows_up_waits_10_seconds_and_then_is_unconfirmed():
    page = SlowPage(standard_site(after_apply=NO_QUESTIONS), after=999)
    with pytest.raises(browser.Unconfirmed):
        browser.apply_one(page, OFFER, lambda questions: [])
    assert 10 <= fakesite.CLOCK.t - page.arrived < 12


def test_unconfirmed_is_not_a_halt():
    assert not issubclass(browser.Unconfirmed, browser.Halt)


def test_an_unconfirmed_screen_is_saved_without_personal_data_and_the_run_goes_on(conn, tmp_path):
    first, second = add_offer(conn, 1), add_offer(conn, 2)
    result, _ = run(conn, tmp_path, sequence(standard_site(after_apply=NO_QUESTIONS), ["bad", "ok"]))
    assert result["halted"] is None and [i["id16"] for i in result["sent"]] == [second]
    assert status_of(conn, first) == "unconfirmed" and status_of(conn, second) == "applied"
    [item] = result["unconfirmed_now"]
    saved = Path(item["saved"])
    text = saved.read_text(encoding="utf-8")
    assert saved.parent == tmp_path / "runs" and saved.name.endswith(f"-postapply-{first}.txt") and len(text) < 3600
    for secret in PERSONAL:
        assert secret not in text
    assert "[dato]" in text and "## Bloques con «postul»" in text and "caracteres: Mirá tus postulaciones" in text
    assert "<html" not in text.lower() and "<h1" not in text
    report = apply.write_live_report(result, {"now": datetime(2026, 11, 6, 23, 0, 0), "threshold": 70, "daily_cap": 10, "applied_today": 0,
                                              "model": MODEL}, tmp_path / "runs2").read_text(encoding="utf-8")
    assert "Pantalla guardada en" in report and saved.name in report


def test_the_saved_text_is_cut_to_3000_characters(tmp_path):
    error = browser.Unconfirmed("x", "línea larga " * 1000, [])
    body = apply.save_screen(tmp_path, "run", "ID", error, []).read_text(encoding="utf-8")
    assert len(body.split("## Texto visible")[1].split("## Bloques")[0].strip()) <= apply.DUMP_MAX


def test_two_unconfirmed_in_a_row_stop_the_run(conn, tmp_path):
    for n in (1, 2, 3):
        add_offer(conn, n)
    result, _ = run(conn, tmp_path, sequence(standard_site(after_apply=NO_QUESTIONS), ["bad"]))
    assert result["halted"] == "2 postulaciones seguidas sin confirmar: revisá las pantallas guardadas"
    assert len(result["unconfirmed_now"]) == 2 and result["sent"] == []
    assert status_of(conn, "0000000000000003") == "new"  # la tercera ni se abrió


def test_a_sent_offer_in_the_middle_restarts_the_count(conn, tmp_path):
    for n in (1, 2, 3, 4):
        add_offer(conn, n)
    result, _ = run(conn, tmp_path, sequence(standard_site(after_apply=NO_QUESTIONS), ["bad", "ok", "bad", "ok"]))
    assert result["halted"] is None and len(result["sent"]) == 2 and len(result["unconfirmed_now"]) == 2


def test_the_streak_is_shared_between_chunks_of_a_batch(conn, tmp_path):
    add_offer(conn, 1)
    add_offer(conn, 2)
    shared = {"n": 0}
    site = sequence(standard_site(after_apply=NO_QUESTIONS), ["bad"])
    first, _ = run(conn, tmp_path, site, batch={"ids": {"0000000000000001"}, "variant": "dev", "max": 5, "deadline": None, "streak": shared})
    assert first["halted"] is None and shared["n"] == 1
    second, _ = run(conn, tmp_path, site, batch={"ids": {"0000000000000002"}, "variant": "dev", "max": 5, "deadline": None, "streak": shared})
    assert second["halted"] == apply.UNCONFIRMED_HALT


APPLICATIONS = [{"title": "Oferta 1", "company": "Empresa 1", "location": "Rosario", "status": "Postulado", "im": "IMX"}]


def make_unconfirmed(conn, n, im=None):
    id16 = add_offer(conn, n)
    db.mark_status(conn, id16, "unconfirmed")
    db.save_application(conn, id16, "dev", 85, "unconfirmed", "x", [], None, im, None)
    return id16


def test_an_unconfirmed_that_shows_in_my_applications_becomes_sent_and_one_that_does_not_stays(conn):
    shown, hidden = make_unconfirmed(conn, 1), make_unconfirmed(conn, 2)
    done = apply.reconcile_unconfirmed(conn, APPLICATIONS)
    assert [i["id16"] for i in done] == [shown]
    assert status_of(conn, shown) == "applied" and status_of(conn, hidden) == "unconfirmed"
    assert rows(conn, "SELECT id16, status, reason FROM applications ORDER BY id16") == [
        (shown, "applied", "confirmada después en Mis postulaciones"), (hidden, "unconfirmed", "x")]


def test_reconciliation_also_matches_by_im(conn):
    by_im = make_unconfirmed(conn, 5, im="IMY")
    done = apply.reconcile_unconfirmed(conn, [{"title": "Otro título", "company": "Otra", "location": "", "status": "Postulado", "im": "IMY"}])
    assert [i["id16"] for i in done] == [by_im]


def test_run_live_reconciles_when_it_reads_my_applications_and_the_report_says_so(conn, tmp_path):
    old = make_unconfirmed(conn, 1)
    add_offer(conn, 2, title="Nueva", company="Otra SA")
    pages = {APPLICATIONS_URL: "<html><body>" + CARD.format(title="Oferta 1", company="Empresa 1") + "</body></html>"}
    result, _ = run(conn, tmp_path, standard_site(after_apply=NO_QUESTIONS, pages=pages))
    assert status_of(conn, old) == "applied" and [i["id16"] for i in result["reconciled"]] == [old]
    text = apply.write_live_report(result, {"now": datetime(2026, 11, 6, 23, 5, 0), "threshold": 70, "daily_cap": 10, "applied_today": 0,
                                            "model": MODEL}, tmp_path / "runs3").read_text(encoding="utf-8")
    assert "Confirmada después en Mis postulaciones: Oferta 1 (Empresa 1)" in text


def test_the_final_check_reconciles_old_unconfirmed_too(conn):
    old = make_unconfirmed(conn, 1)
    page = FakePage(standard_site(pages={APPLICATIONS_URL: "<html><body>" + CARD.format(title="Oferta 1", company="Empresa 1") + "</body></html>"}))
    check = apply._verify_sent(page, [{"id16": "z", "title": "Oferta 1", "company": "Empresa 1", "im": ""}], conn)
    assert [i["id16"] for i in check["reconciled"]] == [old] and status_of(conn, old) == "applied"


def test_an_unconfirmed_counts_for_the_daily_cap(conn):
    make_unconfirmed(conn, 1)
    add_offer(conn, 2)
    assert apply.applied_today(conn, TODAY) == 1
    plan = apply.plan(conn, make_profile(daily_cap=1), MODEL, TODAY)
    assert plan["send"] == [] and [i["reason"] for i in plan["skip"]] == ["tope diario"]


def test_an_unconfirmed_counts_for_the_company_cap(conn):
    make_unconfirmed(conn, 1)
    assert apply.applied_by_company_today(conn, TODAY)[apply.company_key("Empresa 1")] == 1


def test_unconfirmed_count_for_the_cap_of_the_batch(conn, monkeypatch):
    import test_batch as tb
    site = tb.Site({("mozo-en-capital-federal", 1): tb.listing_html(["Mozo"] * 3, 1), ("mozo-en-capital-federal", 2): tb.listing_html(["Mozo"] * 3, 10)})
    prof, seen = tb.two_variants(), []

    def live(b):
        plan_ = apply.plan(conn, prof, MODEL, datetime.now().date(), only=b["ids"], variant=b["variant"], cap=b["max"], company_cap_on=b["company_cap"])
        seen.append(b["max"])
        for item in plan_["send"]:
            db.mark_status(conn, item["id16"], "unconfirmed")
        return {"plan": plan_, "sent": [], "unconfirmed_now": list(plan_["send"]), "skipped": [], "halted": None, "time_up": False,
                "pdfs": {"before": 3, "max": 10, "uploaded": 0, "reused": 0}, "requeued": 0, "completed": 0, "cost_usd": 0.0,
                "read": 0, "start_default": None, "restore": None, "deleted": [], "pending": [], "answers": []}

    out, _ = tb.walk(conn, site, ["mozo"], monkeypatch, zones=["Capital Federal"], opts={"max": 4}, live=live)
    assert out["end"] == "tope" and seen == [4, 1]
