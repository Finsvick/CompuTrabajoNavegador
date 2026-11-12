"""Tests de ui_logic.py (la lógica de la interfaz de escritorio, sin Tk). Datos falsos; sin red ni ventana."""
import json
import os
import queue
import subprocess
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from compuapply import cli, db, profile, score, ui_logic

ROOT = Path(__file__).parent.parent
KEY = "sk-test-FALSA-123"
FAKE_RESUME = {"personal_information": {"name": "Ana", "email": "ana@falso.test"},
               "experience_details": [{"position": "Dev", "company": "Acme"}]}
PDF_BYTES = b"%PDF-1.4\n%%EOF\n"


@pytest.fixture(autouse=True)
def no_model_in_the_environment(monkeypatch):
    monkeypatch.delenv("OPENAI_MODEL", raising=False)


def full_data() -> dict:
    return {
        "variants": [
            {"id": "ventas", "resume": "a.yaml", "keywords": ["ventas", "atención al cliente"], "pdf": "a.pdf"},
            {"id": "datos", "resume": "b.yaml", "keywords": ["datos"]},
        ],
        "exclude_companies": ["Empresa Falsa SA", "Otra SRL"],
        "exclude_title_words": ["senior"],
        "apply": {"threshold": 80, "daily_cap": 5},
        "run": {"pages": 3, "max_details": 10, "score_limit": 15, "max_cost": 0.25, "max_cost_month": 3.5},
    }


def write_profile(tmp_path: Path, data: dict | None = None) -> Path:
    """profile.yaml con los CV `a.yaml` y `b.yaml` y el PDF `a.pdf` a su lado."""
    for name in ("a", "b"):
        (tmp_path / f"{name}.yaml").write_text(yaml.safe_dump(FAKE_RESUME), encoding="utf-8")
    (tmp_path / "a.pdf").write_bytes(PDF_BYTES)
    path = tmp_path / "profile.yaml"
    path.write_text(yaml.safe_dump(full_data() if data is None else data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


# ── status ───────────────────────────────────────────────────────────────────

def test_status_with_key_and_model_never_returns_the_key(tmp_path):
    env = tmp_path / ".env"
    env.write_text(f"OTRA=1\nOPENAI_API_KEY={KEY}\nOPENAI_MODEL=gpt-5-nano\n", encoding="utf-8")
    result = ui_logic.status(env, write_profile(tmp_path))
    assert (result["key_ok"], result["model"]) == (True, "gpt-5-nano")
    assert KEY not in str(result)


@pytest.mark.parametrize("content", [None, "", "OTRA=1\n", "OPENAI_API_KEY=\n", "OPENAI_API_KEY=''\n",
                                     f"# OPENAI_API_KEY={KEY}\n"])
def test_status_without_a_usable_key(tmp_path, content):
    env = tmp_path / ".env"
    if content is not None:
        env.write_text(content, encoding="utf-8")
    result = ui_logic.status(env, write_profile(tmp_path))
    assert result["key_ok"] is False and result["model"] == score.DEFAULT_MODEL and KEY not in str(result)


def test_status_model_from_the_environment_wins_like_in_setup(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("OPENAI_MODEL=gpt-4o-mini\n", encoding="utf-8")
    assert ui_logic.status(env, tmp_path / "profile.yaml")["model"] == "gpt-4o-mini"
    monkeypatch.setenv("OPENAI_MODEL", "gpt-5-nano")
    assert ui_logic.status(env, tmp_path / "profile.yaml")["model"] == "gpt-5-nano"


def test_status_unreadable_env_counts_as_no_key(tmp_path):
    env = tmp_path / ".env"
    env.write_bytes(b"\xff\xfe\x00 no es UTF-8 \x80")
    assert ui_logic.status(env, tmp_path / "profile.yaml")["key_ok"] is False


def test_status_valid_profile_lists_variants_and_their_pdf(tmp_path):
    result = ui_logic.status(tmp_path / ".env", write_profile(tmp_path))
    assert result["profile_ok"] is True and result["profile_msg"] == "2 variantes"
    assert result["variants"] == [{"id": "ventas", "pdf_ok": True}, {"id": "datos", "pdf_ok": False}]


def test_status_one_variant_is_singular(tmp_path):
    data = {"variants": [{"id": "a", "resume": "a.yaml"}]}
    result = ui_logic.status(tmp_path / ".env", write_profile(tmp_path, data))
    assert result["profile_msg"] == "1 variante" and result["variants"] == [{"id": "a", "pdf_ok": False}]


def test_status_invalid_profile_gives_the_error_and_no_variants(tmp_path):
    missing = ui_logic.status(tmp_path / ".env", tmp_path / "profile.yaml")
    assert missing["profile_ok"] is False and "No existe el perfil" in missing["profile_msg"] and missing["variants"] == []
    data = full_data()
    data["run"]["pages"] = 0
    bad = ui_logic.status(tmp_path / ".env", write_profile(tmp_path, data))
    assert bad["profile_ok"] is False and "run.pages" in bad["profile_msg"] and bad["variants"] == []


def test_status_gives_the_monthly_cap_of_the_profile_or_the_default(tmp_path):
    assert ui_logic.status(tmp_path / ".env", write_profile(tmp_path))["month_cap"] == 3.5
    assert ui_logic.status(tmp_path / ".env", tmp_path / "no-existe.yaml")["month_cap"] == 2.0  # perfil que no carga: el de por defecto
    data = full_data()
    data["run"]["max_cost_month"] = 0
    assert ui_logic.status(tmp_path / ".env", write_profile(tmp_path, data))["month_cap"] == 2.0


def test_status_gives_the_score_limit_of_the_profile_or_the_default(tmp_path):
    assert ui_logic.status(tmp_path / ".env", write_profile(tmp_path))["score_limit"] == 15
    assert ui_logic.status(tmp_path / ".env", tmp_path / "no-existe.yaml")["score_limit"] == 20


def test_schedule_blocker_says_why_the_run_cannot_be_scheduled(tmp_path):
    ready = {"key_ok": True, "profile_ok": True, "profile_msg": "2 variantes", "score_limit": 20}
    assert ui_logic.schedule_blocker(ready) is None
    assert "clave de OpenAI" in ui_logic.schedule_blocker({**ready, "key_ok": False})
    reason = ui_logic.schedule_blocker({**ready, "profile_ok": False, "profile_msg": "'run.pages' en profile.yaml está mal"})
    assert "perfil" in reason and "'run.pages' en profile.yaml está mal" in reason
    both = ui_logic.schedule_blocker({**ready, "key_ok": False, "profile_ok": False, "profile_msg": "x"})
    assert "perfil" in both  # el perfil hace falta siempre; la clave depende de las ofertas a puntuar
    real = ui_logic.status(tmp_path / ".env", write_profile(tmp_path))
    assert "clave" in ui_logic.schedule_blocker(real)  # sin .env y con 15 ofertas a puntuar: falta la clave
    (tmp_path / ".env").write_text("OPENAI_API_KEY=sk-test-FALSA-123\n", encoding="utf-8")
    assert ui_logic.schedule_blocker(ui_logic.status(tmp_path / ".env", tmp_path / "profile.yaml")) is None


def test_schedule_blocker_does_not_ask_for_the_key_when_the_run_scores_nothing(tmp_path):
    no_key = {"key_ok": False, "profile_ok": True, "profile_msg": "2 variantes"}
    assert ui_logic.schedule_blocker({**no_key, "score_limit": 0}) is None
    reason = ui_logic.schedule_blocker({**no_key, "score_limit": 1})
    assert "Falta la clave de OpenAI" in reason and "«Ofertas a puntuar» en 0" in reason
    zero = full_data()
    zero["run"]["score_limit"] = 0
    assert ui_logic.schedule_blocker(ui_logic.status(tmp_path / ".env", write_profile(tmp_path, zero))) is None
    assert "perfil" in ui_logic.schedule_blocker({**no_key, "score_limit": 0, "profile_ok": False, "profile_msg": "x"})


# ── prueba de puntaje ────────────────────────────────────────────────────────

def probe_response(best="ventas", points=82):
    data = {"best_variant": best, "scores": [{"variant_id": "ventas", "score": points if best == "ventas" else 20},
                                            {"variant_id": "datos", "score": points if best == "datos" else 20}],
            "reasons": ["Encaja"], "missing": []}
    return SimpleNamespace(output_text=json.dumps(data), output=[], status="completed",
                           usage=SimpleNamespace(input_tokens=1000, input_tokens_details=SimpleNamespace(cached_tokens=400),
                                                 output_tokens=200))


def narrow(item, kwargs):
    """Cada pedido lleva un solo CV: una respuesta armada con las dos variantes se reduce a las pedidas."""
    try:
        data = json.loads(item.output_text)
        ids = kwargs["text"]["format"]["schema"]["properties"]["scores"]["items"]["properties"]["variant_id"]["enum"]
        scores = data["scores"]
        keep = [s for s in scores if s["variant_id"] in ids]
    except (AttributeError, TypeError, ValueError, KeyError):
        return item
    if not keep or len(keep) == len(scores) or data.get("best_variant") != max(scores, key=lambda s: s["score"])["variant_id"]:
        return item
    return SimpleNamespace(**{**vars(item), "output_text": json.dumps({**data, "scores": keep, "best_variant": keep[0]["variant_id"]})})


class ProbeFactory:
    """Cliente de OpenAI falso: anota la clave con la que se armó y los pedidos que recibió."""

    def __init__(self, response=None):
        self.api_keys, self.calls, self._response = [], [], response or probe_response()

    def __call__(self, api_key):
        self.api_keys.append(api_key)
        return SimpleNamespace(responses=SimpleNamespace(create=lambda **kw: self.calls.append(kw) or narrow(self._response, kw)))


def test_probe_line_and_summary():
    info = {"score": 82, "best_variant": "ventas", "input_tokens": 1000, "output_tokens": 200, "cost_usd": 0.00031}
    assert ui_logic.probe_line("gpt-6-luna", info) == (
        "Prueba OK con gpt-6-luna: puntaje 82 para ventas · tokens: entrada 1000 / salida 200 · costo: US$ 0,0003")
    assert ui_logic.probe_summary(info) == "puntaje 82, US$ 0,0003"
    each = {**info, "all_scores": {"ventas": 82, "datos": 30}}
    assert ui_logic.probe_line("gpt-6-luna", each) == (
        "Prueba OK con gpt-6-luna: puntaje 82 para ventas · puntajes: ventas 82 / datos 30 · tokens: entrada 1000 / salida 200 "
        "· costo: US$ 0,0003")


def test_run_probe_uses_the_key_and_model_of_the_env_file_and_the_profile(tmp_path):
    env = tmp_path / ".env"
    env.write_text(f"OPENAI_API_KEY={KEY}\nOPENAI_MODEL=gpt-5-nano\n", encoding="utf-8")
    factory = ProbeFactory()
    info = ui_logic.run_probe(env, write_profile(tmp_path), factory)
    assert factory.api_keys == [KEY] and factory.calls[0]["model"] == "gpt-5-nano"
    assert (info["score"], info["best_variant"]) == (82, "ventas") and KEY not in str(info)
    assert info["all_scores"] == {"ventas": 82, "datos": 20} and len(factory.calls) == 2  # un pedido por variante
    assert not (tmp_path / "data").exists()  # no escribe nada


def test_run_probe_without_a_key_does_not_build_a_client(tmp_path):
    factory = ProbeFactory()
    with pytest.raises(RuntimeError, match="Falta la clave de OpenAI"):
        ui_logic.run_probe(tmp_path / ".env", write_profile(tmp_path), factory)
    assert factory.api_keys == []


def test_run_probe_with_a_bad_profile_is_a_clear_error(tmp_path):
    (tmp_path / ".env").write_text(f"OPENAI_API_KEY={KEY}\n", encoding="utf-8")
    factory = ProbeFactory()
    with pytest.raises(RuntimeError):
        ui_logic.run_probe(tmp_path / ".env", tmp_path / "no-existe.yaml", factory)
    assert factory.calls == []


def test_status_bad_pdf_is_a_profile_error(tmp_path):
    data = full_data()
    data["variants"][0]["pdf"] = "no-existe.pdf"
    result = ui_logic.status(tmp_path / ".env", write_profile(tmp_path, data))
    assert result["profile_ok"] is False and "PDF de la variante 'ventas'" in result["profile_msg"]


# ── formulario del perfil ────────────────────────────────────────────────────

def test_profile_to_form_gives_every_field_as_text(tmp_path):
    assert ui_logic.profile_to_form(write_profile(tmp_path)) == {
        "variants": [
            {"id": "ventas", "resume": "a.yaml", "keywords": "ventas, atención al cliente", "pdf": "a.pdf"},
            {"id": "datos", "resume": "b.yaml", "keywords": "datos", "pdf": ""},
        ],
        "exclude_companies": "Empresa Falsa SA\nOtra SRL", "exclude_title_words": "senior", "zones": "",
        "apply": {"threshold": "80", "daily_cap": "5", "company_daily_cap": "1"},
        "run": {"pages": "3", "max_details": "10", "score_limit": "15", "max_cost": "0.25", "max_cost_month": "3.5"},
        "answers": dict.fromkeys(("salary", "availability", "modality", "start", "location", "mobility", "salary_part_time", "salary_daily"), ""), "review_cv": "",
    }


def test_form_round_trip_gives_back_the_same_profile(tmp_path):
    path = write_profile(tmp_path)
    expected = yaml.safe_load(path.read_text(encoding="utf-8"))
    expected["apply"]["company_daily_cap"] = 1  # el formulario siempre lo trae, con su valor por defecto
    assert ui_logic.form_to_profile(ui_logic.profile_to_form(path)) == expected


def test_form_of_a_minimal_profile_fills_the_defaults(tmp_path):
    path = write_profile(tmp_path, {"variants": [{"id": "a", "resume": "a.yaml"}]})
    assert ui_logic.form_to_profile(ui_logic.profile_to_form(path)) == {
        "variants": [{"id": "a", "resume": "a.yaml", "keywords": []}],
        "exclude_companies": [], "exclude_title_words": [],
        "apply": {"threshold": 75, "daily_cap": 10, "company_daily_cap": 1},
        "run": {"pages": 2, "max_details": 20, "score_limit": 20, "max_cost": 0.5, "max_cost_month": 2.0},
    }


def test_form_without_a_profile_is_the_empty_form(tmp_path):
    form = ui_logic.profile_to_form(tmp_path / "no-existe.yaml")
    assert form == ui_logic.empty_form()
    assert form["variants"] == [{"id": "", "resume": "", "keywords": "", "pdf": ""}]
    assert (form["apply"], form["run"]) == (
        {"threshold": "75", "daily_cap": "10", "company_daily_cap": "1"},
        {"pages": "2", "max_details": "20", "score_limit": "20", "max_cost": "0.5", "max_cost_month": "2.0"})


def test_form_to_profile_trims_splits_and_drops_blanks():
    form = ui_logic.empty_form()
    form["variants"] = [{"id": " ventas ", "resume": " cv.yaml ", "keywords": " uno, dos ,, tres ,", "pdf": "   "}]
    form["exclude_companies"] = "  Acme \n\n Otra  \n"
    form["exclude_title_words"] = "\n"
    data = ui_logic.form_to_profile(form)
    assert data["variants"] == [{"id": "ventas", "resume": "cv.yaml", "keywords": ["uno", "dos", "tres"]}]  # sin `pdf`
    assert (data["exclude_companies"], data["exclude_title_words"]) == (["Acme", "Otra"], [])


def test_form_to_profile_accepts_a_decimal_comma_for_the_costs():
    form = ui_logic.empty_form()
    form["run"]["max_cost"], form["run"]["max_cost_month"] = " 0,30 ", "1,5"
    run = ui_logic.form_to_profile(form)["run"]
    assert (run["max_cost"], run["max_cost_month"]) == (0.3, 1.5)


@pytest.mark.parametrize("section, key, value, message", [
    ("apply", "threshold", "abc", r"«Umbral de puntaje» tiene que ser un número entero \(hay «abc»\)"),
    ("apply", "threshold", "7.5", "«Umbral de puntaje» tiene que ser un número entero"),
    ("apply", "daily_cap", "", r"«Tope diario» tiene que ser un número entero \(está vacío\)"),
    ("run", "pages", "dos", "«Páginas por keyword» tiene que ser un número entero"),
    ("run", "max_details", "-", "«Detalles a bajar»"),
    ("run", "score_limit", "x", "«Ofertas a puntuar»"),
    ("run", "max_cost", "mucho", r"«Gasto máximo \(USD\)» tiene que ser un número, por ejemplo 0.50 \(hay «mucho»\)"),
    ("run", "max_cost_month", "x", r"«Tope de gasto mensual \(USD\)» tiene que ser un número"),
])
def test_form_to_profile_names_the_field_with_a_bad_number(section, key, value, message):
    form = ui_logic.empty_form()
    form[section][key] = value
    with pytest.raises(RuntimeError, match=message):
        ui_logic.form_to_profile(form)


def test_profile_to_form_errors_do_not_quote_the_file(tmp_path):
    path = tmp_path / "profile.yaml"
    path.write_text("variants: [SECRETO-123\n  x: y\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="no es un YAML válido") as err:
        ui_logic.profile_to_form(path)
    assert "SECRETO" not in str(err.value)
    path.write_text("variants: hola\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="formato esperado"):
        ui_logic.profile_to_form(path)


# ── save_profile ─────────────────────────────────────────────────────────────

def test_save_profile_writes_a_profile_that_loads(tmp_path):
    path = write_profile(tmp_path, {"variants": [{"id": "viejo", "resume": "a.yaml"}]})
    data = full_data()
    ui_logic.save_profile(data, path)
    assert yaml.safe_load(path.read_text(encoding="utf-8")) == data
    loaded = profile.load_profile(path)
    assert [v["id"] for v in loaded["variants"]] == ["ventas", "datos"] and loaded["run"]["max_cost"] == 0.25
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a.pdf", "a.yaml", "b.yaml", "profile.yaml"]  # sin temporales


def test_save_profile_creates_the_file_when_there_was_none(tmp_path):
    path = write_profile(tmp_path)
    path.unlink()
    ui_logic.save_profile(full_data(), path)
    assert profile.load_profile(path)["apply"] == {"threshold": 80, "daily_cap": 5, "company_daily_cap": 1}


@pytest.mark.parametrize("change, message", [
    (lambda d: d["apply"].update(threshold=150), r"'apply\.threshold'"),
    (lambda d: d["run"].update(pages=0), r"'run\.pages'"),
    (lambda d: d["run"].update(max_cost=0), r"'run\.max_cost'"),
    (lambda d: d["variants"][0].update(resume="no-existe.yaml"), "No existe el CV de la variante 'ventas'"),
    (lambda d: d["variants"][0].update(pdf="no-existe.pdf"), "PDF de la variante 'ventas'"),
    (lambda d: d["variants"].clear(), "'variants'"),
    (lambda d: d["variants"][1].update(id="ventas"), "repetido"),
])
def test_save_profile_with_a_bad_value_changes_nothing(tmp_path, change, message):
    path = write_profile(tmp_path)
    before = path.read_bytes()
    data = full_data()
    change(data)
    with pytest.raises(RuntimeError, match=message) as err:
        ui_logic.save_profile(data, path)
    assert path.read_bytes() == before and not (tmp_path / "profile.yaml.tmp").exists()
    assert "profile.yaml.tmp" not in str(err.value)  # el error nombra el archivo real, no el temporal


def test_save_profile_does_not_touch_the_file_if_replacing_fails(tmp_path, monkeypatch):
    path = write_profile(tmp_path)
    before = path.read_bytes()

    def broken_replace(src, dst):
        raise OSError("disco lleno")

    monkeypatch.setattr(ui_logic.os, "replace", broken_replace)
    with pytest.raises(OSError):
        ui_logic.save_profile(full_data(), path)
    assert path.read_bytes() == before and not (tmp_path / "profile.yaml.tmp").exists()


# ── set_threshold ────────────────────────────────────────────────────────────

def test_set_threshold_changes_only_apply_threshold(tmp_path):
    path = write_profile(tmp_path)
    original = yaml.safe_load(path.read_text(encoding="utf-8"))
    ui_logic.set_threshold(path, 60, "gpt-4o-mini")
    assert yaml.safe_load(path.read_text(encoding="utf-8")) == {
        **original, "apply": {**original["apply"], "threshold": 60, "calibrated_with": "gpt-4o-mini v5"}}
    assert profile.load_profile(path)["apply"] == {"threshold": 60, "daily_cap": 5, "company_daily_cap": 1, "calibrated_with": "gpt-4o-mini v5"}
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a.pdf", "a.yaml", "b.yaml", "profile.yaml"]  # sin temporales


@pytest.mark.parametrize("apply_value", [None, {}])
def test_set_threshold_creates_the_apply_section_when_there_is_none(tmp_path, apply_value):
    data = {"variants": [{"id": "a", "resume": "a.yaml"}], "apply": apply_value}
    path = write_profile(tmp_path, data)
    ui_logic.set_threshold(path, 90, "gpt-6-luna")
    assert yaml.safe_load(path.read_text(encoding="utf-8"))["apply"] == {"threshold": 90, "calibrated_with": "gpt-6-luna v5"}
    assert profile.load_profile(path)["apply"] == {"threshold": 90, "daily_cap": 10, "company_daily_cap": 1, "calibrated_with": "gpt-6-luna v5"}


@pytest.mark.parametrize("threshold", [0, 100])
def test_set_threshold_accepts_the_limits(tmp_path, threshold):
    path = write_profile(tmp_path)
    ui_logic.set_threshold(path, threshold, "gpt-6-luna")
    assert profile.load_profile(path)["apply"]["threshold"] == threshold


@pytest.mark.parametrize("threshold", [101, -1, 7.5, True, "80"])
def test_set_threshold_out_of_range_or_not_an_integer_changes_nothing(tmp_path, threshold):
    path = write_profile(tmp_path)
    before = path.read_bytes()
    with pytest.raises(RuntimeError, match=r"'apply\.threshold'.*entero entre 0 y 100") as err:
        ui_logic.set_threshold(path, threshold, "gpt-6-luna")
    assert path.read_bytes() == before and not (tmp_path / "profile.yaml.tmp").exists()
    assert "profile.yaml.tmp" not in str(err.value)


def test_set_threshold_without_a_profile_does_not_create_one(tmp_path):
    with pytest.raises(RuntimeError, match="No existe el perfil"):
        ui_logic.set_threshold(tmp_path / "profile.yaml", 80, "gpt-6-luna")
    assert list(tmp_path.iterdir()) == []


# ── results, spend y reports ─────────────────────────────────────────────────

@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / "t.db")
    yield connection
    connection.close()


def add_score(conn, n: int, points: int, *, model="gpt-6-luna", scored_at=None, cost=0.001, variant="backend",
              reasons=("Encaja",), missing=(), status=None, unmet=()) -> str:
    id16 = f"{n:016X}"
    db.upsert_offers(conn, [{"id16": id16, "data_id": f"{n:032X}", "title": f"Oferta {n}", "company": f"Empresa {n}",
                             "location": "Rosario", "url": f"https://ar.computrabajo.com/ofertas-de-trabajo/o-{n}"}], "x")
    db.save_score(conn, id16, model, "v5", {"best_variant": variant, "score": points, "all_scores": {variant: points},
                                            "reasons": list(reasons), "missing": list(missing), "unmet_required": list(unmet)},
                  (1000, 400, 200), cost)
    if scored_at:
        conn.execute("UPDATE scores SET scored_at = ? WHERE id16 = ? AND model = ?", (scored_at, id16, model))
        conn.commit()
    if status:
        db.mark_status(conn, id16, status)
    return id16


def test_results_gives_every_field_of_each_scored_offer(conn):
    add_score(conn, 1, 88, variant="datos", reasons=("Python", "Remoto"), missing=("Inglés",), status="excluded",
              scored_at="2026-11-01T10:00:00+00:00", unmet=("Node.js",))
    assert ui_logic.results(conn, "gpt-6-luna") == [{
        "id16": f"{1:016X}", "score": 88, "title": "Oferta 1", "company": "Empresa 1", "variant": "datos",
        "status": "excluded", "scored_at": "2026-11-01T10:00:00+00:00", "reasons": ["Python", "Remoto"],
        "missing": ["Inglés"], "unmet_required": ["Node.js"], "url": "https://ar.computrabajo.com/ofertas-de-trabajo/o-1"}]


def test_results_only_has_the_scores_of_the_model_and_version_the_plan_uses_and_no_duplicates(conn):
    add_score(conn, 1, 40, model="gpt-4o-mini", variant="otro-modelo")
    add_score(conn, 1, 88, variant="actual")
    add_score(conn, 2, 70)
    conn.execute("UPDATE scores SET prompt_version = 'v1' WHERE id16 = ?", (f"{2:016X}",))
    add_score(conn, 3, 99, status="duplicate")
    rows = ui_logic.results(conn, "gpt-6-luna")
    assert [(r["id16"], r["score"], r["variant"]) for r in rows] == [(f"{1:016X}", 88, "actual")]
    assert [r["score"] for r in ui_logic.results(conn, "gpt-4o-mini")] == [40]


def test_results_are_sorted_from_the_highest_score_and_ties_keep_the_most_recent_first(conn):
    add_score(conn, 1, 60)
    add_score(conn, 2, 90)
    add_score(conn, 3, 75, scored_at="2026-01-01T00:00:00+00:00")
    add_score(conn, 4, 75, scored_at="2026-09-01T00:00:00+00:00")
    add_score(conn, 5, 75, scored_at="2026-05-01T00:00:00+00:00")
    assert [r["score"] for r in ui_logic.results(conn, "gpt-6-luna")] == [90, 75, 75, 75, 60]
    tied = [r["id16"] for r in ui_logic.results(conn, "gpt-6-luna") if r["score"] == 75]
    assert tied == [f"{4:016X}", f"{5:016X}", f"{3:016X}"]


def test_results_filter_by_threshold_includes_the_threshold_itself(conn):
    for n, points in enumerate((100, 80, 75, 74, 10), 1):
        add_score(conn, n, points)
    assert [r["score"] for r in ui_logic.results(conn, "gpt-6-luna", 75)] == [100, 80, 75]
    assert [r["score"] for r in ui_logic.results(conn, "gpt-6-luna", 0)] == [100, 80, 75, 74, 10]
    assert [r["score"] for r in ui_logic.results(conn, "gpt-6-luna", None)] == [100, 80, 75, 74, 10]
    assert ui_logic.results(conn, "gpt-6-luna", 101) == []


def test_results_skips_offers_without_a_score_and_an_empty_base_gives_nothing(conn):
    assert ui_logic.results(conn, "gpt-6-luna") == []
    db.upsert_offers(conn, [{"id16": f"{7:016X}", "data_id": "x", "title": "Sin puntaje", "company": "c", "location": "l",
                             "url": "u"}], "x")
    assert ui_logic.results(conn, "gpt-6-luna") == []


def test_spend_sums_the_month_and_the_total(conn):
    for n, (stamp, cost) in enumerate((
            ("2026-11-01T00:00:00+00:00", 0.01), ("2026-11-30T23:59:59+00:00", 0.02),
            ("2026-10-31T23:59:59+00:00", 0.04), ("2025-11-15T10:00:00+00:00", 0.08)), 1):
        add_score(conn, n, 70, scored_at=stamp, cost=cost)
    november = ui_logic.spend(conn, date(2026, 11, 2))
    assert november["month"] == pytest.approx(0.03) and november["total"] == pytest.approx(0.15)  # noviembre de 2025 no cuenta
    assert ui_logic.spend(conn, date(2026, 10, 15))["month"] == pytest.approx(0.04)
    assert ui_logic.spend(conn, date(2027, 1, 1))["month"] == 0.0


def test_spend_ignores_a_missing_cost_and_an_empty_base_is_zero(conn):
    assert ui_logic.spend(conn, date(2026, 11, 2)) == {"month": 0.0, "total": 0.0}
    add_score(conn, 1, 70, scored_at="2026-11-02T10:00:00+00:00", cost=0.5)
    conn.execute("UPDATE scores SET cost_usd = NULL")
    conn.commit()
    add_score(conn, 2, 70, scored_at="2026-11-02T10:00:00+00:00", cost=0.25)
    result = ui_logic.spend(conn, date(2026, 11, 2))
    assert result == {"month": 0.25, "total": 0.25} and type(result["month"]) is float


def test_reports_are_sorted_from_the_newest_and_only_markdown_files(tmp_path):
    runs = tmp_path / "data" / "runs"
    runs.mkdir(parents=True)
    for name, stamp in (("viejo-run.md", 1_700_000_000), ("nuevo-run.md", 1_700_002_000), ("medio-apply-seco.md", 1_700_001_000)):
        (runs / name).write_text("# informe\n", encoding="utf-8")
        os.utime(runs / name, (stamp, stamp))
    (runs / "notas.txt").write_text("no es un informe", encoding="utf-8")
    (runs / "carpeta.md").mkdir()
    assert [p.name for p in ui_logic.reports(runs)] == ["nuevo-run.md", "medio-apply-seco.md", "viejo-run.md"]
    assert ui_logic.reports(runs)[0] == runs / "nuevo-run.md"  # rutas completas, listas para abrir


def test_reports_with_the_same_file_date_go_by_name_and_a_missing_folder_is_empty(tmp_path):
    runs = tmp_path / "runs"
    runs.mkdir()
    for name in ("20261101-100000-run.md", "20261102-090000-run.md", "20261030-080000-run.md"):
        (runs / name).write_text("# informe\n", encoding="utf-8")
        os.utime(runs / name, (1_700_000_000, 1_700_000_000))
    assert [p.name for p in ui_logic.reports(runs)] == [
        "20261102-090000-run.md", "20261101-100000-run.md", "20261030-080000-run.md"]
    assert ui_logic.reports(tmp_path / "no-existe") == []


# ── acceso directo ───────────────────────────────────────────────────────────

DESKTOP, PROJECT, PYTHONW = Path("C:/Users/Ana/Desktop"), Path("C:/Dev/compuapply"), Path("C:/Python313/pythonw.exe")


def test_shortcut_command_builds_the_powershell_call_without_running_it(monkeypatch):
    monkeypatch.setattr(ui_logic.subprocess, "run", lambda *a, **k: pytest.fail("no se ejecuta"))
    monkeypatch.setattr(ui_logic.subprocess, "Popen", lambda *a, **k: pytest.fail("no se ejecuta"))
    assert ui_logic.shortcut_command(DESKTOP, PROJECT, PYTHONW) == [
        "powershell", "-NoProfile", "-Command",
        f"$s = (New-Object -ComObject WScript.Shell).CreateShortcut('{DESKTOP / 'compuapply.lnk'}'); "
        f"$s.TargetPath = '{PYTHONW}'; $s.Arguments = '-m compuapply.gui'; "
        f"$s.WorkingDirectory = '{PROJECT}'; $s.Save()"]


def test_shortcut_command_doubles_single_quotes_in_the_paths():
    script = ui_logic.shortcut_command(Path("C:/Users/O'Brien/Desktop"), Path("C:/Dev/it's"), PYTHONW)[3]
    assert "O''Brien" in script and "it''s" in script and "O'Brien" not in script.replace("O''Brien", "")
    assert script.count("'") % 2 == 0  # las comillas siguen balanceadas


def test_create_shortcut_runs_the_command_and_says_whether_it_replaced_one(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(ui_logic.subprocess, "run", lambda command, **kwargs: seen.append((command, kwargs)) or SimpleNamespace(
        returncode=0, stderr="", stdout=""))
    desktop, project, pythonw = tmp_path / "Desktop", tmp_path / "proyecto", tmp_path / "pythonw.exe"
    desktop.mkdir()
    assert ui_logic.create_shortcut(desktop, project, pythonw) == "Acceso directo creado en el escritorio."
    (desktop / "compuapply.lnk").write_bytes(b"")  # el que dejó la vez anterior
    assert ui_logic.create_shortcut(desktop, project, pythonw) == "Acceso directo reemplazado en el escritorio."
    assert [command for command, _ in seen] == [ui_logic.shortcut_command(desktop, project, pythonw)] * 2
    assert seen[0][1]["creationflags"] == ui_logic.CREATE_NO_WINDOW and seen[0][1]["capture_output"] is True


def test_create_shortcut_reports_the_powershell_error(tmp_path, monkeypatch):
    monkeypatch.setattr(ui_logic.subprocess, "run", lambda command, **kwargs: SimpleNamespace(
        returncode=1, stderr="\n  Acceso denegado al escritorio\nmás detalle\n", stdout=""))
    with pytest.raises(RuntimeError, match="No se pudo crear el acceso directo: Acceso denegado al escritorio"):
        ui_logic.create_shortcut(tmp_path, tmp_path, tmp_path / "pythonw.exe")


def test_pythonw_exe_is_the_one_next_to_python_and_fails_clearly_without_it(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "executable", str(tmp_path / "python.exe"))
    (tmp_path / "python.exe").write_bytes(b"")
    with pytest.raises(RuntimeError, match="pythonw.exe"):
        ui_logic.pythonw_exe()
    (tmp_path / "pythonw.exe").write_bytes(b"")
    assert ui_logic.pythonw_exe() == tmp_path / "pythonw.exe"


@pytest.mark.skipif(sys.platform != "win32", reason="la carpeta Escritorio por API es de Windows")
def test_desktop_dir_is_an_existing_folder():
    assert ui_logic.desktop_dir().is_dir()


# ── build_command y report_path ──────────────────────────────────────────────

def test_python_exe_is_the_python_next_to_pythonw(tmp_path, monkeypatch):
    (tmp_path / "pythonw.exe").write_bytes(b"")
    monkeypatch.setattr(sys, "executable", str(tmp_path / "pythonw.exe"))
    assert ui_logic.python_exe() == sys.executable  # sin python.exe al lado: se queda con el que hay
    (tmp_path / "python.exe").write_bytes(b"")
    assert ui_logic.python_exe() == str(tmp_path / "python.exe")


def test_build_command_uses_the_run_values_of_the_profile(tmp_path):
    settings = profile.load_profile(write_profile(tmp_path))["run"]
    exe = [ui_logic.python_exe(), "-u", "-m", "compuapply"]
    assert ui_logic.build_command("run", settings) == [*exe, "run"]  # `run` toma los valores del perfil por su cuenta
    assert ui_logic.build_command("score", settings) == [*exe, "score", "--limit", "15", "--max-cost", "0.25"]
    assert ui_logic.build_command("apply", settings) == [*exe, "apply"]
    assert ui_logic.build_command("login", settings) == [*exe, "login"]
    assert ui_logic.build_command("session_check", settings) == [*exe, "session", "check"]
    assert ui_logic.build_command("apply_live", settings) == [*exe, "apply", "--live"]
    with pytest.raises(ValueError, match="desconocida"):
        ui_logic.build_command("otra", settings)


def test_build_command_adds_live_only_for_apply_live():
    for action in ("run", "score", "apply", "login", "session_check"):
        assert "--live" not in ui_logic.build_command(action, {"score_limit": 20, "max_cost": 0.5})


@pytest.mark.parametrize("action", ["run", "score", "apply", "login", "session_check"])
def test_build_command_flags_exist_in_the_cli(action, capsys):
    command = ui_logic.build_command(action, {"score_limit": 20, "max_cost": 0.5})
    assert command[1:4] == ["-u", "-m", "compuapply"]
    with pytest.raises(SystemExit) as stop:
        cli.main([*command[4:], "--help"])  # el parser acepta cada flag que arma la interfaz
    assert stop.value.code == 0
    capsys.readouterr()


@pytest.mark.parametrize("lines, expected", [
    (["Scan: keywords: 2", "Informe: data\\runs\\20261102-140305-run.md"], "data\\runs\\20261102-140305-run.md"),
    (["Informe: data/runs/20261102-140305-apply-seco.md"], "data/runs/20261102-140305-apply-seco.md"),
    (["Informe: data\\runs\\viejo-run.md", "otra línea", "Informe: data\\runs\\nuevo-run.md"], "data\\runs\\nuevo-run.md"),
    (["ver README.md", "Ofertas exportadas: 3 · archivo: data\\review.csv", "Informe: sin ruta"], None),
    ([], None),
])
def test_report_path_finds_the_last_report(lines, expected):
    assert ui_logic.report_path(lines) == (Path(expected) if expected else None)


# ── subprocesos ──────────────────────────────────────────────────────────────

def run_job(code: str, interactive: bool = False, feed: str | None = None):
    """Corre `python -c code` con start_job; devuelve (proceso, líneas hasta el final)."""
    output = queue.Queue()
    proc = ui_logic.start_job([sys.executable, "-u", "-c", code], output, interactive)
    if feed is not None:
        proc.stdin.write(feed)
        proc.stdin.close()
    lines = []
    while (item := output.get(timeout=30)) is not None:
        lines.append(item)
    proc.wait(timeout=30)
    return proc, lines


def test_start_job_sends_stdout_and_stderr_lines_then_the_end_marker():
    proc, lines = run_job("import sys; print('hola ñandú'); print('falla', file=sys.stderr); sys.exit(3)")
    assert lines == ["hola ñandú", "falla"] and proc.returncode == 3  # UTF-8 y sin los saltos de línea


def test_start_job_sets_the_environment_for_a_utf8_unbuffered_cli():
    _, lines = run_job("import os; print(os.environ['PYTHONIOENCODING'], os.environ['PYTHONUNBUFFERED'])")
    assert lines == ["utf-8 1"]


def test_start_job_gives_no_stdin_unless_interactive():
    _, lines = run_job("import sys; print(repr(sys.stdin.read()))")
    assert lines == ["''"]  # fin de entrada inmediato: un input() no se cuelga


def test_start_job_interactive_receives_the_enter_for_login():
    proc, lines = run_job("input(); print('recibido')", interactive=True, feed="\n")
    assert lines == ["recibido"] and proc.returncode == 0


def test_start_job_launches_without_a_console_window(monkeypatch):
    seen = {}
    monkeypatch.setattr(ui_logic.subprocess, "Popen", lambda command, **kwargs: seen.update(kwargs) or SimpleNamespace(stdout=iter(())))
    output = queue.Queue()
    ui_logic.start_job(["x"], output)
    assert output.get(timeout=10) is None
    assert seen["creationflags"] == 0x08000000 == ui_logic.CREATE_NO_WINDOW
    assert (seen["stdout"], seen["stderr"], seen["stdin"]) == (subprocess.PIPE, subprocess.STDOUT, subprocess.DEVNULL)
    assert (seen["text"], seen["encoding"]) == (True, "utf-8")


def test_stop_file_is_created_and_removed(tmp_path):
    stop = tmp_path / "STOP"
    assert ui_logic.clear_stop(stop) is False
    ui_logic.request_stop(stop)
    assert stop.is_file() and stop.read_bytes() == b""
    assert ui_logic.clear_stop(stop) is True and not stop.exists()
    assert ui_logic.clear_stop(stop) is False


def test_kill_tree_asks_taskkill_for_the_whole_tree(monkeypatch):
    seen = []
    monkeypatch.setattr(ui_logic.subprocess, "run", lambda command, **kwargs: seen.append((command, kwargs)))
    ui_logic.kill_tree(1234)
    assert seen[0][0] == ["taskkill", "/PID", "1234", "/T", "/F"]


@pytest.mark.skipif(sys.platform != "win32", reason="taskkill es de Windows")
def test_kill_tree_really_stops_a_running_process():
    output = queue.Queue()
    proc = ui_logic.start_job([sys.executable, "-u", "-c", "import time; print('vivo', flush=True); time.sleep(120)"], output)
    assert output.get(timeout=30) == "vivo"
    ui_logic.kill_tree(proc.pid)
    assert proc.wait(timeout=30) != 0
    assert output.get(timeout=30) is None


# ── Iniciar.bat ──────────────────────────────────────────────────────────────

def test_launcher_changes_to_its_folder_and_starts_the_gui_without_a_console():
    raw = (ROOT / "Iniciar.bat").read_bytes()
    lines = raw.decode("ascii").splitlines()  # sin acentos: cmd los lee según la página de códigos
    assert b"\r\n" in raw and lines[0] == "@echo off"
    assert 'cd /d "%~dp0"' in lines
    start = lines.index('start "" pythonw -m compuapply.gui')
    check = next(i for i, line in enumerate(lines) if line.startswith("where pythonw"))
    assert check < start and any(line.strip() == "pause" for line in lines[check:start])  # sin pythonw: mensaje y pausa


def test_probe_texts_use_the_decimal_comma():
    info = {"score": 70, "best_variant": "a", "input_tokens": 10, "output_tokens": 5, "cost_usd": 0.00041}
    assert "costo: US$ 0,0004" in ui_logic.probe_line("gpt-4o-mini", info)
    assert ui_logic.probe_summary(info) == "puntaje 70, US$ 0,0004"
    assert "." not in ui_logic.probe_summary(info).split(",")[1]


# ── TAREA-023: calibrated_with ───────────────────────────────────────────────

def test_save_profile_keeps_calibrated_with_for_the_same_threshold_and_drops_it_for_another(tmp_path):
    path = write_profile(tmp_path)
    ui_logic.set_threshold(path, 60, "gpt-4o-mini")
    data = full_data()
    data["apply"] = {"threshold": 60, "daily_cap": 7}  # lo que arma el formulario: sin calibrated_with
    ui_logic.save_profile(data, path)
    assert profile.load_profile(path)["apply"] == {"threshold": 60, "daily_cap": 7, "company_daily_cap": 1, "calibrated_with": "gpt-4o-mini v5"}
    data["apply"] = {"threshold": 65, "daily_cap": 7}
    ui_logic.save_profile(data, path)
    assert profile.load_profile(path)["apply"] == {"threshold": 65, "daily_cap": 7, "company_daily_cap": 1}


def test_save_profile_without_calibration_or_with_a_broken_old_file_just_saves(tmp_path):
    path = write_profile(tmp_path)
    ui_logic.save_profile(full_data(), path)
    assert "calibrated_with" not in profile.load_profile(path)["apply"]
    path.write_text("esto: [no cierra", encoding="utf-8")
    ui_logic.save_profile(full_data(), path)
    assert profile.load_profile(path)["apply"] == {"threshold": 80, "daily_cap": 5, "company_daily_cap": 1}


def test_calibrated_with_must_be_text(tmp_path):
    path = write_profile(tmp_path, {"variants": [{"id": "a", "resume": "a.yaml"}], "apply": {"threshold": 70, "calibrated_with": 5}})
    with pytest.raises(RuntimeError, match=r"'apply\.calibrated_with'.*texto"):
        profile.load_profile(path)


def test_status_has_the_threshold_and_its_note(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    path = write_profile(tmp_path)
    st = ui_logic.status(tmp_path / ".env", path)
    assert st["threshold"] == 80 and st["calibrated_with"] is None and st["threshold_note"].startswith("El umbral 80 no salió de la calibración")
    ui_logic.set_threshold(path, 60, "gpt-6-luna")
    st = ui_logic.status(tmp_path / ".env", path)
    assert (st["threshold"], st["calibrated_with"], st["threshold_note"]) == (60, "gpt-6-luna v5", None)
    ui_logic.set_threshold(path, 60, "gpt-4o-mini")
    assert ui_logic.status(tmp_path / ".env", path)["threshold_note"] == (
        "El umbral 60 se calibró con gpt-4o-mini v5; ahora se puntúa con gpt-6-luna v5: calibrá de nuevo antes de postular.")
    assert ui_logic.status(tmp_path / ".env", tmp_path / "no-existe.yaml")["threshold"] is None


def test_results_unmet_required_is_empty_for_scores_without_the_list(conn):
    add_score(conn, 1, 70)
    conn.execute("UPDATE scores SET unmet_required = NULL")
    assert ui_logic.results(conn, "gpt-6-luna")[0]["unmet_required"] == []


def test_save_profile_keeps_the_truths_of_each_variant_because_the_form_does_not_have_them(tmp_path):
    (tmp_path / "verdades.yaml").write_text("- Usé Node.js en la tecnicatura\n", encoding="utf-8")
    path = write_profile(tmp_path, {"variants": [{"id": "a", "resume": "a.yaml", "truths": "verdades.yaml"},
                                                 {"id": "b", "resume": "b.yaml"}]})
    data = full_data()
    data["variants"] = [{"id": "a", "resume": "a.yaml", "keywords": ["x"]}, {"id": "b", "resume": "b.yaml", "keywords": []},
                        {"id": "nueva", "resume": "a.yaml", "keywords": []}]
    ui_logic.save_profile(data, path)
    saved = {v["id"]: v for v in yaml.safe_load(path.read_text(encoding="utf-8"))["variants"]}
    assert saved["a"]["truths"] == "verdades.yaml" and "truths" not in saved["b"] and "truths" not in saved["nueva"]
    assert profile.load_profile(path)["variants"][0]["truths"] == ["Usé Node.js en la tecnicatura"]
    data["variants"][0]["truths"] = "otro.yaml"  # si el dato ya trae uno, no se pisa
    (tmp_path / "otro.yaml").write_text("- Otra verdad\n", encoding="utf-8")
    ui_logic.save_profile(data, path)
    assert profile.load_profile(path)["variants"][0]["truths"] == ["Otra verdad"]
