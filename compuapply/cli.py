"""Línea de comandos de compuapply."""
import argparse
import getpass
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import openai
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from compuapply import apply, batch, browser, cv, db, pending, profile, review, run, scan, schedule, score, setup, ui_logic

PROJECT = Path(__file__).resolve().parent.parent  # la carpeta del proyecto: ahí corre la tarea programada
LIVE_NOT_IMPLEMENTED = "El envío real de `run` todavía no está implementado: postulá con `python -m compuapply apply --live`."


def _count(conn: sqlite3.Connection, where: str) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM offers WHERE {where}").fetchone()[0]


def _bad_url_note(bad: int) -> str:
    return f" · URL inválida: {bad}" if bad else ""


def _no_detail_note(missing: int) -> str:
    return f" · sin descripción: {missing}" if missing else ""


def _scan(args: argparse.Namespace) -> int:
    saved = bad = missing = None  # None: el error, si lo hay, no ocurrió bajando detalles
    try:
        zones = profile.load_zones(args.profile)
        conn = db.connect(args.db)
        try:
            offers, pages_read = scan.collect(args.keyword, args.pages, zones=zones)
            new, seen = db.upsert_offers(conn, offers, args.keyword)
            saved_before = _count(conn, "detail_fetched_at IS NOT NULL")
            bad_before = _count(conn, "status = 'bad_url'")
            missing_before = _count(conn, "status = 'no_detail'")
            try:
                scan.fetch_details(conn, args.max_details, zones=zones)
            finally:
                saved = _count(conn, "detail_fetched_at IS NOT NULL") - saved_before
                bad = _count(conn, "status = 'bad_url'") - bad_before
                missing = _count(conn, "status = 'no_detail'") - missing_before
        finally:
            conn.close()
    except (RuntimeError, sqlite3.Error, OSError) as e:
        print(f"Error: {e}", file=sys.stderr)
        if saved is not None:
            print(f"Detalles guardados antes del error: {saved}{_bad_url_note(bad)}{_no_detail_note(missing)}",
                  file=sys.stderr)
        return 1
    print(
        f"Páginas leídas: {pages_read} · encontradas: {len(offers)} · nuevas: {new} · ya vistas: {seen} "
        f"· detalles: {saved}{_bad_url_note(bad)}{_no_detail_note(missing)}"
    )
    return 0


def _probe(args: argparse.Namespace) -> int:
    """`score --probe`: puntúa una oferta de ejemplo para comprobar el pedido real a OpenAI (no escribe en la base)."""
    try:
        loaded = profile.load_profile(args.profile)
        model = score.load_settings()
        info = score.probe(openai.OpenAI(), model, loaded)
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    print(ui_logic.probe_line(model, info))
    return 0


def _score(args: argparse.Namespace) -> int:
    if args.probe:
        return _probe(args)
    try:
        loaded = profile.load_profile(args.profile)
        model = score.load_settings()
        client = openai.OpenAI()
        conn = db.connect(args.db)
    except (RuntimeError, sqlite3.Error, OSError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    started = datetime.now(timezone.utc).isoformat()
    today, cap = datetime.now(timezone.utc).date(), loaded["run"]["max_cost_month"]
    this_run = "FROM scores s JOIN offers o ON o.id16 = s.id16 WHERE s.scored_at >= ? AND s.model = ? AND s.prompt_version = ?"
    try:
        spent = score.month_spent(conn, today)
        if cap - spent <= 0:  # sin llamar a la API
            print(f"Tope mensual alcanzado: {score.amounts(spent, cap)}")
            return 0
        budget = min(args.max_cost, cap - spent)  # lo que queda del tope mensual también corta la corrida
        try:
            result = score.score_pending(conn, loaded, client, model, args.limit, budget, retry_errors=args.retry_errors)
        except (RuntimeError, sqlite3.Error) as e:
            # El error no trae los totales de la corrida: se leen de lo que alcanzó a guardarse.
            n, cost = conn.execute(
                f"SELECT COUNT(*), COALESCE(SUM(s.cost_usd), 0) {this_run}", (started, model, score.PROMPT_VERSION)).fetchone()
            print(f"Error: {e}", file=sys.stderr)
            print(f"Antes del error: puntuadas {n} · costo (solo las guardadas): {score.money(cost)}", file=sys.stderr)
            return 1
        top = conn.execute(
            f"SELECT s.score, o.title, o.company, s.best_variant {this_run} ORDER BY s.score DESC, o.rowid LIMIT 5",
            (started, model, score.PROMPT_VERSION),
        ).fetchall()
        spent = score.month_spent(conn, today)
    finally:
        conn.close()
    print(
        f"Puntuadas: {result['scored']} · excluidas: {result['excluded']}{run.duplicates_part(result)} · con error: {result['errors']} "
        f"· costo: {score.money(result['cost_usd'])} · tokens: entrada {result['input_tokens']} / salida {result['output_tokens']}"
    )
    for points, title, company, variant in top:
        print(f"{points} · {title} · {company} · {variant}")
    if result["stopped_by_cost"]:
        limit = (f"--max-cost {score.money(budget, 2)}" if budget == args.max_cost
                 else f"lo que quedaba del tope mensual, {score.money(budget)}")
        print(f"Se llegó al tope de gasto ({limit}): quedan ofertas sin puntuar.")
    print(f"Gasto del mes: {score.amounts(spent, cap)}")
    return 0


def _apply_live(args: argparse.Namespace) -> int:
    """`apply --live`: postula de verdad a las ofertas de «Enviaría», con el navegador visible; frena ante lo desconocido."""
    try:
        loaded = profile.load_profile(args.profile)
        score.load_env()
        model = os.environ.get("OPENAI_MODEL") or score.DEFAULT_MODEL
        client = openai.OpenAI() if os.environ.get("OPENAI_API_KEY") else None  # solo hace falta si aparecen preguntas
        conn = db.connect(args.db)
    except (RuntimeError, sqlite3.Error, OSError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    now = datetime.now()
    holder = {}
    try:
        meta = {"now": now, "threshold": loaded["apply"]["threshold"], "daily_cap": loaded["apply"]["daily_cap"],
                "company_daily_cap": loaded["apply"]["company_daily_cap"], "applied_today": apply.applied_today(conn, now.date()), "model": model}
        requeued = apply.requeue_answerable(conn, loaded)  # las preguntas que ya tienen respuesta devuelven su oferta a la cola
        if not apply.plan(conn, loaded, model, now.date())["send"] or apply.review_required(loaded):
            # nada que enviar, o la revisión de CVs está activada (frena antes de subir): no se abre el navegador
            holder["result"] = apply.run_live(conn, loaded, model, client, None, now.date(), archive_dir=apply.ARCHIVE_DIR)
        else:
            def action(context, pw) -> bool:
                page = browser._page(context)
                if not browser.is_logged_in(page):
                    raise RuntimeError("Sesión vencida: corré python -m compuapply login e iniciá sesión a mano.")
                holder["result"] = apply.run_live(conn, loaded, model, client, page, now.date(), pw=pw, archive_dir=apply.ARCHIVE_DIR)  # los PDF usan este mismo Playwright
                return True

            if not _run_browser(action, with_playwright=True):
                return 1
        result = holder["result"]
        result["requeued"] += requeued
        report = apply.write_live_report(result, meta)
    except (RuntimeError, sqlite3.Error, OSError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    print(apply.live_summary(result))
    for note in [*apply.plan_notes(result["plan"]), *(line[2:] for line in apply.archive_lines(result["plan"]) if result["plan"].get('archive_pruned'))]:
        print(note)
    if result["halted"]:
        print(f"Freno: {result['halted']}", file=sys.stderr)
    print(f"Informe: {report}")
    return 1 if result["halted"] else 0


def _apply(args: argparse.Namespace) -> int:
    if args.live:
        return _apply_live(args)
    try:
        loaded = profile.load_profile(args.profile)
        score.load_env()  # solo para OPENAI_MODEL: apply no llama a la API ni necesita la clave
        model = os.environ.get("OPENAI_MODEL") or score.DEFAULT_MODEL
        now = datetime.now()
        conn = db.connect(args.db)
        try:
            result = apply.plan(conn, loaded, model, now.date())
            result["archive_pruned"] = apply.prune_archive(apply.ARCHIVE_DIR)  # también en seco: solo cv/postulaciones/
            meta = {"now": now, "threshold": loaded["apply"]["threshold"], "daily_cap": loaded["apply"]["daily_cap"],
                    "company_daily_cap": loaded["apply"]["company_daily_cap"], "applied_today": apply.applied_today(conn, now.date()), "model": model}
        finally:
            conn.close()
        report = apply.write_report(result, meta)
    except (RuntimeError, sqlite3.Error, OSError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    print(apply.summary(result))
    for note in [*apply.plan_notes(result), *(line[2:] for line in apply.archive_lines(result) if result.get('archive_pruned'))]:
        print(note)
    print(f"Informe: {report}")
    return 0


def _run(args: argparse.Namespace) -> int:
    if args.live:
        print(LIVE_NOT_IMPLEMENTED, file=sys.stderr)
        return 1
    try:
        loaded = profile.load_profile(args.profile)
        score.load_env()
        model = os.environ.get("OPENAI_MODEL") or score.DEFAULT_MODEL
        # La clave hace falta solo si la etapa de puntaje va a llamar a la API: `run_all` lo decide y, si falta, frena ahí.
        llm_client = openai.OpenAI() if os.environ.get("OPENAI_API_KEY") else None
        conn = db.connect(args.db)
    except (RuntimeError, sqlite3.Error, OSError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    now = datetime.now()
    pages, max_details, score_limit, max_cost = (  # un flag explícito gana; si no, la sección `run` del perfil
        loaded["run"][key] if flag is None else flag
        for key, flag in (("pages", args.pages), ("max_details", args.max_details),
                          ("score_limit", args.score_limit), ("max_cost", args.max_cost)))
    try:
        with httpx.Client(timeout=scan.TIMEOUT) as http_client:
            result = run.run_all(conn, loaded, http_client, llm_client, model, pages, max_details, score_limit,
                                 max_cost, now.date())
        meta = {"now": now, "model": model, "threshold": loaded["apply"]["threshold"],
                "daily_cap": loaded["apply"]["daily_cap"], "company_daily_cap": loaded["apply"]["company_daily_cap"], "applied_today": apply.applied_today(conn, now.date())}
        report = run.write_report(result, meta)
    except (RuntimeError, sqlite3.Error, OSError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    for line in run.summary_lines(result):
        print(line)
    if result["stopped"]:
        print(f"Freno en la etapa {result['stopped']['stage']}: {result['stopped']['message']}", file=sys.stderr)
    print(f"Informe: {report}")
    return 1 if result["stopped"] else 0


def _max_arg(text: str):
    """`--max`: un entero de 0 o más, o `all` (sin tope: manda solo el tiempo)."""
    if text.strip().casefold() == "all":
        return "all"
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError("tiene que ser un entero de 0 o más, o all (sin tope)") from None
    if value < 0:
        raise argparse.ArgumentTypeError("tiene que ser un entero de 0 o más, o all (sin tope)")
    return value


def _tanda(args: argparse.Namespace) -> int:
    """`tanda`: recorre las búsquedas elegidas en el orden dado, página por página y zona por zona (`zones`); por cada página baja los
    detalles, puntúa, arma el plan y postula (o solo planea, con `--dry`) con el CV (variante) elegido, hasta que se acaba la lista, el
    tiempo (`--minutes`) o el tope (`--max`: sin él, el tope diario; `all`, sin tope). Un solo informe."""
    try:
        loaded = profile.load_profile(args.profile)
        keywords = batch.parse_keywords(args.keywords)
        score.load_env()
        model = os.environ.get("OPENAI_MODEL") or score.DEFAULT_MODEL
        llm_client = openai.OpenAI() if os.environ.get("OPENAI_API_KEY") else None
        conn = db.connect(args.db)
    except (RuntimeError, sqlite3.Error, OSError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    max_send = loaded["apply"]["daily_cap"] if args.max is None else (None if args.max == "all" else args.max)
    minutes = args.minutes
    started, now = time.monotonic(), datetime.now()
    opts = {"max": max_send, "afin": args.afin_puntaje, "por_empresa": args.por_empresa}
    try:
        if args.variant not in {v["id"] for v in loaded["variants"]}:
            raise RuntimeError(f"La variante «{args.variant}» no está en profile.yaml (hay: {', '.join(v['id'] for v in loaded['variants'])}).")
        if not keywords:
            raise RuntimeError("Falta al menos una búsqueda: --keywords \"Atención al cliente, Mozo\".")
        if minutes < 1:
            raise RuntimeError("--minutes tiene que ser 1 o más.")
        try:
            ui_logic.remember_batch({"variant": args.variant, "keywords": ", ".join(keywords), "minutes": minutes, "max": max_send,
                                     "afin": args.afin_puntaje, "per_company": args.por_empresa}, args.db.parent / "last_batch.json")
        except (RuntimeError, OSError):
            pass  # no poder anotar la última tanda no frena la tanda
        deadline = started + minutes * 60
        with httpx.Client(timeout=scan.TIMEOUT) as http_client:
            def walk(live) -> dict:
                return batch.walk(conn, loaded, http_client, llm_client, model, args.variant, keywords, now.date(), opts, live=live,
                                  deadline=deadline)

            holder = {}
            if args.dry:
                holder["out"] = walk(None)
            elif apply.review_required(loaded):
                holder["out"] = walk(lambda b: apply.run_live(conn, loaded, model, llm_client, None, now.date(), archive_dir=apply.ARCHIVE_DIR, batch=b))
            else:
                def action(context, pw) -> bool:
                    page = browser._page(context)
                    if not browser.is_logged_in(page):
                        raise RuntimeError("Sesión vencida: corré python -m compuapply login e iniciá sesión a mano.")
                    try:  # «Mis postulaciones» se lee una vez al principio (y otra al final, en la verificación)
                        opts["applications"] = browser.read_applications(page, pages=apply.VERIFY_PAGES)
                    except Exception as e:
                        raise RuntimeError(apply._halt_text(e)) from e
                    early = apply.reconcile_unconfirmed(conn, opts["applications"])  # las sin confirmar que ya figuran pasan a enviadas
                    holder["out"] = walk(lambda b: apply.run_live(conn, loaded, model, llm_client, page, now.date(), pw=pw,
                                                                  archive_dir=apply.ARCHIVE_DIR, batch=b))
                    if holder["out"]["live"] is not None:
                        holder["out"]["live"]["reconciled"] = early + holder["out"]["live"].get("reconciled", [])
                    apply.final_check(holder["out"]["live"], page, conn)  # una sola lectura de «Mis postulaciones» para todas las enviadas
                    return True

                if not _run_browser(action, with_playwright=True) and "out" not in holder:
                    return 1
        out = holder["out"]
        prep, live, plan_ = out["prep"], out["live"], out["plan"]
        if plan_ is None and live is None:
            plan_ = apply.plan(conn, loaded, model, now.date(), only=set(), variant=args.variant, cap=0)
        meta = {"now": now, "minutes": minutes, "max": max_send, "threshold": loaded["apply"]["threshold"], "model": model, "dry": args.dry,
                "company_daily_cap": loaded["apply"]["company_daily_cap"], "per_company": args.por_empresa, "afin": args.afin_puntaje,
                "end": out["end"], "minutes_used": (time.monotonic() - started) / 60}
        report = batch.write_report(prep, plan_, live, meta)
    except (RuntimeError, sqlite3.Error, OSError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    print(batch.summary_line(prep, live, plan_, meta["minutes_used"]))
    print(f"Fin: {batch.END_TEXT.get(out['end'], out['end'])}")
    for note in apply.plan_notes(plan_ or live["plan"]):
        print(note)
    if prep["stopped"]:
        print(f"Freno en la etapa {prep['stopped']['stage']}: {prep['stopped']['message']}", file=sys.stderr)
    if live and live["halted"]:
        print(f"Freno: {live['halted']}", file=sys.stderr)
    print(f"Informe: {report}")
    return 1 if prep["stopped"] or (live and live["halted"]) else 0


def _schedule(args: argparse.Namespace) -> int:
    try:
        if args.schedule_command == "set":
            print(schedule.set_daily(ui_logic.pythonw_exe(), PROJECT, args.at))
        elif args.schedule_command == "off":
            print(schedule.removal_message(schedule.remove()))
        else:
            print(schedule.describe(schedule.current()))
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    return 0


def _with_browser(action, with_playwright: bool = False) -> bool:
    """Abre el perfil persistente, corre `action(context)` (o `action(context, pw)`, con la instancia de Playwright abierta) y cierra el contexto pase lo que pase."""
    with sync_playwright() as pw:
        context = browser.open_context(pw)
        try:
            return action(context, pw) if with_playwright else action(context)
        finally:
            context.close()


def _run_browser(action, with_playwright: bool = False) -> bool | None:
    """Resultado de `action`, o None si falló (el error ya quedó impreso)."""
    try:
        return _with_browser(action, with_playwright)
    except (RuntimeError, PlaywrightError) as e:
        lines = str(e).splitlines()
        print(f"Error: {lines[0] if lines else 'falló el navegador'}", file=sys.stderr)
        if isinstance(e, PlaywrightError):
            if browser.browser_name() == "Chrome":
                print(f"Revisá que Chrome esté instalado y que no haya otra ventana de Chrome usando el perfil "
                      f"{browser.CHROME_PROFILE.as_posix()}/ (si abriste Chrome con login, cerralo).", file=sys.stderr)
            else:
                print("Revisá que Chromium esté instalado (python -m playwright install chromium) y que no haya "
                      "otra ventana usando el perfil .browser-profile/.", file=sys.stderr)
    except EOFError:
        print("Error: hace falta una terminal interactiva para esperar el Enter.", file=sys.stderr)
    except KeyboardInterrupt:
        print("Cancelado.", file=sys.stderr)
    return None


def _login_in_chrome(chrome) -> bool | None:
    """Abre Chrome como un proceso común, espera a que el usuario cierre la ventana y recién ahí verifica la sesión con Playwright
    (un Chrome abierto con el mismo perfil lo bloquea). None si falló (el error ya quedó impreso)."""
    try:
        process = browser.start_chrome(chrome)
        print(browser.CHROME_PROMPT)
        process.wait()
    except OSError as e:
        print(f"Error: no se pudo abrir Chrome ({e}).", file=sys.stderr)
        return None
    except KeyboardInterrupt:
        print("Cancelado.", file=sys.stderr)
        return None
    return _run_browser(browser.session_check)


def _login(args: argparse.Namespace) -> int:
    chrome = browser.find_chrome()
    if chrome:
        logged_in = _login_in_chrome(chrome)
    else:
        print(browser.NO_CHROME, file=sys.stderr)
        logged_in = _run_browser(lambda context: browser.login(context, wait=input))
    if logged_in is None:
        return 1
    if not logged_in:
        print("Error: sigue sin haber sesión. Corré python -m compuapply login de nuevo e iniciá sesión en la ventana.",
              file=sys.stderr)
        return 1
    print(f"Sesión iniciada. El perfil quedó en {browser.profile_dir().as_posix()}/")
    return 0


def _session_restore_default(args: argparse.Namespace) -> int:
    try:
        loaded = profile.load_profile(args.profile)
    except (RuntimeError, OSError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    def action(context):
        page = browser._page(context)
        if not browser.is_logged_in(page):
            raise RuntimeError("Sesión vencida: corré python -m compuapply login")
        return apply.restore_default_cv(page, loaded, args.variant)

    done = _run_browser(action)
    if done is None:
        return 1
    print(f"CV por defecto antes: {done['was'] or 'ninguno'} · ahora: {done['now'] or 'ninguno'} (declarado: {done['name']}).")
    return 0 if done["now"] and browser.same_name(done["name"], done["now"]) else 1


def _session_check(args: argparse.Namespace) -> int:
    if args.session_command == "restore-default":
        return _session_restore_default(args)
    active = _run_browser(browser.session_check)
    if active is None:
        return 1
    if not active:
        print("Sesión vencida: corré python -m compuapply login", file=sys.stderr)
        return 1
    print("Sesión activa")
    return 0


def _pending(args: argparse.Namespace) -> int:
    try:
        conn = db.connect(args.db)
        try:
            pending.run(conn, args.variant, args.profile, args.answer)
        finally:
            conn.close()
    except (RuntimeError, sqlite3.Error, OSError, EOFError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    return 0


def _percent(value: float | None) -> str:
    """Porcentaje truncado (así un 80% mostrado siempre cumple >= 80), o `-` si no hay divisor."""
    return "-" if value is None else f"{int(value)}%"


def _review(args: argparse.Namespace) -> int:
    try:
        score.load_env()  # solo para OPENAI_MODEL: los puntajes que cuentan son los del modelo en uso
        model = os.environ.get("OPENAI_MODEL") or score.DEFAULT_MODEL
        if args.review_command == "export":
            invalid = review.count_invalid_ids(args.out)  # antes de exportar: el export reescribe el archivo
            conn = db.connect(args.db)
            try:
                total, kept = review.export_csv(conn, args.out, model)
            finally:
                conn.close()
            print(f"Ofertas exportadas: {total} · etiquetas conservadas: {kept} · archivo: {args.out}")
            if invalid:
                print(f"Filas con id inválido: {invalid}", file=sys.stderr)
            return 0
        if args.review_command == "label":
            zones = profile.load_zones(args.profile)
            conn = db.connect(args.db)
            try:
                review.label_session(conn, args.limit, zones=zones)
            finally:
                conn.close()
            return 0
        if args.csv is None:
            conn = db.connect(args.db)
            try:
                zones = profile.load_zones(args.profile)  # solo cuentan las etiquetas de ofertas en zona
                labeled, invalid, unscored = review.db_labels(conn, model, zones), 0, review.unscored_note(conn, model)
            finally:
                conn.close()
        else:
            labeled = review.read_labels(args.csv)
            invalid, unscored = review.count_invalid_ids(args.csv), None
    except (RuntimeError, sqlite3.Error, OSError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    if invalid:
        print(f"Filas con id inválido: {invalid}", file=sys.stderr)
    if unscored:
        print(unscored)
    if len(labeled) < review.MIN_LABELS:
        print(f"Etiquetá al menos {review.MIN_LABELS} ofertas (hay {len(labeled)})", file=sys.stderr)
        return 1
    table, suggested = review.threshold_table(labeled)
    for row in table:
        print(f"{row['threshold']} · enviaría: {row['send']} · precisión: {_percent(row['precision'])} "
              f"· cobertura: {_percent(row['recall'])}")
    if suggested is None:
        print(f"Ningún umbral llega a {review.MIN_PRECISION}% de precisión")
    else:
        row = next(r for r in table if r["threshold"] == suggested)
        print(f"Umbral sugerido: {suggested} (precisión {_percent(row['precision'])}, cobertura {_percent(row['recall'])})")
    return 0


def _truths(args: argparse.Namespace) -> int:
    """`cv truths propose|list`: propone verdades deducidas del CV base (imprime cuántas y el costo, nunca el texto) o lista el banco."""
    try:
        if args.truths_command == "list":
            bank, data = ui_logic.load_truths(args.variant, args.profile), ui_logic.load_proposals(args.variant, args.profile)
            print("\n".join([f"Banco aprobado ({len(bank)}):", *(f"- {text}" for text in bank),
                             f"Propuestas pendientes ({len(data['propuestas'])}):",
                             *(f"- {p['text']}  [{p['reason']}]" for p in data["propuestas"]),
                             f"Rechazadas: {len(data['rechazadas'])}"]))
            return 0
        model = score.load_settings()
        info = ui_logic.propose(args.variant, args.profile, args.db, openai.OpenAI(), model)
    except (RuntimeError, sqlite3.Error, OSError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    print(f"{ui_logic.propose_line(info)} Pendientes de aprobar: {info['pending']}. Aprobalas en la pestaña Banco de la ventana.")
    return 0


def _cv(args: argparse.Namespace) -> int:
    """`cv make`: arma el CV a medida de una oferta (o de las N mejores sin CV); imprime rutas y costo, nunca el contenido."""
    if args.cv_command == "truths":
        return _truths(args)
    one = None
    try:
        if args.id is not None:
            one = args.id.strip()
            one = one[len(review.ID_PREFIX):] if one[:len(review.ID_PREFIX)].lower() == review.ID_PREFIX else one
            if not re.fullmatch(r"[0-9A-Fa-f]{16}", one):
                raise RuntimeError(f"«{args.id}» no es un id de oferta (16 caracteres hexadecimales, con o sin «ct-» adelante).")
        loaded = profile.load_profile(args.profile)
        model = score.load_settings()
        client = openai.OpenAI()
        conn = db.connect(args.db)
    except (RuntimeError, sqlite3.Error, OSError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    spent_run, cap, error = 0.0, loaded["run"]["max_cost_month"], None
    try:
        ids = [one.upper()] if one else cv.top_candidates(conn, model, args.top)
        for id16 in ids:
            if spent_run >= args.max_cost:
                print(f"Se llegó al tope de gasto (--max-cost {score.money(args.max_cost, 2)}): quedan ofertas sin CV.")
                break
            info = cv.make(conn, loaded, client, model, id16)
            spent_run += info["cost_usd"]
            print(f"{id16} · {info['variant']} · {info['pdf']} · costo: {score.money(info['cost_usd'])}")
            if info["fallback_reason"]:
                print(f"  Se armó solo con ids del CV base (sin texto adaptado): {info['fallback_reason']}")
            elif info.get("replaced"):
                n = len(info["replaced"])
                print(f"  Se {'reemplazó 1 línea' if n == 1 else f'reemplazaron {n} líneas'}: {' '.join(info['replaced'])}")
        else:
            if not ids:
                print("No hay ofertas puntuadas sin CV.")
    except (RuntimeError, sqlite3.Error, OSError) as e:
        error = e
    try:
        spent = score.month_spent(conn, datetime.now(timezone.utc).date())
    finally:
        conn.close()
    print(f"Gasto del mes: {score.amounts(spent, cap)}")
    if error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    return 0


def _setup(args: argparse.Namespace) -> int:
    try:
        if args.setup_command == "env":
            print(setup.setup_env(Path(".env"), ask=getpass.getpass, client_factory=openai.OpenAI))
            return 0
        names = setup.list_cv_dirs(args.cv_dir)
        if args.variants is None:
            if not names:
                raise RuntimeError(f"En --cv-dir no hay ninguna subcarpeta con {setup.RESUME_NAME}.")
            print("\n".join(names))
            print("Elegí 1 a 3 con --variants")
            return 0
        chosen = [name.strip() for name in args.variants.split(",") if name.strip()]
        out = setup.write_profile(args.cv_dir, chosen, args.out)
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except OSError as e:  # sin e.filename: la ruta puede llevar el nombre de usuario
        print(f"Error: no se pudo leer o escribir un archivo ({e.strerror or type(e).__name__}).", file=sys.stderr)
        return 1
    except EOFError:
        print("Error: hace falta una terminal interactiva para pegar la clave.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Cancelado.", file=sys.stderr)
        return 1
    print(f"{out.name} creado con {len(chosen)} variante(s). Completá las keywords y, cuando los tengas, los PDF.")
    return _profile_check(argparse.Namespace(profile=out))


def _profile_check(args: argparse.Namespace) -> int:
    try:
        loaded = profile.load_profile(args.profile)
        lines = []
        for variant in loaded["variants"]:
            resume = variant["resume"]
            if variant["pdf"] is None:
                pdf_note = "falta"
            else:
                pdf_note = f"OK ({-(-variant['pdf'].stat().st_size // 1024)} KB)"  # hacia arriba; la ruta no se imprime
            lines.append(
                f"{variant['id']} · experiencias: {len(resume.get('experience_details') or [])} "
                f"· proyectos: {len(resume.get('projects') or [])} "
                f"· caracteres para el LLM: {len(profile.resume_for_llm(resume))} · pdf: {pdf_note}"
            )
    except (RuntimeError, OSError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    print("\n".join(lines))
    return 0


def main(argv: list[str] | None = None) -> int:
    """Punto de entrada: devuelve el código de salida (0 ok, 1 error)."""
    parser = argparse.ArgumentParser(prog="compuapply")
    sub = parser.add_subparsers(dest="command", required=True)
    scan_p = sub.add_parser("scan", help="busca ofertas en el listado, las guarda y baja el detalle de las pendientes")
    scan_p.add_argument("--keyword", required=True)
    scan_p.add_argument("--pages", type=int, default=3)
    scan_p.add_argument("--max-details", type=int, default=20, help="detalles a bajar (0 = ninguno)")
    scan_p.add_argument("--profile", type=Path, default=Path("profile.yaml"), help="solo se lee `zones`: no baja el detalle de otras zonas")
    scan_p.add_argument("--db", type=Path, default=Path("data/compuapply.db"))
    score_p = sub.add_parser("score", help="filtra y puntúa las ofertas con descripción contra cada variante de CV")
    score_p.add_argument("--limit", type=int, default=20, help="máximo de ofertas a puntuar en la corrida")
    score_p.add_argument("--max-cost", type=float, default=0.50, help="tope de gasto de la corrida, en USD")
    score_p.add_argument("--retry-errors", action="store_true", help="vuelve a puntuar las ofertas en score_error")
    score_p.add_argument("--probe", action="store_true",
                         help="puntúa una oferta de ejemplo (menos de US$ 0,001) para comprobar que el pedido a OpenAI anda; no escribe en la base")
    score_p.add_argument("--profile", type=Path, default=Path("profile.yaml"))
    score_p.add_argument("--db", type=Path, default=Path("data/compuapply.db"))
    apply_p = sub.add_parser("apply", help="muestra a qué ofertas postularía y cuáles saltaría (modo seco), o postula de verdad con --live")
    apply_p.add_argument("--live", action="store_true",
                         help="postula de verdad a las ofertas de «Enviaría», con el navegador visible (contesta las preguntas con tu CV y tus respuestas)")
    apply_p.add_argument("--profile", type=Path, default=Path("profile.yaml"))
    apply_p.add_argument("--db", type=Path, default=Path("data/compuapply.db"))
    run_p = sub.add_parser("run", help="scan, detalles, puntaje y plan de apply (modo seco) con un solo informe")
    run_p.add_argument("--live", action="store_true", help="envío real: todavía no está implementado")
    run_p.add_argument("--pages", type=int, help="páginas del listado por keyword (por defecto, run.pages del perfil: 2)")
    run_p.add_argument("--max-details", type=int, help="detalles a bajar, 0 = ninguno (por defecto, run.max_details: 20)")
    run_p.add_argument("--score-limit", type=int,
                       help="máximo de ofertas a puntuar en la corrida (por defecto, run.score_limit: 20)")
    run_p.add_argument("--max-cost", type=float, help="tope de gasto de la corrida, en USD (por defecto, run.max_cost: 0.50)")
    run_p.add_argument("--profile", type=Path, default=Path("profile.yaml"))
    run_p.add_argument("--db", type=Path, default=Path("data/compuapply.db"))
    review_p = sub.add_parser("review", help="etiqueta ofertas puntuadas (en la terminal o por CSV) y propone el umbral")
    review_sub = review_p.add_subparsers(dest="review_command", required=True)
    label_p = review_sub.add_parser("label", help="pregunta «¿te postularías?» por ofertas puntuadas, sin mostrar el puntaje")
    label_p.add_argument("--limit", type=int, default=30, help="máximo de ofertas a preguntar en la sesión")
    label_p.add_argument("--profile", type=Path, default=Path("profile.yaml"), help="solo se lee `zones`: no ofrece ofertas de otras zonas")
    label_p.add_argument("--db", type=Path, default=Path("data/compuapply.db"))
    export_p = review_sub.add_parser("export", help="escribe el CSV de ofertas puntuadas, conservando las etiquetas")
    export_p.add_argument("--out", type=Path, default=Path("data/review.csv"))
    export_p.add_argument("--db", type=Path, default=Path("data/compuapply.db"))
    threshold_p = review_sub.add_parser("threshold", help="propone el umbral con las ofertas etiquetadas")
    threshold_p.add_argument("--csv", type=Path, help="usa las etiquetas de este CSV en vez de las de la base")
    threshold_p.add_argument("--db", type=Path, default=Path("data/compuapply.db"))
    threshold_p.add_argument("--profile", type=Path, default=Path("profile.yaml"), help="solo se lee `zones`: cuenta las etiquetas de ofertas en zona")
    tanda_p = sub.add_parser("tanda", help="postula con un CV (variante) solo a las ofertas de las búsquedas elegidas, durante N minutos o hasta un tope")
    tanda_p.add_argument("--variant", required=True, help="id de la variante (el CV con que se puntúa y se postula)")
    tanda_p.add_argument("--keywords", required=True, help="las búsquedas, separadas por comas")
    tanda_p.add_argument("--minutes", type=int, default=batch.DEFAULT_MINUTES, help="pasado ese tiempo no se empieza otra oferta (por defecto 60)")
    tanda_p.add_argument("--max", type=_max_arg, help="postulaciones como máximo en la tanda, en lugar del tope diario; sin esta opción se usa "
                                                       "apply.daily_cap y all es sin tope (manda solo el tiempo)")
    tanda_p.add_argument("--afin-puntaje", action="store_true", help="también cuenta como de la búsqueda la oferta con puntaje igual o mayor al umbral")
    tanda_p.add_argument("--por-empresa", action="store_true", help="aplica apply.company_daily_cap (por defecto la tanda no limita por empresa)")
    tanda_p.add_argument("--dry", action="store_true", help="hace todo menos el envío: scan, detalles, puntaje y plan")
    tanda_p.add_argument("--profile", type=Path, default=Path("profile.yaml"))
    tanda_p.add_argument("--db", type=Path, default=Path("data/compuapply.db"))
    sub.add_parser("login", help="abre Chromium para que inicies sesión a mano en Computrabajo (perfil persistente)")
    session_p = sub.add_parser("session", help="comandos de la sesión de Computrabajo")
    session_sub = session_p.add_subparsers(dest="session_command", required=True)
    session_sub.add_parser("check", help="dice si el perfil tiene una sesión activa (abre la ventana, solo lee)")
    restore_p = session_sub.add_parser("restore-default", help="deja como CV por defecto de la cuenta el default_pdf_name de la variante "
                                                              "(no postula, no sube ni borra nada)")
    restore_p.add_argument("--variant", default="ventas", help="id de la variante (por defecto ventas)")
    restore_p.add_argument("--profile", type=Path, default=Path("profile.yaml"))
    pending_p = sub.add_parser("pending", help="lista las preguntas pendientes sin repetir; con --answer pide la respuesta de cada una "
                                               "y la guarda como hecho en el banco de verdades")
    pending_p.add_argument("--answer", action="store_true", help="pide la respuesta de cada una (Enter salta, «-» la marca «no la contesto»)")
    pending_p.add_argument("--variant", default="ventas", help="id de la variante cuyo banco recibe las respuestas (por defecto ventas)")
    pending_p.add_argument("--profile", type=Path, default=Path("profile.yaml"))
    pending_p.add_argument("--db", type=Path, default=Path("data/compuapply.db"))
    profile_p = sub.add_parser("profile", help="comandos del perfil")
    check_p = profile_p.add_subparsers(dest="profile_command", required=True).add_parser(
        "check", help="carga profile.yaml y resume cada variante de CV sin mostrar sus datos")
    check_p.add_argument("--profile", type=Path, default=Path("profile.yaml"))
    schedule_p = sub.add_parser("schedule", help="programa una corrida diaria de `run` en modo seco (tarea de Windows)")
    schedule_sub = schedule_p.add_subparsers(dest="schedule_command", required=True)
    schedule_set_p = schedule_sub.add_parser("set", help="programa (o reprograma) la corrida de todos los días")
    schedule_set_p.add_argument("--at", required=True, metavar="HH:MM", help="la hora, de 00:00 a 23:59 (por ejemplo 09:00)")
    schedule_sub.add_parser("off", help="quita la corrida programada")
    schedule_sub.add_parser("show", help="dice si hay una corrida programada y cómo le fue la última")
    cv_p = sub.add_parser("cv", help="arma un CV a medida de una oferta, solo para revisar (no se sube nada)")
    cv_sub = cv_p.add_subparsers(dest="cv_command", required=True)
    make_p = cv_sub.add_parser(
        "make", help="elige y ordena items de tu CV base para una oferta puntuada y deja el HTML y el PDF en data/cvs/")
    which = make_p.add_mutually_exclusive_group(required=True)
    which.add_argument("--id", help="id de la oferta (16 caracteres, con o sin «ct-» adelante)")
    which.add_argument("--top", type=int, help="las N ofertas nuevas mejor puntuadas que todavía no tienen CV")
    make_p.add_argument("--max-cost", type=float, default=0.05, help="tope de gasto de la corrida, en USD (por defecto 0.05)")
    make_p.add_argument("--profile", type=Path, default=Path("profile.yaml"))
    make_p.add_argument("--db", type=Path, default=Path("data/compuapply.db"))
    truths_sub = cv_sub.add_parser("truths", help="banco de verdades: el programa propone, vos aprobás en la ventana").add_subparsers(
        dest="truths_command", required=True)
    for name, text in (("propose", "pide al modelo verdades que se siguen de tu CV base (no aprueba nada)"),
                       ("list", "muestra el banco aprobado y las propuestas pendientes")):
        one_p = truths_sub.add_parser(name, help=text)
        one_p.add_argument("--variant", required=True, help="id de la variante")
        one_p.add_argument("--profile", type=Path, default=Path("profile.yaml"))
        one_p.add_argument("--db", type=Path, default=Path("data/compuapply.db"))
    setup_p = sub.add_parser("setup", help="deja listos .env y profile.yaml sin editarlos a mano")
    setup_sub = setup_p.add_subparsers(dest="setup_command", required=True)
    setup_sub.add_parser("env", help="pide tu clave de OpenAI (no se ve), la verifica gratis y crea .env")
    setup_profile_p = setup_sub.add_parser("profile", help="crea profile.yaml a partir de tus CV en formato AIHawk")
    setup_profile_p.add_argument("--cv-dir", type=Path, required=True,
                                 help="carpeta cuyas subcarpetas tienen cada una un plain_text_resume.yaml")
    setup_profile_p.add_argument("--variants", help="de 1 a 3 nombres de subcarpeta, separados por coma")
    setup_profile_p.add_argument("--out", type=Path, default=Path("profile.yaml"))
    args = parser.parse_args(argv)
    handlers = {"scan": _scan, "score": _score, "apply": _apply, "run": _run, "review": _review,
                "profile": _profile_check, "login": _login, "session": _session_check, "setup": _setup,
                "schedule": _schedule, "cv": _cv, "tanda": _tanda, "pending": _pending}
    return handlers[args.command](args)
