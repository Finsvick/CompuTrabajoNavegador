"""CV a medida de una oferta, solo para revisar: el modelo adapta el texto, el código verifica que no agregue ningún hecho.

Cero invención: todo dato sale del `resume` de la variante (AIHawk), de su banco de verdades (`truths`) o de
`personal_information`. El modelo elige y ordena items por id y escribe un título, un perfil y viñetas; el código rechaza
cualquier tecnología, número, sigla o nivel de seniority que no figure en el CV base ni en el banco. Si el texto no
verifica tras un reintento, se arma el CV solo con ids (`c4`). No se sube nada a Computrabajo.
"""
import html
import json
import os
import re
import sqlite3
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

from compuapply import db, score
from compuapply.profile import MAX_PDF_BYTES, technical_skills

CV_PROMPT_VERSION = "c8"
IDS_ONLY_VERSION = "c4"  # el CV de solo elección de ids, si el texto adaptado no verifica
BLOCKS = ("relevante", "otra")
CATEGORIES = {"technical_skills": ("T", "Conocimientos técnicos"), "projects": ("P", "Proyectos"), "achievements": ("A", "Logros"),
              "certifications": ("C", "Certificaciones"), "interests": ("I", "Intereses")}
MAX_HEADLINE, MAX_SUMMARY, MAX_BULLET, MAX_BULLETS = 60, 350, 200, 5
MAX_CV_TOKENS = 4000
# Palabras de nivel: solo pueden aparecer si están en algún puesto o estudio del CV base.
LEVEL_WORDS = {"semi", "semisenior", "ssr", "senior", "sr", "líder", "lead", "jefe", "gerente", "head", "coordinador"}
# Palabras de nivel que describen hacia abajo: no exageran, así que valen siempre.
LOW_LEVELS = {"junior", "trainee", "pasante", "practicante", "estudiante"}
# Palabras de puesto: en el título del CV valen si están en el título de la oferta, aunque el CV base no las tenga.
ROLE_WORDS = frozenset((
    "desarrollador developer programador programmer engineer ingeniero analista analyst frontend front end backend back "
    "fullstack full stack web software aplicaciones applications sistemas integración automatización soporte support técnico "
    "asistente assistant vendedor asesor ejecutivo comercial ventas atención cliente clientes público operador administrativo "
    "cajero recepcionista representante telemarketer customer service").split())
COPY_REASON = "las viñetas son copia de su fuente: reescribí las viñetas con el vocabulario de la oferta."
SYSTEM = (
    "Armás un CV a medida para una oferta de trabajo. Recibís la oferta, los items del CV base del candidato (cada uno con "
    "un id) y, si hay, un banco de verdades: hechos ciertos que el candidato escribió, con ids X. Tenés que reescribir cada "
    "viñeta con el vocabulario y las tareas de la oferta, conservando el hecho: no copies la línea del CV base tal cual. "
    "Estilo: cada viñeta y el perfil se escriben en estilo nominal, con sustantivos de acción («Gestión de…», «Atención de…», "
    "«Control y reposición de…»), sin verbos conjugados en primera persona. Si hace falta un verbo, todos los verbos de la "
    "viñeta van en el mismo tiempo: nunca mezcles, por ejemplo, «controlé» con «reponía». "
    "Ejemplo genérico (datos falsos): si la oferta pide «atención al cliente y soporte» y la línea original dice «Respondía "
    "consultas de los clientes por correo y teléfono», la versión adaptada es «Atención al cliente por correo y teléfono: "
    "respuesta a consultas». Priorizá lo que la oferta pide y presentá como transferible lo que sirve de otros rubros. Pero "
    "no podés agregar ningún hecho: cada viñeta y el perfil tienen que apoyarse en items de la lista (poné su id en "
    "`source`/`sources`); los ids van solo ahí: nunca los escribas dentro de `headline`, `summary` ni de las viñetas. No inventes tecnologías, herramientas, años, cantidades, porcentajes, certificados, títulos ni "
    "niveles de seniority que no estén en los items. `headline` (hasta 60 caracteres): el nombre del puesto de la oferta si "
    "es de la misma familia que alguna experiencia o estudio del candidato; si no, un título que describa su perfil real. "
    "`summary` (hasta 350 caracteres): el perfil, con los ids que usa en `sources`. Todas las experiencias del CV base van "
    "en el CV. Para cada experiencia que nombres elegí el bloque: «relevante» si sirve para el puesto de la oferta (entonces "
    "escribí de 1 a 5 viñetas de hasta 200 caracteres, cada una con su `source`: un id E*.R* de esa experiencia o un id X; y "
    "elegí habilidades de esa experiencia, si tiene) u «otra» si es de otro rubro o no suma (entonces sin habilidades, y a "
    "lo sumo una viñeta con una habilidad transferible al puesto de la oferta, con el `source` E*.R* de esa experiencia; si "
    "no hay ninguna transferible, sin viñetas). Las que no nombres se muestran como «otra». Elegí también conocimientos "
    "técnicos (ids T o X; primero los que pide la oferta), proyectos, logros, certificaciones e intereses; pueden quedar "
    "vacíos. Si te paso «Requisitos que el CV no muestra», mostrá lo más cercano que sea cierto, sin afirmar el requisito."
)
# Niveles de idioma en inglés (AIHawk) -> español; cualquier otro valor sale tal cual.
LEVELS = {"native": "Nativo", "fluent": "Fluido", "advanced": "Avanzado", "upper intermediate": "Intermedio alto",
          "intermediate": "Intermedio", "basic": "Básico", "beginner": "Principiante"}
FAIL = "No se armó el CV: "
NOT_SAVED = " No se guardó nada."
# Datos personales en el orden en que salen; todo sale tal cual está en el CV base.
CONTACT = ("date_of_birth", "address", "city", "country", "phone_prefix", "phone", "email", "github", "linkedin")
CSS = """
@page { size: A4; margin: 14mm; }
body { font: 10.5pt/1.4 Arial, Helvetica, sans-serif; color: #222; margin: 0; }
h1 { font-size: 20pt; margin: 0 0 4px; }
h1 span + span::before { content: " "; }
h2 { font-size: 11pt; text-transform: uppercase; letter-spacing: .06em; border-bottom: 1px solid #999; margin: 14px 0 6px; }
h3 { font-size: 11pt; margin: 8px 0 0; }
h4 { display: inline; font-size: 10pt; margin: 0 4px 0 0; }
.headline { font-size: 12pt; margin: 0 0 4px; }
.meta { color: #555; font-size: 9.5pt; }
.meta span + span::before, .contact span + span::before { content: " · "; }
ul { margin: 3px 0 3px 18px; padding: 0; }
.skills span + span::before { content: ", "; }
.name { font-weight: bold; }
.name + span::before { content: ": "; }
"""
REVISION_CSS = """
body { font: 10pt/1.4 Arial, Helvetica, sans-serif; color: #222; margin: 20px; max-width: 1000px; }
table { border-collapse: collapse; width: 100%; margin: 6px 0 14px; }
th, td { border: 1px solid #bbb; padding: 4px 8px; text-align: left; vertical-align: top; }
th { background: #eee; }
.warn { background: #fff4d6; padding: 6px 10px; border: 1px solid #e0b84a; }
"""


class _Bad(ValueError):
    """La respuesta del modelo no sirve; el mensaje dice por qué (sin el prefijo de `FAIL`)."""


FORMAT = "la respuesta del modelo no tiene el formato esperado."


def _text(value) -> str:
    return str(value).strip() if isinstance(value, (str, int, float)) and not isinstance(value, bool) else ""


def _entries(resume: dict, key: str) -> list[dict]:
    return [e for e in resume.get(key) or [] if isinstance(e, dict)]


def _recent_first(entries: list, date_of) -> list:
    """Ordena de la más nueva a la más vieja según `date_of(item)` -> (año, mes) o None; sin fecha legible, al final en su orden."""
    return sorted(entries, key=lambda item: (1, 0, 0) if (when := date_of(item)) is None else (0, -when[0], -when[1]))


def _start(period: str) -> tuple[int, int] | None:
    """(año, mes) de inicio de un `employment_period` («MM/AAAA - …» o «AAAA - …»); el mes es 0 si no está."""
    found = re.match(r"\s*(?:(\d{1,2})/)?(\d{4})\b", period)
    return (int(found.group(2)), int(found.group(1) or 0)) if found else None


def _end_year(value) -> int | None:
    found = re.match(r"\s*(\d{4})\b", _text(value))
    return int(found.group(1)) if found else None


def _parse(resume: dict, truths=()) -> dict:
    """El CV base con un id por item: `experiences` (con sus responsabilidades y habilidades) y las demás secciones.

    Los items del banco de verdades (`X1`, `X2`…) cuentan como hechos del CV base: van en `truths` y también entre los
    `technical_skills`, que así pueden elegirlos.
    """
    experiences = []
    for n, e in enumerate(_entries(resume, "experience_details"), 1):
        responsibilities = [_text(v) for r in e.get("key_responsibilities") or [] for v in (r.values() if isinstance(r, dict) else [r])]
        experiences.append({
            "id": f"E{n}", "position": _text(e.get("position")), "period": _text(e.get("employment_period")),
            "meta": [t for t in (_text(e.get(k)) for k in ("company", "employment_period", "location", "industry")) if t],
            "responsibilities": {f"E{n}.R{i}": t for i, t in enumerate((t for t in responsibilities if t), 1)},
            "skills": {f"E{n}.S{i}": t for i, t in enumerate((t for t in map(_text, e.get("skills_acquired") or []) if t), 1)},
        })
    bank = {f"X{n}": (t,) for n, t in enumerate((t for t in map(_text, truths) if t), 1)}
    parsed = {"experiences": experiences, "truths": bank,
              "technical_skills": {**{f"T{n}": (t,) for n, t in enumerate(technical_skills(resume), 1)}, **bank}}
    for key, prefix in (("projects", "P"), ("achievements", "A"), ("certifications", "C")):
        parsed[key] = {f"{prefix}{n}": (_text(e.get("name")), _text(e.get("description")))
                       for n, e in enumerate(_entries(resume, key), 1)}
    parsed["interests"] = {f"I{n}": (t,) for n, t in enumerate((t for t in map(_text, resume.get("interests") or []) if t), 1)}
    return parsed


def _line(parts: tuple) -> str:
    return ": ".join(part for part in parts if part)


def items(resume: dict, truths=()) -> dict[str, str]:
    """Los items que puede usar el LLM: id -> texto del CV base.

    `E1` (experiencia: el puesto), `E1.R2` (responsabilidad), `E1.S1` (habilidad), `P1` (proyecto), `A1` (logro),
    `C1` (certificación), `I1` (interés), `T1` (conocimiento técnico) y `X1` (banco de verdades). Proyectos, logros y
    certificaciones son «nombre: descripción».
    """
    parsed, found = _parse(resume, truths), {}
    for e in parsed["experiences"]:
        found[e["id"]] = e["position"]
        found.update(e["responsibilities"])
        found.update(e["skills"])
    for key in CATEGORIES:
        found.update({id_: _line(parts) for id_, parts in parsed[key].items()})
    return found


# ── verificación del texto: nada de hechos nuevos ────────────────────────────

_EDGE = ",;:()[]{}\"'¿?¡!«»…“”"
_END_OF_SENTENCE = (".", "!", "?", "…")


def _words(text: str) -> list[tuple[str, bool]]:
    """(token, empieza_una_frase) de cada palabra del texto, sin los signos de los bordes; los guiones sueltos se descartan."""
    out, start = [], True
    for raw in text.split():
        token = raw.lstrip(_EDGE).rstrip(_EDGE + ".")
        if any(ch.isalnum() for ch in token):
            out.append((token, start))
        start = raw.rstrip("\"')]»”").endswith(_END_OF_SENTENCE)
    return out


def _is_protected(token: str, at_start: bool, plain=()) -> bool:
    """Con un dígito, con . # + / adentro, o con una mayúscula fuera del inicio de la frase.

    Una mayúscula inicial sola no protege si su forma en minúsculas está en `plain` (aparece en minúsculas en la oferta o
    en el CV base): «Automatización» en un título. `JavaScript`, `AWS` y compañía siguen protegidos.
    """
    if any(ch.isdigit() for ch in token) or any(ch in token for ch in ".#+/"):
        return True
    if not any(ch.isupper() for ch in (token[1:] if at_start else token)):
        return False
    return at_start or any(ch.isupper() for ch in token[1:]) or token.lower() not in plain


def _fold(text: str) -> str:
    """En minúsculas y sin acentos."""
    return "".join(ch for ch in unicodedata.normalize("NFD", text.lower()) if not unicodedata.combining(ch))


_ROLE = frozenset(map(_fold, ROLE_WORDS))


def protected_tokens(text: str, plain=(), title: bool = False) -> list[str]:
    """Los tokens del texto que el CV base tiene que contener para que el texto valga: `Node.js`, `C#`, `AWS`, `2`, `30%`…

    En un título (`title=True`) la primera palabra también se controla (la mayúscula es de título, no de inicio de frase),
    salvo que sea una palabra de puesto de `ROLE_WORDS`.
    """
    found = []
    for n, (token, at_start) in enumerate(_words(text)):
        if title and n == 0:
            at_start = _fold(token) in _ROLE
        if _is_protected(token, at_start, plain):
            found.append(token)
    return found


def title_roles(title: str) -> frozenset[str]:
    """Las palabras de puesto (de `ROLE_WORDS`, sin mayúsculas ni acentos) que están en el título de una oferta."""
    parts = (part for token, _ in _words(title) for part in re.split(r"[/\-]", _fold(token)))
    return frozenset(part for part in parts if part in _ROLE)


def _leaves(value):
    """Todos los textos (claves y valores, números como texto) de una estructura del CV."""
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _leaves(key)
            yield from _leaves(item)
    elif isinstance(value, list):
        for item in value:
            yield from _leaves(item)
    elif isinstance(value, (str, int, float)) and not isinstance(value, bool):
        yield str(value)


def _vocabulary(texts) -> set[str]:
    """Tokens en minúsculas de los textos, enteros y también partidos por «/» y «-» (así `2018` está en `01/2018`)."""
    vocab = set()
    for text in texts:
        for token, _ in _words(text):
            low = token.lower()
            vocab.add(low)
            vocab.update(part for part in re.split(r"[/\-]", low) if part)
    return vocab


@dataclass(frozen=True)
class _Vocab:
    """Lo que vale en un texto generado: `words` (tokens del CV base y del banco, en minúsculas), `acronyms` (iniciales de
    2 a 5 palabras seguidas de esos textos) y `plain` (palabras que aparecen en minúsculas en ellos o en la oferta)."""
    words: set[str]
    acronyms: set[str] = field(default_factory=set)
    plain: set[str] = field(default_factory=set)


_STOP = {"de", "del", "la", "las", "los", "el", "y", "e", "en", "para"}  # no cuentan en las iniciales de una sigla
_ENDINGS = {"a", "o", "as", "os", "es"}  # terminaciones de género: «Desarrollador/a», «y/o»


_CLAUSE_END = re.compile(r"[,;:.]+(?:\s+|$)|\n+")  # una sigla no cruza una coma, punto y coma, dos puntos, punto ni salto de línea


def _acronyms(texts) -> set[str]:
    """Las iniciales (en minúsculas) de 2 a 5 palabras seguidas de un mismo item, sin contar `_STOP`: «Inteligencia Artificial» -> `ia`."""
    found = set()
    for text in texts:
        for clause in _CLAUSE_END.split(text):
            core = [token.lower() for token, _ in _words(clause) if token.lower() not in _STOP]
            for size in range(2, 6):
                found.update("".join(word[0] for word in core[i:i + size]) for i in range(len(core) - size + 1))
    return found


def _base_vocabulary(resume: dict, truths, offer: str = "") -> _Vocab:
    """El vocabulario del CV base completo (todas las secciones menos los datos personales), del banco de verdades y de la oferta."""
    rest = {key: value for key, value in resume.items() if key != "personal_information"}
    texts = [*_leaves(rest), *map(_text, truths)]
    plain = {token for text in (offer, *texts) for token, _ in _words(text) if token == token.lower() and token[:1].isalpha()}
    return _Vocab(_vocabulary(texts), _acronyms(texts), plain)


def _allowed_levels(resume: dict) -> set[str]:
    """Las palabras de nivel que figuran en algún puesto o estudio del CV base."""
    texts = [*_leaves([e.get("position") for e in _entries(resume, "experience_details")]),
             *_leaves(_entries(resume, "education_details"))]
    return _vocabulary(texts) & LEVEL_WORDS


def _covered(token: str, vocab: _Vocab) -> bool:
    """Si un token protegido vale: está en el vocabulario, es un nivel hacia abajo, su plural (sin `s`/`es`) o su sigla están,
    o, con «/» o «-», cada parte vale (sin contar las terminaciones de género ni conectores como «y»)."""
    low = token.lower()
    if low in vocab.words or low in LOW_LEVELS:
        return True
    if (low.endswith("s") and low[:-1] in vocab.words) or (low.endswith("es") and low[:-2] in vocab.words):
        return True
    stem = token[:-1] if token.endswith("s") and len(token) > 2 else token
    if stem.isupper() and stem.lower() in vocab.acronyms:
        return True
    parts = [part for part in re.split(r"[/\-]", low) if part]
    return len(parts) > 1 and all(part in _STOP | _ENDINGS or _covered(part, vocab) for part in parts)


def _is_role(token: str, roles: frozenset[str]) -> bool:
    """Si el token son palabras de puesto del título de la oferta (partido por «/» y «-»)."""
    parts = [part for part in re.split(r"[/\-]", _fold(token)) if part]
    return bool(parts) and all(part in roles for part in parts)


# Sustantivos comunes de un CV que terminan en «ía» (no son un imperfecto) y las familias de sustantivos que terminan igual.
IA_NOUNS = frozenset((
    "día categoría tecnología garantía auditoría ingeniería mensajería cafetería librería panadería energía economía compañía policía "
    "galería secretaría carnicería ferretería papelería pastelería zapatería joyería relojería cerrajería mercadería lavandería "
    "farmacopea alegría cortesía batería sociología psicología biología metodología filosofía geografía fotografía bibliografía "
    "mayoría minoría junta tía guía vía mía fría vacía envía").split())
IA_SUFFIXES = ("logía", "grafía", "nomía", "ería")
PRETERITE_NOT_VERBS = frozenset("café bebé comité canapé purré consomé carné chalé buqué masaje".split())


def mixed_tenses(text: str) -> tuple[str, str] | None:
    """(pretérito, imperfecto) si el texto trae un verbo en pretérito perfecto de primera persona (termina en «é»: «controlé») y otro en
    imperfecto (termina en «aba» o «ía»: «reponía»); None si no mezcla. No cuentan los sustantivos en «ía» («categoría», «mensajería»)."""
    words = [token.lower() for token, _ in _words(text)]
    preterite = next((w for w in words if len(w) >= 5 and w.endswith("é") and w not in PRETERITE_NOT_VERBS), None)
    imperfect = next((w for w in words if len(w) >= 5 and (w.endswith("aba") or w.endswith("ía"))
                      and w not in IA_NOUNS and not w.endswith(IA_SUFFIXES)), None)
    return (preterite, imperfect) if preterite and imperfect else None


def _line_problems(label: str, text: str, limit: int, vocab: _Vocab, levels: set[str], roles: frozenset[str] | None = None,
                   tenses: bool = True) -> list[str]:
    """Por qué una línea generada no sirve (lista vacía si sirve): largo, tokens protegidos, palabras de nivel y tiempos verbales mezclados.

    `roles` solo se pasa para el título: las palabras de puesto del título de la oferta valen aunque el CV no las tenga.
    `tenses` apaga el control de tiempos (las respuestas abiertas de las preguntas no lo usan).
    """
    problems = []
    if len(text) > limit:
        problems.append(f"{label}: tiene {len(text)} caracteres y el máximo es {limit}.")
    missing = [token for token in dict.fromkeys(protected_tokens(text, vocab.plain, title=roles is not None))
               if not _covered(token, vocab) and not (roles and _is_role(token, roles))]
    if missing:
        problems.append(f"{label}: «{'», «'.join(missing)}» no figura en el CV base ni en el banco de verdades.")
    words = {part for token, _ in _words(text) for part in [token.lower(), *re.split(r"[/\-]", token.lower())]}
    if extra := sorted((words & LEVEL_WORDS) - levels):
        problems.append(f"{label}: la palabra de nivel «{extra[0]}» no figura en ningún puesto ni estudio del CV base.")
    if tenses and (mixed := mixed_tenses(text)):
        problems.append(f"{label}: mezcla tiempos verbales («{mixed[0]}» y «{mixed[1]}»): escribila en estilo nominal o con un solo tiempo.")
    return problems


_ID = r"(?:E\d+(?:\.[RS]\d+)?|[TPACIX]\d+)"
_LOOSE_ID = re.compile(rf"(?<![\w.]){_ID}(?!\w)")
_EMPTY_BRACKETS = re.compile(r"[\(\[][\s,;]*[\)\]]")


def _strip_ids(text: str, vocab: _Vocab) -> str:
    """Saca de un texto generado los ids del CV base (sueltos o entre paréntesis) y los espacios que sobran; sacar no inventa nada.

    Un «id» que el CV base trae como palabra (por ejemplo un nivel «C1») se deja.
    """
    text = _LOOSE_ID.sub(lambda m: m.group(0) if m.group(0).lower() in vocab.words else "", text)
    text = _EMPTY_BRACKETS.sub("", text)
    text = re.sub(r"\s+([,.;:!?])", r"\1", " ".join(text.split()))
    return text.strip().lstrip(",;:").strip()


# ── pedido y esquema ─────────────────────────────────────────────────────────

def _ids_array(ids: list[str]) -> dict:
    """Lista de ids del enum (el JSON Schema no admite un enum vacío: sin ids, la lista tiene que quedar vacía)."""
    if not ids:
        return {"type": "array", "maxItems": 0, "items": {"type": "string"}}
    return {"type": "array", "items": {"type": "string", "enum": ids}}


def _all_ids(parsed: dict) -> list[str]:
    ids = [i for e in parsed["experiences"] for i in (e["id"], *e["responsibilities"], *e["skills"])]
    return ids + [i for key in CATEGORIES for i in parsed[key]]


def _schema(parsed: dict) -> dict:
    """JSON Schema de la respuesta: título, perfil, experiencias con viñetas y las listas de ids de cada sección."""
    exp_ids = [e["id"] for e in parsed["experiences"]]
    sources = [i for e in parsed["experiences"] for i in e["responsibilities"]] + list(parsed["truths"])
    bullet = {"type": "object", "properties": {"source": {"type": "string", "enum": sources}, "text": {"type": "string"}},
              "required": ["source", "text"], "additionalProperties": False}
    entry = {
        "type": "object",
        "properties": {
            "experience": {"type": "string", "enum": exp_ids},
            "block": {"type": "string", "enum": list(BLOCKS)},
            "bullets": {"type": "array", "items": bullet} if sources else _ids_array([]),
            "skills": _ids_array([i for e in parsed["experiences"] for i in e["skills"]]),
        },
        "required": ["experience", "block", "bullets", "skills"],
        "additionalProperties": False,
    }
    summary = {"type": "object", "properties": {"text": {"type": "string"}, "sources": _ids_array(_all_ids(parsed))},
               "required": ["text", "sources"], "additionalProperties": False}
    properties = {"headline": {"type": "string"}, "summary": summary,
                  "experiences": {"type": "array", "items": entry} if exp_ids else _ids_array([])}
    properties.update({key: _ids_array(list(parsed[key])) for key in CATEGORIES})
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def _request_text(parsed: dict, unmet=()) -> str:
    """Los items con su id para el LLM: puestos, empresas y períodos sí; nunca datos personales ni links."""
    lines = ["Items del CV base:", ""]
    for e in parsed["experiences"]:
        lines.append(f"{e['id']}: {e['position']}" + (f" ({' · '.join(e['meta'][:2])})" if e["meta"] else ""))
        lines += [f"  {id_}: {t}" for id_, t in e["responsibilities"].items()]
        lines += [f"  {id_}: {t}" for id_, t in e["skills"].items()]
    for key, (_, title) in CATEGORIES.items():
        shown = {id_: parts for id_, parts in parsed[key].items() if id_ not in parsed["truths"]}
        if shown:
            lines += ["", f"{title}:"] + [f"{id_}: {_line(parts)}" for id_, parts in shown.items()]
    if parsed["truths"]:
        lines += ["", "Banco de verdades (hechos ciertos del candidato; se pueden usar como fuente y como conocimiento técnico):"]
        lines += [f"{id_}: {parts[0]}" for id_, parts in parsed["truths"].items()]
    if unmet:
        lines += ["", "Requisitos que el CV no muestra:"] + [f"- {item}" for item in unmet]
    return "\n".join(lines)


# ── verificación de la respuesta contra el CV base ───────────────────────────

def _pick(ids, valid: set[str], where: str) -> list[str]:
    """Ids elegidos de una lista: tienen que existir en `valid` y no repetirse."""
    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
        raise _Bad(FORMAT)
    seen = set()
    for id_ in ids:
        if id_ not in valid:
            raise _Bad(f"el modelo eligió «{id_}» {where}, y ese id no corresponde ahí.")
        if id_ in seen:
            raise _Bad(f"el modelo repitió «{id_}» {where}.")
        seen.add(id_)
    return ids


def _validate_ids(parsed: dict, data) -> dict:
    """La verificación del CV de solo ids (`c4`): ver `validate`."""
    if not isinstance(data, dict) or not isinstance(data.get("experiences"), list) \
            or any(not isinstance(data.get(key), list) for key in CATEGORIES):
        raise _Bad(FORMAT)
    by_id = {e["id"]: e for e in parsed["experiences"]}
    chosen = {}
    for entry in data["experiences"]:
        if not isinstance(entry, dict):
            raise _Bad(FORMAT)
        if entry.get("experience") not in by_id:
            raise _Bad(f"el modelo eligió una experiencia que no existe en el CV base ({entry.get('experience')!r}).")
        exp = by_id[entry["experience"]]
        if exp["id"] in chosen:
            raise _Bad(f"el modelo repitió la experiencia {exp['id']}.")
        if entry.get("block") not in BLOCKS:
            raise _Bad(FORMAT)
        where = f"en la experiencia {exp['id']}"
        responsibilities = _pick(entry.get("responsibilities"), set(exp["responsibilities"]), where)
        skills = _pick(entry.get("skills"), set(exp["skills"]), where)
        relevant = entry["block"] == "relevante" and bool(responsibilities)
        chosen[exp["id"]] = {"block": "relevante" if relevant else "otra",
                             "responsibilities": responsibilities if relevant else [], "skills": skills if relevant else []}
    for exp_id in by_id:  # lo que el modelo no nombró va a «otra»
        chosen.setdefault(exp_id, {"block": "otra", "responsibilities": [], "skills": []})
    selection = {"experiences": chosen}
    for key in CATEGORIES:
        selection[key] = _pick(data[key], set(parsed[key]), f"en {CATEGORIES[key][1].lower()}")
    return selection


def validate(parsed: dict, data) -> dict:
    """Verifica una respuesta de solo ids contra el CV base y devuelve la selección; si algo falla, RuntimeError.

    Ids existentes, sin repetir y cada responsabilidad y habilidad dentro de su experiencia; si no, error. Todas las
    experiencias del CV base quedan en la selección: la que el modelo no nombró, o dio como «relevante» sin
    responsabilidades, pasa a «otra» (sin responsabilidades ni habilidades), sin error.
    """
    try:
        return _validate_ids(parsed, data)
    except _Bad as e:
        raise RuntimeError(FAIL + str(e) + NOT_SAVED) from None


def _validate_text(parsed: dict, data, vocab: _Vocab, levels: set[str], roles: frozenset[str] = frozenset()) -> tuple[dict, list[tuple]]:
    """Verifica la respuesta con texto adaptado (`c8`): (selección, líneas inválidas como (campo, motivo)).

    El campo es `"headline"`, `"summary"` o `("bullet", id de la experiencia, posición desde 0)`.

    Lanza `_Bad` si la estructura no sirve (id inexistente, repetido, de otra experiencia, más de 5 viñetas, más de 1 en una
    experiencia «otra», formato).
    Las líneas inválidas (largo, tokens protegidos, palabras de nivel) no son un error de estructura: se devuelven aparte.
    Antes del control se sacan los ids del texto (`_strip_ids`); una viñeta que queda vacía es error de formato. `roles`: las
    palabras de puesto del título de la oferta (`title_roles`), que valen en el título.
    """
    if not isinstance(data, dict) or not isinstance(data.get("headline"), str) or not isinstance(data.get("summary"), dict) \
            or not isinstance(data["summary"].get("text"), str) or not isinstance(data.get("experiences"), list) \
            or any(not isinstance(data.get(key), list) for key in CATEGORIES):
        raise _Bad(FORMAT)
    universe = set(_all_ids(parsed))
    by_id = {e["id"]: e for e in parsed["experiences"]}
    problems, chosen = [], {}
    headline, summary_text = _strip_ids(data["headline"], vocab), _strip_ids(data["summary"]["text"], vocab)
    problems += [("headline", m) for m in _line_problems("el título", headline, MAX_HEADLINE, vocab, levels, roles)]
    problems += [("summary", m) for m in _line_problems("el perfil", summary_text, MAX_SUMMARY, vocab, levels)]
    summary_sources = _pick(data["summary"].get("sources"), universe, "como fuente del perfil")
    for entry in data["experiences"]:
        if not isinstance(entry, dict):
            raise _Bad(FORMAT)
        if entry.get("experience") not in by_id:
            raise _Bad(f"el modelo eligió una experiencia que no existe en el CV base ({entry.get('experience')!r}).")
        exp = by_id[entry["experience"]]
        if exp["id"] in chosen:
            raise _Bad(f"el modelo repitió la experiencia {exp['id']}.")
        if entry.get("block") not in BLOCKS or not isinstance(entry.get("bullets"), list):
            raise _Bad(FORMAT)
        bullets, other = entry["bullets"], entry["block"] == "otra"
        if len(bullets) > (1 if other else MAX_BULLETS):
            raise _Bad(f"el modelo escribió {len(bullets)} viñetas en la experiencia {exp['id']} y el máximo es {1 if other else MAX_BULLETS}"
                       + (" (es «otra»)." if other else "."))
        valid_sources, cleaned = (set(exp["responsibilities"]) if other else {*exp["responsibilities"], *parsed["truths"]}), []
        for n, bullet in enumerate(bullets, 1):
            if not isinstance(bullet, dict) or not isinstance(bullet.get("source"), str) or not isinstance(bullet.get("text"), str):
                raise _Bad(FORMAT)
            if bullet["source"] not in valid_sources:
                raise _Bad(f"el modelo usó «{bullet['source']}» como fuente en la experiencia {exp['id']}, y ese id no corresponde ahí.")
            text = _strip_ids(bullet["text"], vocab)
            if not text:
                raise _Bad(f"la viñeta {n} de la experiencia {exp['id']} quedó vacía después de sacar los ids.")
            problems += [(("bullet", exp["id"], n - 1), m) for m in _line_problems(f"viñeta {n} de {exp['id']}", text, MAX_BULLET, vocab, levels)]
            cleaned.append({"source": bullet["source"], "text": text})
        skills = _pick(entry.get("skills"), set(exp["skills"]), f"en la experiencia {exp['id']}")
        relevant = entry["block"] == "relevante" and bool(bullets)
        chosen[exp["id"]] = {
            "block": "relevante" if relevant else "otra", "bullets": cleaned,
            "responsibilities": list(dict.fromkeys(b["source"] for b in bullets if b["source"] in exp["responsibilities"])) if relevant else [],
            "skills": skills if relevant else []}
    for exp_id in by_id:
        chosen.setdefault(exp_id, {"block": "otra", "bullets": [], "responsibilities": [], "skills": []})
    selection = {"headline": headline, "summary": {"text": summary_text, "sources": summary_sources},
                 "experiences": chosen}
    for key in CATEGORIES:
        selection[key] = _pick(data[key], set(parsed[key]), f"en {CATEGORIES[key][1].lower()}")
    return selection, problems


def _norm(text: str) -> str:
    """Un texto sin mayúsculas, con los espacios colapsados y sin puntuación final, para decidir si una viñeta es copia."""
    return " ".join(text.lower().split()).rstrip(" .,;:!?…")


def _bullet_sources(parsed: dict) -> dict[str, str]:
    """id -> texto de lo que una viñeta puede tener como fuente: las responsabilidades del CV base y el banco de verdades."""
    found = {id_: text for e in parsed["experiences"] for id_, text in e["responsibilities"].items()}
    return {**found, **{id_: parts[0] for id_, parts in parsed["truths"].items()}}


def _is_copy(text: str, source_text: str) -> bool:
    return _norm(text) == _norm(source_text)


def _all_copies(parsed: dict, selection: dict) -> bool:
    """Si hay viñetas y todas son copia de su fuente."""
    sources = _bullet_sources(parsed)
    bullets = [b for picked in selection["experiences"].values() for b in picked["bullets"]]
    return bool(bullets) and all(_is_copy(b["text"], sources.get(b["source"], "")) for b in bullets)


def _repair(parsed: dict, selection: dict, problems: list[tuple]) -> dict:
    """Reemplaza solo las líneas inválidas de una selección con texto adaptado; el resto se conserva.

    Título: el puesto de la experiencia relevante más reciente, tal cual está en el CV base (sin título si no hay
    relevantes). Perfil: se saca. Viñeta: el texto de su fuente (una copia); si esa fuente ya está en otra viñeta de la
    misma experiencia, la viñeta se saca.
    """
    out = {**selection, "summary": dict(selection["summary"]),
           "experiences": {k: {**v, "bullets": list(v["bullets"])} for k, v in selection["experiences"].items()}}
    fields = [field for field, _ in problems]
    if "headline" in fields:
        relevant = [e for e in _recent_first(parsed["experiences"], lambda e: _start(e["period"]))
                    if out["experiences"][e["id"]]["block"] == "relevante"]
        out["headline"] = relevant[0]["position"] if relevant else ""
    if "summary" in fields:
        out["summary"] = {"text": "", "sources": []}
    sources, bad = _bullet_sources(parsed), {}
    for field in fields:
        if isinstance(field, tuple):
            bad.setdefault(field[1], set()).add(field[2])
    for exp_id, positions in bad.items():
        bullets = out["experiences"][exp_id]["bullets"]
        used, kept = {b["source"] for n, b in enumerate(bullets) if n not in positions}, []
        for n, b in enumerate(bullets):
            if n not in positions:
                kept.append(b)
            elif b["source"] not in used:
                used.add(b["source"])
                kept.append({"source": b["source"], "text": sources[b["source"]]})
        out["experiences"][exp_id]["bullets"] = kept
    return out


def _salvage(parsed: dict, data) -> dict:
    """Lo que sirve de una respuesta con texto inválido, en el formato de solo ids: ids válidos, sin repetir; nada del texto."""
    def unique(ids, valid):
        return list(dict.fromkeys(i for i in (ids if isinstance(ids, list) else []) if isinstance(i, str) and i in valid))

    by_id = {e["id"]: e for e in parsed["experiences"]}
    experiences, seen = [], set()
    for entry in (data.get("experiences") if isinstance(data, dict) and isinstance(data.get("experiences"), list) else []):
        if not isinstance(entry, dict) or entry.get("experience") not in by_id or entry["experience"] in seen:
            continue
        seen.add(entry["experience"])
        exp = by_id[entry["experience"]]
        bullets = [b.get("source") for b in entry.get("bullets") or [] if isinstance(b, dict)] if isinstance(entry.get("bullets"), list) else []
        experiences.append({"experience": exp["id"], "block": entry.get("block") if entry.get("block") in BLOCKS else "otra",
                            "responsibilities": unique(bullets, set(exp["responsibilities"])),
                            "skills": unique(entry.get("skills"), set(exp["skills"]))})
    out = {"experiences": experiences}
    for key in CATEGORIES:
        out[key] = unique(data.get(key) if isinstance(data, dict) else [], set(parsed[key]))
    return out


# ── HTML ─────────────────────────────────────────────────────────────────────

def _spans(*values) -> str:
    return "".join(f"<span>{html.escape(v)}</span>" for v in values if v)


def build_html(resume: dict, selection: dict, truths=()) -> str:
    """CV en HTML (una columna, A4, CSS en línea, sin recursos externos).

    Todo el texto sale del CV base, del banco de verdades, de `personal_information` o (título, perfil y viñetas, ya
    verificados) de la selección, escapado: cada dato en su propio elemento y los separadores en el CSS. Todas las
    experiencias salen, de la más nueva a la más vieja (sin fecha legible, al final en el orden del CV base): las
    «relevante» primero, con sus viñetas (o responsabilidades) y habilidades, y las «otra» después, solo con el puesto y la
    meta (y la viñeta de habilidad transferible, si el modelo escribió una). Estudios, idiomas y datos personales salen siempre completos.
    """
    parsed, person = _parse(resume, truths), resume.get("personal_information")
    person = person if isinstance(person, dict) else {}
    out = ["<!doctype html>", '<html lang="es"><head><meta charset="utf-8"><title>Currículum</title>',
           f"<style>{CSS}</style></head><body>",
           f"<header><h1>{_spans(_text(person.get('name')), _text(person.get('surname')))}</h1>"]
    if selection.get("headline"):
        out.append(f'<div class="headline">{html.escape(selection["headline"])}</div>')
    out.append(f'<div class="contact">{_spans(*(_text(person.get(k)) for k in CONTACT))}</div></header>')
    if selection.get("summary", {}).get("text"):
        out.append(f"<section><h2>Perfil</h2><p>{html.escape(selection['summary']['text'])}</p></section>")
    by_date = _recent_first(parsed["experiences"], lambda e: _start(e["period"]))
    relevant = [e for e in by_date if selection["experiences"][e["id"]]["block"] == "relevante"]
    other = [e for e in by_date if selection["experiences"][e["id"]]["block"] != "relevante"]
    if relevant:
        out.append("<section><h2>Experiencia relevante</h2>")
        for e in relevant:
            picked = selection["experiences"][e["id"]]
            out.append(f'<h3>{html.escape(e["position"])}</h3><div class="meta">{_spans(*e["meta"])}</div><ul>')
            if picked.get("bullets"):
                out += [f"<li>{html.escape(b['text'])}</li>" for b in picked["bullets"]]
            else:
                out += [f"<li>{html.escape(e['responsibilities'][i])}</li>" for i in picked["responsibilities"]]
            out.append("</ul>")
            if picked["skills"]:
                out.append(f'<div class="skills"><h4>Habilidades</h4>{_spans(*(e["skills"][i] for i in picked["skills"]))}</div>')
        out.append("</section>")
    if other:
        out.append(f"<section><h2>{'Otra experiencia' if relevant else 'Experiencia'}</h2>")
        for e in other:
            out.append(f'<h3>{html.escape(e["position"])}</h3><div class="meta">{_spans(*e["meta"])}</div>')
            if bullets := selection["experiences"][e["id"]].get("bullets"):
                out.append("<ul>" + "".join(f"<li>{html.escape(b['text'])}</li>" for b in bullets) + "</ul>")
        out.append("</section>")
    if selection["technical_skills"]:
        out.append("<section><h2>Conocimientos técnicos</h2><ul>")
        out += [f"<li>{html.escape(parsed['technical_skills'][i][0])}</li>" for i in selection["technical_skills"]]
        out.append("</ul></section>")
    for key in ("projects", "achievements", "certifications"):
        if selection[key]:
            out.append(f"<section><h2>{CATEGORIES[key][1]}</h2><ul>")
            for id_ in selection[key]:
                name, description = parsed[key][id_]
                out.append("<li>" + (f'<span class="name">{html.escape(name)}</span>' if name else "")
                           + (f"<span>{html.escape(description)}</span>" if description else "") + "</li>")
            out.append("</ul></section>")
    education = _recent_first(_entries(resume, "education_details"),
                              lambda e: (year, 0) if (year := _end_year(e.get("year_of_completion"))) else None)
    if education:
        out.append("<section><h2>Educación</h2><ul>")
        for e in education:
            fields = ["education_level", "field_of_study", "institution", "start_date", "year_of_completion", "final_evaluation_grade"]
            values = [_text(e.get(k)) for k in fields]
            if (end := _end_year(e.get("year_of_completion"))) and end > datetime.now().year:
                values[4] += " (en curso)"  # el texto del CV base no se toca: se suma la marca
            out.append(f'<li class="meta">{_spans(*values)}</li>')
        out.append("</ul></section>")
    languages = _entries(resume, "languages")
    if languages:
        out.append("<section><h2>Idiomas</h2><ul>")
        out += [f'<li class="meta">{_spans(_text(l.get("language")), LEVELS.get(_text(l.get("proficiency")).lower(), _text(l.get("proficiency"))))}</li>'
                for l in languages]
        out.append("</ul></section>")
    if selection["interests"]:
        out.append("<section><h2>Intereses</h2><ul>")
        out += [f"<li>{html.escape(parsed['interests'][i][0])}</li>" for i in selection["interests"]]
        out.append("</ul></section>")
    out.append("</body></html>")
    return "\n".join(out)


def build_revision(resume: dict, selection: dict, truths=(), unmet=(), version: str = CV_PROMPT_VERSION,
                   fallback_reason: str | None = None, replaced=()) -> str:
    """Página para revisar el CV: cada línea generada junto a la línea del CV base de donde sale, y lo que no se cubre."""
    base = items(resume, truths)

    def source(id_: str) -> str:
        return f"{html.escape(id_)}: {html.escape(base.get(id_, ''))}"

    out = ["<!doctype html>", '<html lang="es"><head><meta charset="utf-8"><title>Revisión del CV</title>',
           f"<style>{REVISION_CSS}</style></head><body>", "<h1>Revisión del CV</h1>",
           f"<p>Versión del armado: {html.escape(version)}.</p>"]
    if fallback_reason:
        out.append(f'<p class="warn">Se armó solo con ids del CV base, sin texto adaptado: {html.escape(fallback_reason)}</p>')
    if replaced:
        out.append('<p class="warn">Estas líneas no pasaron el control y se reemplazaron: el título por el puesto de la experiencia '
                   'relevante más reciente (o se sacó), el perfil se sacó y cada viñeta por el texto de su fuente (copia) o se sacó '
                   'si esa fuente ya estaba en otra viñeta de la experiencia.</p><ul>'
                   + "".join(f"<li>{html.escape(item)}</li>" for item in replaced) + "</ul>")
    if selection.get("headline"):
        out.append(f"<h2>Título</h2><p>{html.escape(selection['headline'])}</p>")
    summary = selection.get("summary") or {}
    if summary.get("text"):
        out.append(f"<h2>Perfil</h2><p>{html.escape(summary['text'])}</p><ul>")
        out += [f"<li>Fuente {source(i)}</li>" for i in summary.get("sources", [])]
        out.append("</ul>")
    bullets = [(e_id, b) for e_id, picked in selection["experiences"].items() for b in picked.get("bullets", [])]
    if bullets:
        kinds = ["copia" if _is_copy(b["text"], base.get(b["source"], "")) else "adaptada" for _, b in bullets]
        if "adaptada" not in kinds:
            out.append('<p class="warn">Todas las viñetas son copia de la línea del CV base: el modelo no las reescribió con el vocabulario de la oferta.</p>')
        out.append("<h2>Viñetas generadas</h2><table><tr><th>Experiencia</th><th>Línea generada</th><th>Línea del CV base de la que sale</th><th>Tipo</th></tr>")
        out += [f"<tr><td>{html.escape(e_id)}</td><td>{html.escape(b['text'])}</td><td>{source(b['source'])}</td><td>{kind}</td></tr>"
                for (e_id, b), kind in zip(bullets, kinds)]
        out.append("</table>")
    out.append("<h2>Requisitos que el CV no muestra</h2>")
    out.append("<ul>" + "".join(f"<li>{html.escape(item)}</li>" for item in unmet) + "</ul>" if unmet else "<p>Ninguno.</p>")
    out.append("</body></html>")
    return "\n".join(out)


def _print_pdf(pw, html_path: Path, pdf_path: Path) -> None:
    browser = pw.chromium.launch(headless=True)
    try:
        context = browser.new_context()
        # Solo el HTML local: cualquier otro pedido se corta.
        context.route("**/*", lambda route: route.continue_() if route.request.url.startswith("file:") else route.abort())
        page = context.new_page()
        page.goto(html_path.resolve().as_uri())
        page.pdf(path=str(pdf_path), format="A4", print_background=True, prefer_css_page_size=True)
    finally:
        browser.close()


def render_pdf(html_path: Path, pdf_path: Path, playwright=None, pw=None) -> None:
    """Imprime el HTML local a PDF con Chromium headless en un contexto nuevo: nunca el perfil persistente `.browser-profile/`.

    `pw` es la instancia de Playwright que ya está abierta (la de `apply --live`): se usa esa, porque abrir otro `sync_playwright`
    dentro del primero falla («Sync API inside the asyncio loop»). Sin `pw` se abre uno (`playwright`, por defecto `sync_playwright`).
    """
    if pw is not None:
        _print_pdf(pw, html_path, pdf_path)
        return
    with (playwright or sync_playwright)() as opened:
        _print_pdf(opened, html_path, pdf_path)


def existing(conn: sqlite3.Connection, id16: str) -> dict | None:
    """El CV guardado de una oferta (`variant`, `html`, `pdf`, `created_at`), o None si no hay."""
    row = conn.execute("SELECT variant, html, pdf, created_at FROM cvs WHERE id16 = ? ORDER BY created_at DESC LIMIT 1",
                       (id16,)).fetchone()
    return dict(zip(("variant", "html", "pdf", "created_at"), row)) if row else None


def revision_path(html_path) -> Path:
    """Dónde está la revisión de un CV: `<stem>.revision.html`, junto a `<stem>.html`."""
    html_path = Path(html_path)
    return html_path.with_name(html_path.name.removesuffix(".html") + ".revision.html")


def headline_of(html_path) -> str:
    """El titular que `build_html` puso en un CV guardado (vacío si no tiene o no se puede leer)."""
    try:
        page = Path(html_path).read_text(encoding="utf-8")
    except OSError:
        return ""
    found = re.search(r'<div class="headline">(.*?)</div>', page, re.S)
    return html.unescape(found.group(1)).strip() if found else ""


def text_of(html_path) -> str:
    """El texto de un CV a medida guardado, una línea por renglón, sin el nombre ni los datos de contacto ni el CSS (vacío si no se puede leer)."""
    try:
        page = Path(html_path).read_text(encoding="utf-8")
    except OSError:
        return ""
    page = re.sub(r'<style>.*?</style>|<title>.*?</title>|<h1>.*?</h1>|<div class="contact">.*?</div>', "", page, flags=re.S)
    page = re.sub(r"</span>", " · ", page)
    page = re.sub(r"</(?:h[1-6]|p|li|div|section|ul|header)>|<br\s*/?>", "\n", page)
    lines = [" ".join(html.unescape(line).split()).strip(" ·") for line in re.sub(r"<[^>]+>", "", page).splitlines()]
    return "\n".join(line for line in lines if line)


def _slug(text: str) -> str:
    plain = "".join(ch for ch in unicodedata.normalize("NFD", text) if not unicodedata.combining(ch))
    return re.sub(r"[^A-Za-z0-9]+", "-", plain).strip("-")


def pdf_name(person: dict, headline: str) -> str:
    """El nombre con el que se sube un CV a Computrabajo: `CV-<Nombre>-<Apellido>-<Titulo>.pdf`.

    Nombre y apellido salen de `personal_information` y el título es el `headline` del CV. Solo letras sin acentos, dígitos y
    guiones, hasta 80 caracteres antes de la extensión (Computrabajo saca los espacios del nombre, por eso no hay).
    """
    person = person if isinstance(person, dict) else {}
    parts = ["CV", _slug(_text(person.get("name"))), _slug(_text(person.get("surname"))), _slug(_text(headline))]
    return "-".join(part for part in parts if part)[:80].rstrip("-") + ".pdf"


NAME_MAX = 80  # caracteres del nombre antes de la extensión
CONFIDENTIAL_COMPANY = "Confidencial"


def application_pdf_name(person: dict, title: str, company: str | None) -> str:
    """El nombre con el que se sube el CV de una postulación: `CV-<Nombre>-<Apellido>-<Puesto de la oferta>-<Empresa>.pdf`.

    El puesto es el título de la oferta y la empresa, la de la oferta («Confidencial» si no tiene). Solo letras sin acentos, dígitos y
    guiones, hasta `NAME_MAX` caracteres antes de la extensión: si no entra, se recorta primero el puesto (después la empresa).
    """
    person = person if isinstance(person, dict) else {}
    head = "-".join(part for part in ("CV", _slug(_text(person.get("name"))), _slug(_text(person.get("surname")))) if part)
    job, firm = _slug(_text(title)), _slug(_text(company)) or CONFIDENTIAL_COMPANY
    room = NAME_MAX - len(head) - 1  # lo que queda para «puesto-empresa» (el guion que lo une al nombre)
    if len(job) + 1 + len(firm) > room:
        job = job[:max(room - 1 - len(firm), 0)].rstrip("-")
    body = "-".join(part for part in (job, firm) if part)[:max(room, 0)].rstrip("-")
    return "-".join(part for part in (head, body) if part)[:NAME_MAX].rstrip("-") + ".pdf"


SMALL_WORDS = frozenset({"a", "al", "de", "del", "el", "la", "las", "los", "y", "e", "o", "u", "en", "con", "para", "por", "un", "una"})


def natural_pdf_name(person: dict, keyword: str | None, fallback: str) -> str:
    """El nombre con el que se sube el CV de una postulación: `CV-<Nombre>-<Apellido>-<Puesto>.pdf`. El puesto es la búsqueda que trajo la
    oferta (`keyword`) con palabras en mayúscula inicial, sin tildes y con guiones («atención al cliente» -> `Atencion-al-Cliente`); sin
    búsqueda, el `fallback` (el id de la variante). Nunca la empresa ni el título de la oferta. Hasta `NAME_MAX` caracteres antes de la extensión."""
    person = person if isinstance(person, dict) else {}
    head = "-".join(part for part in ("CV", _slug(_text(person.get("name"))), _slug(_text(person.get("surname")))) if part)
    words = [w for w in (_slug(_text(keyword)) or _slug(_text(fallback))).split("-") if w]
    role = "-".join(w.lower() if n and w.lower() in SMALL_WORDS else w[:1].upper() + w[1:].lower() for n, w in enumerate(words))
    return "-".join(part for part in (head, role) if part)[:NAME_MAX].rstrip("-") + ".pdf"


def numbered_name(name: str, taken) -> str:
    """El mismo nombre si `taken(nombre)` es falso; si no, con `-2`, `-3`… (el primer número libre). Sigue entrando en `NAME_MAX`."""
    stem, extension = (name[:-4], name[-4:]) if name.lower().endswith(".pdf") else (name, "")
    candidate, number = name, 1
    while taken(candidate):
        number += 1
        tail = f"-{number}"
        candidate = f"{stem[:NAME_MAX - len(tail)].rstrip('-')}{tail}{extension}"
    return candidate


def add_suffix(name: str, id16: str) -> str:
    """El mismo nombre con los 4 últimos caracteres de `id16` (cuando la cuenta ya tiene un CV con ese nombre para otra oferta); sigue
    entrando en `NAME_MAX`."""
    stem, extension = (name[:-4], name[-4:]) if name.lower().endswith(".pdf") else (name, "")
    tail = _slug(id16)[-4:]
    return f"{stem[:NAME_MAX - len(tail) - 1].rstrip('-')}-{tail}{extension}" if tail else name


def top_candidates(conn: sqlite3.Connection, model: str, n: int) -> list[str]:
    """Las `n` ofertas nuevas mejor puntuadas (modelo y `PROMPT_VERSION` actuales) que todavía no tienen CV de su variante."""
    return [row[0] for row in conn.execute(
        "SELECT s.id16 FROM scores s JOIN offers o ON o.id16 = s.id16 WHERE s.model = ? AND s.prompt_version = ? "
        "AND o.status = 'new' AND NOT EXISTS (SELECT 1 FROM cvs c WHERE c.id16 = s.id16 AND c.variant = s.best_variant) "
        "ORDER BY s.score DESC, o.first_seen, o.rowid LIMIT ?", (model, score.PROMPT_VERSION, max(n, 0)))]


def _ask_and_check(client, model: str, user: str, parsed: dict, vocab: _Vocab, levels: set[str], roles: frozenset[str], calls: list):
    """Un pedido con texto adaptado: anota su costo en `calls` y devuelve (selección, data, motivos de rechazo, copió).

    `copió` es True si todas las viñetas son copia de su fuente: no es un motivo de rechazo en sí, pero pide un reintento.
    """
    resp = score.ask_json(client, model, SYSTEM, user, "cv_adaptado", _schema(parsed), max_tokens=MAX_CV_TOKENS)
    tokens = score._tokens(resp)
    call = {"cost": score.cost_usd(model, *tokens), "tokens": tokens, "valid": False}
    calls.append(call)
    if getattr(resp, "status", None) != "completed" or score._has_refusal(getattr(resp, "output", None)):
        return None, None, [(None, "el modelo no terminó la respuesta o rechazó el pedido.")], False
    try:
        data = json.loads(resp.output_text)
    except (AttributeError, TypeError, ValueError):
        return None, None, [(None, "la respuesta del modelo no es un JSON válido.")], False
    try:
        selection, problems = _validate_text(parsed, data, vocab, levels, roles)
    except _Bad as e:
        return None, data, [(None, str(e))], False
    copied = _all_copies(parsed, selection)
    call["valid"] = not problems and not copied
    return selection, data, problems, copied


def make(conn: sqlite3.Connection, profile: dict, client, model: str, id16: str, out_dir: Path = Path("data/cvs"),
         render=None, stop_file: Path = Path("STOP"), variant: str | None = None) -> dict:
    """Arma el CV de una oferta con la variante que mejor le encaja (o con `variant`, la que eligió el usuario en una tanda) y lo guarda en `out_dir` (HTML, PDF y revisión) y en `cvs`.

    La oferta tiene que tener puntaje de `model` y el `PROMPT_VERSION` actual y no ser `duplicate`. Respeta el tope
    mensual y frena con `STOP`. `render(html, pdf)` imprime el PDF (por defecto `render_pdf`). El modelo adapta título,
    perfil y viñetas; el código rechaza lo que agregue un hecho que no está en el CV base ni en el banco de verdades. Si el
    primer pedido no verifica, o todas sus viñetas son copia de su fuente, se repite una vez con los motivos; si vuelve a
    fallar solo en algunas líneas, se reemplazan esas líneas (`_repair`) y el resto se conserva (`replaced` dice cuáles y por
    qué); si falla la estructura (ids inexistentes, fuentes de otra experiencia, viñetas de más), se arma el CV de solo ids
    (`c4`) con lo que sirva de esa respuesta y `fallback_reason` dice por qué. Si la segunda respuesta sigue copiando pero no
    tiene otro problema, se usa igual (copiar no inventa nada) y la revisión lo avisa. Cada respuesta paga queda en `api_spend`. Si ni
    así hay CV (o el PDF no sirve), no guarda nada (ni archivos ni fila en `cvs`). Devuelve `id16`, `variant`, `html`, `pdf`,
    `revision` (rutas), `version` (`c8` o `c4`), `fallback_reason`, `replaced` (lista de «campo: motivo»), `cost_usd`, `input_tokens` y `output_tokens` (de todos los pedidos).
    """
    if stop_file.exists():
        raise RuntimeError(f"Existe el archivo {stop_file}: se frena antes de armar el CV. Borralo para volver a correr.")
    if model not in score.PRICES:
        raise RuntimeError(f"No tengo el precio del modelo '{model}': agregá su precio en score.PRICES.")
    offer = conn.execute("SELECT title, company, location, employment_type, description, status FROM offers WHERE id16 = ?",
                         (id16,)).fetchone()
    if offer is None:
        raise RuntimeError(f"No existe la oferta {id16} en la base.")
    if offer[5] == "duplicate":
        raise RuntimeError(f"La oferta {id16} es una duplicada de otra: armá el CV de la original.")
    row = conn.execute("SELECT best_variant, unmet_required FROM scores WHERE id16 = ? AND model = ? AND prompt_version = ?",
                       (id16, model, score.PROMPT_VERSION)).fetchone()
    if row is None:
        raise RuntimeError(f"La oferta {id16} no tiene puntaje de {model} {score.PROMPT_VERSION}: corré el puntaje primero.")
    variant, unmet = variant or row[0], (json.loads(row[1] or "[]") if variant in (None, row[0]) else [])  # los excluyentes son los de la mejor variante
    chosen = next((v for v in profile["variants"] if v["id"] == variant), None)
    if chosen is None:
        raise RuntimeError(f"La variante «{variant}» de la oferta {id16} ya no está en profile.yaml.")
    resume, truths = chosen["resume"], chosen.get("truths", [])
    cap, spent = profile["run"]["max_cost_month"], score.month_spent(conn, datetime.now(timezone.utc).date())
    if cap - spent <= 0:
        raise RuntimeError(f"Tope mensual alcanzado: {score.amounts(spent, cap)}. No se arma el CV.")

    offer_text = score._offer_text(*offer[:5])
    parsed, vocab, levels = _parse(resume, truths), _base_vocabulary(resume, truths, offer_text), _allowed_levels(resume)
    roles = title_roles(offer[0])
    user = offer_text + "\n\n" + _request_text(parsed, unmet)
    calls, saved = [], False
    try:  # cada respuesta ya se pagó: se anota en `api_spend` aunque no sirva (ok = 0)
        selection, data, problems, copied = _ask_and_check(client, model, user, parsed, vocab, levels, roles, calls)
        if problems or copied:
            feedback = "\n".join(f"- {p}" for p in [*(m for _, m in problems), *([COPY_REASON] if copied else [])])
            intro = ("Tu respuesta anterior tuvo líneas inválidas; corregilas (usá solo hechos de los items y del banco de "
                     "verdades, sin siglas, tecnologías, números ni niveles que no estén ahí):" if problems
                     else "Tu respuesta anterior se puede mejorar:")
            selection, data, problems, copied = _ask_and_check(client, model, f"{user}\n\n{intro}\n{feedback}", parsed, vocab, levels, roles, calls)
            if not problems:
                calls[-1]["valid"] = True  # copiar no inventa nada: si la segunda sigue copiando, se usa igual
        fallback_reason, version, replaced = None, CV_PROMPT_VERSION, []
        reasons = [m for _, m in problems]
        if problems:
            if data is None:
                raise RuntimeError(FAIL + " ".join(reasons) + NOT_SAVED)
            if any(field is None for field, _ in problems):  # error de estructura: CV de solo ids con lo que sirva de la 2.ª respuesta
                fallback_reason = " ".join(reasons)
                selection, version = _validate_ids(parsed, _salvage(parsed, data)), IDS_ONLY_VERSION
            else:  # solo fallan algunas líneas: se reemplazan y el resto de la adaptación se conserva
                selection, replaced = _repair(parsed, selection, problems), reasons
                calls[-1]["valid"] = True
        page = build_html(resume, selection, truths)
        review = build_revision(resume, selection, truths, unmet, version, fallback_reason, replaced)

        out_dir.mkdir(parents=True, exist_ok=True)
        stem = re.sub(r"[^A-Za-z0-9_-]", "_", f"{id16}-{variant}")
        html_path, pdf_path = out_dir / f"{stem}.html", out_dir / f"{stem}.pdf"
        review_path = revision_path(html_path)
        tmp_html, tmp_pdf, tmp_review = (out_dir / f"{stem}.tmp.html", out_dir / f"{stem}.tmp.pdf", out_dir / f"{stem}.tmp.revision.html")
        try:
            tmp_html.write_text(page, encoding="utf-8")
            tmp_review.write_text(review, encoding="utf-8")
            (render or render_pdf)(tmp_html, tmp_pdf)
            if not tmp_pdf.is_file() or tmp_pdf.stat().st_size >= MAX_PDF_BYTES:
                raise RuntimeError(FAIL + f"el PDF no se generó o pesa {MAX_PDF_BYTES // 1024 // 1024} MB o más." + NOT_SAVED)
            os.replace(tmp_html, html_path)
            os.replace(tmp_pdf, pdf_path)
            os.replace(tmp_review, review_path)
        finally:
            for tmp in (tmp_html, tmp_pdf, tmp_review):
                tmp.unlink(missing_ok=True)
        cost = sum(c["cost"] for c in calls)
        db.save_cv(conn, id16, variant, model, version, str(html_path), str(pdf_path), cost,
                   sum(c["tokens"][0] for c in calls), sum(c["tokens"][2] for c in calls))
        saved = True
    finally:
        for call in calls:
            db.add_spend(conn, "cv", id16, model, call["cost"], call["tokens"][0], call["tokens"][2], saved and call["valid"])
    return {"id16": id16, "variant": variant, "html": html_path, "pdf": pdf_path, "revision": review_path, "version": version,
            "fallback_reason": fallback_reason, "replaced": replaced, "cost_usd": cost, "input_tokens": sum(c["tokens"][0] for c in calls),
            "output_tokens": sum(c["tokens"][2] for c in calls)}


# ── banco de verdades propuesto por el programa ──────────────────────────────

MAX_PROPOSALS, MAX_TRUTH, MAX_REASON = 25, 120, 120
PROPOSE_SYSTEM = (
    "Recibís el CV de una persona, con un id por item. Proponé hechos que se siguen de lo que esa persona hizo, para que ella "
    "los apruebe o los rechace. Solo deducciones directas y conservadoras: herramientas que un trabajo implica (quien usó "
    "React usó JavaScript y npm), habilidades blandas que una tarea demuestra, o conceptos que una materia o una "
    "certificación cubre. Cada propuesta lleva `text` (hasta 120 caracteres, en primera persona y cierta para quien hizo lo "
    "que dice el CV), `sources` (los ids de los items de los que se deduce) y `reason` (hasta 120 caracteres: por qué se "
    "deduce). Nada de años, números, niveles de seniority, títulos ni empresas. No repitas lo que el CV ya dice ni lo que "
    "figura en «Ya propuesto, aprobado o rechazado». Si no hay nada que deducir con seguridad, devolvé una lista vacía."
)


def _study(e: dict) -> str:
    """Un estudio como una línea: nivel, carrera, institución y, si las trae, las materias."""
    subjects = e.get("exam")
    names = [_text(k) for k in (subjects if isinstance(subjects, (dict, list)) else [])]
    base = " - ".join(t for t in (_text(e.get("education_level")), _text(e.get("field_of_study")), _text(e.get("institution"))) if t)
    return base + (f". Materias: {', '.join(n for n in names if n)}" if any(names) else "")


def source_items(resume: dict) -> dict[str, str]:
    """Los items con id que pueden ser fuente de una propuesta: los de `items` (sin el banco) más los estudios (`S1`, `S2`…)."""
    found = items(resume)
    found.update({f"S{n}": _study(e) for n, e in enumerate(_entries(resume, "education_details"), 1)})
    return found


def _proposals_schema(ids: list[str]) -> dict:
    proposal = {"type": "object", "properties": {"text": {"type": "string"}, "sources": _ids_array(ids), "reason": {"type": "string"}},
                "required": ["text", "sources", "reason"], "additionalProperties": False}
    return {"type": "object", "properties": {"proposals": {"type": "array", "maxItems": MAX_PROPOSALS, "items": proposal}},
            "required": ["proposals"], "additionalProperties": False}


def _proposal_problem(text: str, sources: list, ids: set[str]) -> bool:
    """Si una propuesta no sirve: números, ids en el texto, palabras de nivel, o fuentes vacías o inexistentes."""
    if any(ch.isdigit() for ch in text) or _LOOSE_ID.search(text):
        return True
    words = {part for token, _ in _words(text) for part in [token.lower(), *re.split(r"[/\-]", token.lower())]}
    return bool(words & LEVEL_WORDS) or not sources or any(s not in ids for s in sources)


def propose_truths(resume: dict, client, model: str, known=(), calls: list | None = None) -> list[dict]:
    """Un pedido a la API con el CV base (nunca datos personales ni links): lo que se sigue de lo que la persona hizo.

    Devuelve `[{text, sources, reason}]` (hasta 25). Descarta las propuestas con números, ids en el texto, palabras de nivel
    (`LEVEL_WORDS`), `sources` vacías o inexistentes, de más de 120 caracteres, y las que repiten un item del CV, uno de
    `known` (banco, propuestas pendientes y rechazadas) o una propuesta anterior de la misma respuesta. `calls` recibe el
    costo de la respuesta (como en `make`); si la respuesta no sirve, RuntimeError.
    """
    sources = source_items(resume)
    if not sources:
        return []
    parsed = _parse(resume)
    studies = [f"{id_}: {text}" for id_, text in sources.items() if id_.startswith("S")]
    user = _request_text(parsed) + ("\n\nEstudios:\n" + "\n".join(studies) if studies else "")
    if known:
        user += "\n\nYa propuesto, aprobado o rechazado:\n" + "\n".join(f"- {t}" for t in known)
    resp = score.ask_json(client, model, PROPOSE_SYSTEM, user, "verdades_propuestas", _proposals_schema(list(sources)),
                          max_tokens=MAX_CV_TOKENS)
    tokens = score._tokens(resp)
    call = {"cost": score.cost_usd(model, *tokens), "tokens": tokens, "valid": False}
    if calls is not None:
        calls.append(call)
    fail = "No se propusieron verdades: "
    if getattr(resp, "status", None) != "completed" or score._has_refusal(getattr(resp, "output", None)):
        raise RuntimeError(fail + "el modelo no terminó la respuesta o rechazó el pedido.")
    try:
        data = json.loads(resp.output_text)
    except (AttributeError, TypeError, ValueError):
        raise RuntimeError(fail + "la respuesta del modelo no es un JSON válido.") from None
    if not isinstance(data, dict) or not isinstance(data.get("proposals"), list):
        raise RuntimeError(fail + FORMAT)
    call["valid"] = True
    seen = {_norm(t) for t in [*sources.values(), *map(_text, known)]}
    out = []
    for item in data["proposals"][:MAX_PROPOSALS]:
        if not isinstance(item, dict) or not isinstance(item.get("text"), str) or not isinstance(item.get("sources"), list) \
                or not all(isinstance(s, str) for s in item["sources"]) or not isinstance(item.get("reason"), str):
            continue
        text, reason = " ".join(item["text"].split()), " ".join(item["reason"].split())
        ids = list(dict.fromkeys(item["sources"]))
        if not text or len(text) > MAX_TRUTH or _norm(text) in seen or _proposal_problem(text, ids, set(sources)):
            continue
        seen.add(_norm(text))
        out.append({"text": text, "sources": ids, "reason": reason if len(reason) <= MAX_REASON else reason[:MAX_REASON - 1] + "…"})
    return out
