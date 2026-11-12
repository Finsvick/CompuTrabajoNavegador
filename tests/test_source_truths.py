"""TAREA-075: un SÍ de experiencia sale de algo que el CV nombra; una respuesta que dice lo que una verdad no queda pendiente por la etiqueta."""
from test_apply_live import MODEL, FakeClient, make_profile, reply
from compuapply import apply

TRUTHS = ["No estoy trabajando en este momento",
          "No tengo carnet de manipulación de alimentos ni libreta sanitaria, y nunca los tuve; estoy dispuesto a sacarlos a la brevedad"]


def profile(truths=(), answers=None, with_apparel=False):
    prof = make_profile(answers)
    variant = dict(prof["variants"][0])
    if truths:
        variant["truths"] = list(truths)
    if with_apparel:
        resume = dict(variant["resume"])
        resume["experience_details"] = [{"position": "Vendedor", "company": "Tienda", "employment_period": "01/2024 - 06/2024",
                                         "key_responsibilities": [{"responsibility": "Venta de indumentaria en local"}], "skills_acquired": []}]
        variant["resume"] = resume
    prof["variants"][0] = variant
    return prof


def closed(text, *options):
    return {"text": text, "kind": "closed", "options": list(options) or ["Sí", "No"]}


def go(question, prof, *items):
    client = FakeClient(reply(*items)) if items else FakeClient(reply())
    [entry] = apply.answer_questions([question], prof, client, MODEL, "dev", [])
    return entry, client


APPAREL = "¿Contas con experiencia en venta de indumentaria?"


# ── SÍ de experiencia ────────────────────────────────────────────────────────

def test_a_yes_to_experience_in_something_the_cv_does_not_name_becomes_no():
    entry, _ = go(closed(APPAREL, "SI", "NO"), profile(), {"option": 0, "source": "cv"})
    assert entry["source"] == "cv" and entry["option"] == 1


def test_with_a_cv_that_names_it_the_yes_stays():
    entry, _ = go(closed(APPAREL, "SI", "NO"), profile(with_apparel=True), {"option": 0, "source": "cv"})
    assert entry["source"] == "cv" and entry["option"] == 0


def test_a_no_to_experience_passes_as_before():
    entry, _ = go(closed(APPAREL, "SI", "NO"), profile(), {"option": 1, "source": "cv"})
    assert entry["source"] == "cv" and entry["option"] == 1


def test_an_open_yes_without_support_is_pending():
    question = {"text": APPAREL, "kind": "open", "options": []}
    entry, _ = go(question, profile(), {"text": "Sí, tengo experiencia en venta de indumentaria.", "source": "cv"})
    assert entry["source"] == "unknown" and entry["reason"] == "sí sin respaldo en el CV"


def test_generic_experience_words_alone_need_no_specific_support():
    assert apply._specific_experience({"text": "¿Tenés experiencia en ventas y atención al cliente?"}) == []
    assert apply._specific_experience({"text": APPAREL}) == ["indumentaria"]
    entry, _ = go(closed("¿Tenés experiencia en ventas?"), profile(), {"option": 0, "source": "cv"})
    assert entry["option"] == 0 and entry["source"] == "cv"


# ── verdades primero ─────────────────────────────────────────────────────────

WORK_CARNET = ["¿SE ENCUENTRA TRABAJANDO ACTUALMENTE?", "Estás trabajando actualmente?", "¿Contas con Carnet de Manipulación de Alimentos?",
               "Tenés Libreta Sanitaria? Vencimiento?"]


def test_work_carnet_and_libreta_labelled_answers_pass_with_source_truths():
    for text in WORK_CARNET:
        for label in ("answers", "cv"):
            entry, _ = go(closed(text), profile(TRUTHS), {"option": 1, "source": label})
            assert entry["source"] == "truths" and entry["option"] == 1, (text, label)


def test_a_yes_that_contradicts_a_truth_answers_with_the_truth():  # 085: ya no queda pendiente, contesta la verdad
    for text in ("¿Estás trabajando actualmente?", "¿Contas con Carnet de Manipulación de Alimentos?"):
        entry, _ = go(closed(text), profile(TRUTHS), {"option": 0, "source": "answers"})
        assert entry["source"] == "truths" and entry["option"] == 1 and entry["reason"] is None, text


def test_without_a_related_truth_it_is_still_without_source():
    entry, _ = go(closed("¿Sos monotributista?"), profile(TRUTHS), {"option": 1, "source": "cv"})
    assert entry["reason"] == "sin fuente"


def test_a_non_yes_no_answer_is_not_rescued_by_a_truth():
    entry, _ = go(closed("¿Cómo está tu carnet?", "Vigente", "Vencido"), profile(TRUTHS), {"option": 1, "source": "answers"})
    assert entry["source"] == "unknown" and entry["reason"] == "sin fuente"


# ── ruteo ────────────────────────────────────────────────────────────────────

ROUTE = "El ruteo incluye ir por CABA, La Plata, Quilmes y Lanús. ¿Tenés algún inconveniente?"


def test_the_route_question_with_total_availability_is_no_without_the_model():
    entry, client = go(closed(ROUTE), profile(answers={"availability": "Disponibilidad total", "modality": "presencial"}))
    assert entry["source"] == "answers" and entry["option"] == 1 and client.calls == []


def test_the_route_question_without_availability_is_pending():
    prof = profile()
    prof["answers"].pop("availability")
    entry, client = go(closed(ROUTE), prof)
    assert entry["source"] == "unknown" and client.calls == []


def test_the_prompt_is_a9_and_says_truths_beat_the_label():
    assert apply.ANSWER_PROMPT_VERSION == "a15" and "nunca «answers»" in apply.ANSWER_SYSTEM
