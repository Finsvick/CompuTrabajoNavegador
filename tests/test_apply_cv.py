"""Tests de la postulación con CV a medida: nombre del PDF, subida y verificación, restauración del CV por defecto, revisión.

Misma base que test_apply_live.py: página falsa (tests/fakesite.py) y cliente falso; no se abre Computrabajo ni un navegador.
"""
import re
import shutil
from datetime import datetime
from pathlib import Path

import pytest
import yaml

import fakesite
from fakesite import APPLY_URL, BASE_CV, CHECK_URL, DETAIL_URL, KQ_URL, MATCH_URL, POSTAPPLY_URL, FakePage, fixture, standard_site
from test_apply_live import (unknown_modality_reply as unknown_reply, CV_NAME, MODEL, RESUME, SALARY, TODAY, FakeClient, add_offer, conn, go, known_reply, live_env, make_profile,
                             rows, status_of, stub_cv, write_profile_with)  # noqa: F401  (conn y live_env son fixtures)
from compuapply import apply, browser, cli, cv, db, profile, score, ui_logic

NO_QUESTIONS = (MATCH_URL, APPLY_URL, POSTAPPLY_URL)
OFFER = {"id16": "0000000000000001", "url": DETAIL_URL, "title": "Oferta 1"}


def fill_all(questions):
    return [{"kind": q["kind"], "options": q["options"], "option": 0 if q["kind"] == "closed" else None,
             "text": "Mi respuesta" if q["kind"] == "open" else None} for q in questions]


def cv_info(tmp_path, name=CV_NAME, version="c8"):
    folder = tmp_path / "copia"
    folder.mkdir(exist_ok=True)
    (folder / name).write_bytes(b"%PDF-" + b"0" * 100)
    return {"path": folder / name, "name": name, "version": version, "dir": folder, "cost_usd": 0.0}


# ── nombre del PDF ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("name, surname, title, expected", [
    ("Juan", "Apellido", "Desarrollador/a Frontend", "CV-Juan-Apellido-Desarrollador-a-Frontend.pdf"),
    ("Juan Carlos", "Núñez Güemes", "Analista de Datos (Jr.)", "CV-Juan-Carlos-Nunez-Guemes-Analista-de-Datos-Jr.pdf"),
    ("Ana", "Pérez", "", "CV-Ana-Perez.pdf"),
    ("", "", "Soporte técnico", "CV-Soporte-tecnico.pdf"),
])
def test_pdf_name_has_only_letters_digits_and_hyphens_without_accents(name, surname, title, expected):
    assert cv.pdf_name({"name": name, "surname": surname}, title) == expected


def test_pdf_name_cuts_a_long_title_at_80_characters_without_a_trailing_hyphen():
    stem = cv.pdf_name({"name": "Ana", "surname": "Pérez"}, "Desarrollador Backend " * 10)
    assert stem.endswith(".pdf") and len(stem) - len(".pdf") <= 80 and stem.startswith("CV-Ana-Perez-Desarrollador-Backend")
    assert not stem[:-4].endswith("-")
    exact = cv.pdf_name({"name": "A", "surname": "B"}, "x" * 200)
    assert len(exact) == 80 + len(".pdf")
    assert cv.pdf_name(None, "Título") == "CV-Titulo.pdf" and cv.pdf_name({"name": 5}, "") == "CV-5.pdf"


def test_headline_of_reads_the_title_of_a_saved_cv_unescaped(tmp_path):
    selection = {"headline": "Analista & más", "summary": {"text": "", "sources": []},
                 "experiences": {}, "technical_skills": [], "projects": [], "achievements": [], "certifications": [], "interests": []}
    path = tmp_path / "cv.html"
    path.write_text(cv.build_html(RESUME, {**selection, "experiences": {f"E{n}": {"block": "otra", "bullets": [], "responsibilities": [], "skills": []}
                                                                         for n in (1, 2, 3)}}), encoding="utf-8")
    assert cv.headline_of(path) == "Analista & más"
    path.write_text("<html><body>sin titular</body></html>", encoding="utf-8")
    assert cv.headline_of(path) == "" and cv.headline_of(tmp_path / "no-existe.html") == ""


# ── perfil y formulario ──────────────────────────────────────────────────────

def test_the_profile_loads_review_cv_max_account_pdfs_and_default_pdf_name(tmp_path):
    (tmp_path / "a.yaml").write_text(yaml.safe_dump(RESUME), encoding="utf-8")
    path = tmp_path / "profile.yaml"
    path.write_text(yaml.safe_dump({"variants": [{"id": "dev", "resume": "a.yaml", "default_pdf_name": " CV-Base.pdf "}, {"id": "b", "resume": "a.yaml"}],
                                    "apply": {"review_cv": False}, "run": {"max_account_pdfs": 5}}), encoding="utf-8")
    loaded = profile.load_profile(path)
    assert loaded["apply"]["review_cv"] is False and loaded["run"]["max_account_pdfs"] == 5
    assert loaded["variants"][0]["default_pdf_name"] == "CV-Base.pdf" and "default_pdf_name" not in loaded["variants"][1]
    plain = profile.load_profile(write_profile_with(tmp_path))
    assert "review_cv" not in plain["apply"] and "max_account_pdfs" not in plain["run"]
    assert apply.review_required(plain) is True and apply.review_required(loaded) is False  # sin declarar vale «revisar»


@pytest.mark.parametrize("extra, message", [
    ({"apply": {"review_cv": "no"}}, "review_cv.*true o false"),
    ({"apply": {"review_cv": 0}}, "review_cv.*true o false"),
    ({"run": {"max_account_pdfs": 0}}, "max_account_pdfs.*1 o más"),
    ({"run": {"max_account_pdfs": "20"}}, "max_account_pdfs.*1 o más"),
])
def test_bad_values_give_a_clear_error(tmp_path, extra, message):
    with pytest.raises(RuntimeError, match=message):
        profile.load_profile(write_profile_with(tmp_path, **extra))


def test_a_bad_default_pdf_name_is_a_clear_error(tmp_path):
    (tmp_path / "a.yaml").write_text(yaml.safe_dump(RESUME), encoding="utf-8")
    path = tmp_path / "profile.yaml"
    path.write_text(yaml.safe_dump({"variants": [{"id": "dev", "resume": "a.yaml", "default_pdf_name": ["x"]}]}), encoding="utf-8")
    with pytest.raises(RuntimeError, match="default_pdf_name.*dev"):
        profile.load_profile(path)


def test_the_form_round_trips_review_cv_and_saving_keeps_the_fields_it_does_not_have(tmp_path):
    (tmp_path / "a.yaml").write_text(yaml.safe_dump(RESUME), encoding="utf-8")
    path = tmp_path / "profile.yaml"
    path.write_text(yaml.safe_dump({"variants": [{"id": "dev", "resume": "a.yaml", "keywords": ["x"], "default_pdf_name": "CV-Base.pdf"}],
                                    "apply": {"threshold": 70, "daily_cap": 5, "review_cv": False}, "run": {"max_account_pdfs": 7}}), encoding="utf-8")
    form = ui_logic.profile_to_form(path)
    assert form["review_cv"] == "false"
    assert ui_logic.form_to_profile(form)["apply"] == {"threshold": 70, "daily_cap": 5, "company_daily_cap": 1, "review_cv": False}
    form["review_cv"] = "true"
    ui_logic.save_profile(ui_logic.form_to_profile(form), path)
    saved = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert saved["apply"]["review_cv"] is True and saved["variants"][0]["default_pdf_name"] == "CV-Base.pdf" and saved["run"]["max_account_pdfs"] == 7
    form["review_cv"] = ""  # el formulario sin el dato no escribe nada
    assert "review_cv" not in ui_logic.form_to_profile(form)["apply"] and ui_logic.empty_form()["review_cv"] == ""


def test_live_blocker_says_why_nothing_is_uploaded_while_the_review_is_on(tmp_path):
    path = write_profile_with(tmp_path)
    assert ui_logic.live_blocker(path) == apply.REVIEW_MESSAGE and "Parámetros" in apply.REVIEW_MESSAGE
    path = write_profile_with(tmp_path, apply={"threshold": 70, "review_cv": False})
    assert ui_logic.live_blocker(path) is None


def test_save_application_keeps_the_file_the_im_and_the_cv_version(conn):
    db.save_application(conn, "A" * 16, "dev", 80, "applied", "ok", [], CV_NAME, "IM123", "c8")
    assert conn.execute("SELECT cv_file, im, cv_version FROM applications").fetchone() == (CV_NAME, "IM123", "c8")
    db.save_application(conn, "B" * 16, "dev", 80, "pending_question", "x", [])
    assert conn.execute("SELECT cv_file, im, cv_version FROM applications WHERE id16 = ?", ("B" * 16,)).fetchone() == (None, None, None)


# ── browser: la página de CV de la cuenta ────────────────────────────────────

def test_account_cvs_lists_the_pdfs_and_the_default_without_the_no_attach_option():
    page = FakePage(standard_site(cvs=(BASE_CV, "Otro.pdf"), default="Otro.pdf"))
    assert browser.account_cvs(page) == [{"name": BASE_CV, "default": False, "id": "fc0001"}, {"name": "Otro.pdf", "default": True, "id": "fc0002"}]
    assert page.visited == [CHECK_URL] and page.clicks() == []


def test_account_cvs_halts_without_a_session():
    site = standard_site()
    original = site.load
    site.load = lambda page, url, keep=False: original(page, fakesite.LOGIN_URL if url == CHECK_URL else url)
    with pytest.raises(browser.Halt, match="Sin sesión"):
        browser.account_cvs(FakePage(site))


def test_upload_cv_uploads_the_file_and_checks_it_is_the_default(tmp_path):
    site = standard_site()
    page = FakePage(site)
    info = cv_info(tmp_path)
    browser.upload_cv(page, info["path"], info["name"], [])
    assert site.uploads == [CV_NAME] and site.default == CV_NAME and ("upload", CV_NAME) in page.log
    assert page.visited == [CHECK_URL, CHECK_URL]  # abre la página, sube y la recarga para verificar


def test_upload_cv_halts_if_the_new_pdf_did_not_become_the_default(tmp_path):
    info = cv_info(tmp_path)
    with pytest.raises(browser.Halt, match="no es"):
        browser.upload_cv(FakePage(standard_site(upload_makes_default=False)), info["path"], info["name"], [])


def test_upload_cv_halts_without_the_upload_field(tmp_path):
    site = standard_site()
    site.html_for = lambda url, keep=False: "<html><body><input type='radio' name='cv' value='0'></body></html>"
    info = cv_info(tmp_path)
    with pytest.raises(browser.Halt, match="No hay campo para subir"):
        browser.upload_cv(FakePage(site), info["path"], info["name"], [])


def test_set_default_marks_the_pdf_and_checks_it_by_reloading():
    site = standard_site(cvs=(BASE_CV, CV_NAME), default=CV_NAME)
    page = FakePage(site)
    browser.set_default(page, "CV- Base.pdf")  # Computrabajo saca los espacios: se compara sin ellos
    assert site.default == BASE_CV and ("default", f"{1:032x}") in page.log


def test_set_default_does_nothing_if_it_already_is_and_halts_if_it_does_not_stick_or_is_missing():
    site = standard_site(cvs=(BASE_CV, CV_NAME), default=BASE_CV)
    page = FakePage(site)
    browser.set_default(page, BASE_CV)
    assert page.log == []
    with pytest.raises(browser.Halt, match="no es"):
        browser.set_default(FakePage(standard_site(cvs=(BASE_CV, CV_NAME), default=CV_NAME, check_changes_default=False)), BASE_CV)
    with pytest.raises(browser.Halt, match="no está en la cuenta"):
        browser.set_default(FakePage(standard_site()), "Otro-que-no-existe.pdf")


# ── browser.apply_one con CV a medida ────────────────────────────────────────

def calls_of(page):
    return [entry[0] for entry in page.log]


def test_apply_one_uploads_the_cv_then_posts_with_it_and_checks_the_attached_file(tmp_path):
    site, made = standard_site(), []
    page = FakePage(site)
    outcome = browser.apply_one(page, OFFER, fill_all, lambda: made.append(1) or cv_info(tmp_path))
    assert outcome["status"] == "applied" and outcome["im"] == "IM123" and outcome["alert"] is True and outcome["cv"]["name"] == CV_NAME
    assert outcome["qa"] and made == [1]
    # el orden: detalle -> página de CV (subir y verificar) -> detalle -> «Postularme» -> enviar; nunca se navega a match/ ni a apply/
    assert page.visited == [DETAIL_URL, CHECK_URL, CHECK_URL, DETAIL_URL]
    assert site.uploads == [CV_NAME] and site.attached == CV_NAME and len(site.submitted) == 1
    assert calls_of(page).index("upload") < calls_of(page).index("click")


def test_apply_one_never_opens_match_or_apply_itself_and_uploads_before_touching_postularme(tmp_path):
    page = FakePage(standard_site())
    browser.apply_one(page, OFFER, fill_all, lambda: cv_info(tmp_path))
    assert MATCH_URL not in page.visited and APPLY_URL not in page.visited and KQ_URL not in page.visited
    assert calls_of(page).index("upload") < calls_of(page).index("click")


def test_apply_one_match_saying_already_posted_after_the_click_is_already_applied_and_the_cv_stays_uploaded(tmp_path):
    site = standard_site(after_apply=(MATCH_URL,), pages={MATCH_URL: fixture("apply_match_applied.html")})
    outcome = browser.apply_one(FakePage(site), OFFER, fill_all, lambda: cv_info(tmp_path))
    assert outcome["status"] == "already_applied" and outcome["cv"]["name"] == CV_NAME
    assert site.uploads == [CV_NAME] and site.submitted == []


def test_apply_one_without_questions_reaches_postapply_with_the_uploaded_cv_and_replaces_nothing(tmp_path):
    site = standard_site(after_apply=NO_QUESTIONS)
    page = FakePage(site)
    outcome = browser.apply_one(page, OFFER, fill_all, lambda: cv_info(tmp_path))
    assert outcome["status"] == "applied" and outcome["repaired"] is False and outcome["im"] == "IM123" and outcome["qa"] == []
    assert site.attached == CV_NAME and site.uploads == [CV_NAME] and site.submitted == []
    assert [e[1] for e in page.clicks()] == ["a"] and MATCH_URL not in page.visited  # solo «Postularme»; nada de «Actualizar»


def test_apply_one_repairs_a_wrong_attached_file_from_postapply_and_says_so(tmp_path):
    site = standard_site(attached_override="Otro-CV.pdf")
    outcome = browser.apply_one(FakePage(site), OFFER, fill_all, lambda: cv_info(tmp_path))
    assert outcome["status"] == "applied" and outcome["repaired"] is True
    assert site.attached == CV_NAME and site.uploads == [CV_NAME, CV_NAME]  # la subida y la reparación


def test_apply_one_without_questions_also_repairs_a_wrong_attached_file(tmp_path):
    site = standard_site(after_apply=NO_QUESTIONS, attached_override="Otro-CV.pdf")
    outcome = browser.apply_one(FakePage(site), OFFER, fill_all, lambda: cv_info(tmp_path))
    assert outcome["repaired"] is True and site.attached == CV_NAME


def test_apply_one_halts_if_the_repair_does_not_leave_the_uploaded_file(tmp_path):
    site = standard_site(attached_override="Otro-CV.pdf", edit_changes=False)
    with pytest.raises(browser.Halt, match=r"no es el subido \(se ve «Otro-CV.pdf»"):
        browser.apply_one(FakePage(site), OFFER, fill_all, lambda: cv_info(tmp_path))
    assert len(site.submitted) == 1  # ya se había enviado: el freno avisa, no deshace


def test_apply_one_halts_if_the_upload_does_not_become_the_default_and_never_posts(tmp_path):
    site = standard_site(upload_makes_default=False)
    page = FakePage(site)
    with pytest.raises(browser.Halt):
        browser.apply_one(page, OFFER, fill_all, lambda: cv_info(tmp_path))
    assert site.submitted == [] and page.clicks() == []


def test_apply_one_with_an_unanswered_question_leaves_the_uploaded_cv_and_sends_nothing(tmp_path):
    site = standard_site()
    outcome = browser.apply_one(FakePage(site), OFFER, lambda questions: None, lambda: cv_info(tmp_path))
    assert outcome["status"] == "pending_question" and site.submitted == [] and site.uploads == [CV_NAME]


# ── run_live: revisión, tope de PDFs, restauración ───────────────────────────

def test_with_the_review_on_run_live_stops_before_building_or_uploading_anything(conn, tmp_path):
    add_offer(conn)
    built, page = [], FakePage(standard_site())
    result = apply.run_live(conn, make_profile(review=True), MODEL, None, page, TODAY, stop_file=tmp_path / "STOP", pause=lambda: None,
                            cv_provider=stub_cv(calls=built))
    assert result["halted"] == apply.REVIEW_MESSAGE and result["sent"] == [] and built == [] and page.visited == []
    assert [i["reason"] for i in result["skipped"]] == ["sin procesar: la revisión de CVs está activada"] and result["restore"] is None
    assert status_of(conn, "0000000000000001") == "new" and rows(conn, "SELECT * FROM applications") == []
    default = make_profile()
    del default["apply"]["review_cv"]  # sin declarar: vale revisar
    assert apply.run_live(conn, default, MODEL, None, page, TODAY, stop_file=tmp_path / "STOP", pause=lambda: None)["halted"] == apply.REVIEW_MESSAGE


def test_the_review_message_and_the_report_say_what_to_do(conn, tmp_path):
    add_offer(conn)
    result = apply.run_live(conn, make_profile(review=True), MODEL, None, None, TODAY, stop_file=tmp_path / "STOP")
    text = apply.write_live_report(result, {"now": datetime(2026, 11, 3, 10, 0, 0), "threshold": 70, "daily_cap": 10,
                                           "applied_today": 0, "model": MODEL}, tmp_path / "runs").read_text(encoding="utf-8")
    assert "## Freno" in text and "Revisá los CVs de data/cvs/" in text and "apagá la revisión" in text


def test_with_more_pdfs_than_the_maximum_run_live_does_not_post(conn, tmp_path):
    add_offer(conn)
    prof = make_profile()
    prof["run"]["max_account_pdfs"] = 2
    built = []
    result, page = go(conn, tmp_path, standard_site(cvs=("A.pdf", "B.pdf", "C.pdf"), default="A.pdf"), FakeClient(known_reply()), prof=prof,
                      provider=stub_cv(calls=built))
    assert "Hay 3 PDFs" in result["halted"] and "run.max_account_pdfs" in result["halted"] and "limpiá" in result["halted"]
    assert result["pdfs"] == {"before": 3, "max": 2, "uploaded": 0, "reused": 0} and built == [] and page.visited == [CHECK_URL] and result["sent"] == []
    assert result["restore"] is None and status_of(conn, "0000000000000001") == "new"


def test_the_default_maximum_of_pdfs_is_10(conn, tmp_path):
    add_offer(conn)
    names = tuple(f"{n}.pdf" for n in range(11))
    result, _ = go(conn, tmp_path, standard_site(cvs=names, default="0.pdf"), FakeClient(known_reply()))
    assert result["pdfs"]["max"] == 10 and "Hay 11 PDFs" in result["halted"]
    ok, _ = go(conn, tmp_path, standard_site(cvs=names[:9], default="0.pdf"), FakeClient(known_reply()))
    assert ok["halted"] is None and len(ok["sent"]) == 1


def test_run_live_uploads_posts_records_the_file_the_im_and_the_version_and_cleans_the_copy(conn, tmp_path):
    id16 = add_offer(conn)
    built, site = [], standard_site()
    result, page = go(conn, tmp_path, site, FakeClient(known_reply()), provider=stub_cv(calls=built))
    assert [i["id16"] for i in result["sent"]] == [id16] and result["halted"] is None and built == [id16]
    assert result["sent"][0]["file"] == CV_NAME and result["sent"][0]["version"] == "c8" and result["sent"][0]["alert"] is True
    assert rows(conn, "SELECT status, cv_file, im, cv_version FROM applications") == [("applied", CV_NAME, "IM123", "c8")]
    assert site.uploads == [CV_NAME] and site.attached == CV_NAME
    assert result["pdfs"] == {"before": 1, "max": 10, "uploaded": 1, "reused": 0}


def test_run_live_removes_the_copy_of_the_pdf_when_the_offer_is_done(conn, tmp_path):
    add_offer(conn)
    made = []
    base = stub_cv()

    def provider(item):
        info = base(item)
        made.append(info["dir"])
        assert info["path"].is_file()
        return info

    result, _ = go(conn, tmp_path, standard_site(), FakeClient(known_reply()), provider=provider)
    assert len(result["sent"]) == 1 and made and not made[0].exists()  # la copia con el nombre presentable no queda en disco


def test_run_live_removes_the_copy_even_when_the_offer_halts(conn, tmp_path):
    add_offer(conn)
    made = []
    base = stub_cv()

    def provider(item):
        info = base(item)
        made.append(info["dir"])
        return info

    site = standard_site(attached_override="Otro-CV.pdf", edit_changes=False)
    result, _ = go(conn, tmp_path, site, FakeClient(known_reply()), provider=provider)
    assert "no es el subido" in result["halted"] and made and not made[0].exists()


def test_run_live_skips_an_offer_that_is_in_my_applications_without_building_uploading_or_opening_it(conn, tmp_path):
    id16 = add_offer(conn, title="Analista de pruebas", company="Empresa Falsa SA", location="Rosario, Santa Fe")
    site, built = standard_site(), []
    result, page = go(conn, tmp_path, site, FakeClient(known_reply()), provider=stub_cv(calls=built))
    assert [i["reason"] for i in result["skipped"]] == ["ya estaba postulada (figura en Mis postulaciones)"] and result["sent"] == []
    assert built == [] and site.uploads == [] and result["halted"] is None and result["read"] == 4
    assert DETAIL_URL not in page.visited and MATCH_URL not in page.visited and page.clicks() == []
    assert status_of(conn, id16) == "applied" and result["restore"] is None and result["pdfs"]["uploaded"] == 0
    assert rows(conn, "SELECT status, reason, im, cv_file FROM applications") == [("already_applied", "ya estaba en Mis postulaciones", "IM00000000000000000000000000000A1", None)]
    assert apply.applied_today(conn, TODAY) == 0  # no cuenta para el tope diario


def test_the_key_of_my_applications_ignores_case_accents_and_extra_spaces_and_drops_an_empty_company(conn, tmp_path):
    first = add_offer(conn, 1, 90, title="  ANALISTA de   PRUEBAS ", company=" empresa  FALSA sa ", location="rosario,  santa fe")
    second = add_offer(conn, 2, 80, title="Programador Confidencial", company="", location="CORDOBA,  cordoba")
    result, _ = go(conn, tmp_path, standard_site(), FakeClient(known_reply()))
    assert sorted(i["id16"] for i in result["skipped"]) == sorted([first, second]) and result["sent"] == []
    assert rows(conn, "SELECT id16, im FROM applications ORDER BY id16") == [
        (first, "IM00000000000000000000000000000A1"), (second, "IM00000000000000000000000000000B2")]


@pytest.mark.parametrize("title, company, location", [
    ("Analista de pruebas", "Otra Empresa SA", "Rosario, Santa Fe"),  # otra empresa
    ("Analista de pruebas", "Empresa Falsa SA", "Mendoza"),  # otro lugar
    ("Analista de datos", "Empresa Falsa SA", "Rosario, Santa Fe"),  # otro título
    ("Analista de pruebas", "", "Rosario, Santa Fe"),  # sin empresa no calza con una tarjeta que sí la tiene
    ("Programador confidencial", "Una Empresa", "Córdoba, Córdoba"),  # con empresa no calza con la confidencial
])
def test_an_offer_that_does_not_match_a_card_goes_on_to_be_posted(conn, tmp_path, title, company, location):
    id16 = add_offer(conn, title=title, company=company, location=location)
    result, _ = go(conn, tmp_path, standard_site(), FakeClient(known_reply()))
    assert [i["id16"] for i in result["sent"]] == [id16] and result["skipped"] == []


def test_a_skipped_offer_does_not_cost_a_pause_and_the_next_one_is_still_posted(conn, tmp_path):
    add_offer(conn, 1, 95, title="Analista de pruebas", company="Empresa Falsa SA", location="Rosario, Santa Fe")
    second = add_offer(conn, 2, 90)
    pauses, page = [], FakePage(standard_site())
    result = apply.run_live(conn, make_profile({"salary": SALARY}), MODEL, FakeClient(known_reply()), page, TODAY, stop_file=tmp_path / "STOP",
                            pause=lambda: pauses.append(1), cv_provider=stub_cv())
    assert [i["id16"] for i in result["sent"]] == [second] and len(result["skipped"]) == 1 and pauses == []


def test_run_live_match_saying_already_posted_after_the_click_marks_applied_and_restores_the_default(conn, tmp_path):
    id16 = add_offer(conn)
    site = standard_site(after_apply=(MATCH_URL,), pages={MATCH_URL: fixture("apply_match_applied.html")})
    result, _ = go(conn, tmp_path, site, FakeClient(known_reply()), prof=make_profile(default_pdf_name=BASE_CV))
    assert [i["reason"] for i in result["skipped"]] == ["ya estaba postulada"] and result["sent"] == []
    assert status_of(conn, id16) == "applied" and site.uploads == [CV_NAME]  # el CV subido queda en la cuenta
    assert site.default == BASE_CV and result["restore"]["ok"] is True  # y el por defecto se restaura


def test_run_live_halts_without_session_when_reading_my_applications(conn, tmp_path):
    add_offer(conn)
    site = standard_site()
    original = site.load
    site.load = lambda page, url, keep=False: original(page, fakesite.LOGIN_URL if url == fakesite.APPLICATIONS_URL else url)
    built = []
    result, page = go(conn, tmp_path, site, FakeClient(known_reply()), provider=stub_cv(calls=built))
    assert "Sin sesión" in result["halted"] and built == [] and result["sent"] == [] and DETAIL_URL not in page.visited
    assert [i["reason"] for i in result["skipped"]] == ["sin procesar: la corrida frenó"] and result["read"] is None


def two_variants(first_name=BASE_CV, second_name="CV-Ventas.pdf"):
    prof = make_profile({"salary": SALARY})
    other = {"id": "ventas", "keywords": ["x"], "resume": RESUME, "pdf": None}
    if first_name:
        prof["variants"][0]["default_pdf_name"] = first_name
    if second_name:
        other["default_pdf_name"] = second_name
    prof["variants"].append(other)
    return prof


def test_with_two_variants_the_first_default_pdf_name_of_the_profile_is_restored_not_the_last_offers(conn, tmp_path):
    add_offer(conn, 1, 90, variant="dev")
    add_offer(conn, 2, 80, variant="ventas")  # la última oferta es de la segunda variante
    site = standard_site(cvs=(BASE_CV, "CV-Ventas.pdf"), default="CV-Ventas.pdf", after_apply=NO_QUESTIONS)
    result, _ = go(conn, tmp_path, site, FakeClient(known_reply()), prof=two_variants())
    assert len(result["sent"]) == 2 and site.default == BASE_CV
    assert result["restore"] == {"name": BASE_CV, "variant": "dev", "ok": True, "message": ""}


def test_the_first_variant_that_declares_a_name_is_the_one_even_if_the_offer_is_of_another(conn, tmp_path):
    add_offer(conn, 1, 90, variant="dev")
    site = standard_site(cvs=(BASE_CV, "CV-Ventas.pdf"), default=BASE_CV, after_apply=NO_QUESTIONS)
    result, _ = go(conn, tmp_path, site, FakeClient(known_reply()), prof=two_variants(first_name=None, second_name="CV-Ventas.pdf"))
    assert site.default == "CV-Ventas.pdf" and result["restore"]["variant"] == "ventas" and result["restore"]["name"] == "CV-Ventas.pdf"


def test_the_report_names_the_restored_cv_and_its_variant_the_applications_read_and_the_pending_questions(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(apply, "availability_answer", lambda *a, **k: None)  # el modelo deja una sin respuesta
    add_offer(conn, 1, 90)
    add_offer(conn, 2, 80)
    site = standard_site(cvs=(BASE_CV, "CV-Ventas.pdf"), default="CV-Ventas.pdf")
    result, _ = go(conn, tmp_path, site, FakeClient(unknown_reply(), known_reply()), prof=two_variants())
    text = apply.write_live_report(result, {"now": datetime(2026, 11, 3, 10, 0, 0), "threshold": 70, "daily_cap": 10, "applied_today": 0,
                                           "model": MODEL}, tmp_path / "runs").read_text(encoding="utf-8")
    assert f"restaurado «{BASE_CV}» (declarado en `default_pdf_name`)" in text
    assert "- Mis postulaciones (solo lectura): 4 leídas" in text
    assert "- Ofertas con pregunta sin respuesta: 1; su CV ya quedó subido en la cuenta" in text
    assert result["pdfs"]["uploaded"] == 2  # la que quedó con pregunta también subió su CV


def test_the_report_counts_the_repaired_attachments(conn, tmp_path):
    add_offer(conn)
    site = standard_site(attached_override="Otro-CV.pdf", after_apply=NO_QUESTIONS)
    result, _ = go(conn, tmp_path, site, FakeClient(known_reply()))
    assert result["sent"][0]["repaired"] is True
    text = apply.write_live_report(result, {"now": datetime(2026, 11, 3, 10, 0, 0), "threshold": 70, "daily_cap": 10, "applied_today": 0,
                                           "model": MODEL}, tmp_path / "runs").read_text(encoding="utf-8")
    assert "Adjunto reparado desde la pantalla final (no era el subido): 1 de 1 postulaciones." in text
    assert rows(conn, "SELECT reason FROM applications") == [("Te postulaste correctamente (el adjunto no era el subido y se cambió desde la pantalla final)",)]


def test_run_live_restores_the_base_cv_as_default_at_the_end(conn, tmp_path):
    add_offer(conn)
    site = standard_site()
    result, _ = go(conn, tmp_path, site, FakeClient(known_reply()), prof=make_profile({"salary": SALARY}, default_pdf_name=BASE_CV))
    assert site.default == BASE_CV and CV_NAME in site.cvs  # el subido sigue en la cuenta, no se borra nada
    assert result["restore"] == {"name": BASE_CV, "variant": "dev", "ok": True, "message": ""} and result["halted"] is None


def test_run_live_restores_the_base_cv_even_when_the_run_halts_halfway(conn, tmp_path):
    add_offer(conn, 1, 90)
    add_offer(conn, 2, 80)
    site = standard_site(attached_override="Otro-CV.pdf", edit_changes=False)  # la primera oferta frena después de haber subido el CV
    result, _ = go(conn, tmp_path, site, FakeClient(known_reply()), prof=make_profile({"salary": SALARY}, default_pdf_name=BASE_CV))
    assert "no es el subido" in result["halted"] and site.default == BASE_CV and result["restore"]["ok"] is True
    assert len(site.uploads) == 1  # la segunda ni se tocó


def test_run_live_restores_the_base_cv_after_a_stop_between_offers(conn, tmp_path):
    add_offer(conn, 1, 90)
    add_offer(conn, 2, 80)
    stop = tmp_path / "STOP"
    site = standard_site(after_apply=NO_QUESTIONS)
    page = FakePage(site)
    result = apply.run_live(conn, make_profile(default_pdf_name=BASE_CV), MODEL, None, page, TODAY, stop_file=stop,
                            pause=lambda: stop.write_text("x"), cv_provider=stub_cv())
    assert len(result["sent"]) == 1 and "Existe el archivo" in result["halted"] and site.default == BASE_CV


def test_run_live_without_default_pdf_name_warns_that_the_last_cv_stays_as_default(conn, tmp_path):
    add_offer(conn)
    site = standard_site()
    result, _ = go(conn, tmp_path, site, FakeClient(known_reply()))
    assert site.default == CV_NAME and result["restore"]["ok"] is None and "default_pdf_name" in result["restore"]["message"]
    text = apply.write_live_report(result, {"now": datetime(2026, 11, 3, 10, 0, 0), "threshold": 70, "daily_cap": 10,
                                           "applied_today": 0, "model": MODEL}, tmp_path / "runs").read_text(encoding="utf-8")
    assert "quedó por defecto el último CV subido" in text


def test_run_live_reports_when_the_base_cv_could_not_be_restored(conn, tmp_path):
    add_offer(conn)
    site = standard_site(check_changes_default=False)
    result, _ = go(conn, tmp_path, site, FakeClient(known_reply()), prof=make_profile({"salary": SALARY}, default_pdf_name=BASE_CV))
    assert result["restore"]["ok"] is False and "No se pudo dejar" in result["halted"] and BASE_CV in result["halted"]
    assert len(result["sent"]) == 1  # lo enviado queda enviado


def test_run_live_a_restore_failure_does_not_hide_the_first_stop_message(conn, tmp_path):
    add_offer(conn)
    site = standard_site(check_changes_default=False, attached_override="Otro-CV.pdf", edit_changes=False)
    result, _ = go(conn, tmp_path, site, FakeClient(known_reply()), prof=make_profile({"salary": SALARY}, default_pdf_name=BASE_CV))
    assert "no es el subido" in result["halted"] and result["restore"]["ok"] is False


def test_the_live_report_has_the_account_the_file_the_version_and_the_alerts(conn, tmp_path):
    add_offer(conn, 1, 90)
    add_offer(conn, 2, 80)
    site = standard_site(after_apply=NO_QUESTIONS, setalert=True)
    result, _ = go(conn, tmp_path, site, FakeClient(known_reply()), prof=make_profile(default_pdf_name=BASE_CV), provider=stub_cv("c4"))
    meta = {"now": datetime(2026, 11, 3, 10, 0, 0), "threshold": 70, "daily_cap": 10, "applied_today": 0, "model": MODEL}
    text = apply.write_live_report(result, meta, tmp_path / "runs").read_text(encoding="utf-8")
    assert "- PDFs en la cuenta al empezar: 1 (máximo 10); CVs subidos en esta corrida: 2." in text
    assert f"- CV por defecto al terminar: restaurado «{BASE_CV}» (declarado en `default_pdf_name`)" in text
    assert "solo con ids (c4, sin texto adaptado): 2 de 2 enviadas" in text
    assert "`setalert.ashx` en 2 de 2 postulaciones" in text and "no toca la configuración de alertas" in text
    assert f"| subido como {CV_NAME} · copia local {CV_NAME} | c4 |" in text and "CV subido" in text and "versión del CV" in text


def test_the_report_says_when_the_site_did_not_ask_for_alerts(conn, tmp_path):
    add_offer(conn)
    result, _ = go(conn, tmp_path, standard_site(setalert=False), FakeClient(known_reply()))
    text = apply.write_live_report(result, {"now": datetime(2026, 11, 3, 10, 0, 0), "threshold": 70, "daily_cap": 10,
                                           "applied_today": 0, "model": MODEL}, tmp_path / "runs").read_text(encoding="utf-8")
    assert "`setalert.ashx` en 0 de 1 postulaciones" in text


# ── el proveedor de CV real ──────────────────────────────────────────────────

def saved_cv(conn, tmp_path, id16, version, variant="dev", headline="Desarrollador de software", with_files=True):
    html = tmp_path / "cvs" / f"{id16}-{variant}.html"
    pdf = html.with_suffix(".pdf")
    if with_files:
        html.parent.mkdir(exist_ok=True)
        html.write_text(f'<html><body><div class="headline">{headline}</div></body></html>', encoding="utf-8")
        pdf.write_bytes(b"%PDF-" + b"0" * 100)
    db.save_cv(conn, id16, variant, MODEL, version, str(html), str(pdf), 0.0, 1, 1)


def test_the_provider_reuses_the_current_cv_names_the_copy_and_does_not_call_the_model(conn, tmp_path, monkeypatch):
    id16 = add_offer(conn)
    saved_cv(conn, tmp_path, id16, cv.CV_PROMPT_VERSION)
    monkeypatch.setattr(cv, "make", lambda *a, **k: pytest.fail("no tenía que armarse otro CV"))
    info = apply.make_cv_provider(conn, make_profile(), None, MODEL)({"id16": id16, "variant": "dev"})
    assert info["name"] == "CV-Giovanni-Bianchi-X.pdf" and info["local"] == "CV-Giovanni-Bianchi-Oferta-1-Empresa-1.pdf"
    assert info["path"].is_file() and info["path"].name == info["name"] and info["version"] == "c8"
    assert info["path"].read_bytes().startswith(b"%PDF-") and info["cost_usd"] == 0.0
    shutil.rmtree(info["dir"])


@pytest.mark.parametrize("version, with_files", [("c4", True), ("c7", True), ("c8", False), (None, False)])
def test_the_provider_builds_a_new_cv_if_there_is_no_current_c8_one(conn, tmp_path, monkeypatch, version, with_files):
    id16 = add_offer(conn)
    if version:
        saved_cv(conn, tmp_path, id16, version, with_files=with_files)
    made = []

    def fake_make(connection, prof, client, model, wanted, out_dir=None, stop_file=None):
        made.append((wanted, out_dir))
        html = Path(out_dir) / "nuevo.html"
        html.parent.mkdir(parents=True, exist_ok=True)
        html.write_text('<div class="headline">Analista &amp; más</div>', encoding="utf-8")
        pdf = html.with_suffix(".pdf")
        pdf.write_bytes(b"%PDF-" + b"0" * 100)
        return {"html": html, "pdf": pdf, "version": "c4", "cost_usd": 0.0002}

    monkeypatch.setattr(cv, "make", fake_make)
    info = apply.make_cv_provider(conn, make_profile(), FakeClient(), MODEL, tmp_path / "salida")({"id16": id16, "variant": "dev"})
    assert made == [(id16, tmp_path / "salida")] and info["version"] == "c4" and info["cost_usd"] == 0.0002
    assert info["name"] == "CV-Giovanni-Bianchi-X.pdf" and info["local"] == "CV-Giovanni-Bianchi-Oferta-1-Empresa-1.pdf"
    shutil.rmtree(info["dir"])


def test_the_provider_needs_the_key_only_when_it_has_to_build_the_cv(conn, tmp_path):
    id16 = add_offer(conn)
    with pytest.raises(RuntimeError, match="Falta la clave de OpenAI"):
        apply.make_cv_provider(conn, make_profile(), None, MODEL)({"id16": id16, "variant": "dev"})


def test_a_cv_that_cannot_be_built_halts_the_run_and_the_error_comes_from_cv_make(conn, tmp_path, monkeypatch):
    add_offer(conn)

    def refuse(*a, **k):
        raise RuntimeError("Tope mensual alcanzado: US$ 2,0000 de US$ 2,00. No se arma el CV.")

    monkeypatch.setattr(cv, "make", refuse)
    page = FakePage(standard_site())
    result = apply.run_live(conn, make_profile(), MODEL, FakeClient(known_reply()), page, TODAY, stop_file=tmp_path / "STOP", pause=lambda: None)
    assert "Tope mensual alcanzado" in result["halted"] and result["sent"] == [] and result["pdfs"]["uploaded"] == 0


# ── CLI ──────────────────────────────────────────────────────────────────────

def test_cli_with_the_review_on_stops_before_opening_the_browser(live_env, tmp_path, capsys):
    path = tmp_path / "profile.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    del data["apply"]["review_cv"]
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    add_offer(live_env.conn)
    assert cli.main(["apply", "--live"]) == 1
    err = capsys.readouterr().err
    assert "Revisar cada CV antes de subirlo" in err and live_env.launched == []
    [report] = (tmp_path / "data" / "runs").glob("*-apply.md")
    assert "## Freno" in report.read_text(encoding="utf-8")


def test_cli_apply_live_uploads_posts_restores_and_reports(live_env, tmp_path, capsys):
    path = tmp_path / "profile.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["variants"][0]["default_pdf_name"] = BASE_CV
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    add_offer(live_env.conn)
    assert cli.main(["apply", "--live"]) == 0
    assert live_env.site.default == BASE_CV and live_env.site.uploads == [CV_NAME]
    [report] = (tmp_path / "data" / "runs").glob("*-apply.md")
    text = report.read_text(encoding="utf-8")
    assert CV_NAME in text and f"restaurado «{BASE_CV}»" in text
    assert "Enviadas: 1" in capsys.readouterr().out


# ── fixtures ─────────────────────────────────────────────────────────────────

def test_the_fixtures_exist_and_have_the_expected_markers():
    assert "{{ROWS}}" in fixture("apply_uploadcv.html") and "ContainerGeneralOverWrite_ContainerGeneral_fuUploadCv" in fixture("apply_uploadcv.html")
    assert "Ya te postulaste a esta oferta" in fixture("apply_match_applied.html")
    post = fixture("apply_postapply.html")
    assert "Te postulaste correctamente" in post and "{{FILE}}" in post and "fuUploadCv" in post and "updateAttached" in post


# ── browser.read_applications ────────────────────────────────────────────────

def test_read_applications_reads_each_field_of_the_four_cards_by_its_selector_and_only_reads():
    page = FakePage(standard_site())
    found = browser.read_applications(page)
    assert found == [
        {"title": "Analista de pruebas", "company": "Empresa Falsa SA", "location": "Rosario, Santa Fe", "status": "Postulado",
         "im": "IM00000000000000000000000000000A1"},
        {"title": "Programador confidencial", "company": "", "location": "Córdoba, Córdoba", "status": "CV Visto",  # sin empresa
         "im": "IM00000000000000000000000000000B2"},
        {"title": "Desarrollador web", "company": "", "location": "Buenos Aires, Capital Federal", "status": "Postulado",  # confidencial
         "im": "IM00000000000000000000000000000C3"},
        {"title": "Soporte técnico", "company": "", "location": "", "status": "Proceso finalizado",  # oferta cerrada: ni empresa ni lugar
         "im": "IM00000000000000000000000000000D4"}]
    assert page.visited == [fakesite.APPLICATIONS_URL] and page.clicks() == [] and page.log == [] and page.handlers == []


def test_the_candidates_and_date_lines_are_neither_the_location_nor_the_status():
    found = browser.read_applications(FakePage(standard_site()))
    assert all("candidatos" not in a["location"] + a["status"] + a["company"] and "Hace" not in a["location"] + a["status"] for a in found)


@pytest.mark.parametrize("label", ["Empresa confidencial", "Confidencial", "Importante empresa del sector", "  IMPORTANTE  empresa   del Sector "])
def test_read_applications_treats_a_confidential_label_as_no_company(label):
    html = fixture("apply_applications.html").replace("Empresa Falsa SA", label)
    found = browser.read_applications(FakePage(standard_site(pages={fakesite.APPLICATIONS_URL: html})))
    assert found[0]["company"] == "" and found[0]["location"] == "Rosario, Santa Fe"


def test_read_applications_keeps_a_company_that_only_contains_a_confidential_word():
    html = fixture("apply_applications.html").replace("Empresa Falsa SA", "Confidencial Seguros SA")
    assert browser.read_applications(FakePage(standard_site(pages={fakesite.APPLICATIONS_URL: html})))[0]["company"] == "Confidencial Seguros SA"


def test_a_card_that_is_not_laid_out_like_the_site_halts_the_whole_read():
    base = fixture("apply_applications.html")
    for change in (lambda h: h.replace('<p class="fs14 fwB fc_link">CV Visto</p>', '<p class="fs14 fc_link">CV Visto</p>'),  # sin p.fwB
                   lambda h: h.replace('<h1 class="fs18 fwB">Desarrollador web</h1>', '<p>Desarrollador web</p>')):  # sin h1
        page = FakePage(standard_site(pages={fakesite.APPLICATIONS_URL: change(base)}))
        with pytest.raises(browser.Halt, match="no tiene título o estado.*el sitio cambió"):
            browser.read_applications(page)


def test_a_card_with_an_empty_heading_or_status_halts():
    base = fixture("apply_applications.html")
    for old, new in (('<h1 class="fs18 fwB">Soporte técnico</h1>', '<h1 class="fs18 fwB">  </h1>'),
                     ('<p class="fs14 fwB fc_aux">Proceso finalizado</p>', '<p class="fs14 fwB fc_aux"></p>')):
        with pytest.raises(browser.Halt):
            browser.read_applications(FakePage(standard_site(pages={fakesite.APPLICATIONS_URL: base.replace(old, new)})))


def test_the_old_line_based_helpers_are_gone():
    assert not hasattr(browser, "DATE_LINE") and not hasattr(browser, "STATUS_WORDS")


def second_page():
    return re.sub(r"IM0+", "IM2", fixture("apply_applications.html"))


def with_next(html, href):
    return html.replace("</body>", f'<a rel="next" href="{href}">Siguiente</a></body>')


def test_read_applications_follows_the_next_page_up_to_the_page_limit_without_repeats():
    p2, p3 = "https://candidato.ar.computrabajo.com/candidate/match/?p=2", "https://candidato.ar.computrabajo.com/candidate/match/?p=3"
    pages = {fakesite.APPLICATIONS_URL: with_next(fixture("apply_applications.html"), "/candidate/match/?p=2"),
             p2: with_next(second_page(), "?p=3"), p3: second_page().replace("IM2", "IM3")}
    page = FakePage(standard_site(pages=pages))
    assert [a["im"][:3] for a in browser.read_applications(page, pages=3)] == ["IM0"] * 4 + ["IM2"] * 4 + ["IM3"] * 4
    assert page.visited == [fakesite.APPLICATIONS_URL, p2, p3]
    limited = FakePage(standard_site(pages=pages))
    assert len(browser.read_applications(limited, pages=2)) == 8 and len(limited.visited) == 2
    repeated = FakePage(standard_site(pages={fakesite.APPLICATIONS_URL: with_next(fixture("apply_applications.html"), "/candidate/match/")}))
    assert len(browser.read_applications(repeated, pages=3)) == 4  # la misma página otra vez no suma nada


def test_read_applications_does_not_follow_a_next_link_to_another_site_or_page():
    for href in ("https://otro.sitio/candidate/match/?p=2", "/candidate/cv/uploadcv", "#"):
        page = FakePage(standard_site(pages={fakesite.APPLICATIONS_URL: with_next(fixture("apply_applications.html"), href)}))
        assert len(browser.read_applications(page, pages=3)) == 4 and page.visited == [fakesite.APPLICATIONS_URL]


def redirect_applications(site, target):
    original = site.load
    site.load = lambda page, url, keep=False: original(page, target if url == fakesite.APPLICATIONS_URL else url)


def test_read_applications_halts_without_a_session_or_on_another_screen():
    site = standard_site()
    redirect_applications(site, fakesite.LOGIN_URL)
    with pytest.raises(browser.Halt, match="Sin sesión"):
        browser.read_applications(FakePage(site))
    other = standard_site()
    redirect_applications(other, fakesite.OTHER_URL)
    with pytest.raises(browser.Halt, match="terminó en otra pantalla"):
        browser.read_applications(FakePage(other))


def test_read_applications_with_no_cards_gives_an_empty_list():
    assert browser.read_applications(FakePage(standard_site(pages={fakesite.APPLICATIONS_URL: "<html><body><h1>Mis postulaciones</h1></body></html>"}))) == []


def test_application_key_and_index():
    assert apply.application_key("  Analista ", "EMPRESA  Falsa", "Rosario") == ("analista", "empresa falsa", "rosario")
    assert apply.application_key("Analista", "", "Rosario") == ("analista", "rosario") and apply.application_key("Analista", None, "Rosario") == ("analista", "rosario")
    index = apply.application_index([{"title": "Á", "company": "B", "location": "C", "im": "I1"}, {"title": "D", "company": "", "location": "E", "im": "I2"}])
    assert index == {("a", "b", "c"): "I1", ("d", "e"): "I2"}
