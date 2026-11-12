"""TAREA-069: «al menos N años» no contradice la duración del CV y las opciones que descartan solas no se eligen."""
from datetime import date

import pytest

from test_apply_live import MODEL, FakeClient, make_profile, reply
from compuapply import apply

TODAY = date(2026, 11, 7)
TRUTHS = ["No tengo carnet de manipulación de alimentos ni libreta sanitaria, y nunca los tuve; estoy dispuesto a sacarlos a la brevedad",
          "Mi género es masculino"]


def preventista(period):
    return {"position": "Vendedor Preventista", "company": "Norte S.A.", "employment_period": period, "key_responsibilities": [],
            "skills_acquired": []}


SEVEN, FOURTEEN = preventista("03/2023 - 09/2023"), preventista("03/2023 - 04/2024")


def profile(job=SEVEN, truths=TRUTHS):
    prof = make_profile()
    prof["variants"][0] = {**prof["variants"][0], "truths": list(truths),
                           "resume": {**prof["variants"][0]["resume"], "experience_details": [job]}}
    return prof


def go(question, prof, *items):
    [entry] = apply.answer_questions([question], prof, FakeClient(reply(*items)), MODEL, "dev", [], today=TODAY)
    return entry


OPEN = {"text": "¿Contás con al menos 1 año de experiencia como Preventista? DETALLAR", "kind": "open", "options": []}
YES_NO = {"text": "¿Contás con al menos 1 año de experiencia como Preventista?", "kind": "closed", "options": ["Sí", "No"]}
SAID = "Sí, tengo 7 meses de experiencia como Vendedor Preventista en Norte S.A."
CARNET4 = {"text": "¿Contas con Carnet sanitario/curso de manipulación de alimentos al día?", "kind": "closed", "options": [
    "Si, lo tengo al día", "Si, pero se encuentra vencido", "No poseo libreta/carnet pero estoy dispuesto a realizarlo a la brevedad",
    "No poseo y no cuento con disponibilidad para realizarlo"]}
CARNET2 = {"text": "¿Contas con Carnet sanitario/ curso de manipulación de alimentos al día?", "kind": "closed", "options": [
    "Si, cuento con libreta o carnet VIGENTE", "No está vigente, debo actualizarla"]}


# ── al menos N ───────────────────────────────────────────────────────────────

def test_an_open_yes_with_7_months_is_pending():
    entry = go(OPEN, profile(), {"text": SAID, "source": "cv", "jobs": [0]})
    assert entry["source"] == "unknown" and entry["text"] is None and entry["reason"] == apply.SHORT_REASON


def test_the_job_is_found_by_the_question_when_the_model_gives_no_jobs():
    entry = go(OPEN, profile(), {"text": SAID, "source": "cv", "jobs": []})
    assert entry["source"] == "unknown" and entry["reason"] == apply.SHORT_REASON


def test_an_open_no_with_7_months_goes():
    entry = go(OPEN, profile(), {"text": "No, tengo 7 meses de experiencia como Vendedor Preventista en Norte S.A.", "source": "cv", "jobs": [0]})
    assert entry["source"] == "cv" and entry["text"].startswith("No")


def test_a_yes_no_closed_becomes_no_with_7_months():
    entry = go(YES_NO, profile(), {"option": 0, "source": "cv", "jobs": [0]})
    assert entry["source"] == "cv" and entry["option"] == 1


def test_with_14_months_yes_is_allowed():
    entry = go(YES_NO, profile(FOURTEEN), {"option": 0, "source": "cv", "jobs": [0]})
    assert entry["source"] == "cv" and entry["option"] == 0
    entry = go(OPEN, profile(FOURTEEN), {"text": "Sí, tengo 1 año y 2 meses como Vendedor Preventista en Norte S.A.", "source": "cv", "jobs": [0]})
    assert entry["source"] == "cv" and entry["text"].startswith("Sí")


def test_unreadable_dates_keep_the_old_behaviour():
    entry = go(YES_NO, profile(preventista("2024")), {"option": 0, "source": "cv", "jobs": [0]})
    assert entry["option"] == 0


def test_a_question_without_a_minimum_is_not_touched():
    entry = go({"text": "¿Tenés experiencia como Preventista?", "kind": "closed", "options": ["Sí", "No"]}, profile(), {"option": 0, "source": "cv", "jobs": [0]})
    assert entry["option"] == 0


@pytest.mark.parametrize("text,months", [
    ("¿al menos 1 año de experiencia?", 12), ("¿Tenés como mínimo 6 meses?", 6), ("¿Mínimo de 2 años en ventas?", 24),
    ("¿Más de 3 años de experiencia?", 36), ("¿Tenés 18 meses o más de experiencia?", 18), ("¿Tenés al menos dos años?", 24),
    ("¿Tenés experiencia?", None)])
def test_minimum_months(text, months):
    assert apply.minimum_months(text) == months


# ── opciones que descartan solas ─────────────────────────────────────────────

def test_the_four_option_carnet_picks_willing_to_do_it():
    entry = go(CARNET4, profile(), {"option": 3, "source": "truths"})
    assert entry["source"] == "truths" and entry["option"] == 2 and entry["reason"] is None


def test_the_two_option_carnet_stays_pending():
    entry = go(CARNET2, profile(), {"option": 1, "source": "truths"})
    assert entry["source"] == "unknown" and entry["option"] is None and entry["reason"] == apply.NO_OPTION_REASON


def test_a_discarding_option_is_fine_when_a_fact_says_it():
    prof = profile(truths=[*TRUTHS, "No cuento con disponibilidad para realizarlo"])
    entry = go(CARNET4, prof, {"option": 3, "source": "truths"})
    assert entry["option"] == 3


def test_a_declared_answer_also_supports_it():
    prof = profile()
    prof["answers"]["extra"] = "no estoy dispuesto a mudarme"
    question = {"text": "¿Te mudarías?", "kind": "closed", "options": ["Sí", "No estoy dispuesto a mudarme", "No, pero lo evalúo"]}
    assert go(question, prof, {"option": 1, "source": "answers"})["option"] == 1
    assert go(question, profile(), {"option": 1, "source": "truths"})["option"] is None  # TAREA-086: ninguna verdad lo dice


def test_a_good_option_is_not_touched():
    assert go(CARNET4, profile(), {"option": 2, "source": "truths"})["option"] == 2


def test_two_clean_options_of_the_same_polarity_are_pending():
    question = {"text": "¿Te interesa viajar?", "kind": "closed", "options": ["No me interesa", "No, no puedo", "No, por ahora no"]}
    entry = go(question, profile(), {"option": 0, "source": "answers"})
    assert entry["source"] == "unknown" and entry["reason"] == apply.NO_OPTION_REASON


def test_the_prompt_is_a7_and_has_both_rules():
    assert apply.ANSWER_PROMPT_VERSION == "a15"
    assert "tengo 7 meses" in apply.ANSWER_SYSTEM and "ningún hecho ni respuesta declarada" in apply.ANSWER_SYSTEM
