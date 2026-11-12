"""TAREA-072: con nombres que se repiten, el archivo de la cuenta es de la fila más nueva; actitud con más palabras. Todo falso."""
from fakesite import BASE_CV, standard_site
from test_apply_live import MODEL, FakeClient, add_offer, conn, make_profile, reply, rows  # noqa: F401
from test_cv_names import UP, live, saved_cv, set_keyword  # noqa: F401
from test_cv_slots import applied, cards, days_ago, full_site, go, offer_to_send, stub_new  # noqa: F401
from compuapply import apply, cv, db

NAME = UP + "Cajero.pdf"


def pending_with(conn, n, name, status="pending_question"):
    id16 = add_offer(conn, n)
    db.mark_status(conn, id16, status)
    db.save_application(conn, id16, "dev", 80, status, "x", [], name, None, cv.CV_PROMPT_VERSION)
    return id16


def test_old_sent_and_new_pending_with_the_same_name_step_1b_picks_that_file(conn, tmp_path):
    applied(conn, 11, NAME, "IM11", days_ago(30))
    pending_with(conn, 2, NAME)
    site = full_site(NAME, apps=(("IM11", "A", "Acme", "Postulado"),))
    offer_to_send(conn)
    result = go(conn, tmp_path, site, provider=stub_new())
    assert result["halted"] is None and site.deleted == [NAME]
    assert result["deleted"][0]["reason"] == "CV de una oferta pendiente que no se envió"


def test_choose_deletion_decides_with_the_newest_row_of_each_file(conn):
    old = applied(conn, 11, NAME, "IM11", days_ago(30))
    new = pending_with(conn, 2, NAME)
    cvs = [{"id": "1", "name": BASE_CV, "default": True}, {"id": "2", "name": NAME, "default": False}]
    choice = apply.choose_deletion(conn, make_profile(), cvs, [], current="otra")
    assert choice["name"] == NAME and "pendiente" in choice["reason"]
    assert old != new


def test_old_sent_and_new_sent_with_the_same_name_the_rotation_uses_the_date_of_the_new_one(conn):
    applied(conn, 11, NAME, "IM11", days_ago(90))
    applied(conn, 12, NAME, "IM12", days_ago(2))  # la nueva: el archivo de la cuenta es de esta
    applied(conn, 13, UP + "Otro.pdf", "IM13", days_ago(10))
    cvs = [{"id": "1", "name": BASE_CV, "default": True}, {"id": "2", "name": NAME, "default": False}, {"id": "3", "name": UP + "Otro.pdf", "default": False}]
    choice = apply.choose_deletion(conn, make_profile(), cvs, [])
    assert choice["name"] == UP + "Otro.pdf"  # con la fecha vieja (90 días) se habría borrado el de «Cajero»
    assert choice["reason"].startswith("rotación")


def test_a_tie_in_applied_at_takes_the_last_written(conn):
    same = days_ago(5)
    first = applied(conn, 11, NAME, "IM11", same)
    second = pending_with(conn, 2, NAME)
    conn.execute("UPDATE applications SET applied_at = ?", (same,))
    conn.commit()
    cvs = [{"id": "2", "name": NAME, "default": False}]
    assert first and second
    assert "pendiente" in apply.choose_deletion(conn, make_profile(), cvs, [])["reason"]  # decide la última escrita: la pendiente


def test_the_new_pending_offer_reuses_its_file_even_if_an_old_row_has_the_same_name(conn, tmp_path):
    applied(conn, 11, NAME, "IM11", days_ago(30))
    new = add_offer(conn, 1, title="Cajero 1")
    set_keyword(conn, new, "cajero")
    saved_cv(conn, tmp_path, new)
    db.mark_status(conn, new, "new")
    db.save_application(conn, new, "dev", 85, "pending_question", "x", [], NAME, None, cv.CV_PROMPT_VERSION, "CV-Giovanni-Bianchi-Cajero-1-Empresa-1.pdf")
    archive = tmp_path / "cv" / "postulaciones"
    archive.mkdir(parents=True)
    (archive / "CV-Giovanni-Bianchi-Cajero-1-Empresa-1.pdf").write_bytes(b"%PDF-" + b"0" * 100)
    site = standard_site(cvs=(BASE_CV, NAME), default=BASE_CV)
    result = live(conn, tmp_path, site, archive)
    assert result["halted"] is None and site.uploads == [] and result["pdfs"]["reused"] == 1
    old_row = rows(conn, "SELECT cv_file, local_file FROM applications WHERE id16 = '%s'" % f"{11:016X}")
    assert old_row == [(NAME, NAME)]  # la fila vieja no cambia


def test_the_old_row_does_not_reuse_when_a_newer_row_owns_the_file(conn):
    old = applied(conn, 11, NAME, "IM11", days_ago(30))
    pending_with(conn, 2, NAME)
    info = {"name": NAME, "version": cv.CV_PROMPT_VERSION}
    assert apply._reusable(conn, {"id16": old}, info, [{"name": NAME}]) is False


# ── actitud ──────────────────────────────────────────────────────────────────

def answer(text, option, source="answers"):
    question = {"text": text, "kind": "closed", "options": ["SI", "NO"]}
    [entry] = apply.answer_questions([question], make_profile(), FakeClient(reply({"option": option, "source": source})), MODEL, "dev", [])
    return entry


def test_proactive_with_yes_passes_with_source_actitud():
    for text in ("¿Sos una persona proactiva?", "¿Sos responsable?", "¿Sos puntual?", "¿Trabajás bien bajo presión?", "¿Tenés buena predisposición?",
                 "¿Tenés buena comunicación?"):
        entry = answer(text, 0)
        assert entry["source"] == "actitud" and entry["option"] == 0, text


def test_a_question_that_asks_for_experience_or_figures_is_not_attitude():
    for text in ("¿Cumpliste objetivos de ventas mensuales?", "¿Cuántos objetivos cumpliste?", "¿Tenés experiencia en trabajo en equipo?",
                 "¿Cumplís objetivos de 3 meses?"):
        assert apply.is_attitude(text) is False, text
    entry = answer("¿Cumpliste objetivos de ventas mensuales?", 0)
    assert entry["source"] == "unknown" and entry["reason"] == "sin fuente"
    assert apply.is_attitude("¿Te gusta trabajar con objetivos?") and apply.is_attitude("¿Sos alguien sociable?")


def test_a_no_to_an_attitude_question_does_not_pass_as_actitud():
    entry = answer("¿Sos una persona proactiva?", 1)
    assert entry["source"] == "unknown"
