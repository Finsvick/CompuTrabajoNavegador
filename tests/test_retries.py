"""Tests de TAREA-042: reintentos que reusan el CV ya subido, la restauración que termina en cvedit y la regla del turno. Todo falso."""
from datetime import datetime

import pytest
from playwright.sync_api import Error as PlaywrightError

import fakesite
from fakesite import BASE_CV, CHECK_URL, CV_EDIT_URL, KQ_URL, POSTAPPLY_URL, FakePage, fixture, standard_site
from test_apply_live import (CV_NAME, MODEL, SALARY, TODAY, FakeClient, add_offer, conn, known_reply, make_profile, reply, rows,  # noqa: F401
                             status_of, stub_cv, unknown_salary_reply)
from compuapply import apply, browser, db, profile

NOT_ATTACHED = "ElementHandle.check: Element is not attached to the DOM"


def run(conn, tmp_path, page, *, prof=None, client=None, provider=None):
    result = apply.run_live(conn, prof or make_profile({"salary": SALARY}), MODEL, client or FakeClient(known_reply()), page, TODAY,
                            stop_file=tmp_path / "STOP", pause=lambda: None, cv_provider=provider or stub_cv())
    return result


def report_text(result, tmp_path):
    meta = {"now": datetime(2026, 11, 5, 10, 0, 0), "threshold": 70, "daily_cap": 10, "applied_today": 0, "model": MODEL}
    return apply.write_live_report(result, meta, tmp_path / "runs").read_text(encoding="utf-8")


def first_attempt_leaves_a_pending_question(conn, tmp_path, the_site):
    """Primer intento: sube el CV, la oferta pide un sueldo que no hay y queda `pending_question` (con su CV ya en la cuenta)."""
    id16 = add_offer(conn)
    result = run(conn, tmp_path, FakePage(the_site), prof=make_profile(), client=FakeClient(unknown_salary_reply()))
    assert status_of(conn, id16) == "pending_question" and result["pdfs"]["uploaded"] == 1 and the_site.uploads == [CV_NAME]
    return id16


def answer_the_question(conn, id16):
    """El usuario contesta en la pestaña Preguntas: la oferta vuelve a la cola (y la fila de `applications` queda como estaba)."""
    conn.execute("DELETE FROM pending_questions WHERE id16 = ?", (id16,))
    db.mark_status(conn, id16, "new")


# ── guardar lo subido ────────────────────────────────────────────────────────

def test_a_pending_question_offer_keeps_the_uploaded_file_and_version(conn, tmp_path):
    id16 = first_attempt_leaves_a_pending_question(conn, tmp_path, standard_site())
    assert rows(conn, "SELECT status, cv_file, cv_version FROM applications") == [("pending_question", CV_NAME, "c8")]
    assert status_of(conn, id16) == "pending_question"


def test_a_run_that_halts_after_the_upload_keeps_the_uploaded_file_and_version(conn, tmp_path):
    id16 = add_offer(conn)
    the_site = standard_site(pages={KQ_URL: fixture("apply_kq_empty.html")})
    result = run(conn, tmp_path, FakePage(the_site))
    assert result["halted"] and status_of(conn, id16) == "new"
    assert rows(conn, "SELECT id16, status, cv_file, cv_version FROM applications") == [(id16, "cv_uploaded", CV_NAME, "c8")]


def test_a_halt_before_the_upload_keeps_nothing(conn, tmp_path):
    add_offer(conn)
    result = run(conn, tmp_path, FakePage(standard_site(statuses={fakesite.DETAIL_URL: 429})))
    assert result["halted"] and rows(conn, "SELECT * FROM applications") == []


def test_an_invalid_model_answer_after_the_upload_keeps_the_file_too(conn, tmp_path):
    id16 = add_offer(conn)
    bad = reply({"option": 1, "source": "cv"}, {"option": 1, "source": "cv"}, {"option": 1, "source": "cv"}, {"option": 1, "source": "cv"})  # cuatro respuestas para tres preguntas: no sirve
    result = run(conn, tmp_path, FakePage(standard_site()), client=FakeClient(bad))
    assert [i["reason"].startswith("respuesta inválida") for i in result["skipped"]] == [True]
    assert rows(conn, "SELECT id16, status, cv_file FROM applications") == [(id16, "cv_uploaded", CV_NAME)]


def test_an_offer_that_turned_out_to_be_already_applied_after_the_upload_keeps_the_file(conn, tmp_path):
    id16 = add_offer(conn)
    the_site = standard_site(after_apply=(fakesite.MATCH_URL,), pages={fakesite.MATCH_URL: fixture("apply_match_applied.html")})
    run(conn, tmp_path, FakePage(the_site))
    assert rows(conn, "SELECT id16, status, cv_file, cv_version FROM applications") == [(id16, "already_applied", CV_NAME, "c8")]


# ── reusar ───────────────────────────────────────────────────────────────────

def test_the_retry_marks_the_cv_already_in_the_account_verifies_it_and_posts_without_uploading(conn, tmp_path):
    the_site = standard_site(cvs=(BASE_CV,), default=BASE_CV)
    id16 = first_attempt_leaves_a_pending_question(conn, tmp_path, the_site)
    answer_the_question(conn, id16)
    the_site.default = BASE_CV  # entre intentos el por defecto cambió
    page = FakePage(the_site)
    result = run(conn, tmp_path, page, prof=make_profile({"salary": SALARY, "start": "Ya"}))
    assert result["halted"] is None and [i["id16"] for i in result["sent"]] == [id16]
    assert the_site.uploads == [CV_NAME] and the_site.cvs.count(CV_NAME) == 1  # ninguna copia nueva
    assert not any(entry[0] == "upload" for entry in page.log) and any(entry[0] == "default" for entry in page.log)  # se marcó, no se subió
    assert result["pdfs"]["uploaded"] == 0 and result["pdfs"]["reused"] == 1 and result["sent"][0]["reused"] is True
    assert the_site.attached == CV_NAME  # la postulación salió con ese CV
    assert rows(conn, "SELECT status, cv_file, cv_version FROM applications") == [("applied", CV_NAME, "c8")]
    text = report_text(result, tmp_path)
    assert "CVs subidos en esta corrida: 0." in text and "- CV reusado: 1" in text and f"{CV_NAME} (CV reusado)" in text


def test_the_retry_after_a_halt_reuses_the_uploaded_cv_and_the_default_is_restored_at_the_end(conn, tmp_path):
    id16 = add_offer(conn)
    the_site = standard_site(cvs=(BASE_CV,), default=BASE_CV)
    the_site.pages[KQ_URL] = fixture("apply_kq_empty.html")
    prof = make_profile({"salary": SALARY}, default_pdf_name=BASE_CV)
    first = run(conn, tmp_path, FakePage(the_site), prof=prof)
    assert first["halted"] and the_site.uploads == [CV_NAME]
    the_site.pages[KQ_URL] = fixture("apply_kq.html")  # el sitio vuelve a andar
    page = FakePage(the_site)
    second = run(conn, tmp_path, page, prof=prof)
    assert second["halted"] is None and [i["id16"] for i in second["sent"]] == [id16]
    assert the_site.uploads == [CV_NAME] and second["pdfs"]["uploaded"] == 0 and second["pdfs"]["reused"] == 1
    assert the_site.default == BASE_CV and second["restore"]["ok"] is True  # al terminar vuelve el base


def test_another_offer_with_the_same_file_name_makes_it_upload_as_before(conn, tmp_path):
    the_site = standard_site(cvs=(BASE_CV,), default=BASE_CV)
    id16 = first_attempt_leaves_a_pending_question(conn, tmp_path, the_site)
    db.save_application(conn, "F" * 16, "dev", 90, "applied", "ok", [], CV_NAME, "IM9", "c8")  # otra oferta usó el mismo nombre
    answer_the_question(conn, id16)
    result = run(conn, tmp_path, FakePage(the_site))
    assert result["pdfs"] == {"before": 2, "max": 10, "uploaded": 1, "reused": 0} and the_site.uploads == [CV_NAME, CV_NAME]


def test_if_the_pdf_is_no_longer_in_the_account_it_uploads_as_before(conn, tmp_path):
    the_site = standard_site(cvs=(BASE_CV,), default=BASE_CV)
    id16 = first_attempt_leaves_a_pending_question(conn, tmp_path, the_site)
    answer_the_question(conn, id16)
    the_site.cvs.remove(CV_NAME)  # el usuario lo borró a mano
    the_site.default = BASE_CV
    result = run(conn, tmp_path, FakePage(the_site))
    assert result["pdfs"]["uploaded"] == 1 and result["pdfs"]["reused"] == 0 and the_site.uploads == [CV_NAME, CV_NAME]
    assert [i["id16"] for i in result["sent"]] == [id16]


def test_a_different_cv_version_uploads_as_before(conn, tmp_path):
    the_site = standard_site(cvs=(BASE_CV,), default=BASE_CV)
    id16 = first_attempt_leaves_a_pending_question(conn, tmp_path, the_site)
    answer_the_question(conn, id16)
    result = run(conn, tmp_path, FakePage(the_site), provider=stub_cv(version="c9"))
    assert result["pdfs"]["uploaded"] == 1 and result["pdfs"]["reused"] == 0 and the_site.uploads == [CV_NAME, CV_NAME]


def test_a_different_file_name_uploads_as_before(conn, tmp_path):
    the_site = standard_site(cvs=(BASE_CV,), default=BASE_CV)
    id16 = first_attempt_leaves_a_pending_question(conn, tmp_path, the_site)
    answer_the_question(conn, id16)
    result = run(conn, tmp_path, FakePage(the_site), provider=stub_cv(name="CV-Giovanni-Bianchi-Otro-Titular.pdf"))
    assert result["pdfs"]["uploaded"] == 1 and the_site.uploads[-1] == "CV-Giovanni-Bianchi-Otro-Titular.pdf"


def test_an_offer_without_a_saved_file_uploads(conn, tmp_path):
    add_offer(conn)
    the_site = standard_site()
    result = run(conn, tmp_path, FakePage(the_site))
    assert result["pdfs"]["uploaded"] == 1 and result["pdfs"]["reused"] == 0 and "CV reusado" not in report_text(result, tmp_path)


def test_a_reused_cv_that_is_not_the_default_after_marking_it_halts(conn, tmp_path):
    the_site = standard_site(cvs=(BASE_CV,), default=BASE_CV)
    id16 = first_attempt_leaves_a_pending_question(conn, tmp_path, the_site)
    answer_the_question(conn, id16)
    the_site.default = BASE_CV
    the_site.check_changes_default = False  # el radio no queda marcado
    result = run(conn, tmp_path, FakePage(the_site))
    assert result["sent"] == [] and "El CV por defecto de la cuenta no es" in result["halted"] and the_site.uploads == [CV_NAME]


# ── set_default: la recarga termina en cvedit ────────────────────────────────

def two_cvs():
    the_site = standard_site(cvs=(BASE_CV, "CV-Otro.pdf"), default="CV-Otro.pdf")
    the_site.radio_reloads_to_cvedit = True
    return the_site


def test_a_radio_whose_reload_ends_in_cvedit_does_not_halt_set_default():
    the_site = two_cvs()
    page = FakePage(the_site)
    page.check_error = PlaywrightError(NOT_ATTACHED)
    browser.set_default(page, BASE_CV)
    assert the_site.default == BASE_CV and page.url == CHECK_URL  # y la verificación recargó la página de CV


def test_a_radio_whose_reload_ends_in_cvedit_without_an_error_is_fine_too():
    the_site = two_cvs()
    page = FakePage(the_site)
    browser.set_default(page, BASE_CV)
    assert the_site.default == BASE_CV


def test_if_the_radio_reloads_to_cvedit_and_does_not_stay_checked_it_still_halts():
    the_site = two_cvs()
    the_site.check_changes_default = False
    page = FakePage(the_site)
    page.check_error = PlaywrightError(NOT_ATTACHED)
    with pytest.raises(browser.Halt, match="El CV por defecto de la cuenta no es «CV-Base.pdf»"):
        browser.set_default(page, BASE_CV)


def test_the_start_restoration_goes_on_when_the_reload_ends_in_cvedit(conn, tmp_path):
    add_offer(conn)
    the_site = standard_site(cvs=(BASE_CV, "CV-De-APEX.pdf"), default="CV-De-APEX.pdf")
    the_site.radio_reloads_to_cvedit = True
    page = FakePage(the_site)
    page.check_error = PlaywrightError(NOT_ATTACHED)
    result = run(conn, tmp_path, page, prof=make_profile({"salary": SALARY}, default_pdf_name=BASE_CV))
    assert result["halted"] is None and len(result["sent"]) == 1 and result["start_default"]["ok"] is True and result["restore"]["ok"] is True


# ── «La navegación no terminó» con la ruta ───────────────────────────────────

def test_an_exhausted_settle_says_the_last_screen_without_the_query():
    page = FakePage(standard_site())
    page.goto(KQ_URL)
    with pytest.raises(browser.Halt) as caught:
        browser._settle(page, [], ("postapply",))
    message = str(caught.value)
    assert "La navegación no terminó (última pantalla: candidato.ar.computrabajo.com/candidate/kq)" in message
    assert "oi=AAAA" not in message and "?" not in message.split(":")[0]


def test_the_exhausted_settle_of_the_start_restoration_names_the_screen(conn, tmp_path):
    add_offer(conn)
    the_site = standard_site(cvs=(BASE_CV, "CV-Otro.pdf"), default="CV-Otro.pdf")
    page = FakePage(the_site)
    page.check_error = PlaywrightError(NOT_ATTACHED)
    original = the_site.radio_reload
    the_site.radio_reload = lambda p: the_site.load(p, fakesite.OTHER_URL)  # la recarga termina en una pantalla conocida que no se espera
    result = run(conn, tmp_path, page, prof=make_profile({"salary": SALARY}, default_pdf_name=BASE_CV))
    assert "La navegación no terminó (última pantalla: candidato.ar.computrabajo.com/candidate/otra)" in result["halted"] or "Pantalla desconocida" in result["halted"]
    assert result["sent"] == []
    the_site.radio_reload = original


# ── regla del turno ──────────────────────────────────────────────────────────

SHIFT = {"text": "Si pudieras elegir tu jornada laboral de 4 hs cual seria", "kind": "closed",
         "options": ["Mañana de 9 a 13", "Tarde de 13 a 17"]}


def test_the_answer_prompt_has_the_shift_rule():
    system = apply.ANSWER_SYSTEM
    assert "pide elegir un turno u horario de trabajo" in system
    assert "elegí la primera opción que entre en `availability`" in system
    assert "si entran varias, la primera de la lista" in system and "si no entra ninguna, `unknown`" in system
    assert "Nunca inventes una disponibilidad que el candidato no declaró" in system


def test_the_request_to_the_model_carries_the_shift_rule_and_the_declared_availability():
    client, calls = FakeClient(reply({"option": 0, "source": "answers"})), []
    # las opciones traen horas (cifras): el usuario ya contestó esta pregunta en Preguntas
    declared = {"availability": "Mañana/Tarde", profile.normalize_question(SHIFT["text"]): "Mañana de 9 a 13"}
    answered = apply.answer_questions([SHIFT], make_profile(declared), client, MODEL, "dev", calls)
    [call] = client.calls
    assert "elegí la primera opción que entre en `availability`" in call["input"][0]["content"]
    assert "- availability: Mañana/Tarde" in call["input"][1]["content"] and "Mañana de 9 a 13" in call["input"][1]["content"]
    assert answered[0]["source"] == "answers" and answered[0]["option"] == 0


def test_a_shift_question_with_no_matching_option_stays_unknown():
    answered = apply.answer_questions([SHIFT], make_profile({"availability": "Noche"}), FakeClient(reply({"source": "unknown"})), MODEL, "dev", [])
    assert answered[0]["source"] == "unknown"
