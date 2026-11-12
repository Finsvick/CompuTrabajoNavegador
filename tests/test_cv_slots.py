"""Tests de TAREA-050: CVs con el nombre de la postulación, copia en cv/postulaciones, límite de 10 CVs en la cuenta, sueldo abierto y
motivos de las preguntas pendientes. Todo falso: sitio, página y cliente."""
import shutil
from datetime import datetime, timedelta, timezone

import pytest

import fakesite
from fakesite import APPLICATIONS_URL, BASE_CV, KQ_URL, FakePage, fixture, standard_site
from test_apply_live import (CV_NAME, MODEL, SALARY, TODAY, FakeClient, add_offer, conn, known_reply, make_profile, reply, rows,  # noqa: F401
                             status_of, stub_cv)
from compuapply import apply, browser, cv, db, profile

PERSON = {"name": "Giovanni", "surname": "Bianchi"}
NEW_CV = "CV-Giovanni-Bianchi-Oferta-5.pdf"  # el CV que sube la oferta nueva (lo pone el proveedor falso)
LIMIT = "La cuenta llegó a 10 CVs (límite de Computrabajo) y ninguno se puede borrar sin riesgo: borrá uno a mano en la página de CV"


# ── el nombre ────────────────────────────────────────────────────────────────

def test_the_name_has_the_position_and_the_company_of_the_offer():
    name = cv.application_pdf_name(PERSON, "Ejecutivo/a de ventas. Zona La Paternal", "Grupo Gestión")
    assert name == "CV-Giovanni-Bianchi-Ejecutivo-a-de-ventas-Zona-La-Paternal-Grupo-Gestion.pdf"


@pytest.mark.parametrize("company", [None, "", "  "])
def test_an_offer_without_company_is_confidencial(company):
    assert cv.application_pdf_name(PERSON, "Vendedor", company) == "CV-Giovanni-Bianchi-Vendedor-Confidencial.pdf"


def test_a_long_name_is_cut_to_80_characters_trimming_the_position_first():
    name = cv.application_pdf_name(PERSON, "Representante de atención al cliente y soporte técnico para empresas de telecomunicaciones", "Grupo Gestión")
    assert len(name) - len(".pdf") <= 80 and name.endswith("-Grupo-Gestion.pdf") and name.startswith("CV-Giovanni-Bianchi-Representante")
    assert "--" not in name


def test_a_very_long_company_is_cut_too_and_never_goes_over_80():
    name = cv.application_pdf_name(PERSON, "Vendedor", "Compañía " * 30)
    assert len(name) - len(".pdf") <= 80 and not name[:-4].endswith("-")


def test_the_suffix_is_the_last_4_characters_of_the_id16_and_still_fits():
    name = cv.application_pdf_name(PERSON, "Vendedor " * 20, "Grupo")
    suffixed = cv.add_suffix(name, "00000000000AB12F")
    assert suffixed.endswith("-B12F.pdf") and len(suffixed) - len(".pdf") <= 80
    assert cv.add_suffix("CV-Ana-Vendedor.pdf", "0000000000000001") == "CV-Ana-Vendedor-0001.pdf"


def saved_cv(conn, tmp_path, id16):
    html = tmp_path / "cvs" / f"{id16}-dev.html"
    html.parent.mkdir(exist_ok=True)
    html.write_text('<html><body><div class="headline">Titular</div></body></html>', encoding="utf-8")
    html.with_suffix(".pdf").write_bytes(b"%PDF-" + b"0" * 100)
    db.save_cv(conn, id16, "dev", MODEL, cv.CV_PROMPT_VERSION, str(html), str(html.with_suffix(".pdf")), 0.0, 1, 1)


def provide(conn, item):
    info = apply.make_cv_provider(conn, make_profile(), None, MODEL)(item)
    shutil.rmtree(info["dir"])
    return info["local"]  # el nombre de la copia local (puesto y empresa de la oferta)


def test_the_provider_names_the_cv_with_the_title_and_the_company_of_the_offer(conn, tmp_path):
    id16 = add_offer(conn, 1, title="Ejecutivo/a de ventas. Zona La Paternal", company="Grupo Gestión")
    saved_cv(conn, tmp_path, id16)
    assert provide(conn, {"id16": id16, "variant": "dev"}) == "CV-Giovanni-Bianchi-Ejecutivo-a-de-ventas-Zona-La-Paternal-Grupo-Gestion.pdf"


def test_an_offer_without_company_gets_confidencial_in_the_name(conn, tmp_path):
    id16 = add_offer(conn, 1, title="Vendedor", company="")
    saved_cv(conn, tmp_path, id16)
    assert provide(conn, {"id16": id16, "variant": "dev"}).endswith("-Vendedor-Confidencial.pdf")


def test_a_name_already_used_for_another_offer_gets_the_last_4_of_the_id16(conn, tmp_path):
    first, second = add_offer(conn, 1, title="Vendedor", company="Acme"), add_offer(conn, 0xAB12, title="Vendedor", company="Acme")
    for id16 in (first, second):
        saved_cv(conn, tmp_path, id16)
    name = provide(conn, {"id16": first, "variant": "dev"})
    db.save_application(conn, first, "dev", 80, "applied", "ok", [], name, "IM1", "c8")
    assert provide(conn, {"id16": second, "variant": "dev"}) == name[:-4] + "-AB12.pdf"


def test_an_offer_that_already_has_a_cv_file_keeps_its_name(conn, tmp_path):
    id16 = add_offer(conn, 1, title="Vendedor", company="Acme")
    saved_cv(conn, tmp_path, id16)
    db.save_application(conn, id16, "dev", 80, "cv_uploaded", "x", [], "CV-Nombre-Viejo.pdf", None, "c8")
    assert provide(conn, {"id16": id16, "variant": "dev"}) == "CV-Nombre-Viejo.pdf"


def test_a_cv_with_a_name_the_account_already_has_for_another_offer_gets_the_suffix(conn, tmp_path):
    other = add_offer(conn, 2)
    db.save_application(conn, other, "dev", 80, "applied", "ok", [], CV_NAME, "IM2", "c8")
    id16 = add_offer(conn, 1)
    site = standard_site(cvs=(BASE_CV, CV_NAME), default=BASE_CV)
    result = go(conn, tmp_path, site)
    assert result["sent"][0]["file"] == CV_NAME[:-4] + "-2.pdf" and site.uploads == [CV_NAME[:-4] + "-2.pdf"] and id16


# ── la copia en cv/postulaciones ─────────────────────────────────────────────

def go(conn, tmp_path, site, *, prof=None, client=None, provider=None, archive=None):
    page = FakePage(site)
    result = apply.run_live(conn, prof or make_profile({"salary": SALARY}), MODEL, client or FakeClient(known_reply()), page, TODAY,
                            stop_file=tmp_path / "STOP", pause=lambda: None, cv_provider=provider or stub_cv(), archive_dir=archive)
    result["page"] = page
    return result


def test_every_uploaded_pdf_is_copied_to_cv_postulaciones_with_the_same_name(conn, tmp_path):
    add_offer(conn)
    archive = tmp_path / "cv" / "postulaciones"
    result = go(conn, tmp_path, standard_site(), archive=archive)
    assert [i["file"] for i in result["sent"]] == [CV_NAME]
    assert [p.name for p in archive.iterdir()] == [CV_NAME] and (archive / CV_NAME).read_bytes().startswith(b"%PDF-")
    assert rows(conn, "SELECT cv_file FROM applications") == [(CV_NAME,)]


def test_the_archive_copy_stays_even_if_the_cv_is_deleted_from_the_account(conn, tmp_path):
    archive = tmp_path / "cv" / "postulaciones"
    archive.mkdir(parents=True)
    (archive / "CV-Viejo.pdf").write_bytes(b"%PDF-1")
    add_offer(conn)
    go(conn, tmp_path, standard_site(), archive=archive)
    assert (archive / "CV-Viejo.pdf").is_file() and (archive / CV_NAME).is_file()


def test_without_an_archive_dir_nothing_is_copied(conn, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    add_offer(conn)
    go(conn, tmp_path, standard_site())
    assert not (tmp_path / "cv").exists()


# ── la cuenta con 10 CVs ─────────────────────────────────────────────────────

def cards(*apps):
    """«Mis postulaciones» falsa: (im, título, empresa, estado) por tarjeta."""
    body = "".join(f'<div data-match="{im}" data-targeturl="/x"><h1>{title}</h1><p class="fc_base mt5">{company}</p>'
                   f'<p class="fc_base">Flores, Capital Federal</p><p class="fwB">{status}</p></div>' for im, title, company, status in apps)
    return f"<html><body>{body}</body></html>"


def nine_and_default(*extra):
    """10 PDFs: el CV base por defecto, los `extra` y relleno que no figura en `applications`."""
    names = [BASE_CV, *extra]
    return tuple(names + [f"Relleno-{n}.pdf" for n in range(10 - len(names))])


def days_ago(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def applied(conn, n, name, im, when=None):
    when = when or days_ago(5)
    id16 = f"{n:016X}"
    db.save_application(conn, id16, "dev", 80, "applied", "ok", [], name, im, "c8")
    conn.execute("UPDATE applications SET applied_at = ? WHERE id16 = ?", (when, id16))
    conn.commit()
    return id16


def full_site(*extra, apps=(), **kw):
    return standard_site(cvs=nine_and_default(*extra), default=BASE_CV, max_docs=10, pages={APPLICATIONS_URL: cards(*apps)}, **kw)


def offer_to_send(conn):
    return add_offer(conn, 5, title="Oferta 5")


def stub_new():
    return stub_cv(name=NEW_CV)


def test_with_10_cvs_and_3_copies_of_a_pending_offer_one_copy_is_deleted_and_the_cv_is_uploaded(conn, tmp_path):
    pending = add_offer(conn, 2)
    db.mark_status(conn, pending, "pending_question")
    db.save_application(conn, pending, "dev", 80, "pending_question", "x", [], "CV-Pendiente.pdf", None, "c8")
    site = full_site("CV-Pendiente.pdf", "CV-Pendiente.pdf", "CV-Pendiente.pdf")
    offer_to_send(conn)
    result = go(conn, tmp_path, site, provider=stub_new())
    assert result["halted"] is None and [i["file"] for i in result["sent"]] == [NEW_CV]
    assert site.deleted == ["CV-Pendiente.pdf"] and site.cvs.count("CV-Pendiente.pdf") == 2 and site.uploads == [NEW_CV]
    assert result["deleted"] == [{"name": "CV-Pendiente.pdf", "local": "CV-Pendiente.pdf", "reason": "copia repetida del CV de una oferta que no se envió"}]
    assert BASE_CV in site.cvs  # el por defecto no se tocó


def report(result, tmp_path):
    meta = {"now": datetime(2026, 11, 6, 10, 0, 0), "threshold": 70, "daily_cap": 10, "applied_today": 0, "model": MODEL}
    return apply.write_live_report(result, meta, tmp_path / "runs").read_text(encoding="utf-8")


def test_the_report_says_how_many_cvs_were_deleted_with_name_and_reason(conn, tmp_path):
    pending = add_offer(conn, 2)
    db.mark_status(conn, pending, "pending_question")
    db.save_application(conn, pending, "dev", 80, "pending_question", "x", [], "CV-Pendiente.pdf", None, "c8")
    site = full_site("CV-Pendiente.pdf", "CV-Pendiente.pdf")
    offer_to_send(conn)
    text = report(go(conn, tmp_path, site, provider=stub_new()), tmp_path)
    assert "- CVs borrados en esta corrida: 1" in text and "CV borrado de la cuenta: CV-Pendiente.pdf, copia local CV-Pendiente.pdf: copia repetida del CV de una oferta que no se envió" in text
    assert "cv/postulaciones/" in text


def test_the_report_says_zero_when_nothing_was_deleted(conn, tmp_path):
    offer_to_send(conn)
    text = report(go(conn, tmp_path, standard_site(), provider=stub_new()), tmp_path)
    assert "- CVs borrados en esta corrida: 0" in text


def finished_and_posted(conn):
    applied(conn, 11, "CV-Terminada.pdf", "IM11")
    applied(conn, 12, "CV-Postulada.pdf", "IM12")
    return (("IM11", "Terminada", "Acme", "Proceso finalizado"), ("IM12", "Postulada", "Beta", "Postulado"))


def test_without_copies_the_cv_of_a_finished_process_is_deleted_and_not_the_one_in_postulado(conn, tmp_path):
    site = full_site("CV-Terminada.pdf", "CV-Postulada.pdf", apps=finished_and_posted(conn))
    offer_to_send(conn)
    result = go(conn, tmp_path, site, provider=stub_new())
    assert result["halted"] is None and site.deleted == ["CV-Terminada.pdf"] and "CV-Postulada.pdf" in site.cvs and site.uploads == [NEW_CV]
    assert result["deleted"][0]["reason"].startswith("rotación: el CV de la postulación más vieja (")


def test_the_rotation_takes_the_oldest_sent_postulation_whatever_its_state(conn, tmp_path):
    applied(conn, 11, "CV-Visto-Nuevo.pdf", "IM11", "2026-10-20T10:00:00+00:00")
    applied(conn, 12, "CV-Visto-Viejo.pdf", "IM12", "2026-10-02T10:00:00+00:00")
    applied(conn, 13, "CV-Postulada.pdf", "IM13", "2026-09-01T10:00:00+00:00")
    apps = (("IM11", "A", "Acme", "CV Visto"), ("IM12", "B", "Acme", "CV Visto"), ("IM13", "C", "Acme", "Postulado"))
    site = full_site("CV-Visto-Nuevo.pdf", "CV-Visto-Viejo.pdf", "CV-Postulada.pdf", apps=apps)
    offer_to_send(conn)
    result = go(conn, tmp_path, site, provider=stub_new())
    assert site.deleted == ["CV-Postulada.pdf"] and "2026-09-01" in result["deleted"][0]["reason"]


def test_the_older_postulation_goes_before_a_finished_one(conn, tmp_path):
    applied(conn, 11, "CV-Visto.pdf", "IM11", "2026-08-01T10:00:00+00:00")
    applied(conn, 12, "CV-Terminada.pdf", "IM12", "2026-10-30T10:00:00+00:00")
    apps = (("IM11", "A", "Acme", "CV Visto"), ("IM12", "B", "Acme", "Proceso finalizado"))
    site = full_site("CV-Visto.pdf", "CV-Terminada.pdf", apps=apps)
    offer_to_send(conn)
    go(conn, tmp_path, site, provider=stub_new())
    assert site.deleted == ["CV-Visto.pdf"]


def test_a_copy_of_an_unsent_offer_goes_before_a_finished_process(conn, tmp_path):
    pending = add_offer(conn, 2)
    db.mark_status(conn, pending, "pending_question")
    db.save_application(conn, pending, "dev", 80, "cv_uploaded", "x", [], "CV-Pendiente.pdf", None, "c8")
    applied(conn, 11, "CV-Terminada.pdf", "IM11")
    site = full_site("CV-Terminada.pdf", "CV-Pendiente.pdf", "CV-Pendiente.pdf", apps=(("IM11", "A", "Acme", "Proceso finalizado"),))
    offer_to_send(conn)
    go(conn, tmp_path, site, provider=stub_new())
    assert site.deleted == ["CV-Pendiente.pdf"] and "CV-Terminada.pdf" in site.cvs


def assert_stopped_without_touching_the_account(result, site, message=LIMIT):
    assert result["halted"] and message in result["halted"]
    assert site.deleted == [] and site.uploads == [] and result["deleted"] == [] and result["sent"] == []


def test_with_everything_in_postulado_the_oldest_rotates(conn, tmp_path):
    applied(conn, 11, "CV-Postulada-1.pdf", "IM11", days_ago(9))
    applied(conn, 12, "CV-Postulada-2.pdf", "IM12", days_ago(2))
    apps = (("IM11", "A", "Acme", "Postulado"), ("IM12", "B", "Beta", "Postulado"))
    site = full_site("CV-Postulada-1.pdf", "CV-Postulada-2.pdf", apps=apps)
    offer_to_send(conn)
    result = go(conn, tmp_path, site, provider=stub_new())
    assert result["halted"] is None and site.deleted == ["CV-Postulada-1.pdf"] and site.uploads == [NEW_CV]


def test_with_free_cv_slots_false_it_halts_instead_of_deleting(conn, tmp_path):
    site = full_site("CV-Terminada.pdf", "CV-Postulada.pdf", apps=finished_and_posted(conn))
    offer_to_send(conn)
    prof = make_profile({"salary": SALARY})
    prof["apply"]["free_cv_slots"] = False
    result = go(conn, tmp_path, site, prof=prof, provider=stub_new())
    assert_stopped_without_touching_the_account(result, site)
    assert "apply.free_cv_slots" in result["halted"]


def test_a_cv_that_is_not_in_applications_is_never_deleted(conn, tmp_path):
    site = full_site()  # solo relleno que el programa no subió
    offer_to_send(conn)
    assert_stopped_without_touching_the_account(go(conn, tmp_path, site, provider=stub_new()), site)


def test_the_default_cv_is_never_deleted(conn, tmp_path):
    applied(conn, 11, "CV-Terminada.pdf", "IM11")
    site = full_site("CV-Terminada.pdf", apps=(("IM11", "A", "Acme", "Proceso finalizado"),))
    site.default = "CV-Terminada.pdf"  # la única terminada es la que está por defecto
    offer_to_send(conn)
    assert_stopped_without_touching_the_account(go(conn, tmp_path, site, provider=stub_new()), site)


def test_the_pdf_of_a_variant_and_the_default_pdf_name_are_never_deleted(conn, tmp_path):
    applied(conn, 11, "CV-Base-Ventas.pdf", "IM11")
    applied(conn, 12, "CV-Base-Dev.pdf", "IM12")
    apps = (("IM11", "A", "Acme", "Proceso finalizado"), ("IM12", "B", "Acme", "Proceso finalizado"))
    site = full_site("CV-Base-Ventas.pdf", "CV-Base-Dev.pdf", apps=apps)
    prof = make_profile({"salary": SALARY})
    prof["variants"][0]["pdf"] = "cv/CV-Base-Ventas.pdf"  # el PDF de la variante
    prof["variants"][0]["default_pdf_name"] = "CV-Base-Dev.pdf"  # y el CV base que se restaura
    offer_to_send(conn)
    result = go(conn, tmp_path, site, prof=prof, provider=stub_new())
    assert site.deleted == [] and site.uploads == [] and [i["file"] for i in result["sent"]] == ["CV-Base-Dev.pdf"]  # TAREA-061: sale con el base (primero el default_pdf_name)


def test_an_unknown_modal_halts_with_the_fingerprint_and_nothing_is_deleted_or_uploaded(conn, tmp_path):
    site = full_site("CV-Terminada.pdf", "CV-Postulada.pdf", apps=finished_and_posted(conn), delete_mode="modal")
    offer_to_send(conn)
    result = go(conn, tmp_path, site, provider=stub_new())
    assert result["halted"] and "no apareció el cuadro de confirmación" in result["halted"] and "button" in result["halted"]
    assert "Texto del cuadro" not in result["halted"] and site.deleted == [] and site.uploads == []


def test_a_native_confirm_is_accepted_and_the_dialog_listener_is_removed(conn, tmp_path):
    site = full_site("CV-Terminada.pdf", "CV-Postulada.pdf", apps=finished_and_posted(conn), delete_mode="confirm")
    offer_to_send(conn)
    result = go(conn, tmp_path, site, provider=stub_new())
    assert site.deleted == ["CV-Terminada.pdf"] and result["page"].dialog_handlers == []


def test_a_delete_button_that_does_not_delete_halts_because_the_id_is_still_there(conn, tmp_path):
    site = full_site("CV-Terminada.pdf", "CV-Postulada.pdf", apps=finished_and_posted(conn), delete_mode="blockui_stays")
    offer_to_send(conn)
    result = go(conn, tmp_path, site, provider=stub_new())
    assert result["halted"] and "sigue en la cuenta después de borrarlo" in result["halted"] and site.uploads == []


def test_delete_cv_reloads_and_requires_the_id_gone_and_two_copies_have_different_ids():
    site = standard_site(cvs=("A.pdf", "A.pdf", BASE_CV), default=BASE_CV)
    page = FakePage(site)
    ids = [c["id"] for c in browser.account_cvs(page)]
    assert len(set(ids)) == 3
    browser.delete_cv(page, ids[1], "A.pdf")
    assert [c["id"] for c in browser.account_cvs(page)] == [ids[0], ids[2]]
    with pytest.raises(browser.Halt, match="no está en la cuenta"):
        browser.delete_cv(page, ids[1], "A.pdf")


def test_deleting_clicks_the_trash_of_the_row_without_force_or_script(conn, tmp_path):
    site = full_site("CV-Terminada.pdf", "CV-Postulada.pdf", apps=finished_and_posted(conn))
    offer_to_send(conn)
    result = go(conn, tmp_path, site, provider=stub_new())
    assert ("click", "span", "") in result["page"].clicks()


def test_the_site_notice_about_the_maximum_of_documents_halts_with_that_text(conn, tmp_path):
    offer_to_send(conn)
    site = standard_site(cvs=nine_and_default(), default=BASE_CV, max_docs=10, pages={APPLICATIONS_URL: cards()})
    prof = make_profile({"salary": SALARY})
    prof["run"]["max_account_pdfs"] = 20  # el programa cree que hay lugar, pero el sitio dice que no
    result = go(conn, tmp_path, site, prof=prof, provider=stub_new())
    assert result["halted"] and "número máximo de documentos permitido" in result["halted"] and site.uploads == []
    assert rows(conn, "SELECT status, cv_file FROM applications") == []  # no se subió: no quedó nada guardado


def test_with_less_than_10_cvs_nothing_is_read_again_nor_deleted(conn, tmp_path):
    offer_to_send(conn)
    site = standard_site(cvs=(BASE_CV, "Otro.pdf"), default=BASE_CV)
    result = go(conn, tmp_path, site, provider=stub_new())
    assert result["halted"] is None and site.deleted == [] and result["deleted"] == []


def test_the_default_account_limit_is_10_and_free_cv_slots_is_true():
    assert profile.DEFAULT_MAX_ACCOUNT_PDFS == 10 and profile.DEFAULT_FREE_CV_SLOTS is True


def test_free_cv_slots_loads_from_the_profile_and_must_be_a_boolean(tmp_path):
    from test_apply_live import write_profile_with
    path = write_profile_with(tmp_path)
    assert "free_cv_slots" not in profile.load_profile(path)["apply"]
    import yaml
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["apply"] = {**(data.get("apply") or {}), "free_cv_slots": False}
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    assert profile.load_profile(path)["apply"]["free_cv_slots"] is False
    data["apply"]["free_cv_slots"] = "no"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    with pytest.raises(RuntimeError, match="free_cv_slots"):
        profile.load_profile(path)


# ── el sueldo abierto ────────────────────────────────────────────────────────

PAY_OPEN = {"text": "Indicar remuneración pretendida", "kind": "open", "options": []}


def test_an_open_pay_question_is_answered_with_salary_even_if_the_model_says_unknown():
    client = FakeClient(reply({"source": "unknown"}, {"source": "unknown"}))
    other = {"text": "¿Tenés estudios en Computer Engineering?", "kind": "closed", "options": ["Sí", "No"]}
    out = apply.answer_questions([PAY_OPEN, other], make_profile({"salary": SALARY}), client, MODEL, "dev", [])
    assert out[0]["source"] == "answers" and out[0]["text"] == SALARY and out[0]["reason"] is None
    assert out[1]["source"] == "unknown" and out[1]["reason"] == "el modelo dijo unknown"


def test_if_every_question_is_pay_the_model_is_not_even_called():
    client = FakeClient(reply())
    calls = []
    out = apply.answer_questions([PAY_OPEN, {**PAY_OPEN, "text": "Pretensión salarial"}], make_profile({"salary": SALARY}), client, MODEL, "dev", calls)
    assert [a["text"] for a in out] == [SALARY, SALARY] and client.calls == [] and calls == []


def test_the_pay_answer_does_not_depend_on_the_model_missing_the_question():
    client = FakeClient(reply({"option": 0, "source": "cv"}))  # solo trae la 0
    other = {"text": "¿Tenés estudios en Computer Engineering?", "kind": "closed", "options": ["Sí", "No"]}
    out = apply.answer_questions([other, PAY_OPEN], make_profile({"salary": SALARY}), client, MODEL, "dev", [])
    assert out[1]["text"] == SALARY and out[0]["source"] == "cv"


def test_without_salary_an_open_pay_question_still_goes_to_the_model():
    client = FakeClient(reply({"source": "unknown"}))
    out = apply.answer_questions([PAY_OPEN], make_profile(), client, MODEL, "dev", [])
    assert out[0]["source"] == "unknown" and len(client.calls) == 1


def test_a_closed_pay_question_is_not_answered_by_the_rule():
    closed = {"text": "¿Cuál es tu remuneración pretendida?", "kind": "closed", "options": ["Hasta $500.000", "Más de $800.000"]}
    out = apply.answer_questions([closed], make_profile({"salary": SALARY}), FakeClient(reply({"source": "unknown"})), MODEL, "dev", [])
    assert out[0]["source"] == "unknown"


# ── el motivo de cada pregunta pendiente ─────────────────────────────────────

def test_the_reasons_for_an_unknown_answer(conn):
    sales = {"text": "¿Cuántas personas tenías a cargo?", "kind": "closed", "options": ["Más de 5 personas", "Ninguna"]}
    years = {"text": "¿Cuántos años de experiencia tenés?", "kind": "closed", "options": ["No tengo experiencia", "Menos de 1 año"]}
    bad = {"text": "¿Tenés secundario?", "kind": "closed", "options": ["Sí", "No"]}
    free = {"text": "Contanos", "kind": "open", "options": []}
    empty = {"text": "Contanos más", "kind": "open", "options": []}
    client = FakeClient(reply({"option": 0, "source": "cv"}, {"option": 0, "source": "cv"}, {"option": 9, "source": "cv"},
                              {"text": "Trabajo con Node.js hace tiempo", "source": "cv"}, {"text": "  ", "source": "cv"}))
    out = apply.answer_questions([sales, years, bad, free, empty], make_profile(), client, MODEL, "dev", [])
    assert [a["reason"] for a in out][:3] == ["el control: opción con cifra sin respuesta declarada",
                                              "el control: pregunta de duración de experiencia sin respuesta declarada",
                                              "el control: la opción elegida no existe"]
    assert out[3]["reason"].startswith("el control: ") and "Node.js" not in out[3]["reason"]
    assert out[4]["reason"] == "el modelo no escribió respuesta"


def test_the_report_lists_each_pending_question_with_its_reason_and_no_answer_values(conn, tmp_path):
    offer_to_send(conn)
    page_site = standard_site(pages={KQ_URL: fixture("apply_kq_sales.html")})
    result = go(conn, tmp_path, page_site, client=FakeClient(reply({"option": 0, "source": "cv"})), provider=stub_new())
    [item] = result["pending"]
    assert item["reason"] == "el control: pregunta de duración de experiencia sin respuesta declarada" and item["question"].startswith("¿Tienes experiencia previa")
    text = report(result, tmp_path)
    assert "Preguntas pendientes (1), con el motivo:" in text and "el control: pregunta de duración de experiencia sin respuesta declarada" in text
    assert "más de 2 años" not in text.split("## Respuestas")[0]  # sin valores: solo la pregunta y el motivo (las opciones son de la oferta y van en «Respuestas»)


def test_a_model_unknown_is_reported_as_such(conn, tmp_path):
    offer_to_send(conn)
    page_site = standard_site(pages={KQ_URL: fixture("apply_kq_sales.html")})
    result = go(conn, tmp_path, page_site, client=FakeClient(reply({"source": "unknown"})), provider=stub_new())
    assert [p["reason"] for p in result["pending"]] == ["el modelo dijo unknown"]
    assert "— el modelo dijo unknown" in report(result, tmp_path)


def test_saving_the_form_keeps_free_cv_slots(tmp_path):
    import yaml
    from compuapply import ui_logic
    from test_apply_live import write_profile_with
    path = write_profile_with(tmp_path)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["apply"] = {**(data.get("apply") or {}), "free_cv_slots": False}
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    from_form = yaml.safe_load(path.read_text(encoding="utf-8"))
    del from_form["apply"]["free_cv_slots"]  # el formulario no tiene ese campo
    ui_logic.save_profile(from_form, path)
    assert profile.load_profile(path)["apply"]["free_cv_slots"] is False


# ── TAREA-052: el cuadro blockUI de borrado ──────────────────────────────────

def three_copies():
    site = standard_site(cvs=("Copia.pdf", "Copia.pdf", "Copia.pdf", BASE_CV), default=BASE_CV)
    page = FakePage(site)
    return site, page, [c["id"] for c in browser.account_cvs(page)]


def test_the_trash_opens_the_box_and_the_visible_delete_button_removes_the_row():
    site, page, ids = three_copies()
    browser.delete_cv(page, ids[0], "Copia.pdf")
    assert [c["id"] for c in browser.account_cvs(page)] == [ids[1], ids[2], ids[3]] and site.deleted == ["Copia.pdf"] and site.cancelled == 0
    clicks = [entry for entry in page.clicks()]
    assert [c[1] for c in clicks] == ["span", "input"] and not any("force" in str(c) for c in clicks)


def test_with_3_rows_of_the_same_name_only_the_chosen_filecandidateid_is_deleted_and_the_others_stay():
    site, page, ids = three_copies()
    browser.delete_cv(page, ids[1], "Copia.pdf")
    left = browser.account_cvs(page)
    assert [c["id"] for c in left] == [ids[0], ids[2], ids[3]] and [c["name"] for c in left].count("Copia.pdf") == 2


def test_the_hidden_original_of_the_box_is_not_the_one_that_gets_the_click():
    site, page, ids = three_copies()
    assert any(not e.is_visible() for e in page.query_selector_all("input#btDelete"))  # el original oculto está en la página
    browser.delete_cv(page, ids[0], "Copia.pdf")  # y aun así el clic fue al clon visible (si no, el fake habría fallado)
    assert site.deleted == ["Copia.pdf"]


def test_a_box_with_another_text_is_cancelled_and_halts_with_the_fingerprint_without_deleting():
    site = standard_site(cvs=("Copia.pdf", BASE_CV), default=BASE_CV, delete_mode="blockui_other")
    page = FakePage(site)
    ids = [c["id"] for c in browser.account_cvs(page)]
    with pytest.raises(browser.Halt, match="apareció un cuadro distinto del de confirmación") as error:
        browser.delete_cv(page, ids[0], "Copia.pdf")
    assert "input[button]" in str(error.value) and "sesión" not in str(error.value)
    assert site.deleted == [] and site.cancelled == 1
    assert [c["name"] for c in browser.account_cvs(page)] == ["Copia.pdf", BASE_CV]


def test_without_a_box_it_halts_with_the_fingerprint_and_clicks_nothing_else():
    site = standard_site(cvs=("Copia.pdf", BASE_CV), default=BASE_CV, delete_mode="stays")
    page = FakePage(site)
    ids = [c["id"] for c in browser.account_cvs(page)]
    with pytest.raises(browser.Halt, match="no apareció el cuadro de confirmación"):
        browser.delete_cv(page, ids[0], "Copia.pdf")
    assert site.deleted == [] and [c[1] for c in page.clicks()] == ["span"]  # solo la papelera


def test_if_the_delete_button_belongs_to_another_file_it_is_cancelled():
    site, page, ids = three_copies()
    original = site.open_box
    site.open_box = lambda p, file_id, text="¿Estás seguro que deseás eliminar este currículum?": original(p, ids[2], text)  # un cuadro de otra fila
    with pytest.raises(browser.Halt):
        browser.delete_cv(page, ids[0], "Copia.pdf")
    assert site.deleted == [] and site.cancelled == 1


def test_the_fixture_has_the_box_of_the_site_hidden_in_the_page():
    html = fixture("apply_uploadcv.html")
    assert 'id="frmDelete"' in html and "eliminar este currículum" in html and 'id="btDelete"' in html and 'id="btCancelDelete"' in html


def test_the_fixture_box_has_the_real_nesting_and_the_text_is_two_levels_above_the_button():
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(fixture("apply_uploadcv.html"), "html.parser")
    button = soup.select_one("#frmDelete > .md-modal > .md-content > .modal-p > .submitbtn > input#btDelete")
    assert button is not None and "currículum" not in button.parent.get_text()  # el padre directo no tiene texto
    assert "currículum" in button.parent.parent.get_text()


def test_the_whole_run_deletes_with_the_box_and_goes_on_with_the_upload(conn, tmp_path):
    site = full_site("CV-Terminada.pdf", "CV-Postulada.pdf", apps=finished_and_posted(conn))
    offer_to_send(conn)
    result = go(conn, tmp_path, site, provider=stub_new())
    assert result["halted"] is None and site.deleted == ["CV-Terminada.pdf"] and site.uploads == [NEW_CV] and [i["file"] for i in result["sent"]] == [NEW_CV]


# ── TAREA-059: lugares por subida, máximo, verificación final ─────────────────

def own_name_provider():
    """Un CV con nombre propio por oferta, como el real."""
    return lambda item: stub_cv(name=f"CV-Oferta-{item['id16'][-2:]}.pdf")(item)


def three_pending(conn):
    names = []
    for n in (2, 3, 4):
        id16 = add_offer(conn, n)
        db.mark_status(conn, id16, "pending_question")
        db.save_application(conn, id16, "dev", 80, "cv_uploaded", "x", [], f"CV-Pend-{n}.pdf", None, "c8")
        names.append(f"CV-Pend-{n}.pdf")
    return names


def test_with_10_pdfs_and_3_new_offers_one_run_deletes_3_uploads_3_and_sends_3(conn, tmp_path):
    names = three_pending(conn)
    for n in (5, 6, 7):
        add_offer(conn, n, title=f"Oferta {n}")
    site = full_site(*names)
    result = go(conn, tmp_path, site, provider=own_name_provider())
    assert result["halted"] is None and len(result["sent"]) == 3 and len(result["deleted"]) == 3
    assert sorted(site.deleted) == sorted(names) and len(site.uploads) == 3 and len(site.cvs) == 10


def test_the_account_is_read_before_every_upload_that_is_not_a_reuse(conn, tmp_path):
    for n in (5, 6):
        add_offer(conn, n, title=f"Oferta {n}")
    page_reads = []
    original = browser.account_cvs
    site = standard_site(cvs=(BASE_CV,), default=BASE_CV, pages={APPLICATIONS_URL: cards()})
    import compuapply.apply as apply_module
    apply_module.browser.account_cvs = lambda page: page_reads.append(1) or original(page)
    try:
        go(conn, tmp_path, site, provider=own_name_provider())
    finally:
        apply_module.browser.account_cvs = original
    assert len(page_reads) == 1 + 2  # una al empezar y una por cada subida


def test_the_notice_of_the_maximum_halts_with_the_text_of_the_site(conn, tmp_path):
    offer_to_send(conn)
    site = standard_site(cvs=nine_and_default(), default=BASE_CV, max_docs=10, pages={APPLICATIONS_URL: cards()})
    prof = make_profile({"salary": SALARY})
    prof["run"]["max_account_pdfs"] = 20
    result = go(conn, tmp_path, site, prof=prof, provider=stub_new())
    assert "El sitio dice que la cuenta llegó al máximo de documentos" in result["halted"] and "pantalla desconocida" not in result["halted"]


def test_an_sent_offer_missing_in_mis_postulaciones_is_listed_as_unconfirmed(conn, tmp_path):
    add_offer(conn, 5, title="Oferta 5")
    add_offer(conn, 6, title="Oferta 6")
    site = standard_site(pages={APPLICATIONS_URL: cards(("IM9", "Oferta 5", "Empresa 5", "Postulado"))}, lists_posted=False)
    result = go(conn, tmp_path, site, provider=own_name_provider())
    assert len(result["sent"]) == 2
    assert [i["title"] for i in result["verify"]["unconfirmed"]] == ["Oferta 6"]
    text = report(result, tmp_path)
    assert "Oferta 6 (Empresa 6): sin confirmar en Mis postulaciones" in text and "Oferta 5 (Empresa 5): sin confirmar" not in text
    assert status_of(conn, "0000000000000006") == "unconfirmed" and status_of(conn, "0000000000000005") == "applied"


def test_no_sent_offers_means_no_final_check(conn, tmp_path):
    result = go(conn, tmp_path, standard_site(), provider=stub_new())
    assert result.get("verify") is None


# ── TAREA-061: cuenta llena, se postula con el CV base de la variante ────────

BASE_VENTAS = "CV-Base-Ventas.pdf"


def full_account(conn, prof_pdf=BASE_VENTAS, apps_count=7, **kw):
    """10 PDFs sin nada que rotar: el por defecto, el base de la variante y relleno que no figura en `applications`."""
    return full_site(*([prof_pdf] if prof_pdf else []), **kw)  # el resto es relleno que no figura en applications: nada para rotar


def base_profile(default_pdf_name=BASE_VENTAS, **flags):
    prof = make_profile({"salary": SALARY}, default_pdf_name=default_pdf_name)
    prof["variants"][0]["pdf"] = f"cv/{BASE_VENTAS}"
    prof["apply"].update(flags)
    return prof


def test_with_a_full_account_and_nothing_deletable_both_offers_go_with_the_base_cv(conn, tmp_path):
    site = full_account(conn)
    for n in (5, 6):
        add_offer(conn, n, title=f"Oferta {n}")
    result = go(conn, tmp_path, site, prof=base_profile(), provider=own_name_provider())
    assert result["halted"] is None and [i["file"] for i in result["sent"]] == [BASE_VENTAS, BASE_VENTAS]
    assert site.uploads == [] and site.deleted == [] and result["deleted"] == [] and len(site.cvs) == 10
    assert rows(conn, "SELECT cv_file, cv_version FROM applications WHERE id16 IN ('0000000000000005', '0000000000000006')") == [(BASE_VENTAS, "base")] * 2
    assert site.default == BASE_VENTAS  # al terminar se restaura el por defecto
    text = report(result, tmp_path)
    assert "Enviadas con el CV base (cuenta llena): 2" in text and "Oferta 5 (Empresa 5)" in text
    assert f"{BASE_VENTAS} (CV base, cuenta llena)" in text


def test_the_questions_of_an_offer_on_the_base_cv_use_the_base_cv_text(conn, tmp_path):
    site = full_account(conn)
    add_offer(conn, 5, title="Oferta 5")
    saved_cv(conn, tmp_path, "0000000000000005")
    seen = []
    original = apply.answer_questions
    apply.answer_questions = lambda *a, **k: seen.append(k.get("cv_text")) or original(*a, **k)
    try:
        go(conn, tmp_path, site, prof=base_profile(), provider=own_name_provider())
    finally:
        apply.answer_questions = original
    assert seen == [None]


def test_if_the_variant_has_no_base_pdf_in_the_account_it_halts_with_todays_notice(conn, tmp_path):
    site = full_account(conn, prof_pdf=None)
    add_offer(conn, 5, title="Oferta 5")
    result = go(conn, tmp_path, site, prof=base_profile(default_pdf_name=None), provider=own_name_provider())
    assert_stopped_without_touching_the_account(result, site)


def test_with_free_cv_slots_false_the_base_cv_is_used_without_trying_to_delete(conn, tmp_path):
    site = full_account(conn)
    add_offer(conn, 5, title="Oferta 5")
    result = go(conn, tmp_path, site, prof=base_profile(free_cv_slots=False), provider=own_name_provider())
    assert [i["file"] for i in result["sent"]] == [BASE_VENTAS] and site.deleted == [] and site.uploads == []


def test_the_default_pdf_name_is_used_first_and_a_retry_does_not_name_the_new_cv_like_the_base(conn, tmp_path):
    prof = base_profile()
    prof["variants"][0]["default_pdf_name"] = "CV-Base-Dev.pdf"
    site = full_account(conn)
    site.cvs[-1] = "CV-Base-Dev.pdf"
    assert apply.base_pdf(prof, "dev", [{"name": n} for n in site.cvs])["name"] == "CV-Base-Dev.pdf"
    id16 = add_offer(conn, 5, title="Oferta 5")
    saved_cv(conn, tmp_path, id16)
    db.save_application(conn, id16, "dev", 80, "cv_uploaded", "x", [], BASE_VENTAS, None, "base")
    assert provide(conn, {"id16": id16, "variant": "dev"}) != BASE_VENTAS


def test_with_a_free_slot_everything_goes_as_before(conn, tmp_path):
    add_offer(conn, 5, title="Oferta 5")
    site = standard_site(cvs=(BASE_VENTAS,), default=BASE_VENTAS, pages={APPLICATIONS_URL: cards()})
    result = go(conn, tmp_path, site, prof=base_profile(), provider=own_name_provider())
    assert len(site.uploads) == 1 and not result["sent"][0]["base"] and "Enviadas con el CV base" not in report(result, tmp_path)
