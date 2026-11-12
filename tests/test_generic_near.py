"""TAREA-085: genéricas con puesto, cercanía, verdades que contestan, confirmación de lectura y muletillas por raíz."""
from test_apply_live import MODEL, FakeClient, make_profile, reply
from test_human_answers import TODAY
from test_residence_experience import closed_q, open_q, profile
from compuapply import apply


def run(question, prof, *items, calls=False):
    client = FakeClient(reply(*items)) if items else FakeClient(reply({"source": "unknown"}))
    [entry] = apply.answer_questions([question], prof, client, MODEL, "dev", [], today=TODAY)
    return (entry, client) if calls else entry


def with_truths(prof, *truths):
    prof["variants"][0] = {**prof["variants"][0], "truths": list(truths)}
    return prof


SALES = closed_q("¿Tienes experiencia en ventas comprobable?")


def test_a_seller_title_backs_the_generic_sales_experience():
    entry = run(SALES, profile(position="Vendedor Preventista"), {"option": 0, "source": "cv"})
    assert entry["option"] == 0 and entry["source"] == "cv"


def test_without_a_sales_job_the_yes_still_turns_into_no():
    entry = run(SALES, profile(position="Cajero"), {"option": 0, "source": "cv"})
    assert entry["option"] == 1


def test_proximity_to_another_place_is_pending_never_yes():
    prof = profile()
    for text in ("¿Vivís cerca de San Isidro, BSAS?", "¿Vivís a menos de 10 km de Caseros?"):
        for q in (closed_q(text), open_q(text)):
            entry, client = run(q, prof, calls=True)
            assert entry["source"] == "unknown" and entry["option"] is None and entry["reason"].startswith("cercanía") and client.calls == []
    entry = run(closed_q("¿Vivís cerca de Quilmes?"), prof)
    assert entry["option"] == 0 and entry["source"] == "answers"
    assert run(open_q("¿Vivís cerca de Quilmes, provincia de Buenos Aires?"), prof)["text"].startswith("Sí")


def test_a_contradicting_truth_answers_with_its_polarity():
    prof = with_truths(profile(), "No tengo experiencia en análisis de datos")
    entry = run(closed_q("¿Tienes experiencia en análisis de datos de ventas?"), prof, {"option": 0, "source": "cv"})
    assert entry["option"] == 1 and entry["source"] == "truths" and entry["reason"] is None


def test_an_open_question_with_a_related_truth_names_it_to_the_model():
    prof = with_truths(profile(), "Emprendí un negocio de venta de ropa online")
    entry, client = run(open_q("¿Tuviste oportunidad de emprender? ¿En qué?"), prof,
                        {"text": "Sí, emprendí un negocio de venta de ropa online.", "source": "truths"}, calls=True)
    assert "Emprendí un negocio de venta de ropa online" in str(client.calls[0]) and entry["source"] == "truths"


def test_a_related_truth_does_not_ask_again_if_the_model_says_unknown():  # TAREA-086: sin segundo pedido
    prof = with_truths(profile(), "Emprendí un negocio de venta de ropa online")
    client = FakeClient(reply({"source": "unknown"}), reply({"text": "Sí, emprendí un negocio de venta de ropa online.", "source": "truths"}))
    [entry] = apply.answer_questions([open_q("¿Tuviste oportunidad de emprender? ¿En qué?")], prof, client, MODEL, "dev", [], today=TODAY)
    assert len(client.calls) == 1 and entry["source"] == "unknown"


def test_the_reading_confirmation_is_fixed_text():
    for text in ("Confirmá que has leído esta indicación y podrás realizar las actividades", "Confirma que leíste el aviso", "Confirmar lectura"):
        entry, client = run(open_q(text), profile(), calls=True)
        assert entry["source"] == "answers" and entry["text"] == "Sí, lo leí y puedo hacerlo." and client.calls == []


def test_conjugated_template_phrases_are_found():
    for text in ("Trabajé como mozo y garantizaba la satisfacción del cliente.", "Aseguraba una buena experiencia para los clientes.",
                 "Lo que me permitió desarrollar habilidades en atención.", "Ofrecía soluciones personalizadas.", "Mi enfoque estaba en vender.",
                 "Mantenía una comunicación fluida y efectiva.", "Logré ventas efectivas.", "Puedo comunicarme en este idioma.",
                 "Atendí mesas. Sin embargo, aprendí."):
        assert apply.ai_hits(text), text
    assert apply.ai_hits("Atendí mesas en un restaurante.") == []
