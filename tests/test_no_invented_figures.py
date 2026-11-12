"""Tests de TAREA-044: una respuesta cerrada con cifras (o de duración de experiencia) solo sale de lo que declaró el usuario. Cliente falso."""
import pytest

from fakesite import KQ_URL, FakePage, fixture, standard_site
from test_apply_live import MODEL, SALARY, TODAY, FakeClient, add_offer, conn, make_profile, reply, rows, status_of, stub_cv, write_profile_with  # noqa: F401
from compuapply import apply, db, profile, ui_logic

SALES = "¿Tienes experiencia previa trabajando en el área de ventas o comercial?"
SALES_OPTIONS = ["Sí, he trabajado más de 2 años en ventas", "Sí, he trabajado menos de 2 años en ventas", "No tengo experiencia en ventas"]
PAY = "¿Cuál es tu remuneración pretendida?"
PAY_OPTIONS = ["Hasta $500.000", "Entre $500.000 y $800.000", "Más de $800.000"]
YEARS = "¿Cuántos años de experiencia tenés?"
YEARS_OPTIONS = ["No tengo experiencia", "Menos de 1 año", "Entre 1 y 3 años"]
SCHOOL = "¿Tenés estudios en Computer Engineering?"


def closed(text, options):
    return {"text": text, "kind": "closed", "options": options}


def ask(question, option, source, answers=None):
    """Lo que `answer_questions` hace con una cerrada cuando el modelo elige `option` diciendo que sale de `source`."""
    client = FakeClient(reply({"option": option, "source": source}))
    [entry] = apply.answer_questions([question], make_profile(answers), client, MODEL, "dev", [])
    return entry


# ── el caso real ─────────────────────────────────────────────────────────────

def test_the_real_case_more_than_2_years_in_sales_from_the_cv_is_unknown():
    entry = ask(closed(SALES, SALES_OPTIONS), 0, "cv")
    assert entry["source"] == "unknown" and entry["option"] is None


def test_the_same_question_with_the_users_declared_answer_and_source_answers_is_accepted():
    declared = {profile.normalize_question(SALES): "Sí, he trabajado menos de 2 años en ventas"}
    entry = ask(closed(SALES, SALES_OPTIONS), 1, "answers", declared)
    assert entry["source"] == "answers" and entry["option"] == 1


def test_the_declared_answer_is_not_enough_if_the_model_says_the_cv_was_the_source():
    declared = {profile.normalize_question(SALES): "Trabajé poco en ventas, menos de dos años"}  # no es igual a ninguna opción
    assert ask(closed(SALES, SALES_OPTIONS), 1, "cv", declared)["source"] == "unknown"


def test_source_answers_without_a_declared_answer_for_that_question_is_unknown():
    assert ask(closed(SALES, SALES_OPTIONS), 0, "answers", {"salary": SALARY, "availability": "Lunes a viernes"})["source"] == "unknown"
    assert ask(closed(SALES, SALES_OPTIONS), 0, "answers", {profile.normalize_question("¿Otra pregunta?"): "x"})["source"] == "unknown"


def test_the_real_case_makes_the_offer_pending_and_the_question_appears_in_preguntas(conn, tmp_path):
    id16 = add_offer(conn)
    page = FakePage(standard_site(pages={KQ_URL: fixture("apply_kq_sales.html")}))
    result = apply.run_live(conn, make_profile({"salary": SALARY}), MODEL, FakeClient(reply({"option": 0, "source": "cv"})), page, TODAY,
                            stop_file=tmp_path / "STOP", pause=lambda: None, cv_provider=stub_cv())
    assert result["sent"] == [] and page.site.submitted == [] and status_of(conn, id16) == "pending_question"
    assert [i["reason"] for i in result["skipped"]] == [apply.PENDING_QUESTION]
    [item] = ui_logic.pending_questions(conn)
    assert item["question"] == SALES and item["key"] == profile.normalize_question(SALES) and item["offers"] == 1 and item["options"] == SALES_OPTIONS


def test_with_the_users_answer_saved_the_next_run_sends_it(conn, tmp_path):
    id16 = add_offer(conn)
    page = FakePage(standard_site(pages={KQ_URL: fixture("apply_kq_sales.html")}))
    kwargs = dict(stop_file=tmp_path / "STOP", pause=lambda: None, cv_provider=stub_cv())
    apply.run_live(conn, make_profile(), MODEL, FakeClient(reply({"option": 0, "source": "cv"})), page, TODAY, **kwargs)
    path = write_profile_with(tmp_path)
    key = ui_logic.pending_questions(conn)[0]["key"]
    assert ui_logic.answer_question(path, tmp_path / "t.db", key, "Sí, he trabajado menos de 2 años en ventas") == 1
    loaded = {**make_profile(), "answers": profile.load_profile(path)["answers"]}
    result = apply.run_live(conn, loaded, MODEL, FakeClient(reply({"option": 1, "source": "answers"})), FakePage(page.site), TODAY, **kwargs)
    assert [i["id16"] for i in result["sent"]] == [id16] and status_of(conn, id16) == "applied"
    assert page.site.submitted[0]["checked"] == {"KillerQuestions[0].ClosedQuestion": "2"}


def test_one_answer_of_the_user_serves_every_offer_that_asks_the_same_question(conn, tmp_path):
    first, second = add_offer(conn, 1), add_offer(conn, 2)
    for id16 in (first, second):
        db.save_pending_question(conn, id16, SALES, SALES_OPTIONS)
        db.mark_status(conn, id16, "pending_question")
    path = write_profile_with(tmp_path)
    [item] = ui_logic.pending_questions(conn)
    assert item["offers"] == 2
    assert ui_logic.answer_question(path, tmp_path / "t.db", item["key"], SALES_OPTIONS[1]) == 2
    assert status_of(conn, first) == "new" and status_of(conn, second) == "new" and ui_logic.pending_questions(conn) == []
    declared = profile.load_profile(path)["answers"]
    for other_offer_question in (SALES, "  ¿TIENES experiencia previa trabajando en el AREA de ventas o comercial?  "):  # la misma pregunta, escrita distinto
        entry = apply.answer_questions([closed(other_offer_question, SALES_OPTIONS)], {**make_profile(), "answers": declared},
                                       FakeClient(reply({"option": 1, "source": "answers"})), MODEL, "dev", [])[0]
        assert entry["source"] == "answers" and entry["option"] == 1


# ── sueldo ───────────────────────────────────────────────────────────────────

def test_a_pay_question_with_amount_options_is_accepted_with_answers_when_salary_is_loaded():
    entry = ask(closed(PAY, PAY_OPTIONS), 1, "answers", {"salary": SALARY})
    assert entry["source"] == "answers" and entry["option"] == 1


def test_a_pay_question_with_amount_options_without_salary_is_unknown():
    assert ask(closed(PAY, PAY_OPTIONS), 1, "answers")["source"] == "unknown"


def test_a_pay_question_with_amounts_chosen_from_the_cv_is_unknown_even_with_salary():
    assert ask(closed(PAY, PAY_OPTIONS), 1, "cv", {"salary": SALARY})["source"] == "unknown"


@pytest.mark.parametrize("text", ["¿Cuál es tu sueldo pretendido?", "¿Qué salario esperás?", "Remuneración deseada"])
def test_the_words_that_make_a_question_about_pay(text):
    assert apply.declared_for(closed(text, PAY_OPTIONS), {"salary": SALARY}) is True
    assert apply.declared_for(closed(text, PAY_OPTIONS), {}) is False


def test_salary_does_not_declare_a_question_that_is_not_about_pay():
    assert apply.declared_for(closed(SALES, SALES_OPTIONS), {"salary": SALARY}) is False


def test_a_pay_question_without_figures_in_its_options_is_accepted_as_before():
    entry = ask(closed(PAY, ["A convenir", "Según convenio"]), 0, "answers", {"salary": SALARY})
    assert entry["source"] == "answers" and entry["option"] == 0


# ── duración de experiencia ──────────────────────────────────────────────────

def test_no_experience_chosen_from_the_cv_in_a_years_question_is_unknown():
    entry = ask(closed(YEARS, YEARS_OPTIONS), 0, "cv")  # «No tengo experiencia» no trae cifra, pero la pregunta es de duración
    assert entry["source"] == "unknown" and entry["option"] is None


def test_no_experience_with_the_declared_answer_and_source_answers_is_accepted():
    entry = ask(closed(YEARS, YEARS_OPTIONS), 0, "answers", {profile.normalize_question(YEARS): "No tengo experiencia"})
    assert entry["source"] == "answers" and entry["option"] == 0


@pytest.mark.parametrize("question, options", [
    ("¿Tenés experiencia?", ["Menos de 1 año", "Más de 1 año", "Ninguna"]),                    # los años solo en las opciones
    ("¿Cuántos meses de experiencia tenés en soporte?", ["Ninguno", "Algunos", "Varios"]),     # los meses solo en la pregunta
    ("Experiencia en ventas", ["Sin experiencia", "Hasta 6 meses", "Más de 6 meses"]),
])
def test_what_makes_a_question_about_the_duration_of_experience(question, options):
    assert apply.is_duration_question(closed(question, options)) is True


@pytest.mark.parametrize("question, options", [
    (SCHOOL, ["Sí", "No"]),
    ("¿Cuántos años tenés?", ["Menos de 30", "Más de 30"]),                                    # años, pero no de experiencia
    ("¿Tenés experiencia en ventas?", ["Sí", "No"]),                                           # experiencia, pero sin años ni meses
    ("¿Podés empezar este mes?", ["Sí", "No"]),
    ("¿Tenés una mesa de trabajo en casa?", ["Sí", "No"]),                                     # «mes» dentro de otra palabra
])
def test_what_is_not_a_duration_question(question, options):
    assert apply.is_duration_question(closed(question, options)) is False


# ── cifras ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", ["Más de 2 años", "10", "$500.000", "Dos años", "UNA vez", "Un año", "Tres semanas", "Diez meses", "Más de un año",
                                  "Año y medio", "Media hora", "Doce horas", "Quince días", "Veinte pesos", "Treinta hs", "Cien mil", "Mil pesos", "Un millón",
                                  "Dos veces", "Más de dos años"])
def test_figures_in_digits_and_in_words(text):
    assert apply.has_figure(text) is True


@pytest.mark.parametrize("text", ["Sí", "No", "Presencial", "Ninguna", "Tengo experiencia", "A convenir", "Universidad completa", "Ninguno", "Mañana", "Tarde",
                                  "Sí, tengo un título", "Tengo una carrera terminada", "Uno de los dos", "Media jornada", "Tres", "Doce"])
def test_text_without_figures(text):
    assert apply.has_figure(text) is False


# ── lo que sigue como hoy ────────────────────────────────────────────────────

def test_a_closed_question_without_figures_or_duration_is_accepted_from_the_cv_as_before():
    entry = ask(closed(SCHOOL, ["Sí", "No"]), 0, "cv")
    assert entry["source"] == "cv" and entry["option"] == 0


def test_a_closed_question_without_figures_or_duration_is_accepted_from_answers_too():
    assert ask(closed("¿Tenés disponibilidad presencial?", ["Sí", "No"]), 0, "answers")["source"] == "answers"


def test_an_unknown_choice_stays_unknown_and_an_invalid_option_is_unknown():
    assert ask(closed(SCHOOL, ["Sí", "No"]), -1, "unknown")["source"] == "unknown"
    assert ask(closed(SCHOOL, ["Sí", "No"]), 7, "cv")["source"] == "unknown"


def test_the_prompt_tells_the_model_the_rule():
    system = apply.ANSWER_SYSTEM
    assert "cifras (dígitos, o un número en palabras junto a una unidad" in system and "cuánta experiencia (años o meses)" in system
    assert "solo se elige con `source` «answers»" in system and "nunca la elijas por lo que dice el CV" in system


# ── TAREA-045: menos falsas alarmas y la opción declarada manda ─────────────

TURN = "Si pudieras elegir tu jornada laboral de 4 hs cual seria"
TURN_OPTIONS = ["Mañana de 9 a 13", "Tarde de 13 a 17"]


def test_a_number_word_is_a_figure_only_with_a_unit_in_the_same_option():
    assert ask(closed(SCHOOL, ["Sí, tengo un título", "No"]), 0, "cv")["source"] == "cv"
    for option in ("Más de un año", "Año y medio"):
        assert ask(closed("¿Cuánto tiempo trabajaste en ventas?", [option, "No"]), 0, "cv")["source"] == "unknown"


@pytest.mark.parametrize("text", ["Mañana de 9 a 13", "de 9 a 13 hs", "9-13", "09:00 a 13:00", "Tarde 13 a 17 hs"])
def test_time_slots_are_not_figures_for_a_shift(text):
    assert apply.only_time_slots(text) is True


@pytest.mark.parametrize("text", ["Sí, 4 hs", "4 hs", "Mañana", "Más de 2 años", "9 a 13 años", "Hasta $500"])
def test_what_is_not_only_time_slots(text):
    assert apply.only_time_slots(text) is False


def test_a_shift_with_availability_and_time_slots_is_accepted_without_a_declared_answer():
    entry = ask(closed(TURN, TURN_OPTIONS), 0, "answers", {"availability": "Mañana/Tarde"})
    assert entry["source"] == "answers" and entry["option"] == 0


def test_a_shift_without_availability_is_unknown():
    assert ask(closed(TURN, TURN_OPTIONS), 0, "answers", {"availability": None})["source"] == "unknown"


def test_a_shift_chosen_from_the_cv_is_unknown():
    assert ask(closed(TURN, TURN_OPTIONS), 0, "cv", {"availability": "Mañana/Tarde"})["source"] == "unknown"


def test_a_lone_hour_count_in_a_shift_question_is_unknown():
    assert ask(closed(TURN, ["Sí, 4 hs", "No"]), 0, "answers", {"availability": "Mañana/Tarde"})["source"] == "unknown"


def test_a_shift_slot_in_an_experience_duration_question_is_not_accepted():
    question = closed("Horario y años de experiencia", ["Mañana de 9 a 13", "Tarde de 13 a 17"])
    assert ask(question, 0, "answers", {"availability": "Mañana/Tarde"})["source"] == "unknown"


def test_a_question_that_is_not_about_shifts_does_not_get_the_slot_exception():
    assert ask(closed("¿Qué día podés venir?", ["Lunes de 9 a 13", "Martes de 9 a 13"]), 0, "answers", {"availability": "Lunes"})["source"] == "unknown"


def test_the_declared_option_wins_over_the_models_choice():
    declared = {profile.normalize_question(SALES): "  SÍ, he trabajado menos de 2 años en ventas. "}
    entry = ask(closed(SALES, SALES_OPTIONS), 0, "answers", declared)
    assert entry["source"] == "answers" and entry["option"] == 1


def test_the_declared_option_wins_even_if_the_model_said_cv():
    declared = {profile.normalize_question(SALES): "No tengo experiencia en ventas"}
    entry = ask(closed(SALES, SALES_OPTIONS), 0, "cv", declared)
    assert entry["source"] == "answers" and entry["option"] == 2


def test_a_declared_answer_that_matches_no_option_is_treated_as_before():
    declared = {profile.normalize_question(SALES): "Un poco"}
    assert ask(closed(SALES, SALES_OPTIONS), 0, "answers", declared)["option"] == 0
    assert ask(closed(SALES, SALES_OPTIONS), 0, "cv", declared)["source"] == "unknown"


def test_an_answer_saved_with_the_text_of_an_option_is_kept_as_is(tmp_path):
    path = write_profile_with(tmp_path)
    assert ui_logic.answer_question(path, tmp_path / "t.db", profile.normalize_question(SALES), SALES_OPTIONS[1]) == 0
    assert profile.load_profile(path)["answers"][profile.normalize_question(SALES)] == SALES_OPTIONS[1]


# ── TAREA-046: la respuesta declarada se usa aunque el modelo no elija ──────

DECLARED_LESS = {profile.normalize_question(SALES): "Sí, menos de 1 año"}
LESS_OPTIONS = ["Sí, más de 2 años", "Sí, menos de 1 año", "No"]


def _raw_reply(*indexed):
    """Una respuesta del modelo que solo trae las preguntas con esos índices."""
    r = reply()
    r.output_text = __import__("json").dumps({"answers": [{"index": n, "option": -1, "text": "", "source": "unknown", **item} for n, item in indexed]})
    return r


@pytest.mark.parametrize("item", [{"option": -1, "source": "unknown"}, {"option": 9, "source": "cv"}, {"option": 9, "source": "answers"}, {"option": 0, "source": "unknown"}])
def test_the_declared_option_is_sent_even_if_the_model_says_unknown_or_gives_a_bad_option(item):
    client = FakeClient(reply(item))
    [entry] = apply.answer_questions([closed(SALES, LESS_OPTIONS)], make_profile(DECLARED_LESS), client, MODEL, "dev", [])
    assert entry["source"] == "answers" and entry["option"] == 1


def test_the_declared_option_is_sent_if_the_model_does_not_bring_the_question():
    questions = [closed(SALES, LESS_OPTIONS), closed(SCHOOL, ["Sí", "No"])]
    client = FakeClient(_raw_reply((1, {"option": 0, "source": "cv"})))
    out = apply.answer_questions(questions, make_profile(DECLARED_LESS), client, MODEL, "dev", [])
    assert [(e["source"], e["option"]) for e in out] == [("answers", 1), ("cv", 0)]


def test_a_missing_question_without_a_declared_option_is_still_an_error():
    questions = [closed(SALES, LESS_OPTIONS), closed(SCHOOL, ["Sí", "No"])]
    with pytest.raises(apply.AnswerError):
        apply.answer_questions(questions, make_profile(), FakeClient(_raw_reply((0, {"option": 0, "source": "cv"}))), MODEL, "dev", [])
    with pytest.raises(apply.AnswerError):  # una abierta no puede faltar
        apply.answer_questions([{"text": "Contanos", "kind": "open", "options": []}], make_profile({profile.normalize_question("Contanos"): "x"}),
                               FakeClient(_raw_reply()), MODEL, "dev", [])


def test_a_declared_answer_that_matches_no_option_leaves_unknown_as_unknown():
    client = FakeClient(reply({"option": -1, "source": "unknown"}))
    [entry] = apply.answer_questions([closed(SALES, LESS_OPTIONS)], make_profile({profile.normalize_question(SALES): "Un poco"}), client, MODEL, "dev", [])
    assert entry["source"] == "unknown" and entry["option"] is None


def test_run_live_sends_an_offer_whose_only_question_is_declared_even_if_the_model_says_unknown(conn, tmp_path):
    id16 = add_offer(conn)
    page = FakePage(standard_site(pages={KQ_URL: fixture("apply_kq_sales.html")}))
    declared = {profile.normalize_question(SALES): SALES_OPTIONS[1]}
    result = apply.run_live(conn, make_profile(declared), MODEL, FakeClient(reply({"option": -1, "source": "unknown"})), page, TODAY,
                            stop_file=tmp_path / "STOP", pause=lambda: None, cv_provider=stub_cv())
    assert [i["id16"] for i in result["sent"]] == [id16] and status_of(conn, id16) == "applied"
    assert page.site.submitted[0]["checked"] == {"KillerQuestions[0].ClosedQuestion": "2"}
    assert ui_logic.pending_questions(conn) == []
