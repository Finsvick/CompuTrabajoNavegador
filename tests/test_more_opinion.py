"""TAREA-083: más preguntas de opinión, disposición, verdades con «sin» y muletillas."""
import pytest

from test_apply_live import MODEL, FakeClient, make_profile, reply
from test_human_answers import TODAY, prof
from compuapply import apply

ARQ = ["Contanos una situación frustrante y cómo avanzaste", "Contanos una situación de éxito y cómo lo lograste",
       "¿Qué expectativas tenés sobre el puesto?", "¿Cómo te ves dentro de 2, 5 y 10 años?", "¿Cómo te describís?",
       "¿Cómo creés que te ve la gente?", "¿Por qué creés que te deberíamos contratar?"]


def q(text, kind="open", *options):
    return {"text": text, "kind": kind, "options": list(options)}


def go(question, *replies, profile=None, offer=None):
    client = FakeClient(*replies)
    [entry] = apply.answer_questions([question], profile or prof(), client, MODEL, "dev", [], today=TODAY, offer=offer)
    return entry, client


@pytest.mark.parametrize("text", ARQ)
def test_the_arq_questions_are_opinion(text):
    assert apply.is_opinion(q(text))


@pytest.mark.parametrize("text", ARQ)
def test_an_arq_opinion_is_not_pending_with_a_fake_model(text):
    entry, _ = go(q(text), reply({"text": "Atendí clientes en un kiosco y aprendí a escuchar antes de ofrecer.", "source": "opinión"}))
    assert entry["source"] == "opinión" and entry["reason"] is None


@pytest.mark.parametrize("text", ["¿Cuáles son tus fortalezas?", "¿Cuáles son tus debilidades?", "¿Qué te gusta de vender?", "¿Qué sabés de la empresa?",
                                  "Contanos un logro", "¿Cómo te describirían tus compañeros?"])
def test_other_opinion_phrases(text):
    assert apply.is_opinion(q(text))


def test_a_situation_that_invents_a_figure_does_not_pass():
    bad = "En mi puesto anterior aumenté las ventas un 30% con una promoción."
    entry, _ = go(q(ARQ[1]), reply({"text": bad, "source": "opinión"}))
    assert entry["source"] == "unknown" and entry["text"] is None


def test_the_prompt_asks_for_situations_from_the_cv():
    assert "Una situación sale de un puesto del CV" in apply.ANSWER_SYSTEM and apply.ANSWER_PROMPT_VERSION == "a15"


# ── disposición ──────────────────────────────────────────────────────────────

def test_willing_for_street_work_is_yes_without_the_model():
    entry, client = go(q("¿Estás dispuesto/a a trabajar en vía pública?"))
    assert entry["source"] == "answers" and entry["text"].startswith("Sí") and client.calls == []
    entry, client = go(q("¿Estás dispuesto/a a trabajar en vía pública?", "closed", "Sí", "No"))
    assert entry["option"] == 0 and client.calls == []
    entry, _ = go(q("¿Te animás a trabajar en la calle?", "closed", "No", "Sí"))
    assert entry["option"] == 1


def test_willing_about_schedule_or_weekends_is_left_to_availability():
    assert apply.willingness_answer(q("¿Estás dispuesto/a a trabajar fines de semana en vía pública?"), {}) is None
    assert apply.willingness_answer(q("¿Estás dispuesto/a a viajar?"), {}) is None


# ── verdades con «sin» ───────────────────────────────────────────────────────

def test_a_truth_does_not_apply_when_the_question_says_sin_that_word():
    question = q("¿Tenés experiencia saliendo a buscar clientes desde cero, sin cartera?", "closed", "Sí", "No")
    entry = {"option": 0, "kind": "closed", "options": ["Sí", "No"], "text": None, "source": "cv"}
    assert apply._truth_source(entry, question, ("No tengo cartera de clientes actual",)) is None
    other = q("¿Tenés cartera de clientes?", "closed", "Sí", "No")
    assert apply._truth_source(entry, other, ("No tengo cartera de clientes actual",)) == "contradice"


# ── muletillas ───────────────────────────────────────────────────────────────

def test_an_answer_that_repeats_the_question_keeps_only_the_yes():
    text = "Sí, tengo experiencia en caja. Trabajé como cajero en un supermercado."
    assert apply._humanize(text, {}, "¿Tenés experiencia en caja?") == "Sí. Trabajé como cajero en un supermercado."
    assert apply._humanize("Sí, trabajé en caja dos años.", {}, "¿Tenés experiencia en caja?") == "Sí, trabajé en caja dos años."


def test_a_residence_answer_is_only_the_declared_place():
    text = "Resido en Quilmes. Es un lugar accesible para trabajar."
    assert apply._humanize(text, {"location": "Quilmes"}, "¿Dónde residís?") == "Resido en Quilmes."


@pytest.mark.parametrize("text", ["En ambos roles, desarrollé habilidades en atención.", "Estoy listo para comenzar.", 
                                  "Es un lugar accesible.", "He trabajado en varios puestos.", "Mi experiencia se ha centrado en ventas.", "Mi experiencia se centra en ventas."])
def test_new_ai_phrases(text):
    assert apply.ai_hits(text)


def test_a_closing_ready_to_start_sentence_is_dropped():
    assert apply._humanize("Atendí clientes en un kiosco. Estoy listo para comenzar ahora mismo.", {}) == "Atendí clientes en un kiosco."
