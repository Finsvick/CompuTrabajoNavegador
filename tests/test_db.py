"""Tests de db.py (SQLite en carpeta temporal)."""
import sqlite3
from datetime import datetime

import pytest

from compuapply import db

# Esquema de la base que creaba TAREA-001 (sin las columnas del detalle).
OLD_SCHEMA = """
CREATE TABLE offers (
    id16 TEXT PRIMARY KEY, data_id TEXT NOT NULL, title TEXT, company TEXT, location TEXT,
    url TEXT, keyword TEXT, description TEXT, status TEXT NOT NULL DEFAULT 'new',
    first_seen TEXT NOT NULL, last_seen TEXT NOT NULL
)
"""


def offer(n: int) -> dict:
    return {
        "id16": f"{n:016X}", "data_id": f"{n:032X}", "title": f"Oferta {n}",
        "company": "Empresa Falsa SA", "location": "Rosario, Santa Fe",
        "url": f"https://ar.computrabajo.com/ofertas-de-trabajo/oferta-{n}",
    }


def test_connect_creates_folder_and_table(tmp_path):
    conn = db.connect(tmp_path / "data" / "t.db")
    cols = [row[1] for row in conn.execute("PRAGMA table_info(offers)")]
    assert cols == ["id16", "data_id", "title", "company", "location", "url", "keyword",
                    "description", "status", "first_seen", "last_seen",
                    "date_posted", "valid_through", "employment_type", "detail_fetched_at", "dup_of"]
    db.connect(tmp_path / "data" / "t.db").close()  # reabrir no falla


def test_connect_creates_scores_table(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    cols = [r[1] for r in conn.execute("PRAGMA table_info(scores)")]
    assert cols == ["id16", "model", "prompt_version", "best_variant", "score", "all_scores", "reasons", "missing",
                    "input_tokens", "cached_tokens", "output_tokens", "cost_usd", "scored_at", "unmet_required"]
    pk = [r[1] for r in sorted(conn.execute("PRAGMA table_info(scores)"), key=lambda r: r[5]) if r[5]]
    assert pk == ["id16", "model", "prompt_version"]


def test_connect_creates_applications_table(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    info = list(conn.execute("PRAGMA table_info(applications)"))
    assert [r[1] for r in info] == ["id16", "variant_id", "score", "status", "reason", "applied_at", "qa", "cv_file", "im", "cv_version", "local_file"]
    assert [r[1] for r in info if r[5]] == ["id16"]  # clave primaria
    assert [r[1] for r in info if r[3]] == ["status"]  # NOT NULL (id16 no cuenta: lo es por ser PK)
    assert conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0] == 0
    db.connect(tmp_path / "t.db").close()  # reabrir no falla


def test_connect_creates_labels_table(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    info = list(conn.execute("PRAGMA table_info(labels)"))
    assert [(r[1], r[2]) for r in info] == [("id16", "TEXT"), ("label", "INTEGER"), ("labeled_at", "TEXT")]
    assert [r[1] for r in info if r[5]] == ["id16"]  # clave primaria
    assert [r[1] for r in info if r[3]] == ["label", "labeled_at"]  # NOT NULL (id16 no cuenta: lo es por ser PK)
    assert conn.execute("SELECT COUNT(*) FROM labels").fetchone()[0] == 0
    db.connect(tmp_path / "t.db").close()  # reabrir no falla


def test_save_score_stores_json_and_tokens(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    result = {"best_variant": "a", "score": 80, "all_scores": {"a": 80, "b": 40}, "reasons": ["Sí encaja"], "missing": []}
    db.save_score(conn, "X1", "gpt-6-luna", "v1", result, (1000, 400, 200), 0.000164)
    row = conn.execute("SELECT best_variant, score, all_scores, reasons, missing, input_tokens, cached_tokens, "
                       "output_tokens, cost_usd, scored_at FROM scores").fetchone()
    assert row[:9] == ("a", 80, '{"a": 80, "b": 40}', '["Sí encaja"]', "[]", 1000, 400, 200, 0.000164)
    assert datetime.fromisoformat(row[9]).utcoffset().total_seconds() == 0


def test_connect_migrates_old_database(tmp_path):
    path = tmp_path / "viejo.db"
    old = sqlite3.connect(path)
    old.execute(OLD_SCHEMA)
    old.execute("INSERT INTO offers (id16, data_id, title, first_seen, last_seen) VALUES ('A', 'AA', 'Vieja', 'x', 'x')")
    old.commit()
    old.close()
    conn = db.connect(path)
    db.connect(path).close()  # dos veces seguidas no falla
    cols = {row[1] for row in conn.execute("PRAGMA table_info(offers)")}
    assert set(db.EXTRA_COLUMNS) <= cols and "dup_of" in cols  # `dup_of` (TAREA-022) también llega a las bases viejas
    assert conn.execute("SELECT title, detail_fetched_at FROM offers").fetchone() == ("Vieja", None)
    assert conn.execute("SELECT COUNT(*) FROM labels").fetchone()[0] == 0  # la base vieja también recibe la tabla nueva


def test_save_detail_writes_fields_and_timestamp(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.upsert_offers(conn, [offer(1), offer(2)], "programador")
    detail = {"description": "Línea 1\nLínea 2", "date_posted": "2026-09-30",
              "valid_through": "2026-11-29", "employment_type": "FULL_TIME"}
    db.save_detail(conn, offer(1)["id16"], detail)
    row = conn.execute("SELECT description, date_posted, valid_through, employment_type, detail_fetched_at "
                       "FROM offers WHERE id16 = ?", (offer(1)["id16"],)).fetchone()
    assert row[:4] == ("Línea 1\nLínea 2", "2026-09-30", "2026-11-29", "FULL_TIME")
    assert datetime.fromisoformat(row[4]).utcoffset().total_seconds() == 0
    other = conn.execute("SELECT description, detail_fetched_at FROM offers WHERE id16 = ?",
                         (offer(2)["id16"],)).fetchone()
    assert other == (None, None)


def test_upsert_twice_counts_new_then_seen(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    offers = [offer(n) for n in range(20)]
    assert db.upsert_offers(conn, offers, "programador") == (20, 0)
    assert db.upsert_offers(conn, offers, "programador") == (0, 20)
    assert conn.execute("SELECT COUNT(*) FROM offers").fetchone()[0] == 20


def test_new_offer_defaults(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.upsert_offers(conn, [offer(1)], "programador")
    status, keyword, description, first, last = conn.execute(
        "SELECT status, keyword, description, first_seen, last_seen FROM offers").fetchone()
    assert (status, keyword, description) == ("new", "programador", None)
    assert first == last
    assert datetime.fromisoformat(first).utcoffset().total_seconds() == 0


def row(conn, n: int) -> tuple:
    return conn.execute("SELECT url, status, last_seen FROM offers WHERE id16 = ?", (offer(n)["id16"],)).fetchone()


def test_upsert_requeues_bad_url_when_url_gets_fixed(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.upsert_offers(conn, [{**offer(1), "url": ""}], "programador")
    db.mark_status(conn, offer(1)["id16"], "bad_url")
    conn.execute("UPDATE offers SET last_seen = 'viejo'")
    assert db.upsert_offers(conn, [offer(1)], "programador") == (0, 1)
    url, status, last_seen = row(conn, 1)
    assert (url, status) == (offer(1)["url"], "new")
    assert last_seen != "viejo"


@pytest.mark.parametrize("new_url", [
    "",
    "https://candidato.ar.computrabajo.com/candidate/apply/?oi=ABC",
    "http://ar.computrabajo.com/ofertas-de-trabajo/oferta-1",
])
def test_upsert_invalid_new_url_changes_nothing_but_last_seen(tmp_path, new_url):
    conn = db.connect(tmp_path / "t.db")
    db.upsert_offers(conn, [{**offer(1), "url": ""}], "programador")
    db.mark_status(conn, offer(1)["id16"], "bad_url")
    conn.execute("UPDATE offers SET last_seen = 'viejo'")
    assert db.upsert_offers(conn, [{**offer(1), "url": new_url}], "programador") == (0, 1)
    url, status, last_seen = row(conn, 1)
    assert (url, status) == ("", "bad_url")
    assert last_seen != "viejo"


def test_upsert_good_new_url_keeps_other_statuses(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.upsert_offers(conn, [offer(1)], "programador")
    db.mark_status(conn, offer(1)["id16"], "skipped")
    moved = {**offer(1), "url": offer(1)["url"] + "-v2"}
    db.upsert_offers(conn, [moved], "programador")
    assert row(conn, 1)[:2] == (moved["url"], "skipped")  # la URL se actualiza, el status no


def test_existing_offer_only_updates_last_seen(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.upsert_offers(conn, [offer(1)], "programador")
    conn.execute("UPDATE offers SET first_seen = 'viejo', last_seen = 'viejo', status = 'skipped'")
    changed = {**offer(1), "title": "Otro título"}
    assert db.upsert_offers(conn, [changed], "otra") == (0, 1)
    title, keyword, status, first, last = conn.execute(
        "SELECT title, keyword, status, first_seen, last_seen FROM offers").fetchone()
    assert (title, keyword, status, first) == ("Oferta 1", "programador", "skipped", "viejo")
    assert last != "viejo"


def test_connect_creates_api_spend_and_add_spend_stores_one_row_per_response(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    assert [r[1] for r in conn.execute("PRAGMA table_info(api_spend)")] == [
        "at", "kind", "id16", "model", "cost_usd", "input_tokens", "output_tokens", "ok"]
    db.add_spend(conn, "cv", "A1", "gpt-6-luna", 0.5, 10, 5, True)
    db.add_spend(conn, "cv", "A1", "gpt-6-luna", 0.25, 10, 5, False)
    rows = conn.execute("SELECT kind, id16, model, cost_usd, input_tokens, output_tokens, ok FROM api_spend ORDER BY rowid").fetchall()
    assert rows == [("cv", "A1", "gpt-6-luna", 0.5, 10, 5, 1), ("cv", "A1", "gpt-6-luna", 0.25, 10, 5, 0)]
    assert datetime.fromisoformat(conn.execute("SELECT at FROM api_spend").fetchone()[0]).utcoffset().total_seconds() == 0


def test_connect_adds_unmet_required_to_an_old_scores_table(tmp_path):
    path = tmp_path / "viejo.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE scores (id16 TEXT, model TEXT, prompt_version TEXT, best_variant TEXT, score INTEGER, all_scores TEXT, "
                "reasons TEXT, missing TEXT, input_tokens INTEGER, cached_tokens INTEGER, output_tokens INTEGER, cost_usd REAL, "
                "scored_at TEXT, PRIMARY KEY (id16, model, prompt_version))")
    old.execute("INSERT INTO scores VALUES ('A', 'm', 'v3', 'b', 70, '{}', '[]', '[]', 1, 0, 1, 0.1, 'x')")
    old.commit()
    old.close()
    conn = db.connect(path)
    db.connect(path).close()  # dos veces seguidas no falla
    assert [r[1] for r in conn.execute("PRAGMA table_info(scores)")][-1] == "unmet_required"
    assert conn.execute("SELECT score, unmet_required FROM scores").fetchone() == (70, None)  # lo viejo queda sin lista
    db.save_score(conn, "B", "m", "v5", {"best_variant": "b", "score": 60, "all_scores": {"b": 60}, "reasons": [], "missing": [],
                                         "unmet_required": ["Node.js"]}, (1, 0, 1), 0.0)
    db.save_score(conn, "C", "m", "v5", {"best_variant": "b", "score": 60, "all_scores": {"b": 60}, "reasons": [], "missing": []},
                  (1, 0, 1), 0.0)
    assert dict(conn.execute("SELECT id16, unmet_required FROM scores WHERE id16 IN ('B', 'C')")) == {"B": '["Node.js"]', "C": "[]"}
