"""`apply`: qué ofertas se postularían y cuáles se saltarían (modo seco), y la postulación en vivo (`--live`), con el informe."""
import json
import random
import re
import shutil
import sqlite3
import tempfile
import time
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from compuapply import browser, cv, db, score
from compuapply.profile import (DEFAULT_COMPANY_DAILY_CAP, DEFAULT_FREE_CV_SLOTS, DEFAULT_MAX_ACCOUNT_PDFS, DEFAULT_REVIEW_CV, in_zone, normalize_question, province,
                                resume_for_llm)
from compuapply.score import PROMPT_VERSION

SEND_REASON = "cumple las reglas"
NOTE = "Las preguntas del formulario se verifican al postular (Fase 4)."
COLUMNS = ("oferta", "empresa", "puntaje", "CV", "motivo", "url")


def applied_today(conn: sqlite3.Connection, today: date) -> int:
    """Postulaciones `applied` cuyo `applied_at` cae hoy en hora local (las fechas con zona se convierten)."""
    count = 0
    for (applied_at,) in conn.execute("SELECT applied_at FROM applications WHERE status IN ('applied', 'unconfirmed')"):
        try:
            when = datetime.fromisoformat(applied_at)
        except (TypeError, ValueError):
            continue
        if (when.astimezone() if when.tzinfo else when).date() == today:
            count += 1
    return count


def company_key(company) -> str:
    """El nombre de la empresa sin mayúsculas, acentos ni espacios de más; "" si no se sabe o es confidencial (esas no cuentan entre sí)."""
    folded = score._fold(str(company or ""))
    return "" if folded in {score._fold(label) for label in browser.CONFIDENTIAL} else folded


def applied_by_company_today(conn: sqlite3.Connection, today: date) -> Counter:
    """Cuántas postulaciones `applied` de hoy (hora local) tiene cada empresa (`company_key`); las confidenciales no entran."""
    counts = Counter()
    for applied_at, company in conn.execute("SELECT a.applied_at, o.company FROM applications a LEFT JOIN offers o ON o.id16 = a.id16 "
                                            "WHERE a.status IN ('applied', 'unconfirmed')"):
        try:
            when = datetime.fromisoformat(applied_at)
        except (TypeError, ValueError):
            continue
        if (when.astimezone() if when.tzinfo else when).date() == today and company_key(company):
            counts[company_key(company)] += 1
    return counts


def _expired(valid_through: str | None, today: date) -> bool:
    try:
        return date.fromisoformat((valid_through or "")[:10]) < today
    except ValueError:  # sin fecha o ilegible: no se la da por vencida
        return False


def plan(conn: sqlite3.Connection, profile: dict, model: str, today: date, only: set | None = None, variant: str | None = None,
         cap: int | None = None, company_cap_on: bool = True) -> dict:
    """Reparte las ofertas puntuadas en `send` (se postularía) y `skip` (se saltaría, con motivo).

    Además `scored` (cuántos puntajes hay con `model` y el `PROMPT_VERSION` actual), `model`, `calibration` (el aviso
    de `calibration_note`, o None), `zones` (`profile["zones"]`) y `out_of_zone` (cuántas ofertas nuevas quedan fuera de
    esas zonas, puntuadas o no), para que el plan avise.
    Motivos, el primero que aplique: ya postulada, fuera de zona («fuera de zona (<provincia>)», con o sin puntaje),
    vencida, puntaje bajo, variante desconocida, tope por empresa (`apply.company_daily_cap` postulaciones por día a la misma
    empresa; las confidenciales no cuentan entre sí), tope diario.
    `send` queda agrupado por variante de CV y, dentro de cada una, de mayor a menor puntaje.
    Una tanda (`only` = los `id16` que aparecieron en sus listados, `variant` = el CV elegido, `cap` = cuántas como máximo, en lugar del
    tope diario): solo toma esas ofertas y las puntúa con el puntaje de esa variante (`all_scores`); si es menor al umbral, el motivo es
    «puntaje bajo para <variante> (p < umbral)»; sin puntaje de esa variante, «sin puntaje para <variante>». Con `company_cap_on` en
    False (la tanda lo trae apagado por defecto) no hay tope por empresa.
    """
    threshold, daily_cap = profile["apply"]["threshold"], profile["apply"]["daily_cap"] if cap is None else cap
    variant_order = {v["id"]: n for n, v in enumerate(profile["variants"])}
    applied = {row[0] for row in conn.execute("SELECT id16 FROM applications WHERE status = 'applied'")}
    room = max(0, daily_cap - (applied_today(conn, today) if cap is None else 0))
    company_cap = profile["apply"].get("company_daily_cap", DEFAULT_COMPANY_DAILY_CAP)
    by_company = applied_by_company_today(conn, today)  # las de hoy y, al recorrer, las que este plan va a mandar
    zones = profile.get("zones") or []
    send, skip = [], []
    for id16, title, company, url, valid_through, points, best, location, all_scores in conn.execute(
            "SELECT o.id16, o.title, o.company, o.url, o.valid_through, s.score, s.best_variant, o.location, s.all_scores "
            "FROM offers o JOIN scores s ON s.id16 = o.id16 AND s.model = ? AND s.prompt_version = ? "
            "WHERE o.status = 'new' ORDER BY s.score DESC, o.first_seen, o.rowid", (model, PROMPT_VERSION)).fetchall():
        if only is not None and id16 not in only:
            continue
        low = f"puntaje bajo ({points} < {threshold})"
        variant_ = best
        if variant:  # tanda: el puntaje es el de la variante elegida
            variant_ = variant
            points = json.loads(all_scores or "{}").get(variant)
            low = f"puntaje bajo para {variant} ({points} < {threshold})"
        if id16 in applied:
            reason = "ya postulada"
        elif not in_zone(location, zones):
            reason = f"fuera de zona ({province(location) or 'sin lugar'})"
        elif _expired(valid_through, today):
            reason = "vencida"
        elif points is None:
            reason = f"sin puntaje para {variant}"
        elif points < threshold:
            reason = low
        elif variant_ not in variant_order:
            reason = f"variante desconocida ({variant_})"
        elif company_cap_on and company_key(company) and by_company[company_key(company)] >= company_cap:
            reason = "tope por empresa"
        elif len(send) >= room:
            reason = "tope diario"
        else:
            reason = None
            by_company[company_key(company)] += 1
        item = {"id16": id16, "title": title, "company": company, "score": points, "variant": variant_,
                "reason": reason or SEND_REASON, "url": url}
        (skip if reason else send).append(item)
    send.sort(key=lambda item: variant_order[item["variant"]])  # estable: el orden por puntaje se mantiene
    scored = conn.execute("SELECT COUNT(*) FROM scores WHERE model = ? AND prompt_version = ?",
                          (model, PROMPT_VERSION)).fetchone()[0]
    out_of_zone = sum(1 for (location,) in conn.execute("SELECT location FROM offers WHERE status = 'new'") if not in_zone(location, zones))
    return {"send": send, "skip": skip, "scored": scored, "model": model, "calibration": calibration_note(profile, model),
            "zones": zones, "out_of_zone": out_of_zone}


def calibration_note(profile: dict, model: str) -> str | None:
    """Aviso si el umbral no salió de la calibración o se calibró con otro modelo o versión (si coincide, None)."""
    threshold, calibrated = profile["apply"]["threshold"], profile["apply"].get("calibrated_with")
    now = f"{model} {PROMPT_VERSION}"
    if not calibrated:
        return f"El umbral {threshold} no salió de la calibración: etiquetá en Calibrar y usá el umbral sugerido."
    if calibrated != now:
        return f"El umbral {threshold} se calibró con {calibrated}; ahora se puntúa con {now}: calibrá de nuevo antes de postular."
    return None


def plan_notes(plan_: dict) -> list[str]:
    """Avisos del plan: cuántas ofertas quedaron fuera de las zonas (si hay `zones`), «todavía no hay ofertas puntuadas» (con el
    modelo y el prompt actuales) y el de `calibration_note`."""
    notes = []
    if plan_.get("zones"):
        notes.append(f"Ofertas nuevas fuera de zona: {plan_['out_of_zone']} (zonas: {', '.join(plan_['zones'])}). No se les baja el "
                     "detalle, no se puntúan, no se calibran ni se postula.")
    if not plan_["scored"]:
        notes.append(f"Todavía no hay ofertas puntuadas con {plan_['model']}: puntuá primero (Correr → Puntuar pendientes, "
                     "o una corrida con score_limit mayor que 0).")
    if plan_["calibration"]:
        notes.append(plan_["calibration"])
    return notes


ARCHIVE_DAYS = 30  # las copias de cv/postulaciones/ y los CV de la cuenta de más de estos días se pueden borrar
ARCHIVE_DIR = Path("cv/postulaciones")


def prune_archive(archive_dir: Path, days: int = ARCHIVE_DAYS, now: float | None = None) -> int:
    """Borra de `archive_dir` (solo esa carpeta, sin entrar en subcarpetas) los PDF con fecha de modificación de más de `days` días.
    Devuelve cuántos borró; si la carpeta no existe, 0."""
    if not archive_dir.is_dir():
        return 0
    limit = (time.time() if now is None else now) - days * 86400
    removed = 0
    for path in archive_dir.iterdir():
        if path.is_file() and path.suffix.lower() == ".pdf" and path.stat().st_mtime < limit:
            path.unlink()
            removed += 1
    return removed


def archive_lines(plan_: dict) -> list[str]:
    """La línea del informe sobre las copias viejas de `cv/postulaciones/` (si esta corrida las revisó)."""
    pruned = plan_.get("archive_pruned")
    return [] if pruned is None else [f"- Copias de más de {ARCHIVE_DAYS} días borradas de cv/postulaciones/: {pruned}"]


def summary(plan_: dict) -> str:
    """Línea de terminal: cuántas se enviarían y cuántas se saltarían, por motivo (el aviso de `plan_notes` va aparte)."""
    counts = Counter(("puntaje bajo" if item["reason"].startswith("puntaje bajo") else item["reason"].split(" (")[0]) for item in plan_["skip"])
    parts = [f"{reason}: {counts[reason]}" for reason in ("ya postulada", "vencida", "puntaje bajo", "tope diario")]
    for extra in ("variante desconocida", "fuera de zona", "tope por empresa"):
        if counts[extra]:
            parts.append(f"{extra}: {counts[extra]}")
    return f"Enviaría: {len(plan_['send'])} · saltaría: {len(plan_['skip'])} ({' · '.join(parts)})"


def company_cap_lines(meta: dict) -> list[str]:
    """Línea del informe con el tope por empresa (vacía si el `meta` no lo trae)."""
    cap = meta.get("company_daily_cap")
    return [f"- Tope por empresa: {cap} por día a la misma empresa (las confidenciales no cuentan entre sí)"] if cap else []


def cell(value) -> str:
    """Texto de una celda de tabla Markdown: una línea, con `\\` y `|` escapados."""
    text = " ".join(str("" if value is None else value).split())
    return text.replace("\\", "\\\\").replace("|", "\\|")


def _table(items: list[dict]) -> list[str]:
    lines = ["| " + " | ".join(COLUMNS) + " |", "|" + "---|" * len(COLUMNS)]
    for item in items:
        cells = (item["title"], item["company"], item["score"], item["variant"], item["reason"], item["url"])
        lines.append("| " + " | ".join(cell(c) for c in cells) + " |")
    if not items:
        lines += ["", "_Ninguna._"]
    return lines


def plan_sections(plan_: dict, level: int = 2) -> list[str]:
    """Líneas Markdown de las tablas «Enviaría» y «Saltaría», con títulos del nivel `level` (2 = `##`)."""
    heading = "#" * level
    return [
        *[line for note in plan_notes(plan_) for line in (note, "")],
        f"{heading} Enviaría ({len(plan_['send'])})",
        "",
        *_table(plan_["send"]),
        "",
        f"{heading} Saltaría ({len(plan_['skip'])})",
        "",
        *_table(plan_["skip"]),
    ]


def write_report(plan_: dict, meta: dict, out_dir: Path = Path("data/runs")) -> Path:
    """Escribe el informe en `out_dir/AAAAMMDD-HHMMSS-apply-seco.md` y devuelve su ruta.

    `meta`: now (datetime), threshold, daily_cap, applied_today y model. No incluye datos del CV.
    """
    now = meta["now"]
    lines = [
        "# Informe de apply (modo seco)",
        "",
        f"- Fecha: {now:%Y-%m-%d %H:%M:%S}",
        "- Modo: seco (no se envió ni se cambió nada en Computrabajo)",
        f"- Umbral: {meta['threshold']}",
        f"- Tope diario: {meta['daily_cap']} (ya postuladas hoy: {meta['applied_today']})",
        *company_cap_lines(meta),
        f"- Modelo: {meta['model']}",
        *archive_lines(plan_),
        "",
        NOTE,
        "",
        *plan_sections(plan_),
        "",
    ]
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{now:%Y%m%d-%H%M%S}-apply-seco.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


# ── respuestas a las preguntas de selección ──────────────────────────────────

MAX_OPEN_ANSWER = 500
NO_KEY = "Falta la clave de OpenAI: hace falta para contestar las preguntas de selección (python -m compuapply setup env)."
ANSWER_PROMPT_VERSION = db.ANSWER_PROMPT_VERSION  # sube cuando cambian las instrucciones: las pendientes de otra versión vuelven a la cola
ANSWER_SYSTEM = (
    "Contestás las preguntas de selección de una oferta de trabajo en nombre de un candidato. Recibís el CV del candidato (el CV "
    "a medida de esta postulación), los puestos del CV base con su índice y su duración ya calculada, "
    "las respuestas que el candidato declaró (`salary` = remuneración pretendida, `availability` = disponibilidad, "
    "`modality` = las modalidades de trabajo que el candidato acepta, una o más de presencial, híbrido y remoto, `start` = "
    "cuándo puede empezar, `location` = barrio y localidad donde vive, `mobility` = movilidad o transporte, "
    "`salary_part_time` y `salary_daily` = pretensión de media jornada y por día, y respuestas a preguntas puntuales) y las "
    "preguntas, cerradas (con opciones numeradas desde 0) o abiertas. Una pregunta de experiencia, de tareas, de estudios o de "
    "situación laboral se contesta desde el CV, también en negativo: si el CV no muestra esa experiencia, decilo, por ejemplo «No tengo "
    "experiencia en call center; sí en atención al público y ventas en …», con `source` «cv». "
    "`unknown` es solo para datos personales que no están ni en el CV ni en las respuestas declaradas ni en los hechos ciertos. "
    "Los hechos ciertos del candidato (banco de verdades) valen tanto como el CV: una pregunta de si tiene un título, un documento, una "
    "vacuna, un conocimiento o una condición se contesta con ellos, también en negativo si el hecho lo dice, con `source` «truths». "
    "Una pregunta de sí o no de si vive o reside en un lugar o zona se contesta con `location`, con `source` «answers»: sí solo si el "
    "lugar nombrado es el mismo o lo incluye con certeza, no solo si `location` dice con claridad otro lugar fuera de esa zona; si no "
    "se puede decidir con certeza, `unknown`; nunca inventes dónde vive. "
    "Una pregunta de sí o no de si tiene al menos N años de experiencia en algo se contesta con el CV y la duración calculada de los "
    "puestos: sí solo si el CV lo muestra. En una pregunta (cerrada de sí o no, o abierta) que pida «al menos / más de / N o más» años o "
    "meses de algo, devolvé en `jobs` los índices de los puestos que cuentan: si sus meses suman menos que lo pedido, la respuesta es no, "
    "nunca sí con la duración real («tengo 7 meses» no es «al menos 1 año»). "
    "Si la respuesta dice lo mismo que un hecho cierto (trabajar hoy, carnet, libreta, estudios, vacunas), `source` es «truths», nunca «answers» "
    "ni «cv». «truths» no es un comodín: solo si un hecho del banco dice eso mismo, con sus palabras y sin años ni temas que no diga; si ninguno lo dice, `unknown`. "
    "Respondé solo lo que se pregunta, en una o dos oraciones; no vuelques otros hechos del banco.Un sí a «¿tenés experiencia en X?» solo si el CV nombra X (no alcanza con otra experiencia parecida): si no, es no. "
    "`source` tiene que ser verdad: «answers» solo si la respuesta sale de una clave de las respuestas declaradas que podrías nombrar (nunca para "
    "una pregunta cuyo tema no está entre ellas); «cv» solo si es un no por algo que el CV muestra (sector, empleo registrado) o un sí con un puesto "
    "del CV; una pregunta de actitud (sociable, objetivos, trabajo en equipo, vía pública) con sí lleva `source` «actitud»; cualquier otra "
    "cosa que el CV y las respuestas no dicen («¿sos monotributista?», el nivel de manejo de una tecnología) es `unknown`. "
    "Entre varias opciones negativas, no elijas la que agrega algo que ningún hecho ni respuesta declarada dice («no cuento con "
    "disponibilidad», «no estoy dispuesto», «no me interesa», «debo actualizarla» si el hecho dice que nunca lo tuvo): elegí la que sí se "
    "apoya en los hechos; si ninguna, `unknown`. "
    "En una pregunta cerrada de cuánta experiencia (años o meses) tiene el candidato, devolvé en `jobs` los índices de los puestos que "
    "cuentan para esa experiencia (una lista vacía si ninguno), y `option` -1: el programa suma los meses con las fechas del CV y elige "
    "la opción; en las demás preguntas `jobs` es una lista vacía (salvo las de «al menos N»). Si una pregunta es por una modalidad de trabajo que está "
    "en la lista de `modality`, la respuesta es sí; si es por una modalidad que no está en la lista, la respuesta es no. Lo "
    "que no sea una modalidad (la zona, el traslado, el horario) no se contesta con `modality`. Excepción: una pregunta de si tiene "
    "disponibilidad para trabajar presencial en la sede o en la zona de la oferta se contesta con `modality` (sí si incluye presencial, "
    "no si no) cuando el pedido dice que la oferta está dentro de la zona del candidato; si pide mudarse, viajar a otra ciudad o "
    "cambiar de provincia, no se contesta así. "
    "Una pregunta de si le interesa la vacante, el puesto, la modalidad o el contrato que la oferta publica («¿te interesa…?», «¿querés…?») "
    "se contesta siempre que sí: postularse y decir que no interesa descarta al candidato. Si elegís entre sí y no, devolvé la opción que "
    "empieza con esa palabra («Sí, puedo» es sí). "
    "Si una pregunta cerrada pide elegir un turno u horario de trabajo (por ejemplo mañana o tarde, o una franja de horas), "
    "elegí la primera opción que entre en `availability`, con `source` «answers»; si entran varias, la primera de la lista; si "
    "no entra ninguna, `unknown`. Nunca inventes una disponibilidad que el candidato no declaró. "
    "Una opción cerrada con cifras (dígitos, o un número en palabras junto a una unidad como año, mes, hora o peso) que no sea de "
    "cuánta experiencia solo se elige con `source` «answers», si el candidato declaró la respuesta a esa misma pregunta (o la "
    "pretensión que corresponde, en una pregunta de sueldo o remuneración); nunca la elijas por lo que dice el CV: si no la declaró, `unknown`. "
    "Estilo de toda respuesta abierta: escribila como la escribiría el postulante, en primera persona, en español rioplatense neutro y en "
    "pretérito simple («trabajé», «atendía», nunca «he trabajado» ni «he desarrollado»). De 1 a 3 oraciones cortas, con un dato concreto del CV "
    "por oración. No repitas la pregunta («Sí, tengo experiencia en X…»), no cierres con frases motivacionales ni de plantilla («brindar un "
    "excelente servicio», «servicio personalizado», «garantizar su satisfacción», «fomentar la fidelización», «Además,», «me apasiona», «cabe "
    "destacar»), no agregues relleno inventado (cómo es el barrio, el transporte) y no escribas nada que no responda lo preguntado. "
    "Las preguntas de opinión (por qué te gustaría ser parte de la empresa, tu motivación, qué te diferencia, qué técnicas usás, cómo "
    "manejarías una situación, contanos sobre vos, contanos una situación o un logro, expectativas, cómo te ves en N años, cómo te describís, "
    "cómo te ven, por qué deberíamos contratarte, fortalezas, debilidades, qué te gusta) las escribís vos con `source` «opinión», nunca `unknown`: salen del CV y de lo que la "
    "oferta dice, y no afirmás hechos que el CV no tenga (empresas, cifras, títulos). Una situación sale de un puesto del CV, sin cifras, nombres "
    "ni resultados que el CV no tenga. Si una pregunta trae «Hecho cierto del banco para esta pregunta», contestala con ese hecho y `source` «truths»; "
    "no devuelvas `unknown`. "
    "Respondé SOLO con lo que dicen el CV o esas respuestas "
    "declaradas: no inventes experiencia, años, herramientas, cifras, sueldos ni disponibilidad. Para cada pregunta devolvé su "
    "`index` (el número de la lista), `source` («cv» si sale del CV, «answers» si sale de las respuestas declaradas, «truths» si sale "
    "de los hechos ciertos, «actitud» si es una pregunta de actitud con sí, «opinión» si es una pregunta de opinión, «unknown» si ninguna alcanza para contestar con certeza), y: en una cerrada, `option` (el número de la opción elegida; "
    "-1 si es unknown) y `text` vacío; en una abierta, `text` (hasta 500 caracteres, en primera persona, con las palabras del "
    "CV o de las respuestas declaradas) y `option` -1. En una abierta nunca dejes `text` vacío si el CV contesta la pregunta. Una cifra "
    "(sueldo, años) solo puede salir de las respuestas declaradas, de la duración calculada de un puesto o de las fechas y años que "
    "figuran en el CV. Ante la duda sobre un dato personal, `unknown`."
)


ANSWER_SOURCES = ("cv", "answers", "truths", "actitud", "opinión")  # de dónde puede salir una respuesta contestada (si no, «unknown»)


class AnswerError(RuntimeError):
    """La respuesta del modelo a las preguntas no sirve."""


def _answers_schema() -> dict:
    item = {"type": "object", "properties": {"index": {"type": "integer"}, "option": {"type": "integer"}, "text": {"type": "string"},
                                              "source": {"type": "string", "enum": ["cv", "answers", "truths", "actitud", "opinión", "unknown"]},
                                              "jobs": {"type": "array", "items": {"type": "integer"}}},
            "required": ["index", "option", "text", "source", "jobs"], "additionalProperties": False}
    return {"type": "object", "properties": {"answers": {"type": "array", "items": item}}, "required": ["answers"],
            "additionalProperties": False}


NUMBER_WORDS = ("un", "uno", "una", "dos", "tres", "cuatro", "cinco", "seis", "siete", "ocho", "nueve", "diez", "once", "doce", "trece",
                "catorce", "quince", "veinte", "treinta", "cuarenta", "cincuenta", "cien", "ciento", "mil", "medio", "media")
NUMBER_WORD = re.compile(r"\b(?:" + "|".join(NUMBER_WORDS) + r")\b")  # sobre texto sin acentos ni mayúsculas
FIGURE_UNIT = re.compile(r"\b(?:anos?|mes(?:es)?|semanas?|dias?|horas?|hs|veces|vez|pesos?|mil|millon(?:es)?)\b|\$")
NON_HOUR_UNIT = re.compile(r"\b(?:anos?|mes(?:es)?|semanas?|dias?|veces|vez|pesos?|mil|millon(?:es)?)\b|\$")
TIME_SLOT = re.compile(r"\b\d{1,2}(?::\d{2})?\s*(?:hs?\b\.?|horas?\b)?\s*(?:a|al|hasta|-|–)\s*\d{1,2}(?::\d{2})?\s*(?:hs?\b\.?|horas?\b)?")
SHIFT_WORDS = ("turno", "horario", "jornada", "franja")
DURATION_UNIT = re.compile(r"\b(?:anos?|mes(?:es)?)\b")
PAY_WORDS = ("sueldo", "remuneracion", "salario", "salarial", "pretension")
OTHER_BASIS_REASON = "sueldo de otra jornada: contestalo en Preguntas"  # por hora
PART_TIME_WORDS = ("medio tiempo", "media jornada", "part time", "part-time", "parttime")
PART_TIME_HOURS = 6  # una franja de 6 horas o menos es de media jornada
BY_HOUR, BY_DAY = re.compile(r"\bpor horas?\b"), re.compile(r"\bpor dias?\b|\bdiari[oa]s?\b")
LOCATION_WORDS = ("donde vivis", "donde vives", "donde vive", "barrio", "localidad", "domicilio", "residencia")
MOBILITY_WORDS = ("movilidad", "transporte", "te trasladas", "como llegas", "traslado")


def has_figure(text: str) -> bool:
    """Si el texto trae una cifra: dígitos, o un número en palabras (un, uno, una, dos … diez, medio, doce, veinte, cien …) junto a una
    unidad en la misma opción (año, mes, semana, día, hora, hs, vez, peso, $, mil, millón). «Sí, tengo un título» no tiene cifra."""
    folded = score._fold(text)
    if any(ch.isdigit() for ch in folded):
        return True
    return bool(NUMBER_WORD.search(folded)) and bool(FIGURE_UNIT.search(folded))


def is_shift_question(question: dict) -> bool:
    """Si la pregunta habla de turno, horario, jornada o franja."""
    folded = score._fold(question["text"])
    return any(re.search(rf"\b{word}s?\b", folded) for word in SHIFT_WORDS)


def only_time_slots(text: str) -> bool:
    """Si las únicas cifras del texto son franjas horarias («9 a 13», «de 9 a 13 hs», «9-13», «09:00 a 13:00»). Una hora suelta («4 hs») no."""
    folded = score._fold(text)
    rest = TIME_SLOT.sub(" ", folded)
    return rest != folded and not has_figure(rest) and not NON_HOUR_UNIT.search(rest)


def is_pay_question(question: dict) -> bool:
    """Si la pregunta habla de sueldo, remuneración, salario o pretensión."""
    return any(word in score._fold(question["text"]) for word in PAY_WORDS)


MONEY_AFTER = re.compile(r"\b(?:mil|millon(?:es)?|pesos?)\b|\$")


def _slot_hours(folded: str, match) -> float | None:
    """El largo en horas de una coincidencia de `TIME_SLOT` si es una franja horaria de verdad: las dos horas van de 0 a 24, no hay un «$»
    justo antes y las 2 palabras que siguen no son plata («mil», «millón», «millones», «pesos», «$»). «De 1 a 2 millones» no es una franja."""
    found = re.findall(r"(\d{1,2})(?::(\d{2}))?", match.group(0))
    if len(found) < 2:
        return None
    start, end = (int(h) + int(m or 0) / 60 for h, m in found[:2])
    if max(start, end) > 24 or folded[:match.start()].rstrip().endswith("$"):
        return None
    if MONEY_AFTER.search(" ".join(folded[match.end():].split()[:2])):
        return None
    return (end - start) % 24


def pay_key(question: dict) -> str | None:
    """La clave de `answers` que contesta una pregunta de sueldo según la jornada: «por día», «diario» o «por jornada» sin franja ->
    `salary_daily`; medio tiempo, media jornada, part time o una franja de 6 horas o menos -> `salary_part_time`; jornada completa o sin
    indicación -> `salary`. None si es «por hora» (queda pendiente)."""
    folded = score._fold(question["text"])
    if BY_HOUR.search(folded):
        return None
    if BY_DAY.search(folded):
        return "salary_daily"
    if any(word in folded for word in PART_TIME_WORDS):
        return "salary_part_time"
    slots = [h for m in TIME_SLOT.finditer(folded) if (h := _slot_hours(folded, m)) is not None]
    if slots:
        return "salary_part_time" if slots[0] <= PART_TIME_HOURS else "salary"
    return "salary_daily" if "por jornada" in folded else "salary"


def _clean(value) -> str:
    return " ".join(str(value or "").split())


def _pay_hold(question: dict, declared: dict) -> str | None:
    """Por qué una abierta de sueldo queda pendiente sin llamar al modelo: es «por hora», o falta la clave de su jornada (`salary_part_time`
    o `salary_daily`). Si el usuario declaró esa pregunta puntual, no se retiene."""
    if question["kind"] != "open" or not is_pay_question(question) or normalize_question(question["text"]) in declared:
        return None
    key = pay_key(question)
    if key is None:
        return OTHER_BASIS_REASON
    return f"falta {key} en Parámetros" if key != "salary" and not _clean(declared.get(key)) else None


def contact_text(question: dict, declared: dict) -> str | None:
    """La respuesta fija a una abierta de dónde vive (`location`) y/o de movilidad (`mobility`): los textos declarados tal cual. None si
    la pregunta no es de eso, es de sueldo, o falta alguno de los datos que pide."""
    if question["kind"] != "open" or is_pay_question(question):
        return None
    folded = score._fold(question["text"])
    keys = [key for key, words in (("location", LOCATION_WORDS), ("mobility", MOBILITY_WORDS)) if any(w in folded for w in words)]
    texts = [_clean(declared.get(key)) for key in keys]
    if not keys or not all(texts):
        return None
    text = texts[0] if len(texts) == 1 else ". ".join(t.rstrip(". ") for t in texts) + "."
    return text if len(text) <= MAX_OPEN_ANSWER else None


def declared_option(question: dict, declared: dict) -> int | None:
    """El índice de la opción cuyo texto es igual (normalizado) a la respuesta que el usuario declaró para esa pregunta, o None."""
    answer = declared.get(normalize_question(question["text"]))
    if not isinstance(answer, str) or not normalize_question(answer):
        return None
    wanted = normalize_question(answer)
    return next((i for i, option in enumerate(question["options"]) if normalize_question(option) == wanted), None)


def is_duration_question(question: dict) -> bool:
    """Si una pregunta cerrada es de cuánta experiencia hay: «experiencia» junto a años o meses, en la pregunta o en alguna opción."""
    text = score._fold(" ".join([question["text"], *question["options"]]))
    return "experiencia" in text and bool(DURATION_UNIT.search(text))


def is_range_question(question: dict) -> bool:
    """Si es una pregunta de cuánta experiencia que se contesta por rangos de las opciones. Una de sí o no («¿tenés al menos 1 año…?» con
    `Sí` / `No`) no: la contesta el modelo con el CV."""
    yes, no = yes_no_options(question["options"])
    return is_duration_question(question) and not (len(question["options"]) == 2 and yes is not None and no is not None)


def declared_for(question: dict, declared: dict) -> bool:
    """Si el usuario declaró la respuesta de esa pregunta (con la pregunta normalizada como clave) o es de sueldo y cargó `salary`."""
    if normalize_question(question["text"]) in declared:
        return True
    return is_pay_question(question) and bool(_clean(declared.get(pay_key(question) or "")))


def _shift_fits(question: dict, option: int, declared: dict) -> bool:
    """Un turno se acepta con `availability` cargada si la pregunta es de turno u horario, no de experiencia, y las cifras de la opción son franjas."""
    return (bool(declared.get("availability")) and is_shift_question(question) and not is_duration_question(question)
            and only_time_slots(question["options"][option]))


def cv_figures(resume: dict, cv_text: str | None = None) -> set[str]:
    """Los tokens con cifras (años, meses, fechas) del CV de la postulación y de los `employment_period` del CV base."""
    periods = [str(e.get("employment_period") or "") for e in resume.get("experience_details") or [] if isinstance(e, dict)]
    return {token for token in cv._vocabulary([cv_text or "", *periods]) if any(ch.isdigit() for ch in token)}


def _offer_values(offer: dict | None) -> list[str]:
    """El título y la empresa de la oferta: valen en el vocabulario de una abierta («RE/MAX Vincit» no la frena)."""
    return [str(offer.get(k)) for k in ("title", "company") if offer and offer.get(k)]


def _open_text_problems(text: str, resume: dict, declared: dict, durations: tuple = (), cv_text: str | None = None,
                        truths: tuple = (), offer_values: list | None = None) -> list[str]:
    """Por qué un texto abierto no sirve: largo, siglas, tecnologías, niveles (como en el CV a medida) y cifras que no son de `answers`,
    ni de los hechos del banco de verdades (`truths`), ni una duración calculada por puesto (`durations`), ni figuran en el CV de la
    postulación (`cv_text`) o en las fechas del CV base."""
    values, figures = [*declared.values(), *durations, *truths, *(offer_values or [])], cv_figures(resume, cv_text)
    vocab = cv._base_vocabulary(resume, [*values, *sorted(figures)], text)
    problems = cv._line_problems("respuesta", text, MAX_OPEN_ANSWER, vocab, cv._allowed_levels(resume), tenses=False)
    allowed = cv._vocabulary(values) | figures
    numbers = [token for token, _ in cv._words(text) if any(ch.isdigit() for ch in token) and token.lower() not in allowed]
    if numbers:
        problems.append("respuesta: una cifra que no está en tus respuestas declaradas.")
    return problems


def fixed_label(question: dict, declared: dict) -> str:
    """Qué clave de `answers` contesta una abierta tal cual (sueldo, dónde vive, movilidad) o «respuesta declarada a esta pregunta»; nunca el valor."""
    if normalize_question(question["text"]) in declared:
        return "respuesta declarada a esta pregunta"
    if is_pay_question(question):
        return pay_key(question) or "salary"
    folded = score._fold(question["text"])
    return " + ".join(key for key, words in (("location", LOCATION_WORDS), ("mobility", MOBILITY_WORDS)) if any(w in folded for w in words))


def _salary_text(question: dict, declared: dict) -> str | None:
    """La pretensión declarada que corresponde a la jornada (`pay_key`), tal cual, si la pregunta es abierta y de sueldo, y entra en 500 caracteres."""
    if question["kind"] != "open" or not is_pay_question(question) or normalize_question(question["text"]) in declared:
        return None
    text = _clean(declared.get(pay_key(question) or ""))
    return text if text and len(text) <= MAX_OPEN_ANSWER else None


# ── cuánta experiencia: los meses los suma el código ─────────────────────────

MONTH_NAMES = re.compile(r"\b(ene|feb|mar|abr|may|jun|jul|ago|sep|set|oct|nov|dic)[a-z]*\.?\s*(?:de\s+)?(\d{4})")
MONTH_ORDER = ("ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic")
PRESENT = re.compile(r"\b(?:actualidad|presente|actual|actualmente|hoy|la fecha|en curso|present|current)\b")
NUMBER_TOKENS = {"un": 1, "uno": 1, "una": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5, "seis": 6, "siete": 7, "ocho": 8, "nueve": 9,
                 "diez": 10, "doce": 12}
NO_EXPERIENCE = re.compile(r"\b(?:no tengo|no poseo|sin experiencia|ninguna|ninguno|nunca|no cuento)\b")
NUMBER_UNIT = re.compile(r"(\d+(?:[.,]\d+)?)\s*(anos?|meses|mes)?\b")
DURATION_REASON = "el control: pregunta de duración de experiencia sin respuesta declarada"
NO_EXPERIENCE_REASON = "el control: el CV no muestra esa experiencia y ninguna opción dice que no"


def parse_period(period: str, today: date) -> tuple[int, int] | None:
    """(primer mes, último mes) de un `employment_period`, contados como año*12+mes, ambos incluidos. Lee «MM/AAAA - MM/AAAA», «mes AAAA»
    y «… - Actualidad». None si falta un mes (solo el año) o no se entiende: no se cuenta un mes de más."""
    folded = score._fold(period)
    folded = MONTH_NAMES.sub(lambda m: f"{MONTH_ORDER.index(m.group(1)) + 1 if m.group(1) != 'set' else 9}/{m.group(2)}", folded)
    dates = [(int(y), int(m)) for m, y in re.findall(r"(?<!\d)(\d{1,2})\s*[/.-]\s*(\d{4})(?!\d)", folded) if 1 <= int(m) <= 12]
    if PRESENT.search(folded) and len(dates) == 1:
        dates.append((today.year, today.month))
    if len(dates) != 2:
        return None
    (y1, m1), (y2, m2) = dates
    start, end = y1 * 12 + m1 - 1, y2 * 12 + m2 - 1
    return (start, end) if end >= start else None


def job_months(resume: dict, today: date) -> list[set[int] | None]:
    """Por puesto de `experience_details` (en el orden del CV base): el conjunto de meses que cubre su `employment_period`, o None si no se lee."""
    jobs = [e for e in resume.get("experience_details") or [] if isinstance(e, dict)]
    out = []
    for e in jobs:
        span = parse_period(str(e.get("employment_period") or ""), today)
        out.append(set(range(span[0], span[1] + 1)) if span else None)
    return out


def duration_words(months: int) -> list[str]:
    """Las formas en que se puede decir una duración calculada («7 meses», «1 año y 5 meses»)."""
    words = [f"{months} mes" if months == 1 else f"{months} meses"]
    if months >= 12:
        years, rest = divmod(months, 12)
        year = "1 año" if years == 1 else f"{years} años"
        words += [year] + ([f"{year} y {rest} " + ("mes" if rest == 1 else "meses")] if rest else [])
    return words


def job_durations(resume: dict, today: date) -> tuple[str, ...]:
    """Las duraciones calculadas por puesto, para que un texto abierto pueda decirlas."""
    return tuple(w for months in job_months(resume, today) if months for w in duration_words(len(months)))


def _option_rule(option: str, default_unit: int | None) -> tuple | None:
    """Qué rango de meses dice una opción de duración: `("none",)`, `("gt", n)`, `("ge", n)`, `("lt", n)`, `("le", n)` o `("range", a, b)`
    (en meses); None si no se entiende."""
    folded = score._fold(option)
    folded = re.sub(r"\b(" + "|".join(NUMBER_TOKENS) + r")\b", lambda m: str(NUMBER_TOKENS[m.group(1)]), folded)
    found = NUMBER_UNIT.findall(folded)
    if not found:
        return ("none",) if NO_EXPERIENCE.search(folded) else None
    values, unit = [], None
    for number, word in reversed(found):  # un número sin unidad toma la del siguiente («1 a 2 años»)
        unit = (12 if word.startswith("ano") else 1) if word else (unit or default_unit)
        if unit is None:
            return None
        values.append(float(number.replace(",", ".")) * unit)
    values.reverse()
    if len(values) == 2 and not re.search(r"\b(?:mas|menos|hasta)\b", folded):
        return ("range", min(values), max(values))
    if len(values) != 1:
        return None
    n = values[0]
    if re.search(r"\b(?:mas de|mayor a|mayor de|mayor que|superior a|mas que)\b", folded):
        return ("gt", n)
    if re.search(r"\bo mas\b|\ben adelante\b|\+", folded):
        return ("ge", n)
    if re.search(r"\b(?:menos de|menos a|menor a|menor de|menor que|inferior a)\b", folded):
        return ("lt", n)
    if re.search(r"\bhasta\b|\bmaximo\b", folded):
        return ("le", n)
    if NO_EXPERIENCE.search(folded):  # «sin experiencia a 1 año»: un rango que empieza en 0
        return ("range", 0.0, n)
    return None


def _option_rules(question: dict) -> list[tuple] | None:
    """Los rangos de todas las opciones de una pregunta de cuánta experiencia, o None si alguna no se entiende."""
    folded = score._fold(" ".join([question["text"], *question["options"]]))
    units = [12 if word.startswith("ano") else 1 for _, word in NUMBER_UNIT.findall(folded) if word]
    default_unit = units[0] if units else None
    rules = [_option_rule(option, default_unit) for option in question["options"]]
    return None if any(rule is None for rule in rules) else rules


def experience_option(question: dict, total: int) -> int | None:
    """La opción de una pregunta de cuánta experiencia que corresponde a `total` meses. Con 0 meses solo vale «no tengo» o un rango que
    empieza en 0 («0 a 1 año»), nunca «menos de N» ni «hasta N»: la empresa lo leería como que hay experiencia. Si no, la única cuyo rango
    lo incluye. None si alguna opción no se entiende o no hay una sola que calce."""
    rules = _option_rules(question)
    if rules is None:
        return None
    if total == 0:
        fits = [i for i, rule in enumerate(rules) if rule == ("none",) or (rule[0] == "range" and rule[1] == 0)]
        return fits[0] if len(fits) == 1 else None
    fits = [i for i, rule in enumerate(rules) if rule[0] != "none" and (
        (rule[0] == "gt" and total > rule[1]) or (rule[0] == "ge" and total >= rule[1]) or (rule[0] == "lt" and total < rule[1])
        or (rule[0] == "le" and total <= rule[1]) or (rule[0] == "range" and rule[1] <= total <= rule[2]))]
    return fits[0] if len(fits) == 1 else None


def _experience_answer(question: dict, jobs, months: list) -> tuple[int | None, str]:
    """(opción, motivo) de una pregunta de cuánta experiencia: el modelo eligió los puestos (`jobs`), el código suma los meses (sin contar
    dos veces los que se superponen) y elige la opción. Sin opción, el motivo dice por qué queda pendiente."""
    if (not isinstance(jobs, list) or any(type(j) is not int or not 0 <= j < len(months) for j in jobs)):
        return None, DURATION_REASON
    picked = [months[j] for j in dict.fromkeys(jobs)]
    if any(m is None for m in picked):
        return None, "el control: no se pudieron leer las fechas del CV"
    total = len(set().union(*picked)) if picked else 0
    option = experience_option(question, total)
    if option is not None:
        return option, ""
    if total == 0 and _option_rules(question) is not None:
        return None, NO_EXPERIENCE_REASON
    return None, "el control: no se pudieron leer los rangos de las opciones"


def _control_reason(problems: list[str]) -> str:
    """El motivo de un texto abierto que no pasó el control, sin los valores (lo que va entre «» se quita)."""
    return "el control: " + re.sub(r"«[^»]*»", "«…»", problems[0]) if problems else "el control: el texto no sirve"


# ── interés y disponibilidad: reglas en código (065) ─────────────────────────

INTEREST_PHRASES = ("te interesa", "le interesa", "te interesaria", "queres", "quiere ", "quisieras", "estas interesad", "esta interesad",
                    "te gustaria", "le gustaria")
MODALITY_WORDS = {"presencial": ("presencial",), "híbrido": ("hibrido", "hibrida"),
                 "remoto": ("remoto", "remota", "home office", "teletrabajo", "a distancia")}
AVAILABILITY_COMPONENTS = {
    "rotativo": ("rotativ",),
    "fin de semana": ("fin de semana", "fines de semana", "finde"),
    "sábado": ("sabado",),
    "domingo": ("domingo",),
    "feriado": ("feriado",),
    "full time": ("full time", "fulltime", "tiempo completo", "jornada completa"),
    "part time": ("part time", "parttime", "medio tiempo", "media jornada", "jornada reducida"),
    "presencial": ("presencial",),
    "horario": ("horario",),
}
ROUTE_WORDS = ("ruteo", "recorrido", "zonas", "zona")
AVAILABILITY_SKIP = ("inmediata", "empezar", "comenzar", "incorporar", "viajar", "mudar", "radicar", "residencia", "cambio de provincia")
AVAILABILITY_TOTAL = ("total", "amplia", "cualquier", "sin restriccion", "completa", "todos los dias", "a convenir")
DAY_RANGE = re.compile(r"\b(?:lunes|martes|miercoles|jueves|viernes|sabados?|domingos?)\s+a\s+(?:lunes|martes|miercoles|jueves|viernes|sabados?|domingos?)\b")
AVAILABILITY_EXCLUDE = ("experiencia", "trabajaste", "conocimiento", "curso", "carnet", "certificado", "libreta", "titulo")
IMMEDIATE_PHRASES = ("disponibilidad inmediata", "incorporacion inmediata", "para un ingreso")
INVEST_WORDS = ("invert", "inversion")
AVAILABILITY_PENDING = "disponibilidad: falta en tus respuestas"
INTEREST_LABEL = "interés en la oferta (siempre sí)"
INTEREST_TEXT = "Sí, me interesa."
AVAILABILITY_LABEL = "availability + modality"
RESIDENCE_LABEL = "location"
MOTIVATION_LABEL = "interés en la oferta (motivación)"
MOTIVATION_PHRASES = ("por que te gustaria", "motivacion para", "tu motivacion", "que te motiva", "por que queres trabajar", "por que queres ser parte")


def _word_start(option: str, word: str) -> bool:
    return re.match(rf"{word}\b", score._fold(option)) is not None


def yes_no_options(options: list[str]) -> tuple[int | None, int | None]:
    """Los índices de la primera opción que empieza con «sí» y de la primera que empieza con «no» (sin importar tildes ni mayúsculas:
    «Sí, puedo», «SI», «No»). None si no hay."""
    yes = next((i for i, option in enumerate(options) if _word_start(option, "si")), None)
    no = next((i for i, option in enumerate(options) if _word_start(option, "no")), None)
    return yes, no


def _word_option(options: list[str], text) -> int | None:
    """Si el modelo dejó «sí» o «no» como respuesta de una cerrada, la opción que empieza con esa palabra."""
    word = score._fold(str(text or "")).strip(" .,;:!¡?¿")
    yes, no = yes_no_options(options)
    return yes if word == "si" else no if word == "no" else None


def _accepted_modalities(declared: dict) -> set[str]:
    folded = score._fold(str(declared.get("modality") or ""))
    return {name for name, words in MODALITY_WORDS.items() if any(w in folded for w in words)}


def last_question(text: str) -> str:
    """La última oración con signo de pregunta («La posición es eventual. ¿Te interesa…?» -> «¿Te interesa…?»); el texto entero si no hay."""
    end = text.rfind("?")
    if end < 0:
        return text
    head = text[:end]
    return head[max(head.rfind(mark) for mark in ".?!\n") + 1:].strip() + "?"


POSITION_REASON = "puesto: ninguna opción coincide con la búsqueda ni con el título"
POSITION_LABEL = "puesto de la búsqueda"


def _position_name(text: str) -> str:
    """El nombre de un puesto sin tildes, mayúsculas, «/a», «/as», «/o»: «Cajero/a» y «cajero» son lo mismo."""
    folded = re.sub(r"/\s*(?:a|as|o|os|e|es)\b|\((?:a|as|o|os)\)", "", score._fold(text))
    return " ".join(re.sub(r"[^a-z0-9ñ ]+", " ", folded).split())


def position_answer(question: dict, offer: dict | None) -> dict | None:
    """«¿A qué puesto te postulás?» con puestos como opciones: la opción igual a la búsqueda que trajo la oferta (`keyword`) o, si no, la que
    está dentro del título de la oferta. Si no hay una sola, queda pendiente. None si no es esa pregunta o no se conoce la oferta."""
    if question["kind"] != "closed" or offer is None or len(question["options"]) < 2:
        return None
    folded = score._fold(question["text"])
    if "puesto" not in folded or "postul" not in folded:
        return None
    options = [_position_name(o) for o in question["options"]]
    keyword, title = _position_name(offer.get("keyword") or ""), f" {_position_name(offer.get('title') or '')} "
    fits = [n for n, o in enumerate(options) if o and keyword and o == keyword] or [
        n for n, o in enumerate(options) if o and f" {o} " in title]
    return {"option": fits[0], "label": POSITION_LABEL} if len(fits) == 1 else {"pending": POSITION_REASON}


def _clean_title(title: str) -> str:
    """El título de la oferta sin lo que va entre paréntesis o corchetes («Vendedor/a (Zona Norte)» da «Vendedor/a»)."""
    return " ".join(re.sub(r"[(\[][^)\]]*[)\]]", " ", title or "").split())


def motivation_answer(question: dict, offer: dict | None, resume: dict) -> dict | None:
    """Una abierta de por qué quiere ser parte de la empresa o qué lo motiva: interés en la oferta, breve. Sale del título y la empresa de la
    oferta y del último puesto del CV; no afirma nada más. None si no es esa pregunta o no se conoce la oferta."""
    if question["kind"] != "open" or offer is None or not _clean_title(offer.get("title") or ""):
        return None
    if not any(phrase in score._fold(question["text"]) for phrase in MOTIVATION_PHRASES):
        return None
    title, company = _clean_title(offer["title"]), " ".join(str(offer.get("company") or "").split())
    jobs = [e for e in resume.get("experience_details") or [] if isinstance(e, dict) and _clean(e.get("position"))]
    text = f"Me interesa sumarme a {company} como {title}" if company else f"Me interesa la vacante de {title}"
    text += (f", un puesto donde puedo aportar mi experiencia como {_clean(jobs[0]['position'])}." if jobs else ".")
    return {"text": text, "label": MOTIVATION_LABEL} if len(text) <= MAX_OPEN_ANSWER else None


WILLING_PHRASES = ("dispuest", "te animas", "te animarias", "podes trabajar", "puedes trabajar", "podrias trabajar", "aceptas trabajar")
WILLING_CONDITIONS = ("via publica", "calle", "eventos", "evento ")
WILLING_TEXT = "Sí, estoy dispuesto."


def willingness_answer(question: dict, declared: dict) -> dict | None:
    """«¿Estás dispuesto/a a…?», «¿Te animás a…?», «¿Podés trabajar en…?» sobre una condición que la oferta pide (vía pública, calle, eventos):
    SÍ, como `interest_answer`. Si nombra horario, fin de semana o zona, la contesta la regla de disponibilidad. None si no es esa pregunta."""
    folded = score._fold(last_question(question["text"]))
    if not any(p in folded for p in WILLING_PHRASES) or not any(c in folded + " " for c in WILLING_CONDITIONS):
        return None
    if any(w in folded for words in AVAILABILITY_COMPONENTS.values() for w in words) or any(re.search(rf"\b{w}\b", folded) for w in ROUTE_WORDS):
        return None
    if question["kind"] == "closed":
        yes, _ = yes_no_options(question["options"])
        return {"option": yes, "label": INTEREST_LABEL} if yes is not None else None
    return {"text": WILLING_TEXT, "label": INTEREST_LABEL}


NEAR = re.compile(r"\bcerca\s+(?:de|del)\s+([^?¿]+)|\ba\s+menos\s+de\s+\d+\s*(?:km|kms|kilometros|cuadras)\s+(?:de|del)\s+([^?¿]+)"
                  r"|\bcomo\s+te\s+queda\s+([^?¿]+)|\ba\s+que\s+distancia\b(?:\s+(?:de|del|queda)\s+([^?¿]+))?")
REGION_NOISE = frozenset({"bsas", "buenos", "aires", "gba", "provincia", "caba", "argentina", "capital", "federal", "zona", "norte", "sur", "oeste", "este",
                          "conurbano", "gran", "ciudad", "localidad", "barrio", "partido", "los", "las", "del", "con", "una", "mas", "tu", "casa",
                          "domicilio", "lugar", "trabajo", "oficina", "sucursal", "centro"})
NEAR_PENDING = "cercanía: la localidad declarada no coincide; es un dato que solo sabés vos"
READ_CONFIRM = re.compile(r"\bconfirm\w*\s+(?:que\s+)?(?:has\s+|ha\s+)?lei|\bconfirm\w*\s+(?:la\s+)?lectura\b")
READ_TEXT = "Sí, lo leí y puedo hacerlo."
READ_LABEL = "confirmación de lectura"


def confirmation_answer(question: dict) -> dict | None:
    """Una abierta que pide confirmar que leyó una indicación («Confirmá que has leído…»): texto fijo. None si no es esa pregunta."""
    if question["kind"] != "open" or not READ_CONFIRM.search(score._fold(question["text"])):
        return None
    return {"text": READ_TEXT, "label": READ_LABEL}


PERSONAL_Q = re.compile(r"\b(?:telefono|(?:tu|su|numero\s+de|nro\.?\s+de)\s+(?:celular|whatsapp|movil)|numero\s+(?:de\s+)?(?:telefono\s+)?(?:celular|movil)|"
                        r"whatsapp\s+(?:o|y)\s+mail|e-?mail|mail|correo\s+electronico|cuil|cuit|dni|documento\s+de\s+identidad|(?:tu|su)\s+direccion|"
                        r"direccion\s+(?:de\s+)?(?:e-?mail|mail|correo|residencia|particular))\b")
PERSONAL_HOLD = "dato personal: contestalo vos en la oferta"
THANKS_Q = re.compile(r"\bgracias\s+por\b|\bagradecemos\b|\bse\s+(?:contactara|comunicara|pondra\s+en\s+contacto)\b|\bnos\s+(?:contactaremos|comunicaremos|pondremos\s+en\s+contacto)\b")
THANKS_LABEL = "agradecimiento"


def personal_hold(question: dict, declared: dict) -> str | None:
    """Teléfono, celular, WhatsApp, mail, dirección, CUIL o DNI: nunca van al modelo; quedan pendientes para el usuario."""
    if normalize_question(question["text"]) in declared:
        return None
    return PERSONAL_HOLD if PERSONAL_Q.search(score._fold(question["text"])) else None


def thanks_answer(question: dict) -> dict | None:
    """Un campo sin «?» que agradece o informa («se contactará», «gracias por tu interés»): «¡Gracias!» por código."""
    if question["kind"] != "open" or "?" in question["text"] or not THANKS_Q.search(score._fold(question["text"])):
        return None
    return {"text": "¡Gracias!", "label": THANKS_LABEL}


def proximity_answer(question: dict, declared: dict) -> dict | None:
    """«¿Vivís cerca de X?», «¿a menos de N km de X?», «¿cómo te queda X?», «¿a qué distancia?»: solo Sí si X es la localidad declarada (sin contar
    provincia ni región); si no, pendiente para el usuario (nunca Sí). None si no es esa pregunta."""
    folded = score._fold(question["text"])
    found = NEAR.search(folded)
    if not found:
        return None
    place = next((g for g in found.groups() if g), "")
    words = {w for w in re.findall(r"[a-z]{3,}", place) if w not in REGION_NOISE}
    here = words & set(re.findall(r"[a-z]{3,}", score._fold(_clean(declared.get("location")))))
    if not words or not here:
        return {"pending": NEAR_PENDING}
    if question["kind"] == "closed":
        yes, _ = yes_no_options(question["options"])
        return {"option": yes, "label": RESIDENCE_LABEL} if yes is not None else {"pending": NEAR_PENDING}
    return {"text": f"Sí, vivo en {_clean(declared.get('location'))}", "label": RESIDENCE_LABEL}


ATTITUDE_YES_PHRASES = ("te motiva", "te gusta", "te interesa", "te sentis comod", "te animas", "estas dispuest")


def attitude_yes_answer(question: dict) -> dict | None:
    """Una cerrada de sí o no sobre si le motiva, le gusta, le interesa, se siente cómodo, se anima o está dispuesto: SÍ, como `interest_answer`.
    No la toma si nombra horario, zona o invertir dinero, ni si pide experiencia o sueldo, ni si ya es de actitud (`is_attitude`: sigue con fuente «actitud»). None si no es esa pregunta."""
    if question["kind"] != "closed":
        return None
    folded = score._fold(last_question(question["text"]))
    whole = score._fold(question["text"])
    if not any(p in folded for p in ATTITUDE_YES_PHRASES) or any(w in whole for w in INVEST_WORDS + AVAILABILITY_EXCLUDE) or is_pay_question(question) or is_attitude(question["text"]):
        return None
    if any(w in whole for words in AVAILABILITY_COMPONENTS.values() for w in words) or any(re.search(rf"\b{w}\b", whole) for w in ROUTE_WORDS):
        return None
    yes, no = yes_no_options(question["options"])
    return {"option": yes, "label": INTEREST_LABEL} if yes is not None and no is not None else None


def interest_answer(question: dict, declared: dict) -> dict | None:
    """Una pregunta (cerrada, o abierta: entonces «Sí, me interesa.» y nada más) de si le interesa la vacante, el puesto, la modalidad o el
    contrato de la oferta se contesta SÍ, nunca NO. Se mira la última oración con «?». Si
    nombra una modalidad que el usuario declaró no aceptar (`modality`), queda pendiente. None si la pregunta no es de interés o, si es cerrada,
    no tiene una opción que empiece con «sí»."""
    folded = score._fold(last_question(question["text"]))
    if question["kind"] == "closed":
        yes, _ = yes_no_options(question["options"])
        if yes is None:
            return None
    elif any(w in score._fold(question["text"]) for words in AVAILABILITY_COMPONENTS.values() for w in words) or "disponibilidad" in folded:
        return None  # una abierta que también pregunta por horarios la contesta la regla de disponibilidad
    else:
        yes = None
    if not any(phrase in folded for phrase in INTEREST_PHRASES):
        return None
    if any(w in folded for w in INVEST_WORDS):
        return None  # invertir dinero nunca es un sí automático
    if question["kind"] == "open" and ("interes" not in folded or re.match(r"[\s¿¡]*(?:por ?que|cual|cuales|como|que|donde|cuando|cuanto)\b", folded)):
        return None  # una abierta solo es de interés si lo pregunta de frente («¿Te interesa…?»), no si pide un motivo o un dato
    asked, accepted = {n for n, words in MODALITY_WORDS.items() if any(w in folded for w in words)}, _accepted_modalities(declared)
    if asked and accepted and not asked <= accepted:
        return {"pending": "interés: la modalidad de la oferta no está en tu modality"}
    return {"option": yes, "label": INTEREST_LABEL} if question["kind"] == "closed" else {"text": INTEREST_TEXT, "label": INTEREST_LABEL}


# raíces: indistint (indistinto/a/os), todo (todos, todas, todo el día), ampli (amplia/o), flexib (flexible), ambos/as; una opción que mezcla un turno
# con una de ellas («Mañana o indistinto») también es amplia
WIDE_OPTION_WORDS = ("indistint", "cualquier", "todo", "rotativ", "ampli", "flexib", "ambos", "ambas", "full time", "fulltime", "tiempo completo", "total")
SCHEDULE_NO_OPTION = "disponibilidad: ninguna opción cubre lo declarado"


def _schedule_choice(question: dict, declared: dict, folded: str, components: list[str]) -> dict | None:
    """Una pregunta de horario con opciones que no son sí/no, con `availability` total o amplia: se elige la opción más amplia (la primera
    que diga indistinto, cualquiera, full time, todos, rotativo, amplia); si ninguna lo es, queda pendiente. Nunca un turno solo.
    None si no es una pregunta de horario o la disponibilidad declarada no es total (lo resuelve el modelo con `_shift_fits`)."""
    availability = score._fold(str(declared.get("availability") or ""))
    if "presencial" in components or not any(w in availability for w in AVAILABILITY_TOTAL):
        return None
    if not ("disponibilidad" in folded or "horari" in folded or is_shift_question(question)):
        return None
    for n, option in enumerate(question["options"]):
        if any(w in score._fold(option) for w in WIDE_OPTION_WORDS):
            return {"option": n, "label": AVAILABILITY_LABEL}
    return {"pending": SCHEDULE_NO_OPTION}


LIVES_IN = re.compile(r"\b(?:reside|residis|residir|vivis|vives|vive|vivir)\s+(?:actualmente\s+|hoy\s+)?en\s+([^?¿]+)")
PLACE_NOISE = frozenset({"alrededores", "zona", "zonas", "cerca", "cercanias", "gran", "provincia", "ciudad", "localidad", "barrio", "partido", "alguna",
                         "alguno"})
ZONE_WORDS = re.compile(r"\b(?:zona|norte|sur|oeste|este|conurbano|gba|alrededores|cercanias|cerca de|aledan\w*|provincia)\b")
WHERE_Q = re.compile(r"\bdonde\b|\b(?:que|cual)\s+(?:zona|localidad|barrio|ciudad|partido)\b")
LIVING = re.compile(r"\b(?:resid\w*|viv\w*)\b")
MOVE_WORDS = ("mudar", "radicar", "trasladar", "cambio de", "viajar", "dispuest")


def residence_answer(question: dict, declared: dict) -> dict | None:
    """«¿Reside en <lugar>?» («¿Residís en Tigre, Nordelta, alrededores? ¿Dónde?»): se contesta con `answers.location`, sin el modelo, salvo que nombre una zona y no coincida (`ZONE_WORDS`: lo contesta el modelo). Si alguna
    palabra del lugar está en la localidad declarada, «Sí, vivo en <localidad>» (en una cerrada, la opción Sí); si no, «No, vivo en <localidad>»
    (la opción No). None si no es esa pregunta, falta `location`, o una cerrada no tiene Sí y No."""
    location = _clean(declared.get("location"))
    folded = score._fold(question["text"])
    found = LIVES_IN.search(folded)
    if location and question["kind"] == "open" and WHERE_Q.search(folded) and LIVING.search(folded) and not found and not any(w in folded for w in MOVE_WORDS + MOBILITY_WORDS):
        text = f"Vivo en {location.rstrip('. ')}."  # «¿En qué zona estás residiendo actualmente?»
        return {"text": text, "label": RESIDENCE_LABEL} if len(text) <= MAX_OPEN_ANSWER else None
    if not location or not found or any(w in folded for w in MOVE_WORDS):
        return None
    places = {w for w in re.findall(r"[a-z]{3,}", found.group(1)) if w not in PLACE_NOISE and w not in {"los", "las", "del", "con", "una"}}
    if not places:
        return None
    here = places & set(re.findall(r"[a-z]{3,}", score._fold(location)))
    if question["kind"] == "open" and WHERE_Q.search(folded) and not here and not any(w in folded for w in MOBILITY_WORDS):  # «¿Residís en Tigre, Nordelta? ¿Dónde?»: la localidad declarada, sin relleno
        text = f"No, vivo en {location.rstrip('. ')}."
        return {"text": text, "label": RESIDENCE_LABEL} if len(text) <= MAX_OPEN_ANSWER else None
    if not here and ZONE_WORDS.search(folded):
        return None  # nombra una zona y el código no sabe geografía: la contesta el modelo con `location`
    if question["kind"] == "closed":
        yes, no = yes_no_options(question["options"])
        option = yes if here else no
        return {"option": option, "label": RESIDENCE_LABEL} if option is not None else None
    text = f"Sí, vivo en {location}" if here else f"No, vivo en {location}"
    return {"text": text, "label": RESIDENCE_LABEL} if len(text) <= MAX_OPEN_ANSWER else None


def availability_answer(question: dict, declared: dict, zone_ok: bool = True) -> dict | None:
    """Una sola regla para las preguntas de disponibilidad (turnos rotativos, fines de semana, feriados, full o part time, horarios,
    sábados, presencial), con las claves `availability` y `modality`: cada cosa que pregunta tiene que estar cubierta por lo declarado
    (`availability` total, o que la nombre) para contestar sí; si lo declarado la niega («sin fines de semana») o `modality` no incluye
    presencial, no; si las claves no alcanzan, queda pendiente con «disponibilidad: falta en tus respuestas», nunca un no. Cerradas: solo
    con opciones sí/no; abiertas: el texto de `availability` (y `modality` si pregunta por presencial) tal cual, solo cuando es sí.
    `zone_ok` es False si hay zonas y la oferta cae fuera. None si la pregunta no es de disponibilidad."""
    folded = score._fold(question["text"])
    table = dict(AVAILABILITY_COMPONENTS)
    for span in DAY_RANGE.findall(folded):  # «de lunes a viernes»: lo declarado tiene que decir lo mismo
        table[span] = (span,)
    inconvenience = "inconvenient" in folded and any(w in folded for w in ROUTE_WORDS)  # «¿Tenés algún inconveniente con el ruteo / las zonas?»
    if inconvenience:
        table["recorrido"] = ROUTE_WORDS
    components = [name for name, words in table.items() if any(w in folded for w in words)]
    if any(w in folded for w in AVAILABILITY_EXCLUDE):
        return None  # pide experiencia, conocimiento o un curso, carnet, certificado, libreta o título: no es de disponibilidad
    immediate = any(p in folded for p in IMMEDIATE_PHRASES)
    if not components and "disponibilidad" not in folded and not immediate or any(w in folded for w in AVAILABILITY_SKIP if not (immediate and w == "inmediata")):
        return None
    if is_pay_question(question) or (question["kind"] == "closed" and (is_duration_question(question) or "experiencia" in folded)):
        return None
    yes = no = None
    if question["kind"] == "closed":
        yes, no = yes_no_options(question["options"])
        if yes is None or no is None:  # opciones que no son sí/no (por ejemplo, turnos)
            return _schedule_choice(question, declared, folded, components)
    availability, accepted = score._fold(str(declared.get("availability") or "")), _accepted_modalities(declared)
    total = any(w in availability for w in AVAILABILITY_TOTAL)
    verdicts = []
    for name in components or ["general"]:
        if name == "presencial":
            verdicts.append("pending" if not accepted or not zone_ok else "yes" if "presencial" in accepted else "no")
            continue
        words = table.get(name, ())
        negated = any(re.search(rf"\b(?:no|sin|nunca)\b(?:\s+\w+){{0,3}}?\s+{re.escape(w)}|\bsin\b[^.;,]*?\bni\b[^.;,]*?{re.escape(w)}", availability)
                      for w in words)
        verdicts.append("pending" if not availability else "no" if negated else "yes" if total or any(w in availability for w in words) else "pending")
    verdict = "no" if "no" in verdicts else "pending" if "pending" in verdicts else "yes"
    if inconvenience:  # preguntan si hay un problema: con la disponibilidad cubierta es NO (sin inconveniente); si no, pendiente
        return {"option": no, "label": AVAILABILITY_LABEL} if verdict == "yes" and question["kind"] == "closed" else {"pending": AVAILABILITY_PENDING}
    if question["kind"] == "open":
        parts = [_clean(declared.get("availability"))] if availability else []
        if "presencial" in components and accepted:
            parts.append(_clean(declared.get("modality")))
        text = ". ".join(p.rstrip(". ") for p in parts if p) + "." if parts else ""
        if verdict != "yes" or not text.strip(".") or len(text) > MAX_OPEN_ANSWER:
            return {"pending": AVAILABILITY_PENDING}
        return {"text": text, "label": AVAILABILITY_LABEL}
    if verdict == "pending":
        return {"pending": AVAILABILITY_PENDING}
    return {"option": yes if verdict == "yes" else no, "label": AVAILABILITY_LABEL}


_N, _U = r"(\d+|" + "|".join(NUMBER_TOKENS) + r")", r"(anos?|meses|mes)\b"
MINIMUM = re.compile(r"(?:al menos|como minimo|minim[oa](?:\s+de)?|por lo menos|mas de|mayor a|no menos de)\s+" + _N + r"\s+" + _U
                     + r"|" + _N + r"\s+" + _U + r"\s+(?:o mas|(?:de experiencia\s+)?minim[oa])\b"
                     + r"|" + _N + r"\s*\+\s*" + _U)
SHORT_REASON = "la respuesta dice sí y el CV tiene menos tiempo"
NO_OPTION_REASON = "ninguna opción coincide con los hechos"
# opciones que agregan algo que quizá ningún hecho dice: solo valen si una verdad o una respuesta declarada lo dice con esas palabras
DISCARDING = ("no cuento con disponibilidad", "no tengo disponibilidad", "sin disponibilidad", "no estoy dispuest", "no estoy interesad",
              "no me interesa", "debo actualiz", "se encuentra vencid", "esta vencid", "no esta vigente", "no esta al dia")


def minimum_months(text: str) -> int | None:
    """Los meses que pide una pregunta de «al menos N años/meses», «más de N», «N años o más»; None si no pide un mínimo."""
    found = MINIMUM.search(score._fold(text))
    if not found:
        return None
    number, unit = next((found.group(i), found.group(i + 1)) for i in (1, 3, 5) if found.group(i))
    return int(NUMBER_TOKENS.get(number) or number) * (12 if unit.startswith("ano") else 1)


def _short_of_minimum(question: dict, jobs, months: list, resume: dict) -> bool:
    """Si la pregunta pide un mínimo de tiempo y los puestos que cuentan (`jobs` del modelo o, si no los dio, los que el texto nombra) suman
    menos en el CV. False si no se puede calcular (sin puestos o con fechas ilegibles)."""
    need = minimum_months(question["text"])
    if need is None:
        return False
    picked = [j for j in dict.fromkeys(jobs) if type(j) is int and 0 <= j < len(months)] if isinstance(jobs, list) else []
    if not picked:
        asked = set(re.findall(r"[a-z]{5,}", score._fold(question["text"])))
        details = [e for e in resume.get("experience_details") or [] if isinstance(e, dict)]
        picked = [j for j, e in enumerate(details) if j < len(months) and asked & set(re.findall(r"[a-z]{5,}", score._fold(str(e.get("position") or ""))))]
        if not picked:  # «ventas» y el puesto «Vendedor»: la raíz del puesto
            titles = [set(re.findall(r"[a-z]{3,}", score._fold(str(e.get("position") or "")))) for e in details]
            picked = [j for j, t in enumerate(titles) if j < len(months) and any(c.startswith(root) for w in asked for root in TITLE_ROOTS.get(w, ()) for c in t)]
    if not picked or any(months[j] is None for j in picked):
        return False
    return len(set().union(*(months[j] for j in picked))) < need


def _discarding(option: str, support: str) -> bool:
    """Si la opción trae una frase que descarta o que da a entender algo que ningún hecho ni respuesta declarada (`support`, plegado) dice."""
    folded = score._fold(option)
    return any(phrase in folded and phrase not in support for phrase in DISCARDING)


ATTITUDE_WORDS = ("sociable", "objetivo", "trabajo en equipo", "trabajar en equipo", "via publica", "proactiv", "responsable", "puntual",
                  "bajo presion", "buena predisposicion", "comunicacion")
NOT_ATTITUDE = ("experiencia", "cumpliste", "cumplio", "lograste", "alcanzaste", "trabajaste", "cuanto", "cuantos", "cuanta", "cuantas",
                "anos", "meses", "estudi")  # pide experiencia, estudios o cifras: no es de actitud aunque nombre una de esas palabras
RESPONSIBLE_OF = re.compile(r"responsables?\s+(?:de|del|en|por|a cargo)\b")  # «responsable de caja»: una tarea, no una actitud


def is_attitude(text: str) -> bool:
    """Si es una pregunta de actitud (sociable, objetivos, trabajo en equipo, vía pública, proactivo, responsable, puntual, bajo presión,
    predisposición, comunicación; «responsable» solo sin «de/del/en/por/a cargo»). Una que pide experiencia, estudios o cifras («¿cumpliste objetivos de ventas…?», «¿cuántos…?») no lo es."""
    folded = score._fold(text)
    attitude = RESPONSIBLE_OF.sub(" ", folded)  # «responsable» solo cuenta si no sigue «de», «del», «en», «por» ni «a cargo»
    return (any(w in attitude for w in ATTITUDE_WORDS) and not any(re.search(rf"\b{w}", folded) for w in NOT_ATTITUDE)
            and not any(ch.isdigit() for ch in folded))
NO_SOURCE = "sin fuente"
EXPERIENCE_WORDS = ("experiencia", "trabajaste", "trabajado", "trabajas", "trabajar como", "desempen", "empleo", "puesto", "rubro", "sector",
                    "industria")
RESIDENCE_WORDS = ("reside", "residis", "vivis", "vives", "vive en", "zona")
START_WORDS = ("empezar", "comenzar", "incorporar", "inmediata")
SOURCE_STOP = frozenset({"tenes", "contas", "cuenta", "tienes", "estas", "sobre", "gusta", "puedes", "podes", "podria", "tiene", "alguien",
                        "considera", "consideras", "trabajo", "trabajar"})


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z]{5,}", score._fold(text))) - SOURCE_STOP


def _polarity(entry: dict) -> str | None:
    """«yes» / «no» si la respuesta empieza con sí o con no (la opción elegida o el texto abierto); None si no."""
    text = score._fold(entry["options"][entry["option"]] if entry["kind"] == "closed" and entry["option"] is not None else (entry["text"] or ""))
    text = text.lstrip(" ¡¿")
    return "yes" if re.match(r"si\b", text) else "no" if re.match(r"no\b", text) else None


def answers_key(question: dict, entry: dict, declared: dict) -> str | None:
    """La clave de `answers` (o la respuesta declarada a esa misma pregunta) en que se apoya la respuesta, si el código la puede nombrar: la
    pregunta es de sueldo, de dónde vive, de movilidad, de modalidad, de disponibilidad o de cuándo empezar y esa clave está cargada, o la
    respuesta repite un valor declarado. None si no."""
    key = normalize_question(question["text"])
    if key in declared:
        return key
    folded, said = score._fold(question["text"]), score._fold(answer_text(entry))
    if is_pay_question(question):
        pay = pay_key(question)
        return pay if pay and _clean(declared.get(pay)) else None
    availability = tuple(w for words in AVAILABILITY_COMPONENTS.values() for w in words) + ("disponibilidad", "turno")
    modality = tuple(w for words in MODALITY_WORDS.values() for w in words) + ("modalidad",)
    if is_shift_question(question) and _clean(declared.get("availability")):
        return "availability"
    for name, words in (("location", LOCATION_WORDS + RESIDENCE_WORDS), ("mobility", MOBILITY_WORDS), ("modality", modality),
                        ("availability", availability), ("start", START_WORDS)):
        if any(w in folded for w in words) and _clean(declared.get(name)):
            return name
    for name, value in declared.items():
        value = score._fold(_clean(value))
        if len(value) >= 3 and len(said) >= 3 and (value in said or said in value):
            return name
    return None


def _cv_backed(question: dict, entry: dict, resume: dict, cv_text: str | None) -> bool:
    """Si una respuesta con fuente «cv» tiene de dónde: la pregunta es de experiencia, de trabajo, de puesto, de sector o de rubro, o nombra algo
    que está en el CV, o el sí nombra un puesto o una empresa del CV."""
    folded = score._fold(question["text"])
    if any(w in folded for w in EXPERIENCE_WORDS) or minimum_months(question["text"]) is not None:  # «al menos N años»: lo respaldan las fechas del CV
        return True
    everything = _words(json.dumps(resume, ensure_ascii=False, default=str) + " " + (cv_text or ""))
    if _words(question["text"]) & everything:
        return True
    jobs = [e for e in resume.get("experience_details") or [] if isinstance(e, dict)]
    named = _words(" ".join(f"{e.get('position') or ''} {e.get('company') or ''}" for e in jobs))
    return _polarity(entry) != "no" and bool(_words(answer_text(entry)) & named)


GENERIC_EXPERIENCE = frozenset({"experiencia", "venta", "ventas", "atencion", "cliente", "clientes", "manejo", "trabajo", "trabajar", "trabajando",
                                "previa", "laboral", "area", "rubro", "sector", "similar", "tareas", "puesto", "minimo", "menos", "anos", "meses",
                                "comercial", "contas", "tenes", "tienes", "alguna", "algun"})
EXPERIENCE_OF = re.compile(r"experiencia\s+(?:previa\s+|laboral\s+)?(?:en|de|como|con|realizando|haciendo|trabajando\s+(?:en|como|con))\s+([^?¿.;:]+)")
YES_UNBACKED = "sí sin respaldo en el CV"
CONTRADICTS = "contradice una verdad"
TRUTH_GENERIC = frozenset({"actualmente", "encuentra", "siempre", "tenes", "contas", "estas", "alguna", "alguno", "inconveniente", "tengas",
                           "puede", "puedes", "podes", "tiene", "tienes", "sobre", "gusta", "alguien", "considera", "consideras", "cuenta",
                           "experiencia", "venta", "ventas", "cliente", "clientes", "atencion", "manejo"})


def _stems(text: str, stop=frozenset()) -> set[str]:
    """Las palabras de 5 letras o más (sin tildes ni mayúsculas) cortadas a sus 5 primeras letras («trabajando» y «trabajar» dan «traba»)."""
    return {w[:5] for w in re.findall(r"[a-z]{5,}", score._fold(text)) if w not in stop}


def _specific_experience(question: dict) -> list[str]:
    """Las palabras de lo que la pregunta pide como experiencia («venta de indumentaria» da «indumentaria»), sin las genéricas."""
    found = EXPERIENCE_OF.search(score._fold(question["text"]))
    if not found:
        return []
    return [w for w in re.findall(r"[a-z]{4,}", found.group(1)) if w not in GENERIC_EXPERIENCE and w not in {"para", "como", "tipo", "algun", "cual"}]


LIST_SPLIT = re.compile(r"\s*,\s*|\s+y/o\s+|\s+[ou]\s+")
LIST_NOISE = frozenset({"para", "como", "tipo", "algun", "cual", "similares", "similar", "afines", "otras", "otros", "posiciones", "puestos", "cargos",
                        "tareas", "areas", "rubros", "tipos"})
SHORT_ROOT_EXTRA = 3  # una raíz de 3 letras («caj») compara con palabras del CV de hasta 3 letras más: caja, cajero, cajera


def _root(word: str) -> str:
    base = word.rstrip("s")
    return base[:-1] if base[-1:] in "aeo" else base  # «administrativas» y «administrativo»: la misma raíz


def _in_cv(word: str, cv_words: set[str]) -> bool:
    root = _root(word)
    if len(root) >= 4:
        return any(c.startswith(root) for c in cv_words)
    return len(root) == 3 and any(c.startswith(root) and len(c) <= 3 + SHORT_ROOT_EXTRA for c in cv_words)


QUALIFIERS = frozenset({"comprobable", "comprobables", "previa", "previas", "demostrable", "demostrables", "formal", "formales", "comprobada", "comprobadas"})
TOPIC_GENERIC = frozenset({"venta", "ventas", "atencion", "cliente", "clientes", "comercial", "comerciales"})
TITLE_ROOTS = {"venta": ("vent", "vend"), "ventas": ("vent", "vend"), "comercial": ("comerc", "vend", "vent"), "comerciales": ("comerc", "vend", "vent"),
               "cliente": ("client", "atenc"), "clientes": ("client", "atenc"), "atencion": ("atenc",)}


def _title_backs(words: list[str], resume: dict) -> bool:
    """Si la raíz de alguna palabra genérica («ventas», «atención») está en el título de un puesto del CV base («Vendedor», «Atención al Público»)."""
    titles = set(re.findall(r"[a-z]{3,}", score._fold(" ".join(str(e.get("position") or "") for e in resume.get("experience_details") or [] if isinstance(e, dict)))))
    return any(t.startswith(root) for w in words for root in TITLE_ROOTS.get(w, (_root(w),)) for t in titles)


def _yes_unbacked(question: dict, entry: dict, resume: dict, cv_text: str | None) -> bool:
    """Si es un SÍ de experiencia con fuente «cv» y nada de lo específico que se pide (`_specific_experience`) aparece en el CV. En una lista
    («promoción, ventas o posiciones similares») alcanza con que una opción esté en el CV, también una genérica."""
    if _polarity(entry) != "yes":
        return False
    found = EXPERIENCE_OF.search(score._fold(question["text"]))
    items = LIST_SPLIT.split(found.group(1).strip()) if found else []
    words = [w for w in _specific_experience(question) if w not in QUALIFIERS]
    jobs_only = False
    if len(items) > 1:
        words = [w for item in items for w in re.findall(r"[a-z]{3,}", item) if w not in LIST_NOISE and w not in {"las", "los", "una", "uno", "del"}]
        jobs_only = True  # una lista se respalda con un puesto, no con habilidades, proyectos ni estudios
    if not words:
        # solo palabras genéricas («ventas comprobable»): las respalda el título de un puesto del CV base; sin calificativo no se frena
        asked = [w for w in re.findall(r"[a-z]{4,}", found.group(1)) if w in TOPIC_GENERIC] if found else []
        qualified = bool(found) and any(w in QUALIFIERS for w in re.findall(r"[a-z]{4,}", found.group(1)))
        return qualified and bool(asked) and not _title_backs(asked, resume)
    source = json.dumps([e for e in resume.get("experience_details") or [] if isinstance(e, dict)] if jobs_only else resume, ensure_ascii=False, default=str)
    cv_words = set(re.findall(r"[a-z]{3,}", score._fold(source + ("" if jobs_only else " " + (cv_text or "")))))
    return not any(_in_cv(w, cv_words) for w in words)


LIST_SLASH = re.compile(r"\s*/\s*|\s*,\s*|\s+y/o\s+|\s+[ou]\s+")
INTEREST_CLAIM = re.compile(r"\b(?:interes(?:ado|ada)?\s+(?:en|por)|me\s+interesan?|me\s+gustan?)\s+([^.!?;]*)")
INTEREST_STOP = frozenset({"para", "como", "tipo", "algo", "todo", "toda", "todos", "todas", "mucho", "mucha", "siempre", "tambien", "ademas"})


def _background(resume: dict, cv_text: str | None, truths: tuple) -> set[str]:
    """Las palabras (sin tildes, 3 letras o más) del CV base, el CV de la postulación y las verdades."""
    return set(re.findall(r"[a-z]{3,}", score._fold(json.dumps(resume, ensure_ascii=False, default=str) + " " + (cv_text or "") + " " + " ".join(truths))))


def list_experience_yes(question: dict, resume: dict, cv_text: str | None, truths: tuple) -> int | None:
    """Una cerrada de experiencia con una lista («Atención al Cliente / Call Center o Supermercados»): la opción Sí si alguna de las opciones
    de la lista está literal en un puesto del CV base (título o tareas) o en las verdades. None si no es esa pregunta o ninguna está."""
    if question["kind"] != "closed" or is_range_question(question):
        return None
    yes, no = yes_no_options(question["options"])
    found = EXPERIENCE_OF.search(score._fold(question["text"]))
    if yes is None or no is None or not found:
        return None
    items = [i for i in LIST_SLASH.split(found.group(1).strip()) if i.strip()]
    if len(items) < 2:
        return None
    jobs = json.dumps([e for e in resume.get("experience_details") or [] if isinstance(e, dict)], ensure_ascii=False, default=str)  # solo puestos, no habilidades ni estudios
    text = " " + " ".join(re.findall(r"[a-z0-9]+", score._fold(jobs + " " + " ".join(truths)))) + " "
    for item in items:
        words = [w for w in re.findall(r"[a-z0-9]+", item) if w not in LIST_NOISE]
        phrase = " ".join(words)
        if len(phrase) >= 4 and f" {phrase} " in text:
            return yes
    return None


def _drop_invented_interest(text: str, resume: dict, cv_text: str | None, truths: tuple) -> str | None:
    """Saca la oración que afirma un interés («interés en…», «me interesa…», «me gusta…») cuyo tema no está en el CV ni en las verdades.
    None si no queda ninguna oración."""
    background = _background(resume, cv_text, truths)
    kept = []
    for sentence in SENTENCES.split(text.strip()):
        claim = INTEREST_CLAIM.search(score._fold(sentence))
        topic = [w for w in re.findall(r"[a-z]{4,}", claim.group(1)) if w not in INTEREST_STOP] if claim else []
        if topic and not any(_in_cv(w, background) for w in topic):
            continue
        kept.append(sentence)
    return " ".join(kept) or None


def _truth_polarity(truth: str) -> str:
    """«no» si el hecho niega («No estoy trabajando», «Nunca estuve en…», «No tengo…»); «yes» si afirma."""
    folded = score._fold(truth).strip()
    return "no" if re.match(r"(?:no|nunca|ningun[oa]?|sin)\b", folded) else "yes"


def _follow_truth(entry: dict, question: dict, truths: tuple) -> bool:
    """En una cerrada de sí o no cuya respuesta contradice las verdades relacionadas, y todas tienen la misma polaridad, se contesta con la de la
    verdad (la opción No si niega), fuente «truths». False si no hay una sola polaridad o la pregunta no tiene Sí y No."""
    if entry["kind"] != "closed":
        return False
    polarities = {_truth_polarity(tr) for tr in _related_truths(question, truths)}
    yes, no = yes_no_options(entry["options"])
    if len(polarities) != 1 or yes is None or no is None:
        return False
    entry["option"], entry["source"], entry["reason"] = (yes if polarities == {"yes"} else no), "truths", None
    return True


def _related_truths(question: dict, truths: tuple) -> list[str]:
    """Las verdades que comparten con la pregunta una raíz de 5 letras que no sea genérica (sin las que la pregunta niega con «sin»)."""
    asked = _stems(question["text"], TRUTH_GENERIC | SOURCE_STOP)
    folded = score._fold(question["text"])

    def shared(tr: str) -> set[str]:  # las raíces que comparten, sin las que la pregunta niega con «sin» («sin cartera»)
        return {s for s in asked & _stems(tr, TRUTH_GENERIC) if not re.search(rf"\bsin\s+(?:[a-z]+\s+){{0,2}}{s}", folded)}

    return [tr for tr in truths if shared(tr)]


def _truth_source(entry: dict, question: dict, truths: tuple) -> str | None:
    """Antes de dejar una respuesta sin fuente: si alguna verdad comparte con la pregunta una palabra de 5 letras o más que no sea genérica y la
    respuesta tiene la misma polaridad (sí/no) que esa verdad, «truths»; si la tiene otra, «contradice»; None si ninguna verdad aplica o la
    respuesta no es de sí o no."""
    polarity = _polarity(entry)
    if polarity is None or not truths:
        return None
    related = _related_truths(question, truths)
    if not related:
        return None
    return "truths" if any(_truth_polarity(tr) == polarity for tr in related) else "contradice"


TRUTH_UNBACKED = "truths sin respaldo"
NOT_ASKED = "no responde lo preguntado"
STUDY_REASON = "el CV dice que ya terminó esa carrera"
STOP_PREFIX = ("trabaj", "teng", "tien", "tene", "cuent", "cont", "pued", "pod", "experien", "estoy", "esta", "hast", "desd", "sobr", "tambi", "dond",
               "cuand", "cuant", "cual", "hice", "hac", "hech", "nunc", "siempr", "ademas", "tuvi", "tuve", "para", "como", "tipo", "algo", "acept",
               "gust", "podri", "tiemp", "puest", "empres", "alguna", "algun", "tuviste", "quer", "quier", "tecnolog", "herramient", "tarea", "funcion", "detall",
               "explic", "descrip", "coment", "rubro", "sector", "area", "industr", "ambito", "labor", "similar")


def _content(text: str) -> list[str]:
    """Las palabras de contenido (4 letras o más, sin tildes) que no son verbos ni muletillas de pregunta."""
    return [w for w in re.findall(r"[a-z]{4,}", score._fold(text)) if not w.startswith(STOP_PREFIX)]


def _related(a: str, b: str) -> bool:
    """Si dos palabras tienen la misma raíz («caja» y «cajero», «ventas» y «vendedor»)."""
    ra, rb = (re.sub(r"^aten[dc]\w*", "aten", re.sub(r"^vend", "vent", _root(w))) for w in (a, b))
    short = min(ra, rb, key=len)
    return len(short) >= 3 and ra.startswith(short) and rb.startswith(short) and (len(short) >= 4 or max(len(a), len(b)) <= 3 + SHORT_ROOT_EXTRA)


def _overlap(a: str, b: str) -> bool:
    """Si dos textos comparten una raíz de contenido."""
    words_b = _content(b)
    return any(_related(x, y) for x in _content(a) for y in words_b)


def _on_topic(text: str, question: str) -> tuple[str | None, str | None]:
    """Sin volcado: cada oración (salvo la primera) tiene que compartir una raíz de contenido con la pregunta o con la primera; si no, se saca.
    Si la primera (que no sea solo «Sí»/«No») no comparte nada con la pregunta, `(None, NOT_ASKED)`. Una pregunta sin palabras de contenido no se controla."""
    sentences = [s for s in SENTENCES.split(text.strip()) if s]
    if len(sentences) == 0 or not _content(question):
        return text, None
    first = sentences[0]
    if _content(first) and not re.match(r"[¡¿\s]*(?:si|no)\b", score._fold(first)) and not _overlap(first, question):
        return None, NOT_ASKED
    kept = [first]
    for sentence in sentences[1:]:
        if _overlap(sentence, question) or _overlap(sentence, first):
            kept.append(sentence)
    return " ".join(kept), None


YEARS_CLAIM = re.compile(r"(mas de)?\s*(\d+|" + "|".join(NUMBER_TOKENS) + r")\s+anos?\b")
EXPERIENCE_SENTENCE = re.compile(r"experiencia|trabaj|vend|desempen|ejerc")


def _claim_short(text: str, resume: dict, months: list) -> bool:
    """Si una abierta afirma «N años» / «más de N años» de experiencia en un área y los puestos del CV base de esa área suman menos meses. Los puestos
    del área son los que nombran una raíz de la oración; si ninguno, todos. False si no se pueden calcular (fechas ilegibles)."""
    details = [e for e in resume.get("experience_details") or [] if isinstance(e, dict)]
    for sentence in SENTENCES.split(score._fold(text)):
        if not EXPERIENCE_SENTENCE.search(sentence):
            continue
        for found in YEARS_CLAIM.finditer(sentence):
            need = int(NUMBER_TOKENS.get(found.group(2)) or found.group(2)) * 12
            picked = [j for j, e in enumerate(details) if j < len(months) and _overlap(sentence, json.dumps(e, ensure_ascii=False, default=str))] \
                or [j for j in range(min(len(details), len(months)))]
            if not picked or any(months[j] is None for j in picked):
                continue
            have = len(set().union(*(months[j] for j in picked)))
            if have < need or (found.group(1) and have <= need):
                return True
    return False


STUDY_CLAIM = re.compile(r"\b(?:estudiante|cursando|finalizare|terminare|recibire|egresare|estoy\s+estudiando|estudio\s+actualmente)\b")


def _study_conflict(text: str, resume: dict, today: date) -> bool:
    """Si una abierta dice «estudiante», «cursando» o «finalizaré» sobre una carrera que el CV marca como terminada."""
    if not STUDY_CLAIM.search(score._fold(text)):
        return False
    for study in resume.get("education_details") or []:
        if not isinstance(study, dict):
            continue
        year = re.search(r"\d{4}", str(study.get("year_of_completion") or ""))
        done = (year is not None and int(year.group()) <= today.year) or bool(re.search(r"graduad|egresad|finalizad|completo", score._fold(json.dumps(study, ensure_ascii=False))))
        if done and _overlap(text, f"{study.get('field_of_study') or ''} {study.get('education_level') or ''}"):
            return True
    return False


def _truths_backed(entry: dict, question: dict, truths: tuple) -> bool:
    """Una respuesta con fuente «truths» tiene que tener un hecho del banco con la misma polaridad: en una cerrada, una verdad relacionada con la
    pregunta (`_truth_source`); en una abierta, una verdad que comparte una raíz con el texto, y si el texto empieza con sí o no, que no lo contradiga."""
    if entry["kind"] == "closed":
        return _truth_source(entry, question, truths) == "truths"
    shared = [tr for tr in truths if _overlap(entry["text"] or "", tr)]
    if not shared:
        return False
    polarity = _polarity(entry)
    return polarity is None or any(_truth_polarity(tr) == polarity for tr in shared)


def _source_guard(entry: dict, question: dict, declared: dict, resume: dict, cv_text: str | None, truths: tuple = ()) -> None:
    """La fuente de una respuesta del modelo tiene que ser verdad. «answers» vale si el código nombra la clave que usó (`answers_key`); «cv»
    si el CV la respalda (`_cv_backed`); las de actitud (sociable, objetivos, trabajo en equipo, vía pública) con SÍ pasan con la fuente
    «actitud». Si no, queda pendiente con «sin fuente». Las de «truths» pasan solo con un hecho del banco que las respalde (`_truths_backed`)."""
    source = entry["source"]
    if source == "truths":
        if entry["kind"] == "closed" and _truth_source(entry, question, truths) == "contradice" and _follow_truth(entry, question, truths):
            return
        if not _truths_backed(entry, question, truths):
            entry["source"], entry["option"], entry["text"], entry["reason"] = "unknown", None, None, TRUTH_UNBACKED
        return
    if _polarity(entry) == "yes" and is_attitude(question["text"]):
        entry["source"] = "actitud"
        return
    if source == "cv" and _truth_source(entry, question, truths) == "contradice":  # una respuesta del CV que dice lo contrario de una verdad
        if not _follow_truth(entry, question, truths):
            entry["source"], entry["option"], entry["text"], entry["reason"] = "unknown", None, None, CONTRADICTS
        return
    if source == "cv" and _yes_unbacked(question, entry, resume, cv_text):  # un SÍ de experiencia en algo que el CV no nombra
        yes, no = yes_no_options(entry["options"]) if entry["kind"] == "closed" else (None, None)
        if entry["kind"] == "closed" and yes is not None and no is not None and len(entry["options"]) == 2:
            entry["option"] = no
        else:
            entry["source"], entry["option"], entry["text"], entry["reason"] = "unknown", None, None, YES_UNBACKED
        return
    ok = (source == "answers" and answers_key(question, entry, declared) is not None) or (source == "cv" and _cv_backed(question, entry, resume, cv_text))
    if not ok:
        verdict = _truth_source(entry, question, truths)
        if verdict == "truths":
            entry["source"] = "truths"
            return
        if verdict == "contradice" and _follow_truth(entry, question, truths):
            return
        entry["source"], entry["option"], entry["text"] = "unknown", None, None
        entry["reason"] = CONTRADICTS if verdict == "contradice" else NO_SOURCE


def _guard_answer(entry: dict, question: dict, jobs, months: list, resume: dict, support: str) -> None:
    """Controla una respuesta que eligió el modelo. «Al menos N años» con menos tiempo en el CV: la cerrada de sí o no pasa a NO y la abierta
    tiene que empezar con «No» (si no, pendiente). Una opción cerrada que descarta sola (`_discarding`) se cambia por la única de su misma
    polaridad (sí / no) que no descarte; si no hay una sola, pendiente con «ninguna opción coincide con los hechos»."""
    short = _short_of_minimum(question, jobs, months, resume)
    if question["kind"] == "open":
        short = short or _claim_short(entry["text"] or "", resume, months)
        if short and not re.match(r"no\b", score._fold(entry["text"] or "").lstrip(" ¡¿")):
            entry["source"], entry["text"], entry["reason"] = "unknown", None, SHORT_REASON
        return
    options, option = question["options"], entry["option"]
    if option is None:
        return
    yes, no = yes_no_options(options)
    if short and option == yes:
        if no is None:
            entry["source"], entry["option"], entry["reason"] = "unknown", None, SHORT_REASON
            return
        entry["option"] = option = no
    if not _discarding(options[option], support):
        return
    polarity = "no" if _word_start(options[option], "no") else "si" if _word_start(options[option], "si") else None
    fits = [i for i, o in enumerate(options) if polarity and _word_start(o, polarity) and not _discarding(o, support)]
    if len(fits) == 1:
        entry["option"] = fits[0]
    else:
        entry["source"], entry["option"], entry["reason"] = "unknown", None, NO_OPTION_REASON


AI_PHRASES = ("mi enfoque siempre ha sido", "brindar un excelente servicio", "excelente servicio", "servicio de calidad", "servicio personalizado",
              "lo que me permite", "garantizar su satisfaccion", "fomentar la fidelizacion", "he desarrollado", "cabe destacar", "sin duda",
              "en resumen", "me apasiona", "soy una persona proactiva", "valor agregado", "siguiente nivel", "desarrolle habilidades", "listo para comenzar", "lista para comenzar",
              "en ambos roles", "es un lugar accesible", "he trabajado", "se ha centrado", "se centra en")
# se comparan por raíz (expresiones regulares sobre el texto sin tildes): así toman las conjugaciones
AI_PATTERNS = (r"garantiz\w*\s+(?:la|su)\s+satisfaccion", r"asegur\w*\s+(?:que\s+el\s+cliente|una\s+buena\s+experiencia)", r"lo\s+que\s+me\s+permit\w*",
               r"desarroll\w*\s+habilidades", r"\bmi\s+enfoque", r"soluciones\s+personalizadas", r"fluida\s+y\s+efectiva", r"ventas\s+efectivas",
               r"puedo\s+comunicarme\s+en\s+este\s+idioma", r"(?:^|[.!?]\s+)sin\s+embargo,", r"esto\s+me\s+permit\w*", r"experiencia\s+enriquecedora",
               r"orientad[oa]\s+a\s+resultados", r"enfoque\s+emprendedor")
STYLE_REASON = "suena a plantilla"
STYLE_NOTE = "Tu respuesta anterior usó «{phrase}»: reescribila sin esa frase ni otras de plantilla, en primera persona y con un dato concreto del CV."
TRUTH_NOTE = "Hay un hecho cierto del banco que contesta esta pregunta: usalo (source truths), no unknown. Hecho: {facts}"
HINT_STOP = frozenset({"tuviste", "oportunidad", "trabajar", "gustaria", "cuenta", "tenes", "tienes", "puedes", "podes"})


def _hint_truths(question: dict, truths: tuple) -> list[str]:
    """Los hechos del banco que comparten raíz (5 letras o más, sin genéricas) con una pregunta abierta («emprend» con «Emprendí un negocio…»)."""
    asked = _stems(question["text"], TRUTH_GENERIC | SOURCE_STOP | HINT_STOP)
    return [tr for tr in truths if asked & _stems(tr, TRUTH_GENERIC | HINT_STOP)]


OPINION_NOTE = "Es una pregunta de opinión: escribila vos (1 a 3 oraciones, con el CV y la oferta, sin hechos que el CV no tenga) con source «opinión»; nunca unknown."
OPINION_PHRASES = ("por que te gustaria", "motivacion", "que te motiva", "que te diferencia", "que tecnicas", "tecnicas utilizas", "tecnicas usas",
                   "como manejarias", "como manejaria", "como resolverias", "sobre vos", "sobre ti", "hablanos de vos", "hablame de vos",
                   "por que queres", "por que deberiamos", "deberiamos contratar", "deberiamos elegir", "elegirte", "contratarte", "expectativas",
                   "como te ves", "como te describis", "como te defin", "como te ven", "como te ve ", "te ve la gente", "te describirian",
                   "te definirian", "fortalezas", "debilidades", "que te gusta", "que no te gusta", "que sabes de la empresa", "que sabes de nosotros",
                   "que conoces de la empresa")
OPINION_STORY = re.compile(r"\b(?:contanos|conta|cuentanos|cuenta|relatanos|relata|describi|describinos)\s+(?:una|un|alguna|algun)\s+"
                           r"(?:situacion|logro|desafio|error|momento|experiencia)\b")
SENTENCES = re.compile(r"(?<=[.!?])\s+")
PLACE_SENTENCE = re.compile(r"\b(?:barrio|zona)\b")
OPINION_NO_TEXT = "opinión sin texto"


def is_opinion(question: dict) -> bool:
    """Si es una abierta de opinión (motivación, «por qué te gustaría», qué te diferencia, técnicas, cómo manejarías, contanos sobre vos): la
    escribe el modelo con fuente «opinión»."""
    folded = score._fold(question["text"])
    return question["kind"] == "open" and (any(p in folded for p in OPINION_PHRASES) or OPINION_STORY.search(folded) is not None)


def ai_hits(text: str) -> list[str]:
    """Las frases de plantilla (`AI_PHRASES`, y «Además,» al empezar una oración) que tiene un texto, en orden."""
    folded = score._fold(text)
    hits = [p for p in AI_PHRASES if p in folded]
    hits += [m.group(0).strip(" .!?") for pattern in AI_PATTERNS if (m := re.search(pattern, folded))]
    if re.search(r"(?:^|[.!?]\s+)ademas,", folded):
        hits.append("Además,")
    return hits


ECHO_STOP = frozenset({"tengo", "tenes", "tiene", "tenia", "tuve", "tengas", "puedo", "podes", "soy", "sido", "estoy", "estas", "tambien", "mucha",
                       "mucho"})
RESIDENCE_Q = re.compile(r"\b(?:resid\w*|vivis|vives|vive|vivir|vivo|domicilio|localidad)\b")


def _echo_words(text: str) -> set[str]:
    return {w[:4] for w in re.findall(r"[a-z]{4,}", score._fold(text)) if w not in ECHO_STOP}


def _humanize(text: str, declared: dict, question: str = "") -> str | None:
    """El texto sin las oraciones que sobran: la que habla del barrio o la zona sin nombrar la localidad declarada se saca; si la única oración
    con una frase de plantilla es la última y queda al menos otra, también. None si todavía suena a plantilla (o no queda nada)."""
    sentences = [s for s in SENTENCES.split(text.strip()) if s]
    place = set(re.findall(r"[a-z]{3,}", score._fold(_clean(declared.get("location")))))
    kept = [s for s in sentences if not (PLACE_SENTENCE.search(score._fold(s)) and not place & set(re.findall(r"[a-z]{3,}", score._fold(s))))]
    if question and len(kept) > 1 and RESIDENCE_Q.search(score._fold(question)):  # residencia: solo la localidad declarada, sin oraciones extra
        kept = [next((s for s in kept if place & set(re.findall(r"[a-z]{3,}", score._fold(s)))), kept[0])]
    elif question and len(kept) > 1:
        head = score._fold(kept[0]).lstrip(" ¡¿")
        rest = re.sub(r"^\W*(?:si|no)\b", " ", head)
        words = _echo_words(rest)
        if re.match(r"(?:si|no)\b", head) and words and words <= _echo_words(question):  # la primera oración repite la pregunta: queda «Sí.» / «No.»
            kept[0] = "Sí." if head.startswith("si") else "No."
    flagged = [i for i, s in enumerate(kept) if ai_hits(s)]
    if flagged:
        if flagged != [len(kept) - 1] or len(kept) < 2:
            return None
        kept = kept[:-1]
    return " ".join(kept) or None


def _opinion_entry(entry: dict, item: dict, question: dict, resume: dict, declared: dict, durations: tuple, cv_text, truths: tuple, offer) -> None:
    """Una pregunta de opinión: el texto del modelo (fuente «opinión») si pasa el control de vocabulario y de estilo; si no, la respuesta de
    motivación en código cuando la pregunta es de motivación; si no, pendiente."""
    text = " ".join(str(item.get("text") or "").split()) if item.get("source") in ANSWER_SOURCES else ""
    problems = _open_text_problems(text, resume, declared, durations, cv_text, truths, _offer_values(offer)) if text else []
    cleaned = _humanize(text, declared, question["text"]) if text and not problems else None
    if cleaned:
        entry["source"], entry["text"], entry["reason"] = "opinión", cleaned, None
        return
    rule = motivation_answer(question, offer, resume)
    if rule and not _open_text_problems(rule["text"], resume, declared, (), cv_text, truths, _offer_values(offer)):
        entry["source"], entry["text"], entry["reason"], entry["fixed"] = "answers", rule["text"], None, rule["label"]
        return
    entry["source"], entry["text"] = "unknown", None
    entry["reason"] = STYLE_REASON if text and not problems else _control_reason(problems) if problems else OPINION_NO_TEXT


def answer_questions(questions: list[dict], profile: dict, client, model: str, variant: str | None = None,
                     calls: list | None = None, cv_text: str | None = None, today: date | None = None,
                     offer_in_zone: bool = False, offer: dict | None = None) -> list[dict]:
    """Contesta las preguntas de selección con el CV de la variante y las respuestas declaradas (`profile["answers"]`); nunca inventa.

    `questions`: `[{"text", "kind": "closed" | "open", "options": [texto]}]`. Un solo pedido con `resume_for_llm` (sin datos
    personales), `answers` y las preguntas, en JSON estricto. Devuelve una entrada por pregunta, en orden:
    `{question, kind, options, option (cerradas), text (abiertas), source}` con `source` «cv», «answers» o «unknown». Es
    «unknown» si el modelo lo dice, si la opción no existe o si el texto abierto está vacío, pasa de 500 caracteres o no pasa el
    control (siglas, tecnologías y niveles que no estén en el CV ni en `answers`; los números solo pueden venir de `answers`).
    Si el usuario declaró la respuesta a una cerrada y es igual (normalizada) al texto de una opción, se envía esa
    (`declared_option`), con `source` «answers», aunque el modelo diga unknown, dé una opción inválida o no traiga la pregunta. Si no, una cerrada cuya opción elegida tiene una cifra (`has_figure`), o que es de
    duración de experiencia (`is_duration_question`), se acepta solo con `source` «answers» y si el usuario declaró esa pregunta o,
    en una de sueldo, su `salary` (`declared_for`), o si es un turno con `availability` y solo franjas horarias (`_shift_fits`); si
    no, es «unknown» (la oferta espera en Preguntas).
    En una cerrada de cuánta experiencia el modelo solo elige los puestos (`jobs`): el código suma los meses con las fechas del CV
    base y elige la opción (`_experience_answer`); lo que el modelo diga de la opción no manda. `cv_text` es el CV a medida de la
    postulación (se usa en lugar del base); `today` cuenta el «Actualidad» de un puesto. Las cifras del CV de la postulación y las fechas
    del CV base valen en las abiertas. Si el modelo deja vacía una abierta con `source` «cv» o «answers», se le pide una vez más solo
    esas; si vuelve vacía queda pendiente. `offer_in_zone` le dice al modelo que la sede de la oferta está en la zona del candidato.
    `offer` = `{title, keyword}` de la oferta (la búsqueda que la trajo): contesta «¿a qué puesto te postulás?» (`position_answer`).
    Los hechos del banco de verdades de la variante (`truths`) van al modelo y valen como fuente «truths».
    `calls` recibe el costo del pedido. Lanza `AnswerError` si la respuesta no sirve y RuntimeError si falla la API.
    """
    chosen = next((v for v in profile["variants"] if v["id"] == variant), profile["variants"][0])
    declared, today = profile.get("answers", {}), today or date.today()
    truths = tuple(chosen.get("truths") or ())
    support = score._fold(" ".join([*truths, *(str(v) for v in declared.values())]))  # lo que los hechos y lo declarado dicen
    fixed = {n: text for n, q in enumerate(questions) if (text := _salary_text(q, declared) or contact_text(q, declared))}  # sin el modelo
    held = {n: reason for n, q in enumerate(questions) if (reason := _pay_hold(q, declared) or personal_hold(q, declared)) and n not in fixed}  # pendiente sin el modelo
    zone_ok = not profile.get("zones") or offer_in_zone
    coded = {}  # interés y disponibilidad: una regla en código, sin el modelo (el texto declarado para esa misma pregunta manda sobre ella)
    for n, q in enumerate(questions):
        if n in fixed or n in held or declared_option(q, declared) is not None or normalize_question(q["text"]) in declared:
            continue
        if rule := confirmation_answer(q) or thanks_answer(q) or proximity_answer(q, declared) or interest_answer(q, declared) or availability_answer(q, declared, zone_ok) or willingness_answer(q, declared) or attitude_yes_answer(q) or position_answer(q, offer) or residence_answer(q, declared):
            coded[n] = rule
    opinions = {n for n, q in enumerate(questions) if n not in fixed and n not in held and n not in coded and is_opinion(q)
                and normalize_question(q["text"]) not in declared}  # las escribe el modelo (fuente «opinión»)
    months, durations = job_months(chosen["resume"], today), job_durations(chosen["resume"], today)
    hints = {n: _hint_truths(q, truths) for n, q in enumerate(questions) if q["kind"] == "open" and n not in fixed and n not in held and n not in coded}
    hints = {n: facts for n, facts in hints.items() if facts}  # un hecho del banco que contesta esa abierta: el modelo lo usa

    def render(indices: list[int]) -> str:
        rows = []
        for k, n in enumerate(indices):
            q = questions[n]
            rows.append(f"{k}. [{'cerrada' if q['kind'] == 'closed' else 'abierta'}] {q['text']}")
            rows += [f"   {i}) {option}" for i, option in enumerate(q["options"])]
            if n in hints:
                rows.append("   Hecho cierto del banco relacionado con esta pregunta (si la contesta, usalo con source truths; no agregues nada que no diga): " + " | ".join(hints[n]))
        return "\n".join(rows)

    jobs = [e for e in chosen["resume"].get("experience_details") or [] if isinstance(e, dict)]
    job_lines = [f"{i}) {e.get('position') or ''} — {e.get('employment_period') or 'sin fechas'}"
                 + (f" ({' o '.join(duration_words(len(m))[:1] + duration_words(len(m))[1:2])})" if m else "") for i, (e, m) in enumerate(zip(jobs, months))]
    user = ("CV del candidato:\n" + (cv_text or resume_for_llm(chosen["resume"]))
            + ("\n\nPuestos del CV base (índice, fechas y duración calculada):\n" + "\n".join(job_lines) if job_lines else "")
            + "\n\nRespuestas declaradas por el candidato:\n"
            + ("\n".join(f"- {key}: {value}" for key, value in declared.items()) or "(ninguna)")
            + ("\n\nHechos ciertos del candidato (banco de verdades):\n" + "\n".join(f"- {fact}" for fact in truths) if truths else "")
            + ("\n\nLa oferta está dentro de la zona del candidato." if offer_in_zone else "")
            + "\n\nPreguntas:\n")
    if len(fixed) + len(held) + len(coded) == len(questions):  # todas se contestan con el código: no hace falta pedirle nada al modelo
        items, call, data = {}, {"valid": True}, {"answers": []}
    else:
        everything = list(range(len(questions)))
        resp = score.ask_json(client, model, ANSWER_SYSTEM, user + render(everything), "respuestas", _answers_schema())
        tokens = score._tokens(resp)
        call = {"cost": score.cost_usd(model, *tokens), "tokens": tokens, "valid": False}
        if calls is not None:
            calls.append(call)
        if getattr(resp, "status", None) != "completed" or score._has_refusal(getattr(resp, "output", None)):
            raise AnswerError("el modelo no terminó la respuesta a las preguntas o la rechazó.")
        try:
            data = json.loads(resp.output_text)
            items = {item["index"]: item for item in data["answers"]}
        except (AttributeError, TypeError, ValueError, KeyError):
            raise AnswerError("la respuesta del modelo a las preguntas no tiene el formato esperado.") from None
        empty = [n for n, q in enumerate(questions) if n not in fixed and n not in held and n not in coded and q["kind"] == "open" and n in items
                 and items[n].get("source") in ANSWER_SOURCES and not _clean(items[n].get("text"))]
        if empty and set(items) <= set(everything):  # una sola vez más, solo esas
            retry = {"cost": 0.0, "tokens": (0, 0, 0), "valid": False}
            try:
                again = score.ask_json(client, model, ANSWER_SYSTEM, user + render(empty), "respuestas", _answers_schema())
                tokens = score._tokens(again)
                retry["cost"], retry["tokens"] = score.cost_usd(model, *tokens), tokens
                if calls is not None:
                    calls.append(retry)
                if getattr(again, "status", None) == "completed" and not score._has_refusal(getattr(again, "output", None)):
                    for item in json.loads(again.output_text)["answers"]:
                        if type(item.get("index")) is int and 0 <= item["index"] < len(empty) and _clean(item.get("text")):
                            items[empty[item["index"]]] = item
                    retry["valid"] = True
            except (AttributeError, TypeError, ValueError, KeyError):
                pass
        notes = {}  # una vez más: una opinión sin texto y una abierta que suena a plantilla
        for n, q in enumerate(questions):
            if q["kind"] != "open" or n in fixed or n in held or n in coded:
                continue
            item = items.get(n) or {}
            text = _clean(item.get("text"))
            if n in opinions and (item.get("source") not in ANSWER_SOURCES or not text):
                notes[n] = OPINION_NOTE
            elif item.get("source") in ANSWER_SOURCES and text and (hits := ai_hits(text)):
                notes[n] = STYLE_NOTE.format(phrase=hits[0])
        if notes and set(items) <= set(everything):
            order = list(notes)
            style = {"cost": 0.0, "tokens": (0, 0, 0), "valid": False}
            try:
                asked = user + render(order) + "\n\nNotas por pregunta:\n" + "\n".join(f"{k}. {notes[n]}" for k, n in enumerate(order))
                again = score.ask_json(client, model, ANSWER_SYSTEM, asked, "respuestas", _answers_schema())
                tokens = score._tokens(again)
                style["cost"], style["tokens"] = score.cost_usd(model, *tokens), tokens
                if calls is not None:
                    calls.append(style)
                if getattr(again, "status", None) == "completed" and not score._has_refusal(getattr(again, "output", None)):
                    for item in json.loads(again.output_text)["answers"]:
                        if type(item.get("index")) is int and 0 <= item["index"] < len(order) and item.get("source") in ANSWER_SOURCES and _clean(item.get("text")):
                            items[order[item["index"]]] = item
                    style["valid"] = True
            except (AttributeError, TypeError, ValueError, KeyError):
                pass
    # puede faltar una pregunta solo si el usuario ya la contestó con el texto de una de sus opciones (esa manda)
    missing = set(range(len(questions))) - set(items)
    if (not set(items) <= set(range(len(questions))) or len(data["answers"]) != len(items)
            or any(n not in fixed and n not in held and n not in coded and n not in opinions and (questions[n]["kind"] != "closed" or declared_option(questions[n], declared) is None) for n in missing)):
        raise AnswerError("la respuesta del modelo no contesta exactamente cada pregunta.")
    call["valid"] = True
    out = []
    for n, q in enumerate(questions):
        item = items.get(n, {})
        entry = {"question": q["text"], "kind": q["kind"], "options": list(q["options"]), "option": None, "text": None,
                 "source": item.get("source") if item.get("source") in ANSWER_SOURCES else "unknown", "reason": None}
        if entry["source"] == "unknown":
            entry["reason"] = "el modelo dijo unknown"
        mandated = declared_option(q, declared) if q["kind"] == "closed" else None
        if n in fixed:  # abierta de sueldo con `salary` cargado: va tal cual, sin depender del modelo
            entry["source"], entry["text"], entry["reason"], entry["fixed"] = "answers", fixed[n], None, fixed_label(q, declared)
        elif n in held:
            entry["source"], entry["reason"] = "unknown", held[n]
        elif n in coded:  # interés (siempre sí) y disponibilidad (availability + modality): la respuesta sale del código
            rule = coded[n]
            if "pending" in rule:
                entry["source"], entry["reason"] = "unknown", rule["pending"]
            else:
                entry["source"], entry["reason"], entry["fixed"] = "answers", None, rule["label"]
                entry["option" if "option" in rule else "text"] = rule.get("option", rule.get("text"))
        elif n in opinions:
            _opinion_entry(entry, item, q, chosen["resume"], declared, durations, cv_text, truths, offer)
        elif mandated is None and (listed := list_experience_yes(q, chosen["resume"], cv_text, truths)) is not None:  # una opción de la lista está en el CV: Sí
            entry["source"], entry["option"], entry["reason"] = "cv", listed, None
        elif mandated is not None:  # lo declarado es igual a una opción: se envía esa, diga lo que diga el modelo (unknown, inválida o ausente)
            entry["source"], entry["option"], entry["reason"], entry["fixed"] = "answers", mandated, None, "respuesta declarada a esta pregunta"
        elif (entry["source"] != "unknown" and q["kind"] == "closed" and is_range_question(q)
              and not (entry["source"] == "answers" and declared_for(q, declared))):  # cuánta experiencia: el código suma los meses
            option, why = _experience_answer(q, item.get("jobs"), months)
            if option is None:
                entry["source"], entry["reason"] = "unknown", why
            else:
                entry["source"], entry["option"] = "cv", option
        elif entry["source"] != "unknown" and q["kind"] == "closed":
            option = item.get("option")
            if type(option) is int and 0 <= option < len(q["options"]):
                entry["option"] = option
                # una cifra (o una duración de experiencia) solo sale de lo que el usuario declaró: el CV no alcanza (regla 6)
                if (has_figure(q["options"][option]) or is_range_question(q)) and not (
                        entry["source"] == "answers" and (declared_for(q, declared) or _shift_fits(q, option, declared))):
                    entry["source"], entry["option"] = "unknown", None
                    entry["reason"] = ("el control: pregunta de duración de experiencia sin respuesta declarada" if is_range_question(q)
                                       else "el control: opción con cifra sin respuesta declarada")
            elif (word := _word_option(q["options"], item.get("text"))) is not None:  # el modelo dijo «sí» o «no» sin número de opción
                entry["option"] = word
            else:
                entry["source"], entry["reason"] = "unknown", "el control: la opción elegida no existe"
        elif entry["source"] != "unknown":
            text = " ".join(str(item.get("text") or "").split())
            problems = _open_text_problems(text, chosen["resume"], declared, durations, cv_text, truths, _offer_values(offer)) if text else []
            cleaned = _humanize(text, declared, q["text"]) if text and not problems else None
            if cleaned:
                cleaned = _drop_invented_interest(cleaned, chosen["resume"], cv_text, truths)
            blocked = None
            if cleaned:
                cleaned, blocked = _on_topic(cleaned, q["text"])
            if cleaned and _study_conflict(cleaned, chosen["resume"], today):
                cleaned, blocked = None, STUDY_REASON
            if cleaned:
                entry["text"] = cleaned
            elif blocked:
                entry["source"], entry["reason"] = "unknown", blocked
            elif text and not problems:
                entry["source"], entry["reason"] = "unknown", STYLE_REASON
            else:
                entry["source"], entry["reason"] = "unknown", ("el modelo no escribió respuesta" if not text else _control_reason(problems))
        if (n not in fixed and n not in held and n not in coded and n not in opinions and mandated is None and entry["source"] != "unknown"
                and not (q["kind"] == "closed" and is_range_question(q))):
            _guard_answer(entry, q, item.get("jobs"), months, chosen["resume"], support)
            if entry["source"] != "unknown":
                _source_guard(entry, q, declared, chosen["resume"], cv_text, truths)
        out.append(entry)
    return out


def answer_text(entry: dict) -> str:
    """La respuesta de una pregunta ya contestada, como texto (la opción elegida o el texto abierto)."""
    return entry["options"][entry["option"]] if entry["kind"] == "closed" else entry["text"]


# ── postulación en vivo ──────────────────────────────────────────────────────

PENDING_QUESTION = "pregunta sin respuesta"
MAX_SOFT_FAILURES = 3
REVIEW_MESSAGE = ("«Revisar cada CV antes de subirlo» está activado (apply.review_cv): apply --live no sube CVs a medida que no "
                  "revisaste. Revisá los CVs de data/cvs/ (cada uno tiene su .revision.html) o apagá la revisión en Parámetros.")


def human_pause() -> None:
    """Pausa entre postulaciones (regla 4): varios segundos, con variación."""
    time.sleep(random.uniform(6.0, 14.0))


def review_required(profile: dict) -> bool:
    """Si hay que revisar los CVs a medida antes de subirlos (`apply.review_cv`, por defecto sí)."""
    return profile["apply"].get("review_cv", DEFAULT_REVIEW_CV)


def application_key(title, company, location) -> tuple:
    """La clave con la que se reconoce una oferta en «Mis postulaciones»: (título, empresa, lugar) sin mayúsculas, acentos ni
    espacios de más. Sin empresa (confidencial) es (título, lugar)."""
    parts = [score._fold(str(part or "")) for part in (title, company, location)]
    return (parts[0], parts[1], parts[2]) if parts[1] else (parts[0], parts[2])


def application_index(applications: list[dict]) -> dict[tuple, str]:
    """clave de `application_key` -> `im` de cada postulación que leyó `browser.read_applications`."""
    return {application_key(a["title"], a["company"], a["location"]): a["im"] for a in applications}


def describe_error(error: Exception) -> str:
    """Un error inesperado para el informe: la clase y la primera línea de su mensaje, sin query strings (llevan ids y tokens)."""
    lines = str(error).strip().splitlines()
    first = re.sub(r"(https?://[^\s?#]+)[?#]\S*", r"\1", lines[0]).strip()[:200] if lines else ""
    return f"{type(error).__name__}: {first}" if first else type(error).__name__


def make_cv_provider(conn: sqlite3.Connection, profile: dict, client, model: str, cv_dir: Path = Path("data/cvs"),
                     stop_file: Path = Path("STOP"), pw=None, archive_dir: Path | None = None):
    """La función que arma (o reutiliza) el CV a medida de una oferta del plan y lo deja listo para subir.

    Usa el CV `cv.CV_PROMPT_VERSION` guardado de la oferta y su variante si sus archivos siguen; si no, `cv.make` (el gasto
    entra en el tope mensual). Su copia local se llama `cv.application_pdf_name` (`CV-<Nombre>-<Apellido>-<Puesto>-<Empresa>.pdf`, del
    título y la empresa de la oferta) es el nombre de la copia local (`local`); el CV se sube como `cv.natural_pdf_name` (`CV-<Nombre>-<Apellido>-<Puesto>.pdf`,
    con la búsqueda que trajo la oferta) en una carpeta temporal; si la oferta ya tiene un `cv_file` en `applications`, conserva esos nombres. Devuelve
    `{"path", "name", "local", "version", "dir", "cost_usd"}`; `dir` se borra al terminar la oferta.
    Con `pw` (la instancia de Playwright que ya está abierta) imprime el PDF con esa, sin abrir otro `sync_playwright`.
    Si la oferta ya tiene `cv_file` (y su versión es la actual) y `archive_dir` tiene esa copia, se sube esa copia: no se llama al modelo.
    """
    render = {"render": lambda html, pdf: cv.render_pdf(html, pdf, pw=pw)} if pw is not None else {}

    def provide(item: dict) -> dict:
        variant = next(v for v in profile["variants"] if v["id"] == item["variant"])
        row = conn.execute("SELECT html, pdf, prompt_version FROM cvs WHERE id16 = ? AND variant = ?", (item["id16"], item["variant"])).fetchone()
        cost = 0.0
        saved = conn.execute("SELECT cv_file, cv_version, COALESCE(NULLIF(local_file, ''), cv_file) FROM applications WHERE id16 = ?",
                             (item["id16"],)).fetchone()
        copy = archive_dir / saved[2] if archive_dir is not None and saved and saved[0] else None
        if copy is not None and copy.is_file() and saved[1] in (None, "", cv.CV_PROMPT_VERSION):  # la copia de cv/postulaciones/ sirve
            folder = Path(tempfile.mkdtemp(prefix="compuapply-"))
            try:
                shutil.copyfile(copy, folder / saved[0])
            except OSError:
                shutil.rmtree(folder, ignore_errors=True)
                raise
            return {"path": folder / saved[0], "name": saved[0], "local": saved[2], "version": saved[1] or cv.CV_PROMPT_VERSION, "dir": folder,
                    "cost_usd": 0.0}
        if row and row[2] == cv.CV_PROMPT_VERSION and Path(row[0]).is_file() and Path(row[1]).is_file():
            html_path, pdf_path, version = Path(row[0]), Path(row[1]), row[2]
        else:
            if client is None:
                raise RuntimeError(NO_KEY)
            best = conn.execute("SELECT best_variant FROM scores WHERE id16 = ? AND model = ? AND prompt_version = ?",
                                (item["id16"], model, PROMPT_VERSION)).fetchone()
            chosen_ = {"variant": item["variant"]} if best and best[0] != item["variant"] else {}  # tanda con una variante que no es la mejor
            made = cv.make(conn, profile, client, model, item["id16"], out_dir=cv_dir, stop_file=stop_file, **chosen_, **render)
            html_path, pdf_path, version, cost = made["html"], made["pdf"], made["version"], made["cost_usd"]
        stored = conn.execute("SELECT cv_file, COALESCE(NULLIF(local_file, ''), cv_file) FROM applications WHERE id16 = ? "
                              "AND COALESCE(cv_version, '') != ?", (item["id16"], BASE_VERSION)).fetchone()
        if stored and stored[0]:  # un CV ya subido de esta oferta conserva sus nombres (así el reuso lo reconoce)
            name, local = stored
        else:  # se sube con el puesto genérico (la búsqueda); la copia local lleva el puesto y la empresa de la oferta
            title, company, keyword = conn.execute("SELECT title, company, keyword FROM offers WHERE id16 = ?", (item["id16"],)).fetchone() or ("", "", "")
            person = variant["resume"].get("personal_information")
            name = cv.natural_pdf_name(person, keyword, variant["id"])  # el número (-2, -3…) lo pone `cv_fn`, que ve la cuenta
            local = cv.application_pdf_name(person, title, company or None)
            if conn.execute("SELECT 1 FROM applications WHERE COALESCE(NULLIF(local_file, ''), cv_file) = ? AND id16 != ?", (local, item["id16"])).fetchone():
                local = cv.add_suffix(local, item["id16"])  # ya es la copia local de otra oferta
        folder = Path(tempfile.mkdtemp(prefix="compuapply-"))
        try:
            shutil.copyfile(pdf_path, folder / name)
        except OSError:
            shutil.rmtree(folder, ignore_errors=True)
            raise
        return {"path": folder / name, "name": name, "local": local, "version": version, "dir": folder, "cost_usd": cost}

    return provide


RESTORE_TRIES, RESTORE_WAIT_S = 3, 10  # restaurar el CV por defecto y la verificación final: intentos y segundos entre ellos
RESTORE_COMMAND = "python -m compuapply session restore-default"


def is_timeout(error: BaseException) -> bool:
    """Si el error es un timeout del navegador (`TimeoutError` de Playwright o un mensaje con «Timeout … ms»)."""
    return "timeout" in type(error).__name__.lower() or "Timeout" in str(error)


def _fresh_page(page):
    """Una página nueva del mismo contexto (la vieja puede haber quedado colgada); la misma si no se puede abrir otra."""
    try:
        return page.context.new_page()
    except Exception:
        return page


def with_retries(page, action):
    """`action(page)`; si falla por timeout se reintenta hasta `RESTORE_TRIES` veces en total, con una página nueva y `RESTORE_WAIT_S` segundos
    de espera. Otros errores, o el último timeout, se propagan."""
    for attempt in range(1, RESTORE_TRIES + 1):
        try:
            return action(page)
        except Exception as e:
            if attempt == RESTORE_TRIES or not is_timeout(e):
                raise
            time.sleep(RESTORE_WAIT_S)
            page = _fresh_page(page)


def restore_default_cv(page, profile: dict, variant_id: str | None = None) -> dict:
    """Pone como CV por defecto de la cuenta el `default_pdf_name` de la variante (la primera que lo declare si no se da una). No postula,
    sube ni borra nada. Devuelve `{"name", "variant", "was", "now"}`; lanza RuntimeError si la variante no declara el nombre o la cuenta no lo tiene."""
    variant = next((v for v in profile["variants"] if (variant_id is None or v["id"] == variant_id) and v.get("default_pdf_name")), None)
    if variant is None:
        raise RuntimeError("la variante no existe o no declara default_pdf_name en profile.yaml.")
    name = variant["default_pdf_name"]

    def go(p):
        was = next((c["name"] for c in browser.account_cvs(p) if c["default"]), None)
        if was is None or not browser.same_name(name, was):
            browser.set_default(p, name)
        now = next((c["name"] for c in browser.account_cvs(p) if c["default"]), None)
        return {"name": name, "variant": variant["id"], "was": was, "now": now}

    return with_retries(page, go)


def _restore_default(page, profile: dict, result: dict) -> None:
    """Vuelve a marcar como por defecto el CV base (`default_pdf_name` de la primera variante del perfil que lo declare) y lo
    verifica, con reintentos si falla por timeout (`with_retries`); el resultado queda en `result["restore"]` (`name`, `variant`, `ok`, `message`).
    Si no se pudo, la primera línea de `result["halted"]` dice dónde quedó el CV por defecto y el comando que lo arregla."""
    variant = next((v for v in profile["variants"] if v.get("default_pdf_name")), {})
    name = variant.get("default_pdf_name")
    state = result["restore"] = {"name": name, "variant": variant.get("id"), "ok": None, "message": ""}
    if not name:
        state["message"] = "ninguna variante declara default_pdf_name: quedó por defecto el último CV subido"
        return
    try:
        with_retries(page, lambda p: browser.set_default(p, name))
        state["ok"] = True
    except Exception as e:  # el programa ya no confía en el estado de la cuenta: se avisa, con la clase y la primera línea si no es un error conocido
        state["ok"], state["message"] = False, str(e) if isinstance(e, RuntimeError) else f"error inesperado del navegador ({describe_error(e)})"
        where = result.get("last_uploaded")
        first = f"el CV por defecto quedó en {'«' + where + '»' if where else 'un CV a medida'}: corré `{RESTORE_COMMAND}`"
        now = f"No se pudo dejar «{name}» como CV por defecto: {state['message']}"
        result["halted"] = f"{first}. {result['halted'] + ' · ' if result['halted'] else ''}{now}"


def _reusable(conn: sqlite3.Connection, item: dict, info: dict, cvs: list[dict]) -> bool:
    """Si el CV de la oferta ya está subido en la cuenta y se puede marcar sin subir otra copia: una fila de esa misma oferta
    guarda ese `cv_file` y esa `cv_version`, `account_cvs` tiene un PDF con ese nombre y esta oferta es la fila más nueva con ese `cv_file`
    (las filas viejas con el mismo nombre son de archivos que ya no están)."""
    row = conn.execute("SELECT cv_file, cv_version FROM applications WHERE id16 = ?", (item["id16"],)).fetchone()
    if not row or not row[0] or row[0] != info["name"] or row[1] != info.get("version"):
        return False
    if not any(browser.same_file(info["name"], cv_["name"]) for cv_ in cvs):
        return False
    newest = conn.execute("SELECT id16 FROM applications WHERE cv_file = ? ORDER BY applied_at DESC, rowid DESC LIMIT 1", (info["name"],)).fetchone()
    return newest is not None and newest[0] == item["id16"]  # el archivo de la cuenta es de la fila más nueva con ese nombre


def _answerable(question: str, declared: dict) -> bool:
    """Si una pregunta pendiente ya se puede contestar sin el modelo: es de sueldo y está la pretensión de su jornada, es de dónde vive o
    de movilidad y están esos datos, o su clave normalizada está en `answers`."""
    asked = {"text": question, "kind": "open", "options": []}
    pay = is_pay_question(asked) and bool(_clean(declared.get(pay_key(asked) or "")))
    return (pay or (not is_pay_question(asked) and (contact_text(asked, declared) is not None or residence_answer(asked, declared) is not None))
            or normalize_question(question) in declared)


def requeue_answerable(conn: sqlite3.Connection, profile: dict) -> int:
    """Las ofertas en `pending_question` cuyas preguntas pendientes ya se pueden contestar (`_answerable`), o que quedaron pendientes con
    otra `ANSWER_PROMPT_VERSION` (instrucciones viejas), vuelven a `new` y sus preguntas salen de `pending_questions`. Devuelve cuántas
    ofertas volvieron."""
    declared = profile.get("answers", {})
    pending: dict[str, list[str]] = {}
    old = set()
    for id16, question, version in conn.execute("SELECT p.id16, p.question, p.answer_version FROM pending_questions p JOIN offers o "
                                                "ON o.id16 = p.id16 WHERE o.status = 'pending_question'"):
        pending.setdefault(id16, []).append(question)
        if version != ANSWER_PROMPT_VERSION:
            old.add(id16)
    requeued = 0
    for id16, questions in pending.items():
        if id16 in old or all(_answerable(q, declared) for q in questions):
            conn.execute("DELETE FROM pending_questions WHERE id16 = ?", (id16,))
            conn.execute("UPDATE offers SET status = 'new' WHERE id16 = ? AND status = 'pending_question'", (id16,))
            requeued += 1
    conn.commit()
    return requeued


def complete_cv_files(conn: sqlite3.Connection, profile: dict, cvs: list[dict]) -> int:
    """Completa `cv_file` y `cv_version` de las filas de `applications` en `cv_uploaded` o `pending_question` que se subieron antes de que
    se guardara el archivo. Si la oferta tiene un CV en `cvs` de esa variante y versión, se calcula el nombre viejo
    (`cv.pdf_name` con el titular de ese CV) y, si `cvs` (la cuenta) tiene un PDF con ese nombre, se guarda. Si dos ofertas dan el
    mismo nombre no se completa ninguna. Devuelve cuántas filas completó."""
    found: dict[str, list[tuple]] = {}
    for id16, variant_id, version in conn.execute(
            "SELECT id16, variant_id, cv_version FROM applications WHERE status IN ('cv_uploaded', 'pending_question') "
            "AND (cv_file IS NULL OR cv_file = '')").fetchall():
        variant = next((v for v in profile["variants"] if v["id"] == variant_id), None)
        saved = conn.execute("SELECT html, prompt_version FROM cvs WHERE id16 = ? AND variant = ?", (id16, variant_id)).fetchone()
        wanted = version or cv.CV_PROMPT_VERSION
        if variant is None or not saved or saved[1] != wanted or not Path(saved[0]).is_file():
            continue
        name = cv.pdf_name(variant["resume"].get("personal_information"), cv.headline_of(Path(saved[0])))
        if any(browser.same_file(name, c["name"]) for c in cvs):
            found.setdefault(browser._plain(name), []).append((id16, name, wanted))
    done = 0
    for rows_ in found.values():
        if len(rows_) == 1:
            id16, name, version = rows_[0]
            conn.execute("UPDATE applications SET cv_file = ?, local_file = ?, cv_version = ? WHERE id16 = ?", (name, name, version, id16))
            done += 1
    conn.commit()
    return done


FINAL_STATUS, SEEN_STATUS = "proceso finalizado", "cv visto"  # estados de «Mis postulaciones» (sin mayúsculas)
UNSENT = ("cv_uploaded", "pending_question")  # postulaciones del programa cuya oferta no se envió
LIMIT_MESSAGE = ("La cuenta llegó a {limit} CVs (límite de Computrabajo) y ninguno se puede borrar sin riesgo: "
                 "borrá uno a mano en la página de CV")


def _protected_names(profile: dict) -> list[str]:
    """Los nombres de los CV que nunca se borran: el `default_pdf_name` y el PDF de cada variante del perfil."""
    names = []
    for variant in profile["variants"]:
        names += [variant.get("default_pdf_name"), Path(str(variant["pdf"])).name if variant.get("pdf") else None]
    return [name for name in names if name]


def choose_deletion(conn: sqlite3.Connection, profile: dict, cvs: list[dict], site_apps: list[dict],
                    now: datetime | None = None, current: str | None = None) -> dict | None:
    """El CV de la cuenta que se puede borrar sin quitarle nada a una empresa, o None. Cada uno: `{"id", "name", "reason"}`.

    En este orden: (1) una copia repetida del `cv_file` de una oferta no enviada (queda al menos una); (1b) el CV de una oferta no
    enviada (`pending_question` o `cv_uploaded`), la de `applied_at` más viejo, si ninguna postulación enviada usa ese archivo ni la
    oferta `current` (la que se está por postular); la copia de `cv/postulaciones/` queda y la fila conserva su `cv_file`; (2) rotación:
    el CV de la postulación enviada más vieja (`applied_at`), en cualquier estado (también «Postulado» de menos de 30 días); si un
    archivo lo usan varias postulaciones, cuenta la más nueva. Nunca el CV por defecto, el `default_pdf_name` ni el PDF de una
    variante, ni un CV que no figure en `applications`, ni el de la oferta `current`.
    """
    protected = _protected_names(profile)
    rows = conn.execute("SELECT a.id16, a.status, a.cv_file, a.im, a.applied_at, o.company, a.rowid FROM applications a "
                        "LEFT JOIN offers o ON o.id16 = a.id16 WHERE a.cv_file IS NOT NULL AND a.cv_file != ''").fetchall()

    def uses(name: str) -> list[tuple]:
        """Un archivo de la cuenta es de **una** fila: la más nueva que lo subió (`applied_at`; si empatan, la última escrita). Las demás
        filas con ese nombre son de CVs que ya no están en la cuenta."""
        same = [r for r in rows if browser.same_file(r[2], name)]
        return [max(same, key=lambda r: (r[4] or "", r[6]))] if same else []

    free = [c for c in cvs if not c["default"] and not any(browser.same_name(p, c["name"]) for p in protected)]
    for cv_ in free:  # 1. copia repetida de una oferta no enviada
        mine = uses(cv_["name"])
        copies = [c for c in cvs if browser.same_file(c["name"], cv_["name"])]
        if mine and len(copies) >= 2 and all(r[1] in UNSENT for r in mine):
            return {"id": cv_["id"], "name": cv_["name"], "reason": "copia repetida del CV de una oferta que no se envió"}
    unsent = []
    for cv_ in free:  # 1b. el CV de una oferta que nunca se envió
        mine = uses(cv_["name"])
        if mine and all(r[1] in UNSENT and r[0] != current for r in mine):
            unsent.append((min(r[4] or "" for r in mine), cv_))
    if unsent:
        _, cv_ = min(unsent, key=lambda r: r[0])
        return {"id": cv_["id"], "name": cv_["name"], "reason": "CV de una oferta pendiente que no se envió"}
    sent = []
    for cv_ in free:  # 2. rotación: la postulación enviada más vieja
        mine = uses(cv_["name"])
        if mine and all(r[1] not in UNSENT and r[0] != current for r in mine):
            newest = max(mine, key=lambda r: r[4] or "")
            sent.append((newest[4] or "", cv_, newest))
    if not sent:
        return None
    when, cv_, row = min(sent, key=lambda r: r[0])
    return {"id": cv_["id"], "name": cv_["name"],
            "reason": f"rotación: el CV de la postulación más vieja ({row[5] or 'sin empresa'}, {when[:10] or 'sin fecha'})"}


class FullAccount(RuntimeError):
    """La cuenta está en el límite de CVs y no hay uno para rotar ni un CV base que usar."""


FULL_REASON = "cuenta llena y sin CV para rotar"
BASE_VERSION = "base"  # `applications.cv_version` de una postulación enviada con el PDF base de la variante (cuenta llena)


def base_pdf(profile: dict, variant_id: str, cvs: list[dict]) -> dict | None:
    """El PDF base de la variante tal como figura en la cuenta: `{"name", "path"}`. Primero su `default_pdf_name`, después el nombre de su `pdf`
    (`same_file`). None si la variante no tiene ninguno de los dos o no está en `cvs`."""
    variant = next((v for v in profile["variants"] if v["id"] == variant_id), None)
    if variant is None:
        return None
    others = [n for v in profile["variants"] if v["id"] != variant_id
              for n in (v.get("default_pdf_name"), Path(str(v["pdf"])).name if v.get("pdf") else None) if n]  # los que declara otra variante
    for wanted in (variant.get("default_pdf_name"), Path(str(variant["pdf"])).name if variant.get("pdf") else None):
        shown = next((c["name"] for c in cvs if wanted and browser.same_file(wanted, c["name"])), None)
        if shown and not any(browser.same_file(shown, other) for other in others):
            return {"name": shown, "path": Path(str(variant["pdf"])) if variant.get("pdf") else Path(shown)}
    return None


def _free_slot(conn: sqlite3.Connection, profile: dict, page, cvs: list[dict], site_apps: list[dict], result: dict, limit: int,
               current: str | None = None, variant: str | None = None) -> dict | None:
    """Antes de subir: lee la cuenta y, si tiene `limit` CVs, borra uno (`choose_deletion`) y lo verifica. Si no hay lugar posible (nada se
    puede borrar sin riesgo, o `apply.free_cv_slots` está en false) devuelve el PDF base de la variante que ya está en la cuenta
    (`base_pdf`; no vale si su nombre lo declara otra variante): la oferta se envía con ese, sin subir ni borrar. Si ese tampoco está, lanza
    `FullAccount` (la corrida frena; en una tanda se salta la oferta y se sigue)."""
    cvs[:] = browser.account_cvs(page)
    if len(cvs) < limit:
        return None
    message = LIMIT_MESSAGE.format(limit=limit)
    choice = (choose_deletion(conn, profile, cvs, site_apps, current=current)
              if profile["apply"].get("free_cv_slots", DEFAULT_FREE_CV_SLOTS) else None)
    if choice is None:
        base = base_pdf(profile, variant, cvs) if variant else None
        if base is None:
            raise FullAccount(message + ("." if profile["apply"].get("free_cv_slots", DEFAULT_FREE_CV_SLOTS)
                                          else " (apply.free_cv_slots está en false: el programa no borra CVs)."))
        return base
    browser.delete_cv(page, choice["id"], choice["name"])  # verifica recargando; si no, frena
    local = next((r[1] for r in conn.execute("SELECT cv_file, COALESCE(NULLIF(local_file, ''), cv_file) FROM applications WHERE cv_file IS NOT NULL "
                                             "AND cv_file != '' ORDER BY applied_at DESC") if browser.same_file(r[0], choice["name"])), "")
    result["deleted"].append({"name": choice["name"], "local": local, "reason": choice["reason"]})
    cvs[:] = [c for c in cvs if c["id"] != choice["id"]]
    return None


def _halt_text(error: Exception) -> str:
    """Por qué frenó la corrida: el mensaje de un error conocido (`RuntimeError`) o, de un error inesperado, su clase y la primera línea."""
    if isinstance(error, RuntimeError):
        return str(error)
    return f"error inesperado del navegador ({describe_error(error)}): {browser.HALT}."


def _check_start_default(page, profile: dict, cvs: list[dict], result: dict) -> None:
    """Antes de la primera oferta: si el CV por defecto de la cuenta no es el `default_pdf_name` de la primera variante que lo
    declara, lo restaura. El resultado queda en `result["start_default"]`; si no se puede, `result["halted"]` y no se postula."""
    variant = next((v for v in profile["variants"] if v.get("default_pdf_name")), None)
    if variant is None:
        return
    name = variant["default_pdf_name"]
    was = next((c["name"] for c in cvs if c["default"]), None)
    state = result["start_default"] = {"name": name, "variant": variant["id"], "was": was, "restored": False, "ok": True, "message": ""}
    if was is not None and browser.same_name(name, was):
        return
    state["restored"] = True
    try:
        browser.set_default(page, name)
    except Exception as e:
        state["ok"], state["message"] = False, _halt_text(e)
        result["halted"] = f"No se pudo dejar «{name}» como CV por defecto antes de empezar: {state['message']} No se postuló a nada."


def run_live(conn: sqlite3.Connection, profile: dict, model: str, client, page, today: date, stop_file: Path = Path("STOP"),
             pause=human_pause, apply_one=None, cv_provider=None, cv_dir: Path = Path("data/cvs"), pw=None,
             archive_dir: Path | None = None, batch: dict | None = None, runs_dir: Path | None = None) -> dict:
    """Postula a las ofertas que el plan manda a «Enviaría», una por una, con el navegador que maneja `page`.

    Antes de empezar frena, sin tocar el navegador, si `apply.review_cv` está activado (por defecto) o si la cuenta tiene más
    PDFs que `run.max_account_pdfs`; después lee «Mis postulaciones» una sola vez (solo lectura) y, si el CV por defecto de la
    cuenta no es el `default_pdf_name` de la primera variante que lo declara, lo restaura **antes de la primera oferta** (si no
    puede, frena sin postular). Por cada oferta: si ya está
    en «Mis postulaciones» (`application_key`) la marca `already_applied` con su `im` y sigue, sin armar ni subir nada; si no,
    arma el CV a medida (`make_cv_provider`), lo sube con un nombre presentable, verifica que quedó por defecto, **recién entonces**
    postula y verifica qué archivo quedó adjunto (`browser.apply_one`). Al final, aunque frene, vuelve a marcar como por
    defecto el CV base de la primera variante del perfil que declare `default_pdf_name`. `pw` es la instancia de Playwright
    abierta: con ella se imprimen los PDF de los CV que haya que armar (no se abre otra). Con `archive_dir`
    (`cv/postulaciones`) cada PDF que se sube se copia ahí con su mismo nombre; esa copia nunca se borra.
    Con la cuenta en `run.max_account_pdfs` CVs (10, el límite de Computrabajo) libera un lugar antes de subir (`_free_slot`).
    Con `batch` (una tanda: `ids`, `variant`, `max`, y `deadline` con su `clock`) el plan es solo el de esas ofertas con el puntaje de esa
    variante, `max` reemplaza al tope diario, pasado `deadline` no se empieza otra oferta (la que está en curso termina) y una oferta sin
    lugar en la cuenta ni CV para rotar se salta («cuenta llena y sin CV para rotar») y se sigue. `batch["company_cap"]` (por defecto True)
    enciende el tope por empresa y `batch["defer_verify"]` deja la verificación final para quien llama (la tanda va por tramos); `batch["applications"]` es la lista de «Mis
    postulaciones» que la tanda leyó una vez al principio: no se vuelve a leer y se le suma cada oferta enviada.

    Devuelve `plan`, `sent` y `skipped` (listas de ofertas con su motivo; las enviadas suman `file`, `version`, `questions` y
    `alert` y `repaired`), `halted` (None, o por qué frenó la corrida), `cost_usd` (todo lo que quedó en `api_spend` durante la
    corrida, también lo de un CV que falló), `pdfs` (`before`, `max`, `uploaded`), `read` (cuántas postulaciones leyó de «Mis
    postulaciones»; None si no leyó), `start_default` (`name`, `variant`, `was`, `restored`, `ok`, `message`; None si no se
    comprobó) y `restore` (`name`, `variant`, `ok`, `message`; None si no se subió nada). Frena ante `STOP`, un error del
    navegador o de la API, una pantalla desconocida, el tope mensual de gasto y 3 respuestas inválidas del modelo seguidas.
    Cada postulación queda en `applications` con sus preguntas y respuestas, el archivo subido, el `im` y la versión del CV;
    una oferta con una pregunta sin respuesta no se envía: queda `pending_question` y la pregunta en `pending_questions`.
    """
    apply_one = apply_one or browser.apply_one
    provide = cv_provider or make_cv_provider(conn, profile, client, model, cv_dir, stop_file, pw=pw, archive_dir=archive_dir)
    started = datetime.now(timezone.utc).isoformat()
    pruned = prune_archive(archive_dir) if archive_dir is not None else None  # las copias de más de 30 días de cv/postulaciones/
    requeued = requeue_answerable(conn, profile)  # antes del plan: esas ofertas vuelven a la cola
    plan_ = plan(conn, profile, model, today, **({"only": batch["ids"], "variant": batch["variant"], "cap": batch["max"],
                                                                            "company_cap_on": batch.get("company_cap", True)} if batch else {}))
    plan_["archive_pruned"] = pruned
    result = {"plan": plan_, "requeued": requeued, "completed": 0, "sent": [], "skipped": [], "halted": None, "cost_usd": 0.0,
              "pdfs": {"before": None, "max": profile["run"].get("max_account_pdfs", DEFAULT_MAX_ACCOUNT_PDFS), "uploaded": 0, "reused": 0},
              "restore": None, "read": None, "start_default": None, "deleted": [], "pending": [], "answers": [], "time_up": False,
              "unconfirmed_now": [], "reconciled": []}
    streak = batch.setdefault("streak", {"n": 0}) if batch is not None else {"n": 0}  # `Unconfirmed` seguidas (la tanda la comparte entre tramos)
    run_name, private = datetime.now().strftime("%Y%m%d-%H%M%S"), personal_values(profile)
    todo = list(plan_["send"])
    limit, site_apps = result["pdfs"]["max"], []

    def skip_all(reason: str) -> dict:
        result["skipped"] += [{**i, "reason": reason} for i in todo]
        return result

    if todo and review_required(profile):
        result["halted"] = REVIEW_MESSAGE
        return skip_all("sin procesar: la revisión de CVs está activada")
    if todo:
        try:
            cvs = browser.account_cvs(page)
        except Exception as e:
            result["halted"] = _halt_text(e)
            return skip_all("sin procesar: la corrida frenó")
        result["pdfs"]["before"] = len(cvs)
        result["completed"] = complete_cv_files(conn, profile, cvs)  # los CV subidos antes de la 042: se reconocen para reusarlos y borrar copias
        if len(cvs) > result["pdfs"]["max"]:
            result["halted"] = (f"Hay {len(cvs)} PDFs en la cuenta de Computrabajo (el máximo es {result['pdfs']['max']}, "
                                "run.max_account_pdfs): limpiá la página de CV a mano y volvé a correr. No se postuló a nada.")
            return skip_all("sin procesar: demasiados PDFs en la cuenta")
        shared = batch.get("applications") if batch else None  # la tanda las leyó una vez al principio y les suma lo que envía
        try:
            applications = shared if shared is not None else browser.read_applications(page)
        except Exception as e:
            result["halted"] = _halt_text(e)
            return skip_all("sin procesar: la corrida frenó")
        result["read"] = len(applications)
        result["reconciled"] = reconcile_unconfirmed(conn, applications)
        site_apps = applications
        posted = application_index(applications)
        _check_start_default(page, profile, cvs, result)
        if result["halted"]:
            return skip_all("sin procesar: no se pudo dejar el CV por defecto de la cuenta")
    soft, uploaded_any, browsed = 0, False, False

    def stopped(item: dict) -> bool:
        if stop_file.exists():
            result["halted"] = f"Existe el archivo {stop_file}: se frena antes de la oferta «{item['title']}». Borralo para volver a correr."
        return bool(result["halted"])

    try:
        for item in todo:
            if stopped(item):
                break
            if batch and batch.get("deadline") is not None and batch["clock"]() >= batch["deadline"]:
                result["time_up"] = True
                break
            location, keyword = conn.execute("SELECT location, keyword FROM offers WHERE id16 = ?", (item["id16"],)).fetchone() or ("", "")
            im = posted.get(application_key(item["title"], item["company"], location))
            if im is not None:  # ya figura en «Mis postulaciones»: no se arma ni se sube nada ni se abre la oferta
                db.mark_status(conn, item["id16"], "applied")
                db.save_application(conn, item["id16"], item["variant"], item["score"], "already_applied", "ya estaba en Mis postulaciones",
                                    [], None, im, None)
                result["skipped"].append({**item, "reason": "ya estaba postulada (figura en Mis postulaciones)"})
                continue
            if browsed:
                pause()
                if stopped(item):  # después de la pausa: es lo último que se mira antes de abrir la oferta
                    break
            browsed = True
            offer = {"id16": item["id16"], "url": item["url"], "title": item["title"]}
            made = {}

            def uploaded(made=made):
                result["pdfs"]["uploaded"] += 1  # cuenta en cuanto el CV se sube, aunque después la oferta frene
                made["in_account"] = True

            def cv_fn(item=item, made=made):
                nonlocal uploaded_any
                info = provide(item)
                own = conn.execute("SELECT cv_file FROM applications WHERE id16 = ?", (item["id16"],)).fetchone()
                mine = bool(own and own[0] and browser.same_file(own[0], info["name"]))
                made.update(info)
                uploaded_any = True
                result["last_uploaded"] = info["name"]
                reuse = _reusable(conn, item, info, cvs)
                if reuse:
                    result["pdfs"]["reused"] += 1
                    made["in_account"] = True
                else:  # antes de cada subida se lee la cuenta (sin cálculos acumulados) y, si está llena, se libera un lugar
                    base = _free_slot(conn, profile, page, cvs, site_apps, result, limit, current=item["id16"], variant=item["variant"])
                    if base is not None:  # cuenta llena y nada borrable: se postula con el PDF base de la variante, que ya está en la cuenta
                        made.update({"name": base["name"], "version": BASE_VERSION, "in_account": True, "base": True})
                        return {"path": base["path"], "name": base["name"], "version": BASE_VERSION, "uploaded": uploaded, "reuse": True,
                                "base": True, "dir": None}
                    if not mine:  # la cuenta (ya sin el CV borrado) puede tener ese nombre para otra oferta: el primer número libre
                        renamed = cv.numbered_name(info["name"], lambda n: any(browser.same_file(n, c["name"]) for c in cvs))
                        if renamed != info["name"]:
                            info["path"] = info["path"].rename(info["path"].with_name(renamed))
                            info["name"] = made["name"] = renamed
                if archive_dir is not None:  # la copia de lo que se sube queda en la PC (con puesto y empresa) y no se borra
                    archive_dir.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(info["path"], archive_dir / info.get("local", info["name"]))
                return {**info, "uploaded": uploaded, "reuse": reuse}

            def keep_cv(item=item, made=made, reason="CV subido; la corrida frenó antes de postular"):
                """Si el CV de esta oferta quedó en la cuenta, la fila de `applications` guarda su archivo y su versión (para reusarlo)."""
                if made.get("in_account"):
                    db.save_application(conn, item["id16"], item["variant"], item["score"], "cv_uploaded", reason, [], made["name"], None, made["version"],
                                    made.get("local"))

            def answers_fn(questions, item=item, made=made):
                if client is None:
                    raise RuntimeError(NO_KEY)
                cap, spent = profile["run"]["max_cost_month"], score.month_spent(conn, datetime.now(timezone.utc).date())
                if cap - spent <= 0:
                    raise RuntimeError(f"Tope mensual alcanzado: {score.amounts(spent, cap)}. No se contestan más preguntas.")
                calls = []
                try:
                    saved = (None if made.get("base") else  # con el CV base las preguntas se contestan con el CV base
                             conn.execute("SELECT html FROM cvs WHERE id16 = ? AND variant = ?", (item["id16"], item["variant"])).fetchone())
                    zones = profile.get("zones") or []
                    answered = answer_questions(questions, profile, client, model, item["variant"], calls,
                                                cv_text=(cv.text_of(saved[0]) if saved else "") or None, today=today,
                                                offer_in_zone=bool(zones) and in_zone(location, zones),
                                                offer={"title": item["title"], "keyword": keyword, "company": item["company"]})
                finally:  # cada respuesta ya se pagó, sirva o no
                    for call in calls:
                        db.add_spend(conn, "answers", item["id16"], model, call["cost"], call["tokens"][0], call["tokens"][2], call["valid"])
                result["answers"].append({"id16": item["id16"], "title": item["title"], "company": item["company"], "entries": answered})
                unknown = [a for a in answered if a["source"] == "unknown"]
                for a in unknown:
                    db.save_pending_question(conn, item["id16"], a["question"], a["options"], reason=a.get("reason"))
                    result["pending"].append({"title": item["title"], "company": item["company"], "question": a["question"],
                                              "reason": a.get("reason") or "el modelo dijo unknown"})
                return None if unknown else answered

            try:
                outcome = apply_one(page, offer, answers_fn, cv_fn)
            except FullAccount as e:
                keep_cv()
                if batch is None:
                    result["skipped"].append({**item, "reason": "frenó la corrida"})
                    result["halted"] = str(e)
                    break
                result["skipped"].append({**item, "reason": FULL_REASON})
                continue
            except AnswerError as e:
                keep_cv(reason="CV subido; la respuesta del modelo no sirvió")
                soft += 1
                result["skipped"].append({**item, "reason": f"respuesta inválida del modelo: {e}"})
                if soft >= MAX_SOFT_FAILURES:
                    result["halted"] = f"{MAX_SOFT_FAILURES} respuestas inválidas del modelo seguidas: se frena la corrida."
                    break
                continue
            except browser.Unconfirmed as e:  # se tocó el envío pero la pantalla final no confirma esta oferta: no cuenta como enviada
                keep_cv()
                db.mark_status(conn, item["id16"], UNCONFIRMED)
                db.save_application(conn, item["id16"], item["variant"], item["score"], UNCONFIRMED, str(e)[:300], [])
                saved = save_screen(runs_dir or RUNS_DIR, run_name, item["id16"], e, private)
                streak["n"] += 1
                result["unconfirmed_now"].append({**item, "why": str(e), "saved": str(saved) if saved else ""})
                result["skipped"].append({**item, "reason": "sin confirmar: la pantalla final no la confirma"})
                if streak["n"] >= UNCONFIRMED_STREAK:
                    result["halted"] = UNCONFIRMED_HALT
                    break
                continue
            except RuntimeError as e:
                keep_cv()
                result["skipped"].append({**item, "reason": "frenó la corrida"})
                result["halted"] = str(e)
                break
            except Exception as e:  # el navegador falló de una forma que no conocemos: no se sigue; el informe lleva la clase y la primera línea
                keep_cv()
                result["skipped"].append({**item, "reason": "frenó la corrida"})
                result["halted"] = _halt_text(e)
                if is_timeout(e):  # un timeout de click: el dump limpio de la pantalla y qué oferta y paso estaban en curso
                    saved = save_freno(runs_dir or RUNS_DIR, run_name, page, item, e, private)
                    result["halted"] += f" Oferta en curso: «{item['title']}» ({item['company']}), paso: {saved['step']}." + (
                        f" Pantalla guardada en {saved['path']}." if saved["path"] else "")
                break
            finally:
                if made.get("dir"):
                    shutil.rmtree(made["dir"], ignore_errors=True)  # la copia con el nombre presentable no queda en disco
            soft, streak["n"] = 0, 0
            status, qa, used = outcome["status"], outcome.get("qa", []), outcome.get("cv") or {}
            if status == "applied":
                note = " (el adjunto no era el subido y se cambió desde la pantalla final)" if outcome.get("repaired") else ""
                db.mark_status(conn, item["id16"], "applied")
                db.save_application(conn, item["id16"], item["variant"], item["score"], "applied", "Te postulaste correctamente" + note, qa,
                                    used.get("name"), outcome.get("im"), used.get("version"), used.get("local"))
                if batch and batch.get("applications") is not None:
                    batch["applications"].append({"title": item["title"], "company": item["company"], "location": location,
                                                  "status": "Postulado", "im": outcome.get("im") or ""})
                result["sent"].append({**item, "questions": len(qa), "file": used.get("name", ""), "local": used.get("local") or used.get("name", ""), "version": used.get("version", ""),
                                       "alert": bool(outcome.get("alert")), "alerts": outcome.get("alerts") or [], "postapply_s": outcome.get("postapply_s", 0.0),
                                       "repaired": bool(outcome.get("repaired")),
                                       "reused": bool(used.get("reuse")) and not used.get("base"), "base": bool(used.get("base")),
                                       "im": outcome.get("im") or "", "final": outcome.get("final") or ""})
            elif status == "already_applied":
                db.mark_status(conn, item["id16"], "applied")
                db.save_application(conn, item["id16"], item["variant"], item["score"], "already_applied", "ya estaba postulada", [],
                                    used.get("name"), None, used.get("version"), used.get("local"))
                result["skipped"].append({**item, "reason": "ya estaba postulada"})
            else:
                db.mark_status(conn, item["id16"], "pending_question")
                db.save_application(conn, item["id16"], item["variant"], item["score"], "pending_question", PENDING_QUESTION, [],
                                    used.get("name"), None, used.get("version"), used.get("local"))  # el CV ya está en la cuenta: la próxima vez se reusa
                result["skipped"].append({**item, "reason": PENDING_QUESTION})
    finally:
        if uploaded_any:  # se subió al menos un CV: la cuenta queda como estaba, aunque la corrida haya frenado
            _restore_default(page, profile, result)
    blocked = any(t in (result["halted"] or "") for t in ("CAPTCHA", "respondió", "Sin sesión"))  # con el sitio bloqueando no se vuelve a pedir nada
    if result["sent"] and not blocked and not (batch and batch.get("defer_verify")):  # la tanda verifica una sola vez, al final
        result["verify"] = _verify_sent(page, result["sent"], conn)
    if result["halted"] or result["time_up"]:
        done = {i["id16"] for i in result["sent"] + result["skipped"]}
        why = "sin procesar: la corrida frenó" if result["halted"] else "sin procesar: se acabó el tiempo de la tanda"
        result["skipped"] += [{**i, "reason": why} for i in todo if i["id16"] not in done]
    result["cost_usd"] = conn.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM api_spend WHERE at >= ?", (started,)).fetchone()[0]
    return result


VERIFY_PAGES = 10  # páginas de «Mis postulaciones» que la verificación final lee, como máximo
NO_CAP = 10**9  # «sin tope» para `plan(cap=)` y `run_live(batch["max"])`


ALERT_NOTE = "lo crea el sitio al cargar la pantalla de éxito; el programa no toca alertas (D-163)"
UNCONFIRMED_STREAK = 2  # `Unconfirmed` seguidas que frenan la tanda
UNCONFIRMED_HALT = "2 postulaciones seguidas sin confirmar: revisá las pantallas guardadas"
DATA_MARK = "[dato]"
DUMP_MAX = 3000
RUNS_DIR = Path("data/runs")  # donde se guardan las pantallas sin confirmar (los tests lo cambian a una carpeta temporal)
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
PHONE_OR_ID = re.compile(r"\+?\d[\d\s().-]{6,}\d")


def personal_values(profile: dict) -> list[str]:
    """Los textos de `personal_information` de los CV base (nombre, apellido, email, teléfono, documento…), del más largo al más corto."""
    values = set()
    for variant in profile.get("variants") or []:
        info = (variant.get("resume") or {}).get("personal_information")
        if isinstance(info, dict):
            values |= {text for value in info.values() if len(text := _clean(value)) >= 3}
    return sorted(values, key=len, reverse=True)


def scrub(text: str, values: list[str]) -> str:
    """El texto sin datos personales: los del perfil, los emails y las secuencias que parecen teléfono o documento, como «[dato]»."""
    for value in values:
        text = re.sub(re.escape(value), DATA_MARK, text, flags=re.I)
    return PHONE_OR_ID.sub(DATA_MARK, EMAIL.sub(DATA_MARK, text))


def save_screen(runs_dir: Path, run_name: str, id16: str, error, values: list[str]) -> Path | None:
    """Guarda en `runs_dir/<corrida>-postapply-<id16>.txt` el texto visible de la pantalla final que no confirmó (sin datos personales,
    hasta `DUMP_MAX` caracteres) y la lista de bloques con «postul» y su largo. Nunca el HTML ni cookies. None si no se pudo escribir."""
    text = "\n".join(line for line in scrub(getattr(error, "text", ""), values).splitlines() if line.strip())[:DUMP_MAX]
    blocks = [f"- {size} caracteres: {scrub(start, values)}" for size, start in getattr(error, "blocks", [])]
    body = "\n".join([f"Pantalla final sin confirmar ({id16})", "", scrub(str(error), values), "", "## Texto visible", "", text or "(sin texto)", "",
                      "## Bloques con «postul»", "", *(blocks or ["(ninguno)"]), ""])
    try:
        runs_dir.mkdir(parents=True, exist_ok=True)
        path = runs_dir / f"{run_name}-postapply-{id16}.txt"
        path.write_text(body, encoding="utf-8")
        return path
    except OSError:
        return None


def save_freno(runs_dir: Path, run_name: str, page, item: dict, error, values: list[str]) -> dict:
    """Guarda en `runs_dir/<corrida>-freno.txt` el texto visible de la pantalla en que frenó un timeout (sin datos personales, hasta `DUMP_MAX`
    caracteres) y la oferta y el paso en curso. Devuelve `{"path", "step"}` (path None si no se pudo escribir); nunca lanza."""
    try:
        step = browser._screen(page)
    except Exception:
        step = "desconocido"
    try:
        text = page.inner_text("body") or ""
    except Exception:
        text = ""
    text = "\n".join(line for line in scrub(text, values).splitlines() if line.strip())[:DUMP_MAX]
    body = "\n".join([f"Freno por timeout ({item['id16']})", "", f"Oferta: {scrub(str(item.get('title') or ''), values)} · {scrub(str(item.get('company') or ''), values)}",
                      f"Paso: {step}", "", scrub(str(error), values).splitlines()[0] if str(error) else "", "", "## Texto visible", "", text or "(sin texto)", ""])
    try:
        runs_dir.mkdir(parents=True, exist_ok=True)
        path = runs_dir / f"{run_name}-freno.txt"
        path.write_text(body, encoding="utf-8")
        return {"path": path, "step": step}
    except OSError:
        return {"path": None, "step": step}


def reconcile_unconfirmed(conn, applications: list[dict]) -> list[dict]:
    """Las ofertas `unconfirmed` que figuran en esas postulaciones (por `im`, o por título y empresa; sin empresa, por título y lugar) pasan a
    enviadas. Devuelve `[{id16, title, company}]` de las que se confirmaron."""
    ims = {a["im"] for a in applications if a["im"]}
    names = {(score._fold(a["title"]), score._fold(a["company"])) for a in applications if score._fold(a["company"])}
    index = application_index(applications)
    done = []
    for id16, title, company, location, im in conn.execute(
            "SELECT o.id16, o.title, o.company, o.location, a.im FROM offers o LEFT JOIN applications a ON a.id16 = o.id16 "
            "WHERE o.status = ?", (UNCONFIRMED,)).fetchall():
        found = (im if im and im in ims else None) or index.get(application_key(title, company, location))
        if found is None and not ((score._fold(title), score._fold(company)) in names):
            continue
        db.mark_status(conn, id16, "applied")
        conn.execute("UPDATE applications SET status = 'applied', reason = ?, im = COALESCE(NULLIF(im, ''), ?) WHERE id16 = ?",
                     ("confirmada después en Mis postulaciones", found or "", id16))
        done.append({"id16": id16, "title": title, "company": company})
    conn.commit()
    return done


UNCONFIRMED = "unconfirmed"  # estado de una oferta cuyo envío el sitio no confirmó: no cuenta como enviada ni se vuelve a postular sola


def _mark_unconfirmed(conn, items: list[dict]) -> None:
    for item in items:
        db.mark_status(conn, item["id16"], UNCONFIRMED)
        conn.execute("UPDATE applications SET status = ?, reason = ? WHERE id16 = ?",
                     (UNCONFIRMED, "no figura en Mis postulaciones después del envío", item["id16"]))
    conn.commit()


def _verify_sent(page, sent: list[dict], conn=None) -> dict:
    """Relee «Mis postulaciones» (solo lectura) y devuelve `{"read", "unconfirmed", "error"}`: las enviadas que no aparecen, por `im` o
    por título y empresa. Con `conn`, las que no aparecen pasan a «sin confirmar» en la base (ni enviadas ni para volver a postular). Si no
    se puede leer, `error` dice por qué y no se cambia nada. Lee las páginas que hagan falta (hasta `VERIFY_PAGES`) para encontrar todas."""
    def missing_in(found):
        ims = {a["im"] for a in found if a["im"]}
        names = {(score._fold(a["title"]), score._fold(a["company"])) for a in found}
        return [i for i in sent if not (i.get("im") and i["im"] in ims) and (score._fold(i["title"]), score._fold(i["company"])) not in names]

    try:
        found = with_retries(page, lambda p: browser.read_applications(p, pages=VERIFY_PAGES, want=lambda found: not missing_in(found)))
    except Exception as e:
        return {"read": None, "unconfirmed": [], "error": _halt_text(e)}
    reconciled = reconcile_unconfirmed(conn, found) if conn is not None else []
    missing = missing_in(found)
    if conn is not None:
        _mark_unconfirmed(conn, missing)
    return {"read": len(found), "unconfirmed": missing, "reconciled": reconciled, "error": ""}


def final_check(result: dict | None, page, conn=None) -> None:
    """La verificación final de una tanda (que corre por tramos): relee «Mis postulaciones» una vez para todas las enviadas, salvo que el
    sitio haya bloqueado. Deja el resultado en `result["verify"]`."""
    if result and result["sent"] and not any(t in (result["halted"] or "") for t in ("CAPTCHA", "respondió", "Sin sesión")):
        result["verify"] = _verify_sent(page, result["sent"], conn)


def live_summary(result: dict) -> str:
    """Línea de terminal de una corrida en vivo: enviadas, saltadas y, si hay, preguntas sin respuesta."""
    waiting = sum(1 for i in result["skipped"] if i["reason"] == PENDING_QUESTION)
    return (f"Enviadas: {len(result['sent'])} · saltadas: {len(result['skipped'])}"
            + (f" (con pregunta sin respuesta: {waiting})" if waiting else "") + f" · saltadas por el plan: {len(result['plan']['skip'])}")


def account_lines(result: dict) -> list[str]:
    """Líneas del informe sobre la cuenta: PDFs, CV por defecto, CVs `c4` y alertas."""
    lines = []
    pdfs = result["pdfs"]
    if result.get("requeued"):
        lines.append(f"- Ofertas con pregunta que ya tiene respuesta o que quedaron pendientes con instrucciones viejas, de vuelta a la cola: {result['requeued']}")
    if pdfs["before"] is not None:
        lines.append(f"- Filas de applications completadas con el archivo ya subido (cv_file): {result.get('completed', 0)}")
        lines.append(f"- PDFs en la cuenta al empezar: {pdfs['before']} (máximo {pdfs['max']}); CVs subidos en esta corrida: {pdfs['uploaded']}. "
                     "Cada PDF subido también queda copiado en cv/postulaciones/ con el mismo nombre (se borra solo cuando pasan 30 días).")
        deleted = result.get("deleted") or []
        lines.append(f"- CVs borrados en esta corrida: {len(deleted)}"
                     + ("" if deleted else " (solo se borra uno cuando la cuenta llega al máximo y es seguro)"))
        lines += [f"  - CV borrado de la cuenta: {cell(d['name'])}, copia local {cell(d.get('local') or 'sin copia local registrada')}: {cell(d['reason'])}"
                  for d in deleted]
    on_base = [i for i in result["sent"] if i.get("base")]
    if on_base:
        lines.append(f"- Enviadas con el CV base (cuenta llena): {len(on_base)} (la cuenta tenía {pdfs['max']} CVs y ninguno se podía borrar sin "
                     "riesgo: se usó el PDF base de la variante, que ya estaba en la cuenta, sin subir ni borrar nada).")
        lines += [f"  - {cell(i['title'])} ({cell(i['company'])}): «{cell(i['file'])}»" for i in on_base]
    if pdfs.get("reused"):
        lines.append(f"- CV reusado: {pdfs['reused']} (el PDF de un intento anterior de esa misma oferta ya estaba en la cuenta: se marcó como "
                     "por defecto, sin subir otra copia; no suma en «CVs subidos»).")
    if result["read"] is not None:
        lines.append(f"- Mis postulaciones (solo lectura): {result['read']} leídas; las ofertas que figuran ahí se saltearon sin armar ni subir nada.")
    waiting = sum(1 for i in result["skipped"] if i["reason"] == PENDING_QUESTION)
    if waiting:
        lines.append(f"- Ofertas con pregunta sin respuesta: {waiting}; su CV ya quedó subido en la cuenta (cuenta en «CVs subidos») y no se postuló.")
    start = result["start_default"]
    if start:
        if not start["ok"]:
            lines.append(f"- CV por defecto al empezar: NO se pudo restaurar «{start['name']}» ({cell(start['message'])}); no se postuló a nada.")
        elif start["restored"]:
            lines.append(f"- CV por defecto al empezar: estaba «{start['was'] or 'ninguno'}»; se restauró «{start['name']}» antes de la primera oferta.")
        else:
            lines.append(f"- CV por defecto al empezar: ya era «{start['name']}» (declarado en `default_pdf_name`).")
    elif pdfs["before"] is not None:
        lines.append("- CV por defecto al empezar: no se comprobó, porque ninguna variante declara default_pdf_name.")
    restore = result["restore"]
    if restore:
        if restore["name"] and restore["ok"]:
            lines.append(f"- CV por defecto al terminar: restaurado «{restore['name']}» (declarado en `default_pdf_name`).")
        elif restore["name"]:
            lines.append(f"- CV por defecto al terminar: NO se pudo restaurar «{restore['name']}» ({cell(restore['message'])}).")
        else:
            lines.append(f"- CV por defecto al terminar: {restore['message']}.")
    pending = result.get("pending") or []
    if pending:
        lines.append(f"- Preguntas pendientes ({len(pending)}), con el motivo:")
        lines += [f"  - {cell(p['title'])} ({cell(p['company'])}): «{cell(p['question'])}» — {cell(p['reason'])}" for p in pending]
    ids_only = [i for i in result["sent"] if i["version"] == cv.IDS_ONLY_VERSION]
    if ids_only:
        lines.append(f"- CVs armados solo con ids ({cv.IDS_ONLY_VERSION}, sin texto adaptado): {len(ids_only)} de {len(result['sent'])} enviadas.")
    repaired = sum(1 for i in result["sent"] if i.get("repaired"))
    if repaired:
        lines.append(f"- Adjunto reparado desde la pantalla final (no era el subido): {repaired} de {len(result['sent'])} postulaciones.")
    verify = result.get("verify")
    if verify:
        if verify["error"]:
            lines.append(f"- Verificación final en Mis postulaciones: no se pudo leer ({cell(verify['error'])}).")
        else:
            lines.append(f"- Verificación final en Mis postulaciones: {verify['read']} leídas; enviadas sin confirmar: {len(verify['unconfirmed'])}.")
            lines += [f"  - {cell(i['title'])} ({cell(i['company'])}): sin confirmar en Mis postulaciones (queda «sin confirmar» en la base y no se "
                      f"vuelve a postular sola); oferta: {cell(i['url'])} · pantalla final: {cell(i.get('final') or 'sin dato')}"
                      for i in verify["unconfirmed"]]
    for i in result.get("unconfirmed_now") or []:
        lines.append(f"- Sin confirmar al enviar: {cell(i['title'])} ({cell(i['company'])}): {cell(i['why'])} Oferta: {cell(i['url'])}; "
                     "queda «sin confirmar» en la base y no se vuelve a postular sola; si aparece en Mis postulaciones pasa a enviada. "
                     + (f"Pantalla guardada en {cell(i['saved'])}." if i.get("saved") else "No se pudo guardar la pantalla."))
    confirmed = (result.get("reconciled") or []) + ((verify or {}).get("reconciled") or [])
    lines += [f"- Confirmada después en Mis postulaciones: {cell(i['title'])} ({cell(i['company'])}): pasa de «sin confirmar» a enviada."
              for i in {x["id16"]: x for x in confirmed}.values()]
    if result["sent"]:
        alerts = sum(1 for i in result["sent"] if i["alert"])
        lines.append(f"- Alertas: el sitio pidió `setalert.ashx` en {alerts} de {len(result['sent'])} postulaciones "
                     "(«Avísame si hay nuevas ofertas»); el programa no toca la configuración de alertas.")
        lines += _alert_lines(result["sent"])
    return lines


def _alert_lines(sent: list[dict]) -> list[str]:
    """Resumen de los pedidos a `setalert.ashx` (método, status, nombres de parámetros, antes o después de la pantalla de éxito) y de los
    segundos que pasó el programa en `postapply`. Nunca valores."""
    notes = [a for i in sent for a in i.get("alerts") or []]
    lines = []
    if notes:
        count = lambda key: ", ".join(f"{k} ×{n}" for k, n in sorted(Counter(str(a.get(key)) for a in notes).items()))  # noqa: E731
        names = lambda key: ", ".join(sorted({p for a in notes for p in a.get(key) or []})) or "ninguno"  # noqa: E731
        lines.append(f"  - `setalert.ashx`: método {count('method')}; status {count('status')}; parámetros de la URL: {names('url_params')}; "
                     f"del cuerpo: {names('body_params')}; llegó {count('when')} de confirmarse la pantalla de éxito; {ALERT_NOTE}.")
    times = [i["postapply_s"] for i in sent if i.get("postapply_s") is not None]
    if times:
        lines.append(f"- Tiempo en `postapply`: promedio {sum(times) / len(times):.1f} s, máximo {max(times):.1f} s ({len(times)} envíos).")
    return lines


def write_live_report(result: dict, meta: dict, out_dir: Path = Path("data/runs")) -> Path:
    """Escribe el informe de una corrida en vivo en `out_dir/AAAAMMDD-HHMMSS-apply.md` y devuelve su ruta.

    `meta`: now (datetime), threshold, daily_cap, applied_today (antes de la corrida) y model. Lista las enviadas (con el
    archivo subido y la versión del CV) y las saltadas con su motivo, la cuenta y el freno si lo hubo; no incluye las
    respuestas ni datos del CV.
    """
    now, plan_ = meta["now"], result["plan"]
    state = "completa" if not result["halted"] else "frenó (ver «Freno»)"
    lines = [
        "# Informe de apply (en vivo)", "",
        f"- Fecha: {now:%Y-%m-%d %H:%M:%S}",
        "- Modo: en vivo (se postuló de verdad a las ofertas de «Enviadas»)",
        f"- Umbral: {meta['threshold']}",
        f"- Tope diario: {meta['daily_cap']} (ya postuladas hoy: {meta['applied_today']})",
        *company_cap_lines(meta),
        f"- Modelo: {meta['model']}",
        f"- Costo (respuestas y CVs armados, también los que fallaron): {score.money(result['cost_usd'])}",
        f"- Estado: {state}",
        *archive_lines(plan_), *account_lines(result), "",
        *[line for note in plan_notes(plan_) for line in (note, "")],
        f"## Enviadas ({len(result['sent'])})", "",
        *_live_table(result["sent"], True), "",
        f"## Saltadas en esta corrida ({len(result['skipped'])})", "",
        *_live_table(result["skipped"], False), "",
        f"## Saltadas por el plan ({len(plan_['skip'])})", "",
        *_table(plan_["skip"]),
        *answer_section(result),
    ]
    if result["halted"]:
        lines += ["", "## Freno", "", f"- Mensaje: {cell(result['halted'])}"]
    lines.append("")
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{now:%Y%m%d-%H%M%S}-apply.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def answer_section(result: dict) -> list[str]:
    """La sección «Respuestas» del informe: por cada oferta con preguntas (enviada o pendiente), cada pregunta con su respuesta (la
    opción o el texto), la fuente y, si quedó pendiente, el motivo. Lo que sale tal cual de `answers` (sueldo, dónde vive, movilidad o una
    respuesta declarada a esa pregunta) se muestra como «(respuesta fija: <clave>)», sin el valor."""
    records = result.get("answers") or []
    if not records:
        return []
    sent = {i["id16"] for i in result["sent"]}
    lines = ["", f"## Respuestas ({len(records)} ofertas con preguntas)", ""]
    for record in records:
        entries = record["entries"]
        state = "enviada" if record["id16"] in sent else ("pendiente" if any(e["source"] == "unknown" for e in entries) else "no enviada")
        lines += [f"### {cell(record['title'])} ({cell(record['company'])}) — {state}", ""]
        for e in entries:
            if e["source"] == "unknown":
                shown = f"(sin respuesta) — pendiente: {cell(e.get('reason') or 'sin motivo')}"
            elif e.get("fixed"):
                shown = f"(respuesta fija: {e['fixed']})"
            else:
                shown = cell(answer_text(e))
            choices = f" · opciones (texto de la oferta): {cell(' / '.join(e['options']))}" if e.get("kind") == "closed" and e.get("options") else ""
            lines.append(f"- «{cell(e['question'])}» → {shown}" + ("" if e["source"] == "unknown" else f" · fuente: {e['source']}") + choices)
        lines.append("")
    return lines


def _live_table(items: list[dict], sent: bool) -> list[str]:
    columns = ("oferta", "empresa", "puntaje", "CV", "url") + (("CV subido", "versión del CV", "preguntas contestadas", "pantalla final")
                                                              if sent else ("motivo",))
    lines = ["| " + " | ".join(columns) + " |", "|" + "---|" * len(columns)]
    for item in items:
        last = (f"subido como {item['file']}" + ("" if item.get("base") else f" · copia local {item.get('local') or item['file']}")
                + (" (CV base, cuenta llena)" if item.get("base") else " (CV reusado)" if item.get("reused") else ""),
                item["version"], item["questions"], item.get("final") or "") if sent else (item["reason"],)
        lines.append("| " + " | ".join(cell(c) for c in (item["title"], item["company"], item["score"], item["variant"], item["url"], *last)) + " |")
    if not items:
        lines += ["", "_Ninguna._"]
    return lines
