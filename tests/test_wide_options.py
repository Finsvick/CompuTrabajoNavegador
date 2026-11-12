"""TAREA-077: «Indistinta» y otras formas amplias son opciones amplias para una pregunta de horario con disponibilidad total."""
import pytest

from test_apply_live import MODEL, FakeClient, make_profile, reply
from compuapply import apply

QUESTION = "INDIQUE SU DISPONIBILIDAD HORARIA"


def ask(*options, availability="Disponibilidad total"):
    question = {"text": QUESTION, "kind": "closed", "options": list(options)}
    client = FakeClient(reply({"source": "unknown"}))
    [entry] = apply.answer_questions([question], make_profile({"availability": availability}), client, MODEL, "dev", [])
    return entry, client


@pytest.mark.parametrize("options,chosen", [
    (("MAÑANA", "TARDE", "INDISTINTA"), 2), (("Mañana", "Tarde", "Ambos turnos"), 2), (("Horario flexible", "Solo mañana"), 0),
    (("Mañana", "Tarde", "Indistinto"), 2), (("Mañana", "Tarde", "Indistintos"), 2), (("Mañana", "Todos los turnos"), 1),
    (("Mañana", "Tarde", "Todo el día"), 2), (("Mañana", "Tarde", "Ambas"), 2), (("Mañana o indistinto", "Tarde"), 0),
    (("Mañana", "Tarde", "Turno rotativo"), 2), (("Mañana", "Horario amplio"), 1), (("Mañana", "Cualquier horario"), 1),
])
def test_a_wide_option_is_chosen_with_total_availability(options, chosen):
    entry, client = ask(*options)
    assert entry["source"] == "answers" and entry["option"] == chosen and entry["reason"] is None and client.calls == []


def test_morning_or_afternoon_alone_stays_pending():
    entry, _ = ask("Mañana", "Tarde")
    assert entry["source"] == "unknown" and entry["reason"] == apply.SCHEDULE_NO_OPTION


def test_without_total_availability_the_wide_option_is_not_forced():
    entry, _ = ask("Mañana", "Tarde", "Indistinta", availability="Lunes a viernes")
    assert entry["source"] == "unknown" and entry["option"] is None  # la regla de código no actúa: lo decide el modelo (aquí, unknown)
