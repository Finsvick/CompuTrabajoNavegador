"""TAREA-078: «mínima de N años» y otras formas son un mínimo."""
import pytest

from compuapply import apply
from test_minimum_discard import go, preventista, profile

Q = "¿Tenés experiencia previa como Preventista, en eventos, ferias o atención al público? mínima de 1 año"


@pytest.mark.parametrize("text,months", [
    ("¿Tenés experiencia previa como promotor/a, en eventos, ferias o atención al público? mínima de 1 año", 12),
    ("¿Tenés experiencia mínima de 1 año?", 12), ("¿Tenés 1 año mínimo?", 12), ("¿Tenés no menos de un año?", 12),
    ("¿Tenés 2+ años?", 24), ("¿Tenés 2 años de experiencia mínima?", 24), ("¿Tenés mínimo 6 meses?", 6),
    ("¿Tenés mínima de dos años?", 24), ("¿Tenés experiencia?", None)])
def test_minimum_forms(text, months):
    assert apply.minimum_months(text) == months


def test_10_months_becomes_no_and_14_stays_yes():
    closed = {"text": Q, "kind": "closed", "options": ["Sí", "No"]}
    short = go(closed, profile(preventista("03/2023 - 12/2023")), {"option": 0, "source": "cv", "jobs": [0]})
    assert short["source"] == "cv" and short["option"] == 1
    enough = go(closed, profile(preventista("03/2023 - 04/2024")), {"option": 0, "source": "cv", "jobs": [0]})
    assert enough["option"] == 0


def test_open_with_10_months_is_not_a_yes():
    entry = go({"text": Q, "kind": "open", "options": []}, profile(preventista("03/2023 - 12/2023")),
               {"text": "Sí, tengo 10 meses como Vendedor Preventista en Norte S.A.", "source": "cv", "jobs": [0]})
    assert entry["source"] == "unknown" and entry["text"] is None
