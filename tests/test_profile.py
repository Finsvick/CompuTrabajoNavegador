"""Tests de profile.py y de `profile check`. Solo datos falsos."""
import re
from pathlib import Path

import pytest
import yaml

from compuapply import cli, profile

ROOT = Path(__file__).parent.parent
EXAMPLE = ROOT / "profile.example.yaml"

FAKE_RESUME = {
    "personal_information": {"name": "Ana", "surname": "Falsa", "email": "ana@falso.test"},
    "experience_details": [{"position": "Dev", "company": "Acme"}],
}


def write_yaml(path: Path, data) -> Path:
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return path


PDF_BYTES = b"%PDF-1.4\n%%EOF\n"


def make_profile(tmp_path: Path, variants: list[dict], **extra) -> Path:
    """profile.yaml en tmp_path; cada variante con `resume: dict` se escribe como su propio CV.

    Si la variante no trae la clave `pdf`, se le crea un PDF válido `<id>.pdf` (con `pdf: None` queda sin PDF).
    """
    for v in variants:
        if isinstance(v.get("resume"), dict):
            v["resume"] = write_yaml(tmp_path / f"{v['id']}.yaml", v["resume"]).name
        if "pdf" not in v:
            v["pdf"] = f"{v['id']}.pdf"
            (tmp_path / v["pdf"]).write_bytes(PDF_BYTES)
    return write_yaml(tmp_path / "profile.yaml", {"variants": variants, **extra})


def pdf_profile(tmp_path: Path, name: str, content: bytes | None) -> Path:
    """Perfil de una variante 'a' cuyo `pdf` es `name`; si `content` no es None, se escribe ese archivo."""
    if content is not None:
        (tmp_path / name).write_bytes(content)
    return make_profile(tmp_path, [{"id": "a", "resume": dict(FAKE_RESUME), "pdf": name}])


# ── load_profile ─────────────────────────────────────────────────────────────

def test_load_example_profile():
    loaded = profile.load_profile(EXAMPLE)
    [variant] = loaded["variants"]
    assert variant["id"] == "backend"
    assert variant["keywords"] == ["python", "backend", "api"]
    assert variant["resume"]["experience_details"]
    assert (loaded["exclude_companies"], loaded["exclude_title_words"]) == ([], [])


def test_load_profile_resolves_resume_relative_to_profile_folder(tmp_path, monkeypatch):
    monkeypatch.chdir(ROOT)  # otro cwd: la ruta relativa no depende de dónde se corre
    path = make_profile(tmp_path, [{"id": "a", "resume": dict(FAKE_RESUME)}],
                        exclude_companies=["Mala SA"], exclude_title_words="ventas")
    loaded = profile.load_profile(path)
    assert loaded["exclude_companies"] == ["Mala SA"]
    assert loaded["exclude_title_words"] == ["ventas"]
    assert loaded["variants"][0]["keywords"] == []


def test_load_profile_accepts_absolute_resume_path(tmp_path):
    (tmp_path / "cv").mkdir()
    resume = write_yaml(tmp_path / "cv" / "abs.yaml", FAKE_RESUME)
    pdf = tmp_path / "cv" / "abs.pdf"
    pdf.write_bytes(PDF_BYTES)
    path = write_yaml(tmp_path / "profile.yaml", {"variants": [{"id": "a", "resume": str(resume), "pdf": str(pdf)}]})
    assert profile.load_profile(path)["variants"][0]["resume"]["experience_details"]


def test_load_profile_accepts_projects_without_experience(tmp_path):
    path = make_profile(tmp_path, [{"id": "a", "resume": {"projects": [{"name": "P"}]}}])
    assert profile.load_profile(path)["variants"][0]["id"] == "a"


def test_error_profile_missing(tmp_path):
    with pytest.raises(RuntimeError, match=r"No existe el perfil.*profile\.yaml"):
        profile.load_profile(tmp_path / "profile.yaml")


@pytest.mark.parametrize("variants", [[], None])
def test_error_no_variants(tmp_path, variants):
    path = write_yaml(tmp_path / "profile.yaml", {} if variants is None else {"variants": variants})
    with pytest.raises(RuntimeError, match="1 a 3 variantes"):
        profile.load_profile(path)


def test_error_more_than_three_variants(tmp_path):
    path = make_profile(tmp_path, [{"id": f"v{n}", "resume": dict(FAKE_RESUME)} for n in range(4)])
    with pytest.raises(RuntimeError, match="1 a 3 variantes"):
        profile.load_profile(path)


def test_error_duplicate_id(tmp_path):
    path = make_profile(tmp_path, [{"id": "a", "resume": dict(FAKE_RESUME)}, {"id": "a", "resume": dict(FAKE_RESUME)}])
    with pytest.raises(RuntimeError, match="'a' está repetido"):
        profile.load_profile(path)


@pytest.mark.parametrize("vid", ["", "   ", None])
def test_error_empty_id(tmp_path, vid):
    path = write_yaml(tmp_path / "profile.yaml", {"variants": [{"id": vid, "resume": "x.yaml"}]})
    with pytest.raises(RuntimeError, match="no tiene 'id'"):
        profile.load_profile(path)


def test_error_missing_resume_key(tmp_path):
    path = write_yaml(tmp_path / "profile.yaml", {"variants": [{"id": "a"}]})
    with pytest.raises(RuntimeError, match="no tiene 'resume'"):
        profile.load_profile(path)


def test_error_resume_file_missing(tmp_path):
    path = write_yaml(tmp_path / "profile.yaml", {"variants": [{"id": "a", "resume": "no-esta.yaml"}]})
    with pytest.raises(RuntimeError, match=r"No existe el CV de la variante 'a'.*profile\.yaml.*no-esta\.yaml"):
        profile.load_profile(path)


@pytest.mark.parametrize("resume", [{"personal_information": {"name": "Ana"}}, {"experience_details": [], "projects": None}])
def test_error_resume_without_experience_or_projects(tmp_path, resume):
    path = make_profile(tmp_path, [{"id": "a", "resume": resume}])
    with pytest.raises(RuntimeError, match="'experience_details' ni 'projects'"):
        profile.load_profile(path)


# ── pdf por variante ─────────────────────────────────────────────────────────

def test_example_profile_resolves_the_example_pdf():
    [variant] = profile.load_profile(EXAMPLE)["variants"]
    assert variant["pdf"] == (ROOT / "examples" / "cv.example.pdf").resolve()


def test_pdf_path_is_resolved_relative_to_the_profile_folder(tmp_path, monkeypatch):
    monkeypatch.chdir(ROOT)
    path = make_profile(tmp_path, [{"id": "a", "resume": dict(FAKE_RESUME)}])
    assert profile.load_profile(path)["variants"][0]["pdf"] == (tmp_path / "a.pdf").resolve()


def test_pdf_accepts_uppercase_extension_and_exactly_the_maximum_size(tmp_path):
    big = PDF_BYTES + b"0" * (profile.MAX_PDF_BYTES - len(PDF_BYTES))
    assert len(big) == profile.MAX_PDF_BYTES
    assert profile.load_profile(pdf_profile(tmp_path, "CV.PDF", big))["variants"][0]["pdf"].name == "CV.PDF"


def test_pdf_is_optional(tmp_path):
    path = make_profile(tmp_path, [{"id": "a", "resume": dict(FAKE_RESUME), "pdf": None},
                                   {"id": "b", "resume": dict(FAKE_RESUME)}])
    loaded = profile.load_profile(path)
    assert loaded["variants"][0]["pdf"] is None
    assert loaded["variants"][1]["pdf"] == (tmp_path / "b.pdf").resolve()
    assert not hasattr(profile, "missing_pdf")  # sin la nota de «variantes sin PDF», nadie la usa


@pytest.mark.parametrize("pdf", [None, "", "sin-clave"])
def test_pdf_key_absent_empty_or_null_means_no_pdf(tmp_path, pdf):
    variant = {"id": "a", "resume": dict(FAKE_RESUME)}
    if pdf != "sin-clave":
        variant["pdf"] = pdf
    path = make_profile(tmp_path, [variant])
    if pdf == "sin-clave":  # make_profile agrega un PDF si falta la clave: se la saca del YAML
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        del data["variants"][0]["pdf"]
        write_yaml(path, data)
    assert profile.load_profile(path)["variants"][0]["pdf"] is None


def test_a_declared_pdf_is_still_validated_when_another_variant_has_none(tmp_path):
    (tmp_path / "b.pdf").write_bytes(b"no es un pdf")
    path = make_profile(tmp_path, [{"id": "a", "resume": dict(FAKE_RESUME), "pdf": None},
                                   {"id": "b", "resume": dict(FAKE_RESUME), "pdf": "b.pdf"}])
    with pytest.raises(RuntimeError, match=r"variante 'b'.*%PDF-"):
        profile.load_profile(path)


def test_two_variants_without_pdf_are_not_a_duplicate(tmp_path):
    path = make_profile(tmp_path, [{"id": "a", "resume": dict(FAKE_RESUME), "pdf": None},
                                   {"id": "b", "resume": dict(FAKE_RESUME), "pdf": None}])
    assert [v["pdf"] for v in profile.load_profile(path)["variants"]] == [None, None]


def test_error_pdf_does_not_exist(tmp_path):
    with pytest.raises(RuntimeError, match=r"PDF de la variante 'a' \(no-esta\.pdf\) no existe"):
        profile.load_profile(pdf_profile(tmp_path, "no-esta.pdf", None))


@pytest.mark.parametrize("name", ["cv.docx", "cv.txt", "cv"])
def test_error_pdf_other_extension(tmp_path, name):
    with pytest.raises(RuntimeError, match=r"variante 'a'.*no tiene extensión .pdf"):
        profile.load_profile(pdf_profile(tmp_path, name, PDF_BYTES))


def test_error_pdf_without_signature_does_not_quote_its_content(tmp_path):
    with pytest.raises(RuntimeError, match=r"variante 'a'.*no empieza con %PDF-") as err:
        profile.load_profile(pdf_profile(tmp_path, "cv.pdf", b"Hola, soy el SECRETO-PDF " * 10))
    assert "SECRETO" not in str(err.value)


def test_error_pdf_empty(tmp_path):
    with pytest.raises(RuntimeError, match=r"variante 'a'.*está vacío"):
        profile.load_profile(pdf_profile(tmp_path, "cv.pdf", b""))


def test_error_pdf_over_5_mb(tmp_path):
    with pytest.raises(RuntimeError, match=r"variante 'a'.*pesa 6\.00 MB y el máximo es 5 MB"):
        profile.load_profile(pdf_profile(tmp_path, "cv.pdf", PDF_BYTES + b"0" * (6 * 1024 * 1024 - len(PDF_BYTES))))


def test_error_pdf_one_byte_over_the_maximum(tmp_path):
    with pytest.raises(RuntimeError, match="el máximo es 5 MB"):
        profile.load_profile(pdf_profile(tmp_path, "cv.pdf", PDF_BYTES + b"0" * (profile.MAX_PDF_BYTES - len(PDF_BYTES) + 1)))


def test_error_two_variants_with_the_same_pdf(tmp_path):
    (tmp_path / "x.pdf").write_bytes(PDF_BYTES)
    for spelling in ("x.pdf", "./x.pdf"):  # distinta escritura, mismo archivo
        path = make_profile(tmp_path, [{"id": "a", "resume": dict(FAKE_RESUME), "pdf": "x.pdf"},
                                       {"id": "b", "resume": dict(FAKE_RESUME), "pdf": spelling}])
        with pytest.raises(RuntimeError, match=r"variantes 'a' y 'b' usan el mismo PDF"):
            profile.load_profile(path)


def test_example_pdf_is_a_valid_minimal_pdf():
    data = (ROOT / "examples" / "cv.example.pdf").read_bytes()
    assert data.startswith(b"%PDF-") and data.rstrip().endswith(b"%%EOF")
    assert b"(CV de ejemplo) Tj" in data and data.count(b"/Type /Page ") == 1
    start = int(data.rsplit(b"startxref\n", 1)[1].split()[0])
    assert data[start:].startswith(b"xref\n0 6\n")
    entries = data[start + len(b"xref\n0 6\n"):][:6 * 20]
    assert entries[:20] == b"0000000000 65535 f \n"
    for n in range(1, 6):  # cada entrada del xref apunta al "N 0 obj" correspondiente
        offset = int(entries[n * 20:n * 20 + 10])
        assert data[offset:].startswith(f"{n} 0 obj\n".encode())


# ── sección apply ────────────────────────────────────────────────────────────

def apply_profile(tmp_path: Path, apply_value) -> Path:
    path = make_profile(tmp_path, [{"id": "a", "resume": dict(FAKE_RESUME)}])
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["apply"] = apply_value
    return write_yaml(path, data)


def test_apply_defaults_when_the_section_is_missing_or_partial(tmp_path):
    assert profile.load_profile(make_profile(tmp_path, [{"id": "a", "resume": dict(FAKE_RESUME)}]))["apply"] == {
        "threshold": 75, "daily_cap": 10, "company_daily_cap": 1}
    for value in (None, {}, {"threshold": None}):
        assert profile.load_profile(apply_profile(tmp_path, value))["apply"] == {"threshold": 75, "daily_cap": 10, "company_daily_cap": 1}
    assert profile.load_profile(apply_profile(tmp_path, {"threshold": 60}))["apply"] == {"threshold": 60, "daily_cap": 10, "company_daily_cap": 1}
    assert profile.load_profile(apply_profile(tmp_path, {"daily_cap": 3}))["apply"] == {"threshold": 75, "daily_cap": 3, "company_daily_cap": 1}


def test_apply_accepts_the_limits(tmp_path):
    assert profile.load_profile(apply_profile(tmp_path, {"threshold": 0, "daily_cap": 1}))["apply"] == {
        "threshold": 0, "daily_cap": 1, "company_daily_cap": 1}
    assert profile.load_profile(apply_profile(tmp_path, {"threshold": 100}))["apply"]["threshold"] == 100


def test_example_profile_apply_section_matches_the_defaults():
    assert profile.load_profile(EXAMPLE)["apply"] == {"threshold": 75, "daily_cap": 10, "company_daily_cap": 1}


@pytest.mark.parametrize("threshold", [101, -1, 75.5, "75", True, [75]])
def test_error_apply_threshold_out_of_range_or_not_an_integer(tmp_path, threshold):
    with pytest.raises(RuntimeError, match=r"'apply\.threshold'.*entero entre 0 y 100"):
        profile.load_profile(apply_profile(tmp_path, {"threshold": threshold}))


@pytest.mark.parametrize("daily_cap", [0, -3, 2.5, "10", False])
def test_error_apply_daily_cap_below_one_or_not_an_integer(tmp_path, daily_cap):
    with pytest.raises(RuntimeError, match=r"'apply\.daily_cap'.*entero de 1 o más"):
        profile.load_profile(apply_profile(tmp_path, {"daily_cap": daily_cap}))


def test_error_apply_section_is_not_a_map(tmp_path):
    with pytest.raises(RuntimeError, match=r"'apply'.*mapa"):
        profile.load_profile(apply_profile(tmp_path, [75, 10]))


# ── sección run ──────────────────────────────────────────────────────────────

RUN_DEFAULTS = {"pages": 2, "max_details": 20, "score_limit": 20, "max_cost": 0.5, "max_cost_month": 2.0}


def run_profile(tmp_path: Path, run_value) -> Path:
    path = make_profile(tmp_path, [{"id": "a", "resume": dict(FAKE_RESUME)}])
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["run"] = run_value
    return write_yaml(path, data)


def test_run_defaults_when_the_section_is_missing_or_partial(tmp_path):
    assert profile.load_profile(make_profile(tmp_path, [{"id": "a", "resume": dict(FAKE_RESUME)}]))["run"] == RUN_DEFAULTS
    for value in (None, {}, {"pages": None}):
        assert profile.load_profile(run_profile(tmp_path, value))["run"] == RUN_DEFAULTS
    assert profile.load_profile(run_profile(tmp_path, {"pages": 5}))["run"] == {**RUN_DEFAULTS, "pages": 5}
    assert profile.load_profile(run_profile(tmp_path, {"max_cost": 1}))["run"] == {**RUN_DEFAULTS, "max_cost": 1.0}


def test_run_accepts_the_limits_and_returns_the_cost_as_a_float(tmp_path):
    loaded = profile.load_profile(run_profile(tmp_path, {"pages": 1, "max_details": 0, "score_limit": 0, "max_cost": 1,
                                                         "max_cost_month": 5}))
    assert loaded["run"] == {"pages": 1, "max_details": 0, "score_limit": 0, "max_cost": 1.0, "max_cost_month": 5.0}
    assert type(loaded["run"]["max_cost"]) is float and type(loaded["run"]["max_cost_month"]) is float
    assert profile.load_profile(run_profile(tmp_path, {"max_cost": 0.01}))["run"]["max_cost"] == 0.01
    assert profile.load_profile(run_profile(tmp_path, {"max_cost_month": 0.01}))["run"]["max_cost_month"] == 0.01


def test_example_profile_run_section_matches_the_defaults():
    assert profile.load_profile(EXAMPLE)["run"] == RUN_DEFAULTS == profile.DEFAULT_RUN


@pytest.mark.parametrize("key, minimum, bad", [
    ("pages", 1, [0, -1, 2.5, "2", True, [2]]),
    ("max_details", 0, [-1, 1.5, "20", False]),
    ("score_limit", 0, [-1, 0.5, "20", True]),
])
def test_error_run_counts_below_the_minimum_or_not_an_integer(tmp_path, key, minimum, bad):
    for value in bad:
        with pytest.raises(RuntimeError, match=rf"'run\.{key}'.*entero de {minimum} o más"):
            profile.load_profile(run_profile(tmp_path, {key: value}))


@pytest.mark.parametrize("key", ["max_cost", "max_cost_month"])
@pytest.mark.parametrize("cost", [0, -0.5, "0.5", True, False, [0.5], float("nan")])
def test_error_run_costs_not_a_positive_number(tmp_path, key, cost):
    with pytest.raises(RuntimeError, match=rf"'run\.{key}'.*número mayor que 0"):
        profile.load_profile(run_profile(tmp_path, {key: cost}))


def test_error_run_section_is_not_a_map(tmp_path):
    with pytest.raises(RuntimeError, match=r"'run'.*mapa"):
        profile.load_profile(run_profile(tmp_path, [2, 20]))


def test_invalid_yaml_error_does_not_quote_file_content(tmp_path):
    path = write_yaml(tmp_path / "profile.yaml", {"variants": [{"id": "a", "resume": "cv.yaml"}]})
    (tmp_path / "cv.yaml").write_text("experience_details:\n  - position: [SECRETO-123\n    company: x\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="no es un YAML válido") as err:
        profile.load_profile(path)
    assert "SECRETO" not in str(err.value)


# ── resume_for_llm ───────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def example_text() -> str:
    return profile.resume_for_llm(profile.load_profile(EXAMPLE)["variants"][0]["resume"])


def test_resume_for_llm_includes_allowed_sections(example_text):
    for expected in ("Experiencia:", "Conocimientos técnicos:", "Proyectos:", "Estudios:", "Certificaciones:", "Idiomas:", "Intereses:", "Modalidad:",
                     "Senior Software Engineer", "TechSolutions", "Led a team of developers", "Software architecture",
                     "E-Commerce Platform", "- Python", "- AWS (EC2, S3, Lambda)", "- Terraform", "University of Rome", "Computer Engineering",
                     "AWS Certified DevOps Engineer", "Italian", "Native", "Cloud Computing", "Trabajo remoto: Yes"):
        assert expected in example_text, expected


@pytest.mark.parametrize("forbidden", [
    "Bianchi", "example.com", "3345678901", "1988", "Via Nazionale", "Male", "90000",
    "Giovanni", "giovanni", "+39", "Rome, Italy",  # nombre, prefijo y ubicación (personal_information / location)
    "github.com", "https://",                      # projects.link trae el usuario de GitHub
    "110/110", "30/30", "Computer Networks",      # nota final y exámenes
    "He/Him", "White", "sponsorship", "relocation", "drug",  # self_identification, legal_authorization, otras preferencias
])
def test_resume_for_llm_excludes_personal_data(example_text, forbidden):
    assert forbidden not in example_text


def test_resume_for_llm_skips_empty_sections():
    text = profile.resume_for_llm({"projects": [{"name": "P", "description": "D"}], "experience_details": [],
                                   "interests": [], "personal_information": {"name": "Ana"}})
    assert text == "Proyectos:\n- P: D"


def test_resume_for_llm_handles_yaml_booleans_and_empty_resume():
    assert profile.resume_for_llm({}) == ""
    text = profile.resume_for_llm({"work_preferences": {"remote_work": True, "in_person_work": False}})
    assert text == "Modalidad:\n- Trabajo remoto: Sí\n- Trabajo presencial: No"


def test_resume_for_llm_puts_the_technical_skills_after_the_experience_and_in_order(example_text):
    assert example_text.index("Experiencia:") < example_text.index("Conocimientos técnicos:") < example_text.index("Proyectos:")
    block = example_text.split("Conocimientos técnicos:\n")[1].split("\n\n")[0]
    assert block.splitlines() == ["- Python", "- Java", "- AWS (EC2, S3, Lambda)", "- Kubernetes y Docker", "- PostgreSQL", "- Terraform"]


@pytest.mark.parametrize("value, expected", [
    (["Python", " Docker ", "SQL"], ["Python", "Docker", "SQL"]),
    ([{"Python": "avanzado"}, {"Docker": 3}], ["Python: avanzado", "Docker: 3"]),
    ([{"name": "Python", "level": "avanzado"}, {"skill": "SQL", "level": "básico"}, {"name": "Git"}],
     ["Python: avanzado", "SQL: básico", "Git"]),
    ({"Lenguajes": ["Python", "Java"], "Nube": ["AWS"]}, ["Python", "Java", "AWS"]),
    ({"Python": "avanzado", "Java": "medio"}, ["Python: avanzado", "Java: medio"]),
    (["Python", "", "   ", None, 5, True, [], {}, {"x": None}, {"name": ""}, ["Go", 7]], ["Python", "Go"]),
    ("Python", ["Python"]),
    (None, []), (7, []), ([], []),
])
def test_technical_skills_accepts_the_three_formats_and_ignores_what_is_not_text(value, expected):
    assert profile.technical_skills({"technical_skills": value}) == expected
    assert profile.technical_skills({}) == []


def test_resume_for_llm_without_technical_skills_has_no_such_section_and_with_them_alone_has_only_it():
    assert "Conocimientos técnicos" not in profile.resume_for_llm({"projects": [{"name": "P", "description": "D"}]})
    assert profile.resume_for_llm({"technical_skills": {"Nube": ["AWS"]}}) == "Conocimientos técnicos:\n- AWS"


# ── profile check ────────────────────────────────────────────────────────────

def test_cli_profile_check_prints_one_line_per_variant(tmp_path, capsys):
    path = make_profile(tmp_path, [
        {"id": "a", "resume": dict(FAKE_RESUME)},
        {"id": "b", "resume": {"projects": [{"name": "P1"}, {"name": "P2"}]}},
    ])
    assert cli.main(["profile", "check", "--profile", str(path)]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 2
    assert re.fullmatch(r"a · experiencias: 1 · proyectos: 0 · caracteres para el LLM: \d+ · pdf: OK \(1 KB\)", lines[0])
    assert re.fullmatch(r"b · experiencias: 0 · proyectos: 2 · caracteres para el LLM: \d+ · pdf: OK \(1 KB\)", lines[1])


def test_cli_profile_check_example_prints_no_resume_data_and_no_pdf_path(capsys):
    assert cli.main(["profile", "check", "--profile", str(EXAMPLE)]) == 0
    out = capsys.readouterr().out
    assert re.fullmatch(r"backend · experiencias: 3 · proyectos: 2 · caracteres para el LLM: \d+ · pdf: OK \(1 KB\)\n", out)
    assert "cv.example" not in out and str(ROOT) not in out


def test_cli_profile_check_reports_pdf_size_rounded_up(tmp_path, capsys):
    path = pdf_profile(tmp_path, "cv.pdf", PDF_BYTES + b"0" * 1100)  # 1.1 KB y pico
    assert cli.main(["profile", "check", "--profile", str(path)]) == 0
    assert capsys.readouterr().out.strip().endswith("· pdf: OK (2 KB)")


def test_cli_profile_check_shows_pdf_missing_without_failing(tmp_path, capsys):
    path = make_profile(tmp_path, [{"id": "a", "resume": dict(FAKE_RESUME), "pdf": None},
                                   {"id": "b", "resume": dict(FAKE_RESUME)}])
    assert cli.main(["profile", "check", "--profile", str(path)]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert re.fullmatch(r"a · experiencias: 1 · proyectos: 0 · caracteres para el LLM: \d+ · pdf: falta", lines[0])
    assert re.fullmatch(r"b · experiencias: 1 · proyectos: 0 · caracteres para el LLM: \d+ · pdf: OK \(1 KB\)", lines[1])


def test_cli_profile_check_pdf_error_exits_1_without_output(tmp_path, capsys):
    path = pdf_profile(tmp_path, "cv.pdf", b"no es un pdf")
    assert cli.main(["profile", "check", "--profile", str(path)]) == 1
    captured = capsys.readouterr()
    assert "variante 'a'" in captured.err and "%PDF-" in captured.err and captured.out == ""


def test_cli_profile_check_error_exits_1(tmp_path, capsys):
    assert cli.main(["profile", "check", "--profile", str(tmp_path / "profile.yaml")]) == 1
    captured = capsys.readouterr()
    assert "No existe el perfil" in captured.err and captured.out == ""


# ── banco de verdades (truths) ───────────────────────────────────────────────

def truths_profile(tmp_path: Path, content: str | None, **variant) -> Path:
    if content is not None:
        (tmp_path / "verdades.yaml").write_text(content, encoding="utf-8")
    return make_profile(tmp_path, [{"id": "a", "resume": dict(FAKE_RESUME), "truths": "verdades.yaml", **variant}])


def test_a_variant_without_truths_loads_as_always_and_has_no_truths_key(tmp_path):
    loaded = profile.load_profile(make_profile(tmp_path, [{"id": "a", "resume": dict(FAKE_RESUME)}]))
    assert "truths" not in loaded["variants"][0]


def test_truths_are_loaded_as_a_list_of_texts_with_the_path_relative_to_the_profile(tmp_path):
    path = truths_profile(tmp_path, "- Usé Node.js en la tecnicatura\n-   Armé una API REST  \n")
    assert profile.load_profile(path)["variants"][0]["truths"] == ["Usé Node.js en la tecnicatura", "Armé una API REST"]


def test_the_example_truths_file_is_valid_and_fake():
    items = yaml.safe_load((ROOT / "examples" / "truths.example.yaml").read_text(encoding="utf-8"))
    assert isinstance(items, list) and items and all(isinstance(item, str) and item.strip() for item in items)


@pytest.mark.parametrize("content, message", [
    (None, "no existe"),
    ("clave: valor\n", "tiene que ser una lista de textos"),
    ("solo un texto\n", "tiene que ser una lista de textos"),
    ("- uno\n- 5\n", "un item que no es texto \\(el 2\\)"),
    ("- uno\n- [a, b]\n", "un item que no es texto \\(el 2\\)"),
    ("- uno\n- null\n", "un item que no es texto \\(el 2\\)"),
    ("- uno\n- true\n", "un item que no es texto \\(el 2\\)"),
    ("- uno\n- '   '\n- tres\n", "un item vacío \\(el 2\\)"),
    ("- uno: [sin cerrar\n", "no es un YAML válido"),
])
def test_bad_truths_give_a_clear_error_that_never_quotes_the_content(tmp_path, content, message):
    with pytest.raises(RuntimeError, match=message) as err:
        profile.load_profile(truths_profile(tmp_path, content))
    assert "banco de verdades de la variante 'a'" in str(err.value) and "uno" not in str(err.value)
