"""TAREA-054: el CV de una oferta pendiente libera lugar, se vuelve a subir desde la copia, y el sueldo por jornada queda pendiente."""
import json
from datetime import date

import pytest

from fakesite import BASE_CV, standard_site
from test_apply_live import MODEL, SALARY, FakeClient, add_offer, conn, make_profile, reply, rows  # noqa: F401
from test_cv_slots import NEW_CV, applied, cards, days_ago, full_site, go, offer_to_send, stub_new  # noqa: F401
from compuapply import apply, cv, db


def pending(conn, n, name, days):
    id16 = add_offer(conn, n)
    db.mark_status(conn, id16, "pending_question")
    db.save_application(conn, id16, "dev", 80, "pending_question", "x", [], name, None, "c8")
    conn.execute("UPDATE applications SET applied_at = ? WHERE id16 = ?", (days_ago(days), id16))
    conn.commit()
    return id16


def sent_three(conn):
    """3 enviadas de menos de 30 días, en «Postulado»."""
    for n, name in enumerate(("CV-Env-1.pdf", "CV-Env-2.pdf", "CV-Env-3.pdf"), start=20):
        applied(conn, n, name, f"IM{n}")
    return tuple((f"IM{n}", f"Oferta {n}", f"Empresa {n}", "Postulado") for n in (20, 21, 22))


def test_with_two_copies_of_a_the_second_copy_goes_first_and_then_the_oldest_pending_when_a_has_one(conn, tmp_path):
    pending(conn, 2, "CV-A.pdf", 3)
    pending(conn, 3, "CV-B.pdf", 9)
    pending(conn, 4, "CV-C.pdf", 6)
    apps = sent_three(conn)
    site = full_site("CV-A.pdf", "CV-A.pdf", "CV-B.pdf", "CV-C.pdf", "CV-Env-1.pdf", "CV-Env-2.pdf", "CV-Env-3.pdf", apps=apps)
    offer_to_send(conn)  # D
    result = go(conn, tmp_path, site, provider=stub_new())
    assert site.deleted == ["CV-A.pdf"] and site.cvs.count("CV-A.pdf") == 1 and "CV-B.pdf" in site.cvs
    assert result["deleted"][0]["reason"] == "copia repetida del CV de una oferta que no se envió"


def test_with_one_copy_of_a_the_oldest_pending_cv_is_deleted_and_never_a_sent_one(conn, tmp_path):
    pending(conn, 2, "CV-A.pdf", 3)
    b = pending(conn, 3, "CV-B.pdf", 9)
    pending(conn, 4, "CV-C.pdf", 6)
    apps = sent_three(conn)
    site = full_site("CV-A.pdf", "CV-B.pdf", "CV-C.pdf", "CV-Env-1.pdf", "CV-Env-2.pdf", "CV-Env-3.pdf", "Relleno-x.pdf", apps=apps)
    offer_to_send(conn)  # E
    result = go(conn, tmp_path, site, provider=stub_new())
    assert result["halted"] is None and site.deleted == ["CV-B.pdf"] and site.uploads == [NEW_CV]
    assert result["deleted"] == [{"name": "CV-B.pdf", "local": "CV-B.pdf", "reason": "CV de una oferta pendiente que no se envió"}]
    assert conn.execute("SELECT cv_file FROM applications WHERE id16 = ?", (b,)).fetchone() == ("CV-B.pdf",)  # la fila conserva el nombre
    assert all(n in site.cvs for n in ("CV-Env-1.pdf", "CV-Env-2.pdf", "CV-Env-3.pdf"))


def test_only_sent_ones_in_the_account_rotates_the_oldest(conn, tmp_path):
    apps = sent_three(conn)
    site = full_site("CV-Env-1.pdf", "CV-Env-2.pdf", "CV-Env-3.pdf", apps=apps)
    offer_to_send(conn)
    result = go(conn, tmp_path, site, provider=stub_new())
    assert result["halted"] is None and site.deleted == ["CV-Env-1.pdf"] and len(site.uploads) == 1


def test_the_cv_of_the_offer_being_posted_is_never_the_one_deleted(conn, tmp_path):
    current = pending(conn, 5, "CV-Propio.pdf", 20)  # la oferta que se está por postular
    db.mark_status(conn, current, "new")  # (reintento: vuelve a la cola)
    site = full_site("CV-Propio.pdf", apps=())
    # el nombre del proveedor es otro: el propio CV (de la misma oferta) no se puede borrar
    result = go(conn, tmp_path, site, provider=stub_new())
    assert current and site.deleted == [] and result["halted"]


def test_the_default_and_the_variant_pdfs_are_not_deleted_even_if_pending(conn, tmp_path):
    pending(conn, 2, BASE_CV, 30)
    site = full_site(apps=())
    offer_to_send(conn)
    result = go(conn, tmp_path, site, provider=stub_new())
    assert site.deleted == [] and result["halted"]


def test_a_retried_offer_without_its_pdf_in_the_account_is_uploaded_from_the_archive_without_calling_the_cv_client(conn, tmp_path):
    archive = tmp_path / "cv" / "postulaciones"
    archive.mkdir(parents=True)
    (archive / "CV-B.pdf").write_bytes(b"%PDF-" + b"1" * 50)
    b = add_offer(conn, 3)
    db.save_application(conn, b, "dev", 80, "pending_question", "x", [], "CV-B.pdf", None, "c8")
    prof = make_profile({"salary": SALARY})
    provider = apply.make_cv_provider(conn, prof, None, MODEL, archive_dir=archive)  # sin cliente: armar el CV de nuevo fallaría
    result = go(conn, tmp_path, standard_site(), prof=prof, provider=provider, archive=archive)
    assert result["halted"] is None and result["sent"][0]["file"] == "CV-B.pdf" and result["sent"][0]["id16"] == b
    assert result["pdfs"]["uploaded"] == 1 and result["pdfs"]["reused"] == 0


# ── el sueldo según la jornada (057) ────────────────────────────────────────

SHIFT_PAY = "Indicar pretensión económica de bolsillo por jornada de Lunes a viernes de 9 a 13 hs"
FULL, PART, DAILY = "$ 900.000", "$ 500.000", "$ 40.000 por día"
PAY = {"salary": FULL, "salary_part_time": PART, "salary_daily": DAILY}


def open_q(text):
    return {"text": text, "kind": "open", "options": []}


def ask(question, answers=None, *items, **kw):
    client = FakeClient(reply(*items)) if items else FakeClient(reply())
    return apply.answer_questions([question], make_profile(answers), client, MODEL, "dev", [], **kw)[0], client


@pytest.mark.parametrize("text,key", [
    (SHIFT_PAY, "salary_part_time"), ("Sueldo pretendido para el turno de 14 a 18", "salary_part_time"),
    ("Pretensión salarial medio tiempo", "salary_part_time"), ("Sueldo para media jornada", "salary_part_time"),
    ("Pretensión salarial part time", "salary_part_time"), ("Sueldo pretendido de 09:00 a 13:00 hs", "salary_part_time"),
    ("Pretensión por día", "salary_daily"), ("Sueldo diario pretendido", "salary_daily"), ("Pretensión por jornada", "salary_daily"),
    ("Indicar pretensión económica", "salary"), ("Sueldo pretendido, jornada completa de 8 a 17", "salary"),
    ("Indicar pretensión económica, de 1 a 2 millones", "salary"), ("Pretensión salarial ($ 800 a 900 mil)", "salary"),
    ("Pretensión entre 15 a 20 mil pesos", "salary"), ("Sueldo entre $ 5 a 8", "salary"),
])
def test_the_pay_answer_is_the_declared_one_for_the_shift(text, key):
    entry, client = ask(open_q(text), PAY)
    assert entry["source"] == "answers" and entry["text"] == PAY[key] and client.calls == []
    assert apply.pay_key(open_q(text)) == key


@pytest.mark.parametrize("text", ["Sueldo pretendido por hora", "Pretensión salarial por horas"])
def test_a_pay_question_per_hour_stays_pending(text):
    entry, client = ask(open_q(text), PAY)
    assert entry["source"] == "unknown" and entry["reason"] == "sueldo de otra jornada: contestalo en Preguntas" and client.calls == []


@pytest.mark.parametrize("text,key", [(SHIFT_PAY, "salary_part_time"), ("Pretensión por día", "salary_daily")])
def test_without_the_key_of_the_shift_it_stays_pending_naming_the_key(text, key):
    entry, client = ask(open_q(text), {"salary": FULL})
    assert entry["source"] == "unknown" and entry["reason"] == f"falta {key} en Parámetros" and entry["text"] is None and client.calls == []


def test_a_declared_answer_for_that_exact_question_is_still_used():
    prof = make_profile({"salary": FULL, apply.normalize_question(SHIFT_PAY): "$ 20.000"})
    client = FakeClient(reply({"text": "$ 20.000", "source": "answers"}))
    [entry] = apply.answer_questions([open_q(SHIFT_PAY)], prof, client, MODEL, "dev", [])
    assert entry["source"] == "answers" and entry["text"] == "$ 20.000"


def test_requeue_uses_the_pay_of_the_shift(conn):
    id16 = add_offer(conn, 7)
    db.mark_status(conn, id16, "pending_question")
    db.save_pending_question(conn, id16, SHIFT_PAY, [])
    assert apply.requeue_answerable(conn, make_profile({"salary": FULL})) == 0
    assert apply.requeue_answerable(conn, make_profile(PAY)) == 1


# ── dónde vive y movilidad ───────────────────────────────────────────────────

LOCATION, MOBILITY = "Palermo, Capital Federal", "Subte y colectivo"


def test_where_do_you_live_and_mobility_are_answered_with_the_declared_texts():
    entry, client = ask(open_q("¿Dónde vivís? ¿Tenés movilidad?"), {"location": LOCATION, "mobility": MOBILITY})
    assert entry["source"] == "answers" and LOCATION in entry["text"] and MOBILITY in entry["text"] and client.calls == []


def test_each_topic_alone_uses_its_text_as_is():
    assert ask(open_q("¿En qué barrio vivís?"), {"location": LOCATION})[0]["text"] == LOCATION
    assert ask(open_q("¿Cuál es tu movilidad?"), {"mobility": MOBILITY})[0]["text"] == MOBILITY


def test_if_one_of_the_two_is_missing_it_goes_to_the_model_and_stays_pending_if_it_says_unknown():
    entry, client = ask(open_q("¿Dónde vivís? ¿Tenés movilidad?"), {"location": LOCATION}, {"source": "unknown"})
    assert entry["source"] == "unknown" and len(client.calls) == 1


def test_requeue_considers_location_and_mobility_answerable(conn):
    id16 = add_offer(conn, 9)
    db.mark_status(conn, id16, "pending_question")
    db.save_pending_question(conn, id16, "¿Dónde vivís? ¿Tenés movilidad?", [])
    assert apply.requeue_answerable(conn, make_profile({"location": LOCATION})) == 0
    assert apply.requeue_answerable(conn, make_profile({"location": LOCATION, "mobility": MOBILITY})) == 1


# ── el CV de la postulación y la experiencia ─────────────────────────────────

NORTE = {"position": "Norte", "company": "Norte", "employment_period": "03/2023 - 09/2023", "key_responsibilities": [], "skills_acquired": []}
CAJERO = {"position": "Cajero", "company": "Super", "employment_period": "03/2022 - 12/2022", "key_responsibilities": [], "skills_acquired": []}
SALES = {"text": "¿Cuánta experiencia en ventas tenés?", "kind": "closed", "options": ["Más de 2 años", "Menos de 2 años", "No tengo experiencia"]}
CALL = {"text": "¿Cuánta experiencia en call center tenés?", "kind": "closed", "options": ["Más de 2 años", "Menos de 2 años", "No tengo experiencia"]}


def jobs_profile(*jobs, answers=None):
    prof = make_profile(answers)
    prof["variants"][0] = {**prof["variants"][0], "resume": {**prof["variants"][0]["resume"], "experience_details": list(jobs)}}
    return prof


def run(questions, prof, *items):
    client = FakeClient(reply(*items))
    return apply.answer_questions(questions, prof, client, MODEL, "dev", [], today=date(2026, 11, 6)), client


def test_the_months_are_added_by_the_code_and_the_model_option_does_not_count():
    [entry], client = run([SALES], jobs_profile(NORTE, CAJERO), {"option": 0, "source": "cv", "jobs": [0, 1]})  # 7 + 10 = 17 meses
    assert entry["source"] == "cv" and entry["option"] == 1 and entry["reason"] is None
    sent = json.dumps(client.calls[0], ensure_ascii=False)
    assert "0) Norte" in sent and "7 meses" in sent and "10 meses" in sent  # los puestos y sus duraciones van con el CV


def test_no_jobs_means_no_experience():
    [entry], _ = run([CALL], jobs_profile(NORTE, CAJERO), {"option": 0, "source": "cv", "jobs": []})
    assert entry["source"] == "cv" and entry["option"] == 2


def test_more_than_two_years_when_the_months_add_up():
    long_job = {**CAJERO, "employment_period": "01/2020 - 12/2022"}
    [entry], _ = run([SALES], jobs_profile(long_job), {"option": 1, "source": "cv", "jobs": [0]})
    assert entry["option"] == 0


def test_overlapping_months_are_not_counted_twice():
    a, b = {**NORTE, "employment_period": "01/2023 - 12/2023"}, {**CAJERO, "employment_period": "06/2023 - 05/2024"}
    [entry], _ = run([SALES], jobs_profile(a, b), {"option": 0, "source": "cv", "jobs": [0, 1]})  # 01/2023 a 05/2024 = 17, no 24
    assert entry["option"] == 1
    assert apply.experience_option(SALES, 24) is None  # exactamente 2 años: ni «más de» ni «menos de»


def test_an_open_end_counts_until_today():
    now = {**NORTE, "employment_period": "11/2024 - Actualidad"}
    [entry], _ = run([SALES], jobs_profile(now), {"option": 1, "source": "cv", "jobs": [0]})  # 11/2024 a 11/2026 = 25 meses
    assert entry["option"] == 0


@pytest.mark.parametrize("period", ["2024 - 2025", "reciente", "03/2023"])
def test_unreadable_dates_leave_it_pending(period):
    [entry], _ = run([SALES], jobs_profile({**NORTE, "employment_period": period}), {"option": 0, "source": "cv", "jobs": [0]})
    assert entry["source"] == "unknown" and entry["reason"] == "el control: no se pudieron leer las fechas del CV"


def test_unreadable_options_or_bad_jobs_leave_it_pending():
    odd = {"text": "¿Cuánta experiencia tenés en años?", "kind": "closed", "options": ["Alguna", "Mucha"]}
    [entry], _ = run([odd], jobs_profile(NORTE), {"option": 0, "source": "cv", "jobs": [0]})
    assert entry["source"] == "unknown" and entry["reason"] == "el control: no se pudieron leer los rangos de las opciones"
    [entry], _ = run([SALES], jobs_profile(NORTE), {"option": 0, "source": "cv", "jobs": [7]})
    assert entry["source"] == "unknown" and entry["reason"] == apply.DURATION_REASON
    [entry], _ = run([SALES], jobs_profile(NORTE), {"option": 0, "source": "cv"})  # sin `jobs`
    assert entry["source"] == "unknown"


def test_a_model_unknown_stays_unknown_and_a_declared_answer_still_wins():
    [entry], _ = run([SALES], jobs_profile(NORTE), {"source": "unknown", "jobs": [0]})
    assert entry["source"] == "unknown"
    prof = jobs_profile(NORTE, answers={apply.normalize_question(SALES["text"]): "Más de 2 años"})
    [entry], _ = run([SALES], prof, {"source": "unknown", "jobs": []})
    assert entry["source"] == "answers" and entry["option"] == 0


@pytest.mark.parametrize("option,months,expected", [
    ("Entre 1 y 3 años", 18, True), ("1 a 2 años", 24, True), ("Entre 6 meses y 1 año", 9, True), ("Dos años o más", 24, True),
    ("Hasta 1 año", 12, True), ("Menos de 1 año", 12, False), ("Más de 6 meses", 7, True), ("3 años o más", 30, False)])
def test_option_ranges(option, months, expected):
    question = {"text": "¿Cuánta experiencia tenés?", "kind": "closed", "options": [option, "No tengo experiencia"]}
    assert (apply.experience_option(question, months) == 0) is expected


def test_the_text_of_the_application_cv_is_used_as_the_candidate_cv():
    client = FakeClient(reply({"source": "unknown"}))
    apply.answer_questions([{"text": "¿Tenés secundario?", "kind": "closed", "options": ["Sí", "No"]}], jobs_profile(NORTE), client, MODEL, "dev", [],
                           cv_text="TEXTO DEL CV A MEDIDA")
    assert "TEXTO DEL CV A MEDIDA" in json.dumps(client.calls[0], ensure_ascii=False)


def test_the_open_experience_answer_from_the_cv_is_sent_in_the_negative_and_an_invented_figure_is_rejected():
    question = open_q("¿Tenés experiencia en call center? Detallar")
    ok = "No tengo experiencia en call center; sí en atención al público y ventas en Norte."
    [entry], _ = run([question], jobs_profile(NORTE, CAJERO), {"text": ok, "source": "cv", "jobs": []})
    assert entry["source"] == "cv" and entry["text"] == ok
    [entry], _ = run([question], jobs_profile(NORTE, CAJERO), {"text": "Trabajé 9 meses en Norte.", "source": "cv", "jobs": []})
    assert entry["source"] == "unknown" and entry["reason"].startswith("el control: respuesta:")
    [entry], _ = run([question], jobs_profile(NORTE, CAJERO), {"text": "No en call center; trabajé 7 meses en Norte.", "source": "cv", "jobs": []})  # duración calculada
    assert entry["source"] == "cv"


def test_the_prompt_is_the_new_version_and_asks_for_negative_answers_and_jobs():
    assert apply.ANSWER_PROMPT_VERSION == "a15"
    assert "también en negativo" in apply.ANSWER_SYSTEM and "`jobs`" in apply.ANSWER_SYSTEM and "solo para datos personales" in apply.ANSWER_SYSTEM


def test_cv_text_of_a_saved_cv_has_no_name_nor_contact(tmp_path):
    html = ('<!doctype html><html><head><title>Currículum</title><style>h1{}</style></head><body><header><h1><span>Ana</span><span>Pérez</span></h1>'
            '<div class="headline">Vendedora</div><div class="contact"><span>ana@example.com</span><span>1155550000</span></div></header>'
            '<section><h2>Experiencia relevante</h2><h3>Cajero</h3><div class="meta"><span>Super</span><span>03/2022 - 12/2022</span></div>'
            '<ul><li>Atendí caja</li></ul></section></body></html>')
    path = tmp_path / "cv.html"
    path.write_text(html, encoding="utf-8")
    text = cv.text_of(path)
    assert "Vendedora" in text and "Atendí caja" in text and "03/2022 - 12/2022" in text
    assert "ana@example.com" not in text and "1155550000" not in text and "Pérez" not in text and "h1{}" not in text
    assert cv.text_of(tmp_path / "missing.html") == ""


# ── las pendientes de otra versión vuelven a la cola ─────────────────────────

def db_status(conn, id16):
    return conn.execute("SELECT status FROM offers WHERE id16 = ?", (id16,)).fetchone()[0]


def test_a_pending_question_saved_with_the_current_version_stays_and_an_old_one_goes_back_to_new(conn):
    new, old, null = add_offer(conn, 11), add_offer(conn, 12), add_offer(conn, 13)
    for id16, version in ((new, apply.ANSWER_PROMPT_VERSION), (old, "a1"), (null, None)):
        db.mark_status(conn, id16, "pending_question")
        db.save_pending_question(conn, id16, "¿Algo personal que no tengo cargado?", [], answer_version=version)
    assert apply.requeue_answerable(conn, make_profile()) == 2
    assert [db_status(conn, i) for i in (new, old, null)] == ["pending_question", "new", "new"]
    assert rows(conn, "SELECT id16 FROM pending_questions") == [(new,)]


def test_a_new_pending_question_is_saved_with_the_current_version(conn):
    id16 = add_offer(conn, 14)
    db.save_pending_question(conn, id16, "¿Algo?", [])
    assert rows(conn, "SELECT answer_version FROM pending_questions") == [(apply.ANSWER_PROMPT_VERSION,)]


def test_the_new_answer_keys_are_in_the_profile_and_the_form():
    from compuapply import gui, profile
    assert {"location", "mobility", "salary_part_time", "salary_daily"} <= set(profile.ANSWER_KEYS)
    assert set(profile.ANSWER_KEYS) <= set(gui.ANSWER_LABELS)


# ── cero meses no es «menos de 1 año» (058) ──────────────────────────────────

def exp_q(*options):
    return {"text": "¿Cuánta experiencia tenés?", "kind": "closed", "options": list(options)}


APEX = exp_q("Sí, tengo MÁS de 2 años de experiencia", "Sí, tengo MENOS de 2 años de experiencia", "No, no tengo experiencia")
NO_EXPERIENCE_WHY = "el control: el CV no muestra esa experiencia y ninguna opción dice que no"


def test_zero_months_never_picks_less_than_n():
    question = exp_q("Menos de 1 año", "De 1 a 2 años", "Más de 2 años")
    assert apply.experience_option(question, 0) is None
    assert apply.experience_option(question, 5) == 0
    assert apply.experience_option(exp_q("Hasta 1 año", "Más de 1 año"), 0) is None


def test_zero_months_picks_a_range_that_starts_at_zero():
    assert apply.experience_option(exp_q("0 a 1 año", "1 a 3 años", "Más de 3 años"), 0) == 0
    assert apply.experience_option(exp_q("Sin experiencia a 1 año", "De 1 a 2 años"), 0) == 0


def test_apex_options():
    assert apply.experience_option(APEX, 0) == 2 and apply.experience_option(APEX, 17) == 1


def test_a_cv_without_that_experience_and_no_option_for_it_stays_pending_with_the_reason():
    question = exp_q("Menos de 1 año", "De 1 a 2 años", "Más de 2 años")
    [entry], _ = run([question], jobs_profile(NORTE), {"option": 0, "source": "cv", "jobs": []})
    assert entry["source"] == "unknown" and entry["option"] is None and entry["reason"] == NO_EXPERIENCE_WHY
    [entry], _ = run([question], jobs_profile(NORTE), {"option": 0, "source": "cv", "jobs": [0]})  # 7 meses
    assert entry["source"] == "cv" and entry["option"] == 0
    [entry], _ = run([APEX], jobs_profile(NORTE), {"option": 0, "source": "cv", "jobs": []})
    assert entry["source"] == "cv" and entry["option"] == 2


# ── TAREA-060: cifras del CV, respuesta vacía, presencial en la zona, informe ─

OPEN_EXP = {"text": "Contanos dónde, cuándo y cuánto tiempo trabajaste", "kind": "open", "options": []}


def no_declared(*jobs):
    prof = jobs_profile(*jobs)
    prof["answers"] = {}
    return prof


def test_dates_of_the_base_cv_pass_the_control_and_a_figure_that_is_not_in_the_cv_does_not():
    prof = no_declared(NORTE)
    assert apply._open_text_problems("Trabajé de 03/2023 a 09/2023 en Norte.", prof["variants"][0]["resume"], {}) == []
    with_sa = no_declared({**NORTE, "company": "Norte S.A."})["variants"][0]["resume"]
    assert apply._open_text_problems("Trabajé de 03/2023 a 09/2023 en Norte S.A.", with_sa, {}) == []
    problems = apply._open_text_problems("Trabajé 3 años en Norte.", prof["variants"][0]["resume"], {})
    assert any("cifra" in p for p in problems)


def test_dates_and_years_of_the_cv_of_the_application_also_count():
    resume = no_declared(NORTE)["variants"][0]["resume"]
    assert any("cifra" in p for p in apply._open_text_problems("Estudié hasta 2019 en Norte.", resume, {}))
    assert apply._open_text_problems("Estudié hasta 2019 en Norte.", resume, {}, cv_text="Secundario completo 2019") == []


def test_an_open_answer_with_dates_from_the_cv_is_sent_through_answer_questions():
    [entry], _ = run([OPEN_EXP], no_declared(NORTE), {"text": "Trabajé de 03/2023 a 09/2023 en Norte.", "source": "cv"})
    assert entry["source"] == "cv" and entry["text"].startswith("Trabajé de 03/2023")


def test_an_empty_open_answer_is_asked_again_once_and_the_second_text_is_used():
    client = FakeClient(reply({"source": "cv", "text": ""}), reply({"source": "cv", "text": "Trabajé en Norte."}))
    [entry] = apply.answer_questions([OPEN_EXP], no_declared(NORTE), client, MODEL, "dev", [], today=date(2026, 11, 6))
    assert entry["source"] == "cv" and entry["text"] == "Trabajé en Norte." and len(client.calls) == 2


def test_an_open_answer_empty_twice_stays_pending_with_the_same_reason():
    client = FakeClient(reply({"source": "cv", "text": ""}), reply({"source": "cv", "text": " "}))
    calls = []
    [entry] = apply.answer_questions([OPEN_EXP], no_declared(NORTE), client, MODEL, "dev", calls, today=date(2026, 11, 6))
    assert entry["source"] == "unknown" and entry["reason"] == "el modelo no escribió respuesta" and len(client.calls) == 2 and len(calls) == 2


def test_only_the_empty_questions_are_asked_again_and_a_model_unknown_is_not_retried():
    other = {"text": "¿Tenés estudios en Computer Engineering?", "kind": "closed", "options": ["Sí", "No"]}
    client = FakeClient(reply({"source": "unknown"}, {"option": 0, "source": "cv"}, {"source": "cv", "text": ""}), reply({"source": "cv", "text": "Trabajé en Norte."}))
    out = apply.answer_questions([OPEN_EXP, other, {**OPEN_EXP, "text": "Contanos más"}], no_declared(NORTE), client, MODEL, "dev", [])
    assert [e["source"] for e in out] == ["unknown", "cv", "cv"] and out[2]["text"] == "Trabajé en Norte."
    second = client.calls[1]["input"] if "input" in client.calls[1] else str(client.calls[1])
    assert "Contanos más" in str(second) and "secundario" not in str(second)


def test_the_zone_line_and_the_in_person_rule_reach_the_model_and_the_version_is_bumped(monkeypatch):
    monkeypatch.setattr(apply, "availability_answer", lambda *a, **k: None)  # el camino en que contesta el modelo
    q = {"text": "¿Tenés disponibilidad para trabajar 100% presencial en nuestra sede de Microcentro?", "kind": "closed", "options": ["Sí", "No"]}
    prof = make_profile({"modality": "presencial"})
    client = FakeClient(reply({"option": 0, "source": "answers"}))
    [yes] = apply.answer_questions([q], prof, client, MODEL, "dev", [], offer_in_zone=True)
    assert yes["options"][yes["option"]] == "Sí" and "dentro de la zona del candidato" in str(client.calls[0])
    client = FakeClient(reply({"option": 1, "source": "answers"}))
    [no] = apply.answer_questions([q], make_profile({"modality": "remoto"}), client, MODEL, "dev", [], offer_in_zone=True)
    assert no["options"][no["option"]] == "No"
    client = FakeClient(reply({"source": "unknown"}))
    apply.answer_questions([q], prof, client, MODEL, "dev", [])
    assert "dentro de la zona" not in str(client.calls[0]).replace("cuando el pedido dice que la oferta está dentro de la zona del candidato", "")
    assert "sede" in apply.ANSWER_SYSTEM and "mudarse" in apply.ANSWER_SYSTEM and apply.ANSWER_PROMPT_VERSION != "a2"


def test_the_report_has_the_answers_of_a_sent_offer_and_of_a_pending_one_without_the_declared_values(conn, tmp_path):
    from fakesite import KQ_URL, fixture
    from test_cv_slots import report
    add_offer(conn, 5, title="Oferta 5")
    site = standard_site(pages={KQ_URL: fixture("apply_kq_sales.html")})
    result = go(conn, tmp_path, site, client=FakeClient(reply({"option": 0, "source": "cv"})), provider=stub_new())
    text = report(result, tmp_path)
    assert "## Respuestas (1 ofertas con preguntas)" in text and "— pendiente" in text and "(sin respuesta) — pendiente:" in text


def test_fixed_answers_are_shown_as_a_key_and_never_with_their_value(conn, tmp_path):
    from test_cv_slots import report
    pay = {"text": "Indicar remuneración pretendida", "kind": "open", "options": []}
    where = {"text": "¿En qué barrio vivís?", "kind": "open", "options": []}
    prof = make_profile({"salary": SALARY, "location": LOCATION})
    entries = apply.answer_questions([pay, where], prof, FakeClient(reply()), MODEL, "dev", [])
    result = {"plan": {"skip": [], "send": [], "notes": []}, "answers": [{"id16": "X", "title": "T", "company": "C", "entries": entries}],
              "sent": [{"id16": "X"}], "skipped": [], "halted": None}
    lines = "\n".join(apply.answer_section(result))
    assert "(respuesta fija: salary)" in lines and "(respuesta fija: location)" in lines and SALARY not in lines and LOCATION not in lines
    assert "— enviada" in lines


def test_the_question_text_without_the_form_header():
    from compuapply import browser
    from fakesite import FakePage, FakeSite, KQ_URL, fixture, standard_site as site_
    site = site_(pages={KQ_URL: fixture("apply_kq_header.html")})
    page = FakePage(site)
    page.goto(KQ_URL)
    [q] = browser.read_questions(page)
    assert q["text"] == "¿Tenés experiencia en el área?"


# ── TAREA-065: interés y disponibilidad ──────────────────────────────────────

INTEREST = "¿Te interesa una vacante eventual por cobertura de vacaciones?"
ROTATING = "¿Tenés disponibilidad para turnos rotativos?"
ROTATING_ERD = "¿Puede trabajar en turnos rotativos, incluyendo fines de semana y feriados?"
ONSITE_FULL = "¿Tenés disponibilidad 100% presencial en Florida Oeste, full time y sábados de 9 a 13?"
SI_NO = ["SI", "NO"]
CAN = ["Sí, puedo", "No"]
TOTAL = {"availability": "Disponibilidad total", "modality": "presencial"}


def coded(text, options, answers, offer_in_zone=True):
    client = FakeClient(reply({"option": 1, "source": "answers"}))  # si el modelo contestara, diría la opción 1 («No»)
    q = {"text": text, "kind": "closed", "options": options}
    [entry] = apply.answer_questions([q], make_profile({**answers, "availability": answers.get("availability")}), client, MODEL, "dev", [],
                                     offer_in_zone=offer_in_zone)
    return entry, client


def chosen(entry):
    return entry["options"][entry["option"]] if entry["option"] is not None else None


def test_an_interest_question_is_always_yes_with_any_yes_no_options_and_never_calls_the_model():
    for options, yes in ((["Sí", "No"], "Sí"), (SI_NO, "SI"), (CAN, "Sí, puedo"), (["No me interesa", "Sí, me interesa"], "Sí, me interesa")):
        entry, client = coded(INTEREST, options, {"modality": "presencial"})
        assert chosen(entry) == yes and entry["source"] == "answers" and client.calls == []
    entry, _ = coded("¿Estás interesado en el puesto de vendedor?", CAN, {})
    assert chosen(entry) == "Sí, puedo"


def test_an_interest_question_that_clashes_with_the_declared_modality_stays_pending_and_never_no():
    entry, _ = coded("¿Te interesa trabajar de forma remota?", ["Sí", "No"], {"modality": "presencial"})
    assert entry["source"] == "unknown" and entry["option"] is None and "modality" in entry["reason"]
    entry, _ = coded("¿Te interesa trabajar de forma remota?", ["Sí", "No"], {"modality": "remoto"})
    assert chosen(entry) == "Sí"


def test_rotating_shifts_give_the_same_answer_with_si_no_and_with_si_puedo_no():
    for text in (ROTATING, ROTATING_ERD):
        a, _ = coded(text, SI_NO, TOTAL)
        b, _ = coded(text, CAN, TOTAL)
        assert (a["option"], b["option"]) == (0, 0) and a["source"] == b["source"] == "answers"
        no_a, _ = coded(text, SI_NO, {"availability": "Sin fines de semana ni turnos rotativos", "modality": "presencial"})
        no_b, _ = coded(text, CAN, {"availability": "Sin fines de semana ni turnos rotativos", "modality": "presencial"})
        assert (no_a["option"], no_b["option"]) == (1, 1)  # lo declarado lo niega: ahí sí es «No»


def test_the_in_person_full_time_saturday_question_comes_from_availability_or_stays_pending_with_the_new_reason():
    yes, client = coded(ONSITE_FULL, SI_NO, TOTAL)
    assert chosen(yes) == "SI" and client.calls == [] and yes["fixed"] == "availability + modality"
    short, _ = coded(ONSITE_FULL, SI_NO, {"availability": "Lunes a viernes", "modality": "presencial"})
    assert short["source"] == "unknown" and short["option"] is None and short["reason"] == "disponibilidad: falta en tus respuestas"
    remote, _ = coded(ONSITE_FULL, SI_NO, {"availability": "Disponibilidad total", "modality": "remoto"})
    assert chosen(remote) == "NO"  # modality no incluye presencial


def test_with_an_empty_availability_the_question_is_pending_and_never_no():
    for options in (SI_NO, CAN, ["Sí", "No"]):
        entry, _ = coded(ROTATING, options, {"modality": "presencial"})
        assert entry["source"] == "unknown" and entry["option"] is None and entry["reason"] == "disponibilidad: falta en tus respuestas"


def test_an_open_availability_answer_is_the_declared_availability_and_a_yes_no_word_from_the_model_picks_the_option():
    q = {"text": "¿Qué disponibilidad horaria tenés? Full time, fines de semana y feriados", "kind": "open", "options": []}
    [entry] = apply.answer_questions([q], make_profile(TOTAL), FakeClient(reply({"source": "unknown"})), MODEL, "dev", [])
    assert entry["source"] == "answers" and entry["text"].startswith("Disponibilidad total")
    other = {"text": "¿Tenés estudios en Computer Engineering?", "kind": "closed", "options": ["Sí, completos", "No"]}
    [yes] = apply.answer_questions([other], make_profile({}), FakeClient(reply({"option": -1, "source": "cv", "text": "Sí"})), MODEL, "dev", [])
    assert yes["option"] == 0 and yes["source"] == "cv"
    [bad] = apply.answer_questions([{**other, "options": ["Completos", "Incompletos"]}], make_profile({}),
                                   FakeClient(reply({"option": 9, "source": "cv", "text": ""})), MODEL, "dev", [])
    assert bad["reason"] == "el control: la opción elegida no existe"
    assert "siempre que sí" in apply.ANSWER_SYSTEM and apply.ANSWER_PROMPT_VERSION == "a15"
