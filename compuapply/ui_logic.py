"""Lógica de la interfaz de escritorio, sin Tk: estado, formulario del perfil, comandos y subprocesos."""
import ctypes
import json
import os
import queue
import re
import sqlite3
import subprocess
import sys
import threading
from datetime import date, datetime, timezone
from pathlib import Path

import yaml

from compuapply import apply, cv, db, profile, score, setup

ENV = Path(".env")
PROFILE = Path("profile.yaml")
STOP = Path("STOP")
DB = Path("data/compuapply.db")
RUNS = Path("data/runs")
LAST_BATCH = Path("data/last_batch.json")
SHORTCUT = "compuapply.lnk"
CREATE_NO_WINDOW = 0x08000000
REPORT = re.compile(r"data[\\/]runs[\\/][^\s\\/]+\.md")
# Nombres de los campos numéricos tal como se ven en el formulario (los usan los mensajes de error).
LABELS = {
    "threshold": "Umbral de puntaje", "daily_cap": "Tope diario", "company_daily_cap": "Tope diario por empresa", "pages": "Páginas por keyword",
    "max_details": "Detalles a bajar", "score_limit": "Ofertas a puntuar", "max_cost": "Gasto máximo (USD)",
    "max_cost_month": "Tope de gasto mensual (USD)",
}


def _env_lines(env_path: Path) -> list[str]:
    try:
        return env_path.read_text(encoding="utf-8").splitlines() if env_path.is_file() else []
    except (OSError, UnicodeDecodeError):  # un .env ilegible se ve como «falta la clave»: Cargar clave lo explica
        return []


def status(env_path: Path, profile_path: Path) -> dict:
    """Estado de la clave, el modelo y el perfil para la pestaña Estado. Nunca devuelve la clave.

    `month_cap` es el tope de gasto mensual del perfil (`run.max_cost_month`) y `score_limit` las ofertas a puntuar por
    corrida (`run.score_limit`); si el perfil no carga, los de por defecto. `threshold` es el umbral de `apply` (None si
    el perfil no carga), `calibrated_with` con qué se calibró y `threshold_note` el aviso de `apply.calibration_note`
    (None si el umbral se calibró con el modelo y la versión de prompt actuales).
    """
    lines = _env_lines(env_path)
    result = {
        "key_ok": bool(setup._env_value(lines, "OPENAI_API_KEY")),
        "model": os.environ.get("OPENAI_MODEL") or setup._env_value(lines, "OPENAI_MODEL") or score.DEFAULT_MODEL,
        "profile_ok": False, "profile_msg": "", "variants": [], "month_cap": profile.DEFAULT_RUN["max_cost_month"],
        "score_limit": profile.DEFAULT_RUN["score_limit"], "threshold": None, "calibrated_with": None, "threshold_note": None,
    }
    try:
        loaded = profile.load_profile(profile_path)
    except RuntimeError as e:
        result["profile_msg"] = str(e)
        return result
    count = len(loaded["variants"])
    result.update(profile_ok=True, profile_msg=f"{count} variante{'' if count == 1 else 's'}",
                  variants=[{"id": v["id"], "pdf_ok": v["pdf"] is not None} for v in loaded["variants"]],
                  month_cap=loaded["run"]["max_cost_month"], score_limit=loaded["run"]["score_limit"],
                  threshold=loaded["apply"]["threshold"], calibrated_with=loaded["apply"].get("calibrated_with"),
                  threshold_note=apply.calibration_note(loaded, result["model"]))
    return result


def probe_line(model: str, info: dict) -> str:
    """La línea de `score --probe` con el resultado de `score.probe`."""
    each = info.get("all_scores", {})
    by_variant = " · puntajes: " + " / ".join(f"{vid} {points}" for vid, points in each.items()) if len(each) > 1 else ""
    return (f"Prueba OK con {model}: puntaje {info['score']} para {info['best_variant']}{by_variant} · tokens: entrada "
            f"{info['input_tokens']} / salida {info['output_tokens']} · costo: {score.money(info['cost_usd'])}")


def probe_summary(info: dict) -> str:
    """Lo que muestra la fila «Prueba de puntaje» de Estado cuando anduvo."""
    return f"puntaje {info['score']}, {score.money(info['cost_usd'])}"


def run_probe(env_path: Path, profile_path: Path, client_factory) -> dict:
    """Prueba de puntaje para la ventana: `score.probe` con la clave y el modelo de `.env` (sin tocar el entorno del proceso).

    `client_factory(api_key=...)` arma el cliente de OpenAI. Lanza RuntimeError si falta la clave o el perfil no carga,
    o si la prueba falla.
    """
    st = status(env_path, profile_path)
    if not st["key_ok"]:
        raise RuntimeError("Falta la clave de OpenAI: cargala antes de probar el puntaje.")
    loaded = profile.load_profile(profile_path)
    key = setup._env_value(_env_lines(env_path), "OPENAI_API_KEY")
    return score.probe(client_factory(api_key=key), st["model"], loaded)


def run_cv(env_path: Path, profile_path: Path, db_path: Path, id16: str, client_factory, render=None) -> dict:
    """Arma el CV de una oferta para la ventana: `cv.make` con la clave y el modelo de `.env` (sin tocar el entorno del proceso).

    `client_factory(api_key=...)` arma el cliente de OpenAI. Lanza RuntimeError si falta la clave, el perfil no carga o `cv.make` falla.
    """
    st = status(env_path, profile_path)
    if not st["key_ok"]:
        raise RuntimeError("Falta la clave de OpenAI: cargala antes de armar el CV.")
    loaded = profile.load_profile(profile_path)
    key = setup._env_value(_env_lines(env_path), "OPENAI_API_KEY")
    conn = db.connect(db_path)
    try:
        return cv.make(conn, loaded, client_factory(api_key=key), st["model"], id16, render=render)
    finally:
        conn.close()


def cv_line(info: dict) -> str:
    """Lo que muestra la ventana cuando `cv.make` terminó (la ruta del PDF y el costo; nunca el contenido del CV)."""
    note = f" Se armó solo con ids del CV base (sin texto adaptado): {info['fallback_reason']}" if info.get("fallback_reason") else ""
    if info.get("replaced"):
        n = len(info["replaced"])
        note = f" Se {'reemplazó 1 línea' if n == 1 else f'reemplazaron {n} líneas'}: {' '.join(info['replaced'])}"
    return f"CV guardado en {info['pdf']} · costo: {score.money(info['cost_usd'])}.{note} Revisalo antes de usarlo (hay un archivo de revisión al lado)."


# ── formulario del perfil ────────────────────────────────────────────────────

def empty_form() -> dict:
    """Formulario sin perfil: una variante vacía y los valores por defecto."""
    return {
        "variants": [{"id": "", "resume": "", "keywords": "", "pdf": ""}],
        "exclude_companies": "", "exclude_title_words": "", "zones": "",
        "apply": {"threshold": str(profile.DEFAULT_THRESHOLD), "daily_cap": str(profile.DEFAULT_DAILY_CAP),
                  "company_daily_cap": str(profile.DEFAULT_COMPANY_DAILY_CAP)},
        "run": {key: str(value) for key, value in profile.DEFAULT_RUN.items()},
        "answers": dict.fromkeys(profile.ANSWER_KEYS, ""),
        "review_cv": "",  # «true» / «false» / vacío (no declarado: vale true)
    }


def profile_to_form(path: Path) -> dict:
    """Formulario (todo texto) con lo que dice `profile.yaml`, sin cargar los CV. Sin archivo, el formulario vacío."""
    if not path.is_file():
        return empty_form()
    data = profile._read_yaml(path, "el perfil")
    items = data.get("variants") or []
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        raise RuntimeError(f"'variants' en {path} no tiene el formato esperado: corregilo a mano.")
    form = empty_form()
    form["variants"] = [
        {"id": str(item.get("id") or ""), "resume": str(item.get("resume") or ""),
         "keywords": ", ".join(profile._str_list(item.get("keywords"))), "pdf": str(item.get("pdf") or "")}
        for item in items
    ] or form["variants"]
    form["exclude_companies"] = "\n".join(profile._str_list(data.get("exclude_companies")))
    form["exclude_title_words"] = "\n".join(profile._str_list(data.get("exclude_title_words")))
    form["zones"] = "\n".join(profile._str_list(data.get("zones")))
    apply_values = data.get("apply")
    if isinstance(apply_values, dict) and isinstance(apply_values.get("review_cv"), bool):
        form["review_cv"] = "true" if apply_values["review_cv"] else "false"
    answers = data.get("answers")
    if isinstance(answers, dict):
        form["answers"] = {key: " ".join(str(answers.get(key) or "").split()) for key in profile.ANSWER_KEYS}
    for section in ("apply", "run"):
        values = data.get(section)
        if isinstance(values, dict):
            form[section] = {key: default if values.get(key) is None else str(values[key])
                             for key, default in form[section].items()}
    return form


def _split(text: str, separator: str) -> list[str]:
    return [part.strip() for part in text.split(separator) if part.strip()]


def _number(text: str, label: str, kind=int):
    """`text` como entero o decimal (con coma o punto); si no lo es, RuntimeError con el nombre del campo."""
    shown = text.strip()
    try:
        return kind(shown.replace(",", ".") if kind is float else shown)
    except ValueError:
        what = "un número entero" if kind is int else "un número, por ejemplo 0.50"
        raise RuntimeError(f"«{label}» tiene que ser {what} ({'está vacío' if not shown else f'hay «{shown}»'}).") from None


def form_to_profile(form: dict) -> dict:
    """Datos de `profile.yaml` a partir del formulario. Lanza RuntimeError si un número no es un número."""
    variants = []
    for v in form["variants"]:
        item = {"id": v["id"].strip(), "resume": v["resume"].strip(), "keywords": _split(v["keywords"], ",")}
        if v["pdf"].strip():
            item["pdf"] = v["pdf"].strip()
        variants.append(item)
    apply_, run = form["apply"], form["run"]
    data = {
        "variants": variants,
        "exclude_companies": _split(form["exclude_companies"], "\n"),
        "exclude_title_words": _split(form["exclude_title_words"], "\n"),
        "apply": {key: _number(apply_[key], LABELS[key]) for key in ("threshold", "daily_cap", "company_daily_cap")},
        "run": {
            **{key: _number(run[key], LABELS[key]) for key in ("pages", "max_details", "score_limit")},
            **{key: _number(run[key], LABELS[key], float) for key in ("max_cost", "max_cost_month")},
        },
    }
    zones = _split(form.get("zones", ""), "\n")
    if zones:  # vacío = todas: la clave no se escribe
        data["zones"] = zones
    review = str(form.get("review_cv", "")).strip().casefold()
    if review in ("true", "false"):
        data["apply"]["review_cv"] = review == "true"
    answers = {key: " ".join(form.get("answers", {}).get(key, "").split()) for key in profile.ANSWER_KEYS}
    if answers["modality"]:  # en su forma canónica; una inválida pasa tal cual y `save_profile` la rechaza con un error claro
        answers["modality"] = profile.canonical_modality(answers["modality"]) or answers["modality"]
    if any(answers.values()):
        data["answers"] = {key: text for key, text in answers.items() if text}
    return data


def save_profile(data: dict, path: Path) -> None:
    """Valida `data` con `load_profile` sobre un temporal junto a `path` y recién ahí lo pone en su lugar.

    Si el perfil actual tiene `apply.calibrated_with` y `data` conserva el mismo umbral, lo mantiene; con otro umbral lo
    borra, porque ya no viene de la calibración. El formulario no tiene los campos `truths` ni `default_pdf_name` de las
    variantes ni `run.max_account_pdfs`: lo que había (mismo `id`) se conserva; tampoco tiene las respuestas a preguntas puntuales de `answers` (solo las 4 claves fijas): se conservan.
    """
    try:
        current_data = profile._read_yaml(path, "el perfil") if path.is_file() else {}
    except RuntimeError:  # un perfil roto no tiene calibración ni verdades que conservar: el guardado lo reemplaza
        current_data = {}
    current, apply_ = current_data.get("apply"), data.get("apply")
    for key in ("truths", "default_pdf_name"):  # campos de la variante que el formulario no tiene: se conservan por `id`
        old = {v.get("id"): v[key] for v in current_data.get("variants") or [] if isinstance(v, dict) and v.get(key)}
        if old and isinstance(data.get("variants"), list):
            data = {**data, "variants": [{**v, key: old[v.get("id")]} if isinstance(v, dict) and key not in v
                                         and v.get("id") in old else v for v in data["variants"]]}
    old_run = current_data.get("run")
    for key in ("max_account_pdfs", "last_batch"):  # el formulario no tiene estas claves de `run`: se conservan
        if isinstance(old_run, dict) and old_run.get(key) is not None and isinstance(data.get("run"), dict) and key not in data["run"]:
            data = {**data, "run": {**data["run"], key: old_run[key]}}
    if isinstance(current, dict) and isinstance(current.get("free_cv_slots"), bool) and isinstance(data.get("apply"), dict)             and "free_cv_slots" not in data["apply"]:  # `apply.free_cv_slots` no está en el formulario: se conserva
        data = {**data, "apply": {**data["apply"], "free_cv_slots": current["free_cv_slots"]}}
        apply_ = data["apply"]
    old_answers = current_data.get("answers")
    if isinstance(old_answers, dict):  # el formulario solo tiene las 4 claves fijas: las respuestas a preguntas puntuales se conservan
        kept = {key: text for key, text in old_answers.items() if key not in profile.ANSWER_KEYS}
        if kept:
            data = {**data, "answers": {**(data.get("answers") if isinstance(data.get("answers"), dict) else {}), **kept}}
    if isinstance(current, dict) and isinstance(apply_, dict) and "calibrated_with" not in apply_:
        if current.get("calibrated_with") is not None and current.get("threshold") == apply_.get("threshold"):
            data = {**data, "apply": {**apply_, "calibrated_with": current["calibrated_with"]}}
    tmp = path.with_name(path.name + ".tmp")  # misma carpeta: las rutas relativas de resume y pdf se resuelven igual
    try:
        tmp.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
        try:
            profile.load_profile(tmp)
        except RuntimeError as e:
            raise RuntimeError(str(e).replace(str(tmp), str(path))) from None
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def set_threshold(profile_path: Path, threshold: int, model: str) -> None:
    """Pone `apply.threshold` y `apply.calibrated_with` («<modelo> <PROMPT_VERSION>») sin tocar el resto de `profile.yaml`.

    `save_profile` lo valida (umbral de 0 a 100) antes de reemplazar el archivo.
    """
    data = profile._read_yaml(profile_path, "el perfil")
    current = data.get("apply")
    data["apply"] = {**(current if isinstance(current, dict) else {}), "threshold": threshold,
                     "calibrated_with": f"{model} {score.PROMPT_VERSION}"}
    save_profile(data, profile_path)


# ── banco de verdades: el programa propone, el usuario aprueba ───────────────

def _safe(variant: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", variant)


def _declared_truths(variant: str, profile_path: Path) -> str | None:
    """La ruta que `profile.yaml` declara como `truths` de la variante, o None (también si el perfil no se puede leer)."""
    try:
        data = profile._read_yaml(profile_path, "el perfil")
    except RuntimeError:
        return None
    return next((str(v["truths"]) for v in data.get("variants") or [] if isinstance(v, dict) and v.get("id") == variant and v.get("truths")), None)


def truths_path(variant: str, profile_path: Path = PROFILE) -> Path:
    """El archivo del banco de la variante: el que declara el perfil o, si no declara, `cv/truths-<variante>.yaml` junto al perfil."""
    declared = _declared_truths(variant, profile_path)
    return profile_path.parent / (declared or f"cv/truths-{_safe(variant)}.yaml")


def _proposals_path(variant: str, profile_path: Path) -> Path:
    return profile_path.parent / "cv" / f"truths-{_safe(variant)}.propuestas.yaml"


def _write_yaml(path: Path, data, header: str = "") -> None:
    """Escribe un YAML en UTF-8 por un temporal junto al archivo (nunca queda a medias)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(header + yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def load_truths(variant: str, profile_path: Path = PROFILE) -> list[str]:
    """El banco aprobado de la variante (lista vacía si todavía no hay archivo)."""
    path = truths_path(variant, profile_path)
    return profile._load_truths(path, path.name, variant) if path.is_file() else []


def _write_truths(variant: str, texts: list[str], profile_path: Path) -> None:
    _write_yaml(truths_path(variant, profile_path), texts,
                f"# Banco de verdades de la variante {variant}: lo aprobaste vos. El CV a medida puede usar estos hechos.\n")


def _declare_truths(variant: str, profile_path: Path) -> None:
    """Si la variante no declara `truths` en `profile.yaml`, le agrega `cv/truths-<variante>.yaml` sin tocar lo demás."""
    if _declared_truths(variant, profile_path):
        return
    data = profile._read_yaml(profile_path, "el perfil")
    item = next((v for v in data.get("variants") or [] if isinstance(v, dict) and v.get("id") == variant), None)
    if item is None:
        raise RuntimeError(f"La variante '{variant}' no está en {profile_path.name}: no se puede declarar su banco de verdades.")
    item["truths"] = f"cv/truths-{_safe(variant)}.yaml"
    save_profile(data, profile_path)


def load_proposals(variant: str, profile_path: Path = PROFILE) -> dict:
    """`{"propuestas": [{text, sources, reason}], "rechazadas": [texto]}` de la variante (vacío si todavía no hay archivo)."""
    path = _proposals_path(variant, profile_path)
    empty = {"propuestas": [], "rechazadas": []}
    if not path.is_file():
        return empty
    where = f"El archivo de propuestas de la variante '{variant}' ({path.name})"
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        raise RuntimeError(f"{where} no se pudo leer como YAML en UTF-8: corregilo o borralo.") from None
    if data is None:  # archivo vacío
        return empty
    pending, rejected = (data.get("propuestas"), data.get("rechazadas")) if isinstance(data, dict) else ("?", "?")
    pending, rejected = [] if pending is None else pending, [] if rejected is None else rejected
    if not isinstance(pending, list) or not isinstance(rejected, list) or not all(isinstance(t, str) for t in rejected) or not all(
            isinstance(p, dict) and isinstance(p.get("text"), str) and isinstance(p.get("reason"), str)
            and isinstance(p.get("sources"), list) and all(isinstance(s, str) for s in p["sources"]) for p in pending):
        raise RuntimeError(f"{where} no tiene el formato esperado (propuestas con text, sources y reason; rechazadas con textos): corregilo o borralo.")
    return {"propuestas": [{"text": p["text"], "sources": p["sources"], "reason": p["reason"]} for p in pending], "rechazadas": list(rejected)}


def _save_proposals(variant: str, data: dict, profile_path: Path) -> None:
    _write_yaml(_proposals_path(variant, profile_path), data)


def _take(variant: str, texts: list[str], profile_path: Path) -> tuple[dict, list[dict]]:
    """Las propuestas pendientes y las que tienen esos textos; si alguna ya no está, RuntimeError (sin citar su texto)."""
    data = load_proposals(variant, profile_path)
    chosen = [p for p in data["propuestas"] if p["text"] in set(texts)]
    if len({p["text"] for p in chosen}) != len(set(texts)):
        raise RuntimeError("Alguna de las propuestas elegidas ya no está en la lista: actualizá y probá de nuevo.")
    return data, chosen


def approve(variant: str, texts: list[str], profile_path: Path = PROFILE) -> int:
    """Pasa las propuestas con esos textos al banco (aprobarlas es declararlas) y las saca de las pendientes; devuelve cuántas.

    Si la variante no declara `truths` en `profile.yaml`, se lo agrega sin tocar lo demás.
    """
    data, chosen = _take(variant, texts, profile_path)
    bank = load_truths(variant, profile_path)
    _write_truths(variant, bank + [p["text"] for p in chosen if p["text"] not in bank], profile_path)
    _declare_truths(variant, profile_path)
    _save_proposals(variant, {**data, "propuestas": [p for p in data["propuestas"] if p not in chosen]}, profile_path)
    return len(chosen)


def reject(variant: str, texts: list[str], profile_path: Path = PROFILE) -> int:
    """Borra las propuestas con esos textos y las guarda en `rechazadas`, para que no se vuelvan a proponer; devuelve cuántas."""
    data, chosen = _take(variant, texts, profile_path)
    _save_proposals(variant, {"propuestas": [p for p in data["propuestas"] if p not in chosen],
                              "rechazadas": data["rechazadas"] + [p["text"] for p in chosen if p["text"] not in data["rechazadas"]]},
                    profile_path)
    return len(chosen)


def add_truth(variant: str, text: str, profile_path: Path = PROFILE) -> None:
    """Agrega al banco un hecho que escribió el usuario (y declara `truths` en el perfil si hacía falta)."""
    text = " ".join(text.split())
    if not text:
        raise RuntimeError("Escribí el hecho antes de agregarlo.")
    bank = load_truths(variant, profile_path)
    if text in bank:
        raise RuntimeError("Ese hecho ya está en el banco.")
    _write_truths(variant, [*bank, text], profile_path)
    _declare_truths(variant, profile_path)


def remove_truth(variant: str, text: str, profile_path: Path = PROFILE) -> None:
    """Quita un hecho del banco aprobado."""
    bank = load_truths(variant, profile_path)
    if text not in bank:
        raise RuntimeError("Ese hecho ya no está en el banco: actualizá la lista.")
    _write_truths(variant, [t for t in bank if t != text], profile_path)


def bank_view(variant: str, profile_path: Path = PROFILE) -> dict:
    """Lo que muestra la pestaña Banco: `propuestas` (con `source_lines`, las líneas del CV de donde salen), `banco` y `rechazadas` (cuántas)."""
    loaded = profile.load_profile(profile_path)
    chosen = next((v for v in loaded["variants"] if v["id"] == variant), None)
    if chosen is None:
        raise RuntimeError(f"La variante '{variant}' no está en {profile_path.name}.")
    lines = cv.source_items(chosen["resume"])
    data = load_proposals(variant, profile_path)
    return {"propuestas": [{**p, "source_lines": [lines[s] for s in p["sources"] if s in lines]} for p in data["propuestas"]],
            "banco": load_truths(variant, profile_path), "rechazadas": len(data["rechazadas"])}


def propose(variant: str, profile_path: Path, db_path: Path, client, model: str) -> dict:
    """Pide al modelo propuestas de verdades para la variante y las suma a las pendientes. No aprueba nada.

    Respeta el tope mensual (`run.max_cost_month`): lleno, no llama a la API. El gasto va a `api_spend` con `kind = 'truths'`
    aunque la respuesta no sirva. Devuelve `count` (propuestas nuevas), `pending` (en total) y `cost_usd`.
    """
    loaded = profile.load_profile(profile_path)
    chosen = next((v for v in loaded["variants"] if v["id"] == variant), None)
    if chosen is None:
        raise RuntimeError(f"La variante '{variant}' no está en {profile_path.name}.")
    if model not in score.PRICES:
        raise RuntimeError(f"No tengo el precio del modelo '{model}': agregá su precio en score.PRICES.")
    data = load_proposals(variant, profile_path)
    known = [*load_truths(variant, profile_path), *(p["text"] for p in data["propuestas"]), *data["rechazadas"]]
    try:
        conn = db.connect(db_path)
    except sqlite3.Error as e:
        raise RuntimeError(f"No se pudo abrir la base: {e}") from None
    calls: list = []
    try:
        cap, spent = loaded["run"]["max_cost_month"], score.month_spent(conn, datetime.now(timezone.utc).date())
        if cap - spent <= 0:
            raise RuntimeError(f"Tope mensual alcanzado: {score.amounts(spent, cap)}. No se piden propuestas.")
        try:
            found = cv.propose_truths(chosen["resume"], client, model, known, calls)
        finally:  # cada respuesta ya se pagó, sirva o no
            for call in calls:
                db.add_spend(conn, "truths", variant, model, call["cost"], call["tokens"][0], call["tokens"][2], call["valid"])
    except sqlite3.Error as e:
        raise RuntimeError(f"No se pudo usar la base: {e}") from None
    finally:
        conn.close()
    _save_proposals(variant, {"propuestas": [*data["propuestas"], *found], "rechazadas": data["rechazadas"]}, profile_path)
    return {"count": len(found), "pending": len(data["propuestas"]) + len(found), "cost_usd": sum(c["cost"] for c in calls)}


def propose_line(info: dict) -> str:
    """Lo que muestran la ventana y la consola cuando `propose` terminó (cuántas y el costo; nunca el texto)."""
    n = info["count"]
    return f"{'Propuso 1 verdad nueva' if n == 1 else f'Propuso {n} verdades nuevas'} · costo: {score.money(info['cost_usd'])}."


def run_propose(env_path: Path, profile_path: Path, db_path: Path, variant: str, client_factory) -> dict:
    """`propose` para la ventana, con la clave y el modelo de `.env` (sin tocar el entorno del proceso)."""
    st = status(env_path, profile_path)
    if not st["key_ok"]:
        raise RuntimeError("Falta la clave de OpenAI: cargala antes de pedir propuestas.")
    key = setup._env_value(_env_lines(env_path), "OPENAI_API_KEY")
    return propose(variant, profile_path, db_path, client_factory(api_key=key), st["model"])


# ── postulación en vivo y preguntas sin respuesta ────────────────────────────

def live_count(profile_path: Path, db_path: Path, model: str) -> int:
    """Cuántas ofertas mandaría `apply --live` ahora (las de «Enviaría» del plan): lo que dice la confirmación de la ventana."""
    loaded = profile.load_profile(profile_path)
    conn = db.connect(db_path)
    try:
        return len(apply.plan(conn, loaded, model, date.today())["send"])
    finally:
        conn.close()


def live_blocker(profile_path: Path) -> str | None:
    """Por qué `apply --live` no puede subir CVs ahora (la revisión de CVs está activada), o None."""
    return apply.REVIEW_MESSAGE if apply.review_required(profile.load_profile(profile_path)) else None


def pending_questions(conn: sqlite3.Connection) -> list[dict]:
    """Las preguntas sin respuesta, una por pregunta normalizada: `key`, `question` (como la vio el programa), `options`, `offers` y `first_seen`."""
    found: dict[str, dict] = {}
    for id16, question, options, first_seen in conn.execute(
            "SELECT id16, question, options, first_seen FROM pending_questions ORDER BY first_seen, rowid"):
        entry = found.setdefault(profile.normalize_question(question), {
            "key": profile.normalize_question(question), "question": question, "options": json.loads(options or "[]"),
            "offers": 0, "first_seen": first_seen})
        entry["offers"] += 1
    return list(found.values())


def answer_question(profile_path: Path, db_path: Path, key: str, answer: str) -> int:
    """Guarda la respuesta del usuario a una pregunta (`key` = pregunta normalizada) en `answers` de `profile.yaml`.

    Las ofertas que esperaban esa pregunta (`pending_question`) vuelven a la cola y la pregunta sale de las pendientes.
    Devuelve cuántas ofertas volvieron.
    """
    answer = " ".join(answer.split())
    if not answer:
        raise RuntimeError("Escribí la respuesta antes de guardarla.")
    if not key or key in profile.ANSWER_KEYS:
        raise RuntimeError("Esa pregunta no se puede guardar como respuesta.")
    data = profile._read_yaml(profile_path, "el perfil")
    current = data.get("answers")
    data["answers"] = {**(current if isinstance(current, dict) else {}), key: answer}
    save_profile(data, profile_path)
    conn = db.connect(db_path)
    try:
        rows = [(id16, question) for id16, question in conn.execute("SELECT id16, question FROM pending_questions")
                if profile.normalize_question(question) == key]
        requeued = 0
        for id16, question in rows:
            conn.execute("DELETE FROM pending_questions WHERE id16 = ? AND question = ?", (id16, question))
            if conn.execute("UPDATE offers SET status = 'new' WHERE id16 = ? AND status = 'pending_question'", (id16,)).rowcount:
                requeued += 1
        conn.commit()
        return requeued
    finally:
        conn.close()


# ── resultados, informes y gasto ─────────────────────────────────────────────

def results(conn: sqlite3.Connection, model: str, threshold: int | None = None, zones=()) -> list[dict]:
    """El puntaje de `model` y el `PROMPT_VERSION` actual de cada oferta no duplicada, de mayor a menor; con `threshold`, solo las que lo alcanzan.

    Con `zones` deja afuera las ofertas cuya provincia no es una de esas (`results_split` también las cuenta). Sin `zones`, todas.

    Claves: id16, score, title, company, variant, status, scored_at, reasons (lista), missing (lista), unmet_required (lista: los
    excluyentes que no cumple) y url.
    """
    return results_split(conn, model, threshold, zones)[0]


def results_split(conn: sqlite3.Connection, model: str, threshold: int | None = None, zones=()) -> tuple[list[dict], int]:
    """Como `results`, pero devuelve también cuántas ofertas (con ese `threshold`) quedaron afuera por estar fuera de `zones`."""
    outside = 0
    rows = [{"id16": id16, "score": points, "title": title or "", "company": company or "", "variant": variant,
             "status": status, "scored_at": scored_at, "reasons": json.loads(reasons or "[]"),
             "missing": json.loads(missing or "[]"), "unmet_required": json.loads(unmet or "[]"), "url": url or "",
             "location": location or ""}
            for id16, points, title, company, variant, status, scored_at, reasons, missing, unmet, url, location in conn.execute(
                "SELECT s.id16, s.score, o.title, o.company, s.best_variant, o.status, s.scored_at, s.reasons, s.missing, "
                "s.unmet_required, o.url, o.location "
                "FROM scores s JOIN offers o ON o.id16 = s.id16 WHERE s.model = ? AND s.prompt_version = ? "
                "AND o.status != 'duplicate' ORDER BY s.scored_at DESC, s.rowid DESC", (model, score.PROMPT_VERSION))]
    if threshold is not None:
        rows = [row for row in rows if row["score"] >= threshold]
    kept = [row for row in rows if profile.in_zone(row["location"], zones)]
    outside = len(rows) - len(kept)
    for row in kept:
        del row["location"]  # solo servía para el filtro
    return sorted(kept, key=lambda row: -row["score"]), outside  # estable: a igual puntaje queda primero el más reciente


def reports(runs_dir: Path = RUNS) -> list[Path]:
    """Los informes `*.md` de `runs_dir`, el más nuevo primero (a igual fecha de archivo, por nombre)."""
    if not runs_dir.is_dir():
        return []
    found = [path for path in runs_dir.glob("*.md") if path.is_file()]
    return sorted(found, key=lambda path: (path.stat().st_mtime, path.name), reverse=True)


def spend(conn: sqlite3.Connection, today: date) -> dict:
    """Gasto estimado en OpenAI (suma de `scores.cost_usd` y `api_spend.cost_usd`): `month`, el del mes de `today`, y `total`.

    El mes se toma de la fecha UTC de cada puntaje (así factura OpenAI), por eso `today` también tiene que ser la de UTC.
    """
    total = sum(conn.execute(f"SELECT COALESCE(SUM(cost_usd), 0) FROM {table}").fetchone()[0] for table in ("scores", "api_spend"))
    return {"month": score.month_spent(conn, today), "total": float(total)}


def schedule_blocker(st: dict) -> str | None:
    """Por qué no se puede programar la corrida diaria, o None si se puede.

    El perfil tiene que estar en ✔ siempre; la clave, solo si `run.score_limit` es mayor que 0 (con 0, `run` busca sin
    clave). `st` es el resultado de `status`.
    """
    if not st["profile_ok"]:
        return f"El perfil no está en orden, así que la corrida programada fallaría: {st['profile_msg']}"
    if not st["key_ok"] and st["score_limit"] > 0:
        return ("Falta la clave de OpenAI: cargala en la pestaña Estado antes de programar la corrida "
                "(sin clave solo se puede programar con «Ofertas a puntuar» en 0: así busca sin puntuar).")
    return None


# ── acceso directo ───────────────────────────────────────────────────────────

def shortcut_command(desktop: Path, project: Path, pythonw: Path) -> list[str]:
    """Comando de PowerShell que crea `compuapply.lnk` en `desktop`: destino `pythonw`, `-m compuapply.gui`, en `project`."""
    def quoted(value) -> str:
        return "'" + str(value).replace("'", "''") + "'"

    script = (f"$s = (New-Object -ComObject WScript.Shell).CreateShortcut({quoted(desktop / SHORTCUT)}); "
              f"$s.TargetPath = {quoted(pythonw)}; $s.Arguments = '-m compuapply.gui'; "
              f"$s.WorkingDirectory = {quoted(project)}; $s.Save()")
    return ["powershell", "-NoProfile", "-Command", script]


def pythonw_exe() -> Path:
    """El `pythonw.exe` de la carpeta de `sys.executable`; RuntimeError si no está."""
    exe = Path(sys.executable).with_name("pythonw.exe")
    if not exe.is_file():
        raise RuntimeError("No se encontró pythonw.exe junto a python.exe: reinstalá Python.")
    return exe


def desktop_dir() -> Path:
    """La carpeta Escritorio real del usuario (respeta OneDrive y el idioma de Windows)."""
    buffer = ctypes.create_unicode_buffer(260)
    ctypes.windll.shell32.SHGetFolderPathW(None, 0x0010, None, 0, buffer)  # CSIDL_DESKTOPDIRECTORY
    return Path(buffer.value)


def create_shortcut(desktop: Path, project: Path, pythonw: Path) -> str:
    """Crea (o reemplaza) `compuapply.lnk` en `desktop` y devuelve el mensaje para el usuario."""
    replaced = (desktop / SHORTCUT).exists()
    done = subprocess.run(shortcut_command(desktop, project, pythonw), capture_output=True, text=True,
                          creationflags=CREATE_NO_WINDOW)
    if done.returncode != 0:
        detail = next((line.strip() for line in done.stderr.splitlines() if line.strip()), "sin detalle")
        raise RuntimeError(f"No se pudo crear el acceso directo: {detail}")
    return "Acceso directo reemplazado en el escritorio." if replaced else "Acceso directo creado en el escritorio."


# ── comandos y subprocesos ───────────────────────────────────────────────────

def python_exe() -> str:
    """El `python.exe` de la carpeta de `sys.executable` (con `pythonw.exe` el subproceso no tendría salida)."""
    exe = Path(sys.executable).with_name("python.exe")
    return str(exe if exe.is_file() else sys.executable)


def build_command(action: str, settings: dict) -> list[str]:
    """Comando de la CLI para `run`, `score`, `apply`, `apply_live`, `login` o `session_check`; `settings` es la sección `run:` del perfil."""
    base = [python_exe(), "-u", "-m", "compuapply"]
    if action == "score":
        return [*base, "score", "--limit", str(settings["score_limit"]), "--max-cost", str(settings["max_cost"])]
    if action in ("batch", "batch_dry"):  # `settings`: variant, keywords, minutes, max
        command = ["tanda", "--variant", settings["variant"], "--keywords", settings["keywords"], "--minutes", str(settings["minutes"]),
                   "--max", "all" if settings["max"] is None else str(settings["max"])]
        command += [*(["--afin-puntaje"] if settings.get("afin") else []), *(["--por-empresa"] if settings.get("per_company") else [])]
        return [*base, *command, *(["--dry"] if action == "batch_dry" else [])]
    commands = {"run": ["run"], "apply": ["apply"], "apply_live": ["apply", "--live"], "login": ["login"],
                "session_check": ["session", "check"]}
    if action not in commands:
        raise ValueError(f"Acción desconocida: {action}")
    return [*base, *commands[action]]


def remember_batch(batch: dict, path: Path = LAST_BATCH) -> None:
    """Guarda la última tanda (`variant`, `keywords`, `minutes`, `max`) en `data/last_batch.json`; nunca toca `profile.yaml`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(batch, ensure_ascii=False, indent=2), encoding="utf-8")


def _saved_batch(path: Path) -> dict | None:
    """La tanda guardada en el JSON, o None si falta, está rota o no tiene la forma esperada."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    ints = all(data.get(k) is None or (type(data[k]) is int and data[k] >= (1 if k == "minutes" else 0)) for k in ("minutes", "max"))
    return {"variant": str(data.get("variant") or ""), "keywords": str(data.get("keywords") or ""), "minutes": data.get("minutes"),
            "max": data.get("max"), "afin": data.get("afin") is True, "per_company": data.get("per_company") is True} if ints else None


def batch_defaults(path: Path, batch_path: Path = LAST_BATCH) -> dict:
    """Lo que muestra el bloque «Tanda»: la última tanda de `data/last_batch.json` (si no hay, la `run.last_batch` vieja del perfil, solo
    leída), 60 minutos y el tope diario por defecto; vacío sin perfil. `max` None es el tope vacío (sin tope) de una tanda guardada; `afin` y
    `per_company` (las casillas) vienen apagadas salvo que la tanda guardada las haya encendido."""
    try:
        loaded = profile.load_profile(path)
    except RuntimeError:
        return {"variants": [], "variant": "", "keywords": "", "minutes": 60, "max": 10, "afin": False, "per_company": False}
    saved = _saved_batch(batch_path)
    last = saved or loaded["run"].get("last_batch") or {}
    ids = [v["id"] for v in loaded["variants"]]
    cap = last.get("max") if saved else (loaded["apply"]["daily_cap"] if last.get("max") is None else last["max"])
    return {"variants": ids, "variant": last.get("variant") if last.get("variant") in ids else ids[0], "keywords": last.get("keywords") or "",
            "minutes": last.get("minutes") or 60, "max": cap, "afin": bool(last.get("afin")), "per_company": bool(last.get("per_company"))}


def report_path(lines: list[str]) -> Path | None:
    """Ruta del último informe `data/runs/*.md` que aparece en la salida, o None."""
    for line in reversed(lines):
        found = REPORT.search(line)
        if found:
            return Path(found.group())
    return None


def clear_stop(stop_file: Path = STOP) -> bool:
    """Borra el archivo STOP si existe; devuelve si existía."""
    existed = stop_file.exists()
    stop_file.unlink(missing_ok=True)
    return existed


def request_stop(stop_file: Path = STOP) -> None:
    """Crea el archivo STOP: el programa frena en el próximo control."""
    stop_file.touch()


def start_job(command: list[str], output: "queue.Queue[str | None]", interactive: bool = False) -> subprocess.Popen:
    """Lanza `command` sin consola; cada línea de su salida va a `output` y un `None` marca el final.

    Con `interactive` el proceso recibe un stdin al que se le puede escribir (el `Enter` de `login`).
    """
    proc = subprocess.Popen(
        command, stdin=subprocess.PIPE if interactive else subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"},
        creationflags=CREATE_NO_WINDOW, text=True, encoding="utf-8", errors="replace", bufsize=1)

    def pump() -> None:
        for line in proc.stdout:
            output.put(line.rstrip("\r\n"))
        output.put(None)

    threading.Thread(target=pump, daemon=True).start()
    return proc


def kill_tree(pid: int) -> None:
    """Cierra el proceso y todos sus hijos."""
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, creationflags=CREATE_NO_WINDOW)
