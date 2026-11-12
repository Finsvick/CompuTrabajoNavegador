"""TAREA-073: «responsable de…» y «estudiaste comunicación» no son preguntas de actitud."""
import pytest

from test_apply_live import MODEL, FakeClient, make_profile, reply
from compuapply import apply


@pytest.mark.parametrize("text", ["¿Sos una persona responsable?", "¿Sos responsable?", "¿Tenés buena comunicación?", "¿Sos una persona proactiva?"])
def test_these_are_attitude(text):
    assert apply.is_attitude(text) is True


@pytest.mark.parametrize("text", [
    "¿Sos responsable de caja?", "¿Fuiste responsable del depósito?", "¿Sos responsable en el área de ventas?", "¿Fuiste responsable por un equipo?",
    "¿Estuviste a cargo? ¿Fuiste responsable a cargo de un local?", "¿Estudiaste comunicación?", "¿Tenés experiencia en comunicación institucional?"])
def test_these_are_not_attitude(text):
    assert apply.is_attitude(text) is False


def test_a_yes_to_responsible_of_does_not_pass_as_actitud():
    question = {"text": "¿Sos responsable de caja?", "kind": "closed", "options": ["SI", "NO"]}
    [entry] = apply.answer_questions([question], make_profile(), FakeClient(reply({"option": 0, "source": "answers"})), MODEL, "dev", [])
    assert entry["source"] == "unknown" and entry["reason"] == "sin fuente"
    question["text"] = "¿Sos una persona responsable?"
    [entry] = apply.answer_questions([question], make_profile(), FakeClient(reply({"option": 0, "source": "answers"})), MODEL, "dev", [])
    assert entry["source"] == "actitud" and entry["option"] == 0
