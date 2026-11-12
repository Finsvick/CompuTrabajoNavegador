"""`tanda`: el usuario elige un CV (variante) y las búsquedas; el programa hace scan, detalles, puntaje y plan solo de las ofertas de esos
listados y, en vivo, postula con ese CV durante N minutos o hasta un tope. Un solo informe."""
import json
import random
import sqlite3
import time
from datetime import date, datetime, timezone
from pathlib import Path

from compuapply import apply, db, scan, score
from compuapply.profile import DEFAULT_RUN, in_zone

DEFAULT_MINUTES = 60
STREAK_LIMIT = 3  # ofertas seguidas que no son de la búsqueda tras las cuales se pasa a la siguiente
SHORT_WORDS = {"del", "los", "las", "con", "para", "por", "una", "uno", "sin", "que"}  # además de las de 1 o 2 letras
ZONE_NAMES = {"capital-federal": "Capital Federal", "buenos-aires-gba": "Buenos Aires-GBA", None: "todo el país"}
NO_KEY = "Falta la clave de OpenAI: hace falta para puntuar las ofertas de la tanda (python -m compuapply setup env)."


def parse_keywords(text: str) -> list[str]:
    """Las búsquedas de una tanda: texto separado por comas, sin vacías ni repetidas (se comparan por slug)."""
    seen, found = set(), []
    for part in (text or "").split(","):
        keyword = " ".join(part.split())
        if keyword and scan.slug(keyword) and scan.slug(keyword) not in seen:
            seen.add(scan.slug(keyword))
            found.append(keyword)
    return found


def search_words(keyword: str) -> list[str]:
    """Las palabras que definen una búsqueda: sin tildes ni mayúsculas y sin palabras cortas («al», «de», «y»)."""
    words = score._fold(keyword).split()
    return [w for w in words if len(w) > 2 and w not in SHORT_WORDS] or words


def is_from_search(title: str, words: list[str]) -> bool:
    """Si el título contiene todas las palabras de la búsqueda (sin tildes ni mayúsculas)."""
    folded = score._fold(title)
    return bool(words) and all(w in folded for w in words)


def _needs_score(conn: sqlite3.Connection, ids: set, model: str, zones) -> int:
    """Cuántas ofertas de la tanda (nuevas, con descripción, en zona) siguen sin puntaje."""
    return sum(1 for id16, location in conn.execute(
        "SELECT o.id16, o.location FROM offers o WHERE o.status = 'new' AND o.description IS NOT NULL AND o.description != '' "
        "AND NOT EXISTS (SELECT 1 FROM scores s WHERE s.id16 = o.id16 AND s.model = ? AND s.prompt_version = ?)",
        (model, score.PROMPT_VERSION)) if id16 in ids and in_zone(location, zones))


def prepare(conn: sqlite3.Connection, profile: dict, http_client, llm_client, model: str, variant: str, keywords: list[str], today: date,
            stop_file: Path = Path("STOP"), pause=lambda: time.sleep(random.uniform(0.7, 1.5))) -> dict:
    """Las tres primeras etapas de la tanda: `scan` de cada búsqueda (con `run.pages`), detalles solo de las ofertas que aparecieron en esos
    listados (nuevas o ya vistas, en zona) y puntaje de las que no tienen (con la variante elegida siempre incluida).

    Devuelve `variant`, `keywords`, `ids` (los `id16` de los listados), `scan` (una fila por búsqueda), `details`, `score` (los totales de
    `score_pending`, o None) y `stopped` (None, o `stage` y `message`)."""
    run = profile["run"]
    if variant not in {v["id"] for v in profile["variants"]}:
        raise RuntimeError(f"La variante «{variant}» no está en profile.yaml (hay: {', '.join(v['id'] for v in profile['variants'])}).")
    if not keywords:
        raise RuntimeError("Falta al menos una búsqueda para la tanda.")
    prep = {"variant": variant, "keywords": keywords, "ids": set(), "scan": [], "details": {"saved": 0, "gone": 0, "no_detail": 0, "bad_url": 0},
            "score": None, "stopped": None}
    zones = profile.get("zones")
    try:
        for i, keyword in enumerate(keywords):
            if i:
                pause()
            scan._check_stop(stop_file, f"la búsqueda «{keyword}»")
            offers, pages_read = scan.collect(keyword, run["pages"], client=http_client, stop_file=stop_file)
            new, seen = db.upsert_offers(conn, offers, keyword)
            prep["ids"] |= {o["id16"] for o in offers}
            prep["scan"].append({"keyword": keyword, "pages_read": pages_read, "found": len(offers), "new": new, "seen": seen})
    except RuntimeError as e:
        prep["stopped"] = {"stage": "scan", "message": str(e)}
        return prep

    def count(status):
        return conn.execute("SELECT COUNT(*) FROM offers WHERE status = ?", (status,)).fetchone()[0]

    before = {s: count(s) for s in ("gone", "no_detail", "bad_url")}
    saved_before = conn.execute("SELECT COUNT(*) FROM offers WHERE detail_fetched_at IS NOT NULL").fetchone()[0]
    error = None
    try:
        scan.fetch_details(conn, run["max_details"], client=http_client, stop_file=stop_file, zones=zones, only=prep["ids"])
    except RuntimeError as e:
        error = e
    details = prep["details"]
    details["saved"] = conn.execute("SELECT COUNT(*) FROM offers WHERE detail_fetched_at IS NOT NULL").fetchone()[0] - saved_before
    for status in before:
        details[status] = count(status) - before[status]
    if error:
        prep["stopped"] = {"stage": "details", "message": str(error)}
        return prep

    cap, spent = run.get("max_cost_month", DEFAULT_RUN["max_cost_month"]), score.month_spent(conn, datetime.now(timezone.utc).date())
    waiting = _needs_score(conn, prep["ids"], model, zones)
    if waiting and cap - spent > 0:
        try:
            if llm_client is None:
                raise RuntimeError(NO_KEY)
            prep["score"] = score.score_pending(conn, profile, llm_client, model, waiting, min(run.get("max_cost", DEFAULT_RUN["max_cost"]), cap - spent),
                                                stop_file=stop_file, only=prep["ids"], extra_variant=variant)
        except RuntimeError as e:
            prep["stopped"] = {"stage": "score", "message": str(e)}
    elif waiting:
        prep["stopped"] = {"stage": "score", "message": f"Tope mensual alcanzado: {score.amounts(spent, cap)}. No se puntúan más ofertas."}
    return prep


def batch_args(prep: dict, max_send: int) -> dict:
    """Lo que `apply.run_live` y `apply.plan` necesitan de la tanda."""
    return {"ids": prep["ids"], "variant": prep["variant"], "max": max_send}


def plan_for(conn: sqlite3.Connection, profile: dict, model: str, today: date, prep: dict, max_send: int) -> dict:
    return apply.plan(conn, profile, model, today, only=prep["ids"], variant=prep["variant"], cap=max_send)


def summary_line(prep: dict, live: dict | None, plan_: dict, minutes_used: float) -> str:
    sent, skipped = (len(live["sent"]), len(live["skipped"])) if live else (0, 0)
    base = f"Tanda con {prep['variant']}: ofertas de los listados: {len(prep['ids'])} · minutos usados: {minutes_used:.1f}"
    if live:
        return f"{base} · enviadas: {sent} · saltadas: {skipped}"
    return f"{base} · {apply.summary(plan_)}"


END_TEXT = {"lista": "se acabó la lista de búsquedas",
            "tiempo": "se acabó el tiempo", "tope": "se llegó al tope de postulaciones", "frenó": "la tanda frenó (ver «Freno»)"}


def _scan_table(rows: list[dict], dry: bool) -> list[str]:
    """La tabla de búsquedas del informe: una fila por búsqueda (y por zona, en la tanda por páginas)."""
    cell = apply.cell
    if not any("zone" in r for r in rows):
        return ["| búsqueda | páginas leídas | encontradas | nuevas | ya vistas |", "|---|---|---|---|---|",
                *[f"| {cell(r['keyword'])} | {r['pages_read']} | {r['found']} | {r['new']} | {r['seen']} |" for r in rows]]
    return [f"| búsqueda | zona | páginas leídas | en zona | de la búsqueda | {'se enviarían' if dry else 'enviadas'} | por qué pasó a la siguiente |",
            "|---|---|---|---|---|---|---|",
            *[f"| {cell(r['keyword'])} | {cell(r['zone'])} | {r['pages_read']} | {r['in_zone']} | {r['matching']} | {r['sent']} | {cell(r['reason'] + (' · ' if r['reason'] and r.get('site_slug') else '') + (f"el sitio usa `{r['site_slug']}`" if r.get('site_slug') else ''))} |"
              for r in rows]]


def write_report(prep: dict, plan_: dict | None, live: dict | None, meta: dict, out_dir: Path = Path("data/runs")) -> Path:
    """El informe de la tanda en `out_dir/AAAAMMDD-HHMMSS-tanda.md`: búsquedas, variante, minutos usados, enviadas, saltadas con su motivo y
    respuestas. `meta`: now, minutes (los pedidos), minutes_used, max, threshold, model, dry. `live` es el resultado de `run_live` (None en seco)."""
    now, cell = meta["now"], apply.cell
    stopped = prep["stopped"]
    state = "completa" if not stopped and not (live and live["halted"]) else "frenó (ver «Freno»)"
    lines = [
        f"# Informe de tanda ({'seco' if meta['dry'] else 'en vivo'})", "",
        f"- Fecha: {now:%Y-%m-%d %H:%M:%S}",
        "- Modo: " + ("seco (no se envió ni se cambió nada en Computrabajo)" if meta["dry"] else "en vivo (se postuló de verdad a las ofertas de «Enviadas»)"),
        f"- CV (variante): {cell(prep['variant'])}",
        f"- Búsquedas: {cell(', '.join(prep['keywords']))}",
        f"- Minutos: usados {meta['minutes_used']:.1f} de {meta['minutes']}" + (" (se acabó el tiempo: no se empezaron más ofertas)" if live and live.get("time_up") else ""),
        "- Tope de la tanda: " + ("sin tope (manda solo el tiempo)" if meta["max"] is None else f"{meta['max']} postulaciones (reemplaza al tope diario)"),
        f"- Umbral: {meta['threshold']}",
        *(apply.company_cap_lines(meta) if meta.get("per_company", True) else
          ["- Tope por empresa: apagado (la tanda no limita postulaciones por empresa; las duplicadas siguen fuera)"]),
        *([f"- Afín por puntaje: {'encendido (también cuenta como de la búsqueda la oferta con puntaje igual o mayor al umbral)' if meta['afin'] else 'apagado'}"]
          if "afin" in meta else []),
        *([f"- Fin: {END_TEXT.get(meta['end'], meta['end'])}"] if meta.get("end") else []),
        f"- Modelo: {meta['model']}",
        f"- Estado: {state}", "",
        "## Búsquedas", "",
        *_scan_table(prep["scan"], meta["dry"]),
    ]
    if not prep["scan"]:
        lines += ["", "_Ninguna búsqueda llegó a leerse._"]
    d = prep["details"]
    lines += ["", f"- Ofertas de los listados: {len(prep['ids'])}",
              f"- Detalles guardados: {d['saved']} · vencidas (301/302): {d['gone']} · sin descripción: {d['no_detail']} · URL inválida: {d['bad_url']}"]
    points = prep["score"]
    if points:
        lines.append(f"- Puntaje: puntuadas {points['scored']} · excluidas {points['excluded']} · con error {points['errors']} · "
                     f"costo {score.money(points['cost_usd'])}")
    if live:
        lines += ["", *apply.account_lines(live), "",
                  f"## Enviadas ({len(live['sent'])})", "", *apply._live_table(live["sent"], True), "",
                  f"## Saltadas en esta corrida ({len(live['skipped'])})", "", *apply._live_table(live["skipped"], False), "",
                  f"## Saltadas por el plan ({len(live['plan']['skip'])})", "", *apply._table(live["plan"]["skip"]),
                  *apply.answer_section(live)]
    elif plan_ is not None:
        lines += ["", *apply.plan_sections(plan_)]
    if stopped:
        lines += ["", "## Freno", "", f"- Etapa: {stopped['stage']}", f"- Mensaje: {cell(stopped['message'])}"]
    if live and live["halted"]:
        lines += ["", "## Freno", "", f"- Mensaje: {cell(live['halted'])}"]
    lines.append("")
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{now:%Y%m%d-%H%M%S}-tanda.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def merge_live(results: list[dict]) -> dict | None:
    """Junta los resultados de `run_live` de cada tramo de la tanda en uno solo (None si no hubo ninguno)."""
    if not results:
        return None
    first = results[0]
    merged = {**first, "sent": [], "skipped": [], "deleted": [], "pending": [], "answers": [], "unconfirmed_now": [], "reconciled": [], "pdfs": dict(first["pdfs"]),
              "plan": {**first["plan"], "send": [], "skip": []}, "requeued": 0, "completed": 0, "cost_usd": 0.0,
              "halted": None, "time_up": False}
    for n, r in enumerate(results):
        for key in ("sent", "skipped", "deleted", "pending", "answers", "unconfirmed_now", "reconciled"):
            merged[key] += r.get(key) or []
        merged["plan"]["send"] += r["plan"]["send"]
        merged["plan"]["skip"] += r["plan"]["skip"]
        merged["requeued"] += r.get("requeued", 0)
        merged["completed"] += r.get("completed", 0)
        merged["cost_usd"] += r.get("cost_usd", 0.0)
        if n:
            merged["pdfs"]["uploaded"] += r["pdfs"]["uploaded"]
            merged["pdfs"]["reused"] = merged["pdfs"].get("reused", 0) + r["pdfs"].get("reused", 0)
        if merged["pdfs"]["before"] is None:
            merged["pdfs"]["before"] = r["pdfs"]["before"]
        for key in ("read", "start_default"):
            if merged.get(key) is None:
                merged[key] = r.get(key)
        merged["restore"] = r.get("restore") or merged.get("restore")
        merged["halted"] = merged["halted"] or r["halted"]
        merged["time_up"] = merged["time_up"] or r["time_up"]
    return merged


def _merge_plans(plans: list[dict]) -> dict | None:
    if not plans:
        return None
    merged = {**plans[-1], "send": [], "skip": []}
    for plan_ in plans:
        merged["send"] += plan_["send"]
        merged["skip"] += plan_["skip"]
    return merged


def walk(conn: sqlite3.Connection, profile: dict, http_client, llm_client, model: str, variant: str, keywords: list[str], today: date,
         opts: dict, live=None, stop_file: Path = Path("STOP"), pause=lambda: time.sleep(random.uniform(0.7, 1.5)),
         clock=time.monotonic, deadline: float | None = None) -> dict:
    """La tanda: toma las búsquedas en el orden en que se escribieron y, para cada una y cada zona de `zones`, lee el listado página por
    página; por cada página baja los detalles de sus ofertas, las puntúa, arma el plan restringido a ellas y postula (`live`) antes de leer
    la siguiente. Pasa a la siguiente búsqueda tras `STREAK_LIMIT` ofertas seguidas, en el orden del listado, que no son de la búsqueda
    (con `opts["afin"]` también cuenta como de la búsqueda la de puntaje igual o mayor al umbral) o cuando no hay más páginas; cada zona
    lleva su racha. Termina con la lista, `deadline` (en la hora de `clock`), `opts["max"]` (None: sin tope) o un freno.

    `opts`: `max`, `afin`, `por_empresa` y, en vivo, `applications` (la lista de «Mis postulaciones» leída una vez al principio, que los
    tramos no vuelven a leer). `live(batch)` corre `apply.run_live` con ese `batch` (`ids`, `variant`, `max`, `deadline`, `clock`,
    `company_cap`, `defer_verify`) y devuelve su resultado; None es una tanda en seco (solo plan).
    Devuelve `prep` (como `prepare`, con `scan` de una fila por búsqueda y zona), `live` (los tramos juntos, o None), `plan` (los planes
    juntos, o None) y `end` («lista», «tiempo», «tope» o «frenó»)."""
    run, zones, threshold = profile["run"], profile.get("zones") or [], profile["apply"]["threshold"]
    if variant not in {v["id"] for v in profile["variants"]}:
        raise RuntimeError(f"La variante «{variant}» no está en profile.yaml (hay: {', '.join(v['id'] for v in profile['variants'])}).")
    if not keywords:
        raise RuntimeError("Falta al menos una búsqueda para la tanda.")
    limit = apply.NO_CAP if opts.get("max") is None else opts["max"]
    prep = {"variant": variant, "keywords": keywords, "ids": set(), "scan": [], "details": {"saved": 0, "gone": 0, "no_detail": 0, "bad_url": 0},
            "score": None, "stopped": None}
    results, plans, spent, used, fetched = [], [], 0.0, 0, False
    end = None

    def left() -> int:
        return limit - used

    def why_stop() -> str | None:
        if deadline is not None and clock() >= deadline:
            return "tiempo"
        return "tope" if left() <= 0 else None

    def count(status):
        return conn.execute("SELECT COUNT(*) FROM offers WHERE status = ?", (status,)).fetchone()[0]

    def halt(stage, message, row):
        nonlocal end
        prep["stopped"] = {"stage": stage, "message": message}
        row["reason"], end = "frenó", "frenó"

    canon: dict = {}  # el camino canónico que el sitio usa para cada búsqueda y zona (se aprende con el primer 301)
    for keyword in keywords:
        words = search_words(keyword)
        for zone in scan.zone_slugs(zones):
            row = {"keyword": keyword, "zone": ZONE_NAMES.get(zone, zone), "pages_read": 0, "found": 0, "new": 0, "seen": 0, "in_zone": 0,
                   "matching": 0, "sent": 0, "reason": ""}
            prep["scan"].append(row)
            if end:
                row["reason"] = f"no se llegó ({end})"
                continue
            streak, seen_ids, page_no = 0, set(), 0
            while not end and not row["reason"]:
                if reason := why_stop():
                    row["reason"], end = reason, reason
                    break
                page_no += 1
                try:
                    if fetched:
                        pause()
                    scan._check_stop(stop_file, f"la página {page_no} de «{keyword}»")
                    html = scan.get_listing(http_client, keyword, page_no, zone, f"la página {page_no}", canon)
                    if (scan.slug(keyword), zone) in canon:
                        row["site_slug"] = scan.site_slug(canon[(scan.slug(keyword), zone)], zone)
                    listing = scan.parse_listing(html)
                    fetched = True
                    row["pages_read"] += 1
                    if not listing and page_no == 1 and scan.no_offers_notice(html):
                        row["reason"] = "sin ofertas"  # una búsqueda sin resultados en esa zona: se sigue con la siguiente
                        break
                    if not listing and page_no == 1:
                        raise RuntimeError("La página 1 no trajo ofertas (posible cambio de layout o desafío). Se frena la corrida; revisá el sitio a mano.")
                except RuntimeError as e:
                    halt("scan", str(e), row)
                    break
                fresh = [o for o in listing if o["id16"] not in seen_ids]
                if not fresh:
                    row["reason"] = "sin más páginas"
                    break
                seen_ids |= {o["id16"] for o in fresh}
                new, seen = db.upsert_offers(conn, fresh, keyword)
                row["new"] += new
                row["seen"] += seen
                row["found"] += len(fresh)
                ids = {o["id16"] for o in fresh}
                before = {s: count(s) for s in ("gone", "no_detail", "bad_url")}
                saved_before = conn.execute("SELECT COUNT(*) FROM offers WHERE detail_fetched_at IS NOT NULL").fetchone()[0]
                error = None
                try:
                    scan.fetch_details(conn, len(ids), client=http_client, stop_file=stop_file, zones=zones, only=ids)
                except RuntimeError as e:
                    error = e
                prep["details"]["saved"] += conn.execute("SELECT COUNT(*) FROM offers WHERE detail_fetched_at IS NOT NULL").fetchone()[0] - saved_before
                for status in before:
                    prep["details"][status] += count(status) - before[status]
                if error:
                    halt("details", str(error), row)
                    break
                waiting = _needs_score(conn, ids, model, zones)
                if waiting:
                    month_cap, month_spent = run.get("max_cost_month", DEFAULT_RUN["max_cost_month"]), score.month_spent(conn, datetime.now(timezone.utc).date())
                    budget = min(run.get("max_cost", DEFAULT_RUN["max_cost"]) - spent, month_cap - month_spent)
                    try:
                        if llm_client is None:
                            raise RuntimeError(NO_KEY)
                        if budget <= 0:
                            raise RuntimeError(f"Tope de gasto alcanzado ({score.amounts(month_spent, month_cap)} en el mes; "
                                               f"{score.money(spent)} de {score.money(run['max_cost'])} en la tanda). No se puntúan más ofertas.")
                        out = score.score_pending(conn, profile, llm_client, model, waiting, budget, stop_file=stop_file, only=ids,
                                                  extra_variant=variant)
                        spent += out.get("cost_usd", 0.0)
                        total = prep["score"] or {"scored": 0, "excluded": 0, "errors": 0, "cost_usd": 0.0}
                        prep["score"] = {**out, **{k: total[k] + out.get(k, 0) for k in total}}
                    except RuntimeError as e:
                        halt("score", str(e), row)
                        break
                points = {id16: json.loads(raw or "{}").get(variant) for id16, raw in conn.execute(
                    "SELECT id16, all_scores FROM scores WHERE model = ? AND prompt_version = ?", (model, score.PROMPT_VERSION))}
                taken = fresh
                for index, offer in enumerate(fresh):
                    mine = is_from_search(offer["title"], words) or (opts.get("afin") and (points.get(offer["id16"]) or -1) >= threshold)
                    row["matching"] += 1 if mine else 0
                    row["in_zone"] += 1 if in_zone(offer["location"], zones) else 0
                    streak = 0 if mine else streak + 1
                    if streak >= STREAK_LIMIT:
                        taken, row["reason"] = fresh[:index + 1], f"{STREAK_LIMIT} seguidas de otra cosa"
                        break
                chunk = {o["id16"] for o in taken}
                prep["ids"] |= chunk
                batch = {"ids": chunk, "variant": variant, "max": left(), "deadline": deadline, "clock": clock,
                         "company_cap": bool(opts.get("por_empresa")), "defer_verify": True,
                         "applications": opts.get("applications"), "streak": opts.setdefault("streak", {"n": 0})}
                if live is None:
                    plan_ = apply.plan(conn, profile, model, today, only=chunk, variant=variant, cap=left(), company_cap_on=batch["company_cap"])
                    plans.append(plan_)
                    row["sent"] += len(plan_["send"])
                    used += len(plan_["send"])
                else:
                    result = live(batch)
                    results.append(result)
                    row["sent"] += len(result["sent"])
                    used += len(result["sent"]) + len(result.get("unconfirmed_now") or [])  # las sin confirmar cuentan para el tope
                    if result["halted"]:
                        row["reason"], end = "frenó", "frenó"
                    elif result["time_up"]:
                        row["reason"], end = "tiempo", "tiempo"
                if not end and (reason := why_stop()):
                    row["reason"], end = reason, reason
    live_result = merge_live(results)
    return {"prep": prep, "live": live_result, "plan": _merge_plans(plans), "end": end or "lista"}
