"""Tests de `apply --live`: respuestas a las preguntas, `browser.apply_one` y `apply.run_live`. Sin red, sin navegador y sin OpenAI.

La página es `fakesite.FakePage` (HTML falso de tests/fixtures/) y el cliente de OpenAI es falso.
"""
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

import fakesite
from fakesite import DETAIL_URL, KQ_URL, LOGIN_URL, MATCH_URL, OTHER_URL, POSTAPPLY_URL, FakePage, fixture, standard_site
from compuapply import apply, browser, cli, db, profile, score, ui_logic

ROOT = Path(__file__).parent.parent
RESUME = yaml.safe_load((ROOT / "examples" / "resume.example.yaml").read_text(encoding="utf-8"))
TODAY = datetime.now().astimezone().date()  # las postulaciones se fechan con el reloj real: el tope diario las cuenta en este día
MODEL = "gpt-6-luna"
SALARY = "1.500.000 pesos"
QUESTION_JS = "¿Cuántos años de experiencia tenés con JavaScript?"
QUESTION_SALARY = "¿Cuál es tu remuneración pretendida?"
QUESTION_ONSITE = "¿Tenés disponibilidad presencial de lunes a viernes?"


# ── cliente de OpenAI falso ──────────────────────────────────────────────────

def reply(*items, tokens=(1000, 0, 200), status="completed"):
    data = {"answers": [{"index": n, "option": -1, "text": "", "source": "unknown", **item} for n, item in enumerate(items)]}
    return SimpleNamespace(output_text=json.dumps(data), output=[], status=status,
                           usage=SimpleNamespace(input_tokens=tokens[0], input_tokens_details=SimpleNamespace(cached_tokens=tokens[1]),
                                                 output_tokens=tokens[2]))


class FakeClient:
    def __init__(self, *responses):
        self.calls, self._responses = [], list(responses)
        self.responses = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        item = self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]
        if isinstance(item, Exception):
            raise item
        return item


def model_decides_onsite(monkeypatch):
    """Apaga la regla de disponibilidad (065) para probar el camino en que el modelo contesta la pregunta de presencial."""
    monkeypatch.setattr(apply, "availability_answer", lambda *a, **k: None)


def known_reply():
    """Las tres preguntas de la fixture, contestadas: años (cv), sueldo (answers) y presencial (answers)."""
    return reply({"option": 1, "source": "answers"}, {"text": f"Mi remuneración pretendida es de {SALARY}", "source": "answers"},
                 {"option": 0, "source": "answers"})


def unknown_salary_reply():
    return reply({"option": 1, "source": "answers"}, {"source": "unknown"}, {"option": 0, "source": "answers"})


def unknown_modality_reply():
    """Como `known_reply`, pero la modalidad (la tercera) queda sin respuesta: la oferta espera en Preguntas. El sueldo abierto sale de `salary`."""
    return reply({"option": 1, "source": "answers"}, {"text": "x", "source": "answers"}, {"option": -1, "source": "unknown"})


# ── perfil y base ────────────────────────────────────────────────────────────

# La opción de «años de experiencia» tiene cifras: solo se elige con una respuesta declarada a esa misma pregunta (TAREA-044).
DECLARED_YEARS = {profile.normalize_question(QUESTION_JS): "Entre 1 y 3 años"}


def make_profile(answers=None, daily_cap=10, month=2.0, review=False, default_pdf_name=None):
    variant = {"id": "dev", "keywords": ["x"], "resume": RESUME, "pdf": None}
    if default_pdf_name:
        variant["default_pdf_name"] = default_pdf_name
    return {"variants": [variant],
            "exclude_companies": [], "exclude_title_words": [],
            "apply": {"threshold": 70, "daily_cap": daily_cap, "calibrated_with": f"{MODEL} {score.PROMPT_VERSION}", "review_cv": review},
            "run": {"max_cost_month": month},
            "answers": {k: v for k, v in {**DECLARED_YEARS, "modality": "presencial", "availability": "Lunes a viernes", **(answers or {})}.items() if v is not None}}  # modality None: sin declarar


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / "t.db")
    yield connection
    connection.close()


def add_offer(conn, n=1, points=85, url=DETAIL_URL, title=None, company=None, location="Rosario", variant="dev") -> str:
    id16 = f"{n:016X}"
    db.upsert_offers(conn, [{"id16": id16, "data_id": f"{n:032X}", "title": title or f"Oferta {n}",
                             "company": f"Empresa {n}" if company is None else company, "location": location, "url": url}], "x")
    db.save_detail(conn, id16, {"description": "Buscamos desarrollador.", "date_posted": "", "valid_through": "", "employment_type": ""})
    db.save_score(conn, id16, MODEL, score.PROMPT_VERSION,
                  {"best_variant": variant, "score": points, "all_scores": {variant: points}, "reasons": [], "missing": []}, (1, 0, 1), 0.0)
    return id16


CV_NAME = "CV-Giovanni-Bianchi-Desarrollador-de-pruebas.pdf"


def stub_cv(version="c8", name=CV_NAME, calls=None):
    """El proveedor de CV falso: deja un PDF mínimo con el nombre presentable en una carpeta temporal, como el real."""
    import tempfile

    def provide(item):
        folder = Path(tempfile.mkdtemp(prefix="compuapply-test-"))
        (folder / name).write_bytes(b"%PDF-" + b"0" * 100)
        if calls is not None:
            calls.append(item["id16"])
        return {"path": folder / name, "name": name, "version": version, "dir": folder, "cost_usd": 0.0}

    return provide


def go(conn, tmp_path, site, client=None, *, prof=None, stop_file=None, provider=None):
    page = FakePage(site)
    result = apply.run_live(conn, prof or make_profile({"salary": SALARY}), MODEL, client, page, TODAY,
                            stop_file=stop_file or tmp_path / "STOP", pause=lambda: None, cv_provider=provider or stub_cv())
    return result, page


def rows(conn, sql):
    return conn.execute(sql).fetchall()


def status_of(conn, id16):
    return conn.execute("SELECT status FROM offers WHERE id16 = ?", (id16,)).fetchone()[0]


# ── normalización y perfil ───────────────────────────────────────────────────

def test_normalize_question_drops_case_accents_and_signs():
    assert profile.normalize_question("¿Cuál es tu  REMUNERACIÓN pretendida?") == "cual es tu remuneracion pretendida"
    assert profile.normalize_question("Años de experiencia (Node.js)") == "anos de experiencia node js"


def write_profile_with(tmp_path, **extra):
    (tmp_path / "a.yaml").write_text(yaml.safe_dump(RESUME), encoding="utf-8")
    path = tmp_path / "profile.yaml"
    path.write_text(yaml.safe_dump({"variants": [{"id": "dev", "resume": "a.yaml", "keywords": ["x"]}], **extra}), encoding="utf-8")
    return path


def test_the_profile_loads_answers_with_the_fixed_keys_and_normalized_questions(tmp_path):
    path = write_profile_with(tmp_path, answers={"salary": 1500000, "modality": "Híbrido", "start": "De inmediato",
                                                 "¿Tenés VISA de trabajo?": "No tengo", "availability": "Lunes a viernes"})
    assert profile.load_profile(path)["answers"] == {"salary": "1500000", "modality": "híbrido", "start": "De inmediato",
                                                      "tenes visa de trabajo": "No tengo", "availability": "Lunes a viernes"}
    assert profile.load_profile(write_profile_with(tmp_path))["answers"] == {}


@pytest.mark.parametrize("answers, message", [
    ("texto", "tiene que ser un mapa"),
    ({"salary": ""}, "texto no vacío"),
    ({"salary": ["a"]}, "texto no vacío"),
    ({"modality": "volando"}, "una o más de presencial, híbrido o remoto"),
    ({"modality": "Presencial/full time"}, "una o más de presencial, híbrido o remoto"),
    ({"modality": "/"}, "una o más de presencial, híbrido o remoto"),
    ({"???": "algo"}, "no tiene letras ni números"),
])
def test_bad_answers_give_a_clear_error(tmp_path, answers, message):
    with pytest.raises(RuntimeError, match=message):
        profile.load_profile(write_profile_with(tmp_path, answers=answers))


@pytest.mark.parametrize("written, canonical", [
    ("Presencial/Hibrido/Remoto", "presencial, híbrido, remoto"),
    ("remoto y presencial", "presencial, remoto"),
    ("HÍBRIDO", "híbrido"),
    ("remoto, remoto", "remoto"),
    ("Remoto o Híbrido; presencial", "presencial, híbrido, remoto"),
    ("  hibrido  /  remoto ", "híbrido, remoto"),
    ("remoto,", "remoto"),
])
def test_modality_accepts_one_or_more_options_and_is_saved_in_its_canonical_form(tmp_path, written, canonical):
    assert profile.canonical_modality(written) == canonical
    assert profile.load_profile(write_profile_with(tmp_path, answers={"modality": written}))["answers"] == {"modality": canonical}


@pytest.mark.parametrize("written", ["Presencial/full time", "volando", "/", " y ", "remoto y volando"])
def test_modality_with_a_part_that_is_not_one_of_the_three_is_rejected(written):
    assert profile.canonical_modality(written) is None


# ── answer_questions ─────────────────────────────────────────────────────────

QUESTIONS = [{"text": QUESTION_JS, "kind": "closed", "options": ["Menos de 1 año", "Entre 1 y 3 años", "Más de 3 años"]},
             {"text": QUESTION_SALARY, "kind": "open", "options": []},
             {"text": QUESTION_ONSITE, "kind": "closed", "options": ["Sí", "No"]}]


def ask(client, answers=None, questions=QUESTIONS, calls=None):
    return apply.answer_questions(questions, make_profile(answers), client, MODEL, "dev", calls)


def test_answer_questions_uses_the_declared_salary_text_and_the_request_has_no_personal_data():
    client, calls = FakeClient(known_reply()), []
    out = ask(client, {"salary": SALARY, "modality": "presencial"}, calls=calls)
    assert [a["source"] for a in out] == ["answers", "answers", "answers"]
    assert out[0]["option"] == 1 and out[2]["option"] == 0 and SALARY in out[1]["text"]
    assert apply.answer_text(out[0]) == "Entre 1 y 3 años" and apply.answer_text(out[1]).endswith(SALARY)
    [call] = client.calls
    user = call["input"][1]["content"]
    assert f"- salary: {SALARY}" in user and "- modality: presencial" in user and QUESTION_SALARY in user and "   1) Entre 1 y 3 años" in user
    for personal in ("Giovanni", "Bianchi", "giovanni.bianchi@example.com", "3345678901", "Via Nazionale", "github.com"):
        assert personal not in json.dumps(call, ensure_ascii=False)
    fmt = call["text"]["format"]
    assert fmt["name"] == "respuestas" and fmt["strict"] is True
    assert calls[0]["valid"] is True and calls[0]["tokens"] == (1000, 0, 200)


def test_a_salary_question_without_a_declared_salary_is_unknown():
    out = ask(FakeClient(unknown_salary_reply()))
    assert [a["source"] for a in out] == ["answers", "unknown", "answers"] and out[1]["text"] is None


def test_an_invented_number_in_an_open_text_makes_it_unknown():
    invented = reply({"option": 1, "source": "cv"}, {"text": "Pretendo 2.000.000 pesos", "source": "answers"}, {"option": 0, "source": "cv"})
    assert ask(FakeClient(invented))[1]["source"] == "unknown"  # sin `salary` cargado la abierta de sueldo la contesta el modelo, y el control corre
    cv_number = reply({"option": 1, "source": "cv"}, {"text": "Trabajo desde 2011 en lo mío", "source": "cv"}, {"option": 0, "source": "cv"})
    assert ask(FakeClient(cv_number))[1]["source"] == "unknown"  # los números solo salen de `answers`


def test_an_open_text_with_an_unknown_technology_or_a_level_is_unknown():
    tech = [{"text": "Contanos tu experiencia con tecnologías", "kind": "open", "options": []}]
    for text in ("Trabajo con Node.js hace tiempo", "Fui gerente de equipos"):
        out = ask(FakeClient(reply({"text": text, "source": "cv"})), questions=tech)
        assert out[0]["source"] == "unknown", text
    ok = ask(FakeClient(reply({"text": "Trabajo con Python y AWS", "source": "cv"})), questions=tech)
    assert ok[0]["source"] == "cv"  # el CV de ejemplo trae Python y AWS


def test_a_text_over_500_characters_or_an_empty_one_or_a_bad_option_is_unknown():
    out = ask(FakeClient(reply({"option": 7, "source": "cv"}, {"text": "a" * 501, "source": "cv"}, {"option": -1, "source": "answers"})))
    assert [a["source"] for a in out] == ["answers", "unknown", "answers"]  # la 0 tiene respuesta declarada igual a una opción: esa manda (046); la 2 es de disponibilidad: sale de modality (065)
    assert out[0]["option"] == 1
    assert ask(FakeClient(reply({"option": 0, "source": "cv"}, {"text": "  ", "source": "cv"}, {"option": 1, "source": "cv"})))[1]["source"] == "unknown"


@pytest.mark.parametrize("bad, message", [
    (SimpleNamespace(output_text="[1]x", output=[], status="completed", usage=None), "formato esperado"),
    (reply({"source": "cv"}), "exactamente cada pregunta"),
    (reply({"source": "cv"}, {"source": "cv"}, {"source": "cv"}, {"source": "cv"}), "exactamente cada pregunta"),
    (reply({"source": "cv"}, status="incomplete"), "no terminó"),
])
def test_an_unusable_answer_raises_answer_error(bad, message):
    calls = []
    with pytest.raises(apply.AnswerError, match=message):
        ask(FakeClient(bad), calls=calls)
    assert len(calls) == 1 and calls[0]["valid"] is False


def test_the_system_prompt_forbids_inventing():
    assert "no inventes" in apply.ANSWER_SYSTEM and "unknown" in apply.ANSWER_SYSTEM


def test_the_answer_prompt_explains_that_modality_is_a_list_of_accepted_options():
    system = apply.ANSWER_SYSTEM
    assert "las modalidades de trabajo que el candidato acepta, una o más de presencial, híbrido y remoto" in system
    assert "que está en la lista de `modality`, la respuesta es sí" in system
    assert "que no está en la lista, la respuesta es no" in system
    assert "la zona, el traslado, el horario) no se contesta con `modality`" in system


def test_the_request_carries_the_canonical_modality_of_the_loaded_profile(tmp_path):
    loaded = profile.load_profile(write_profile_with(tmp_path, answers={"salary": SALARY, "modality": "Presencial/Hibrido/Remoto"}))
    client = FakeClient(known_reply())
    apply.answer_questions(QUESTIONS, loaded, client, MODEL, "dev", [])
    assert "- modality: presencial, híbrido, remoto" in client.calls[0]["input"][1]["content"]


# ── browser.apply_one ────────────────────────────────────────────────────────

OFFER = {"id16": "0000000000000001", "url": DETAIL_URL, "title": "Oferta 1"}


def fill_all(seen=None):
    def answers_fn(questions):
        if seen is not None:
            seen.append(questions)
        return [{"kind": q["kind"], "options": q["options"], "option": 1 if q["kind"] == "closed" else None,
                 "text": "Mi respuesta" if q["kind"] == "open" else None} for q in questions]
    return answers_fn


def test_apply_one_reads_closed_and_open_questions_answers_them_sends_and_checks_the_final_screen():
    site, seen = standard_site(), []
    page = FakePage(site)
    outcome = browser.apply_one(page, OFFER, fill_all(seen))
    assert outcome["status"] == "applied"
    [questions] = seen
    assert [(q["kind"], q["text"]) for q in questions] == [("closed", QUESTION_JS), ("open", QUESTION_SALARY), ("closed", QUESTION_ONSITE)]
    assert questions[0]["options"] == ["Menos de 1 año", "Entre 1 y 3 años", "Más de 3 años"] and questions[2]["options"] == ["Sí", "No"]
    assert outcome["qa"] == [{"question": QUESTION_JS, "answer": "Entre 1 y 3 años"}, {"question": QUESTION_SALARY, "answer": "Mi respuesta"},
                             {"question": QUESTION_ONSITE, "answer": "No"}]
    [sent] = site.submitted
    assert sent["checked"] == {"KillerQuestions[0].ClosedQuestion": "2", "KillerQuestions[2].ClosedQuestion": "2"}
    assert sent["filled"] == {"KillerQuestions[1].OpenQuestion": "Mi respuesta"}
    assert page.visited == [DETAIL_URL]  # solo se navega al detalle: el resto lo hace el botón del sitio
    assert [c[1] for c in page.clicks()] == ["a", "button"] and page.handlers == []  # el oyente se quita al terminar


def test_apply_one_without_a_questions_screen_goes_straight_to_the_final_one():
    page = FakePage(standard_site(after_apply=(MATCH_URL, fakesite.APPLY_URL, POSTAPPLY_URL)))
    called = []
    outcome = browser.apply_one(page, OFFER, lambda questions: called.append(questions))
    assert (outcome["status"], outcome["qa"], outcome["cv"]) == ("applied", [], None) and outcome["im"] == "IM123" and outcome["alert"] is True
    assert called == [] and [c[1] for c in page.clicks()] == ["a"]


def test_apply_one_a_single_question_keeps_its_own_text():
    seen = []
    site = standard_site(pages={KQ_URL: fixture("apply_kq_one.html")})
    browser.apply_one(FakePage(site), OFFER, fill_all(seen))
    assert [(q["kind"], q["text"]) for q in seen[0]] == [("open", "Contanos por qué te interesa el puesto")]


def test_apply_one_already_applied_visible_skips_without_touching_postularme():
    page = FakePage(standard_site(pages={DETAIL_URL: fixture("apply_detail_applied.html")}))
    assert browser.apply_one(page, OFFER, fill_all())["status"] == "already_applied"
    assert page.clicks() == []


def test_apply_one_already_applied_hidden_goes_on():
    page = FakePage(standard_site())  # el detalle trae «Ya aplicaste» en un div oculto
    assert browser.apply_one(page, OFFER, fill_all())["status"] == "applied" and page.clicks()


def test_apply_one_with_an_unanswered_question_sends_nothing():
    site = standard_site()
    page = FakePage(site)
    assert browser.apply_one(page, OFFER, lambda questions: None)["status"] == "pending_question"
    assert site.submitted == [] and [c[1] for c in page.clicks()] == ["a"] and page.filled == {} and page.checked == {}


@pytest.mark.parametrize("site, message", [
    (standard_site(after_apply=(LOGIN_URL,)), "Sin sesión"),
    (standard_site(after_apply=(MATCH_URL, OTHER_URL)), "Pantalla desconocida"),
    (standard_site(pages={KQ_URL: fixture("apply_captcha.html")}), "desafío"),
    (standard_site(statuses={KQ_URL: 429}), "respondió 429"),
    (standard_site(statuses={DETAIL_URL: 403}), "respondió 403"),
    (standard_site(pages={KQ_URL: fixture("apply_kq_unknown_field.html")}), "no es el esperado"),
    (standard_site(pages={KQ_URL: "<html><body><form action='/candidate/kq'><button type='submit'>Enviar mi CV</button></form></body></html>"}),
     "no trae preguntas"),
    (standard_site(pages={DETAIL_URL: "<html><body><h1>Sin botón</h1></body></html>"}), "No hay botón"),
    (standard_site(after_apply=()), "La navegación no terminó"),
    (standard_site(pages={DETAIL_URL: fixture("apply_captcha.html")}), "desafío"),
])
def test_apply_one_halts_on_anything_unknown(site, message):
    with pytest.raises(browser.Halt, match=message) as err:
        browser.apply_one(FakePage(site), OFFER, fill_all())
    assert "revisá la ventana a mano" in str(err.value) or "Sin sesión" in str(err.value)
    assert site.submitted == []


def test_apply_one_halts_when_there_is_no_submit_button_or_the_answer_is_too_long():
    no_submit = fixture("apply_kq.html").replace('<button type="submit" class="b_primary">Enviar mi CV</button>', "")
    with pytest.raises(browser.Halt, match="botón para enviar"):
        browser.apply_one(FakePage(standard_site(pages={KQ_URL: no_submit})), OFFER, fill_all())

    def too_long(questions):
        return [{"kind": q["kind"], "options": q["options"], "option": 0, "text": "a" * 501} for q in questions]

    site = standard_site()
    with pytest.raises(browser.Halt, match="500 caracteres"):
        browser.apply_one(FakePage(site), OFFER, too_long)
    assert site.submitted == []


@pytest.mark.parametrize("url", ["https://otro.sitio/ofertas-de-trabajo/x", "https://candidato.ar.computrabajo.com/match/?oi=1",
                                 "https://ar.computrabajo.com/candidate/apply/?oi=1", ""])
def test_apply_one_only_opens_offer_detail_urls(url):
    page = FakePage(standard_site())
    with pytest.raises(browser.Halt, match="no es de un detalle"):
        browser.apply_one(page, {**OFFER, "url": url}, fill_all())
    assert page.visited == []


def test_apply_one_answers_with_the_wrong_length_halt_before_filling():
    page = FakePage(standard_site())
    with pytest.raises(browser.Halt, match="no coinciden"):
        browser.apply_one(page, OFFER, lambda questions: [])
    assert page.filled == {} and page.checked == {}


# ── run_live ─────────────────────────────────────────────────────────────────

def test_run_live_answers_sends_and_records_the_application_and_the_spend(conn, tmp_path):
    id16 = add_offer(conn)
    site, client = standard_site(), FakeClient(known_reply())
    result, page = go(conn, tmp_path, site, client)
    assert [i["id16"] for i in result["sent"]] == [id16] and result["skipped"] == [] and result["halted"] is None
    assert result["sent"][0]["questions"] == 3
    assert status_of(conn, id16) == "applied"
    [(variant, points, status, reason, applied_at, qa)] = rows(conn, "SELECT variant_id, score, status, reason, applied_at, qa FROM applications")
    assert (variant, points, status) == ("dev", 85, "applied") and reason == "Te postulaste correctamente"
    assert datetime.fromisoformat(applied_at) and apply.applied_today(conn, datetime.fromisoformat(applied_at).astimezone().date()) == 1
    assert json.loads(qa) == [{"question": QUESTION_JS, "answer": "Entre 1 y 3 años"},
                              {"question": QUESTION_SALARY, "answer": SALARY},
                              {"question": QUESTION_ONSITE, "answer": "Sí"}]
    assert rows(conn, "SELECT kind, id16, ok FROM api_spend") == [("answers", id16, 1)]
    assert result["cost_usd"] == pytest.approx((1000 * 0.10 + 200 * 0.50) / 1_000_000)
    assert score.month_spent(conn, datetime.now().astimezone().date()) == pytest.approx(result["cost_usd"])
    assert site.submitted[0]["filled"] == {"KillerQuestions[1].OpenQuestion": SALARY}  # el sueldo abierto sale de `salary`, tal cual


def test_run_live_an_unanswered_salary_does_not_send_and_leaves_the_question_pending(conn, tmp_path):
    id16 = add_offer(conn)
    site = standard_site()
    result, page = go(conn, tmp_path, site, FakeClient(unknown_salary_reply()), prof=make_profile())
    assert result["sent"] == [] and [i["reason"] for i in result["skipped"]] == ["pregunta sin respuesta"]
    assert site.submitted == [] and status_of(conn, id16) == "pending_question"
    [(question, options, first_seen)] = rows(conn, "SELECT question, options, first_seen FROM pending_questions")
    assert question == QUESTION_SALARY and json.loads(options) == [] and first_seen
    assert rows(conn, "SELECT status, reason FROM applications") == [("pending_question", "pregunta sin respuesta")]
    assert apply.applied_today(conn, TODAY) == 0
    assert "(con pregunta sin respuesta: 1)" in apply.live_summary(result)
    assert apply.plan(conn, make_profile(), MODEL, TODAY)["send"] == []  # no se reintenta hasta que el usuario conteste


def test_answering_the_pending_question_requeues_the_offer_and_the_next_run_sends_it_with_that_text(conn, tmp_path):
    id16 = add_offer(conn)
    go(conn, tmp_path, standard_site(), FakeClient(unknown_salary_reply()), prof=make_profile())
    (tmp_path / "a.yaml").write_text(yaml.safe_dump(RESUME), encoding="utf-8")
    path = tmp_path / "profile.yaml"
    path.write_text(yaml.safe_dump({"variants": [{"id": "dev", "resume": "a.yaml", "keywords": ["x"]}]}), encoding="utf-8")
    [item] = ui_logic.pending_questions(conn)
    assert item["key"] == profile.normalize_question(QUESTION_SALARY) and item["offers"] == 1 and item["question"] == QUESTION_SALARY
    assert ui_logic.answer_question(path, tmp_path / "t.db", item["key"], f"  {SALARY} ") == 1
    assert profile.load_profile(path)["answers"] == {item["key"]: SALARY}
    assert status_of(conn, id16) == "new" and ui_logic.pending_questions(conn) == []
    result, _ = go(conn, tmp_path, standard_site(), FakeClient(known_reply()), prof={**make_profile(), "answers": {"salary": SALARY, "modality": "presencial", "availability": "Lunes a viernes", **DECLARED_YEARS}})
    assert [i["id16"] for i in result["sent"]] == [id16] and status_of(conn, id16) == "applied"
    assert rows(conn, "SELECT status FROM applications") == [("applied",)]  # la fila de «pendiente» se reemplazó


def test_run_live_already_applied_marks_the_offer_and_does_not_count_for_the_daily_cap(conn, tmp_path):
    id16 = add_offer(conn)
    site = standard_site(pages={DETAIL_URL: fixture("apply_detail_applied.html")})
    result, page = go(conn, tmp_path, site, FakeClient(known_reply()))
    assert result["sent"] == [] and [i["reason"] for i in result["skipped"]] == ["ya estaba postulada"]
    assert status_of(conn, id16) == "applied" and page.clicks() == []
    assert rows(conn, "SELECT status FROM applications") == [("already_applied",)] and apply.applied_today(conn, TODAY) == 0


@pytest.mark.parametrize("site, message", [
    (standard_site(after_apply=(LOGIN_URL,)), "Sin sesión"),
    (standard_site(pages={KQ_URL: fixture("apply_captcha.html")}), "desafío"),
    (standard_site(after_apply=(MATCH_URL, OTHER_URL)), "Pantalla desconocida"),
    (standard_site(statuses={DETAIL_URL: 429}), "respondió 429"),
])
def test_run_live_halts_on_the_first_unknown_screen_and_leaves_the_rest_unprocessed(conn, tmp_path, site, message):
    first, second = add_offer(conn, 1, 90), add_offer(conn, 2, 80)
    result, page = go(conn, tmp_path, site, FakeClient(known_reply()))
    assert message in result["halted"] and result["sent"] == []
    assert [(i["id16"], i["reason"]) for i in result["skipped"]] == [(first, "frenó la corrida"), (second, "sin procesar: la corrida frenó")]
    assert status_of(conn, first) == "new"
    # si el CV alcanzó a subirse, la fila guarda su archivo (para reusarlo en el reintento); si no, no hay fila
    assert rows(conn, "SELECT id16, status, cv_file, cv_version FROM applications") == ([(first, "cv_uploaded", CV_NAME, "c8")] if site.uploads else [])
    assert page.visited.count(DETAIL_URL) in (1, 2)  # solo la primera oferta (su detalle se abre dos veces si ya subió su CV)


def test_run_live_respects_the_daily_cap_and_pauses_between_offers(conn, tmp_path):
    ids = [add_offer(conn, n, 90 - n) for n in (1, 2, 3)]
    pauses, page = [], FakePage(standard_site(after_apply=(MATCH_URL, fakesite.APPLY_URL, POSTAPPLY_URL)))
    result = apply.run_live(conn, make_profile(daily_cap=2), MODEL, None, page, TODAY, stop_file=tmp_path / "STOP", pause=lambda: pauses.append(1),
                            cv_provider=stub_cv())
    assert [i["id16"] for i in result["sent"]] == ids[:2] and len(pauses) == 1  # una pausa entre las dos, ninguna antes de la primera
    assert [i["reason"] for i in result["plan"]["skip"]] == ["tope diario"] and status_of(conn, ids[2]) == "new"
    assert apply.applied_today(conn, datetime.now().astimezone().date()) == 2
    again = apply.run_live(conn, make_profile(daily_cap=2), MODEL, None, FakePage(standard_site()), TODAY, stop_file=tmp_path / "STOP", pause=lambda: None,
                           cv_provider=stub_cv())
    assert again["sent"] == [] and again["plan"]["send"] == []  # el tope ya está usado


def test_run_live_stops_with_the_stop_file_before_touching_anything(conn, tmp_path):
    add_offer(conn)
    (tmp_path / "STOP").write_text("x")
    result, page = go(conn, tmp_path, standard_site(), FakeClient(known_reply()))
    assert "Existe el archivo" in result["halted"] and DETAIL_URL not in page.visited and result["sent"] == [] and len(result["skipped"]) == 1


def test_run_live_stop_file_created_between_offers_stops_the_next_one(conn, tmp_path):
    first, second = add_offer(conn, 1, 90), add_offer(conn, 2, 80)
    stop = tmp_path / "STOP"
    page = FakePage(standard_site(after_apply=(MATCH_URL, fakesite.APPLY_URL, POSTAPPLY_URL)))
    result = apply.run_live(conn, make_profile(), MODEL, None, page, TODAY, stop_file=stop, pause=lambda: stop.write_text("x"), cv_provider=stub_cv())
    assert [i["id16"] for i in result["sent"]] == [first] and "Existe el archivo" in result["halted"]
    assert status_of(conn, second) == "new"


def test_run_live_three_invalid_model_answers_in_a_row_halt(conn, tmp_path):
    for n in (1, 2, 3, 4):
        add_offer(conn, n, 90 - n)
    bad = SimpleNamespace(output_text="roto", output=[], status="completed", usage=None)
    result, _ = go(conn, tmp_path, standard_site(), FakeClient(bad))
    assert "3 respuestas inválidas" in result["halted"] and result["sent"] == []
    assert [i["reason"].split(":")[0] for i in result["skipped"]] == ["respuesta inválida del modelo"] * 3 + ["sin procesar"]
    assert rows(conn, "SELECT ok FROM api_spend") == [(0,)] * 3


def test_run_live_a_valid_answer_resets_the_invalid_counter(conn, tmp_path):
    for n in (1, 2, 3, 4):
        add_offer(conn, n, 90 - n)
    bad = SimpleNamespace(output_text="roto", output=[], status="completed", usage=None)
    client = FakeClient(bad, bad, known_reply(), bad, bad)
    result, _ = go(conn, tmp_path, standard_site(), client)
    assert result["halted"] is None and len(result["sent"]) == 1 and len(result["skipped"]) == 3


def test_run_live_without_a_key_halts_when_a_question_shows_up_but_not_before(conn, tmp_path):
    add_offer(conn)
    result, _ = go(conn, tmp_path, standard_site(), None)
    assert "Falta la clave de OpenAI" in result["halted"] and result["sent"] == []
    fresh = db.connect(tmp_path / "t2.db")
    add_offer(fresh)
    page = FakePage(standard_site(after_apply=(MATCH_URL, fakesite.APPLY_URL, POSTAPPLY_URL)))
    done = apply.run_live(fresh, make_profile(), MODEL, None, page, TODAY, stop_file=tmp_path / "STOP", pause=lambda: None, cv_provider=stub_cv())
    assert len(done["sent"]) == 1  # sin preguntas no hace falta la clave
    fresh.close()


def test_run_live_halts_when_the_monthly_cap_is_reached_before_asking(conn, tmp_path):
    add_offer(conn)
    db.add_spend(conn, "answers", "x", MODEL, 5.0, 1, 1, True)
    client = FakeClient(known_reply())
    result, _ = go(conn, tmp_path, standard_site(), client, prof=make_profile(month=1.0))
    assert "Tope mensual alcanzado" in result["halted"] and client.calls == [] and result["sent"] == []


def test_run_live_an_unexpected_browser_error_halts_with_the_class_and_the_first_line_without_query_strings(conn, tmp_path):
    add_offer(conn)

    def boom(page, offer, answers_fn, cv_fn=None):
        raise ValueError("falló https://candidato.ar.computrabajo.com/candidate/kq?oi=SECRETO&im=TOKEN al leer\nsegunda línea con otro-secreto")

    page = FakePage(standard_site())
    result = apply.run_live(conn, make_profile(), MODEL, None, page, TODAY, stop_file=tmp_path / "STOP", pause=lambda: None, apply_one=boom,
                            cv_provider=stub_cv())
    assert "ValueError: falló https://candidato.ar.computrabajo.com/candidate/kq al leer" in result["halted"]
    assert "SECRETO" not in result["halted"] and "TOKEN" not in result["halted"] and "otro-secreto" not in result["halted"]


def test_run_live_with_nothing_to_send_never_needs_the_page(conn, tmp_path):
    add_offer(conn, points=10)  # bajo el umbral
    result = apply.run_live(conn, make_profile(), MODEL, None, None, TODAY, stop_file=tmp_path / "STOP", pause=lambda: None)
    assert result["sent"] == [] and result["skipped"] == [] and result["halted"] is None


# ── informe ──────────────────────────────────────────────────────────────────

def test_the_live_report_lists_sent_and_skipped_with_reasons_and_no_answers(conn, tmp_path, monkeypatch):
    model_decides_onsite(monkeypatch)
    add_offer(conn, 1, 90)
    add_offer(conn, 2, 80)
    add_offer(conn, 3, 10)
    site = standard_site(after_apply=(MATCH_URL, KQ_URL), statuses={})
    result, _ = go(conn, tmp_path, site, FakeClient(known_reply(), unknown_modality_reply()))
    meta = {"now": datetime(2026, 11, 3, 9, 30, 0), "threshold": 70, "daily_cap": 10, "applied_today": 0, "model": MODEL}
    path = apply.write_live_report(result, meta, tmp_path / "runs")
    text = path.read_text(encoding="utf-8")
    assert path.name == "20261103-093000-apply.md" and text.startswith("# Informe de apply (en vivo)")
    assert "## Enviadas (1)" in text and "## Saltadas en esta corrida (1)" in text and "pregunta sin respuesta" in text
    assert "## Saltadas por el plan (1)" in text and "puntaje bajo" in text and "Estado: completa" in text
    assert SALARY not in text and "Mi remuneración" not in text 
    assert "opciones (texto de la oferta): Menos de 1 año / Entre 1 y 3 años" in text or "Entre 1 y 3 años" in text  # las opciones son texto de la oferta
    add_offer(conn, 4, 95)
    halted, _ = go(conn, tmp_path, standard_site(after_apply=(LOGIN_URL,)), FakeClient(known_reply()))
    other = apply.write_live_report(halted, {**meta, "now": datetime(2026, 11, 3, 9, 31, 0)}, tmp_path / "runs").read_text(encoding="utf-8")
    assert "Estado: frenó" in other and "## Freno" in other and "Sin sesión" in other


# ── CLI ──────────────────────────────────────────────────────────────────────

class FakeContext:
    def __init__(self, page):
        self.pages, self.closed = [page], False

    def close(self):
        self.closed = True


@pytest.fixture
def live_env(tmp_path, monkeypatch):
    """CLI aislado: carpeta vacía, clave falsa, OpenAI y Playwright reemplazados; devuelve lo que hace falta para mirar."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("os.environ", {"OPENAI_API_KEY": "sk-test-NUNCA-SE-IMPRIME"})
    (tmp_path / "a.yaml").write_text(yaml.safe_dump(RESUME), encoding="utf-8")
    (tmp_path / "profile.yaml").write_text(yaml.safe_dump({
        "variants": [{"id": "dev", "resume": "a.yaml", "keywords": ["x"]}], "apply": {"threshold": 70, "daily_cap": 10, "review_cv": False},
        "answers": {"salary": SALARY, "modality": "presencial", "availability": "Lunes a viernes", **DECLARED_YEARS}}), encoding="utf-8")
    env = SimpleNamespace(client=FakeClient(known_reply()), site=standard_site(), page=None, launched=[])
    monkeypatch.setattr(cli.openai, "OpenAI", lambda: env.client)
    monkeypatch.setattr(apply, "human_pause", lambda: None)
    monkeypatch.setattr(apply, "make_cv_provider", lambda *a, **k: stub_cv())
    monkeypatch.setattr(apply.time, "sleep", lambda s: None)

    def sync_playwright():
        from contextlib import contextmanager

        @contextmanager
        def manager():
            env.page = FakePage(env.site)
            context = FakeContext(env.page)
            env.launched.append(context)
            yield SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=lambda *a, **k: context))

        return manager()

    monkeypatch.setattr(cli, "sync_playwright", sync_playwright)
    connection = db.connect(tmp_path / "data" / "compuapply.db")
    env.conn = connection
    yield env
    connection.close()


def live_args(*extra):
    return ["apply", "--live", *extra]


def test_cli_apply_live_posts_prints_the_summary_and_writes_the_report(live_env, tmp_path, capsys):
    id16 = add_offer(live_env.conn)
    assert cli.main(live_args()) == 0
    out = capsys.readouterr()
    lines = out.out.strip().splitlines()
    assert lines[0].startswith("Enviadas: 1 · saltadas: 0") and lines[-1].startswith("Informe: data")
    assert out.err == "" and "sk-test" not in out.out and SALARY not in out.out
    [report] = (tmp_path / "data" / "runs").glob("*-apply.md")
    assert "## Enviadas (1)" in report.read_text(encoding="utf-8")
    assert status_of(live_env.conn, id16) == "applied" and live_env.launched[0].closed
    assert CHECK_VISIT in live_env.page.visited[0]  # primero se verifica la sesión; recién después se abre la oferta
    assert live_env.page.visited[3] == DETAIL_URL  # [0] verifica la sesión, [1] cuenta los PDF, [2] lee «Mis postulaciones»


CHECK_VISIT = "candidate/cv/uploadcv"


def test_cli_apply_live_without_offers_to_send_never_opens_the_browser(live_env, capsys):
    add_offer(live_env.conn, points=10)
    assert cli.main(live_args()) == 0
    assert live_env.launched == [] and capsys.readouterr().out.startswith("Enviadas: 0 · saltadas: 0")


def test_cli_apply_without_live_never_opens_the_browser(live_env, capsys):
    add_offer(live_env.conn)
    assert cli.main(["apply"]) == 0
    assert live_env.launched == [] and "Enviaría: 1" in capsys.readouterr().out


def test_cli_apply_live_with_an_expired_session_exits_1_and_posts_nothing(live_env, capsys):
    add_offer(live_env.conn)
    live_env.site.pages[fakesite.CHECK_URL] = ""
    original = live_env.site.load
    live_env.site.load = lambda page, url: original(page, LOGIN_URL if url == fakesite.CHECK_URL else url)
    assert cli.main(live_args()) == 1
    assert "Sesión vencida" in capsys.readouterr().err and live_env.page.clicks() == []


def test_cli_apply_live_halt_exits_1_with_the_message_and_still_writes_the_report(live_env, tmp_path, capsys):
    add_offer(live_env.conn)
    live_env.site.pages[KQ_URL] = fixture("apply_captcha.html")
    assert cli.main(live_args()) == 1
    out = capsys.readouterr()
    assert "Freno: " in out.err and "desafío" in out.err
    [report] = (tmp_path / "data" / "runs").glob("*-apply.md")
    assert "## Freno" in report.read_text(encoding="utf-8")


def test_cli_apply_live_respects_stop(live_env, tmp_path, capsys):
    add_offer(live_env.conn)
    (tmp_path / "STOP").write_text("x")
    assert cli.main(live_args()) == 1
    assert "Existe el archivo" in capsys.readouterr().err and live_env.page.clicks() == []


def test_cli_run_live_is_still_not_implemented(capsys):
    assert cli.main(["run", "--live"]) == 1
    assert "apply --live" in capsys.readouterr().err


# ── ventana y formulario ─────────────────────────────────────────────────────

def test_the_form_round_trips_the_four_answers_and_keeps_the_specific_ones(tmp_path):
    path = write_profile_with(tmp_path, answers={"salary": SALARY, "start": "Ya", "tenes visa": "No"})
    form = ui_logic.profile_to_form(path)
    assert form["answers"] == {"salary": SALARY, "availability": "", "modality": "", "start": "Ya", "location": "", "mobility": "", "salary_part_time": "", "salary_daily": ""}
    form["answers"]["modality"] = "remoto"
    form["answers"]["start"] = ""
    ui_logic.save_profile(ui_logic.form_to_profile(form), path)
    assert yaml.safe_load(path.read_text(encoding="utf-8"))["answers"] == {"salary": SALARY, "modality": "remoto", "tenes visa": "No"}
    for key in form["answers"]:
        form["answers"][key] = ""
    ui_logic.save_profile(ui_logic.form_to_profile(form), path)
    assert yaml.safe_load(path.read_text(encoding="utf-8"))["answers"] == {"tenes visa": "No"}


def test_a_form_without_answers_leaves_no_answers_section(tmp_path):
    path = write_profile_with(tmp_path)
    ui_logic.save_profile(ui_logic.form_to_profile(ui_logic.profile_to_form(path)), path)
    assert "answers" not in yaml.safe_load(path.read_text(encoding="utf-8"))


def test_a_bad_modality_in_the_form_is_a_clear_error_and_does_not_save(tmp_path):
    path = write_profile_with(tmp_path)
    form = ui_logic.profile_to_form(path)
    for bad in ("volando", "Presencial/full time"):
        form["answers"]["modality"] = bad
        with pytest.raises(RuntimeError, match="una o más de presencial, híbrido o remoto"):
            ui_logic.save_profile(ui_logic.form_to_profile(form), path)
        assert "answers" not in yaml.safe_load(path.read_text(encoding="utf-8"))


def test_saving_the_form_with_several_modalities_keeps_the_canonical_form_and_reloads_it(tmp_path):
    path = write_profile_with(tmp_path)
    form = ui_logic.profile_to_form(path)
    form["answers"]["modality"] = "Presencial/Hibrido/Remoto"
    assert ui_logic.form_to_profile(form)["answers"] == {"modality": "presencial, híbrido, remoto"}
    ui_logic.save_profile(ui_logic.form_to_profile(form), path)
    assert yaml.safe_load(path.read_text(encoding="utf-8"))["answers"] == {"modality": "presencial, híbrido, remoto"}
    assert ui_logic.profile_to_form(path)["answers"]["modality"] == "presencial, híbrido, remoto"
    assert profile.load_profile(path)["answers"] == {"modality": "presencial, híbrido, remoto"}


def test_pending_questions_group_by_normalized_question_and_count_offers(conn):
    for n in (1, 2):
        db.save_pending_question(conn, f"{n:016X}", QUESTION_SALARY if n == 1 else "¿CUÁL es tu remuneración pretendida", [])
    db.save_pending_question(conn, f"{1:016X}", QUESTION_ONSITE, ["Sí", "No"])
    found = ui_logic.pending_questions(conn)
    assert [(f["question"], f["offers"], f["options"]) for f in found] == [(QUESTION_SALARY, 2, []), (QUESTION_ONSITE, 1, ["Sí", "No"])]
    db.save_pending_question(conn, f"{1:016X}", QUESTION_ONSITE, ["Otra"])  # misma oferta y pregunta: no se duplica ni cambia
    assert len(rows(conn, "SELECT * FROM pending_questions")) == 3


def test_answer_question_rejects_an_empty_answer_and_a_fixed_key(tmp_path):
    path = write_profile_with(tmp_path)
    for key, answer in (("cual es tu sueldo", "  "), ("salary", "algo"), ("", "algo")):
        with pytest.raises(RuntimeError):
            ui_logic.answer_question(path, tmp_path / "t.db", key, answer)


def test_live_count_is_the_number_of_offers_in_the_plan(conn, tmp_path):
    add_offer(conn, 1, 90)
    add_offer(conn, 2, 10)
    path = write_profile_with(tmp_path, apply={"threshold": 70, "daily_cap": 5})
    assert ui_logic.live_count(path, tmp_path / "t.db", MODEL) == 1


def test_the_database_has_the_pending_questions_table_and_the_applications_qa_column_even_on_an_old_database(tmp_path):
    import sqlite3
    old = sqlite3.connect(tmp_path / "old.db")
    old.execute("CREATE TABLE applications (id16 TEXT PRIMARY KEY, variant_id TEXT, score INTEGER, status TEXT NOT NULL, reason TEXT, applied_at TEXT)")
    old.commit()
    old.close()
    migrated = db.connect(tmp_path / "old.db")
    assert "qa" in {r[1] for r in migrated.execute("PRAGMA table_info(applications)")}
    assert [r[1] for r in migrated.execute("PRAGMA table_info(pending_questions)")] == ["id16", "question", "options", "first_seen", "answer_version", "reason"]
    db.save_application(migrated, "A" * 16, "dev", 80, "applied", "ok", [{"question": "q", "answer": "a"}])
    assert json.loads(migrated.execute("SELECT qa FROM applications").fetchone()[0]) == [{"question": "q", "answer": "a"}]
    migrated.close()
