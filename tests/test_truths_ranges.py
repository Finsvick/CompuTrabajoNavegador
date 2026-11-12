"""TAREA-068: las preguntas usan el banco de verdades y el CV con rangos; interés con preámbulo, puesto y residencia."""
import json
from datetime import date

import pytest

from test_apply_live import MODEL, FakeClient, make_profile, reply
from compuapply import apply

TODAY = date(2026, 11, 7)
TRUTHS = ["Tengo el secundario completo (analítico)", "Tengo las vacunas del covid al día", "No tengo carnet de manipulación de alimentos"]
CASHIER = {"position": "Cajero", "company": "Super", "employment_period": "01/2026 - 10/2026", "key_responsibilities": [], "skills_acquired": []}


def profile(truths=(), answers=None, jobs=(CASHIER,)):
    prof = make_profile(answers)
    variant = {**prof["variants"][0], "resume": {**prof["variants"][0]["resume"], "experience_details": list(jobs)}}
    if truths:
        variant["truths"] = list(truths)
    prof["variants"][0] = variant
    return prof


def closed(text, *options):
    return {"text": text, "kind": "closed", "options": list(options)}


def go(question, prof, *items, offer=None):
    client = FakeClient(reply(*items)) if items else FakeClient(reply())
    [entry] = apply.answer_questions([question], prof, client, MODEL, "dev", [], today=TODAY, offer=offer)
    return entry, client


# ── banco de verdades ────────────────────────────────────────────────────────

def test_the_truths_go_to_the_model_as_facts_and_the_answer_has_source_truths():
    question = closed("¿Contas con analítico de Secundario?", "Sí", "No")
    entry, client = go(question, profile(TRUTHS), {"option": 0, "source": "truths"})
    assert entry["source"] == "truths" and entry["option"] == 0 and entry["reason"] is None
    sent = json.dumps(client.calls[0], ensure_ascii=False)
    assert "Hechos ciertos del candidato" in sent and "analítico" in sent and "truths" in sent


def test_without_truths_there_is_no_facts_section():
    _, client = go(closed("¿Tenés algo?", "Sí", "No"), profile(), {"source": "unknown"})
    assert "Hechos ciertos" not in json.dumps(client.calls[0], ensure_ascii=False)


def test_an_open_answer_from_a_truth_passes_the_control():
    question = {"text": "¿Tenés vacunas al día?", "kind": "open", "options": []}
    text = "Tengo las vacunas del covid al día."
    entry, _ = go(question, profile(TRUTHS), {"text": text, "source": "truths"})
    assert entry["source"] == "truths" and entry["text"] == text


def test_the_control_vocabulary_includes_the_truths_and_still_rejects_the_rest():
    resume = profile()["variants"][0]["resume"]
    text = "Tengo carnet de manipulación de alimentos."
    assert apply._open_text_problems(text, resume, {}, truths=tuple(TRUTHS)) == []
    assert apply._open_text_problems("Hablo mandarín con 7 años de práctica.", resume, {}, truths=tuple(TRUTHS))


def test_unknown_from_the_model_stays_pending_even_with_truths():
    entry, _ = go(closed("¿Tenés licencia de conducir?", "Sí", "No"), profile(TRUTHS), {"source": "unknown"})
    assert entry["source"] == "unknown"


def test_the_prompt_version_is_a7_and_mentions_truths_residence_and_years():
    assert apply.ANSWER_PROMPT_VERSION == "a15"
    assert "truths" in apply.ANSWER_SYSTEM and "reside" in apply.ANSWER_SYSTEM and "al menos N años" in apply.ANSWER_SYSTEM


# ── rangos ───────────────────────────────────────────────────────────────────

CASHIER_Q = closed("¿Contás con experiencia trabajando como cajero/a?", "Si, mayor a 1 año", "Si menos a 1 año", "No, no cuento con experiencia")


def test_cashier_with_10_months_picks_less_than_a_year():
    entry, _ = go(CASHIER_Q, profile(), {"option": 0, "source": "cv", "jobs": [0]})
    assert entry["source"] == "cv" and entry["option"] == 1 and entry["reason"] is None


def test_cashier_with_no_jobs_picks_no_experience():
    entry, _ = go(CASHIER_Q, profile(jobs=()), {"option": 0, "source": "cv", "jobs": []})
    assert entry["option"] == 2


@pytest.mark.parametrize("options,months,expected", [
    (("Mayor a 1 año", "Menos a 1 año"), 18, 0), (("Mayor a 1 año", "Menos a 1 año"), 6, 1),
    (("más de 2 años", "menos de 2 años"), 30, 0), (("Hasta 6 meses", "Más de 6 meses"), 6, 0),
    (("Entre 1 y 3 años", "Más de 3 años", "Menos de 1 año"), 24, 0), (("Entre 1 y 3 años", "Más de 3 años", "Menos de 1 año"), 40, 1),
    (("Menor a 1 año", "Mayor de 1 año"), 24, 1), (("Mayor a 1 año", "Menos a 1 año"), 12, None),
])
def test_range_words(options, months, expected):
    assert apply.experience_option(closed("¿Cuánta experiencia tenés?", *options), months) == expected


def test_yes_no_years_question_goes_to_the_model_with_the_cv():
    question = closed("¿Contás con al menos 1 año de experiencia en ventas en compañías de seguros?", "Si", "No")
    assert not apply.is_range_question(question) and apply.is_duration_question(question)
    entry, client = go(question, profile(), {"option": 1, "source": "cv"})
    assert entry["source"] == "cv" and entry["option"] == 1 and entry["reason"] is None and len(client.calls) == 1
    entry, _ = go(question, profile(), {"source": "unknown"})
    assert entry["source"] == "unknown"


# ── interés con preámbulo ────────────────────────────────────────────────────

PREAMBLE = "La posición es eventual desde el 12/11 al 30/11 inclusive. ¿Te interesa una contratación bajo estas condiciones?"


def test_interest_with_a_preamble_is_yes_without_the_model():
    entry, client = go(closed(PREAMBLE, "Sí", "No"), profile())
    assert entry["source"] == "answers" and entry["option"] == 0 and client.calls == []


def test_the_last_question_sentence():
    assert apply.last_question(PREAMBLE) == "¿Te interesa una contratación bajo estas condiciones?"
    assert apply.last_question("Sin pregunta") == "Sin pregunta"
    assert apply.last_question("¿Una? ¿Dos?") == "¿Dos?"


def test_a_preamble_that_mentions_interest_but_asks_something_else_is_not_interest():
    question = closed("Si te interesa, completá esto. ¿Tenés bicicleta?", "Sí", "No")
    assert apply.interest_answer(question, {}) is None


# ── puesto ───────────────────────────────────────────────────────────────────

POSITION_Q = closed("¿A qué puesto te postulas principalmente?", "Preparador/a de Comida Rápida", "Cajero/a")


def test_the_position_is_the_option_equal_to_the_search():
    entry, client = go(POSITION_Q, profile(), offer={"title": "Operario de local", "keyword": "cajero"})
    assert entry["source"] == "answers" and entry["option"] == 1 and client.calls == []


def test_the_position_falls_back_to_the_title_of_the_offer():
    entry, _ = go(POSITION_Q, profile(), offer={"title": "Cajera en local de comidas", "keyword": "mozo"})
    assert entry["source"] == "unknown"  # «Cajera» no es «Cajero/a»: no se inventa
    entry, _ = go(POSITION_Q, profile(), offer={"title": "Cajero/a para sucursal", "keyword": "vendedor"})
    assert entry["option"] == 1


def test_no_match_or_unknown_offer_is_pending():
    entry, client = go(POSITION_Q, profile(), offer={"title": "Operario", "keyword": "vendedor"})
    assert entry["source"] == "unknown" and entry["reason"] == apply.POSITION_REASON and client.calls == []
    entry, _ = go(POSITION_Q, profile(), {"source": "unknown"})  # sin oferta conocida va al modelo
    assert entry["source"] == "unknown"


def test_both_options_matching_the_title_is_pending():
    entry, _ = go(POSITION_Q, profile(), offer={"title": "Cajero y preparador de comida rápida", "keyword": "x"})
    assert entry["source"] == "unknown"


# ── residencia sí/no ─────────────────────────────────────────────────────────

def test_residence_is_answered_by_the_code_with_location():  # TAREA-079: ya no lo decide el modelo
    prof = profile(answers={"location": "Pilar, zona norte"})
    entry, client = go(closed("¿Reside en Canning?", "Sí", "No"), prof)
    assert entry["source"] == "answers" and entry["option"] == 1 and not client.calls
    entry, client = go(closed("¿Vivis en zona norte o aledaños?", "Sí", "No"), prof)
    assert entry["source"] == "answers" and entry["option"] == 0 and not client.calls


def test_residence_with_other_options_goes_to_the_model_with_location_as_a_fact():
    prof = profile(answers={"location": "Pilar, zona norte"})
    entry, client = go(closed("¿Reside en Canning?", "Canning", "Otra zona"), prof, {"option": 1, "source": "answers"})
    assert entry["source"] == "answers" and entry["option"] == 1 and len(client.calls) == 1
    assert "location: Pilar, zona norte" in json.dumps(client.calls[0], ensure_ascii=False)


# ── los tests no escriben en el data/runs del repo ───────────────────────────

def test_runs_go_to_a_temporary_folder_and_a_write_into_repo_data_fails(tmp_path):
    from conftest import REPO
    assert apply.RUNS_DIR.resolve() != (REPO / "data" / "runs").resolve() and REPO not in apply.RUNS_DIR.resolve().parents
    with pytest.raises(AssertionError):
        apply.save_screen(REPO / "data" / "runs", "run", "ID", RuntimeError("x"), [])
    assert apply.save_screen(tmp_path, "run", "ID", RuntimeError("x"), []).parent == tmp_path
