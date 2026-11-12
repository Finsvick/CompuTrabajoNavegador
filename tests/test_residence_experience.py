"""TAREA-079: residencia desde `answers.location` y experiencia que el CV respalda."""
from datetime import date

from test_apply_live import MODEL, FakeClient, add_offer, conn, make_profile, reply  # noqa: F401
from compuapply import apply

TODAY = date(2026, 11, 11)


def profile(position="Vendedor Preventista", extra=(), location="Quilmes"):
    prof = make_profile()
    prof["answers"] = {**prof.get("answers", {}), "location": location}
    var = prof["variants"][0]
    details = [{"position": position, "company": "Kiosco Sol", "employment_period": "02/2022 - 03/2024",
                "key_responsibilities": list(extra), "skills_acquired": []}]
    prof["variants"][0] = {**var, "resume": {**var["resume"], "experience_details": details}}
    return prof


def go(question, prof, *items):
    [entry] = apply.answer_questions([question], prof, FakeClient(reply(*items)), MODEL, "dev", [], today=TODAY)
    return entry


def open_q(text):
    return {"text": text, "kind": "open", "options": []}


def closed_q(text):
    return {"text": text, "kind": "closed", "options": ["Sí", "No"]}


# ── residencia ───────────────────────────────────────────────────────────────

def test_resides_elsewhere_says_no_and_the_declared_place():
    entry = go(open_q("¿Reside en Canning?"), profile())
    assert entry["source"] == "answers" and entry["text"] == "No, vivo en Quilmes" and entry["reason"] is None


def test_resides_there_says_yes():
    entry = go(open_q("¿Vivís en Quilmes?"), profile())
    assert entry["source"] == "answers" and entry["text"].startswith("Sí")
    entry = go(open_q("¿Residís en Quilmes, Bernal, alrededores? ¿Dónde?"), profile())
    assert entry["source"] == "answers" and entry["text"].startswith("Sí")


def test_closed_residence_picks_the_option():
    assert go(closed_q("¿Reside en Canning?"), profile())["option"] == 1
    assert go(closed_q("¿Reside en Quilmes?"), profile())["option"] == 0


def test_without_declared_location_it_is_not_a_code_answer():
    prof = profile()
    prof["answers"].pop("location")
    entry = go(open_q("¿Reside en Canning?"), prof, {"text": "No sé", "source": "unknown", "jobs": []})
    assert entry["source"] == "unknown"


def test_moving_questions_are_not_residence():
    assert apply.residence_answer(open_q("¿Estás dispuesto a mudarte a vivir en Canning?"), {"location": "Quilmes"}) is None


def test_the_vocabulary_includes_declared_values():
    assert apply._open_text_problems("Vivo en Quilmes", {}, {"location": "Quilmes"}) == []


# ── experiencia: caja y listas ───────────────────────────────────────────────

CAJA = open_q("¿Contas con experiencia previa en Caja? ¿Donde?")


def test_caja_is_backed_by_cajero():
    entry = go(CAJA, profile("Cajero", ["Atención de caja en kiosco"]),
               {"text": "Sí, como Cajero en Kiosco Sol.", "source": "cv", "jobs": [0]})
    assert entry["source"] == "cv" and entry["text"].startswith("Sí")


def test_caja_without_caja_or_cajero_is_pending():
    entry = go(CAJA, profile("Vendedor Preventista"), {"text": "Sí, en Kiosco Sol.", "source": "cv", "jobs": [0]})
    assert entry["source"] == "unknown" and entry["reason"] == apply.YES_UNBACKED


def test_list_with_a_generic_option_is_backed():
    entry = go(open_q("¿Tenés experiencia en promoción, ventas o posiciones similares?"), profile("Vendedor Preventista", ["Ventas de bebidas"]),
               {"text": "Sí, como Vendedor Preventista en Kiosco Sol.", "source": "cv", "jobs": [0]})
    assert entry["source"] == "cv" and entry["text"].startswith("Sí")


def test_a_generic_alone_still_does_not_back_a_specific_yes():
    entry = go(closed_q("¿Tenés experiencia en ventas de indumentaria?"), profile("Vendedor Preventista", ["Ventas de bebidas"]),
               {"option": 0, "source": "cv", "jobs": [0]})
    assert entry["option"] == 1


# ── TAREA-080: una zona la contesta el modelo ────────────────────────────────

def test_a_zone_that_does_not_match_goes_to_the_model():
    entry = go(closed_q("¿Reside en zona sur?"), profile(), {"option": 0, "source": "answers", "jobs": []})
    assert entry["source"] == "answers" and entry["option"] == 0  # lo dijo el modelo, no el código
    entry = go(open_q("¿Residís en Tigre, Nordelta, alrededores?"), profile(), {"source": "unknown", "text": "", "jobs": []})
    assert entry["source"] == "unknown" and entry["reason"] == "el modelo dijo unknown"


def test_a_zone_that_matches_is_still_yes_and_a_town_still_no():
    assert apply.residence_answer(closed_q("¿Reside en zona sur?"), {"location": "Quilmes, zona sur"}) == {"option": 0, "label": "location"}
    assert apply.residence_answer(closed_q("¿Reside en zona sur?"), {"location": "Quilmes"}) is None
    assert apply.residence_answer(open_q("¿Residís en Tigre, Nordelta, alrededores?"), {"location": "Quilmes"}) is None
    assert apply.residence_answer(open_q("¿Reside en Canning?"), {"location": "Quilmes"})["text"] == "No, vivo en Quilmes"
    assert apply.residence_answer(open_q("¿Vivís en Quilmes?"), {"location": "Quilmes"})["text"].startswith("Sí")


def test_an_a9_pending_goes_back_to_the_queue_with_a10(conn):
    from compuapply import db
    assert db.ANSWER_PROMPT_VERSION == "a15"
    old = add_offer(conn, 21)
    db.mark_status(conn, old, "pending_question")
    db.save_pending_question(conn, old, "¿Reside en zona sur?", [], answer_version="a9")
    assert apply.requeue_answerable(conn, make_profile()) == 1
    assert conn.execute("SELECT status FROM offers WHERE id16 = ?", (old,)).fetchone()[0] == "new"
