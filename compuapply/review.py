"""Etiquetado de ofertas puntuadas (en la terminal o por CSV) y cálculo del umbral de puntaje que conviene."""
import csv
import json
import random
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from compuapply.profile import in_zone
from compuapply.score import PROMPT_VERSION

# Con letras al frente Excel no toma un id como `1234567E89012345` por un número en notación científica.
ID_PREFIX = "ct-"
COLUMNS =["id16", "score", "variante", "titulo", "empresa", "ubicacion", "url", "razones", "faltantes", "postularia", "excluyentes"]
THRESHOLDS = range(30, 100, 5)
MIN_LABELS = 20
MIN_PRECISION = 80
EXCERPT_CHARS = 800
LABEL_PROMPT = "¿Te postularías? [s/n · Enter = saltar · q = salir]: "


def _read_csv(path: Path, required: tuple[str, ...]) -> list[dict]:
    """Filas del CSV (`;`, UTF-8 con o sin BOM). Falla con un mensaje claro si no se puede usar."""
    try:
        with path.open(encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f, delimiter=";")
            rows = list(reader)
            fields = reader.fieldnames or []
    except UnicodeDecodeError:
        raise RuntimeError(
            f"El CSV {path} no está en UTF-8 (¿Excel lo guardó como «CSV» a secas?). Guardalo como «CSV UTF-8»."
        ) from None
    missing = [c for c in required if c not in fields]
    if missing:
        raise RuntimeError(
            f"Al CSV {path} le faltan las columnas {', '.join(missing)} (o no está separado por «;»). "
            "Si lo editaste, revisá el encabezado; si no, volvé a exportarlo."
        )
    return rows


def _split_by_id(rows: list[dict]) -> tuple[list[tuple[str, dict]], int]:
    """Separa las filas con id válido (`ct-<id16>` o `<id16>`, 16 hex) de las inválidas, que solo se cuentan."""
    valid, invalid = [], 0
    for row in rows:
        text = (row.get("id16") or "").strip()
        if text[:len(ID_PREFIX)].lower() == ID_PREFIX:
            text = text[len(ID_PREFIX):]
        if re.fullmatch(r"[0-9A-Fa-f]{16}", text):
            valid.append((text, row))
        else:
            invalid += 1
    return valid, invalid


def count_invalid_ids(path: Path) -> int:
    """Cuántas filas del CSV tienen un id inválido (0 si el archivo no existe)."""
    return _split_by_id(_read_csv(path, ("id16",)))[1] if path.exists() else 0


def _safe(text: str | None) -> str:
    """Evita que Excel tome como fórmula un texto de la oferta que empieza con = + - @."""
    text = text or ""
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def export_csv(conn: sqlite3.Connection, out: Path, model: str) -> tuple[int, int]:
    """Escribe una fila por oferta puntuada con `model` y el `PROMPT_VERSION` actual (sin duplicadas), de mayor a menor.

    Si `out` ya existe, conserva las etiquetas `postularia` ya cargadas (por id16, con o sin el prefijo
    `ct-`; las filas con id inválido se ignoran). La columna `id16` se escribe como `ct-<id16>`.
    Devuelve (filas totales, etiquetas conservadas).
    """
    previous = {}
    if out.exists():
        for id16, row in _split_by_id(_read_csv(out, ("id16", "postularia")))[0]:
            label = (row.get("postularia") or "").strip()
            if label:
                previous[id16] = label
    rows = sorted(conn.execute(
        "SELECT s.id16, s.score, s.best_variant, o.title, o.company, o.location, o.url, s.reasons, s.missing, s.unmet_required "
        "FROM scores s JOIN offers o ON o.id16 = s.id16 WHERE s.model = ? AND s.prompt_version = ? "
        "AND o.status != 'duplicate' ORDER BY s.scored_at DESC, s.rowid DESC", (model, PROMPT_VERSION)).fetchall(),
        key=lambda r: -r[1])
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow(COLUMNS)
        for id16, points, variant, title, company, location, url, reasons, missing, unmet in rows:
            unmet_list = json.loads(unmet or "[]")
            writer.writerow([
                ID_PREFIX + id16, points, variant, _safe(title), _safe(company), _safe(location), _safe(url),
                _safe(" | ".join(json.loads(reasons))), _safe(" | ".join(json.loads(missing))), previous.get(id16, ""),
                _safe("Requisitos que el CV no muestra: " + " | ".join(unmet_list) if unmet_list else ""),
            ])
    return len(rows), sum(1 for row in rows if row[0] in previous)


def read_labels(path: Path) -> list[dict]:
    """Filas etiquetadas del CSV: `{score: int, label: bool}` (s = True, n = False; el resto se ignora).

    Las filas con id inválido se ignoran (`count_invalid_ids` las cuenta).
    """
    if not path.is_file():
        raise RuntimeError(f"No existe {path}. Corré primero «review export» y etiquetá la columna postularia.")
    labeled = []
    for id16, row in _split_by_id(_read_csv(path, ("id16", "score", "postularia")))[0]:
        label = (row.get("postularia") or "").strip().lower()
        if label not in ("s", "n"):
            continue
        try:
            points = int(row["score"])
        except (TypeError, ValueError):
            raise RuntimeError(f"La fila {id16} de {path} tiene un score que no es un número.") from None
        labeled.append({"score": points, "label": label == "s"})
    return labeled


def db_labels(conn: sqlite3.Connection, model: str, zones=()) -> list[dict]:
    """Ofertas etiquetadas en la base: `{score: int, label: bool}`, con el puntaje de `model` y el `PROMPT_VERSION` actual.

    Son los mismos puntajes con los que decide `apply.plan`; las duplicadas y las etiquetas sin ese puntaje no cuentan.
    Con `zones` solo cuentan las etiquetas de ofertas en esas provincias (las demás siguen en la base, no se borran).
    """
    return [{"score": points, "label": bool(label)} for points, label, location in conn.execute(
        "SELECT s.score, l.label, o.location FROM labels l JOIN offers o ON o.id16 = l.id16 "
        "JOIN scores s ON s.id16 = l.id16 AND s.model = ? AND s.prompt_version = ? "
        "WHERE o.status != 'duplicate' ORDER BY l.labeled_at, l.rowid", (model, PROMPT_VERSION)) if in_zone(location, zones)]


def labels_without_score(conn: sqlite3.Connection, model: str) -> int:
    """Etiquetas de ofertas no duplicadas que no tienen puntaje de `model` y el `PROMPT_VERSION` actual."""
    return conn.execute(
        "SELECT COUNT(*) FROM labels l JOIN offers o ON o.id16 = l.id16 WHERE o.status != 'duplicate' "
        "AND NOT EXISTS (SELECT 1 FROM scores s WHERE s.id16 = l.id16 AND s.model = ? AND s.prompt_version = ?)",
        (model, PROMPT_VERSION)).fetchone()[0]


def unscored_note(conn: sqlite3.Connection, model: str) -> str | None:
    """Aviso de Calibrar y `review threshold` si hay etiquetas sin puntaje de `model` (si no, None)."""
    n = labels_without_score(conn, model)
    return (f"{n} etiquetas sin puntaje de {model} {PROMPT_VERSION}: no cuentan para el umbral. Corré el puntaje primero."
            if n else None)


def _excerpt(text: str | None) -> str:
    """Los primeros 800 caracteres de la descripción, cortando en un espacio y cerrando con `…` si se cortó."""
    text = (text or "").strip()
    if len(text) <= EXCERPT_CHARS:
        return text
    cut = text[:EXCERPT_CHARS]
    if not (cut[-1].isspace() or text[EXCERPT_CHARS].isspace()) and len(cut.split()) > 1:
        cut = cut.rsplit(None, 1)[0]
    return cut.rstrip() + "…"


def label_candidates(conn: sqlite3.Connection, limit: int, rng: random.Random | None = None, zones=()) -> list[dict]:
    """Hasta `limit` ofertas puntuadas sin etiquetar, en orden aleatorio, con lo único que se le muestra al usuario.

    Claves: id16, title, company, location, employment_type, excerpt (800 caracteres de la descripción) y url.
    Nunca el puntaje, la variante, las razones ni los faltantes. Con `zones` solo ofrece las de esas provincias.
    """
    rows = conn.execute(
        "SELECT o.id16, o.title, o.company, o.location, o.employment_type, o.description, o.url FROM offers o "
        "WHERE o.status != 'duplicate' AND EXISTS (SELECT 1 FROM scores s WHERE s.id16 = o.id16) "
        "AND NOT EXISTS (SELECT 1 FROM labels l WHERE l.id16 = o.id16) ORDER BY o.id16").fetchall()
    rows = [row for row in rows if in_zone(row[3], zones)]
    (rng or random.Random()).shuffle(rows)
    return [{"id16": id16, "title": title or "", "company": company or "", "location": location or "",
             "employment_type": kind or "", "excerpt": _excerpt(description), "url": url or ""}
            for id16, title, company, location, kind, description, url in rows[:max(limit, 0)]]


def save_label(conn: sqlite3.Connection, id16: str, label: bool) -> None:
    """Guarda la respuesta del usuario a «¿te postularías?» (sí = True) y la confirma al instante."""
    conn.execute("INSERT OR REPLACE INTO labels (id16, label, labeled_at) VALUES (?, ?, ?)",
                 (id16, int(label), datetime.now(timezone.utc).isoformat()))
    conn.commit()


def label_counts(conn: sqlite3.Connection) -> dict:
    """Etiquetas guardadas: `{total, yes, no}`."""
    yes, no = conn.execute("SELECT COALESCE(SUM(label = 1), 0), COALESCE(SUM(label = 0), 0) FROM labels").fetchone()
    return {"total": yes + no, "yes": yes, "no": no}


def label_session(conn: sqlite3.Connection, limit: int, ask=input, out=print, rng: random.Random | None = None, zones=()) -> dict:
    """Pregunta «¿te postularías?» por ofertas puntuadas sin etiquetar (de `zones`, si hay), en orden aleatorio y sin mostrar el puntaje.

    Guarda cada `s`/`n` al instante; Enter salta; `q`, Ctrl+C o fin de entrada terminan. Devuelve
    `{labeled, yes, no, total}` (`total` = etiquetas en la base).
    """
    candidates = label_candidates(conn, limit, rng, zones)
    yes = no = 0
    try:
        for i, offer in enumerate(candidates, 1):
            out(f"\n[{i}/{len(candidates)}] {offer['title'] or '(sin título)'}")
            out(" · ".join(part for part in (offer["company"], offer["location"], offer["employment_type"]) if part))
            out(offer["excerpt"])
            out(offer["url"])
            while (answer := ask(LABEL_PROMPT).strip().lower()) not in ("s", "n", "", "q"):
                pass
            if answer == "q":
                break
            if answer:
                save_label(conn, offer["id16"], answer == "s")
                yes, no = yes + (answer == "s"), no + (answer == "n")
    except (KeyboardInterrupt, EOFError):
        pass
    total = label_counts(conn)["total"]
    out(f"\nEtiquetadas en esta sesión: {yes + no} (s: {yes} · n: {no}) · total en la base: {total}")
    if total >= MIN_LABELS:
        out("Ya podés correr: python -m compuapply review threshold")
    return {"labeled": yes + no, "yes": yes, "no": no, "total": total}


def threshold_table(rows: list[dict]) -> tuple[list[dict], int | None]:
    """Precisión y cobertura (en %) por umbral de 30 a 95, y el umbral más bajo con precisión >= 80%.

    `precision` es None si ese umbral no enviaría nada; `recall`, si ninguna oferta es `s`.
    """
    positives = sum(1 for r in rows if r["label"])
    table = []
    for threshold in THRESHOLDS:
        sent = [r for r in rows if r["score"] >= threshold]
        hits = sum(1 for r in sent if r["label"])
        table.append({
            "threshold": threshold,
            "send": len(sent),
            "precision": 100 * hits / len(sent) if sent else None,
            "recall": 100 * hits / positives if positives else None,
        })
    suggested = next((r["threshold"] for r in table if r["precision"] is not None and r["precision"] >= MIN_PRECISION), None)
    return table, suggested
