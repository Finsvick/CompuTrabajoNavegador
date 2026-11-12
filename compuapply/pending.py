"""Las preguntas pendientes que solo el usuario sabe: se listan sin repetir y cada respuesta pasa al banco de verdades (TAREA-082)."""
import re
import sqlite3
from pathlib import Path

from . import apply, score, ui_logic

SENSITIVE = re.compile(r"\b(?:cuil|cuit|dni|documento|telefono|celular|whatsapp|pasaporte)\b")
SENSITIVE_NOTE = "contestalo vos en la oferta"
SHORT_MAX = 80
SKIP_MARK = "-"


def is_sensitive(question: str) -> bool:
    """CUIL, DNI, teléfono y parecidos: nunca se guardan; el usuario los contesta en la oferta."""
    return bool(SENSITIVE.search(score._fold(question)))


def groups(conn: sqlite3.Connection) -> list[dict]:
    """Las preguntas pendientes de ofertas en `pending_question`, sin repetir (por pregunta normalizada) y sin las que el usuario marcó «no la
    contesto»: `{key, question, offers, reason, sensitive}`, de la que tiene más ofertas a la que tiene menos."""
    skipped = {row[0] for row in conn.execute("SELECT key FROM skipped_questions")}
    found: dict[str, dict] = {}
    for id16, question, reason in conn.execute("SELECT p.id16, p.question, p.reason FROM pending_questions p JOIN offers o ON o.id16 = p.id16 "
                                               "WHERE o.status = 'pending_question' ORDER BY p.first_seen"):
        key = apply.normalize_question(question)
        if key in skipped:
            continue
        item = found.setdefault(key, {"key": key, "question": question, "offers": set(), "reason": "", "sensitive": is_sensitive(question)})
        item["offers"].add(id16)
        item["reason"] = item["reason"] or (reason or "")
    out = [{**g, "offers": len(g["offers"]),
            "reason": SENSITIVE_NOTE if g["sensitive"] else g["reason"] or "sin respuesta en tu CV, tus respuestas ni tus hechos"}
           for g in found.values()]
    return sorted(out, key=lambda g: (-g["offers"], g["key"]))


def fact_line(question: str, answer: str) -> str:
    """«<pregunta corta>, <respuesta>»: sin «:» (cambiados por «,») para que el YAML del banco no se rompa (D-170)."""
    short = " ".join(question.replace("¿", " ").replace("?", " ").split())
    if len(short) > SHORT_MAX:
        short = short[:SHORT_MAX].rsplit(" ", 1)[0]
    line = f"{short}, {' '.join(answer.split())}"
    return re.sub(r"\s*:\s*", ", ", line)


def requeue(conn: sqlite3.Connection, key: str) -> int:
    """Saca esa pregunta de las pendientes de cada oferta y deja en la cola (`new`) las que ya no tienen otra. Devuelve cuántas ofertas volvieron."""
    back = 0
    for id16, question in conn.execute("SELECT id16, question FROM pending_questions").fetchall():
        if apply.normalize_question(question) != key:
            continue
        conn.execute("DELETE FROM pending_questions WHERE id16 = ? AND question = ?", (id16, question))
        if conn.execute("SELECT 1 FROM pending_questions WHERE id16 = ?", (id16,)).fetchone() is None:
            conn.execute("UPDATE offers SET status = 'new' WHERE id16 = ? AND status = 'pending_question'", (id16,))
            back += 1
    conn.commit()
    return back


def answer(conn: sqlite3.Connection, group: dict, text: str, variant: str, profile_path: Path) -> int:
    """Agrega «<pregunta corta>, <respuesta>» al banco de verdades de la variante y vuelve a encolar las ofertas. Nunca guarda una pregunta de
    CUIL, DNI o teléfono. Devuelve cuántas ofertas volvieron a la cola."""
    if group["sensitive"] or is_sensitive(group["question"]):
        raise RuntimeError(SENSITIVE_NOTE)
    try:
        ui_logic.add_truth(variant, fact_line(group["question"], text), profile_path)
    except RuntimeError as e:
        if "ya está en el banco" not in str(e):
            raise
    return requeue(conn, group["key"])


def skip(conn: sqlite3.Connection, group: dict) -> None:
    """«No la contesto»: no se vuelve a preguntar (las ofertas siguen pendientes)."""
    conn.execute("INSERT OR IGNORE INTO skipped_questions (key) VALUES (?)", (group["key"],))
    conn.commit()


def run(conn: sqlite3.Connection, variant: str, profile_path: Path, ask: bool, input_fn=input, out=print) -> int:
    """`pending`: lista las preguntas pendientes sin repetir (con cuántas ofertas y el motivo); con `ask`, pide la respuesta de cada una
    (Enter salta, «-» la marca «no la contesto»). Devuelve cuántas se contestaron."""
    todo = groups(conn)
    if not todo:
        out("No hay preguntas pendientes.")
        return 0
    answered = 0
    for n, g in enumerate(todo, 1):
        out(f"{n}. {g['question']}  [{g['offers']} oferta{'s' if g['offers'] != 1 else ''}] — {g['reason']}")
        if not ask or g["sensitive"]:
            continue
        text = input_fn("   Respuesta (Enter salta, «-» no la contesto): ").strip()
        if text == SKIP_MARK:
            skip(conn, g)
            out("   Marcada como «no la contesto».")
        elif text:
            back = answer(conn, g, text, variant, profile_path)
            answered += 1
            out(f"   Agregado al banco. Ofertas de vuelta a la cola: {back}.")
    return answered
