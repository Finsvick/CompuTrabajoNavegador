"""TAREA-084: reglas que no se llevan preguntas ajenas; residencia, actitud y lista de experiencia por código."""
import pytest

from test_apply_live import MODEL, FakeClient, make_profile, reply
from test_human_answers import TODAY
from test_residence_experience import closed_q, open_q, profile
from compuapply import apply

FIXED = "availability + modality"


def run(question, prof, *items):
    client = FakeClient(reply(*items)) if items else FakeClient(reply({"source": "unknown"}))
    [entry] = apply.answer_questions([question], prof, client, MODEL, "dev", [], today=TODAY)
    return entry, client


@pytest.mark.parametrize("text", ["¿Cuál es tu experiencia en atención al cliente presencial, virtual y telefónico?",
                                  "¿Contás con el Curso de Manipulación de Alimentos vigente o tenés disponibilidad para tramitarlo?"])
def test_availability_does_not_take_experience_or_course_questions(text):
    prof = make_profile({"availability": "Disponibilidad total", "modality": "presencial"})
    assert apply.availability_answer(open_q(text), prof["answers"]) is None
    assert apply.availability_answer(closed_q(text), prof["answers"]) is None


def test_immediate_availability_gets_the_availability_text():
    prof = make_profile({"availability": "Disponibilidad total e inmediata", "modality": "presencial"})
    entry, client = run(open_q("¿Posees disponibilidad inmediata para un ingreso?"), prof)
    assert entry["source"] == "answers" and entry["text"].startswith("Disponibilidad total") and entry["fixed"] == FIXED and client.calls == []
    entry, _ = run(closed_q("¿Posees disponibilidad inmediata para un ingreso?"), prof)
    assert entry["option"] == 0


# ── lista en cerradas ────────────────────────────────────────────────────────

LIST_Q = closed_q("¿Contás con experiencia en Atención al Cliente / Call Center o Supermercados?")


def test_a_list_option_in_the_cv_is_yes_even_if_the_model_says_no():
    entry, _ = run(LIST_Q, profile(position="Atención al cliente"), {"option": 1, "source": "cv"})
    assert entry["option"] == 0 and entry["source"] == "cv"


def test_a_list_without_any_option_in_the_cv_does_not_change():
    entry, _ = run(LIST_Q, profile(position="Vendedor Preventista"), {"option": 1, "source": "cv"})
    assert entry["option"] == 1


# ── actitud cerrada ──────────────────────────────────────────────────────────

def test_te_motiva_is_yes_and_investing_is_not():
    q = {"text": "¿Te motiva trabajar en un entorno donde los resultados dependen de tu iniciativa y constancia?", "kind": "closed", "options": ["SI", "NO"]}
    entry, client = run(q, make_profile())
    assert entry["option"] == 0 and entry["source"] == "answers" and client.calls == []
    invest = {"text": "¿Tenés posibilidad de invertir dinero en el negocio?", "kind": "closed", "options": ["Sí", "No"]}
    assert apply.attitude_yes_answer(invest) is None and apply.interest_answer(invest, {}) is None
    assert apply.interest_answer({**invest, "text": "¿Te interesa invertir dinero?"}, {}) is None
    assert apply.attitude_yes_answer({**closed_q("¿Te gusta trabajar de noche?"), "text": "¿Te gusta el horario rotativo?"}) is None


# ── residencia ───────────────────────────────────────────────────────────────

def test_the_zone_question_is_answered_by_code():
    entry, client = run(open_q("¿En que zona estas residiendo actualmente?"), profile())
    assert entry["source"] == "answers" and entry["text"] == "Vivo en Quilmes." and client.calls == []


def test_a_residence_question_with_other_places_is_no():
    entry, client = run(open_q("¿Residís en Tigre, Nordelta, alrededores? ¿Dónde?"), profile())
    assert entry["text"] == "No, vivo en Quilmes." and client.calls == []
    entry, _ = run(open_q("¿Residís en Quilmes o alrededores? ¿Dónde?"), profile())
    assert entry["text"].startswith("Sí, vivo en Quilmes")


# ── interés inventado y muletillas ───────────────────────────────────────────

def test_an_invented_interest_sentence_is_dropped_or_pending():
    text = "No tengo experiencia en el rubro, pero tengo un interés en librerías y literatura."
    entry, _ = run(open_q("¿Contanos tu experiencia en el rubro?"), profile(), {"text": text, "source": "cv"})
    assert entry["source"] == "unknown" or "librer" not in entry["text"]
    entry, _ = run(open_q("¿Contanos tu experiencia en el rubro?"), profile(),
                   {"text": "Trabajé de vendedor en un kiosco. Tengo interés en marketing y construcción de marca.", "source": "cv"})
    assert entry["text"] == "Trabajé de vendedor en un kiosco."
    ok = apply._drop_invented_interest("Me gusta la venta.", {"summary": "venta"}, None, ())
    assert ok == "Me gusta la venta."


def test_ahora_mismo_alone_is_not_a_template_phrase():
    assert apply.ai_hits("Puedo empezar ahora mismo.") == []
    assert apply.ai_hits("Estoy listo para comenzar.")
