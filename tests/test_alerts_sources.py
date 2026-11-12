"""TAREA-071: qué es el `setalert.ashx` de cada envío, tiempo en `postapply`, interés abierto y respuestas sin fuente. Todo falso."""
from datetime import datetime

import fakesite
from fakesite import POSTAPPLY_URL, FakePage, standard_site
from test_apply_live import MODEL, SALARY, TODAY, FakeClient, add_offer, conn, known_reply, make_profile, rows, status_of, stub_cv  # noqa: F401
from test_unconfirmed import NO_QUESTIONS, OFFER, SlowPage
from compuapply import apply, browser

META = {"now": datetime(2026, 11, 7, 13, 0, 0), "threshold": 70, "daily_cap": 10, "applied_today": 0, "model": MODEL}


def run(conn, tmp_path, site):
    page = FakePage(site)
    result = apply.run_live(conn, make_profile({"salary": SALARY}), MODEL, FakeClient(known_reply()), page, TODAY, stop_file=tmp_path / "STOP",
                            pause=lambda: None, cv_provider=stub_cv(), runs_dir=tmp_path / "runs")
    return result


def report(result, tmp_path):
    return apply.write_live_report(result, META, tmp_path / "out").read_text(encoding="utf-8")


# ── setalert.ashx ────────────────────────────────────────────────────────────

def test_a_get_setalert_is_noted_with_method_status_and_parameter_names_only():
    page = FakePage(standard_site(after_apply=NO_QUESTIONS))
    outcome = browser.apply_one(page, OFFER, lambda questions: [])
    [note] = outcome["alerts"]
    assert note == {"method": "GET", "status": 200, "url_params": ["lang", "t"], "body_params": [], "when": "antes"}
    assert outcome["alert"] is True and "es" not in note["url_params"]  # el valor «es» de `lang` no se anota


def test_the_body_names_are_noted_from_a_form_and_from_json_but_never_the_values():
    class Response:
        url = "https://x.example/ajax/setalert.ashx?a=1"
        status = 200

        def __init__(self, body):
            self.request = type("R", (), {"method": "POST", "post_data": body})()

    form = browser.alert_note(Response("email=secreto%40x.com&freq=1"))
    assert form["method"] == "POST" and form["body_params"] == ["email", "freq"] and "secreto" not in str(form)
    assert browser.alert_note(Response('{"keyword": "cajero", "city": 3}'))["body_params"] == ["city", "keyword"]
    assert browser.alert_note(Response(None))["body_params"] == []


def test_alerts_that_arrive_after_the_success_screen_say_so():
    notes = [{"method": "GET", "status": 200, "url_params": [], "body_params": []}] * 2
    posted = browser._posted(FakePage(standard_site()), [], None, notes, before=1, seconds=2.349)
    assert [a["when"] for a in posted["alerts"]] == ["antes", "después"] and posted["postapply_s"] == 2.3


def test_a_get_alert_does_not_stop_the_run_and_the_report_summarizes_it(conn, tmp_path):
    add_offer(conn, 1)
    add_offer(conn, 2, 80)
    result = run(conn, tmp_path, standard_site(after_apply=NO_QUESTIONS))
    assert result["halted"] is None and len(result["sent"]) == 2
    text = report(result, tmp_path)
    assert "método GET ×2" in text and "status 200 ×2" in text and "parámetros de la URL: lang, t" in text and "antes ×2 de confirmarse" in text
    assert "no toca la configuración de alertas" in text


def test_a_post_alert_on_every_send_does_not_stop_the_run_and_the_report_summarizes_3_posts(conn, tmp_path):
    ids = [add_offer(conn, n, 90 - n) for n in (1, 2, 3)]
    site = standard_site(after_apply=NO_QUESTIONS)
    site.alert_method, site.alert_body = "POST", "email=secreto%40x.com&frecuencia=diaria"
    result = run(conn, tmp_path, site)
    assert result["halted"] is None and sorted(i["id16"] for i in result["sent"]) == sorted(ids)
    assert all(status_of(conn, i) == "applied" for i in ids)
    text = report(result, tmp_path)
    assert "método POST ×3" in text and "del cuerpo: email, frecuencia" in text and "secreto" not in text
    assert "lo crea el sitio al cargar la pantalla de éxito; el programa no toca alertas (D-163)" in text
    assert "crea alertas con cada postulación" not in text


def test_an_alert_with_status_500_is_noted_and_does_not_stop(conn, tmp_path):
    ids = [add_offer(conn, n, 90 - n) for n in (1, 2)]
    site = standard_site(after_apply=NO_QUESTIONS)
    site.alert_method, site.alert_status = "POST", 500
    result = run(conn, tmp_path, site)
    assert result["halted"] is None and len(result["sent"]) == 2 and all(status_of(conn, i) == "applied" for i in ids)
    assert "status 500 ×2" in report(result, tmp_path)


# ── segundos en postapply ────────────────────────────────────────────────────

def test_the_seconds_in_postapply_are_measured_and_reported(conn, tmp_path):
    page = SlowPage(standard_site(after_apply=NO_QUESTIONS), after=3)
    outcome = browser.apply_one(page, OFFER, lambda questions: [])
    assert 3 <= outcome["postapply_s"] < 10
    add_offer(conn, 1)
    result = run(conn, tmp_path, standard_site(after_apply=NO_QUESTIONS))
    assert result["sent"][0]["postapply_s"] == 0.0
    assert "- Tiempo en `postapply`: promedio 0.0 s, máximo 0.0 s (1 envíos)." in report(result, tmp_path)


# ── interés abierto ──────────────────────────────────────────────────────────

def answer(question, prof, *items):
    client = FakeClient(known_reply() if not items else __import__("test_apply_live").reply(*items))
    [entry] = apply.answer_questions([question], prof, client, MODEL, "dev", [])
    return entry, client


def open_q(text):
    return {"text": text, "kind": "open", "options": []}


def closed(text, *options):
    return {"text": text, "kind": "closed", "options": list(options)}


def test_an_open_interest_question_is_yes_without_the_model():
    entry, client = answer(open_q("¿Te interesa un empleo 100% en la vía pública? (máximo 500 caracteres)"), make_profile())
    assert entry["source"] == "answers" and entry["text"] == "Sí, me interesa." and client.calls == []


def test_an_open_interest_with_a_modality_the_user_does_not_accept_is_pending():
    entry, client = answer(open_q("¿Te interesa trabajar de forma remota?"), make_profile({"modality": "presencial"}))
    assert entry["source"] == "unknown" and "modalidad" in entry["reason"] and client.calls == []


def test_an_open_question_that_is_not_about_interest_still_goes_to_the_model():
    entry, client = answer(open_q("¿Por qué querés trabajar con nosotros?"), make_profile(), {"source": "unknown"})
    # es una pregunta de opinión (082): el modelo la escribe; se pide una vez más y, sin oferta ni texto, queda pendiente
    assert entry["source"] == "unknown" and len(client.calls) == 2 and entry["reason"] == apply.OPINION_NO_TEXT


# ── fuente de la respuesta ───────────────────────────────────────────────────

def test_monotributista_with_source_cv_is_pending_without_source():
    entry, _ = answer(closed("¿Sos monotributista?", "Sí", "No"), make_profile(), {"option": 1, "source": "cv"})
    assert entry["source"] == "unknown" and entry["option"] is None and entry["reason"] == "sin fuente"


def test_sociable_and_objectives_with_yes_pass_with_source_actitud():
    for text in ("¿Sos alguien sociable?", "¿Te gusta trabajar con objetivos?", "¿Te sentís cómodo en la vía pública?"):
        entry, _ = answer(closed(text, "SI", "NO"), make_profile(), {"option": 0, "source": "answers"})
        assert entry["source"] == "actitud" and entry["option"] == 0, text
    entry, _ = answer(closed("¿Sos alguien sociable?", "SI", "NO"), make_profile(), {"option": 1, "source": "answers"})
    assert entry["source"] == "unknown" and entry["reason"] == "sin fuente"  # un NO no entra por actitud


def test_the_cell_phone_technology_level_with_source_answers_is_pending():
    question = closed("¿Qué nivel de manejo de tecnología celular y aplicaciones considerás que tenés?", "Básico", "Intermedio", "Avanzado")
    entry, _ = answer(question, make_profile(), {"option": 1, "source": "answers"})
    assert entry["source"] == "unknown" and entry["reason"] == "sin fuente"


def test_answers_needs_a_key_the_code_can_name():
    prof = make_profile({"location": "Pilar, zona norte", "extra": "Intermedio"})
    entry, _ = answer(closed("¿Vivís en zona norte?", "Sí", "No"), prof, {"option": 0, "source": "answers"})
    assert entry["source"] == "answers" and apply.answers_key({"text": "¿Vivís en zona norte?", "kind": "closed", "options": []}, entry, prof["answers"]) == "location"
    question = closed("¿Qué nivel de tecnología tenés?", "Básico", "Intermedio")
    entry, _ = answer(question, prof, {"option": 1, "source": "answers"})
    assert entry["source"] == "answers"  # la respuesta repite un valor declarado


def test_cv_needs_support_from_the_cv():
    entry, _ = answer(closed("¿Tenés experiencia en call center?", "Sí", "No"), make_profile(), {"option": 1, "source": "cv"})
    assert entry["source"] == "cv"
    entry, _ = answer(closed("¿Conocés Python?", "Sí", "No"), make_profile(), {"option": 0, "source": "cv"})
    assert entry["source"] == "cv"  # el CV de ejemplo trae Python
    entry, _ = answer(closed("¿Tenés carnet de conducir profesional?", "Sí", "No"), make_profile(), {"option": 1, "source": "cv"})
    assert entry["source"] == "unknown" and entry["reason"] == "sin fuente"


def test_truths_are_never_touched_by_the_source_guard():
    prof = make_profile()
    prof["variants"][0] = {**prof["variants"][0], "truths": ["No soy monotributista"]}
    entry, _ = answer(closed("¿Sos monotributista?", "Sí", "No"), prof, {"option": 1, "source": "truths"})
    assert entry["source"] == "truths" and entry["option"] == 1


def test_the_report_shows_the_actitud_source(conn, tmp_path):
    entry, _ = answer(closed("¿Sos alguien sociable?", "SI", "NO"), make_profile(), {"option": 0, "source": "answers"})
    result = {"answers": [{"id16": "A", "title": "Oferta", "company": "Acme", "entries": [entry]}], "sent": [{"id16": "A"}]}
    assert "fuente: actitud" in "\n".join(apply.answer_section(result))


def test_the_prompt_is_a8_and_has_the_source_rules():
    assert apply.ANSWER_PROMPT_VERSION == "a15" and "«actitud»" in apply.ANSWER_SYSTEM and "monotributista" in apply.ANSWER_SYSTEM
