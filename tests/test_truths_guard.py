"""TAREA-086: la fuente «truths» no es un salvoconducto."""
import pytest

from test_apply_live import MODEL, FakeClient, reply
from test_human_answers import TODAY
from test_residence_experience import closed_q, open_q, profile
from compuapply import apply

BANK = ("Trabajé como vendedor puerta a puerta en una distribuidora", "No tengo cartera de clientes actual",
        "No tengo certificado de antecedentes penales actualizado")


def seller(truths=BANK, studies=(), skills=()):
    """Un CV falso con 7 meses de vendedor y un banco sin deporte, Photoshop ni rubro industrial."""
    prof = profile()
    var = prof["variants"][0]
    job = {"position": "Vendedor Preventista", "company": "Distribuidora Sur", "employment_period": "05/2026 - 11/2026",
           "key_responsibilities": ["Venta puerta a puerta"], "skills_acquired": []}
    resume = {**var["resume"], "experience_details": [job], "education_details": list(studies), "skills": list(skills)}
    prof["variants"][0] = {**var, "resume": resume, "truths": list(truths)}
    return prof


def run(question, prof, *items):
    client = FakeClient(reply(*items)) if items else FakeClient(reply({"source": "unknown"}))
    [entry] = apply.answer_questions([question], prof, client, MODEL, "dev", [], today=TODAY)
    return entry, client


def never_sent(entry):
    assert entry["source"] == "unknown" and entry["text"] is None and entry["option"] is None


def test_invented_open_claims_with_source_truths_are_pending():
    cases = [("¿Cuánta experiencia tenés en ventas puerta a puerta?", "Tengo más de 2 años de experiencia en ventas puerta a puerta."),
             ("¿Tenés experiencia en rubros industriales o hidráulica?", "Sí, cuento con experiencia comprobable en estos rubros."),
             ("¿Creaste tu propia cartera de clientes?", "Sí, creé mi propia cartera de clientes."),
             ("¿Practicás algún deporte o actividad?", "Soy apasionado por el deporte, especialmente el fútbol, y he jugado en equipos locales.")]
    for question, text in cases:
        entry, _ = run(open_q(question), seller(), {"text": text, "source": "truths"})
        never_sent(entry)


def test_closed_with_truths_but_no_fact_is_pending():
    for text in ("¿Manejás Photoshop o Illustrator?", "¿Tenés experiencia en rubros industriales?"):
        entry, _ = run(closed_q(text), seller(), {"option": 0, "source": "truths"})
        never_sent(entry)
        assert entry["reason"] == apply.TRUTH_UNBACKED


def test_a_truth_that_says_it_still_backs_the_answer():
    entry, _ = run(open_q("¿Trabajaste vendiendo puerta a puerta?"), seller(), {"text": "Sí, trabajé como vendedor puerta a puerta en una distribuidora.", "source": "truths"})
    assert entry["source"] == "truths" and entry["text"].startswith("Sí")


def test_a_year_that_the_cv_does_not_reach_is_not_claimed():
    prof = seller()
    months = apply.job_months(prof["variants"][0]["resume"], TODAY)
    assert apply._claim_short("Tengo más de 2 años de experiencia en ventas.", prof["variants"][0]["resume"], months)
    assert apply._claim_short("Tengo 1 año de experiencia en ventas.", prof["variants"][0]["resume"], months)
    assert not apply._claim_short("Tengo 7 meses de experiencia en ventas.", prof["variants"][0]["resume"], months)
    assert not apply._claim_short("Tengo 25 años.", prof["variants"][0]["resume"], months)
    entry, _ = run(open_q("¿Cuánto tiempo de experiencia tenés en ventas?"), seller(), {"text": "Tengo más de un año de experiencia en ventas puerta a puerta.", "source": "cv", "jobs": [0]})
    never_sent(entry)


def test_at_least_one_year_of_sales_with_seven_months_is_no():
    entry, _ = run(closed_q("¿Tenés al menos 1 año comprobable en ventas con objetivos de cierre?"), seller(), {"option": 0, "source": "cv", "jobs": []})
    assert entry["option"] == 1


def test_a_phone_never_reaches_the_model():
    for text in ("Transcriba su número de teléfono celular", "Dejanos tu WhatsApp o mail", "Ingresá tu CUIL y tu DNI"):
        client = FakeClient()
        [entry] = apply.answer_questions([open_q(text)], seller(), client, MODEL, "dev", [], today=TODAY)
        assert client.calls == [] and entry["source"] == "unknown" and "contestalo vos" in entry["reason"]


def test_the_brands_answer_loses_the_dumped_sentences():
    text = "Trabajé como vendedor en una distribuidora de marcas. Nunca trabajé para una consultora ni participé en selecciones para estaciones de servicio. Acepto hasta 1 hora y media de viaje."
    entry, _ = run(open_q("¿En qué marcas o concesionarios trabajaste?"), seller(), {"text": text, "source": "cv", "jobs": [0]})
    assert entry["source"] == "unknown" or not any(w in (entry["text"] or "") for w in ("consultora", "estaciones", "viaje"))
    entry, _ = run(open_q("¿En qué marcas o concesionarios trabajaste?"), seller(), {"text": "Nunca trabajé para una consultora ni participé en selecciones para estaciones de servicio.", "source": "truths"})
    never_sent(entry)


def test_the_on_topic_filter_keeps_what_answers():
    text, reason = apply._on_topic("Sí. Tengo una PC con Windows 10. Mi sueldo es bueno.", "¿Contás con PC con Windows 10?")
    assert reason is None and text == "Sí. Tengo una PC con Windows 10."
    assert apply._on_topic("Trabajé en un kiosco.", "¿Dónde trabajaste?") == ("Trabajé en un kiosco.", None)
    assert apply._on_topic("Hace dos años.", "¿Cuántos hijos tenés?")[1] == apply.NOT_ASKED


def test_a_thank_you_field_is_fixed_text():
    for text in ("Gracias por tu interés, se contactará a la brevedad", "Agradecemos tu postulación. Nos pondremos en contacto."):
        entry, client = run(open_q(text), seller())
        assert entry["source"] == "answers" and entry["text"] == "¡Gracias!" and client.calls == []
    assert apply.thanks_answer(open_q("¿Por qué querés trabajar acá? Gracias por tu interés")) is None


@pytest.mark.parametrize("model_answer", [{"option": 0, "source": "cv"}, {"option": 1, "source": "cv"}])
def test_a_list_option_only_in_skills_is_not_yes(model_answer):
    prof = seller(skills=["Diseño gráfico"])
    entry, _ = run(closed_q("¿Tenés experiencia en Iluminación o Construcción o Arquitectura o Diseño?"), prof, model_answer)
    assert entry["option"] != 0


def test_a_list_option_in_a_job_is_yes():
    prof = seller()
    prof["variants"][0]["resume"]["experience_details"][0]["key_responsibilities"] = ["Diseño de exhibidores"]
    entry, _ = run(closed_q("¿Tenés experiencia en Iluminación o Construcción o Arquitectura o Diseño?"), prof, {"option": 1, "source": "cv"})
    assert entry["option"] == 0


STUDIES = ({"education_level": "Tecnicatura", "field_of_study": "Programación", "institution": "ISFT", "year_of_completion": "2024"},)


def test_a_graduate_does_not_say_student_or_will_finish():
    resume = seller(studies=STUDIES)["variants"][0]["resume"]
    assert apply._study_conflict("Tecnicatura en Programación, que finalizaré en 2026.", resume, TODAY.__class__(2026, 11, 12))
    assert apply._study_conflict("Soy estudiante de Programación.", resume, TODAY)
    assert not apply._study_conflict("Soy técnico en Programación.", resume, TODAY)
    entry, _ = run(open_q("¿Qué estudios tenés en Programación?"), seller(studies=STUDIES), {"text": "Tecnicatura en Programación, que finalizaré en 2026.", "source": "cv"})
    never_sent(entry)


def test_conjugated_template_phrases_of_086():
    for text in ("Esto me permitió crecer.", "Fue una experiencia enriquecedora.", "Soy una persona orientada a resultados.", "Tengo un enfoque emprendedor."):
        assert apply.ai_hits(text), text
    assert apply.ANSWER_PROMPT_VERSION == "a15"
