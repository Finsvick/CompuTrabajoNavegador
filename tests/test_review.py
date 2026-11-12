"""Tests de review.py y del subcomando `review`. CSV y bases armados en tmp_path; `ask` y `out` falsos; sin API."""
import csv
import io
import random
import re
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from compuapply import cli, db, review

HEADER = "id16;score;variante;titulo;empresa;ubicacion;url;razones;faltantes;postularia;excluyentes"

# (score, etiqueta) a mano. Positivas: 10. Por umbral (enviaría, precisión truncada, cobertura):
# 30 a 45: 20, 50%, 100% · 50: 19, 52%, 100% · 55: 17, 58%, 100% · 60: 15, 66%, 100% · 65: 13, 69%, 90% · 70: 12, 75%, 90%
# 75: 10, 80%, 80% · 80: 7, 85%, 60% · 85: 5, 100%, 50% · 90: 3, 100%, 30% · 95: 1, 100%, 10%
HAND_ROWS = [(95, True), (92, True), (90, True), (88, True), (85, True), (82, False), (80, True), (78, True),
             (76, False), (75, True), (72, False), (70, True), (66, False), (62, True), (60, False), (58, False),
             (55, False), (52, False), (50, False), (45, False)]
EXPECTED = [  # umbral, enviaría, aciertos
    (30, 20, 10), (35, 20, 10), (40, 20, 10), (45, 20, 10), (50, 19, 10), (55, 17, 10), (60, 15, 10), (65, 13, 9), (70, 12, 9),
    (75, 10, 8), (80, 7, 6), (85, 5, 5), (90, 3, 3), (95, 1, 1),
]


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """CLI aislado: cwd vacío (no se lee ningún .env real) y entorno limpio, así el modelo en uso es gpt-6-luna."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("os.environ", {})


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / "t.db")
    yield connection
    connection.close()


def add_scored(conn, n: int, points: int, *, title=None, company="Empresa Falsa SA", reasons=("Encaja",),
               missing=(), model="gpt-6-luna", scored_at=None) -> str:
    id16 = f"{n:016X}"
    db.upsert_offers(conn, [{"id16": id16, "data_id": f"{n:032X}", "title": title or f"Oferta {n}", "company": company,
                             "location": "Rosario", "url": f"https://ar.computrabajo.com/ofertas-de-trabajo/o-{n}"}], "x")
    result = {"best_variant": "backend", "score": points, "all_scores": {"backend": points},
              "reasons": list(reasons), "missing": list(missing)}
    db.save_score(conn, id16, model, "v5", result, (1000, 400, 200), 0.000164)
    if scored_at:
        conn.execute("UPDATE scores SET scored_at = ? WHERE id16 = ? AND model = ?", (scored_at, id16, model))
        conn.commit()
    return id16


def add_detailed(conn, n: int, points: int, description="Descripción falsa de la oferta.", kind="Full-time") -> str:
    """Oferta puntuada con descripción; razones, faltantes y variante son marcas fáciles de buscar en lo impreso."""
    id16 = add_scored(conn, n, points, reasons=("RAZON-SECRETA",), missing=("FALTANTE-SECRETO",))
    db.save_detail(conn, id16, {"description": description, "date_posted": None, "valid_through": None,
                                "employment_type": kind})
    return id16


def fake_ask(*answers):
    """`ask` falso: devuelve las respuestas en orden; una excepción se levanta; sin respuestas, fin de entrada."""
    queue, prompts = list(answers), []

    def ask(prompt):
        prompts.append(prompt)
        if not queue:
            raise EOFError
        answer = queue.pop(0)
        if isinstance(answer, type) and issubclass(answer, BaseException):
            raise answer
        return answer

    ask.prompts = prompts
    return ask


def session(conn, limit, *answers, seed=1):
    """Corre `label_session` con `ask`/`out` falsos y devuelve (resultado, lo impreso, ask)."""
    lines, ask = [], fake_ask(*answers)
    result = review.label_session(conn, limit, ask=ask, out=lines.append, rng=random.Random(seed))
    return result, lines, ask


def shown_titles(lines: list[str]) -> list[str]:
    return [m.group(1) for line in lines if (m := re.match(r"\[\d+/\d+\] (.+)$", line.strip()))]


def id_of(title: str) -> str:
    return f"{int(title.split()[-1]):016X}"


def stored_labels(tmp_path) -> dict:
    """Etiquetas leídas con otra conexión, para probar que se guardaron de verdad."""
    other = sqlite3.connect(tmp_path / "t.db")
    try:
        return dict(other.execute("SELECT id16, label FROM labels"))
    finally:
        other.close()


def read_rows(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f, delimiter=";"))


def write_label(path: Path, id16: str, label: str) -> None:
    """Edita el CSV como lo haría el usuario: completa `postularia` de una fila."""
    rows = read_rows(path)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=review.COLUMNS, delimiter=";")
        writer.writeheader()
        for row in rows:
            writer.writerow({**row, "postularia": label if row["id16"] == f"ct-{id16}" else row["postularia"]})


def write_labeled_csv(path: Path, rows: list[tuple[int, object]], id_prefix: str = "") -> None:
    """CSV con filas `(score, etiqueta)`; los ids son `<prefijo><N en 16 hex>` (sin prefijo = CSV viejo)."""
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow(review.COLUMNS)
        for n, (points, label) in enumerate(rows, 1):
            writer.writerow([f"{id_prefix}{n:016X}", points, "backend", f"Oferta {n}", "Empresa", "Rosario", "url", "", "",
                             label])


def write_rows(path: Path, rows: list[tuple[str, str]]) -> None:
    """CSV con filas `(id16 tal cual, etiqueta)`, para probar ids de todas las formas."""
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow(review.COLUMNS)
        for raw_id, label in rows:
            writer.writerow([raw_id, 80, "backend", "Oferta", "Empresa", "Rosario", "url", "", "", label])


# ── export ───────────────────────────────────────────────────────────────────

def test_export_writes_exact_header_and_sorts_by_score(conn, tmp_path):
    add_scored(conn, 1, 60, reasons=("Python", "Remoto"), missing=("Inglés",))
    add_scored(conn, 2, 90, title="Desarrollador; senior", missing=("Java", "AWS"))
    add_scored(conn, 3, 75)
    out = tmp_path / "out" / "review.csv"
    assert review.export_csv(conn, out, "gpt-6-luna") == (3, 0)
    raw = out.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf" + HEADER.encode("utf-8") + b"\r\n")  # utf-8-sig y «;»
    rows = read_rows(out)
    assert [r["score"] for r in rows] == ["90", "75", "60"]
    assert list(rows[0]) == review.COLUMNS
    assert rows[0]["titulo"] == "Desarrollador; senior"  # el «;» del dato no rompe la fila
    assert rows[0]["faltantes"] == "Java | AWS" and rows[2]["razones"] == "Python | Remoto"
    assert rows[2]["faltantes"] == "Inglés"
    assert all(r["postularia"] == "" for r in rows)
    assert rows[0]["variante"] == "backend" and rows[0]["empresa"] == "Empresa Falsa SA" and rows[0]["ubicacion"] == "Rosario"
    assert rows[0]["url"] == "https://ar.computrabajo.com/ofertas-de-trabajo/o-2"
    assert [r["id16"] for r in rows] == [f"ct-{2:016X}", f"ct-{3:016X}", f"ct-{1:016X}"]


def test_export_uses_the_most_recent_score_per_offer(conn, tmp_path):
    add_scored(conn, 1, 40, model="gpt-4o-mini", scored_at="2026-01-01T00:00:00+00:00")
    db.save_score(conn, f"{1:016X}", "gpt-6-luna", "v5",
                  {"best_variant": "backend", "score": 88, "all_scores": {"backend": 88}, "reasons": [], "missing": []},
                  (1, 0, 1), 0.0)
    conn.execute("UPDATE scores SET scored_at = '2026-06-01T00:00:00+00:00' WHERE model = 'gpt-6-luna'")
    conn.commit()
    out = tmp_path / "r.csv"
    assert review.export_csv(conn, out, "gpt-6-luna") == (1, 0)
    assert [r["score"] for r in read_rows(out)] == ["88"]


def test_export_with_no_scores_writes_only_the_header(conn, tmp_path):
    out = tmp_path / "r.csv"
    assert review.export_csv(conn, out, "gpt-6-luna") == (0, 0)
    assert out.read_text(encoding="utf-8-sig").strip() == HEADER


def test_second_export_keeps_labels_and_adds_new_offers(conn, tmp_path):
    first = add_scored(conn, 1, 80)
    add_scored(conn, 2, 70)
    out = tmp_path / "r.csv"
    review.export_csv(conn, out, "gpt-6-luna")
    write_label(out, first, "s")
    add_scored(conn, 3, 95)
    assert review.export_csv(conn, out, "gpt-6-luna") == (3, 1)
    rows = read_rows(out)
    assert [(r["id16"], r["score"], r["postularia"]) for r in rows] == [
        (f"ct-{3:016X}", "95", ""), (f"ct-{first}", "80", "s"), (f"ct-{2:016X}", "70", "")]
    assert review.export_csv(conn, out, "gpt-6-luna") == (3, 1)  # idempotente


# ── id con prefijo `ct-` ─────────────────────────────────────────────────────

def test_export_prefixes_every_id_so_excel_cannot_read_it_as_a_number(conn, tmp_path):
    scientific = 0x1234567E89012345  # 1234567E89012345: sin prefijo Excel lo vería como 1,23E+21
    add_scored(conn, scientific, 90)
    add_scored(conn, 2, 80)
    out = tmp_path / "r.csv"
    review.export_csv(conn, out, "gpt-6-luna")
    ids = [r["id16"] for r in read_rows(out)]
    assert ids == ["ct-1234567E89012345", f"ct-{2:016X}"]
    assert all(i.startswith("ct-") for i in ids)


def test_labels_are_kept_from_prefixed_unprefixed_and_mixed_csv(conn, tmp_path):
    ids = [add_scored(conn, n, 90 - n) for n in (1, 2, 3, 4)]
    out = tmp_path / "r.csv"
    write_rows(out, [(f"ct-{ids[0]}", "s"), (ids[1], "n"), (f"CT-{ids[2]}", "S"), (ids[3], "")])  # nuevo, viejo, mayúsculas, sin etiqueta
    assert review.export_csv(conn, out, "gpt-6-luna") == (4, 3)
    assert [(r["id16"], r["postularia"]) for r in read_rows(out)] == [
        (f"ct-{ids[0]}", "s"), (f"ct-{ids[1]}", "n"), (f"ct-{ids[2]}", "S"), (f"ct-{ids[3]}", "")]


def test_invalid_ids_are_ignored_and_counted(conn, tmp_path):
    good = add_scored(conn, 1, 90)
    out = tmp_path / "r.csv"
    write_rows(out, [(f"ct-{good}", "s"), ("ct-XYZ", "s"), ("1234", "s"), ("", "s"), ("ct-" + "A" * 17, "s"),
                     ("ct-" + "A" * 15, "s"), ("ct-" + "Z" * 16, "s"), ("ct-", "s")])
    assert review.count_invalid_ids(out) == 7
    assert review.export_csv(conn, out, "gpt-6-luna") == (1, 1)  # solo la fila con id válido aporta etiqueta
    assert [r["id16"] for r in read_rows(out)] == [f"ct-{good}"]
    assert review.count_invalid_ids(out) == 0
    assert review.count_invalid_ids(tmp_path / "no-existe.csv") == 0


def test_read_labels_accepts_both_id_forms_and_skips_invalid_ids(tmp_path):
    path = tmp_path / "r.csv"
    write_rows(path, [(f"ct-{1:016X}", "s"), (f"{2:016X}", "n"), ("ct-roto", "s"), ("", "n")])
    assert review.read_labels(path) == [{"score": 80, "label": True}, {"score": 80, "label": False}]
    write_labeled_csv(path, [(70, "s"), (60, "n")], id_prefix="ct-")
    assert review.read_labels(path) == [{"score": 70, "label": True}, {"score": 60, "label": False}]


def test_export_neutralizes_spreadsheet_formulas(conn, tmp_path):
    add_scored(conn, 1, 80, title="=HYPERLINK(\"http://x\")", company="+cmd", reasons=("@SUM(A1)",), missing=("-2+3",))
    out = tmp_path / "r.csv"
    review.export_csv(conn, out, "gpt-6-luna")
    [row] = read_rows(out)
    assert row["titulo"].startswith("'=") and row["empresa"].startswith("'+")
    assert row["razones"].startswith("'@") and row["faltantes"].startswith("'-")


def test_export_does_not_overwrite_an_unusable_existing_csv(conn, tmp_path):
    add_scored(conn, 1, 80)
    ansi = tmp_path / "ansi.csv"
    ansi.write_bytes((HEADER + "\r\n" + f"{1:016X};80;b;Diseñador;;;;;;s\r\n").encode("cp1252"))
    with pytest.raises(RuntimeError, match="UTF-8"):
        review.export_csv(conn, ansi, "gpt-6-luna")
    wrong = tmp_path / "wrong.csv"
    wrong.write_text("a,b,c\n1,2,3\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="faltan las columnas"):
        review.export_csv(conn, wrong, "gpt-6-luna")
    assert wrong.read_text(encoding="utf-8") == "a,b,c\n1,2,3\n"  # no se pisó


# ── read_labels ──────────────────────────────────────────────────────────────

def test_read_labels_accepts_s_and_n_in_any_case_and_ignores_the_rest(tmp_path):
    path = tmp_path / "r.csv"
    write_labeled_csv(path, [(90, "s"), (80, "N"), (70, " S "), (60, ""), (50, "x"), (40, "si"), (30, "n")])
    assert review.read_labels(path) == [
        {"score": 90, "label": True}, {"score": 80, "label": False}, {"score": 70, "label": True},
        {"score": 30, "label": False}]


def test_read_labels_errors(tmp_path):
    with pytest.raises(RuntimeError, match="No existe"):
        review.read_labels(tmp_path / "no-esta.csv")
    bad_score = tmp_path / "bad.csv"
    write_labeled_csv(bad_score, [("mucho", "s")])
    with pytest.raises(RuntimeError, match="score que no es un número"):
        review.read_labels(bad_score)
    comma = tmp_path / "comma.csv"
    comma.write_text("id16,score,postularia\n1,80,s\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="faltan las columnas"):
        review.read_labels(comma)


# ── threshold_table ──────────────────────────────────────────────────────────

def test_threshold_table_with_hand_made_rows():
    table, suggested = review.threshold_table([{"score": s, "label": l} for s, l in HAND_ROWS])
    assert [r["threshold"] for r in table] == list(range(30, 100, 5))
    for row, (threshold, send, hits) in zip(table, EXPECTED):
        assert (row["threshold"], row["send"]) == (threshold, send)
        assert row["precision"] == pytest.approx(100 * hits / send)
        assert row["recall"] == pytest.approx(100 * hits / 10)
    assert suggested == 75  # el más bajo con precisión >= 80% (exactamente 80%)


def test_threshold_table_never_reaching_80_percent():
    rows = [{"score": 60, "label": n % 2 == 0} for n in range(20)]
    table, suggested = review.threshold_table(rows)
    assert suggested is None
    assert table[6]["precision"] == pytest.approx(50.0)  # umbral 60
    assert (table[7]["send"], table[7]["precision"]) == (0, None)  # umbral 65: no enviaría nada


def test_threshold_table_without_positives_has_no_recall():
    table, suggested = review.threshold_table([{"score": 90, "label": False}] * 20)
    assert suggested is None and all(r["recall"] is None for r in table)


# ── CLI ──────────────────────────────────────────────────────────────────────

def test_cli_review_export_and_threshold_end_to_end(conn, tmp_path, capsys):
    for n, (points, _) in enumerate(HAND_ROWS, 1):
        add_scored(conn, n, points)
    out = tmp_path / "review.csv"
    db_path = str(tmp_path / "t.db")
    assert cli.main(["review", "export", "--out", str(out), "--db", db_path]) == 0
    assert capsys.readouterr().out.strip() == f"Ofertas exportadas: 20 · etiquetas conservadas: 0 · archivo: {out}"
    for n, (_, label) in enumerate(HAND_ROWS, 1):
        write_label(out, f"{n:016X}", "s" if label else "n")
    assert cli.main(["review", "export", "--out", str(out), "--db", db_path]) == 0
    assert "etiquetas conservadas: 20" in capsys.readouterr().out
    assert cli.main(["review", "threshold", "--csv", str(out)]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert lines[0] == "30 · enviaría: 20 · precisión: 50% · cobertura: 100%"
    assert lines[4] == "50 · enviaría: 19 · precisión: 52% · cobertura: 100%"
    assert lines[9] == "75 · enviaría: 10 · precisión: 80% · cobertura: 80%"
    assert lines[13] == "95 · enviaría: 1 · precisión: 100% · cobertura: 10%"
    assert len(lines) == 15 and lines[-1] == "Umbral sugerido: 75 (precisión 80%, cobertura 80%)"


def test_cli_threshold_needs_20_labels(tmp_path, capsys):
    path = tmp_path / "r.csv"
    write_labeled_csv(path, [(80, "s")] * 19 + [(70, "")])
    assert cli.main(["review", "threshold", "--csv", str(path)]) == 1
    captured = capsys.readouterr()
    assert "Etiquetá al menos 20 ofertas (hay 19)" in captured.err and captured.out == ""
    write_labeled_csv(path, [(80, "s")] * 20)
    assert cli.main(["review", "threshold", "--csv", str(path)]) == 0


def test_cli_threshold_none_reaches_80_percent(tmp_path, capsys):
    path = tmp_path / "r.csv"
    write_labeled_csv(path, [(60, "s" if n % 2 else "n") for n in range(20)])
    assert cli.main(["review", "threshold", "--csv", str(path)]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert lines[7] == "65 · enviaría: 0 · precisión: - · cobertura: 0%"
    assert lines[-1] == "Ningún umbral llega a 80% de precisión"
    assert len(lines) == 15


def test_cli_export_warns_about_invalid_ids_once(conn, tmp_path, capsys):
    good = add_scored(conn, 1, 90)
    out = tmp_path / "review.csv"
    write_rows(out, [(f"ct-{good}", "s"), ("basura", "s"), ("ct-123", "n")])
    args = ["review", "export", "--out", str(out), "--db", str(tmp_path / "t.db")]
    assert cli.main(args) == 0
    captured = capsys.readouterr()
    assert "etiquetas conservadas: 1" in captured.out and captured.err.strip() == "Filas con id inválido: 2"
    assert cli.main(args) == 0  # el export dejó el archivo sin filas inválidas
    assert capsys.readouterr().err == ""


def test_cli_threshold_warns_about_invalid_ids_and_does_not_count_them(tmp_path, capsys):
    path = tmp_path / "r.csv"
    write_rows(path, [(f"ct-{n:016X}", "s") for n in range(1, 20)] + [("x", "s"), ("ct-", "n")])  # 19 válidas
    assert cli.main(["review", "threshold", "--csv", str(path)]) == 1
    err = capsys.readouterr().err
    assert "Filas con id inválido: 2" in err and "Etiquetá al menos 20 ofertas (hay 19)" in err
    write_rows(path, [(f"ct-{n:016X}", "s") for n in range(1, 21)] + [("x", "s")])
    assert cli.main(["review", "threshold", "--csv", str(path)]) == 0
    captured = capsys.readouterr()
    assert captured.err.strip() == "Filas con id inválido: 1" and captured.out.strip().endswith("(precisión 100%, cobertura 100%)")


def test_cli_review_errors_exit_1(tmp_path, capsys):
    assert cli.main(["review", "threshold", "--csv", str(tmp_path / "no-esta.csv")]) == 1
    assert "No existe" in capsys.readouterr().err
    ansi = tmp_path / "ansi.csv"
    ansi.write_bytes((HEADER + "\r\n1;80;b;Diseñador;;;;;;s\r\n").encode("cp1252"))
    assert cli.main(["review", "export", "--out", str(ansi), "--db", str(tmp_path / "t.db")]) == 1
    assert "UTF-8" in capsys.readouterr().err


# ── label_session ────────────────────────────────────────────────────────────

def test_label_session_never_shows_score_variant_reasons_or_missing(conn):
    add_detailed(conn, 1, 87)
    add_detailed(conn, 2, 91, description="Se busca persona con ganas de aprender.", kind="Part-time")
    add_scored(conn, 3, 64, reasons=("RAZON-SECRETA",), missing=("FALTANTE-SECRETO",))  # sin detalle: no hay descripción
    result, lines, ask = session(conn, 30, "s", "n", "s")
    text = "\n".join(lines)
    for secret in ("87", "91", "64", "backend", "RAZON-SECRETA", "FALTANTE-SECRETO", "score", "None"):
        assert secret not in text
    assert all(secret not in prompt for prompt in ask.prompts for secret in ("87", "91", "64", "backend", "SECRETO"))
    assert ask.prompts == [review.LABEL_PROMPT] * 3
    assert review.LABEL_PROMPT == "¿Te postularías? [s/n · Enter = saltar · q = salir]: "
    assert result == {"labeled": 3, "yes": 2, "no": 1, "total": 3}
    assert len(shown_titles(lines)) == 3 and "[1/3]" in text and "[3/3]" in text
    assert "Oferta 2" in text and "Empresa Falsa SA · Rosario · Part-time" in text
    assert "Se busca persona con ganas de aprender." in text
    assert "https://ar.computrabajo.com/ofertas-de-trabajo/o-2" in text


def test_label_session_saves_s_n_skips_enter_and_repeats_after_an_invalid_answer(conn, tmp_path):
    for n in range(1, 6):
        add_detailed(conn, n, 50 + n)
    result, lines, ask = session(conn, 30, "s", "N", "", "x", "q")
    assert result == {"labeled": 2, "yes": 1, "no": 1, "total": 2}
    assert len(ask.prompts) == 5  # 4 ofertas mostradas; la cuarta se preguntó dos veces (la respuesta «x» no vale)
    first, second, third, fourth = shown = shown_titles(lines)
    assert len(shown) == 4 and "[1/5]" in "\n".join(lines)
    assert stored_labels(tmp_path) == {id_of(first): 1, id_of(second): 0}  # tercera saltada, cuarta sin respuesta
    assert lines[-1] == "\nEtiquetadas en esta sesión: 2 (s: 1 · n: 1) · total en la base: 2"


@pytest.mark.parametrize("stop", [KeyboardInterrupt, EOFError])
def test_label_session_keeps_what_was_labeled_when_interrupted(conn, tmp_path, stop):
    for n in range(1, 4):
        add_detailed(conn, n, 70)
    result, lines, _ = session(conn, 30, "s", "n", stop)
    assert result == {"labeled": 2, "yes": 1, "no": 1, "total": 2}
    assert len(stored_labels(tmp_path)) == 2
    assert lines[-1] == "\nEtiquetadas en esta sesión: 2 (s: 1 · n: 1) · total en la base: 2"


def test_label_session_quit_is_case_insensitive_and_saves_nothing_extra(conn, tmp_path):
    add_detailed(conn, 1, 70)
    add_detailed(conn, 2, 70)
    assert session(conn, 30, " S ", "Q")[0] == {"labeled": 1, "yes": 1, "no": 0, "total": 1}
    assert len(stored_labels(tmp_path)) == 1


def test_labeled_offers_do_not_come_back_and_limit_cuts(conn):
    for n in range(1, 6):
        add_detailed(conn, n, 70)
    result, lines, ask = session(conn, 2, "s", "n", "s", "s")
    assert result["labeled"] == 2 and len(ask.prompts) == 2 and "[2/2]" in "\n".join(lines)
    done = shown_titles(lines)
    lines = session(conn, 30, "", "", "")[1]
    assert len(shown_titles(lines)) == 3 and "[1/3]" in "\n".join(lines)
    assert not set(done) & set(shown_titles(lines))
    assert len(shown_titles(session(conn, 30, "", "", "")[1])) == 3  # las saltadas vuelven en la sesión siguiente


def test_label_session_order_is_random_but_reproducible_with_a_seed(conn):
    for n in range(1, 11):
        add_detailed(conn, n, 50 + 4 * n)
    skip = [""] * 10
    first = shown_titles(session(conn, 30, *skip, seed=5)[1])
    assert first == shown_titles(session(conn, 30, *skip, seed=5)[1])
    by_score = [f"Oferta {n}" for n in range(10, 0, -1)]
    assert sorted(first) == sorted(by_score) and first not in (by_score, by_score[::-1])
    assert first != shown_titles(session(conn, 30, *skip, seed=6)[1])
    assert review.label_session(conn, 30, ask=fake_ask(), out=lambda _: None)["labeled"] == 0  # sin rng también anda


def test_label_session_without_candidates_only_reports(conn):
    add_scored(conn, 1, 80)  # puntuada
    db.upsert_offers(conn, [{"id16": f"{2:016X}", "data_id": "x", "title": "Sin puntaje", "company": "c",
                             "location": "l", "url": "u"}], "x")  # sin puntaje: no es candidata
    result, lines, ask = session(conn, 0, "s")
    assert result == {"labeled": 0, "yes": 0, "no": 0, "total": 0} and ask.prompts == []
    assert lines == ["\nEtiquetadas en esta sesión: 0 (s: 0 · n: 0) · total en la base: 0"]
    assert shown_titles(session(conn, 30, "q")[1]) == ["Oferta 1"]  # solo la puntuada


def test_label_session_suggests_threshold_from_20_labels(conn):
    for n in range(1, 21):
        add_detailed(conn, n, 70)
    _, lines, _ = session(conn, 19, *["s"] * 19)
    assert "Ya podés correr" not in "\n".join(lines)
    result, lines, _ = session(conn, 30, "n")
    assert result["total"] == 20 and lines[-1] == "Ya podés correr: python -m compuapply review threshold"
    assert lines[-2] == "\nEtiquetadas en esta sesión: 1 (s: 0 · n: 1) · total en la base: 20"


def test_label_candidates_has_exactly_the_shown_fields_and_nothing_of_the_score(conn):
    add_detailed(conn, 1, 87)
    add_detailed(conn, 2, 91, description="Se busca persona con ganas de aprender.", kind="Part-time")
    add_scored(conn, 3, 64, reasons=("RAZON-SECRETA",), missing=("FALTANTE-SECRETO",))  # sin detalle
    candidates = review.label_candidates(conn, 30, random.Random(1))
    assert len(candidates) == 3
    for candidate in candidates:
        assert set(candidate) == {"id16", "title", "company", "location", "employment_type", "excerpt", "url"}
    dump = repr(candidates)
    for secret in ("87", "91", "64", "backend", "RAZON-SECRETA", "FALTANTE-SECRETO", "score", "None"):
        assert secret not in dump
    shown = next(c for c in candidates if c["id16"] == f"{2:016X}")
    assert shown == {"id16": f"{2:016X}", "title": "Oferta 2", "company": "Empresa Falsa SA", "location": "Rosario",
                     "employment_type": "Part-time", "excerpt": "Se busca persona con ganas de aprender.",
                     "url": "https://ar.computrabajo.com/ofertas-de-trabajo/o-2"}
    assert next(c for c in candidates if c["id16"] == f"{3:016X}")["excerpt"] == ""  # sin descripción: texto vacío


def test_label_candidates_none_fields_become_empty_text(conn):
    id16 = f"{1:016X}"
    db.upsert_offers(conn, [{"id16": id16, "data_id": "x", "title": None, "company": None, "location": None, "url": None}], "x")
    db.save_score(conn, id16, "gpt-6-luna", "v5", {"best_variant": "a", "score": 70, "all_scores": {"a": 70},
                                                    "reasons": [], "missing": []}, (1, 0, 1), 0.0)
    assert review.label_candidates(conn, 5) == [{"id16": id16, "title": "", "company": "", "location": "",
                                                 "employment_type": "", "excerpt": "", "url": ""}]


def test_label_candidates_cuts_the_description_like_the_terminal(conn):
    add_detailed(conn, 1, 70, description="palabra " * 200)
    assert review.label_candidates(conn, 5)[0]["excerpt"] == "palabra " * 99 + "palabra…"


def test_label_candidates_skips_labeled_and_unscored_offers_and_respects_the_limit(conn):
    for n in range(1, 6):
        add_detailed(conn, n, 60 + n)
    db.upsert_offers(conn, [{"id16": f"{9:016X}", "data_id": "x", "title": "Sin puntaje", "company": "c", "location": "l",
                             "url": "u"}], "x")
    review.save_label(conn, f"{2:016X}", True)
    assert {c["id16"] for c in review.label_candidates(conn, 30)} == {f"{n:016X}" for n in (1, 3, 4, 5)}
    assert len(review.label_candidates(conn, 2)) == 2
    assert review.label_candidates(conn, 0) == [] and review.label_candidates(conn, -3) == []


def test_label_candidates_order_is_random_reproducible_and_not_by_score(conn):
    for n in range(1, 11):
        add_detailed(conn, n, 50 + 4 * n)
    first = [c["title"] for c in review.label_candidates(conn, 30, random.Random(5))]
    assert first == [c["title"] for c in review.label_candidates(conn, 30, random.Random(5))]
    by_score = [f"Oferta {n}" for n in range(10, 0, -1)]
    assert sorted(first) == sorted(by_score) and first not in (by_score, by_score[::-1])
    assert first != [c["title"] for c in review.label_candidates(conn, 30, random.Random(6))]
    assert len(review.label_candidates(conn, 30)) == 10  # sin rng también anda


def test_save_label_stores_a_yes_or_no_confirms_it_and_the_offer_does_not_come_back(conn, tmp_path):
    ids = [add_detailed(conn, n, 70) for n in (1, 2, 3)]
    review.save_label(conn, ids[0], True)
    review.save_label(conn, ids[1], False)
    assert stored_labels(tmp_path) == {ids[0]: 1, ids[1]: 0}  # visible desde otra conexión: está confirmado
    assert [c["id16"] for c in review.label_candidates(conn, 30)] == [ids[2]]
    review.save_label(conn, ids[0], False)  # corregir una respuesta
    assert stored_labels(tmp_path)[ids[0]] == 0
    stamp = conn.execute("SELECT labeled_at FROM labels WHERE id16 = ?", (ids[0],)).fetchone()[0]
    assert datetime.fromisoformat(stamp).utcoffset().total_seconds() == 0


def test_label_counts(conn):
    assert review.label_counts(conn) == {"total": 0, "yes": 0, "no": 0}
    for n, answer in ((1, True), (2, True), (3, False)):
        review.save_label(conn, add_detailed(conn, n, 70), answer)
    assert review.label_counts(conn) == {"total": 3, "yes": 2, "no": 1}


@pytest.mark.parametrize("text, expected", [
    ("palabra " * 200, "palabra " * 99 + "palabra…"),  # el corte cae justo entre palabras
    ("x" * 790 + " abcdefghijklmnop " * 5, "x" * 790 + "…"),  # el corte cae dentro de una palabra: la descarta
    ("y" * 900, "y" * 800 + "…"),  # una sola palabra larga: corta igual
    ("z " * 400, "z " * 399 + "z"),  # 800 caracteres justos (el último espacio se recorta): no se corta
    ("Corta.", "Corta."),
])
def test_label_session_cuts_the_description_at_800_characters_on_a_space(conn, text, expected):
    add_detailed(conn, 1, 70, description=text)
    _, lines, _ = session(conn, 30, "q")
    assert lines[2] == expected and len(lines[2]) <= 801


# ── db_labels y threshold desde la base ──────────────────────────────────────

def put_label(conn, id16: str, label: int) -> None:
    conn.execute("INSERT INTO labels (id16, label, labeled_at) VALUES (?, ?, '2026-01-01T00:00:00+00:00')", (id16, label))
    conn.commit()


def test_db_labels_use_only_the_scores_of_the_model_and_version_the_plan_uses(conn):
    current = add_scored(conn, 1, 88)  # gpt-6-luna v5: la que cuenta
    only_v1 = add_scored(conn, 2, 40)
    conn.execute("UPDATE scores SET prompt_version = 'v1' WHERE id16 = ?", (only_v1,))
    only_other_model = add_scored(conn, 3, 55, model="gpt-4o-mini")
    duplicate = add_scored(conn, 4, 99)
    conn.execute("UPDATE offers SET status = 'duplicate', dup_of = ? WHERE id16 = ?", (current, duplicate))
    add_scored(conn, 5, 99)  # puntuada pero sin etiqueta
    for id16 in (current, only_v1, only_other_model, duplicate):
        put_label(conn, id16, 1)
    put_label(conn, f"{6:016X}", 1)  # etiqueta de una oferta que ya no está: no cuenta
    assert review.db_labels(conn, "gpt-6-luna") == [{"score": 88, "label": True}]
    assert review.labels_without_score(conn, "gpt-6-luna") == 2  # solo v1 y solo otro modelo; la duplicada no cuenta en ninguna
    assert review.db_labels(conn, "gpt-4o-mini") == [{"score": 55, "label": True}]
    assert review.labels_without_score(conn, "gpt-4o-mini") == 2


def test_unscored_note_only_when_there_are_labels_without_score(conn):
    assert review.unscored_note(conn, "gpt-6-luna") is None
    put_label(conn, add_scored(conn, 1, 60, model="gpt-4o-mini"), 1)
    put_label(conn, add_scored(conn, 2, 60), 0)
    assert review.unscored_note(conn, "gpt-6-luna") == (
        "1 etiquetas sin puntaje de gpt-6-luna v5: no cuentan para el umbral. Corré el puntaje primero.")
    assert review.unscored_note(conn, "gpt-4o-mini") == (
        "1 etiquetas sin puntaje de gpt-4o-mini v5: no cuentan para el umbral. Corré el puntaje primero.")


def test_export_csv_only_has_current_scores_of_the_model_and_no_duplicates(conn, tmp_path):
    keep = add_scored(conn, 1, 80)
    old = add_scored(conn, 2, 70)
    conn.execute("UPDATE scores SET prompt_version = 'v1' WHERE id16 = ?", (old,))
    add_scored(conn, 3, 60, model="gpt-4o-mini")
    dup = add_scored(conn, 4, 90)
    conn.execute("UPDATE offers SET status = 'duplicate', dup_of = ? WHERE id16 = ?", (keep, dup))
    out = tmp_path / "review.csv"
    assert review.export_csv(conn, out, "gpt-6-luna") == (1, 0)
    assert [r["id16"] for r in read_rows(out)] == [f"ct-{keep}"]
    assert review.export_csv(conn, out, "gpt-4o-mini") == (1, 0)


def test_cli_threshold_from_the_database_says_when_labels_have_no_score_of_the_model(conn, tmp_path, capsys):
    hand_labeled_db(conn, HAND_ROWS)
    put_label(conn, add_scored(conn, 30, 60, model="gpt-4o-mini"), 1)
    assert cli.main(["review", "threshold", "--db", str(tmp_path / "t.db")]) == 0
    out = capsys.readouterr().out
    first, _, rest = out.partition(chr(10))
    assert first == "1 etiquetas sin puntaje de gpt-6-luna v5: no cuentan para el umbral. Corré el puntaje primero."
    assert rest.startswith("30 · enviaría: 20") and "Umbral sugerido: 75" in rest


def hand_labeled_db(conn, rows):
    for n, (points, label) in enumerate(rows, 1):
        put_label(conn, add_scored(conn, n, points), int(label))


def test_cli_threshold_from_the_database_matches_the_equivalent_csv(conn, tmp_path, capsys):
    hand_labeled_db(conn, HAND_ROWS)
    csv_path = tmp_path / "r.csv"
    write_labeled_csv(csv_path, [(points, "s" if label else "n") for points, label in HAND_ROWS])
    assert cli.main(["review", "threshold", "--csv", str(csv_path)]) == 0
    from_csv = capsys.readouterr()
    assert cli.main(["review", "threshold", "--db", str(tmp_path / "t.db")]) == 0
    from_db = capsys.readouterr()
    assert from_db.out == from_csv.out and from_db.err == from_csv.err == ""
    lines = from_db.out.strip().splitlines()
    assert lines[9] == "75 · enviaría: 10 · precisión: 80% · cobertura: 80%"
    assert lines[-1] == "Umbral sugerido: 75 (precisión 80%, cobertura 80%)"


def test_cli_threshold_from_the_database_needs_20_labels(conn, tmp_path, capsys):
    hand_labeled_db(conn, HAND_ROWS[:19])
    assert cli.main(["review", "threshold", "--db", str(tmp_path / "t.db")]) == 1
    captured = capsys.readouterr()
    assert "Etiquetá al menos 20 ofertas (hay 19)" in captured.err and captured.out == ""
    put_label(conn, add_scored(conn, 20, 60), 0)
    assert cli.main(["review", "threshold", "--db", str(tmp_path / "t.db")]) == 0


def test_cli_threshold_without_csv_does_not_read_the_default_csv(conn, tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Path("data").mkdir()
    write_labeled_csv(Path("data/review.csv"), [(80, "s")] * 20)  # el CSV de siempre existe, pero sin --csv no se lee
    assert cli.main(["review", "threshold", "--db", str(tmp_path / "t.db")]) == 1
    assert "hay 0" in capsys.readouterr().err


# ── CLI: review label ────────────────────────────────────────────────────────

def test_cli_review_label_reads_stdin_and_honors_limit(conn, tmp_path, capsys, monkeypatch):
    for n in range(1, 5):
        add_detailed(conn, n, 87)
    db_path = str(tmp_path / "t.db")
    monkeypatch.setattr("sys.stdin", io.StringIO("s\nn\nq\n"))
    assert cli.main(["review", "label", "--db", db_path, "--profile", str(tmp_path / "sin-perfil.yaml")]) == 0
    out = capsys.readouterr().out
    assert "Etiquetadas en esta sesión: 2 (s: 1 · n: 1) · total en la base: 2" in out
    assert "87" not in out and "SECRETO" not in out and review.LABEL_PROMPT in out
    monkeypatch.setattr("sys.stdin", io.StringIO("s\ns\ns\n"))
    assert cli.main(["review", "label", "--limit", "1", "--db", db_path, "--profile", str(tmp_path / "sin-perfil.yaml")]) == 0
    out = capsys.readouterr().out
    assert "[1/1]" in out and "Etiquetadas en esta sesión: 1 (s: 1 · n: 0) · total en la base: 3" in out
    monkeypatch.setattr("sys.stdin", io.StringIO(""))  # fin de entrada
    assert cli.main(["review", "label", "--db", db_path, "--profile", str(tmp_path / "sin-perfil.yaml")]) == 0
    assert "Etiquetadas en esta sesión: 0" in capsys.readouterr().out


def test_export_shows_the_unmet_required_only_when_there_are(conn, tmp_path):
    with_list, without = add_scored(conn, 1, 70), add_scored(conn, 2, 60)
    conn.execute("UPDATE scores SET unmet_required = ? WHERE id16 = ?", ('["2 años de experiencia web", "Node.js"]', with_list))
    conn.execute("UPDATE scores SET unmet_required = NULL WHERE id16 = ?", (without,))  # puntaje de una base vieja
    out = tmp_path / "r.csv"
    assert review.export_csv(conn, out, "gpt-6-luna") == (2, 0)
    rows = {r["id16"]: r for r in read_rows(out)}
    assert rows[f"ct-{with_list}"]["excluyentes"] == "Requisitos que el CV no muestra: 2 años de experiencia web | Node.js"
    assert rows[f"ct-{without}"]["excluyentes"] == ""
