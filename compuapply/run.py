"""Corrida completa en modo seco: scan de las keywords del perfil, detalles, puntaje y plan de apply."""
import random
import sqlite3
import time
from datetime import date, datetime, timezone
from pathlib import Path

from compuapply import apply, db, scan, score
from compuapply.profile import DEFAULT_RUN, in_zone

NO_KEY = "Falta la clave de OpenAI: cargala en la pestaña Estado (o con python -m compuapply setup env)"
NO_SCORE = "Puntaje: no se pidió (score_limit 0)"


def keywords(profile: dict) -> list[str]:
    """Keywords de todas las variantes, sin repetir (se comparan por slug), en el orden en que aparecen."""
    seen, found = set(), []
    for variant in profile["variants"]:
        for keyword in variant["keywords"]:
            key = scan.slug(keyword)
            if key and key not in seen:
                seen.add(key)
                found.append(keyword)
    if not found:
        raise RuntimeError(
            "Ninguna variante de profile.yaml tiene 'keywords': poné al menos una para que `run` sepa qué buscar."
        )
    return found


def _count(conn: sqlite3.Connection, where: str) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM offers WHERE {where}").fetchone()[0]


def _pending_details(conn: sqlite3.Connection, zones) -> int:
    """Ofertas nuevas que siguen sin detalle, sin contar las de otra zona (a esas no se les baja)."""
    return sum(1 for (location,) in conn.execute("SELECT location FROM offers WHERE detail_fetched_at IS NULL AND status = 'new'")
               if in_zone(location, zones))


def run_all(conn: sqlite3.Connection, profile: dict, http_client, llm_client, model: str, pages: int,
            max_details: int, score_limit: int, max_cost: float, today: date,
            stop_file: Path = Path("STOP"), today_utc: date | None = None) -> dict:
    """Corre las etapas en orden y devuelve lo hecho; no escribe archivos.

    Claves: `scan` (una fila por keyword), `details` (`ran`, `saved`, `bad_url`, `no_detail`, `pending`: ofertas que siguen
    sin detalle), `score` y `plan`
    (None si no llegaron a correr), `score_requested` (`score_limit > 0`), `month` (`spent`, `cap`, `reached`: el gasto
    del mes y su tope, `profile["run"]["max_cost_month"]`; None si no se llegó a puntuar) y `stopped` (None, o `stage`
    y `message`; en `score` suma `scored` y `cost_usd`, lo que alcanzó a guardarse). El presupuesto de puntaje es el
    menor entre `max_cost` y lo que queda del tope mensual (el mes de `today_utc`, hoy en UTC); si no queda nada no se
    llama a la API (`reached`, `score` None) y el plan se arma igual. Con `score_limit` 0 tampoco se llama a la API:
    `score` queda None (solo se reaplican las exclusiones) y el plan sale de los puntajes que ya hay.
    `llm_client` puede ser None (no hay clave): solo hace falta si la etapa de puntaje va a llamar a la API (`score_limit`
    mayor que 0 y presupuesto del mes); si hace falta y falta, la etapa frena con `NO_KEY` después de scan y detalles.
    Si una etapa frena con RuntimeError no se corren las siguientes. Un perfil sin keywords lanza RuntimeError antes
    de pedir nada.
    """
    keyword_list = keywords(profile)
    result = {"scan": [], "details": {"ran": False, "saved": 0, "bad_url": 0, "no_detail": 0, "pending": 0}, "score": None,
              "score_requested": score_limit > 0, "month": None, "plan": None, "stopped": None}

    try:
        for i, keyword in enumerate(keyword_list):
            if i:
                time.sleep(random.uniform(0.7, 1.5))
            scan._check_stop(stop_file, f"la keyword «{keyword}»")
            offers, pages_read = scan.collect(keyword, pages, client=http_client, stop_file=stop_file,
                                               zones=profile.get("zones"))
            new, seen = db.upsert_offers(conn, offers, keyword)
            result["scan"].append({"keyword": keyword, "pages_read": pages_read, "found": len(offers),
                                   "new": new, "seen": seen})
    except RuntimeError as e:
        result["stopped"] = {"stage": "scan", "message": str(e)}
        return result

    details = result["details"]
    details["ran"] = True
    saved_before, bad_before = _count(conn, "detail_fetched_at IS NOT NULL"), _count(conn, "status = 'bad_url'")
    missing_before = _count(conn, "status = 'no_detail'")
    error = None
    try:
        scan.fetch_details(conn, max_details, client=http_client, stop_file=stop_file, zones=profile.get("zones"))
    except RuntimeError as e:
        error = e
    details["saved"] = _count(conn, "detail_fetched_at IS NOT NULL") - saved_before
    details["bad_url"] = _count(conn, "status = 'bad_url'") - bad_before
    details["no_detail"] = _count(conn, "status = 'no_detail'") - missing_before
    details["pending"] = _pending_details(conn, profile.get("zones"))
    if error:
        result["stopped"] = {"stage": "details", "message": str(error)}
        return result

    today_utc = today_utc or datetime.now(timezone.utc).date()
    cap = profile.get("run", {}).get("max_cost_month", DEFAULT_RUN["max_cost_month"])
    spent = score.month_spent(conn, today_utc)
    if cap - spent <= 0:  # sin llamar a la API: el plan se arma con lo ya puntuado
        result["month"] = {"spent": spent, "cap": cap, "reached": True}
        score.refresh_exclusions(conn, profile)  # sin costo: el plan no lista una oferta de una empresa excluida después
        score.refresh_duplicates(conn)  # ni una repetida
    else:
        started = datetime.now(timezone.utc).isoformat()
        try:
            if score_limit > 0 and llm_client is None:
                raise RuntimeError(NO_KEY)
            points = score.score_pending(conn, profile, llm_client, model, score_limit,
                                         min(max_cost, cap - spent), stop_file=stop_file)
            result["score"] = points if score_limit > 0 else None  # con 0 solo reaplicó las exclusiones: no hay puntaje
        except RuntimeError as e:
            scored, cost = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(cost_usd), 0) FROM scores "
                "WHERE scored_at >= ? AND model = ? AND prompt_version = ?",
                (started, model, score.PROMPT_VERSION)).fetchone()
            result["stopped"] = {"stage": "score", "message": str(e), "scored": scored, "cost_usd": cost}
        result["month"] = {"spent": score.month_spent(conn, today_utc), "cap": cap, "reached": False}
        if result["stopped"]:
            return result

    result["plan"] = apply.plan(conn, profile, model, today)
    return result


def _month_label(month: dict) -> str:
    return "Tope mensual alcanzado" if month["reached"] else "Gasto del mes"


def duplicates_part(points: dict) -> str:
    """« · duplicadas: D» para las líneas de totales de `score` y `run`; vacío si D es 0."""
    return f" · duplicadas: {points['duplicates']}" if points["duplicates"] else ""


def summary_lines(result: dict) -> list[str]:
    """Una línea de terminal por etapa que corrió, con los mismos totales del informe."""
    rows = result["scan"]
    lines = [
        f"Scan: keywords: {len(rows)} · páginas leídas: {sum(r['pages_read'] for r in rows)} "
        f"· encontradas: {sum(r['found'] for r in rows)} · nuevas: {sum(r['new'] for r in rows)} "
        f"· ya vistas: {sum(r['seen'] for r in rows)}"
    ]
    details = result["details"]
    if details["ran"]:
        bad = f" · URL inválida: {details['bad_url']}" if details["bad_url"] else ""
        missing = f" · sin descripción: {details['no_detail']}" if details["no_detail"] else ""
        waiting = f" · sin detalle todavía: {details['pending']}" if details["pending"] else ""
        lines.append(f"Detalles: guardados: {details['saved']}{bad}{missing}{waiting}")
    points, month = result["score"], result["month"]
    if points:
        cut = " · cortó por tope de gasto" if points["stopped_by_cost"] else ""
        lines.append(
            f"Puntaje: puntuadas: {points['scored']} · excluidas: {points['excluded']}{duplicates_part(points)} · con error: {points['errors']} "
            f"· costo: {score.money(points['cost_usd'])} · tokens: entrada {points['input_tokens']} / salida "
            f"{points['output_tokens']}{cut}"
        )
    elif month and not result["score_requested"]:
        lines.append(NO_SCORE)
    if month:
        lines.append(f"{_month_label(month)}: {score.amounts(month['spent'], month['cap'])}")
    if result["plan"]:
        lines.append(f"Plan (modo seco): {apply.summary(result['plan'])}")
        lines += apply.plan_notes(result["plan"])
    return lines


def write_report(result: dict, meta: dict, out_dir: Path = Path("data/runs")) -> Path:
    """Escribe el informe en `out_dir/AAAAMMDD-HHMMSS-run.md` y devuelve su ruta.

    `meta`: now (datetime), model, threshold, daily_cap, applied_today. No incluye datos del CV ni la clave.
    """
    now, stopped, details, points, month = (
        meta["now"], result["stopped"], result["details"], result["score"], result["month"])
    state = "completa" if not stopped else f"frenó en la etapa {stopped['stage']} (ver «Freno»)"
    lines = [
        "# Informe de run (modo seco)",
        "",
        f"- Fecha: {now:%Y-%m-%d %H:%M:%S}",
        "- Modo: seco (no se envió ni se cambió nada en Computrabajo)",
        f"- Modelo: {meta['model']}",
        f"- Umbral: {meta['threshold']}",
        f"- Tope diario: {meta['daily_cap']} (ya postuladas hoy: {meta['applied_today']})",
        *apply.company_cap_lines(meta),
        f"- Estado: {state}",
        "",
        "## Scan",
        "",
        "| keyword | páginas leídas | encontradas | nuevas | ya vistas |",
        "|---|---|---|---|---|",
        *[f"| {apply.cell(r['keyword'])} | {r['pages_read']} | {r['found']} | {r['new']} | {r['seen']} |"
          for r in result["scan"]],
    ]
    if not result["scan"]:
        lines += ["", "_Ninguna keyword llegó a leerse._"]
    lines += ["", "## Detalles", ""]
    if details["ran"]:
        missing = f" · sin descripción: {details['no_detail']}" if details["no_detail"] else ""
        lines += [f"- Guardados: {details['saved']}", f"- URL inválida: {details['bad_url']}{missing}",
                  *([f"- Sin detalle todavía: {details['pending']}"] if details["pending"] else [])]
    else:
        lines.append("No se corrió: la corrida frenó antes.")
    lines += ["", "## Puntaje", ""]
    if points:
        lines += [
            f"- Puntuadas: {points['scored']}",
            f"- Excluidas: {points['excluded']}",
            *([f"- Duplicadas: {points['duplicates']}"] if points["duplicates"] else []),
            f"- Con error: {points['errors']}",
            f"- Costo: {score.money(points['cost_usd'])} (estimado con la tabla de precios del programa)",
            f"- Tokens: entrada {points['input_tokens']} / salida {points['output_tokens']}",
            f"- Cortó por tope de gasto: {'sí' if points['stopped_by_cost'] else 'no'}",
        ]
    elif stopped and stopped["stage"] == "score":
        lines.append(f"No terminó. Puntuadas y guardadas antes del freno: {stopped['scored']} "
                     f"(costo de esas: {score.money(stopped['cost_usd'])}).")
    elif not month:
        lines.append("No se corrió: la corrida frenó antes.")
    elif not result["score_requested"]:
        lines.append(f"- {NO_SCORE}")
    if month:
        lines.append(f"- {_month_label(month)}: {score.amounts(month['spent'], month['cap'])}")
    lines += ["", "## Plan (modo seco)", ""]
    if result["plan"]:
        lines += [apply.NOTE, "", *apply.plan_sections(result["plan"], level=3)]
    else:
        lines.append("No se armó: la corrida frenó antes.")
    if stopped:
        lines += ["", "## Freno", "", f"- Etapa: {stopped['stage']}", f"- Mensaje: {apply.cell(stopped['message'])}"]
    lines.append("")
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{now:%Y%m%d-%H%M%S}-run.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
