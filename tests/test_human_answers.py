"""TAREA-082: estilo de las abiertas, preguntas de opinión."""
from datetime import date

import pytest

from test_apply_live import MODEL, FakeClient, make_profile, reply
from compuapply import apply

TODAY = date(2026, 11, 12)
OPEN = {"text": "¿Contanos tu experiencia en atención al público?", "kind": "open", "options": []}
GOOD = "Atendí clientes en un kiosco durante dos años."
TEMPLATE = "Mi enfoque siempre ha sido brindar un excelente servicio."


def prof():
    p = make_profile()
    p["answers"] = {**p.get("answers", {}), "location": "Quilmes"}
    return p


def go(question, *replies, offer=None):
    client = FakeClient(*replies)
    [entry] = apply.answer_questions([question], prof(), client, MODEL, "dev", [], today=TODAY, offer=offer)
    return entry, client


def cv_text():
    return GOOD


def test_a_template_last_sentence_is_dropped_after_one_retry():
    again = reply({"text": f"{GOOD} {TEMPLATE}", "source": "cv"})
    entry, client = go(OPEN, reply({"text": f"{GOOD} {TEMPLATE}", "source": "cv"}), again)
    assert len(client.calls) == 2 and "excelente servicio" in str(client.calls[1])  # se pidió de nuevo diciendo cuál
    assert entry["source"] == "cv" and entry["text"] == GOOD


def test_a_template_only_answer_is_pending_after_the_retry():
    entry, client = go(OPEN, reply({"text": TEMPLATE, "source": "cv"}))
    assert len(client.calls) == 2
    assert entry["source"] == "unknown" and entry["reason"] == "suena a plantilla"


def test_a_clean_retry_replaces_the_template():
    entry, _ = go(OPEN, reply({"text": TEMPLATE, "source": "cv"}), reply({"text": GOOD, "source": "cv"}))
    assert entry["text"] == GOOD


def test_the_neighbourhood_sentence_is_dropped():
    text = f"{GOOD} Es un barrio tranquilo y accesible, donde tengo fácil acceso a transporte público."
    entry, client = go(OPEN, reply({"text": text, "source": "cv"}))
    assert len(client.calls) == 1 and entry["text"] == GOOD
    assert apply._humanize("Vivo en Quilmes, un barrio tranquilo.", {"location": "Quilmes"}) == "Vivo en Quilmes, un barrio tranquilo."


@pytest.mark.parametrize("text", ["Mi enfoque siempre ha sido x.", "Además, trabajé bien.", "Cabe destacar que sí.", "Es mi valor agregado."])
def test_ai_phrases_are_found(text):
    assert apply.ai_hits(text)


def test_plain_text_has_no_hits():
    assert apply.ai_hits(GOOD) == []


# ── opinión ──────────────────────────────────────────────────────────────────

WHY = {"text": "¿Por qué te gustaría ser parte de RE/MAX Vincit?", "kind": "open", "options": []}
OFFER = {"title": "Asesor Inmobiliario", "keyword": "x", "company": "RE/MAX Vincit"}


def test_an_unknown_opinion_is_asked_again_as_an_opinion_and_is_not_pending():
    entry, client = go(WHY, reply({"source": "unknown"}),
                       reply({"text": "Quiero sumarme a RE/MAX Vincit porque me gusta vender y atender clientes.", "source": "opinión"}), offer=OFFER)
    assert len(client.calls) == 2 and "opinión" in str(client.calls[1])
    assert entry["source"] == "opinión" and entry["reason"] is None and "RE/MAX Vincit" in entry["text"]


def test_an_opinion_that_names_a_company_not_in_the_cv_or_the_offer_does_not_pass():
    bad = "Quiero sumarme porque trabajé en Galaxia Corporation y me fue muy bien."
    entry, _ = go(WHY, reply({"text": bad, "source": "opinión"}), offer=OFFER)
    assert entry["source"] != "opinión" and "Galaxia" not in (entry["text"] or "")


def test_the_prompt_has_the_style_rules_and_the_version_moved():
    assert apply.ANSWER_PROMPT_VERSION == "a15"
    assert "pretérito simple" in apply.ANSWER_SYSTEM and "opinión" in apply.ANSWER_SYSTEM
