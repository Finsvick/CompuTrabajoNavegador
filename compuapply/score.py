"""Filtros y puntaje de ofertas contra las variantes de CV, con la API de OpenAI."""
import hashlib
import json
import os
import re
import sqlite3
import unicodedata
from datetime import date
from pathlib import Path

import openai

from compuapply import db
from compuapply.profile import in_zone, resume_for_llm

PROMPT_VERSION = "v5"  # v5: un pedido por variante, con un solo CV (con varios CVs en el mismo pedido el modelo los mezclaba con la oferta); v4: lista `unmet_required` (excluyentes que el CV no cumple); v3: el CV del puntaje trae technical_skills; v2: temperature 0 en los modelos sin razonamiento; los puntajes viejos no se reutilizan
DEFAULT_MODEL = "gpt-6-luna"
MAX_DESCRIPTION = 6000
MAX_OUTPUT_TOKENS = 2000
MAX_ERRORS_IN_A_ROW = 3
# USD por millón de tokens: (entrada, entrada cacheada, salida).
PRICES = {
    "gpt-6-luna": (0.10, 0.01, 0.50),
    "gpt-4o-mini": (0.15, 0.075, 0.60),
    "gpt-5-nano": (0.05, 0.005, 0.40),
}
# Modelos de razonamiento: solo a estos se les manda `reasoning`; los demás (gpt-4o-mini) responden 400 si lo reciben.
REASONING_MODELS = {"gpt-6-luna", "gpt-5-nano"}
SYSTEM = (
    "Sos reclutador técnico en Argentina. Vas a recibir el CV de un candidato y una oferta de Computrabajo. "
    "Puntuá de 0 a 100 qué tan bien encaja ese CV con la oferta, usando solo lo que dicen los textos: no supongas "
    "experiencia que no figura en el CV, y no des por cumplido un requisito de la oferta que el CV no muestra. "
    "90-100: cumple todos los requisitos excluyentes. 70-89: "
    "cumple los principales y le falta algo deseable. 40-69: encaje parcial. 0-39: otro rol, otra seniority o un "
    "requisito excluyente que no cumple. Razones: hasta 3, cortas, en español. "
    "Faltantes: lo que la oferta pide y el CV no muestra. Excluyentes que no cumple: solo los requisitos que la oferta "
    "marca como excluyentes, obligatorios o «requisito indispensable» y que el CV no muestra; si la oferta no marca "
    "ninguno así, dejá la lista vacía. Esa lista no cambia el puntaje: puntuá como siempre."
)


# Oferta de ejemplo inventada para `probe`: no es una oferta real ni tiene datos de nadie.
PROBE_OFFER = {
    "title": "Desarrollador/a Frontend React",
    "company": "Empresa de ejemplo",
    "location": "Ciudad Autónoma de Buenos Aires",
    "employment_type": "Híbrido",
    "description": (
        "Buscamos una persona para desarrollar y mantener interfaces web con React.\n"
        "Requisitos:\n"
        "- Experiencia comprobable con React y JavaScript moderno (ES6+).\n"
        "- Conocimientos de HTML5, CSS3 y diseño responsivo.\n"
        "- Manejo de Git y trabajo con pull requests.\n"
        "- Consumo de APIs REST y manejo de estado en el cliente.\n"
        "- Deseable: TypeScript y tests automáticos de componentes.\n"
        "- Capacidad de trabajo en equipo y comunicación clara.\n"
        "Modalidad híbrida: tres días por semana en la oficina."
    ),
}


def load_env(path: Path = Path(".env")) -> None:
    """Carga las líneas CLAVE=valor de `path` en el entorno, sin pisar lo que ya está definido."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


def load_settings(env_path: Path = Path(".env")) -> str:
    """Carga `.env`, exige OPENAI_API_KEY y devuelve el modelo (OPENAI_MODEL o el default)."""
    load_env(env_path)
    if not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError(
            "Falta OPENAI_API_KEY. Definila en el entorno o en un archivo .env con la línea OPENAI_API_KEY=..."
        )
    return os.environ.get("OPENAI_MODEL") or DEFAULT_MODEL


def cost_usd(model: str, input_tokens: int, cached_tokens: int, output_tokens: int) -> float:
    """Costo en USD de una llamada, según `PRICES`."""
    p_in, p_cache, p_out = PRICES[model]
    return ((input_tokens - cached_tokens) * p_in + cached_tokens * p_cache + output_tokens * p_out) / 1_000_000


def month_spent(conn: sqlite3.Connection, today_utc: date) -> float:
    """Gasto estimado en OpenAI en el mes de `today_utc` (suma de `scores.cost_usd` y de `api_spend.cost_usd`).

    El mes de cada puntaje o respuesta es el de su fecha UTC (así factura OpenAI), por eso `today_utc` tiene que ser la fecha de UTC.
    """
    month = f"{today_utc:%Y-%m}"
    spent = conn.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM scores WHERE substr(scored_at, 1, 7) = ?", (month,)).fetchone()[0]
    other = conn.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM api_spend WHERE substr(at, 1, 7) = ?", (month,)).fetchone()[0]
    return float(spent) + float(other)


def money(value: float, decimals: int = 4) -> str:
    """Monto en dólares con coma decimal, como se escribe en español: «US$ 0,0004»."""
    return f"US$ {value:.{decimals}f}".replace(".", ",")


def amounts(spent: float, cap: float) -> str:
    """«US$ X de US$ Y» de los mensajes del tope mensual: lo gastado con 4 decimales y el tope con 2."""
    return f"{money(spent)} de {money(cap, 2)}"


def api_error_detail(e: openai.OpenAIError) -> str:
    """«code en param» de un error de la API (el `type` si no hay `code`); vacío si no trae nada.

    Solo campos estructurados: nunca el texto del mensaje, que con una clave mala puede traer parte de ella.
    """
    fields = [getattr(e, name, None) for name in ("code", "type", "param")]
    code, kind, param = (value if isinstance(value, str) and value else "" for value in fields)
    label = code or kind
    return f"{label} en {param}" if label and param else label


def schema(ids: list[str]) -> dict:
    """JSON Schema de la respuesta, con los ids de las variantes como enum."""
    variant = {"type": "string", "enum": ids}
    strings = {"type": "array", "items": {"type": "string"}}
    entry = {
        "type": "object",
        "properties": {"variant_id": variant, "score": {"type": "integer"}},
        "required": ["variant_id", "score"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "best_variant": variant,
            "scores": {"type": "array", "items": entry},
            "reasons": strings,
            "missing": strings,
            "unmet_required": strings,
        },
        "required": ["best_variant", "scores", "reasons", "missing", "unmet_required"],
        "additionalProperties": False,
    }


def is_excluded(profile: dict, title: str | None, company: str | None) -> bool:
    """Empresa excluida (igualdad sin mayúsculas ni espacios extra) o palabra excluida en el título."""
    def norm(text: str) -> str:
        return " ".join(text.split()).casefold()

    name = norm(company or "")
    if name and any(name == norm(c) for c in profile.get("exclude_companies", [])):
        return True
    return any(
        re.search(rf"\b{re.escape(word.strip())}\b", title or "", re.IGNORECASE)
        for word in profile.get("exclude_title_words", []) if word.strip()
    )


def refresh_exclusions(conn: sqlite3.Connection, profile: dict) -> int:
    """Reaplica los filtros a las ofertas new/excluded; devuelve cuántas pasaron a excluded."""
    newly_excluded = 0
    for id16, title, company, status in conn.execute(
            "SELECT id16, title, company, status FROM offers WHERE status IN ('new', 'excluded')").fetchall():
        excluded = is_excluded(profile, title, company)
        if excluded and status == "new":
            db.mark_status(conn, id16, "excluded")
            newly_excluded += 1
        elif not excluded and status == "excluded":
            db.mark_status(conn, id16, "new")
    return newly_excluded


def refresh_duplicates(conn: sqlite3.Connection) -> int:
    """Marca como `duplicate` las ofertas `new` repetidas; devuelve cuántas pasaron a `duplicate` en esta llamada.

    Repetida = misma empresa (sin mayúsculas ni espacios extra), mismo título y misma descripción (sha1). Solo cuentan
    las ofertas con descripción. La original es la de `first_seen` más viejo (empate: `rowid`); las demás guardan su
    id16 en `dup_of`. Una `duplicate` que ya no repite a nadie (la original desapareció o cambió de descripción)
    vuelve a `new`. Solo toca ofertas `new` y `duplicate`.
    """
    originals: dict[tuple, str] = {}
    changed = 0
    for id16, title, company, description, status, dup_of in conn.execute(
            "SELECT id16, title, company, description, status, dup_of FROM offers "
            "WHERE description IS NOT NULL AND description != '' ORDER BY first_seen, rowid").fetchall():
        key = (" ".join((company or "").split()).casefold(), title, hashlib.sha1(description.encode("utf-8")).hexdigest())
        original = originals.setdefault(key, id16)
        if original == id16:  # la más vieja del grupo no es duplicada de nadie
            if status == "duplicate":
                conn.execute("UPDATE offers SET status = 'new', dup_of = NULL WHERE id16 = ?", (id16,))
        elif status == "new":
            conn.execute("UPDATE offers SET status = 'duplicate', dup_of = ? WHERE id16 = ?", (original, id16))
            changed += 1
        elif status == "duplicate" and dup_of != original:
            conn.execute("UPDATE offers SET dup_of = ? WHERE id16 = ?", (original, id16))
    # Una duplicada sin descripción (se la borraron) no repite a nadie.
    conn.execute("UPDATE offers SET status = 'new', dup_of = NULL WHERE status = 'duplicate' "
                 "AND (description IS NULL OR description = '')")
    conn.commit()
    return changed


def _has_refusal(output) -> bool:
    for item in output or []:
        if getattr(item, "type", None) == "refusal":
            return True
        if any(getattr(part, "type", None) == "refusal" for part in getattr(item, "content", None) or []):
            return True
    return False


def _validate(resp, ids: list[str]) -> dict | None:
    """Resultado validado de la respuesta, o None si no sirve."""
    if getattr(resp, "status", None) != "completed" or _has_refusal(getattr(resp, "output", None)):
        return None
    try:
        data = json.loads(resp.output_text)
    except (AttributeError, TypeError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("scores"), list):
        return None
    all_scores = {}
    for item in data["scores"]:
        if not isinstance(item, dict):
            return None
        variant, value = item.get("variant_id"), item.get("score")
        if variant not in ids or variant in all_scores or type(value) is not int or not 0 <= value <= 100:
            return None
        all_scores[variant] = value
    best = data.get("best_variant")
    if set(all_scores) != set(ids) or not isinstance(best, str) or all_scores.get(best) != max(all_scores.values()):
        return None
    reasons, missing, unmet = data.get("reasons"), data.get("missing"), data.get("unmet_required", [])
    if not all(isinstance(x, list) and all(isinstance(s, str) for s in x) for x in (reasons, missing, unmet)):
        return None
    return {"best_variant": best, "score": all_scores[best], "all_scores": all_scores,
            "reasons": reasons, "missing": missing, "unmet_required": unmet}


def _tokens(resp) -> tuple[int, int, int]:
    """(entrada, entrada cacheada, salida) de `resp.usage`."""
    usage = getattr(resp, "usage", None)
    details = getattr(usage, "input_tokens_details", None)
    return (getattr(usage, "input_tokens", 0) or 0, getattr(details, "cached_tokens", 0) or 0,
            getattr(usage, "output_tokens", 0) or 0)


def _offer_text(title, company, location, employment_type, description) -> str:
    return (
        f"Título: {title or ''}\nEmpresa: {company or ''}\nUbicación: {location or ''}\n"
        f"Tipo de empleo: {employment_type or ''}\nDescripción:\n{(description or '')[:MAX_DESCRIPTION]}"
    )


def _system_prompt(variant: dict) -> str:
    """Instrucciones + el CV de una variante: el prefijo de cada variante es fijo, porque OpenAI cachea el prefijo repetido."""
    return SYSTEM + "\n\n" + f"### CV {variant['id']}\n{resume_for_llm(variant['resume'])}"


def _fold(text: str) -> str:
    """Para comparar keywords: sin mayúsculas, acentos ni espacios de más."""
    plain = "".join(ch for ch in unicodedata.normalize("NFD", text) if not unicodedata.combining(ch))
    return " ".join(plain.casefold().split())


def candidate_variants(profile: dict, keyword: str | None) -> list[dict]:
    """Las variantes que se puntúan para una oferta: las que tienen su `keyword` (sin distinguir mayúsculas ni acentos).

    Si ninguna la tiene (una keyword que ya no está en el perfil, o `scan --keyword`), van todas.
    """
    wanted = _fold(keyword or "")
    found = [v for v in profile["variants"] if wanted and any(_fold(k) == wanted for k in v["keywords"])]
    return found or list(profile["variants"])


def ask_json(client, model: str, system: str, user: str, name: str, json_schema: dict, tail: str = "",
             max_tokens: int = MAX_OUTPUT_TOKENS):
    """Pedido a la API con respuesta en JSON estricto; `tail` cierra el texto de los errores.

    Lo usan el puntaje (`score_pending`, `probe`) y el armado de CV: así comparten `reasoning` o `temperature`.
    """
    # Los modelos de razonamiento no aceptan `temperature`; a los demás se les pide 0 para que el mismo texto dé el mismo puntaje.
    extra = {"reasoning": {"effort": "low"}} if model in REASONING_MODELS else {"temperature": 0}
    try:
        return client.responses.create(
            model=model,
            **extra,
            max_output_tokens=max_tokens,
            input=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            text={"format": {"type": "json_schema", "name": name, "strict": True, "schema": json_schema}},
        )
    except openai.APIStatusError as e:  # no se cita el mensaje de la API: puede traer parte de la clave
        hint = " Revisá OPENAI_API_KEY y el crédito de la cuenta." if e.status_code in (401, 403, 429) else ""
        detail = f": {why}" if (why := api_error_detail(e)) else ""
        raise RuntimeError(f"La API de OpenAI respondió {e.status_code} ({type(e).__name__}){detail}.{hint}{tail}") from None
    except openai.APIConnectionError as e:
        raise RuntimeError(f"No se pudo conectar con la API de OpenAI ({type(e).__name__}). Revisá la conexión.{tail}") from None


def _ask(client, model: str, system: str, offer: str, ids: list[str], tail: str):
    """El pedido de puntaje (el mismo para `score_pending` y `probe`)."""
    return ask_json(client, model, system, offer, "puntaje", schema(ids), tail)


def probe(client, model: str, profile: dict) -> dict:
    """Puntúa `PROBE_OFFER` contra cada variante del perfil (un pedido por variante) con el mismo pedido y la misma validación que `score_pending`.

    No escribe en la base. Devuelve best_variant, score (el mayor), all_scores (el de cada variante), reasons (las del mejor),
    input_tokens, cached_tokens, output_tokens y cost_usd (sumados). Lanza RuntimeError si la API falla o una respuesta no sirve.
    """
    if model not in PRICES:
        raise RuntimeError(f"No tengo el precio del modelo '{model}': agregá su precio en score.PRICES.")
    results, total = {}, [0, 0, 0]
    for variant in profile["variants"]:
        ids = [variant["id"]]
        resp = _ask(client, model, _system_prompt(variant), _offer_text(**PROBE_OFFER), ids, "")
        status = getattr(resp, "status", None)
        result = _validate(resp, ids)
        if result is None:
            if status != "completed":
                why = f"la respuesta quedó '{status}' en vez de 'completed' (¿se cortó antes de terminar?)"
            elif _has_refusal(getattr(resp, "output", None)):
                why = "el modelo rechazó el pedido"
            else:
                why = "la respuesta no cumple el formato JSON esperado (puntaje entero de 0 a 100)"
            raise RuntimeError(f"La prueba de puntaje falló con {model}: {why}.")
        total = [a + b for a, b in zip(total, _tokens(resp))]
        results[variant["id"]] = result
    best = max(results, key=lambda vid: results[vid]["score"])
    return {"best_variant": best, "score": results[best]["score"], "all_scores": {vid: r["score"] for vid, r in results.items()},
            "reasons": results[best]["reasons"], "input_tokens": total[0], "cached_tokens": total[1], "output_tokens": total[2],
            "cost_usd": cost_usd(model, *total)}


def _record_lost(conn: sqlite3.Connection, id16: str, model: str, paid: list) -> None:
    """Anota en `api_spend` lo que costaron los pedidos pagos de una oferta que no llegó a tener puntaje."""
    for cost, tokens in paid:
        db.add_spend(conn, "score", id16, model, cost, tokens[0], tokens[2], False)


def score_pending(conn: sqlite3.Connection, profile: dict, client, model: str, limit: int, max_cost: float,
                  stop_file: Path = Path("STOP"), retry_errors: bool = False, only: set | None = None,
                  extra_variant: str | None = None) -> dict:
    """Filtra y puntúa hasta `limit` ofertas pendientes; devuelve los totales de la corrida.

    Al empezar reaplica los filtros (sin costo): una excluida que ya no coincide vuelve a la cola y una
    nueva que coincide pasa a excluded (`excluded` cuenta estas últimas); y marca las repetidas con
    `refresh_duplicates` (`duplicates` cuenta las que pasaron a `duplicate`, que no se puntúan). Tampoco se puntúan las de
    otra zona (`profile["zones"]`, ver `profile.in_zone`): quedan como están. Con `retry_errors`, las
    `score_error` vuelven a la cola. Cada oferta se puntúa con un pedido por variante candidata (`candidate_variants`),
    cada uno con solo el CV de esa variante; se guarda el mayor puntaje con su variante (y sus razones), `all_scores` con el de
    cada variante pedida, y el costo y los tokens suman todos los pedidos. Cada puntaje se guarda apenas llega. Frena con
    RuntimeError ante `STOP`, un error de la API o 3 ofertas con respuesta inválida seguidas; corta normal al llegar a
    `max_cost` (`stopped_by_cost`), controlado antes de cada pedido. Una oferta que se corta, falla o frena a mitad de sus
    pedidos queda sin puntaje (en la cola, o `score_error` si una respuesta no sirvió) y lo que costaron sus pedidos pagos se
    anota en `api_spend` (`kind = 'score'`, `ok = 0`) para que cuente en el tope mensual.
    Con `only` (un conjunto de `id16`) solo se puntúan esas (la tanda); con `extra_variant`, esa variante se puntúa siempre, aunque la keyword
    de la oferta no sea la suya.
    """
    if model not in PRICES:
        raise RuntimeError(f"No tengo el precio del modelo '{model}': agregá su precio en score.PRICES.")
    if limit < 0:
        raise RuntimeError("--limit no puede ser negativo.")
    if retry_errors:
        conn.execute("UPDATE offers SET status = 'new' WHERE status = 'score_error'")
        conn.commit()
    excluded = refresh_exclusions(conn, profile)
    duplicates = refresh_duplicates(conn)
    systems = {v["id"]: _system_prompt(v) for v in profile["variants"]}  # un prefijo fijo por variante: OpenAI cachea lo repetido
    pending = conn.execute(
        "SELECT id16, title, company, location, employment_type, description, keyword FROM offers o "
        "WHERE status = 'new' AND description IS NOT NULL AND description != '' "
        "AND NOT EXISTS (SELECT 1 FROM scores s WHERE s.id16 = o.id16 AND s.model = ? AND s.prompt_version = ?) "
        "ORDER BY first_seen, rowid",
        (model, PROMPT_VERSION),
    ).fetchall()
    zones = profile.get("zones")
    pending = [row for row in pending if in_zone(row[3], zones) and (only is None or row[0] in only)]  # las de otra zona no se puntúan ni cuestan nada
    totals = {"scored": 0, "excluded": excluded, "duplicates": duplicates, "errors": 0, "cost_usd": 0.0, "input_tokens": 0, "output_tokens": 0,
              "stopped_by_cost": False}
    errors_in_a_row = 0
    for id16, title, company, location, employment_type, description, keyword in pending:
        if totals["scored"] + totals["errors"] >= limit:
            break
        offer = _offer_text(title, company, location, employment_type, description)
        results, paid, failed, cut = {}, [], False, False
        try:
            candidates = candidate_variants(profile, keyword)
            if extra_variant and not any(v["id"] == extra_variant for v in candidates):
                candidates = [*candidates, next(v for v in profile["variants"] if v["id"] == extra_variant)]
            for variant in candidates:
                if stop_file.exists():
                    raise RuntimeError(
                        f"Existe el archivo {stop_file}: se frena la corrida antes de puntuar la oferta {id16}. "
                        "Borralo para volver a correr."
                    )
                if totals["cost_usd"] >= max_cost:
                    cut = True
                    break
                resp = _ask(client, model, systems[variant["id"]], offer, [variant["id"]],
                            " Se frena la corrida; lo ya puntuado quedó guardado.")
                tokens = _tokens(resp)
                cost = cost_usd(model, *tokens)
                paid.append((cost, tokens))
                totals["cost_usd"] += cost
                totals["input_tokens"] += tokens[0]
                totals["output_tokens"] += tokens[2]
                result = _validate(resp, [variant["id"]])
                if result is None:
                    failed = True
                    break
                results[variant["id"]] = result
        except RuntimeError:
            _record_lost(conn, id16, model, paid)
            raise
        if cut:  # sin puntaje a medias: la oferta sigue en la cola
            _record_lost(conn, id16, model, paid)
            totals["stopped_by_cost"] = True
            break
        if failed:
            _record_lost(conn, id16, model, paid)
            db.mark_status(conn, id16, "score_error")
            totals["errors"] += 1
            errors_in_a_row += 1
            if errors_in_a_row == MAX_ERRORS_IN_A_ROW:
                raise RuntimeError(
                    f"{MAX_ERRORS_IN_A_ROW} respuestas inválidas seguidas del modelo: se frena la corrida. "
                    "Las ofertas quedaron como score_error."
                )
            continue
        best = max(results, key=lambda vid: results[vid]["score"])  # a igual puntaje, la primera variante del perfil
        db.save_score(conn, id16, model, PROMPT_VERSION,
                      {**results[best], "all_scores": {vid: r["score"] for vid, r in results.items()}},
                      tuple(sum(t[i] for _, t in paid) for i in range(3)), sum(c for c, _ in paid))
        totals["scored"] += 1
        errors_in_a_row = 0
    return totals
