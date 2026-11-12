"""Persistencia de ofertas en SQLite."""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS offers (
    id16 TEXT PRIMARY KEY,
    data_id TEXT NOT NULL,
    title TEXT,
    company TEXT,
    location TEXT,
    url TEXT,
    keyword TEXT,
    description TEXT,
    status TEXT NOT NULL DEFAULT 'new',
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL
)
"""


SCORES_SCHEMA = """
CREATE TABLE IF NOT EXISTS scores (
    id16 TEXT,
    model TEXT,
    prompt_version TEXT,
    best_variant TEXT,
    score INTEGER,
    all_scores TEXT,
    reasons TEXT,
    missing TEXT,
    input_tokens INTEGER,
    cached_tokens INTEGER,
    output_tokens INTEGER,
    cost_usd REAL,
    scored_at TEXT,
    unmet_required TEXT,
    PRIMARY KEY (id16, model, prompt_version)
)
"""

# Postulaciones hechas. En v1 solo se lee (modo seco): la escribe la Fase 4.
APPLICATIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS applications (
    id16 TEXT PRIMARY KEY,
    variant_id TEXT,
    score INTEGER,
    status TEXT NOT NULL,
    reason TEXT,
    applied_at TEXT
)
"""

# Respuestas del usuario a «¿te postularías?» (1 = s, 0 = n), para calibrar el umbral.
LABELS_SCHEMA = """
CREATE TABLE IF NOT EXISTS labels (
    id16 TEXT PRIMARY KEY,
    label INTEGER NOT NULL,
    labeled_at TEXT NOT NULL
)
"""

# Preguntas de selección que el programa no pudo contestar con el CV ni con `answers`: el usuario las responde en la ventana.
PENDING_QUESTIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS pending_questions (
    id16 TEXT,
    question TEXT,
    options TEXT,
    first_seen TEXT,
    PRIMARY KEY (id16, question)
)
"""
ANSWER_PROMPT_VERSION = "a15"  # la versión de las instrucciones con que se contestan las preguntas; se guarda con cada pendiente

# CV a medida de una oferta para revisar (`cv make`): rutas de los archivos y lo que costó pedirlo.
CVS_SCHEMA = """
CREATE TABLE IF NOT EXISTS cvs (
    id16 TEXT,
    variant TEXT,
    model TEXT,
    prompt_version TEXT,
    html TEXT,
    pdf TEXT,
    created_at TEXT,
    cost_usd REAL,
    input_tokens INTEGER,
    output_tokens INTEGER,
    PRIMARY KEY (id16, variant)
)
"""

# Cada respuesta pagada de la API que no es un puntaje (hoy, los CV por oferta), haya servido (`ok` 1) o no.
API_SPEND_SCHEMA = """
CREATE TABLE IF NOT EXISTS api_spend (
    at TEXT,
    kind TEXT,
    id16 TEXT,
    model TEXT,
    cost_usd REAL,
    input_tokens INTEGER,
    output_tokens INTEGER,
    ok INTEGER
)
"""

# Columnas agregadas después de la primera versión de la tabla: las bases viejas y las nuevas pasan por acá.
# `dup_of`: id16 de la oferta original cuando esta es una duplicada (`status = 'duplicate'`).
EXTRA_COLUMNS = ("date_posted", "valid_through", "employment_type", "detail_fetched_at", "dup_of")


def connect(path: Path = Path("data/compuapply.db")) -> sqlite3.Connection:
    """Abre la base (crea la carpeta y la tabla `offers` si faltan y migra las columnas nuevas)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(SCHEMA)
    conn.execute(SCORES_SCHEMA)
    conn.execute(APPLICATIONS_SCHEMA)
    conn.execute(LABELS_SCHEMA)
    conn.execute(CVS_SCHEMA)
    conn.execute(PENDING_QUESTIONS_SCHEMA)
    have_apps = {row[1] for row in conn.execute("PRAGMA table_info(applications)")}
    # qa: preguntas y respuestas (JSON); cv_file: nombre del PDF subido; im: id de la postulación en postapply; cv_version: `c8`/`c4`
    # local_file: nombre de la copia de cv/postulaciones/ (puede ser distinto del subido); en las filas viejas es igual a cv_file
    for column in ("qa", "cv_file", "im", "cv_version", "local_file"):
        if column not in have_apps:
            conn.execute(f"ALTER TABLE applications ADD COLUMN {column} TEXT")
    conn.execute("UPDATE applications SET local_file = cv_file WHERE (local_file IS NULL OR local_file = '') AND cv_file IS NOT NULL AND cv_file != ''")
    created = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'api_spend'").fetchone() is None
    conn.execute(API_SPEND_SCHEMA)
    if created:  # base anterior a `api_spend`: sus CV ya pagos pasan al gasto una sola vez
        conn.execute("INSERT INTO api_spend (at, kind, id16, model, cost_usd, input_tokens, output_tokens, ok) "
                     "SELECT created_at, 'cv', id16, model, cost_usd, input_tokens, output_tokens, 1 FROM cvs")
    if "answer_version" not in {row[1] for row in conn.execute("PRAGMA table_info(pending_questions)")}:
        conn.execute("ALTER TABLE pending_questions ADD COLUMN answer_version TEXT")  # NULL: de antes de la 057
    if "reason" not in {row[1] for row in conn.execute("PRAGMA table_info(pending_questions)")}:
        conn.execute("ALTER TABLE pending_questions ADD COLUMN reason TEXT")  # por qué quedó pendiente (TAREA-082)
    conn.execute("CREATE TABLE IF NOT EXISTS skipped_questions (key TEXT PRIMARY KEY)")  # «no la contesto»: no se vuelve a preguntar
    if "unmet_required" not in {row[1] for row in conn.execute("PRAGMA table_info(scores)")}:
        conn.execute("ALTER TABLE scores ADD COLUMN unmet_required TEXT")
    have = {row[1] for row in conn.execute("PRAGMA table_info(offers)")}
    for column in EXTRA_COLUMNS:
        if column not in have:
            conn.execute(f"ALTER TABLE offers ADD COLUMN {column} TEXT")
    conn.commit()
    return conn


def save_detail(conn: sqlite3.Connection, id16: str, detail: dict) -> None:
    """Guarda descripción y fechas de una oferta y marca cuándo se bajó el detalle."""
    conn.execute(
        "UPDATE offers SET description = ?, date_posted = ?, valid_through = ?, employment_type = ?, "
        "detail_fetched_at = ? WHERE id16 = ?",
        (detail["description"], detail["date_posted"], detail["valid_through"], detail["employment_type"],
         datetime.now(timezone.utc).isoformat(), id16),
    )
    conn.commit()


def save_score(conn: sqlite3.Connection, id16: str, model: str, prompt_version: str, result: dict,
               tokens: tuple[int, int, int], cost_usd: float) -> None:
    """Guarda el puntaje de una oferta; `tokens` = (entrada, entrada cacheada, salida)."""
    conn.execute(
        "INSERT INTO scores (id16, model, prompt_version, best_variant, score, all_scores, reasons, missing, "
        "input_tokens, cached_tokens, output_tokens, cost_usd, scored_at, unmet_required) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (id16, model, prompt_version, result["best_variant"], result["score"],
         json.dumps(result["all_scores"], ensure_ascii=False), json.dumps(result["reasons"], ensure_ascii=False),
         json.dumps(result["missing"], ensure_ascii=False), *tokens, cost_usd, datetime.now(timezone.utc).isoformat(),
         json.dumps(result.get("unmet_required", []), ensure_ascii=False)),
    )
    conn.commit()


def save_cv(conn: sqlite3.Connection, id16: str, variant: str, model: str, prompt_version: str, html: str, pdf: str,
            cost_usd: float, input_tokens: int, output_tokens: int) -> None:
    """Guarda (o reemplaza) el CV de una oferta y una variante: rutas de los archivos, costo y tokens."""
    conn.execute(
        "INSERT OR REPLACE INTO cvs (id16, variant, model, prompt_version, html, pdf, created_at, cost_usd, input_tokens, "
        "output_tokens) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (id16, variant, model, prompt_version, html, pdf, datetime.now(timezone.utc).isoformat(), cost_usd,
         input_tokens, output_tokens))
    conn.commit()


def add_spend(conn: sqlite3.Connection, kind: str, id16: str, model: str, cost_usd: float, input_tokens: int,
              output_tokens: int, ok: bool) -> None:
    """Anota lo que costó una respuesta de la API (`kind` = 'cv'); `ok` dice si sirvió."""
    conn.execute(
        "INSERT INTO api_spend (at, kind, id16, model, cost_usd, input_tokens, output_tokens, ok) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (datetime.now(timezone.utc).isoformat(), kind, id16, model, cost_usd, input_tokens, output_tokens, int(ok)))
    conn.commit()


def save_application(conn: sqlite3.Connection, id16: str, variant: str, points: int, status: str, reason: str,
                     qa: list[dict], cv_file: str | None = None, im: str | None = None, cv_version: str | None = None,
                     local_file: str | None = None) -> None:
    """Guarda (o reemplaza) la postulación de una oferta: variante, puntaje, resultado (`status`), motivo, sus preguntas y
    respuestas y, si se postuló con un CV a medida, el nombre del archivo subido, el `im` de `postapply` y la versión del CV.
    `local_file` es el nombre de la copia local; sin él es igual al subido."""
    conn.execute(
        "INSERT OR REPLACE INTO applications (id16, variant_id, score, status, reason, applied_at, qa, cv_file, im, cv_version, local_file) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (id16, variant, points, status, reason, datetime.now(timezone.utc).isoformat(), json.dumps(qa, ensure_ascii=False),
         cv_file, im, cv_version, local_file or cv_file))
    conn.commit()


def save_pending_question(conn: sqlite3.Connection, id16: str, question: str, options: list[str],
                          answer_version: str = ANSWER_PROMPT_VERSION, reason: str | None = None) -> None:
    """Anota una pregunta sin respuesta de una oferta (si ya estaba, no cambia su `first_seen`) con la versión de las instrucciones."""
    conn.execute("INSERT OR IGNORE INTO pending_questions (id16, question, options, first_seen, answer_version, reason) VALUES (?, ?, ?, ?, ?, ?)",
                 (id16, question, json.dumps(options, ensure_ascii=False), datetime.now(timezone.utc).isoformat(), answer_version, reason))
    conn.commit()


def mark_status(conn: sqlite3.Connection, id16: str, status: str) -> None:
    """Cambia el `status` de una oferta."""
    conn.execute("UPDATE offers SET status = ? WHERE id16 = ?", (status, id16))
    conn.commit()


def upsert_offers(conn: sqlite3.Connection, offers: list[dict], keyword: str) -> tuple[int, int]:
    """Guarda las ofertas nuevas y marca `last_seen` en las ya vistas; devuelve (nuevas, ya_vistas).

    Una oferta ya vista cuya URL cambió a otra válida actualiza la URL, y si estaba `bad_url` vuelve a `new`.
    """
    from compuapply.scan import DETAIL_PREFIX  # scan importa db: import tardío para no hacer un círculo

    now = datetime.now(timezone.utc).isoformat()
    new = seen = 0
    for o in offers:
        row = conn.execute("SELECT url, status FROM offers WHERE id16 = ?", (o["id16"],)).fetchone()
        if row:
            conn.execute("UPDATE offers SET last_seen = ? WHERE id16 = ?", (now, o["id16"]))
            url, status = row
            if o["url"] != url and o["url"].startswith(DETAIL_PREFIX):
                conn.execute("UPDATE offers SET url = ? WHERE id16 = ?", (o["url"], o["id16"]))
                if status == "bad_url":
                    conn.execute("UPDATE offers SET status = 'new' WHERE id16 = ?", (o["id16"],))
            seen += 1
        else:
            conn.execute(
                "INSERT INTO offers (id16, data_id, title, company, location, url, keyword, first_seen, last_seen) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (o["id16"], o["data_id"], o["title"], o["company"], o["location"], o["url"], keyword, now, now),
            )
            new += 1
    conn.commit()
    return new, seen
