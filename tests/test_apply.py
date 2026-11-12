"""Tests de apply.py (modo seco) y del comando `apply`. Sin red, sin API y sin tocar Computrabajo."""
import os
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from compuapply import apply, cli, db

ROOT = Path(__file__).parent.parent
EXAMPLE = ROOT / "profile.example.yaml"
TODAY = date(2026, 11, 2)
MODEL = "gpt-6-luna"
UNCALIBRATED = "El umbral 75 no salió de la calibración: etiquetá en Calibrar y usá el umbral sugerido."
ITEM_KEYS = {"id16", "title", "company", "score", "variant", "reason", "url"}


def profile_dict(threshold=75, daily_cap=10, variants=("backend", "data"), company_daily_cap=99) -> dict:
    return {"variants": [{"id": v, "keywords": [], "resume": {}} for v in variants],
            "apply": {"threshold": threshold, "daily_cap": daily_cap, "company_daily_cap": company_daily_cap, "calibrated_with": "gpt-6-luna v5"}}


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / "t.db")
    yield connection
    connection.close()


def add(conn, n: int, points: int, *, variant="backend", valid_through="", status="new", model=MODEL,
        title=None, company="Empresa Falsa SA") -> str:
    """Oferta con detalle y puntaje; `n` define el id y el orden de llegada."""
    id16 = f"{n:016X}"
    db.upsert_offers(conn, [{"id16": id16, "data_id": f"{n:032X}", "title": title or f"Oferta {n}", "company": company,
                             "location": "Rosario", "url": f"https://ar.computrabajo.com/ofertas-de-trabajo/o-{n}"}], "x")
    db.save_detail(conn, id16, {"description": "d", "date_posted": "", "valid_through": valid_through,
                                "employment_type": ""})
    if model:
        db.save_score(conn, id16, model, "v5",
                      {"best_variant": variant, "score": points, "all_scores": {variant: points}, "reasons": [],
                       "missing": []}, (1, 0, 1), 0.0)
    if status != "new":
        db.mark_status(conn, id16, status)
    return id16


def mark_applied(conn, id16: str, applied_at: str | None = None, status="applied") -> None:
    conn.execute("INSERT INTO applications (id16, variant_id, score, status, reason, applied_at) VALUES (?, 'backend', 90, ?, '', ?)",
                 (id16, status, applied_at or datetime(2026, 11, 2, 10, 0).astimezone().isoformat()))
    conn.commit()


def reasons(plan: dict) -> dict:
    return {i["id16"]: i["reason"] for i in plan["send"] + plan["skip"]}


def run_plan(conn, **kwargs) -> dict:
    return apply.plan(conn, profile_dict(**kwargs), MODEL, TODAY)


# ── plan: motivos ────────────────────────────────────────────────────────────

def test_each_reason_shows_up_in_the_right_case(conn):
    ok = add(conn, 1, 90)
    applied = add(conn, 2, 95)
    expired = add(conn, 3, 92, valid_through="2026-11-01")
    low = add(conn, 4, 60)
    mark_applied(conn, applied)
    plan = run_plan(conn)
    assert [i["id16"] for i in plan["send"]] == [ok]
    assert reasons(plan) == {ok: "cumple las reglas", applied: "ya postulada", expired: "vencida", low: "puntaje bajo (60 < 75)"}


def test_reason_priority_is_applied_expired_low_unknown_variant_then_cap(conn):
    applied_and_expired = add(conn, 1, 90, valid_through="2020-01-01")
    expired_and_low = add(conn, 2, 10, valid_through="2020-01-01")
    low_and_unknown = add(conn, 3, 10, variant="otra")
    unknown_and_capped = add(conn, 4, 80, variant="otra")
    first = add(conn, 5, 99)
    capped = add(conn, 6, 98)
    mark_applied(conn, applied_and_expired)
    got = reasons(run_plan(conn, daily_cap=2))  # una postulada hoy: queda 1 lugar
    assert got == {applied_and_expired: "ya postulada", expired_and_low: "vencida", low_and_unknown: "puntaje bajo (10 < 75)",
                   unknown_and_capped: "variante desconocida (otra)", first: "cumple las reglas", capped: "tope diario"}


def test_expiry_edges(conn):
    today_ok = add(conn, 1, 90, valid_through="2026-11-02")  # vence hoy: todavía sirve
    empty = add(conn, 2, 90, valid_through="")
    garbage = add(conn, 3, 90, valid_through="pronto")
    with_time = add(conn, 4, 90, valid_through="2026-11-01T23:59:00")
    got = reasons(run_plan(conn))
    assert got[today_ok] == got[empty] == got[garbage] == "cumple las reglas" and got[with_time] == "vencida"


def test_threshold_is_inclusive(conn):
    exact, below = add(conn, 1, 75), add(conn, 2, 74)
    got = reasons(run_plan(conn))
    assert got == {exact: "cumple las reglas", below: "puntaje bajo (74 < 75)"}


def test_only_new_offers_scored_with_the_current_model_are_candidates(conn):
    add(conn, 1, 90, status="excluded")
    add(conn, 2, 90, status="bad_url")
    add(conn, 3, 90, model=None)  # sin puntaje
    add(conn, 4, 90, model="gpt-4o-mini")  # puntuada con otro modelo
    candidate = add(conn, 5, 90)
    assert list(reasons(run_plan(conn))) == [candidate]


def test_applications_that_are_not_applied_do_not_block(conn):
    offer = add(conn, 1, 90)
    mark_applied(conn, offer, status="error")
    assert reasons(run_plan(conn)) == {offer: "cumple las reglas"}


# ── plan: tope diario ────────────────────────────────────────────────────────

def test_daily_cap_minus_applied_today_fills_by_score(conn):
    done = add(conn, 1, 99)
    mark_applied(conn, done)
    low, high, mid = add(conn, 2, 80), add(conn, 3, 95), add(conn, 4, 88)
    plan = run_plan(conn, daily_cap=2)
    assert [i["id16"] for i in plan["send"]] == [high]  # queda 1 lugar y va a la de mayor puntaje
    assert reasons(plan) == {done: "ya postulada", high: "cumple las reglas", mid: "tope diario", low: "tope diario"}


def test_applied_today_counts_only_todays_local_date(conn):
    local_noon = datetime(2026, 11, 2, 12, 0).astimezone()
    mark_applied(conn, add(conn, 1, 90), local_noon.isoformat())  # hoy, con zona local
    mark_applied(conn, add(conn, 2, 90), local_noon.astimezone(timezone.utc).isoformat())  # hoy, en UTC
    mark_applied(conn, add(conn, 3, 90), "2026-11-02T08:30:00")  # hoy, sin zona
    mark_applied(conn, add(conn, 4, 90), (local_noon - timedelta(days=1)).isoformat())  # ayer
    mark_applied(conn, add(conn, 5, 90), "no es una fecha")
    mark_applied(conn, add(conn, 6, 90), None)  # applied_at por defecto del helper = hoy
    conn.execute("UPDATE applications SET applied_at = NULL WHERE id16 = ?", (f"{6:016X}",))
    mark_applied(conn, add(conn, 7, 90), local_noon.isoformat(), status="error")  # no es applied
    assert apply.applied_today(conn, TODAY) == 3


def test_cap_already_used_sends_nothing_and_never_goes_negative(conn):
    for n in (1, 2, 3):
        mark_applied(conn, add(conn, n, 90))
    waiting = add(conn, 4, 95)
    plan = run_plan(conn, daily_cap=2)  # 3 postuladas hoy, tope 2
    assert plan["send"] == [] and reasons(plan)[waiting] == "tope diario"


def test_skipped_offers_do_not_use_up_the_cap(conn):
    add(conn, 1, 60), add(conn, 2, 99, valid_through="2020-01-01"), add(conn, 3, 70, variant="otra")
    ok = add(conn, 4, 80)
    assert [i["id16"] for i in run_plan(conn, daily_cap=1)["send"]] == [ok]


# ── plan: forma y agrupación ─────────────────────────────────────────────────

def test_send_is_grouped_by_variant_in_profile_order_then_by_score(conn):
    ids = {(n, v): add(conn, n, points, variant=v) for n, points, v in
           [(1, 80, "data"), (2, 90, "backend"), (3, 95, "data"), (4, 85, "backend"), (5, 99, "backend")]}
    plan = run_plan(conn)
    assert [(i["variant"], i["score"]) for i in plan["send"]] == [
        ("backend", 99), ("backend", 90), ("backend", 85), ("data", 95), ("data", 80)]
    assert plan["send"][0]["id16"] == ids[(5, "backend")]


def test_items_have_the_documented_keys(conn):
    add(conn, 1, 90, title="Dev", company="Acme")
    add(conn, 2, 10)
    plan = run_plan(conn)
    assert set(plan) == {"send", "skip", "scored", "model", "calibration", "zones", "out_of_zone"}
    assert all(set(i) == ITEM_KEYS for i in plan["send"] + plan["skip"])
    assert plan["send"][0] == {"id16": f"{1:016X}", "title": "Dev", "company": "Acme", "score": 90, "variant": "backend",
                               "reason": "cumple las reglas", "url": "https://ar.computrabajo.com/ofertas-de-trabajo/o-1"}


def test_plan_does_not_write_anything(conn):
    add(conn, 1, 90)
    before = [conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("offers", "scores", "applications")]
    run_plan(conn)
    assert [conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("offers", "scores", "applications")] == before
    assert conn.execute("SELECT status FROM offers").fetchone()[0] == "new"


def test_summary_line(conn):
    applied = add(conn, 1, 90)
    mark_applied(conn, applied)
    add(conn, 2, 90, valid_through="2020-01-01"), add(conn, 3, 50), add(conn, 4, 40), add(conn, 5, 95), add(conn, 6, 85)
    plan = run_plan(conn, daily_cap=2)
    assert apply.summary(plan) == "Enviaría: 1 · saltaría: 5 (ya postulada: 1 · vencida: 1 · puntaje bajo: 2 · tope diario: 1)"
    add(conn, 7, 90, variant="otra")
    assert apply.summary(run_plan(conn, daily_cap=2)).endswith("tope diario: 1 · variante desconocida: 1)")


# ── informe ──────────────────────────────────────────────────────────────────

META = {"now": datetime(2026, 11, 2, 14, 3, 5), "threshold": 75, "daily_cap": 10, "applied_today": 1, "model": MODEL}


def table_rows(text: str, heading: str) -> list[list[str]]:
    """Filas de datos de la tabla bajo `heading`, partidas por los `|` sin escapar."""
    section = text.split(heading, 1)[1].split("\n## ", 1)[0]
    lines = [l for l in section.splitlines() if l.startswith("|")]
    return [re.split(r"(?<!\\)\|", l)[1:-1] for l in lines[2:]]


def test_report_has_header_note_and_the_two_tables(conn, tmp_path):
    add(conn, 1, 90, title="Dev | senior", company="A|B")
    add(conn, 2, 85, variant="data", title="Analista\nde datos")
    add(conn, 3, 40)
    path = apply.write_report(run_plan(conn), META, tmp_path / "runs" / "nuevo")
    assert path == tmp_path / "runs" / "nuevo" / "20261102-140305-apply-seco.md"
    text = path.read_text(encoding="utf-8")
    for expected in ("Fecha: 2026-11-02 14:03:05", "Modo: seco", "Umbral: 75", "Tope diario: 10 (ya postuladas hoy: 1)",
                     "Modelo: gpt-6-luna", "Las preguntas del formulario se verifican al postular (Fase 4).",
                     "## Enviaría (2)", "## Saltaría (1)"):
        assert expected in text, expected
    assert text.count("| oferta | empresa | puntaje | CV | motivo | url |") == 2
    send = table_rows(text, "## Enviaría")
    assert [[c.strip() for c in r] for r in send] == [
        ["Dev \\| senior", "A\\|B", "90", "backend", "cumple las reglas", "https://ar.computrabajo.com/ofertas-de-trabajo/o-1"],
        ["Analista de datos", "Empresa Falsa SA", "85", "data", "cumple las reglas", "https://ar.computrabajo.com/ofertas-de-trabajo/o-2"]]
    skip = table_rows(text, "## Saltaría")
    assert len(skip) == 1 and skip[0][4].strip() == "puntaje bajo (40 < 75)"
    assert all(len(r) == 6 for r in send + skip)  # el `|` del título no agregó columnas


def test_report_escapes_backslashes_so_they_cannot_break_the_table(conn, tmp_path):
    add(conn, 1, 90, title="a\\|b\\")
    text = apply.write_report(run_plan(conn), META, tmp_path).read_text(encoding="utf-8")
    [row] = table_rows(text, "## Enviaría")
    assert len(row) == 6 and row[0].strip() == "a\\\\\\|b\\\\"


def test_report_groups_send_rows_by_variant(conn, tmp_path):
    for n, points, v in [(1, 80, "data"), (2, 90, "backend"), (3, 95, "data"), (4, 85, "backend")]:
        add(conn, n, points, variant=v)
    text = apply.write_report(run_plan(conn), META, tmp_path).read_text(encoding="utf-8")
    assert [r[3].strip() for r in table_rows(text, "## Enviaría")] == ["backend", "backend", "data", "data"]


def test_report_with_nothing_keeps_both_tables(conn, tmp_path):
    text = apply.write_report(run_plan(conn), META, tmp_path).read_text(encoding="utf-8")
    assert text.count("| oferta | empresa |") == 2 and text.count("_Ninguna._") == 2
    assert "## Enviaría (0)" in text and "## Saltaría (0)" in text


def test_report_has_only_offer_data(conn, tmp_path):
    add(conn, 1, 90)
    text = apply.write_report(run_plan(conn), META, tmp_path).read_text(encoding="utf-8")
    assert "experience" not in text and "resume" not in text and "pdf" not in text.lower()


# ── CLI ──────────────────────────────────────────────────────────────────────

@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    """CLI aislado: cwd vacío (no se lee ningún .env real ni se escribe en el repo) y entorno limpio, sin clave."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(os, "environ", {})
    return tmp_path


def cli_args(tmp_path, *extra: str) -> list[str]:
    return ["apply", "--profile", str(EXAMPLE), "--db", str(tmp_path / "t.db"), *extra]


def seeded_db(tmp_path, **kwargs) -> db.sqlite3.Connection:
    connection = db.connect(tmp_path / "t.db")
    add(connection, 1, 90, **kwargs)
    add(connection, 2, 50)
    return connection


def test_cli_live_with_a_missing_profile_exits_1_and_touches_nothing(cli_env, capsys):
    assert cli.main(["apply", "--live", "--profile", str(cli_env / "no-existe.yaml"), "--db", str(cli_env / "t.db")]) == 1
    captured = capsys.readouterr()
    assert "No existe el perfil" in captured.err
    assert captured.out == "" and not (cli_env / "t.db").exists() and not (cli_env / "data").exists()


def test_cli_dry_run_prints_summary_writes_report_and_no_applications(cli_env, capsys):
    conn = seeded_db(cli_env)
    conn.close()
    assert cli.main(cli_args(cli_env)) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert lines[0] == "Enviaría: 1 · saltaría: 1 (ya postulada: 0 · vencida: 0 · puntaje bajo: 1 · tope diario: 0)"
    assert lines[1] == UNCALIBRATED  # profile.example.yaml no declara de dónde salió el umbral
    assert re.fullmatch(r"Informe: data[\\/]runs[\\/]\d{8}-\d{6}-apply-seco\.md", lines[2])
    [report] = (cli_env / "data" / "runs").glob("*-apply-seco.md")
    assert report.read_text(encoding="utf-8").count("| oferta |") == 2
    check = db.connect(cli_env / "t.db")
    assert check.execute("SELECT COUNT(*) FROM applications").fetchone()[0] == 0
    assert check.execute("SELECT status FROM offers ORDER BY rowid").fetchall() == [("new",), ("new",)]
    check.close()


def test_cli_uses_openai_model_from_the_environment_and_needs_no_api_key(cli_env, monkeypatch, capsys):
    conn = db.connect(cli_env / "t.db")
    add(conn, 1, 90, model="gpt-4o-mini")
    conn.close()
    assert cli.main(cli_args(cli_env)) == 0
    assert capsys.readouterr().out.startswith("Enviaría: 0 · saltaría: 0")  # sin OPENAI_MODEL se mira gpt-6-luna
    monkeypatch.setitem(os.environ, "OPENAI_MODEL", "gpt-4o-mini")
    assert cli.main(cli_args(cli_env)) == 0
    assert capsys.readouterr().out.startswith("Enviaría: 1 · saltaría: 0")
    assert "OPENAI_API_KEY" not in os.environ


def test_cli_reads_model_from_dot_env_in_the_cwd(cli_env, capsys):
    conn = db.connect(cli_env / "t.db")
    add(conn, 1, 90, model="gpt-5-nano")
    conn.close()
    (cli_env / ".env").write_text("OPENAI_MODEL=gpt-5-nano\n", encoding="utf-8")
    assert cli.main(cli_args(cli_env)) == 0
    assert capsys.readouterr().out.startswith("Enviaría: 1")


@pytest.mark.parametrize("section, message", [("threshold: 101", "apply.threshold"), ("daily_cap: 0", "apply.daily_cap")])
def test_cli_invalid_apply_settings_exit_1(cli_env, capsys, section, message):
    (cli_env / "examples").mkdir()
    for name in ("resume.example.yaml", "cv.example.pdf"):
        (cli_env / "examples" / name).write_bytes((ROOT / "examples" / name).read_bytes())
    (cli_env / "profile.yaml").write_text(
        "variants:\n  - id: backend\n    resume: examples/resume.example.yaml\n    pdf: examples/cv.example.pdf\n"
        f"apply:\n  {section}\n", encoding="utf-8")
    assert cli.main(["apply", "--profile", str(cli_env / "profile.yaml"), "--db", str(cli_env / "t.db")]) == 1
    captured = capsys.readouterr()
    assert message in captured.err and captured.out == ""
    assert not (cli_env / "data").exists()


# ── variantes sin PDF ────────────────────────────────────────────────────────

def test_report_no_longer_says_that_a_pdf_is_needed_to_apply(conn, tmp_path):
    add(conn, 1, 90)  # con el CV a medida, `apply --live` no usa el `pdf` base: la nota ya no es cierta
    text = apply.write_report(run_plan(conn), {**META, "variants_without_pdf": ["backend"]}, tmp_path / "a").read_text(encoding="utf-8")
    assert "sin PDF" not in text and "hace falta para postular" not in text and not hasattr(apply, "no_pdf_lines")


def write_profile_without_pdf(folder: Path) -> Path:
    (folder / "examples").mkdir(exist_ok=True)
    (folder / "examples" / "resume.example.yaml").write_bytes((ROOT / "examples" / "resume.example.yaml").read_bytes())
    path = folder / "profile.yaml"
    path.write_text("variants:\n  - id: backend\n    resume: examples/resume.example.yaml\n    keywords: [python]\n",
                    encoding="utf-8")
    return path


def test_cli_apply_report_has_no_note_about_variants_without_pdf(cli_env, capsys):
    seeded_db(cli_env).close()
    profile_path = write_profile_without_pdf(cli_env)
    assert cli.main(["apply", "--profile", str(profile_path), "--db", str(cli_env / "t.db")]) == 0
    [report] = (cli_env / "data" / "runs").glob("*-apply-seco.md")
    assert "sin PDF" not in report.read_text(encoding="utf-8")
    assert capsys.readouterr().out.startswith("Enviaría: 1")


NOTE_GPT4 = ("Todavía no hay ofertas puntuadas con gpt-4o-mini: puntuá primero (Correr → Puntuar pendientes, "
             "o una corrida con score_limit mayor que 0).")


def test_plan_counts_the_scores_of_the_model_and_the_note_only_shows_without_any(conn):
    add(conn, 1, 90, model="gpt-6-luna")
    other = apply.plan(conn, profile_dict(), "gpt-4o-mini", TODAY)
    assert other["scored"] == 0 and apply.plan_notes(other) == [NOTE_GPT4, (
        "El umbral 75 se calibró con gpt-6-luna v5; ahora se puntúa con gpt-4o-mini v5: calibrá de nuevo antes de postular.")]
    assert run_plan(conn)["scored"] == 1 and apply.plan_notes(run_plan(conn)) == []


def test_cli_and_report_show_the_note_without_scores_for_the_model(cli_env, monkeypatch, capsys):
    monkeypatch.setitem(os.environ, "OPENAI_MODEL", "gpt-4o-mini")
    seeded_db(cli_env).close()  # puntajes de gpt-6-luna: ninguno es del modelo en uso
    assert cli.main(cli_args(cli_env)) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert lines[0].startswith("Enviaría: 0 · saltaría: 0") and lines[1:3] == [NOTE_GPT4, UNCALIBRATED] and lines[3].startswith("Informe: ")
    [report] = (cli_env / "data" / "runs").glob("*-apply-seco.md")
    assert {NOTE_GPT4, UNCALIBRATED} <= set(report.read_text(encoding="utf-8").splitlines())


def test_cli_and_report_have_no_note_with_scores(cli_env, capsys):
    seeded_db(cli_env).close()
    assert cli.main(cli_args(cli_env)) == 0
    out = capsys.readouterr().out
    [report] = (cli_env / "data" / "runs").glob("*-apply-seco.md")
    assert "Todavía no hay" not in out and "Todavía no hay" not in report.read_text(encoding="utf-8")


def test_calibration_note_has_three_cases():
    def profile_with(**apply_):
        return {**profile_dict(), "apply": {"threshold": 60, "daily_cap": 10, **apply_}}

    assert apply.calibration_note(profile_with(), "gpt-4o-mini") == (
        "El umbral 60 no salió de la calibración: etiquetá en Calibrar y usá el umbral sugerido.")
    assert apply.calibration_note(profile_with(calibrated_with="gpt-6-luna v1"), "gpt-4o-mini") == (
        "El umbral 60 se calibró con gpt-6-luna v1; ahora se puntúa con gpt-4o-mini v5: calibrá de nuevo antes de postular.")
    assert apply.calibration_note(profile_with(calibrated_with="gpt-4o-mini v5"), "gpt-4o-mini") is None


def test_the_calibration_note_is_in_the_plan_and_in_the_report(conn, tmp_path):
    add(conn, 1, 90)
    stale = {**profile_dict(), "apply": {"threshold": 75, "daily_cap": 10, "calibrated_with": "gpt-4o-mini v5"}}
    plan = apply.plan(conn, stale, MODEL, TODAY)
    [note] = apply.plan_notes(plan)
    assert note.startswith("El umbral 75 se calibró con gpt-4o-mini v5; ahora se puntúa con gpt-6-luna v5")
    meta = {"now": datetime(2026, 11, 2, 14, 3, 5), "threshold": 75, "daily_cap": 10, "applied_today": 0, "model": MODEL}
    assert note in apply.write_report(plan, meta, tmp_path).read_text(encoding="utf-8")
    assert apply.plan_notes(run_plan(conn)) == []
