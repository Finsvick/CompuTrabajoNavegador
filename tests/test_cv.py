"""Tests de cv.py y de `cv make`. CV de examples/, cliente, renderizador y Playwright falsos: ni red ni navegador."""
import json
import sqlite3
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace

import httpx
import openai
import pytest
import yaml

from compuapply import cli, cv, db, profile, score, ui_logic

ROOT = Path(__file__).parent.parent
RESUME = yaml.safe_load((ROOT / "examples" / "resume.example.yaml").read_text(encoding="utf-8"))
REQUEST = httpx.Request("POST", "https://api.openai.com/v1/responses")
OFFER_ID = f"{1:016X}"
PERSONAL = ["Giovanni", "Bianchi", "12/02/1988", "Via Nazionale", "3345678901", "giovanni.bianchi@example.com",
            "github.com/giovanni-bianchi", "linkedin.com/in/giovanni-bianchi", "+39", "Rome"]
PROFILE = {"variants": [{"id": "backend", "keywords": [], "resume": RESUME, "pdf": None}],
           "run": {"max_cost_month": 2.0}, "apply": {"threshold": 75, "daily_cap": 10}}


def good(**over) -> dict:
    """Respuesta válida para el CV de ejemplo: lo justo de cada experiencia y dos de cada lista."""
    data = {"experiences": [{"experience": "E1", "block": "relevante", "responsibilities": ["E1.R3", "E1.R1"], "skills": ["E1.S2"]},
                            {"experience": "E2", "block": "relevante", "responsibilities": ["E2.R2"], "skills": []},
                            {"experience": "E3", "block": "relevante", "responsibilities": ["E3.R1"], "skills": ["E3.S3", "E3.S1"]}],
            "technical_skills": ["T3", "T1"], "projects": ["P2"], "achievements": ["A1"], "certifications": ["C2", "C1"], "interests": ["I3"]}
    return {**data, **over}


def entry(experience, responsibilities=(), skills=(), block="relevante") -> dict:
    return {"experience": experience, "block": block, "responsibilities": list(responsibilities), "skills": list(skills)}


def bullet(source, text) -> dict:
    return {"source": source, "text": text}


def entry5(experience, bullets=(), skills=(), block="relevante") -> dict:
    return {"experience": experience, "block": block, "bullets": list(bullets), "skills": list(skills)}


def good5(**over) -> dict:
    """Respuesta válida con texto adaptado: sin siglas, números ni palabras de nivel que no estén en el CV de ejemplo."""
    data = {"headline": "Desarrollador de software", "summary": {"text": "Desarrollador con experiencia en liderazgo de equipos.", "sources": ["E1.R1"]},
            "experiences": [entry5("E1", [bullet("E1.R1", "Lideré equipos para construir soluciones empresariales"),
                                          bullet("E1.R3", "Mejoré el rendimiento de las aplicaciones")], ["E1.S2"]),
                            entry5("E2", [bullet("E2.R2", "Colaboré con diseñadores para mejorar la experiencia de usuario")]),
                            entry5("E3", [bullet("E3.R1", "Participé en el desarrollo de aplicaciones móviles y web")], ["E3.S3", "E3.S1"])],
            "technical_skills": ["T3", "T1"], "projects": ["P2"], "achievements": ["A1"], "certifications": ["C2", "C1"], "interests": ["I3"]}
    return {**data, **over}


def response(data=None, *, status="completed", tokens=(1000, 0, 200)):
    return SimpleNamespace(
        output_text=json.dumps(good5() if data is None else data), output=[], status=status,
        usage=SimpleNamespace(input_tokens=tokens[0], input_tokens_details=SimpleNamespace(cached_tokens=tokens[1]),
                              output_tokens=tokens[2]))


class FakeClient:
    def __init__(self, *responses):
        self.calls, self._responses = [], list(responses) or [response()]
        self.responses = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        item = self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]
        if isinstance(item, Exception):
            raise item
        return item


class FakeRender:
    """Renderizador falso: escribe un PDF mínimo (o lo que se le pida) donde se le indica."""

    def __init__(self, size=2048):
        self.size, self.calls = size, []

    def __call__(self, html_path: Path, pdf_path: Path) -> None:
        self.calls.append((html_path, pdf_path))
        assert html_path.read_text(encoding="utf-8").startswith("<!doctype html>")
        pdf_path.write_bytes(b"%PDF-" + b"0" * (self.size - 5))


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / "t.db")
    yield connection
    connection.close()


def add_offer(conn, n=1, *, points=85, model="gpt-6-luna", status=None, variant="backend") -> str:
    id16 = f"{n:016X}"
    db.upsert_offers(conn, [{"id16": id16, "data_id": f"{n:032X}", "title": "Desarrollador Backend", "company": "Empresa Falsa SA",
                             "location": "Rosario", "url": f"https://ar.computrabajo.com/ofertas-de-trabajo/o-{n}"}], "x")
    db.save_detail(conn, id16, {"description": "Buscamos backend con Python y AWS.", "date_posted": "", "valid_through": "",
                                "employment_type": "Full-time"})
    if model:
        db.save_score(conn, id16, model, score.PROMPT_VERSION,
                      {"best_variant": variant, "score": points, "all_scores": {variant: points}, "reasons": [], "missing": []},
                      (1, 0, 1), 0.0)
    if status:
        db.mark_status(conn, id16, status)
    return id16


def make(conn, tmp_path, client=None, *, id16=OFFER_ID, render=None, model="gpt-6-luna", prof=PROFILE):
    return cv.make(conn, prof, client or FakeClient(), model, id16, out_dir=tmp_path / "cvs", render=render or FakeRender(),
                   stop_file=tmp_path / "STOP")


def nothing_saved(conn, tmp_path):
    assert not (tmp_path / "cvs").exists() or list((tmp_path / "cvs").iterdir()) == []
    assert conn.execute("SELECT COUNT(*) FROM cvs").fetchone()[0] == 0


# ── items ────────────────────────────────────────────────────────────────────

def test_items_have_stable_ids_by_position_and_the_text_of_the_base_cv():
    found = cv.items(RESUME)
    assert found["E1"] == "Senior Software Engineer" and found["E3"] == "Junior Developer"
    assert found["E1.R2"] == "Architected scalable systems to handle high-volume data processing"
    assert found["E2.S3"] == "Automated testing" and found["E3.S1"] == "Mobile app development"
    assert found["P1"] == ("E-Commerce Platform: Developed a scalable e-commerce platform with advanced features like "
                           "real-time inventory tracking and user analytics")
    assert found["A2"].startswith("Best Young Developer: Awarded for") and found["C5"].startswith("Certified Data Privacy")
    assert found["I1"] == "Cloud Computing" and found["I5"] == "Data Privacy"
    assert found["T1"] == "Python" and found["T3"] == "AWS (EC2, S3, Lambda)" and found["T6"] == "Terraform"
    assert len(found) == 3 + 9 + 9 + 6 + 2 + 2 + 5 + 5 and "link" not in " ".join(found.values())


def test_items_skip_empty_entries_and_accept_plain_responsibilities():
    resume = {"experience_details": [{"position": "Dev", "key_responsibilities": ["Hizo algo", {"r1": "Hizo otra cosa"}, ""]}]}
    assert cv.items(resume) == {"E1": "Dev", "E1.R1": "Hizo algo", "E1.R2": "Hizo otra cosa"}
    assert cv.items({}) == {}


# ── el HTML solo tiene texto del CV base ─────────────────────────────────────

class TextNodes(HTMLParser):
    """Los textos del HTML fuera de la plantilla: se ignoran los encabezados de sección, `title` y `style`."""
    SKIP = {"h2", "h4", "title", "style"}

    def __init__(self):
        super().__init__()
        self.stack, self.texts = [], []

    def handle_starttag(self, tag, attrs):
        self.stack.append(tag)

    def handle_endtag(self, tag):
        self.stack.pop()

    def handle_data(self, data):
        if data.strip() and not self.SKIP & set(self.stack):
            self.texts.append(data.strip())


# Lo único que la plantilla suma al texto del CV base: los niveles de idioma traducidos y la marca « (en curso)».
EXTRA_TEXTS = set(cv.LEVELS.values()) | {f"{year} (en curso)" for year in range(2000, 3000)}


def leaves(value) -> set[str]:
    if isinstance(value, dict):
        return {leaf for v in value.values() for leaf in leaves(v)}
    if isinstance(value, list):
        return {leaf for v in value for leaf in leaves(v)}
    return {str(value)}


def test_every_text_of_the_html_is_an_item_of_the_base_cv_or_a_personal_data():
    selection = cv.validate(cv._parse(RESUME), good())
    page = cv.build_html(RESUME, selection)
    nodes = TextNodes()
    nodes.feed(page)
    allowed = leaves({key: RESUME[key] for key in ("personal_information", "education_details", "experience_details",
                                                   "technical_skills", "projects", "achievements", "certifications", "languages", "interests")}) | EXTRA_TEXTS
    assert nodes.texts and [t for t in nodes.texts if t not in allowed] == []
    shown = set(nodes.texts)
    assert set(RESUME["personal_information"].values()) <= shown  # datos personales: siempre, completos
    assert {e["position"] for e in RESUME["experience_details"]} <= shown  # todas las experiencias
    assert {"Master's Degree", "Computer Engineering", "University of Rome", "Italian", "Nativo", "English", "Fluido"} <= shown
    assert "Native" not in shown and "Fluent" not in shown
    items = cv.items(RESUME)
    for id_ in ("E1.R3", "E1.R1", "E1.S2", "E2.R2", "E3.S3", "E3.S1", "I3", "T3", "T1"):
        assert items[id_] in shown
    assert "Led a team of developers" in page and items["E1.R2"] not in shown  # lo no elegido no sale
    assert "Smart Home Automation" in shown and "E-Commerce Platform" not in shown and "Top Innovator Award" in shown


def test_the_html_keeps_the_order_chosen_by_the_model_and_the_base_order_of_experiences():
    page = cv.build_html(RESUME, cv.validate(cv._parse(RESUME), good()))
    assert page.index("Optimized application performance") < page.index("Led a team of developers")  # R3 antes que R1
    assert page.index("Senior Software Engineer") < page.index("Software Developer") < page.index("Junior Developer")
    assert page.index("<h2>Experiencia relevante</h2>") < page.index("<h2>Conocimientos técnicos</h2>") < page.index("<h2>Proyectos</h2>")
    assert page.index("AWS (EC2, S3, Lambda)") < page.index("<li>Python</li>")  # T3 antes que T1
    assert page.index("Certification for DevOps") < page.index("Certification for expertise in ethical")  # C2 antes que C1
    swapped = good(experiences=list(reversed(good()["experiences"])))
    assert cv.build_html(RESUME, cv.validate(cv._parse(RESUME), swapped)) == page  # el orden de las experiencias lo da el CV base


def test_the_html_is_one_column_a4_with_inline_css_and_no_external_resources():
    page = cv.build_html(RESUME, cv.validate(cv._parse(RESUME), good()))
    assert page.startswith("<!doctype html>") and 'lang="es"' in page and "size: A4" in page
    assert "<style>" in page and "<h2>Experiencia relevante</h2>" in page and "<h2>Educación</h2>" in page
    for forbidden in ("src=", "href=", "<link", "<script", "@import", "url(", "http://"):
        assert forbidden not in page
    assert page.count("https://") == 2  # solo los links de personal_information, como texto: nada se carga


def test_the_html_escapes_the_text_of_the_cv():
    resume = {"personal_information": {"name": "<b>Ana</b>"}, "experience_details": [{"position": "Dev & <i>Ops</i>",
              "key_responsibilities": [{"r1": "Hizo <script>alert(1)</script>"}]}]}
    page = cv.build_html(resume, cv.validate(cv._parse(resume), {"experiences": [
        {"experience": "E1", "block": "relevante", "responsibilities": ["E1.R1"], "skills": []}], "technical_skills": [], "projects": [], "achievements": [],
        "certifications": [], "interests": []}))
    assert "<script>" not in page and "<b>" not in page and "&lt;script&gt;" in page and "Dev &amp; &lt;i&gt;Ops" in page


# ── dos bloques: experiencia relevante y otra experiencia ────────────────────

def page_with(*entries) -> str:
    selection = cv.validate(cv._parse(RESUME), good(experiences=list(entries)))
    return cv.build_html(RESUME, selection)


def block(page: str, title: str) -> str:
    return page.split(f"<h2>{title}</h2>", 1)[1].split("</section>", 1)[0]


def test_an_experience_the_model_did_not_name_goes_to_other_experience_without_error_or_bullets():
    page = page_with(entry("E1", ["E1.R3", "E1.R1"], ["E1.S2"]))
    relevant, other = block(page, "Experiencia relevante"), block(page, "Otra experiencia")
    assert "Senior Software Engineer" in relevant and "Led a team of developers" in relevant and "Team leadership" in relevant
    assert "Software Developer" not in relevant and "Junior Developer" not in relevant
    assert page.index("<h2>Experiencia relevante</h2>") < page.index("<h2>Otra experiencia</h2>")
    assert "Software Developer" in other and "Innovatech" in other and "Milan, Italy" in other and "Technology" in other
    assert "Junior Developer" in other and "StartUp Hub" in other and "01/2014 - 05/2015" in other
    assert "<li>" not in other and "<ul>" not in other and "Habilidades" not in other
    assert other.index("Software Developer") < other.index("Junior Developer")  # orden del CV base
    assert "Collaborated with UX/UI designers" not in page and "Web development" not in page  # nada de E2 salvo puesto y meta


def test_an_other_block_experience_ignores_its_bullets_and_a_relevant_one_without_responsibilities_goes_to_other():
    page = page_with(entry("E1", ["E1.R1"]), entry("E2", ["E2.R1"], ["E2.S1"], block="otra"), entry("E3", [], ["E3.S1"]))
    assert "Senior Software Engineer" in block(page, "Experiencia relevante")
    other = block(page, "Otra experiencia")
    assert "Software Developer" in other and "Junior Developer" in other and "<li>" not in other
    assert "Developed and maintained web applications" not in page and "Mobile app development" not in page


def test_with_no_relevant_experience_everything_is_under_experiencia():
    page = page_with(entry("E1", ["E1.R1"], block="otra"))
    assert "<h2>Experiencia relevante</h2>" not in page and "<h2>Otra experiencia</h2>" not in page
    body = block(page, "Experiencia")
    assert all(position in body for position in ("Senior Software Engineer", "Software Developer", "Junior Developer"))
    assert "<li>" not in body
    assert block(cv.build_html(RESUME, cv.validate(cv._parse(RESUME), good(experiences=[]))), "Experiencia") == body


def test_with_every_experience_relevant_there_is_no_other_block():
    page = cv.build_html(RESUME, cv.validate(cv._parse(RESUME), good()))
    assert "<h2>Experiencia relevante</h2>" in page and "Otra experiencia" not in page


def test_every_text_of_the_html_is_still_from_the_base_cv_with_two_blocks():
    nodes = TextNodes()
    nodes.feed(page_with(entry("E1", ["E1.R3"], ["E1.S1"])))
    allowed = leaves({key: RESUME[key] for key in ("personal_information", "education_details", "experience_details",
                                                   "technical_skills", "projects", "achievements", "certifications", "languages", "interests")}) | EXTRA_TEXTS
    assert nodes.texts and [t for t in nodes.texts if t not in allowed] == []


def test_the_schema_asks_for_the_block_of_each_experience():
    entry_schema = cv._schema(cv._parse(RESUME))["properties"]["experiences"]["items"]
    assert entry_schema["properties"]["block"]["enum"] == ["relevante", "otra"] and "block" in entry_schema["required"]
    assert "relevante" in cv.SYSTEM and "otra" in cv.SYSTEM


# ── TAREA-027: orden, estudios en curso e idiomas ───────────────────────────

def resume_with(**changes) -> dict:
    import copy
    resume = copy.deepcopy(RESUME)
    resume.update(changes)
    return resume


def positions(text: str) -> list[str]:
    return [p for p in (e["position"] for e in RESUME["experience_details"]) if p in text]


def test_experiences_go_from_newest_to_oldest_in_each_block_and_undated_ones_last():
    shuffled = resume_with(experience_details=[
        {**RESUME["experience_details"][2], "position": "Viejo", "employment_period": "01/2014 - 05/2015"},
        {**RESUME["experience_details"][1], "position": "Sin fecha", "employment_period": "desde siempre"},
        {**RESUME["experience_details"][0], "position": "Nuevo", "employment_period": "03/2020 - Present"},
        {**RESUME["experience_details"][1], "position": "Solo año", "employment_period": "2017"},
        {**RESUME["experience_details"][2], "position": "Sin período", "employment_period": ""},
        {**RESUME["experience_details"][0], "position": "Mismo año, mes mayor", "employment_period": "09/2017 - 12/2018"},
    ])
    parsed = cv._parse(shuffled)
    entries = [{"experience": e["id"], "block": "otra", "responsibilities": [], "skills": []} for e in parsed["experiences"]]
    entries[2:3] = [{"experience": "E3", "block": "relevante", "responsibilities": ["E3.R1"], "skills": []}]
    entries[5:6] = [{"experience": "E6", "block": "relevante", "responsibilities": ["E6.R1"], "skills": []}]
    entries[0:1] = [{"experience": "E1", "block": "relevante", "responsibilities": ["E1.R1"], "skills": []}]
    page = cv.build_html(shuffled, cv.validate(parsed, good(experiences=entries)))
    relevant, other = block(page, "Experiencia relevante"), block(page, "Otra experiencia")
    order = lambda text, names: sorted(names, key=text.index)  # noqa: E731
    assert order(relevant, ["Nuevo", "Mismo año, mes mayor", "Viejo"]) == ["Nuevo", "Mismo año, mes mayor", "Viejo"]
    assert order(other, ["Solo año", "Sin fecha", "Sin período"]) == ["Solo año", "Sin fecha", "Sin período"]  # sin fecha, al final y en su orden


def test_the_example_experiences_keep_their_order_when_already_newest_first():
    page = cv.build_html(RESUME, cv.validate(cv._parse(RESUME), good(experiences=[entry("E3", ["E3.R1"], block="otra")])))
    assert positions(page) == ["Senior Software Engineer", "Software Developer", "Junior Developer"]


def test_education_goes_from_the_latest_year_and_a_future_end_year_says_en_curso():
    studies = [
        {"education_level": "Grado", "institution": "Vieja", "year_of_completion": "2005"},
        {"education_level": "Sin año", "institution": "X", "year_of_completion": ""},
        {"education_level": "Maestría", "institution": "Futura", "year_of_completion": "2999"},
        {"education_level": "Posgrado", "institution": "Media", "year_of_completion": 2012},
        {"education_level": "Curso", "institution": "Y"},
    ]
    resume = resume_with(education_details=studies)
    page = cv.build_html(resume, cv.validate(cv._parse(resume), good()))
    body = block(page, "Educación")
    assert [name for name in ("Maestría", "Posgrado", "Grado", "Sin año", "Curso") if name in body] == sorted(
        ("Maestría", "Posgrado", "Grado", "Sin año", "Curso"), key=body.index) == ["Maestría", "Posgrado", "Grado", "Sin año", "Curso"]
    assert "<span>2999 (en curso)</span>" in body and "<span>2005</span>" in body and "<span>2012</span>" in body
    assert body.count("(en curso)") == 1
    assert resume["education_details"][2]["year_of_completion"] == "2999"  # el CV base no se toca


def test_language_levels_are_translated_with_a_fixed_table_and_anything_else_stays():
    levels = {"Native": "Nativo", "fluent": "Fluido", "ADVANCED": "Avanzado", "Upper Intermediate": "Intermedio alto",
              "Intermediate": "Intermedio", "basic": "Básico", "Beginner": "Principiante", "Mother tongue": "Mother tongue", "C1": "C1"}
    resume = resume_with(languages=[{"language": f"L{n}", "proficiency": level} for n, level in enumerate(levels)])
    page = cv.build_html(resume, cv.validate(cv._parse(resume), good()))
    for n, translated in enumerate(levels.values()):
        assert f"<span>L{n}</span><span>{translated}</span>" in page
    assert set(cv.LEVELS.values()) == {
        "Nativo", "Fluido", "Avanzado", "Intermedio alto", "Intermedio", "Básico", "Principiante"}


# ── verificación de la respuesta ─────────────────────────────────────────────

BAD = [
    ("id inexistente", good(projects=["P9"]), "«P9»"),
    ("id inexistente en una experiencia", good(experiences=[entry("E1", ["E1.R9"])]), "«E1.R9»"),
    ("experiencia inexistente", good(experiences=[entry("E4")]), "no existe en el CV base"),
    ("id repetido", good(interests=["I1", "I1"]), "repitió «I1»"),
    ("conocimiento inexistente", good(technical_skills=["T9"]), "«T9» en conocimientos técnicos"),
    ("conocimiento repetido", good(technical_skills=["T2", "T2"]), "repitió «T2»"),
    ("responsabilidad repetida", good(experiences=[entry("E1", ["E1.R1", "E1.R1"])]), "repitió «E1.R1»"),
    ("E1.R1 dentro de E2", good(experiences=[entry("E2", ["E1.R1"])]), "«E1.R1» en la experiencia E2"),
    ("E1.R1 dentro de E2 aunque sea «otra»", good(experiences=[entry("E2", ["E1.R1"], block="otra")]), "«E1.R1» en la experiencia E2"),
    ("habilidad de otra experiencia", good(experiences=[entry("E1", ["E1.R1"], ["E2.S1"])]), "«E2.S1»"),
    ("experiencia repetida", good(experiences=[entry("E1", ["E1.R1"]), entry("E1", ["E1.R2"])]), "repitió la experiencia E1"),
    ("bloque desconocido", good(experiences=[entry("E1", ["E1.R1"], block="media")]), "formato esperado"),
    ("id de otra categoría", good(projects=["E1"]), "«E1» en proyectos"),
    ("sin una lista", {k: v for k, v in good().items() if k != "interests"}, "formato esperado"),
]


@pytest.mark.parametrize("name, data, message", BAD, ids=[b[0] for b in BAD])
def test_the_ids_only_verification_gives_a_clear_error(name, data, message):
    with pytest.raises(RuntimeError, match="No se armó el CV") as err:
        cv.validate(cv._parse(RESUME), data)
    assert message in str(err.value) and str(err.value).endswith("No se guardó nada.")


@pytest.mark.parametrize("bad, message", [
    (SimpleNamespace(output_text="esto no es json", output=[], status="completed", usage=None), "JSON válido"),
    (response(status="incomplete"), "no terminó"),
    (SimpleNamespace(output_text="{}", output=[SimpleNamespace(type="refusal")], status="completed", usage=None), "rechazó"),
])
def test_an_unusable_answer_is_asked_again_once_and_then_saves_nothing(conn, tmp_path, bad, message):
    add_offer(conn)
    client = FakeClient(bad)
    with pytest.raises(RuntimeError, match=message):
        make(conn, tmp_path, client)
    nothing_saved(conn, tmp_path)
    assert len(client.calls) == 2 and conn.execute("SELECT COUNT(*), SUM(ok) FROM api_spend").fetchone() == (2, 0)  # las dos se pagaron


# ── el pedido a la API ───────────────────────────────────────────────────────

@pytest.mark.parametrize("model, expected", [
    ("gpt-4o-mini", {"temperature": 0}),
    ("gpt-6-luna", {"reasoning": {"effort": "low"}}),
    ("gpt-5-nano", {"reasoning": {"effort": "low"}}),
])
def test_the_request_has_no_personal_data_and_uses_the_same_rule_as_the_score(conn, tmp_path, model, expected):
    add_offer(conn, model=model)
    client = FakeClient()
    make(conn, tmp_path, client, model=model)
    [call] = client.calls
    assert {k: call[k] for k in ("temperature", "reasoning") if k in call} == expected
    sent = json.dumps(call, ensure_ascii=False)
    assert [value for value in PERSONAL if value in sent] == []
    assert "Desarrollador Backend" in sent and "E1.R2: Architected scalable systems" in sent and "P1: E-Commerce Platform" in sent
    assert "T1: Python" in sent and "T3: AWS (EC2, S3, Lambda)" in sent
    assert call["text"]["format"]["strict"] is True and call["text"]["format"]["name"] == "cv_adaptado"
    assert call["max_output_tokens"] == cv.MAX_CV_TOKENS
    assert call["model"] == model


def test_the_schema_asks_for_headline_summary_bullets_with_source_and_ids_of_the_base_cv():
    schema = cv._schema(cv._parse(RESUME, ["Usé Node.js en la tecnicatura"]))
    assert schema["additionalProperties"] is False and set(schema["required"]) == {
        "headline", "summary", "experiences", "technical_skills", "projects", "achievements", "certifications", "interests"}
    assert schema["properties"]["headline"] == {"type": "string"}
    assert schema["properties"]["summary"]["required"] == ["text", "sources"]
    assert schema["properties"]["projects"]["items"]["enum"] == ["P1", "P2"]
    assert schema["properties"]["technical_skills"]["items"]["enum"] == ["T1", "T2", "T3", "T4", "T5", "T6", "X1"]  # el banco también
    entry_schema = schema["properties"]["experiences"]["items"]
    assert entry_schema["properties"]["experience"]["enum"] == ["E1", "E2", "E3"]
    assert "responsibilities" not in entry_schema["properties"] and entry_schema["required"] == ["experience", "block", "bullets", "skills"]
    sources = entry_schema["properties"]["bullets"]["items"]["properties"]["source"]["enum"]
    assert "E2.R1" in sources and "X1" in sources and "E1.S1" not in sources
    empty = cv._schema(cv._parse({"experience_details": [{"position": "Dev"}]}))
    assert empty["properties"]["projects"]["maxItems"] == 0 and empty["properties"]["interests"]["maxItems"] == 0
    assert empty["properties"]["technical_skills"]["maxItems"] == 0  # sin la sección en el CV base
    assert empty["properties"]["experiences"]["items"]["properties"]["bullets"]["maxItems"] == 0  # ni responsabilidades ni banco


# ── make ─────────────────────────────────────────────────────────────────────

def test_make_saves_html_pdf_and_the_row_and_costs_are_exact(conn, tmp_path):
    add_offer(conn)
    render = FakeRender()
    info = make(conn, tmp_path, render=render)
    assert info["html"] == tmp_path / "cvs" / f"{OFFER_ID}-backend.html" and info["pdf"] == tmp_path / "cvs" / f"{OFFER_ID}-backend.pdf"
    assert info["html"].read_text(encoding="utf-8").startswith("<!doctype html>") and info["pdf"].read_bytes().startswith(b"%PDF-")
    assert sorted(p.name for p in (tmp_path / "cvs").iterdir()) == sorted(
        [info["html"].name, info["pdf"].name, f"{OFFER_ID}-backend.revision.html"])  # sin temporales
    assert info["revision"] == cv.revision_path(info["html"]) and info["revision"].is_file()
    assert info["version"] == "c8" and info["fallback_reason"] is None
    cost = (1000 * 0.10 + 200 * 0.50) / 1_000_000
    assert info["cost_usd"] == pytest.approx(cost) and (info["input_tokens"], info["output_tokens"]) == (1000, 200)
    row = conn.execute("SELECT id16, variant, model, prompt_version, html, pdf, cost_usd, input_tokens, output_tokens FROM cvs").fetchone()
    assert row == (OFFER_ID, "backend", "gpt-6-luna", "c8", str(info["html"]), str(info["pdf"]), pytest.approx(cost), 1000, 200)
    assert cv.CV_PROMPT_VERSION == "c8" and cv.existing(conn, OFFER_ID)["pdf"] == str(info["pdf"])
    assert cv.existing(conn, f"{2:016X}") is None


def test_make_again_replaces_the_cv_of_that_offer_and_variant(conn, tmp_path):
    add_offer(conn)
    make(conn, tmp_path)
    make(conn, tmp_path)
    assert conn.execute("SELECT COUNT(*) FROM cvs").fetchone()[0] == 1 and len(list((tmp_path / "cvs").iterdir())) == 3


def test_make_uses_the_best_variant_of_the_offer(conn, tmp_path):
    add_offer(conn, variant="datos")
    two = {**PROFILE, "variants": [*PROFILE["variants"], {"id": "datos", "keywords": [], "resume": RESUME, "pdf": None}]}
    assert make(conn, tmp_path, prof=two)["variant"] == "datos"
    add_offer(conn, 2, variant="ya-no-existe")
    with pytest.raises(RuntimeError, match="ya no está en profile.yaml"):
        make(conn, tmp_path, id16=f"{2:016X}")


@pytest.mark.parametrize("setup, message", [
    (lambda conn: None, "no tiene puntaje de gpt-6-luna v5"),  # sin ninguna oferta con ese id
    (lambda conn: add_offer(conn, model=None), "no tiene puntaje de gpt-6-luna v5"),
    (lambda conn: add_offer(conn, model="gpt-4o-mini"), "no tiene puntaje de gpt-6-luna v5"),
    (lambda conn: (add_offer(conn), conn.execute("UPDATE scores SET prompt_version = 'v1'")), "no tiene puntaje de gpt-6-luna v5"),
    (lambda conn: add_offer(conn, status="duplicate"), "es una duplicada"),
])
def test_make_without_a_current_score_or_with_a_duplicate_does_not_call_the_api(conn, tmp_path, setup, message):
    setup(conn)
    client = FakeClient()
    with pytest.raises(RuntimeError, match="No existe la oferta|" + message):
        make(conn, tmp_path, client)
    assert client.calls == []
    nothing_saved(conn, tmp_path)


def test_make_with_the_monthly_cap_reached_does_not_call_the_api(conn, tmp_path):
    add_offer(conn)
    db.add_spend(conn, "cv", f"{99:016X}", "gpt-6-luna", 2.0, 1, 1, False)  # un CV que no sirvió ya gastó el tope
    client = FakeClient()
    with pytest.raises(RuntimeError, match="Tope mensual alcanzado: US\\$ 2,0000 de US\\$ 2,00"):
        make(conn, tmp_path, client)
    assert client.calls == []


def test_make_stops_with_the_stop_file_and_checks_the_model_price(conn, tmp_path):
    add_offer(conn)
    client = FakeClient()
    (tmp_path / "STOP").write_text("", encoding="utf-8")
    with pytest.raises(RuntimeError, match="Existe el archivo"):
        make(conn, tmp_path, client)
    (tmp_path / "STOP").unlink()
    with pytest.raises(RuntimeError, match="No tengo el precio del modelo 'gpt-x'"):
        make(conn, tmp_path, client, model="gpt-x")
    assert client.calls == []


def test_api_errors_never_show_the_text_of_the_message(conn, tmp_path):
    add_offer(conn)
    error = openai.AuthenticationError("clave sk-test-SECRETO mala", response=httpx.Response(401, request=REQUEST),
                                       body={"code": "invalid_api_key"})
    with pytest.raises(RuntimeError) as err:
        make(conn, tmp_path, FakeClient(error))
    assert "401" in str(err.value) and "invalid_api_key" in str(err.value) and "sk-test-SECRETO" not in str(err.value)
    nothing_saved(conn, tmp_path)


@pytest.mark.parametrize("size", [cv.MAX_PDF_BYTES, cv.MAX_PDF_BYTES + 1])
def test_a_pdf_that_is_too_big_saves_nothing(conn, tmp_path, size):
    add_offer(conn)
    with pytest.raises(RuntimeError, match="pesa 5 MB o más"):
        make(conn, tmp_path, render=FakeRender(size))
    nothing_saved(conn, tmp_path)


def test_a_failing_renderer_leaves_no_files_nor_row(conn, tmp_path):
    add_offer(conn)

    def broken(html_path, pdf_path):
        pdf_path.write_bytes(b"%PDF-incompleto")
        raise RuntimeError("Chromium no abrió")

    with pytest.raises(RuntimeError, match="Chromium no abrió"):
        make(conn, tmp_path, render=broken)
    nothing_saved(conn, tmp_path)


# ── el renderizador real, con Playwright falso ───────────────────────────────

class FakePlaywright:
    def __init__(self):
        self.log = []
        page = SimpleNamespace(goto=lambda url: self.log.append(("goto", url)),
                               pdf=lambda **kw: (self.log.append(("pdf", kw)), Path(kw["path"]).write_bytes(b"%PDF-1")))
        self.context = SimpleNamespace(new_page=lambda: page, route=lambda pattern, handler: self.log.append(("route", pattern, handler)))
        self.browser = SimpleNamespace(new_context=lambda **kw: (self.log.append(("new_context", kw)), self.context)[1],
                                       close=lambda: self.log.append(("close",)))
        self.chromium = SimpleNamespace(
            launch=lambda **kw: (self.log.append(("launch", kw)), self.browser)[1],
            launch_persistent_context=lambda *a, **kw: pytest.fail("no se abre el perfil persistente"))

    def __call__(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_the_real_renderer_uses_a_new_headless_context_without_the_persistent_profile(tmp_path):
    html_file = tmp_path / "a.html"
    html_file.write_text("<!doctype html><p>hola</p>", encoding="utf-8")
    fake = FakePlaywright()
    cv.render_pdf(html_file, tmp_path / "a.pdf", playwright=fake)
    kinds = [entry[0] for entry in fake.log]
    assert kinds[0] == "launch" and fake.log[0][1] == {"headless": True} and "new_context" in kinds and kinds[-1] == "close"
    goto = next(entry for entry in fake.log if entry[0] == "goto")
    assert goto[1] == html_file.resolve().as_uri() and goto[1].startswith("file:")
    assert next(e for e in fake.log if e[0] == "pdf")[1]["format"] == "A4" and (tmp_path / "a.pdf").is_file()
    handler = next(e for e in fake.log if e[0] == "route")[2]  # solo el HTML local: lo demás se corta
    allowed = SimpleNamespace(request=SimpleNamespace(url="file:///x.html"), continue_=lambda: "ok", abort=lambda: "cortado")
    blocked = SimpleNamespace(request=SimpleNamespace(url="https://example.com/x.js"), continue_=lambda: "ok", abort=lambda: "cortado")
    assert (handler(allowed), handler(blocked)) == ("ok", "cortado")


def test_the_renderer_closes_the_browser_even_when_printing_fails(tmp_path):
    fake = FakePlaywright()
    fake.context.new_page = lambda: SimpleNamespace(goto=lambda url: (_ for _ in ()).throw(RuntimeError("falló")), pdf=None)
    with pytest.raises(RuntimeError, match="falló"):
        cv.render_pdf(tmp_path / "a.html", tmp_path / "a.pdf", playwright=fake)
    assert fake.log[-1] == ("close",)


# ── base: tabla cvs y gasto ──────────────────────────────────────────────────

def test_month_spent_and_spend_add_scores_and_api_spend_and_not_the_cvs_table(conn):
    from datetime import date
    add_offer(conn, 1)
    conn.execute("UPDATE scores SET cost_usd = 0.25")
    db.add_spend(conn, "cv", OFFER_ID, "gpt-6-luna", 0.5, 1, 1, True)
    db.add_spend(conn, "cv", OFFER_ID, "gpt-6-luna", 0.125, 1, 1, False)
    db.add_spend(conn, "cv", f"{2:016X}", "gpt-6-luna", 1.0, 1, 1, True)
    conn.execute("UPDATE api_spend SET at = '2020-01-15T00:00:00+00:00' WHERE cost_usd = 1.0")  # de otro mes
    db.save_cv(conn, OFFER_ID, "backend", "gpt-6-luna", "c4", "a.html", "a.pdf", 99.0, 1, 1)  # `cvs` no suma: ya está en api_spend
    today = date.fromisoformat(conn.execute("SELECT substr(at, 1, 10) FROM api_spend LIMIT 1").fetchone()[0])
    assert score.month_spent(conn, today) == pytest.approx(0.875)
    assert ui_logic.spend(conn, today) == {"month": pytest.approx(0.875), "total": pytest.approx(1.875)}


def test_making_the_cv_again_keeps_both_costs_and_the_last_cv(conn, tmp_path):
    from datetime import date
    add_offer(conn)
    cost = (1000 * 0.10 + 200 * 0.50) / 1_000_000
    make(conn, tmp_path)
    make(conn, tmp_path)
    assert conn.execute("SELECT COUNT(*) FROM api_spend WHERE ok = 1").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM cvs").fetchone()[0] == 1
    assert score.month_spent(conn, date.today()) == pytest.approx(2 * cost) == pytest.approx(ui_logic.spend(conn, date.today())["month"])


def test_an_old_database_with_cvs_moves_their_cost_to_api_spend_once(tmp_path):
    from datetime import date
    path = tmp_path / "viejo.db"
    old = db.connect(path)
    old.execute("DROP TABLE api_spend")  # una base de antes de la TAREA-026
    old.execute("INSERT INTO cvs VALUES ('A1', 'backend', 'gpt-6-luna', 'c2', 'a.html', 'a.pdf', '2026-11-02T10:00:00+00:00', 0.25, 10, 5)")
    old.commit()
    old.close()
    conn = db.connect(path)
    assert conn.execute("SELECT at, kind, id16, model, cost_usd, input_tokens, output_tokens, ok FROM api_spend").fetchall() == [
        ("2026-11-02T10:00:00+00:00", "cv", "A1", "gpt-6-luna", 0.25, 10, 5, 1)]
    assert score.month_spent(conn, date(2026, 11, 2)) == pytest.approx(0.25)  # una sola vez, sin contar `cvs` otra vez
    conn.close()
    again = db.connect(path)  # abrirla de nuevo no copia otra vez
    assert again.execute("SELECT COUNT(*) FROM api_spend").fetchone()[0] == 1
    again.close()


def test_an_old_database_opens_and_gets_the_cvs_table(tmp_path):
    path = tmp_path / "viejo.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE offers (id16 TEXT PRIMARY KEY, data_id TEXT NOT NULL, title TEXT, status TEXT NOT NULL DEFAULT 'new', "
                "first_seen TEXT NOT NULL, last_seen TEXT NOT NULL)")
    old.commit()
    old.close()
    conn = db.connect(path)
    assert [r[1] for r in conn.execute("PRAGMA table_info(cvs)")] == [
        "id16", "variant", "model", "prompt_version", "html", "pdf", "created_at", "cost_usd", "input_tokens", "output_tokens"]
    assert [r[1] for r in conn.execute("PRAGMA table_info(api_spend)")] == [
        "at", "kind", "id16", "model", "cost_usd", "input_tokens", "output_tokens", "ok"]
    conn.close()


def test_top_candidates_are_the_best_new_offers_without_a_cv(conn, tmp_path):
    for n, points in ((1, 60), (2, 90), (3, 80), (4, 95), (5, 70)):
        add_offer(conn, n, points=points)
    db.mark_status(conn, f"{4:016X}", "duplicate")
    db.mark_status(conn, f"{3:016X}", "excluded")
    add_offer(conn, 6, points=99, model="gpt-4o-mini")  # puntaje de otro modelo
    make(conn, tmp_path, id16=f"{2:016X}")  # la 2 ya tiene CV
    assert cv.top_candidates(conn, "gpt-6-luna", 2) == [f"{5:016X}", f"{1:016X}"]
    assert cv.top_candidates(conn, "gpt-6-luna", 0) == []


# ── ui_logic ────────────────────────────────────────────────────────────────

def write_workspace(tmp_path, key=True):
    (tmp_path / "cv").mkdir(exist_ok=True)
    (tmp_path / "cv" / "a.yaml").write_text(yaml.safe_dump(RESUME), encoding="utf-8")
    (tmp_path / "profile.yaml").write_text(yaml.safe_dump({"variants": [{"id": "backend", "resume": "cv/a.yaml"}]}), encoding="utf-8")
    (tmp_path / ".env").write_text("OPENAI_API_KEY=sk-test-FALSA-123\nOPENAI_MODEL=gpt-6-luna\n" if key else "", encoding="utf-8")


def test_run_cv_uses_the_key_and_model_of_the_env_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # el CV se guarda en data/cvs de la carpeta actual
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    write_workspace(tmp_path)
    connection = db.connect(tmp_path / "t.db")
    add_offer(connection)
    connection.close()
    keys, client = [], FakeClient()
    info = ui_logic.run_cv(tmp_path / ".env", tmp_path / "profile.yaml", tmp_path / "t.db", OFFER_ID,
                           lambda api_key: (keys.append(api_key), client)[1], render=FakeRender())
    assert keys == ["sk-test-FALSA-123"] and info["id16"] == OFFER_ID and client.calls[0]["model"] == "gpt-6-luna"
    assert ui_logic.cv_line(info).startswith("CV guardado en ") and "costo: US$ 0,0002" in ui_logic.cv_line(info)
    assert "sin texto adaptado" not in ui_logic.cv_line(info)
    assert Path(info["pdf"]).is_file() and (tmp_path / "data" / "cvs").is_dir()


def test_run_cv_without_a_key_does_not_build_a_client(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    write_workspace(tmp_path, key=False)
    with pytest.raises(RuntimeError, match="Falta la clave de OpenAI"):
        ui_logic.run_cv(tmp_path / ".env", tmp_path / "profile.yaml", tmp_path / "t.db", OFFER_ID,
                        lambda api_key: pytest.fail("no tenía que armarse el cliente"))


# ── CLI ──────────────────────────────────────────────────────────────────────

@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    """CLI aislado: cwd vacío (no se lee ningún .env real), clave falsa, OpenAI() y el renderizador reemplazados."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("os.environ", {"OPENAI_API_KEY": "sk-test-NUNCA-SE-IMPRIME"})
    write_workspace(tmp_path, key=False)
    (tmp_path / ".env").unlink()
    holder = SimpleNamespace(client=FakeClient(), render=FakeRender())
    monkeypatch.setattr(cli.openai, "OpenAI", lambda: holder.client)
    monkeypatch.setattr(cv, "render_pdf", holder.render)
    connection = db.connect(tmp_path / "t.db")
    for n, points in ((1, 60), (2, 90), (3, 80)):
        add_offer(connection, n, points=points)
    connection.close()
    return holder


def cli_args(tmp_path, *extra: str) -> list[str]:
    return ["cv", "make", "--profile", str(tmp_path / "profile.yaml"), "--db", str(tmp_path / "t.db"), *extra]


def test_cli_make_by_id_accepts_the_ct_prefix_and_never_prints_the_content(cli_env, tmp_path, capsys):
    assert cli.main(cli_args(tmp_path, "--id", f"ct-{OFFER_ID}")) == 0
    out = capsys.readouterr()
    lines = out.out.strip().splitlines()
    assert lines[0].startswith(f"{OFFER_ID} · backend · ") and lines[0].endswith(f"{OFFER_ID}-backend.pdf · costo: US$ 0,0002")
    assert lines[1] == "Gasto del mes: US$ 0,0002 de US$ 2,00" and out.err == ""
    assert (tmp_path / "data" / "cvs" / f"{OFFER_ID}-backend.html").is_file()
    assert "Senior Software Engineer" not in out.out and "Giovanni" not in out.out and "sk-test" not in out.out


def test_cli_make_top_makes_the_best_ones_without_a_cv_and_stops_at_max_cost(cli_env, tmp_path, capsys):
    assert cli.main(cli_args(tmp_path, "--top", "2")) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert [l.split(" · ")[0] for l in lines[:2]] == [f"{2:016X}", f"{3:016X}"] and len(cli_env.client.calls) == 2
    assert cli.main(cli_args(tmp_path, "--top", "5")) == 0  # queda la 1
    assert capsys.readouterr().out.startswith(f"{1:016X} · ")
    assert cli.main(cli_args(tmp_path, "--top", "5")) == 0
    assert capsys.readouterr().out.startswith("No hay ofertas puntuadas sin CV.")
    connection = db.connect(tmp_path / "t.db")
    connection.execute("DELETE FROM cvs")
    connection.commit()
    connection.close()
    assert cli.main(cli_args(tmp_path, "--top", "3", "--max-cost", "0.0001")) == 0  # la primera ya cuesta más
    out = capsys.readouterr().out
    assert out.count(" · backend · ") == 1 and "Se llegó al tope de gasto (--max-cost US$ 0,00)" in out


def test_cli_make_errors_exit_1_with_the_spend_and_without_calling_the_api(cli_env, tmp_path, capsys):
    assert cli.main(cli_args(tmp_path, "--id", "basura")) == 1
    assert "no es un id de oferta" in capsys.readouterr().err
    assert cli.main(cli_args(tmp_path, "--id", f"{9:016X}")) == 1
    captured = capsys.readouterr()
    assert "No existe la oferta" in captured.err and captured.out == "Gasto del mes: US$ 0,0000 de US$ 2,00\n"
    assert cli_env.client.calls == []


def test_cli_make_needs_exactly_one_of_id_or_top(tmp_path, capsys):
    for extra in ([], ["--id", OFFER_ID, "--top", "1"]):
        with pytest.raises(SystemExit):
            cli.main(cli_args(tmp_path, *extra))
    capsys.readouterr()


# ── TAREA-028: CV adaptado, verificado por código ────────────────────────────

BARE = resume_with(technical_skills=["Python"], certifications=[],
                   experience_details=[{**e, "position": "Software Engineer"} for e in RESUME["experience_details"]])
TRUTH = "Usé Node.js en trabajos de la tecnicatura"


def problems(text: str, resume=BARE, truths=(), limit: int = 200, offer: str = "") -> list[str]:
    return cv._line_problems("línea", text, limit, cv._base_vocabulary(resume, truths, offer), cv._allowed_levels(resume))


def test_protected_tokens_are_digits_inner_capitals_and_symbols():
    assert cv.protected_tokens("Usé Node.js, C#, AWS y C++ en 2 equipos con 30% menos") == ["Node.js", "C#", "AWS", "C++", "2", "30%"]
    assert cv.protected_tokens("Trabajo en equipo y mantenimiento evolutivo") == []
    assert cv.protected_tokens("Mantuve sistemas. Luego mejoré Java y PostgreSQL") == ["Java", "PostgreSQL"]  # el inicio de frase no cuenta
    assert cv.protected_tokens("Armé APIs y/o servicios (REST).") == ["APIs", "y/o", "REST"]
    assert cv.protected_tokens("Aplicaciones .NET") == [".NET"]


@pytest.mark.parametrize("text", [
    "Desarrollé servicios con Node.js", "Trabajé con AWS en la nube", "Más de 5 años de experiencia",
    "Perfil Senior en desarrollo", "Fui Líder del equipo",
])
def test_a_line_with_a_fact_that_is_not_in_the_base_cv_is_invalid(text):
    reasons = problems(text)
    assert reasons and all(r.startswith("línea: ") and ("no figura en el CV base" in r or "palabra de nivel" in r) for r in reasons)


def test_the_same_facts_are_valid_when_the_bank_or_the_cv_has_them_and_plain_vocabulary_is_free():
    assert problems("Desarrollé servicios con Node.js", truths=[TRUTH]) == []  # con «Node.js» en el banco
    assert problems("Trabajé con AWS en la nube", resume=RESUME) == []  # el CV de ejemplo trae AWS
    assert problems("Perfil Senior en desarrollo", resume=RESUME) == []  # y un puesto «Senior Software Engineer»
    assert problems("Mantenimiento evolutivo de aplicaciones y trabajo en equipo") == []
    assert problems("Automatización de pruebas en proyectos web") == []


def test_the_digits_and_words_of_the_base_cv_count_even_inside_dates_and_ratios():
    assert problems("Egresé en 2013 con 110/110", resume=RESUME) == [] and problems("Mejoré un 20%", resume=RESUME) == []
    assert problems("Mejoré un 25%", resume=RESUME) != [] and problems("Desde 2019", resume=RESUME) != []


def test_the_length_limits_apply_to_each_line():
    assert problems("a" * 61, limit=60) == ["línea: tiene 61 caracteres y el máximo es 60."]
    assert problems("a" * 60, limit=60) == [] and problems("a" * 201) != []


def test_a_level_word_in_the_title_is_rejected_unless_a_position_or_study_has_it():
    assert any("palabra de nivel «senior»" in r for r in problems("Desarrollador Senior"))
    assert any("palabra de nivel «semi»" in r for r in problems("Desarrollador semi-senior"))
    assert any("palabra de nivel «líder»" in r for r in problems("Fui Líder del equipo"))
    studies = resume_with(education_details=[{"education_level": "Tecnicatura", "field_of_study": "Programación Sr"}],
                          experience_details=BARE["experience_details"])
    assert problems("Programador Sr") != [] and problems("Tecnicatura Sr", resume=studies) == []
    assert problems("Desarrollador Junior") == [] and problems("Pasante, trainee y practicante: estudiante de ingeniería") == []


def test_a_valid_first_answer_makes_the_c8_cv_with_title_profile_bullets_and_one_paid_call(conn, tmp_path):
    add_offer(conn)
    client = FakeClient()
    info = make(conn, tmp_path, client)
    assert len(client.calls) == 1 and info["version"] == "c8" and info["fallback_reason"] is None
    page = info["html"].read_text(encoding="utf-8")
    assert '<div class="headline">Desarrollador de software</div>' in page
    assert "<h2>Perfil</h2><p>Desarrollador con experiencia en liderazgo de equipos.</p>" in page
    assert "<li>Lideré equipos para construir soluciones empresariales</li>" in page
    assert "Led a team of developers" not in page  # la viñeta generada reemplaza a la responsabilidad base
    assert [row[0] for row in conn.execute("SELECT ok FROM api_spend")] == [1]


def test_every_text_of_a_c8_html_is_base_text_or_a_line_the_code_verified():
    data = good5()
    parsed = cv._parse(RESUME)
    selection, found = cv._validate_text(parsed, data, cv._base_vocabulary(RESUME, ()), cv._allowed_levels(RESUME))
    assert found == []
    nodes = TextNodes()
    nodes.feed(cv.build_html(RESUME, selection))
    generated = {data["headline"], data["summary"]["text"], *(b["text"] for e in data["experiences"] for b in e["bullets"])}
    allowed = leaves({key: RESUME[key] for key in ("personal_information", "education_details", "experience_details",
                                                   "technical_skills", "projects", "achievements", "certifications", "languages", "interests")}) | EXTRA_TEXTS
    assert [text for text in nodes.texts if text not in allowed | generated] == []
    assert generated <= set(nodes.texts)


def invalid_node() -> dict:
    return good5(experiences=[entry5("E1", [bullet("E1.R1", "Desarrollé servicios con Node.js")], ["E1.S2"]), *good5()["experiences"][1:]])


def test_an_invalid_first_answer_is_asked_again_with_the_reasons_and_a_valid_second_one_makes_the_c8_cv(conn, tmp_path):
    add_offer(conn)
    client = FakeClient(response(invalid_node()), response(good5()))
    info = make(conn, tmp_path, client)
    assert len(client.calls) == 2 and info["version"] == "c8" and info["fallback_reason"] is None
    assert "Node.js" in client.calls[1]["input"][1]["content"] and "Node.js" not in client.calls[0]["input"][1]["content"]
    assert "viñeta 1 de E1" in client.calls[1]["input"][1]["content"]
    assert [row[0] for row in conn.execute("SELECT ok FROM api_spend ORDER BY rowid")] == [0, 1]
    cost = (1000 * 0.10 + 200 * 0.50) / 1_000_000
    assert info["cost_usd"] == pytest.approx(2 * cost) and (info["input_tokens"], info["output_tokens"]) == (2000, 400)
    assert conn.execute("SELECT prompt_version, cost_usd FROM cvs").fetchone() == ("c8", pytest.approx(2 * cost))


def structural_node() -> dict:
    """Una viñeta con una fuente que no existe: error de estructura, no de una línea."""
    return good5(experiences=[entry5("E1", [bullet("E1.R1", "Desarrollé servicios con Node.js"), bullet("E9.R1", "Hice algo")], ["E1.S2"]),
                              *good5()["experiences"][1:]])


def test_two_structural_errors_make_the_ids_only_c4_cv_with_the_reason_and_two_paid_calls(conn, tmp_path):
    add_offer(conn)
    client = FakeClient(response(structural_node()))  # la última respuesta se repite
    info = make(conn, tmp_path, client)
    assert len(client.calls) == 2 and info["version"] == "c4" and "E9.R1" in info["fallback_reason"] and info["replaced"] == []
    page = info["html"].read_text(encoding="utf-8")
    assert "Node.js" not in page and '<div class="headline">' not in page and "<h2>Perfil</h2>" not in page
    assert "Led a team of developers" in page  # del CV base, por el id de la fuente (E1.R1)
    assert "Lideré equipos" not in page and "Mejoré el rendimiento" not in page
    assert [row[0] for row in conn.execute("SELECT ok FROM api_spend ORDER BY rowid")] == [0, 0]
    assert conn.execute("SELECT COUNT(*) FROM api_spend").fetchone()[0] == 2
    assert conn.execute("SELECT prompt_version FROM cvs").fetchone() == ("c4",)
    review = info["revision"].read_text(encoding="utf-8")
    assert "Se armó solo con ids del CV base" in review and "E9.R1" in review and "Versión del armado: c4" in review
    assert "Se armó solo con ids" in ui_logic.cv_line(info) and "E9.R1" in ui_logic.cv_line(info)


@pytest.mark.parametrize("name, bad_entries", [
    ("fuente inexistente", [entry5("E1", [bullet("E9.R1", "Hice algo")])]),
    ("fuente de otra experiencia", [entry5("E2", [bullet("E1.R1", "Hice algo")])]),
    ("fuente que no es una responsabilidad", [entry5("E1", [bullet("E1.S1", "Hice algo")])]),
    ("más de cinco viñetas", [entry5("E1", [bullet("E1.R1", f"Hice algo {n}".replace(str(n), "")) for n in range(6)])]),
    ("experiencia inexistente", [entry5("E7", [bullet("E1.R1", "Hice algo")])]),
    ("habilidad de otra experiencia", [entry5("E1", [bullet("E1.R1", "Hice algo")], ["E2.S1"])]),
])
def test_a_structural_error_repeats_the_request_and_a_valid_second_answer_is_used(conn, tmp_path, name, bad_entries):
    add_offer(conn)
    client = FakeClient(response(good5(experiences=bad_entries)), response(good5()))
    info = make(conn, tmp_path, client)
    assert len(client.calls) == 2 and info["version"] == "c8"
    assert "Tu respuesta anterior tuvo líneas inválidas" in client.calls[1]["input"][1]["content"]
    assert conn.execute("SELECT COUNT(*) FROM api_spend").fetchone()[0] == 2


def test_a_second_structural_error_still_makes_the_c4_cv_keeping_only_the_valid_ids(conn, tmp_path):
    add_offer(conn)
    bad = good5(experiences=[entry5("E1", [bullet("E1.R3", "Mejoré el rendimiento"), bullet("E9.R1", "Hice algo")], ["E1.S2", "E2.S1"]),
                             entry5("E2", [bullet("E1.R1", "Hice algo")])], projects=["P2", "P9"], interests=["I3", "I3"])
    info = make(conn, tmp_path, FakeClient(response(bad)))
    page = info["html"].read_text(encoding="utf-8")
    assert info["version"] == "c4" and "E9.R1" in info["fallback_reason"]
    assert "Optimized application performance and reduced downtime" in page  # E1.R3, del CV base
    assert page.count("Smart Home Automation") == 1 and page.count("IoT Development") == 1  # ids repetidos, una sola vez; inválidos, fuera
    assert "Collaborated with UX/UI designers" not in page  # E2 quedó sin fuente válida: pasa a «otra»


def test_nothing_is_saved_when_the_answer_has_no_ids_to_salvage(conn, tmp_path):
    add_offer(conn)
    with pytest.raises(RuntimeError, match="JSON válido"):
        make(conn, tmp_path, FakeClient(SimpleNamespace(output_text="[1]x", output=[], status="completed", usage=None)))
    nothing_saved(conn, tmp_path)


@pytest.mark.parametrize("field, value", [
    ("headline", "T" * 10 + " " + "a" * 60),
    ("summary", {"text": "a" * 351, "sources": ["E1.R1"]}),
    ("experiences", [entry5("E1", [bullet("E1.R1", "a" * 201)])]),
])
def test_a_line_over_its_limit_is_rejected(conn, tmp_path, field, value):
    add_offer(conn)
    client = FakeClient(response(good5(**{field: value})), response(good5()))
    info = make(conn, tmp_path, client)
    assert len(client.calls) == 2 and "caracteres" in client.calls[1]["input"][1]["content"] and info["version"] == "c8"


def test_the_bank_of_truths_goes_to_the_request_and_makes_a_line_with_its_words_valid(conn, tmp_path):
    add_offer(conn)
    prof = {**PROFILE, "variants": [{**PROFILE["variants"][0], "truths": [TRUTH, "Armé una API REST en un trabajo práctico"]}]}
    data = good5(experiences=[entry5("E1", [bullet("X1", "Desarrollé servicios con Node.js"), bullet("E1.R3", "Mejoré el rendimiento")]),
                              *good5()["experiences"][1:]], technical_skills=["X2", "T1"])
    client = FakeClient(response(data))
    info = make(conn, tmp_path, client, prof=prof)
    sent = client.calls[0]["input"][1]["content"]
    assert len(client.calls) == 1 and info["version"] == "c8"
    assert f"X1: {TRUTH}" in sent and "Banco de verdades" in sent and "X2: Armé una API REST" in sent
    page = info["html"].read_text(encoding="utf-8")
    assert "<li>Desarrollé servicios con Node.js</li>" in page and "<li>Armé una API REST en un trabajo práctico</li>" in page
    assert "Fuente X1: " + TRUTH in info["revision"].read_text(encoding="utf-8") or f"X1: {TRUTH}" in info["revision"].read_text(encoding="utf-8")
    assert [value for value in PERSONAL if value in json.dumps(client.calls[0], ensure_ascii=False)] == []
    schema = client.calls[0]["text"]["format"]["schema"]
    assert "X1" in schema["properties"]["technical_skills"]["items"]["enum"]


def test_every_request_has_no_personal_data_even_when_it_is_repeated_with_the_reasons(conn, tmp_path):
    add_offer(conn)
    client = FakeClient(response(invalid_node()), response(good5()))
    make(conn, tmp_path, client)
    for call in client.calls:
        assert [value for value in PERSONAL if value in json.dumps(call, ensure_ascii=False)] == []


def test_the_request_has_the_unmet_requirements_and_the_revision_lists_them(conn, tmp_path):
    add_offer(conn)
    conn.execute("UPDATE scores SET unmet_required = ?", (json.dumps(["2 años de experiencia web", "Node.js <b>"]),))
    client = FakeClient()
    info = make(conn, tmp_path, client)
    sent = client.calls[0]["input"][1]["content"]
    assert "Requisitos que el CV no muestra:\n- 2 años de experiencia web\n- Node.js <b>" in sent
    review = info["revision"].read_text(encoding="utf-8")
    assert "<h2>Requisitos que el CV no muestra</h2>" in review and "<li>2 años de experiencia web</li>" in review
    assert "<b>" not in review and "Node.js &lt;b&gt;" in review


def test_without_unmet_requirements_the_request_does_not_mention_them_and_the_revision_says_none(conn, tmp_path):
    add_offer(conn)
    client = FakeClient()
    info = make(conn, tmp_path, client)
    assert "Requisitos que el CV no muestra" not in client.calls[0]["input"][1]["content"].split("Items del CV base:")[0]
    assert "Requisitos que el CV no muestra:" not in client.calls[0]["input"][1]["content"]
    assert "<p>Ninguno.</p>" in info["revision"].read_text(encoding="utf-8")


def test_the_revision_shows_each_generated_line_with_its_base_line_and_the_title_and_profile_sources(conn, tmp_path):
    add_offer(conn)
    review = make(conn, tmp_path)["revision"].read_text(encoding="utf-8")
    assert "<h2>Título</h2><p>Desarrollador de software</p>" in review
    assert "<h2>Perfil</h2><p>Desarrollador con experiencia en liderazgo de equipos.</p>" in review
    assert "Fuente E1.R1: Led a team of developers in designing and implementing enterprise software solutions" in review
    assert ("<td>E1</td><td>Lideré equipos para construir soluciones empresariales</td>"
            "<td>E1.R1: Led a team of developers in designing and implementing enterprise software solutions</td>") in review
    assert review.startswith("<!doctype html>") and "Versión del armado: c8" in review and "Se armó solo" not in review


def test_html_and_revision_escape_every_generated_text():
    selection = {"headline": "<script>t</script>", "summary": {"text": "<b>p</b>", "sources": ["E1.R1"]},
                 "experiences": {"E1": {"block": "relevante", "bullets": [{"source": "E1.R1", "text": "<i>x</i> & y"}],
                                        "responsibilities": ["E1.R1"], "skills": []},
                                 "E2": {"block": "otra", "bullets": [], "responsibilities": [], "skills": []},
                                 "E3": {"block": "otra", "bullets": [], "responsibilities": [], "skills": []}},
                 "technical_skills": [], "projects": [], "achievements": [], "certifications": [], "interests": []}
    page = cv.build_html(RESUME, selection)
    review = cv.build_revision(RESUME, selection, unmet=["<u>r</u>"], fallback_reason="<em>motivo</em>")
    for text in (page, review):
        assert "<script>" not in text and "<b>" not in text and "<i>" not in text and "<u>" not in text and "<em>" not in text
    assert "&lt;script&gt;t&lt;/script&gt;" in page and "&lt;i&gt;x&lt;/i&gt; &amp; y" in page
    assert "&lt;em&gt;motivo&lt;/em&gt;" in review and "&lt;u&gt;r&lt;/u&gt;" in review


def test_an_experience_without_bullets_goes_to_other_experience_in_the_c8_answer(conn, tmp_path):
    add_offer(conn)
    data = good5(experiences=[entry5("E1", [bullet("E1.R1", "Lideré equipos")]), entry5("E2", [], block="relevante"),
                              entry5("E3", [], block="otra")])
    page = make(conn, tmp_path, FakeClient(response(data)))["html"].read_text(encoding="utf-8")
    other = block(page, "Otra experiencia")
    assert "Software Developer" in other and "Junior Developer" in other and "<li>" not in other


def test_the_cli_prints_why_the_cv_fell_back_to_ids_only(cli_env, tmp_path, capsys):
    cli_env.client = FakeClient(response(structural_node()))
    assert cli.main(cli_args(tmp_path, "--id", OFFER_ID)) == 0
    out = capsys.readouterr().out
    assert "Se armó solo con ids del CV base (sin texto adaptado):" in out and "E9.R1" in out and "Se reemplaz" not in out


# ── TAREA-029: control más justo y adaptación más fuerte ────────────────────

ES = resume_with(
    technical_skills=["Python"], certifications=[], interests=["Inteligencia Artificial", "Procesamiento de Lenguaje Natural"],
    experience_details=[{"position": "Desarrollador de software", "company": "Empresa Falsa", "employment_period": "2020 - 2022",
                         "key_responsibilities": ["Mantuve APIs internas", "Atención al cliente por correo"], "skills_acquired": []}])
OFFER = "Buscamos un especialista en automatización de procesos. Se valora python y docker."


@pytest.mark.parametrize("text", [
    "Desarrollador/a de software", "Trabajo en equipo y/o en solitario", "Diseño de APIs", "Experiencia en IA aplicada",
    "Desarrollador Junior", "Especialista en Automatización", "Experiencia en PLN", "Desarrollador Python",
])
def test_the_fair_control_accepts_true_words_with_gender_plural_acronym_and_title_case(text):
    assert problems(text, resume=ES, offer=OFFER) == []


@pytest.mark.parametrize("text", [
    "Desarrollé con Node.js", "Trabajé con AWS", "Más de 5 años de experiencia", "Desarrollador Senior", "Desarrollador SSR",
    "Experiencia en Docker", "Perfil JavaScript", "Experiencia en IAM", "Armé servicios REST/SOAP",
])
def test_the_fair_control_still_rejects_what_the_cv_and_the_bank_do_not_have(text):
    assert problems(text, resume=ES, offer="Buscamos Docker, JavaScript y Python") != []


def test_a_capital_initial_is_protected_unless_the_offer_or_the_cv_has_the_word_in_lowercase():
    assert problems("Experiencia en Python", resume=resume_with(technical_skills=[], experience_details=ES["experience_details"]), offer="Buscamos Python") != []
    assert problems("Experiencia en Docker", resume=ES, offer="se valora docker") == []
    assert cv.protected_tokens("Hice Automatización y Docker", plain={"automatización"}) == ["Docker"]
    assert cv.protected_tokens("Hice Automatización y Docker") == ["Automatización", "Docker"]
    assert cv.protected_tokens("Usé JavaScript y AWS", plain={"javascript", "aws"}) == ["JavaScript", "AWS"]


def test_a_token_with_a_slash_is_valid_when_every_part_is_ignoring_gender_endings():
    vocab = cv._base_vocabulary(ES, ())
    assert cv._covered("Desarrollador/a", vocab) and cv._covered("y/o", vocab) and cv._covered("Desarrollador/ora", vocab) is False
    singular = cv._base_vocabulary(resume_with(technical_skills=["API REST"], experience_details=[], projects=[]), ())
    assert cv._covered("APIs", singular) and cv._covered("RESTs", singular) and not cv._covered("Webs", singular)
    assert cv._covered("IA", vocab) and cv._covered("PLN", vocab) and not cv._covered("IAM", vocab)


def user_of(call) -> str:
    return call["input"][1]["content"]


def copies() -> dict:
    """Todas las viñetas son la línea del CV base con otra puntuación y otras mayúsculas."""
    return good5(experiences=[
        entry5("E1", [bullet("E1.R1", "led a team of developers in designing and implementing enterprise software solutions."),
                      bullet("E1.R3", "Optimized application performance and reduced downtime by 20%")]),
        entry5("E2", [bullet("E2.R2", "Collaborated  with UX/UI designers to enhance user experience.")])])


def test_the_system_prompt_asks_to_rewrite_with_the_offer_vocabulary_and_has_a_generic_example():
    assert cv.CV_PROMPT_VERSION == "c8" and "no copies la línea del CV base" in cv.SYSTEM and "Ejemplo genérico" in cv.SYSTEM
    assert "una viñeta con una habilidad transferible" in cv.SYSTEM


def test_an_answer_with_every_bullet_copied_is_asked_again_with_the_reason_and_the_adapted_one_is_used(conn, tmp_path):
    add_offer(conn)
    client = FakeClient(response(copies()), response(good5()))
    info = make(conn, tmp_path, client)
    assert len(client.calls) == 2 and info["version"] == "c8" and info["fallback_reason"] is None
    assert "reescribí las viñetas con el vocabulario de la oferta" in user_of(client.calls[1])
    assert "reescribí" not in user_of(client.calls[0])
    assert [row[0] for row in conn.execute("SELECT ok FROM api_spend ORDER BY rowid")] == [0, 1]
    assert "Atención" not in info["html"].read_text(encoding="utf-8") and "Lideré equipos" in info["html"].read_text(encoding="utf-8")
    review = info["revision"].read_text(encoding="utf-8")
    assert "son copia" not in review and "<td>adaptada</td>" in review


def test_one_adapted_bullet_among_copies_is_enough_to_skip_the_retry(conn, tmp_path):
    add_offer(conn)
    mixed = copies()
    mixed["experiences"][0]["bullets"][1] = bullet("E1.R3", "Mejoré el rendimiento de las aplicaciones")
    client = FakeClient(response(mixed))
    info = make(conn, tmp_path, client)
    assert len(client.calls) == 1 and info["version"] == "c8"
    review = info["revision"].read_text(encoding="utf-8")
    assert review.count("<td>copia</td>") == 2 and review.count("<td>adaptada</td>") == 1 and "son copia de la línea" not in review


def test_a_second_answer_that_copies_again_is_used_with_the_warning_in_the_review_and_two_paid_calls(conn, tmp_path):
    add_offer(conn)
    client = FakeClient(response(copies()))  # la última respuesta se repite
    info = make(conn, tmp_path, client)
    assert len(client.calls) == 2 and info["version"] == "c8" and info["fallback_reason"] is None
    assert [row[0] for row in conn.execute("SELECT ok FROM api_spend ORDER BY rowid")] == [0, 1]
    assert conn.execute("SELECT prompt_version FROM cvs").fetchone() == ("c8",)
    page = info["html"].read_text(encoding="utf-8")
    assert "<li>led a team of developers in designing and implementing enterprise software solutions.</li>" in page
    review = info["revision"].read_text(encoding="utf-8")
    assert "Todas las viñetas son copia de la línea del CV base" in review and review.count("<td>copia</td>") == 3
    assert "<td>adaptada</td>" not in review


def test_a_first_answer_with_other_problems_and_copies_sends_both_reasons(conn, tmp_path):
    add_offer(conn)
    both = copies()
    both["headline"] = "Experto en Node.js"
    client = FakeClient(response(both), response(good5()))
    make(conn, tmp_path, client)
    sent = user_of(client.calls[1])
    assert "el título" in sent and "Node.js" in sent and "reescribí las viñetas con el vocabulario de la oferta" in sent


def test_an_other_experience_can_have_one_transferable_bullet_with_the_same_control(conn, tmp_path):
    add_offer(conn)
    data = good5(experiences=[entry5("E1", [bullet("E1.R1", "Lideré equipos para construir soluciones empresariales")]),
                              entry5("E2", [], block="relevante"),
                              entry5("E3", [bullet("E3.R2", "Participé en revisiones de código y diseño de software")], ["E3.S1"], block="otra")])
    client = FakeClient(response(data))
    info = make(conn, tmp_path, client)
    assert len(client.calls) == 1 and info["version"] == "c8"
    page = info["html"].read_text(encoding="utf-8")
    other = block(page, "Otra experiencia")
    assert "Junior Developer" in other and "<li>Participé en revisiones de código y diseño de software</li>" in other
    assert other.index("Junior Developer") < other.index("Participé") and "Mobile app development" not in page  # sin habilidades
    assert "Participé en revisiones" not in block(page, "Experiencia relevante")
    review = info["revision"].read_text(encoding="utf-8")
    assert "<td>E3</td><td>Participé en revisiones de código y diseño de software</td><td>E3.R2: Participated in code reviews" in review


def test_an_other_experience_bullet_goes_through_the_same_verification(conn, tmp_path):
    add_offer(conn)
    bad = good5(experiences=[*good5()["experiences"][:2], entry5("E3", [bullet("E3.R2", "Revisé código en AWS Lambda y Node.js")], block="otra")])
    client = FakeClient(response(bad), response(good5()))
    info = make(conn, tmp_path, client)
    assert len(client.calls) == 2 and "Node.js" in user_of(client.calls[1]) and "viñeta 1 de E3" in user_of(client.calls[1])
    assert info["version"] == "c8"


@pytest.mark.parametrize("name, bad_entry", [
    ("dos viñetas", entry5("E3", [bullet("E3.R1", "Programé aplicaciones"), bullet("E3.R2", "Revisé código")], block="otra")),
    ("fuente de otra experiencia", entry5("E3", [bullet("E1.R1", "Lideré equipos")], block="otra")),
])
def test_a_second_bullet_or_a_foreign_source_in_an_other_experience_is_a_format_error(conn, tmp_path, name, bad_entry):
    add_offer(conn)
    client = FakeClient(response(good5(experiences=[*good5()["experiences"][:2], bad_entry])), response(good5()))
    info = make(conn, tmp_path, client)
    assert len(client.calls) == 2 and "Tu respuesta anterior tuvo líneas inválidas" in user_of(client.calls[1])
    assert info["version"] == "c8" and conn.execute("SELECT COUNT(*) FROM api_spend").fetchone()[0] == 2
    if name == "dos viñetas":
        assert "máximo es 1" in user_of(client.calls[1])


def test_two_bullets_in_an_other_experience_twice_make_the_c4_cv(conn, tmp_path):
    add_offer(conn)
    bad = good5(experiences=[*good5()["experiences"][:2],
                             entry5("E3", [bullet("E3.R1", "Programé aplicaciones"), bullet("E3.R2", "Revisé código")], block="otra")])
    info = make(conn, tmp_path, FakeClient(response(bad)))
    assert info["version"] == "c4" and "máximo es 1" in info["fallback_reason"]


def test_the_revision_marks_each_bullet_as_adapted_or_copy():
    selection = {"headline": "", "summary": {"text": "", "sources": []},
                 "experiences": {"E1": {"block": "relevante", "responsibilities": ["E1.R1"], "skills": [], "bullets": [
                     bullet("E1.R1", "Led a team of developers in designing and implementing enterprise software solutions"),
                     bullet("E1.R3", "Mejoré el rendimiento")]}}}
    review = cv.build_revision(RESUME, selection)
    assert "<th>Tipo</th>" in review and review.count("<td>copia</td>") == 1 and review.count("<td>adaptada</td>") == 1
    assert "Todas las viñetas son copia" not in review


def test_an_other_experience_bullet_is_text_of_the_html_that_the_walker_test_allows():
    data = good5(experiences=[*good5()["experiences"][:2], entry5("E3", [bullet("E3.R2", "Revisé código y diseño de software")], block="otra")])
    parsed = cv._parse(RESUME)
    selection, found = cv._validate_text(parsed, data, cv._base_vocabulary(RESUME, ()), cv._allowed_levels(RESUME))
    assert found == []
    nodes = TextNodes()
    nodes.feed(cv.build_html(RESUME, selection))
    generated = {data["headline"], data["summary"]["text"], *(b["text"] for e in data["experiences"] for b in e["bullets"])}
    allowed = leaves({key: RESUME[key] for key in ("personal_information", "education_details", "experience_details",
                                                   "technical_skills", "projects", "achievements", "certifications", "languages", "interests")}) | EXTRA_TEXTS
    assert [text for text in nodes.texts if text not in allowed | generated] == []
    assert generated <= set(nodes.texts)


# ── TAREA-030: palabras de puesto, ids en el texto y siglas de un mismo item ─

NOPY = resume_with(technical_skills=[], certifications=[], interests=["Inteligencia Artificial", "Nivel C1 de inglés"],
                   experience_details=[{"position": "Soporte técnico", "key_responsibilities": ["Atención al cliente por correo"]}])


def title_problems(text: str, offer_title: str, resume=NOPY) -> list[str]:
    return cv._line_problems("el título", text, 60, cv._base_vocabulary(resume, ()), cv._allowed_levels(resume), cv.title_roles(offer_title))


def test_the_role_words_are_a_fixed_list_compared_without_case_or_accents():
    assert {"developer", "desarrollador", "integración", "customer", "service", "telemarketer"} <= cv.ROLE_WORDS
    assert cv.title_roles("Analista de INTEGRACIÓN Full-Stack (Python)") == {"analista", "integracion", "full", "stack"}
    assert cv.title_roles("Analista de Integracion") == {"analista", "integracion"}


def test_a_role_word_of_the_offer_title_is_valid_in_the_cv_title_even_if_the_cv_lacks_it():
    assert title_problems("Full Stack Developer", "Full Stack Developer") == []
    assert title_problems("Analista de Integración", "Integracion de sistemas - Analista") == []
    assert title_problems("Desarrollador Web", "Developer") != []  # «Web» no está en el título de la oferta ni en el CV
    assert title_problems("Customer Service", "Customer Service Representative") == []


def test_a_title_word_that_is_not_a_role_word_keeps_the_usual_control():
    assert any("Python" in r for r in title_problems("Python Developer", "Python Developer"))
    assert title_problems("Python Developer", "Python Developer", resume=resume_with(technical_skills=["Python"])) == []
    assert any("Node.js" in r for r in title_problems("Full Stack Developer Node.js", "Node.js Full Stack Developer"))


def test_a_role_word_only_has_the_exemption_in_the_title_not_in_a_bullet_or_the_profile():
    assert problems("Trabajé como Telemarketer en equipos", resume=NOPY, offer="Telemarketer") != []
    assert problems("Soy un Analista de sistemas", resume=NOPY, offer="Analista") != []


def test_the_first_word_of_the_title_is_controlled_unless_it_is_a_role_word():
    assert cv.protected_tokens("Python Developer", title=True) == ["Python", "Developer"]
    assert cv.protected_tokens("Desarrollador Python", title=True) == ["Python"]
    assert cv.protected_tokens("Python Developer") == ["Developer"]  # fuera del título, la primera palabra es inicio de frase


def test_the_title_roles_come_from_the_offer_title_in_make(conn, tmp_path):
    add_offer(conn)
    conn.execute("UPDATE offers SET title = 'Full Stack Developer'")
    data = good5(headline="Full Stack Developer")
    client = FakeClient(response(data))
    info = make(conn, tmp_path, client)
    assert len(client.calls) == 1 and info["version"] == "c8" and '<div class="headline">Full Stack Developer</div>' in info["html"].read_text(encoding="utf-8")
    conn.execute("UPDATE offers SET title = 'Rust Developer'")
    conn.execute("DELETE FROM cvs")
    again = make(conn, tmp_path, FakeClient(response(good5(headline="Rust Developer"))))
    assert again["version"] == "c8" and again["fallback_reason"] is None
    assert len(again["replaced"]) == 1 and "Rust" in again["replaced"][0] and "Developer" not in again["replaced"][0]


def test_strip_ids_removes_loose_and_parenthesized_ids_and_the_extra_spaces():
    vocab = cv._base_vocabulary(NOPY, ())
    assert cv._strip_ids("Experiencia (E1.S1) y T22 en soporte.", vocab) == "Experiencia y en soporte."
    assert cv._strip_ids("Lideré equipos (E1.R2, X3) con A1", vocab) == "Lideré equipos con"
    assert cv._strip_ids("[E1] Soporte técnico  (P1 )", vocab) == "Soporte técnico"
    assert cv._strip_ids("E1.R2: Atención al cliente", vocab) == "Atención al cliente"
    assert cv._strip_ids("Nivel C1 de inglés", vocab) == "Nivel C1 de inglés"  # el CV base trae «C1»
    assert cv._strip_ids("Node.E1 y E10x y XE1", vocab) == "Node.E1 y E10x y XE1"  # no son ids sueltos


def test_a_profile_with_ids_is_saved_without_them_and_passes_the_control(conn, tmp_path):
    add_offer(conn)
    data = good5(summary={"text": "Desarrollador con experiencia en liderazgo de equipos (E1.S1) y conocimientos T22.", "sources": ["E1.R1"]},
                 headline="Desarrollador de software (E1)")
    client = FakeClient(response(data))
    info = make(conn, tmp_path, client)
    assert len(client.calls) == 1 and info["version"] == "c8" and info["fallback_reason"] is None
    page = info["html"].read_text(encoding="utf-8")
    assert "<h2>Perfil</h2><p>Desarrollador con experiencia en liderazgo de equipos y conocimientos.</p>" in page
    assert '<div class="headline">Desarrollador de software</div>' in page and "E1.S1" not in page and "T22" not in page
    assert "E1.S1" not in info["revision"].read_text(encoding="utf-8").split("<h2>Perfil</h2>")[1].split("<ul>")[0]


def test_a_bullet_with_ids_is_saved_without_them(conn, tmp_path):
    add_offer(conn)
    data = good5(experiences=[entry5("E1", [bullet("E1.R1", "E1.R1: Lideré equipos para construir soluciones (E1.R1)")]), *good5()["experiences"][1:]])
    info = make(conn, tmp_path, FakeClient(response(data)))
    assert "<li>Lideré equipos para construir soluciones</li>" in info["html"].read_text(encoding="utf-8")


def test_a_bullet_that_is_empty_after_removing_ids_is_a_format_error_and_the_request_is_repeated(conn, tmp_path):
    add_offer(conn)
    data = good5(experiences=[entry5("E1", [bullet("E1.R1", " (E1.R1) T22 ")]), *good5()["experiences"][1:]])
    client = FakeClient(response(data), response(good5()))
    info = make(conn, tmp_path, client)
    assert len(client.calls) == 2 and "quedó vacía después de sacar los ids" in user_of(client.calls[1]) and info["version"] == "c8"


def test_the_system_prompt_says_ids_go_only_in_the_source_fields():
    assert "los ids van solo ahí" in cv.SYSTEM and "ni de las viñetas" in cv.SYSTEM


def test_an_acronym_needs_words_in_a_row_of_the_same_item_without_crossing_punctuation():
    assert "ia" in cv._acronyms(["Inteligencia Artificial"]) and "pln" in cv._acronyms(["Procesamiento de Lenguaje Natural"])
    found = cv._acronyms(["smart, home automation"])
    assert "sha" not in found and "ha" in found
    for text in ("smart; home automation", "smart: home automation", "smart. Home automation", "smart" + chr(10) + "home automation"):
        assert "sha" not in cv._acronyms([text]) and "ha" in cv._acronyms([text])
    assert "sha" in cv._acronyms(["smart home automation"])
    assert "na" in cv._acronyms(["Node.js apps"])  # un punto dentro de una palabra no corta
    base = resume_with(interests=["Inteligencia Artificial", "smart, home automation"], projects=[])
    vocab = cv._base_vocabulary(base, ())
    assert cv._covered("IA", vocab) and not cv._covered("SHA", vocab)


# ── TAREA-031: arreglo parcial en vez de caer entero al c4 ──────────────────

def angular_title() -> dict:
    return good5(headline="Desarrollador Angular")


def spend_ok(conn) -> list:
    return [row[0] for row in conn.execute("SELECT ok FROM api_spend ORDER BY rowid")]


def test_a_title_that_fails_twice_is_replaced_by_the_latest_relevant_position_and_the_rest_is_kept(conn, tmp_path):
    add_offer(conn)
    client = FakeClient(response(angular_title()))  # la última respuesta se repite
    info = make(conn, tmp_path, client)
    assert len(client.calls) == 2 and info["version"] == "c8" and info["fallback_reason"] is None
    assert len(info["replaced"]) == 1 and info["replaced"][0].startswith("el título: ") and "Angular" in info["replaced"][0]
    page = info["html"].read_text(encoding="utf-8")
    assert '<div class="headline">Senior Software Engineer</div>' in page and "Angular" not in page  # E1 es la relevante más reciente
    assert "<h2>Perfil</h2><p>Desarrollador con experiencia en liderazgo de equipos.</p>" in page
    assert "<li>Lideré equipos para construir soluciones empresariales</li>" in page
    assert len(spend_ok(conn)) == 2 and conn.execute("SELECT prompt_version FROM cvs").fetchone() == ("c8",)
    review = info["revision"].read_text(encoding="utf-8")
    assert "se reemplazaron" in review and "<li>el título: " in review and "Versión del armado: c8" in review and "Se armó solo con ids" not in review


def test_the_replaced_title_is_the_latest_relevant_position_and_empty_when_nothing_is_relevant(conn, tmp_path):
    add_offer(conn)
    only_old = good5(headline="Desarrollador Angular", experiences=[entry5("E1", [], block="otra"), entry5("E3", [bullet("E3.R1", "Programé aplicaciones")])])
    info = make(conn, tmp_path, FakeClient(response(only_old)))
    assert '<div class="headline">Junior Developer</div>' in info["html"].read_text(encoding="utf-8")  # E1 y E2 van a «otra»
    none = good5(headline="Desarrollador Angular", experiences=[entry5("E1", [], block="otra")])
    info = make(conn, tmp_path, FakeClient(response(none)))
    assert '<div class="headline">' not in info["html"].read_text(encoding="utf-8") and len(info["replaced"]) == 1


def test_a_profile_that_fails_twice_is_left_out_and_the_rest_is_kept(conn, tmp_path):
    add_offer(conn)
    bad = good5(summary={"text": "Experto en Node.js y Angular", "sources": ["E1.R1"]})
    info = make(conn, tmp_path, FakeClient(response(bad)))
    page = info["html"].read_text(encoding="utf-8")
    assert info["version"] == "c8" and "<h2>Perfil</h2>" not in page and '<div class="headline">Desarrollador de software</div>' in page
    assert "<li>Lideré equipos para construir soluciones empresariales</li>" in page
    assert len(info["replaced"]) == 1 and info["replaced"][0].startswith("el perfil: ")
    assert "<h2>Perfil</h2>" not in info["revision"].read_text(encoding="utf-8")


def test_a_bullet_that_fails_twice_is_replaced_by_the_text_of_its_source_marked_as_copy(conn, tmp_path):
    add_offer(conn)
    bad = good5(experiences=[entry5("E1", [bullet("E1.R1", "Desarrollé servicios con Node.js"), bullet("E1.R3", "Mejoré el rendimiento de las aplicaciones")],
                                    ["E1.S2"]), *good5()["experiences"][1:]])
    info = make(conn, tmp_path, FakeClient(response(bad)))
    page = info["html"].read_text(encoding="utf-8")
    assert info["version"] == "c8" and "Node.js" not in page
    assert "<li>Led a team of developers in designing and implementing enterprise software solutions</li>" in page  # el texto de E1.R1
    assert "<li>Mejoré el rendimiento de las aplicaciones</li>" in page  # la otra viñeta se conserva
    assert page.index("Led a team") < page.index("Mejoré el rendimiento")  # y el orden
    assert len(info["replaced"]) == 1 and info["replaced"][0].startswith("viñeta 1 de E1: ")
    review = info["revision"].read_text(encoding="utf-8")
    assert "<td>E1</td><td>Led a team of developers" in review and review.count("<td>copia</td>") == 1


def test_a_failing_bullet_whose_source_is_already_used_in_the_experience_is_removed(conn, tmp_path):
    add_offer(conn)
    bad = good5(experiences=[entry5("E1", [bullet("E1.R1", "Desarrollé servicios con Node.js"), bullet("E1.R1", "Lideré equipos de trabajo")], ["E1.S2"]),
                             *good5()["experiences"][1:]])
    info = make(conn, tmp_path, FakeClient(response(bad)))
    page = info["html"].read_text(encoding="utf-8")
    assert "Node.js" not in page and "<li>Lideré equipos de trabajo</li>" in page and "Led a team of developers" not in page
    assert info["version"] == "c8" and len(info["replaced"]) == 1
    two = good5(experiences=[entry5("E1", [bullet("E1.R1", "Desarrollé con Node.js"), bullet("E1.R1", "Armé con AWS y Rust")]), *good5()["experiences"][1:]])
    info = make(conn, tmp_path, FakeClient(response(two)))
    assert info["html"].read_text(encoding="utf-8").count("Led a team of developers") == 1  # la segunda, con la misma fuente, se saca
    assert len(info["replaced"]) == 2


def test_a_failing_bullet_of_an_other_experience_is_replaced_too(conn, tmp_path):
    add_offer(conn)
    bad = good5(experiences=[*good5()["experiences"][:2], entry5("E3", [bullet("E3.R2", "Revisé código en Node.js")], block="otra")])
    info = make(conn, tmp_path, FakeClient(response(bad)))
    other = block(info["html"].read_text(encoding="utf-8"), "Otra experiencia")
    assert "<li>Participated in code reviews and contributed to software design discussions</li>" in other and "Node.js" not in other


def test_the_bank_item_of_a_failing_bullet_is_used_as_its_source_text(conn, tmp_path):
    add_offer(conn)
    prof = {**PROFILE, "variants": [{**PROFILE["variants"][0], "truths": [TRUTH]}]}
    bad = good5(experiences=[entry5("E1", [bullet("X1", "Usé Rust y Go en la tecnicatura")]), *good5()["experiences"][1:]])
    info = make(conn, tmp_path, FakeClient(response(bad)), prof=prof)
    assert f"<li>{TRUTH}</li>" in info["html"].read_text(encoding="utf-8") and info["version"] == "c8"


def test_an_answer_with_a_valid_second_try_has_nothing_replaced(conn, tmp_path):
    add_offer(conn)
    info = make(conn, tmp_path, FakeClient(response(angular_title()), response(good5())))
    assert info["replaced"] == [] and info["fallback_reason"] is None and spend_ok(conn) == [0, 1]
    assert "se reemplazaron" not in info["revision"].read_text(encoding="utf-8")


def test_a_format_error_twice_still_gives_c4_even_if_other_lines_fail(conn, tmp_path):
    add_offer(conn)
    bad = good5(headline="Desarrollador Angular", experiences=[entry5("E1", [bullet("E9.R1", "Hice algo")]), *good5()["experiences"][1:]])
    info = make(conn, tmp_path, FakeClient(response(bad)))
    assert info["version"] == "c4" and "E9.R1" in info["fallback_reason"] and info["replaced"] == []
    assert spend_ok(conn) == [0, 0]


def test_the_cli_and_the_window_say_how_many_lines_were_replaced(cli_env, tmp_path, capsys):
    cli_env.client = FakeClient(response(angular_title()))
    assert cli.main(cli_args(tmp_path, "--id", OFFER_ID)) == 0
    out = capsys.readouterr().out
    assert "Se reemplazó 1 línea: el título: " in out and "Angular" in out and "Se armó solo con ids" not in out
    info = {"pdf": "x.pdf", "cost_usd": 0.0004, "fallback_reason": None, "replaced": ["a: b.", "c: d."]}
    assert "Se reemplazaron 2 líneas: a: b. c: d." in ui_logic.cv_line(info)
    assert "Se reemplaz" not in ui_logic.cv_line({**info, "replaced": []})


def test_the_revision_lists_the_replaced_lines_escaped():
    review = cv.build_revision(RESUME, {"headline": "", "summary": {"text": "", "sources": []}, "experiences": {}}, replaced=["el título: <b>x</b>"])
    assert "<li>el título: &lt;b&gt;x&lt;/b&gt;</li>" in review and "<b>" not in review


# ── TAREA-032: banco de verdades propuesto por el programa y aprobado por el usuario ──

def proposal(text, sources, reason="Lo hizo en su trabajo") -> dict:
    return {"text": text, "sources": list(sources), "reason": reason}


def proposals_response(*items, tokens=(1000, 0, 200)):
    return response({"proposals": list(items)}, tokens=tokens)


JS = proposal("Usé JavaScript y npm en aplicaciones web", ["E2.R1"], "Desarrolló aplicaciones web")
TEAM = proposal("Trabajé en equipo con diseñadores", ["E2.R2"])


def test_propose_truths_keeps_the_valid_ones_and_the_request_has_no_personal_data():
    client = FakeClient(proposals_response(JS, TEAM))
    found = cv.propose_truths(RESUME, client, "gpt-6-luna")
    assert found == [JS, TEAM]
    sent = json.dumps(client.calls[0], ensure_ascii=False)
    assert [value for value in PERSONAL if value in sent and value != "Rome"] == []  # «University of Rome» es un estudio, no un dato personal
    assert "github.com" not in sent and "linkedin.com" not in sent and "Via Nazionale" not in sent
    user = client.calls[0]["input"][1]["content"]
    assert "E2.R1: Developed and maintained web applications" in user and "S1: Master's Degree - Computer Engineering" in user
    assert "Materias: " in user and "Computer Networks" in user  # las materias de un estudio son fuente
    schema = client.calls[0]["text"]["format"]["schema"]["properties"]["proposals"]
    assert schema["maxItems"] == 25 and "S1" in schema["items"]["properties"]["sources"]["items"]["enum"]
    assert "E9.R1" not in schema["items"]["properties"]["sources"]["items"]["enum"]


@pytest.mark.parametrize("bad", [
    proposal("Usé Python durante 5 años", ["E1.R1"]),
    proposal("Fui desarrollador Senior de Java", ["E1.R1"]),
    proposal("Trabajé con Docker", ["E9.R1"]),
    proposal("Trabajé con Docker", []),
    proposal("Python", ["T1"]),  # repite un item del CV
    proposal("python", ["T1"]),
    proposal("Hice lo de E1.R1", ["E1.R1"]),
    proposal("x" * 121, ["E1.R1"]),
    proposal("", ["E1.R1"]),
    {"text": 5, "sources": ["E1.R1"], "reason": "x"},
    "no es un objeto",
])
def test_propose_truths_discards_numbers_levels_bad_sources_repeats_and_long_texts(bad):
    found = cv.propose_truths(RESUME, FakeClient(proposals_response(bad, JS)), "gpt-6-luna")
    assert found == [JS]


def test_propose_truths_discards_what_is_known_and_what_repeats_inside_the_answer():
    client = FakeClient(proposals_response(JS, proposal("usé javascript y npm en aplicaciones web.", ["E2.R1"]), TEAM, TEAM))
    found = cv.propose_truths(RESUME, client, "gpt-6-luna", known=[TEAM["text"]])
    assert found == [JS]
    assert f"- {TEAM['text']}" in client.calls[0]["input"][1]["content"] and "Ya propuesto, aprobado o rechazado" in client.calls[0]["input"][1]["content"]


def test_propose_truths_cuts_a_long_reason_and_dedupes_sources():
    found = cv.propose_truths(RESUME, FakeClient(proposals_response(proposal("Usé JavaScript", ["E2.R1", "E2.R1"], "r" * 300))), "gpt-6-luna")
    assert found[0]["sources"] == ["E2.R1"] and len(found[0]["reason"]) == 120


def test_propose_truths_gives_at_most_25_and_costs_are_reported():
    many = [proposal(f"Hecho {chr(97 + n)}{chr(97 + n)}", ["E1.R1"]) for n in range(26)]
    calls = []
    found = cv.propose_truths(RESUME, FakeClient(proposals_response(*many)), "gpt-6-luna", calls=calls)
    assert len(found) == 25 and calls[0]["valid"] is True and calls[0]["tokens"] == (1000, 0, 200)


@pytest.mark.parametrize("resp, message", [
    (SimpleNamespace(output_text="[1]x", output=[], status="completed", usage=None), "JSON válido"),
    (SimpleNamespace(output_text="{}", output=[], status="incomplete", usage=None), "no terminó"),
    (response({"otra": []}), "formato esperado"),
])
def test_propose_truths_with_an_unusable_answer_raises_and_reports_the_call(resp, message):
    calls = []
    with pytest.raises(RuntimeError, match=message):
        cv.propose_truths(RESUME, FakeClient(resp), "gpt-6-luna", calls=calls)
    assert len(calls) == 1 and calls[0]["valid"] is False


def test_a_cv_without_items_asks_for_nothing():
    client = FakeClient()
    assert cv.propose_truths({"personal_information": {"name": "X"}}, client, "gpt-6-luna") == [] and client.calls == []


def spend_rows(db_path):
    connection = db.connect(db_path)
    try:
        return connection.execute("SELECT kind, id16, model, ok, input_tokens, output_tokens FROM api_spend ORDER BY rowid").fetchall()
    finally:
        connection.close()


def test_propose_saves_the_pending_ones_in_the_proposals_file_and_spends_with_kind_truths(tmp_path):
    write_workspace(tmp_path, key=False)
    prof = tmp_path / "profile.yaml"
    info = ui_logic.propose("backend", prof, tmp_path / "t.db", FakeClient(proposals_response(JS, TEAM)), "gpt-6-luna")
    assert info["count"] == 2 and info["pending"] == 2 and info["cost_usd"] == pytest.approx((1000 * 0.10 + 200 * 0.50) / 1_000_000)
    saved = yaml.safe_load((tmp_path / "cv" / "truths-backend.propuestas.yaml").read_text(encoding="utf-8"))
    assert saved["propuestas"] == [JS, TEAM] and saved["rechazadas"] == []
    assert spend_rows(tmp_path / "t.db") == [("truths", "backend", "gpt-6-luna", 1, 1000, 200)]
    connection = db.connect(tmp_path / "t.db")
    assert score.month_spent(connection, datetime.now(timezone.utc).date()) == pytest.approx(info["cost_usd"])
    connection.close()
    assert ui_logic.propose_line(info) == "Propuso 2 verdades nuevas · costo: US$ 0,0002."
    assert not (tmp_path / "cv" / "truths-backend.yaml").exists()  # proponer no aprueba nada
    assert "truths" not in yaml.safe_load(prof.read_text(encoding="utf-8"))["variants"][0]


def test_propose_adds_to_the_pending_ones_and_does_not_repeat_the_known(tmp_path):
    write_workspace(tmp_path, key=False)
    prof = tmp_path / "profile.yaml"
    ui_logic.propose("backend", prof, tmp_path / "t.db", FakeClient(proposals_response(JS)), "gpt-6-luna")
    client = FakeClient(proposals_response(JS, TEAM))
    info = ui_logic.propose("backend", prof, tmp_path / "t.db", client, "gpt-6-luna")
    assert info["count"] == 1 and info["pending"] == 2 and ui_logic.propose_line({"count": 1, "cost_usd": 0.0001}).startswith("Propuso 1 verdad nueva")
    assert [p["text"] for p in ui_logic.load_proposals("backend", prof)["propuestas"]] == [JS["text"], TEAM["text"]]
    assert f"- {JS['text']}" in client.calls[0]["input"][1]["content"]


def test_a_failed_answer_still_spends_with_ok_zero_and_saves_nothing(tmp_path):
    write_workspace(tmp_path, key=False)
    with pytest.raises(RuntimeError, match="JSON válido"):
        ui_logic.propose("backend", tmp_path / "profile.yaml", tmp_path / "t.db",
                         FakeClient(SimpleNamespace(output_text="x", output=[], status="completed", usage=SimpleNamespace(
                             input_tokens=10, input_tokens_details=SimpleNamespace(cached_tokens=0), output_tokens=5))), "gpt-6-luna")
    assert spend_rows(tmp_path / "t.db") == [("truths", "backend", "gpt-6-luna", 0, 10, 5)]
    assert not (tmp_path / "cv" / "truths-backend.propuestas.yaml").exists()


def test_propose_with_the_monthly_cap_reached_does_not_call_the_api(tmp_path):
    write_workspace(tmp_path, key=False)
    prof = tmp_path / "profile.yaml"
    data = yaml.safe_load(prof.read_text(encoding="utf-8"))
    data["run"] = {"max_cost_month": 0.0001}
    prof.write_text(yaml.safe_dump(data), encoding="utf-8")
    connection = db.connect(tmp_path / "t.db")
    db.add_spend(connection, "truths", "backend", "gpt-6-luna", 0.5, 1, 1, True)
    connection.close()
    client = FakeClient()
    with pytest.raises(RuntimeError, match="Tope mensual alcanzado"):
        ui_logic.propose("backend", prof, tmp_path / "t.db", client, "gpt-6-luna")
    assert client.calls == [] and len(spend_rows(tmp_path / "t.db")) == 1


def test_propose_checks_the_variant_and_the_model_price_before_calling(tmp_path):
    write_workspace(tmp_path, key=False)
    client = FakeClient()
    with pytest.raises(RuntimeError, match="no está en profile.yaml"):
        ui_logic.propose("otra", tmp_path / "profile.yaml", tmp_path / "t.db", client, "gpt-6-luna")
    with pytest.raises(RuntimeError, match="precio del modelo"):
        ui_logic.propose("backend", tmp_path / "profile.yaml", tmp_path / "t.db", client, "modelo-raro")
    assert client.calls == []


def seeded(tmp_path, *items):
    write_workspace(tmp_path, key=False)
    prof = tmp_path / "profile.yaml"
    ui_logic.propose("backend", prof, tmp_path / "t.db", FakeClient(proposals_response(*items)), "gpt-6-luna")
    return prof


def test_approve_moves_the_text_to_the_bank_and_declares_truths_keeping_the_rest_of_the_profile(tmp_path):
    prof = seeded(tmp_path, JS, TEAM)
    data = yaml.safe_load(prof.read_text(encoding="utf-8"))
    data["apply"] = {"threshold": 70, "daily_cap": 5, "calibrated_with": "gpt-6-luna v5"}
    data["exclude_companies"] = ["Mala SA"]
    prof.write_text(yaml.safe_dump(data), encoding="utf-8")
    assert ui_logic.approve("backend", [JS["text"]], prof) == 1
    assert yaml.safe_load((tmp_path / "cv" / "truths-backend.yaml").read_text(encoding="utf-8")) == [JS["text"]]
    saved = yaml.safe_load(prof.read_text(encoding="utf-8"))
    assert saved["variants"][0]["truths"] == "cv/truths-backend.yaml" and saved["apply"]["calibrated_with"] == "gpt-6-luna v5"
    assert saved["exclude_companies"] == ["Mala SA"] and saved["apply"]["threshold"] == 70
    assert [p["text"] for p in ui_logic.load_proposals("backend", prof)["propuestas"]] == [TEAM["text"]]
    assert ui_logic.truths_path("backend", prof) == tmp_path / "cv" / "truths-backend.yaml"
    assert profile.load_profile(prof)["variants"][0]["truths"] == [JS["text"]]


def test_approve_all_and_a_second_approval_add_to_the_bank_without_duplicates(tmp_path):
    prof = seeded(tmp_path, JS, TEAM)
    assert ui_logic.approve("backend", [JS["text"]], prof) == 1
    assert ui_logic.approve("backend", [TEAM["text"]], prof) == 1
    assert ui_logic.load_truths("backend", prof) == [JS["text"], TEAM["text"]]
    assert ui_logic.load_proposals("backend", prof)["propuestas"] == []


def test_approve_uses_the_bank_the_profile_already_declares(tmp_path):
    prof = seeded(tmp_path, JS)
    data = yaml.safe_load(prof.read_text(encoding="utf-8"))
    data["variants"][0]["truths"] = "mis-verdades.yaml"
    (tmp_path / "mis-verdades.yaml").write_text("- Algo cierto\n", encoding="utf-8")
    prof.write_text(yaml.safe_dump(data), encoding="utf-8")
    ui_logic.approve("backend", [JS["text"]], prof)
    assert yaml.safe_load((tmp_path / "mis-verdades.yaml").read_text(encoding="utf-8")) == ["Algo cierto", JS["text"]]
    assert not (tmp_path / "cv" / "truths-backend.yaml").exists()
    assert yaml.safe_load(prof.read_text(encoding="utf-8"))["variants"][0]["truths"] == "mis-verdades.yaml"


def test_approving_or_rejecting_a_text_that_is_not_pending_is_an_error_that_does_not_quote_it(tmp_path):
    prof = seeded(tmp_path, JS)
    for action in (ui_logic.approve, ui_logic.reject):
        with pytest.raises(RuntimeError, match="ya no está en la lista") as err:
            action("backend", ["Un texto secreto"], prof)
        assert "secreto" not in str(err.value)
    assert not (tmp_path / "cv" / "truths-backend.yaml").exists()


def test_reject_saves_it_in_rechazadas_and_a_new_equal_proposal_is_discarded(tmp_path):
    prof = seeded(tmp_path, JS, TEAM)
    assert ui_logic.reject("backend", [JS["text"]], prof) == 1
    saved = yaml.safe_load((tmp_path / "cv" / "truths-backend.propuestas.yaml").read_text(encoding="utf-8"))
    assert saved["rechazadas"] == [JS["text"]] and [p["text"] for p in saved["propuestas"]] == [TEAM["text"]]
    client = FakeClient(proposals_response(JS, proposal("Usé Git para versionar", ["E2.R3"])))
    info = ui_logic.propose("backend", prof, tmp_path / "t.db", client, "gpt-6-luna")
    assert info["count"] == 1 and f"- {JS['text']}" in client.calls[0]["input"][1]["content"]
    assert JS["text"] not in [p["text"] for p in ui_logic.load_proposals("backend", prof)["propuestas"]]
    assert not (tmp_path / "cv" / "truths-backend.yaml").exists()  # rechazar no aprueba


def test_add_and_remove_truth_by_hand(tmp_path):
    write_workspace(tmp_path, key=False)
    prof = tmp_path / "profile.yaml"
    ui_logic.add_truth("backend", "  Uso   Git y GitHub  ", prof)
    assert ui_logic.load_truths("backend", prof) == ["Uso Git y GitHub"]
    assert yaml.safe_load(prof.read_text(encoding="utf-8"))["variants"][0]["truths"] == "cv/truths-backend.yaml"
    with pytest.raises(RuntimeError, match="ya está en el banco"):
        ui_logic.add_truth("backend", "Uso Git y GitHub", prof)
    with pytest.raises(RuntimeError, match="Escribí el hecho"):
        ui_logic.add_truth("backend", "   ", prof)
    ui_logic.remove_truth("backend", "Uso Git y GitHub", prof)
    assert ui_logic.load_truths("backend", prof) == [] and profile.load_profile(prof)["variants"][0]["truths"] == []
    with pytest.raises(RuntimeError, match="ya no está en el banco"):
        ui_logic.remove_truth("backend", "Uso Git y GitHub", prof)


def test_a_variant_that_is_not_in_the_profile_cannot_declare_a_bank(tmp_path):
    write_workspace(tmp_path, key=False)
    with pytest.raises(RuntimeError, match="no está en profile.yaml"):
        ui_logic.add_truth("otra", "Algo cierto", tmp_path / "profile.yaml")


def test_a_broken_proposals_file_gives_a_clear_error_that_does_not_quote_it(tmp_path):
    write_workspace(tmp_path, key=False)
    path = tmp_path / "cv" / "truths-backend.propuestas.yaml"
    for content in ("propuestas: [1, 2]\n", "[a, b]\n", "propuestas: [{text: 5, sources: [], reason: x}]\n", "rechazadas: [3]\n", "a: [sin cerrar\n"):
        path.write_text(content, encoding="utf-8")
        with pytest.raises(RuntimeError, match="propuestas de la variante 'backend'") as err:
            ui_logic.load_proposals("backend", tmp_path / "profile.yaml")
        assert "sin cerrar" not in str(err.value)
    path.write_text("", encoding="utf-8")
    assert ui_logic.load_proposals("backend", tmp_path / "profile.yaml") == {"propuestas": [], "rechazadas": []}


def test_bank_view_shows_the_source_lines_of_each_proposal(tmp_path):
    prof = seeded(tmp_path, JS, proposal("Entiendo redes de computadoras", ["S1"], "Cursó la materia"))
    ui_logic.add_truth("backend", "Uso Git", prof)
    view = ui_logic.bank_view("backend", prof)
    assert view["banco"] == ["Uso Git"] and view["rechazadas"] == 0
    assert view["propuestas"][0]["source_lines"] == ["Developed and maintained web applications using modern technologies"]
    assert "Materias: " in view["propuestas"][1]["source_lines"][0] and "Computer Networks" in view["propuestas"][1]["source_lines"][0]
    with pytest.raises(RuntimeError, match="no está en profile.yaml"):
        ui_logic.bank_view("otra", prof)


def test_the_approved_bank_is_used_by_make_with_x_ids(conn, tmp_path):
    prof_path = seeded(tmp_path, JS)
    ui_logic.approve("backend", [JS["text"]], prof_path)
    loaded = profile.load_profile(prof_path)
    add_offer(conn)
    client = FakeClient()
    info = make(conn, tmp_path, client, prof={**loaded, "run": {"max_cost_month": 2.0}})
    sent = client.calls[0]["input"][1]["content"]
    assert f"X1: {JS['text']}" in sent and "Banco de verdades" in sent and info["version"] == "c8"
    assert "X1" in client.calls[0]["text"]["format"]["schema"]["properties"]["technical_skills"]["items"]["enum"]


def test_cli_truths_propose_prints_the_count_and_the_cost_never_the_text(cli_env, tmp_path, capsys):
    cli_env.client = FakeClient(proposals_response(JS, TEAM))
    args = ["cv", "truths", "propose", "--variant", "backend", "--profile", str(tmp_path / "profile.yaml"), "--db", str(tmp_path / "t.db")]
    assert cli.main(args) == 0
    out = capsys.readouterr()
    assert "Propuso 2 verdades nuevas · costo: US$ 0,0002." in out.out and "Pendientes de aprobar: 2" in out.out and out.err == ""
    assert "JavaScript" not in out.out and "sk-test" not in out.out
    assert [row[0] for row in spend_rows(tmp_path / "t.db")] == ["truths"]


def test_cli_truths_list_shows_the_bank_and_the_pending_ones(cli_env, tmp_path, capsys):
    prof = tmp_path / "profile.yaml"
    ui_logic.propose("backend", prof, tmp_path / "t.db", FakeClient(proposals_response(JS, TEAM)), "gpt-6-luna")
    ui_logic.approve("backend", [JS["text"]], prof)
    assert cli.main(["cv", "truths", "list", "--variant", "backend", "--profile", str(prof)]) == 0
    out = capsys.readouterr().out
    assert "Banco aprobado (1):" in out and f"- {JS['text']}" in out and "Propuestas pendientes (1):" in out and TEAM["text"] in out
    assert "Rechazadas: 0" in out


def test_cli_truths_errors_exit_1_without_calling_the_api(cli_env, tmp_path, capsys):
    cli_env.client = FakeClient()
    args = ["cv", "truths", "propose", "--variant", "otra", "--profile", str(tmp_path / "profile.yaml"), "--db", str(tmp_path / "t.db")]
    assert cli.main(args) == 1
    assert "no está en profile.yaml" in capsys.readouterr().err and cli_env.client.calls == []


def test_run_propose_uses_the_key_and_model_of_the_env_file(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    write_workspace(tmp_path)
    keys, client = [], FakeClient(proposals_response(JS))
    info = ui_logic.run_propose(tmp_path / ".env", tmp_path / "profile.yaml", tmp_path / "t.db", "backend",
                                lambda api_key: (keys.append(api_key), client)[1])
    assert keys == ["sk-test-FALSA-123"] and info["count"] == 1 and client.calls[0]["model"] == "gpt-6-luna"


def test_run_propose_without_a_key_does_not_build_a_client(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    write_workspace(tmp_path, key=False)
    with pytest.raises(RuntimeError, match="Falta la clave de OpenAI"):
        ui_logic.run_propose(tmp_path / ".env", tmp_path / "profile.yaml", tmp_path / "t.db", "backend",
                             lambda api_key: pytest.fail("no tenía que armarse el cliente"))


# ── TAREA-048: un solo tiempo verbal por viñeta ──────────────────────────────

def test_the_system_prompt_asks_for_the_nominal_style_and_one_tense():
    assert "estilo nominal" in cv.SYSTEM and "Control y reposición de" in cv.SYSTEM
    assert "sin verbos conjugados en primera persona" in cv.SYSTEM and "en el mismo tiempo" in cv.SYSTEM


def test_the_cv_request_carries_the_nominal_style_rule(conn, tmp_path):
    add_offer(conn)
    client = FakeClient()
    make(conn, tmp_path, client)
    sent = json.dumps(client.calls[0], ensure_ascii=False)
    assert "estilo nominal" in sent and "Control y reposición de" in sent


@pytest.mark.parametrize("text", ["Controlé y reponía la mercadería", "Gestioné pedidos y atendía clientes", "Atendía el mostrador y cerré la caja"])
def test_a_bullet_that_mixes_preterite_and_imperfect_is_detected(text):
    assert cv.mixed_tenses(text) is not None
    assert any("mezcla tiempos verbales" in p for p in problems(text, resume_with(technical_skills=[], certifications=[])))


@pytest.mark.parametrize("text", [
    "Control y reposición de mercadería", "Atención en cafetería y mensajería", "Controlé y repuse mercadería",
    "Controlaba y reponía mercadería", "Gestión de categoría y garantía", "Auditoría y tecnología de la información",
    "Atención de panadería, librería y ingeniería", "Trabajo de lunes a viernes, cada día", "Controlé el café y el comité",
])
def test_bullets_with_one_tense_or_nouns_in_ia_are_not_detected(text):
    assert cv.mixed_tenses(text) is None


def test_the_open_answers_of_the_questions_do_not_use_the_tense_control():
    from compuapply import apply
    assert apply._open_text_problems("Trabajé en ventas y me gustaba atender", RESUME, {}) == []
