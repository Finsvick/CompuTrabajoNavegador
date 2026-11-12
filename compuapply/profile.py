"""Carga de profile.yaml y de los CVs (formato AIHawk plain_text_resume.yaml)."""
import re
import unicodedata
from pathlib import Path

import yaml

MAX_VARIANTS = 3
DEFAULT_THRESHOLD = 75
DEFAULT_DAILY_CAP = 10
DEFAULT_COMPANY_DAILY_CAP = 1  # `apply.company_daily_cap`: postulaciones por día a una misma empresa
DEFAULT_RUN = {"pages": 2, "max_details": 20, "score_limit": 20, "max_cost": 0.50, "max_cost_month": 2.00}
DEFAULT_MAX_ACCOUNT_PDFS = 10  # `run.max_account_pdfs`: el límite real de Computrabajo; con más PDFs, `apply --live` no postula, y con tantos libera un lugar
DEFAULT_FREE_CV_SLOTS = True  # `apply.free_cv_slots`: con la cuenta llena, borrar un CV sin riesgo para poder subir
DEFAULT_REVIEW_CV = True  # `apply.review_cv`: mientras sea true, `apply --live` no sube ningún CV a medida
# Claves fijas de `answers` (las carga el usuario en Parámetros); las demás son preguntas normalizadas.
ANSWER_KEYS = ("salary", "availability", "modality", "start", "location", "mobility", "salary_part_time", "salary_daily")
MODALITIES = ("presencial", "híbrido", "remoto")
MODALITY_SEPARATORS = re.compile(r"\s*[,/;]\s*|\s+[yo]\s+")  # coma, barra, punto y coma, « y » y « o »
# El límite real de Computrabajo es una incógnita de la Fase 0; este es un tope prudente.
MAX_PDF_BYTES = 5 * 1024 * 1024


def _read_yaml(path: Path, que: str) -> dict:
    """Lee un YAML que tiene que ser un mapa. Los errores nunca citan el contenido del archivo."""
    if not path.is_file():
        raise RuntimeError(f"No existe {que}: {path}.")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        raise RuntimeError(f"No se pudo leer {que} ({path}). Revisá los permisos y que esté guardado en UTF-8.") from None
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        donde = f", cerca de la línea {mark.line + 1}" if mark else ""
        raise RuntimeError(f"{que.capitalize()} ({path}) no es un YAML válido{donde}.") from None
    if not isinstance(data, dict):
        raise RuntimeError(f"{que.capitalize()} ({path}) tiene que ser un mapa YAML de clave: valor.")
    return data


def _str_list(value) -> list[str]:
    if isinstance(value, str):
        value = [value]
    return [str(v) for v in value or []]


def _check_pdf(path: Path, shown: str, vid: str) -> None:
    """Valida el PDF de una variante sin mostrar su contenido (solo lee los 5 bytes de la firma)."""
    donde = f"El PDF de la variante '{vid}' ({shown})"
    if not path.is_file():
        raise RuntimeError(f"{donde} no existe. Generalo con tu otro proyecto y poné su ruta en 'pdf'.")
    if path.suffix.lower() != ".pdf":
        raise RuntimeError(f"{donde} no tiene extensión .pdf.")
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            signature = f.read(5)
    except OSError:
        raise RuntimeError(f"No se pudo leer el PDF de la variante '{vid}' ({shown}). Revisá los permisos.") from None
    if size == 0:
        raise RuntimeError(f"{donde} está vacío.")
    if size > MAX_PDF_BYTES:
        raise RuntimeError(f"{donde} pesa {size / 1024 / 1024:.2f} MB y el máximo es {MAX_PDF_BYTES // 1024 // 1024} MB.")
    if signature != b"%PDF-":
        raise RuntimeError(f"{donde} no empieza con %PDF-: no parece un PDF.")


def normalize_question(text: str) -> str:
    """La pregunta sin mayúsculas, acentos ni signos y con los espacios colapsados (la clave de `answers`)."""
    plain = "".join(ch for ch in unicodedata.normalize("NFD", str(text)) if not unicodedata.combining(ch))
    return " ".join(re.sub(r"[^\w\s]", " ", plain.casefold()).split())


def _unaccented(text: str) -> str:
    return "".join(ch for ch in unicodedata.normalize("NFD", str(text)) if not unicodedata.combining(ch)).casefold()


def _zones(value, path: Path) -> list[str]:
    """Sección opcional `zones`: lista de provincias como las escribe Computrabajo (textos no vacíos). Ausente o vacía: no se filtra."""
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(zone, str) and zone.strip() for zone in value):
        raise RuntimeError(f"'zones' en {path} tiene que ser una lista de provincias (textos no vacíos), por ejemplo "
                           "[Capital Federal, Buenos Aires-GBA].")
    return [" ".join(zone.split()) for zone in value]


def load_zones(path: Path = Path("profile.yaml")) -> list[str]:
    """Las `zones` de `profile.yaml` sin cargar los CV; [] si el archivo no existe. Un valor inválido lanza RuntimeError."""
    return _zones(_read_yaml(path, "el perfil").get("zones"), path) if path.is_file() else []


def province(location: str | None) -> str:
    """La provincia de un lugar «Lugar, Provincia»: lo que viene después de la última coma, o todo el texto si no hay coma."""
    return (location or "").rsplit(",", 1)[-1].strip()


def in_zone(location: str | None, zones) -> bool:
    """True si la provincia del lugar es una de las `zones` (sin mayúsculas, acentos ni espacios de más). Sin `zones` todo entra;
    con `zones`, un lugar vacío queda fuera."""
    if not zones:
        return True
    wanted = {" ".join(_unaccented(zone).split()) for zone in zones}
    return " ".join(_unaccented(province(location)).split()) in wanted


def canonical_modality(text: str) -> str | None:
    """Las modalidades que acepta el candidato en su forma canónica («presencial, híbrido, remoto»: en el orden de `MODALITIES`, con
    acentos y sin repetidas), o None si alguna parte no es ninguna de las 3. Sin distinguir mayúsculas ni acentos; se separan con
    `,`, `/`, `;`, « y » u « o »."""
    parts = {part for part in MODALITY_SEPARATORS.split(_unaccented(text).strip()) if part}
    known = {_unaccented(m): m for m in MODALITIES}
    if not parts or not parts <= set(known):
        return None
    return ", ".join(m for m in MODALITIES if _unaccented(m) in parts)


def _answers(value, path: Path) -> dict[str, str]:
    """Sección opcional `answers`: las 4 claves fijas (`ANSWER_KEYS`) y preguntas normalizadas -> respuesta, todo texto."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise RuntimeError(f"'answers' en {path} tiene que ser un mapa de pregunta a respuesta (más salary, availability, modality, start, location, mobility, salary_part_time y salary_daily).")
    answers = {}
    for key, item in value.items():
        if isinstance(item, bool) or not isinstance(item, (str, int, float)) or not str(item).strip():
            raise RuntimeError(f"La respuesta '{key}' de 'answers' en {path} tiene que ser un texto no vacío.")
        text = " ".join(str(item).split())
        name = str(key) if str(key) in ANSWER_KEYS else normalize_question(key)
        if not name:
            raise RuntimeError(f"Una pregunta de 'answers' en {path} no tiene letras ni números.")
        answers[name] = text
    if "modality" in answers:  # queda en su forma canónica
        answers["modality"] = canonical_modality(answers["modality"])
        if answers["modality"] is None:
            raise RuntimeError(f"'answers.modality' en {path} tiene que ser una o más de presencial, híbrido o remoto.")
    return answers


def _load_truths(path: Path, shown: str, vid: str) -> list[str]:
    """Banco de verdades de una variante: un YAML con una lista de textos cortos y ciertos. Los errores no citan su contenido."""
    donde = f"El banco de verdades de la variante '{vid}' ({shown})"
    if not path.is_file():
        raise RuntimeError(f"{donde} no existe.")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        raise RuntimeError(f"No se pudo leer el banco de verdades de la variante '{vid}' ({shown}). Revisá los permisos y que esté en UTF-8.") from None
    except yaml.YAMLError:
        raise RuntimeError(f"{donde} no es un YAML válido.") from None
    if not isinstance(data, list):
        raise RuntimeError(f"{donde} tiene que ser una lista de textos (una línea «- texto» por cada verdad).")
    for n, item in enumerate(data, 1):
        if not isinstance(item, str):
            raise RuntimeError(f"{donde} tiene un item que no es texto (el {n}).")
        if not item.strip():
            raise RuntimeError(f"{donde} tiene un item vacío (el {n}).")
    return [item.strip() for item in data]


def _apply_settings(value, path: Path) -> dict:
    """Sección opcional `apply`: umbral de puntaje (0 a 100), tope diario (1 o más), tope diario por empresa (`company_daily_cap`, 1 o más,
    por defecto 1), `calibrated_with` (texto) y `review_cv` (booleano), opcionales."""
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise RuntimeError(f"'apply' en {path} tiene que ser un mapa con 'threshold' y 'daily_cap'.")
    threshold = DEFAULT_THRESHOLD if value.get("threshold") is None else value["threshold"]
    daily_cap = DEFAULT_DAILY_CAP if value.get("daily_cap") is None else value["daily_cap"]
    company_cap = DEFAULT_COMPANY_DAILY_CAP if value.get("company_daily_cap") is None else value["company_daily_cap"]
    if type(threshold) is not int or not 0 <= threshold <= 100:
        raise RuntimeError(f"'apply.threshold' en {path} tiene que ser un entero entre 0 y 100 (hay {threshold!r}).")
    if type(daily_cap) is not int or daily_cap < 1:
        raise RuntimeError(f"'apply.daily_cap' en {path} tiene que ser un entero de 1 o más (hay {daily_cap!r}).")
    if type(company_cap) is not int or company_cap < 1:
        raise RuntimeError(f"'apply.company_daily_cap' en {path} tiene que ser un entero de 1 o más (hay {company_cap!r}).")
    calibrated_with = value.get("calibrated_with")
    if calibrated_with is not None and not isinstance(calibrated_with, str):
        raise RuntimeError(f"'apply.calibrated_with' en {path} tiene que ser un texto, por ejemplo 'gpt-4o-mini v2' (hay {calibrated_with!r}).")
    review_cv = value.get("review_cv")
    if review_cv is not None and not isinstance(review_cv, bool):
        raise RuntimeError(f"'apply.review_cv' en {path} tiene que ser true o false (hay {review_cv!r}).")
    free_slots = value.get("free_cv_slots")
    if free_slots is not None and not isinstance(free_slots, bool):
        raise RuntimeError(f"'apply.free_cv_slots' en {path} tiene que ser true o false (hay {free_slots!r}).")
    settings = {"threshold": threshold, "daily_cap": daily_cap, "company_daily_cap": company_cap}
    if free_slots is not None:  # sin él, la clave no está y vale `DEFAULT_FREE_CV_SLOTS`
        settings["free_cv_slots"] = free_slots
    if review_cv is not None:  # sin él, la clave no está y vale `DEFAULT_REVIEW_CV`
        settings["review_cv"] = review_cv
    if (calibrated_with or "").strip():  # sin él, la clave no está: el umbral no salió de la calibración
        settings["calibrated_with"] = calibrated_with.strip()
    return settings


def _run_settings(value, path: Path) -> dict:
    """Sección opcional `run`: páginas (1 o más), detalles y ofertas a puntuar (0 o más), los dos topes de gasto (mayores que 0)
    `max_account_pdfs` (entero de 1 o más) y `last_batch` (la última tanda; las dos claves están solo si el perfil las declara)."""
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise RuntimeError(
            f"'run' en {path} tiene que ser un mapa con 'pages', 'max_details', 'score_limit', 'max_cost' y 'max_cost_month'.")
    settings = {key: default if value.get(key) is None else value[key] for key, default in DEFAULT_RUN.items()}
    for key, minimum in (("pages", 1), ("max_details", 0), ("score_limit", 0)):
        if type(settings[key]) is not int or settings[key] < minimum:
            raise RuntimeError(f"'run.{key}' en {path} tiene que ser un entero de {minimum} o más (hay {settings[key]!r}).")
    for key in ("max_cost", "max_cost_month"):
        cost = settings[key]
        if isinstance(cost, bool) or not isinstance(cost, (int, float)) or not cost > 0:
            raise RuntimeError(f"'run.{key}' en {path} tiene que ser un número mayor que 0 (hay {cost!r}).")
        settings[key] = float(cost)
    pdfs = value.get("max_account_pdfs")
    if pdfs is not None:
        if type(pdfs) is not int or pdfs < 1:
            raise RuntimeError(f"'run.max_account_pdfs' en {path} tiene que ser un entero de 1 o más (hay {pdfs!r}).")
        settings["max_account_pdfs"] = pdfs
    last = value.get("last_batch")
    if last is not None:  # la última tanda (variante, búsquedas, minutos, tope): la guarda el programa
        if not isinstance(last, dict):
            raise RuntimeError(f"'run.last_batch' en {path} tiene que ser un mapa con 'variant', 'keywords', 'minutes' y 'max'.")
        keywords = last.get("keywords")
        keywords = ", ".join(str(k) for k in keywords) if isinstance(keywords, list) else str(keywords or "")
        for key in ("minutes", "max"):
            if last.get(key) is not None and (type(last[key]) is not int or last[key] < (1 if key == "minutes" else 0)):
                raise RuntimeError(f"'run.last_batch.{key}' en {path} tiene que ser un entero ({'1' if key == 'minutes' else '0'} o más).")
        settings["last_batch"] = {"variant": str(last.get("variant") or ""), "keywords": keywords, "minutes": last.get("minutes"),
                                  "max": last.get("max")}
    return settings


def load_profile(path: Path = Path("profile.yaml")) -> dict:
    """Perfil con sus variantes ya cargadas: {variants: [{id, keywords, resume, pdf[, truths][, default_pdf_name]}], exclude_*, apply, run, answers, zones}.

    `zones` son las provincias a las que se limitan las ofertas (`in_zone`); vacía si el perfil no las declara.

    `answers` es el mapa de respuestas del usuario (vacío si no declara la sección): las claves fijas `salary`,
    `availability`, `modality`, `start`, `location`, `mobility`, `salary_part_time` y `salary_daily` (si las cargó) y preguntas normalizadas (`normalize_question`) -> respuesta.

    `truths` (lista de textos) está solo si la variante declara `truths: <ruta a un YAML>`.

    `apply` trae `threshold` y `daily_cap`, y `calibrated_with` solo si el perfil lo declara.

    `pdf` es la ruta resuelta, o None si la variante todavía no lo declara (si lo declara, se valida).
    """
    if not path.is_file():
        raise RuntimeError(f"No existe el perfil: {path}. Copiá profile.example.yaml a {path.name} y completalo.")
    data = _read_yaml(path, "el perfil")
    items = data.get("variants")
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_VARIANTS:
        raise RuntimeError(f"'variants' en {path} tiene que ser una lista de 1 a {MAX_VARIANTS} variantes de CV.")
    variants, ids, pdf_owner = [], set(), {}
    for n, item in enumerate(items, 1):
        if not isinstance(item, dict):
            raise RuntimeError(f"La variante {n} de {path} tiene que tener 'id', 'resume' y 'keywords'.")
        vid = str(item.get("id") or "").strip()
        if not vid:
            raise RuntimeError(f"La variante {n} de {path} no tiene 'id'.")
        if vid in ids:
            raise RuntimeError(f"El id '{vid}' está repetido en {path}.")
        ids.add(vid)
        if not item.get("resume"):
            raise RuntimeError(f"La variante '{vid}' de {path} no tiene 'resume' (ruta al CV).")
        resume_path = path.parent / str(item["resume"])
        resume = _read_yaml(resume_path, f"el CV de la variante '{vid}' (indicado en {path})")
        if not (resume.get("experience_details") or resume.get("projects")):
            raise RuntimeError(f"El CV de la variante '{vid}' ({resume_path}) no tiene 'experience_details' ni 'projects'.")
        pdf = None  # opcional: `apply --live` no lo usa (sube el CV a medida de cada oferta)
        if item.get("pdf"):
            pdf = (path.parent / str(item["pdf"])).resolve()
            _check_pdf(pdf, str(item["pdf"]), vid)
            if pdf in pdf_owner:
                raise RuntimeError(f"Las variantes '{pdf_owner[pdf]}' y '{vid}' usan el mismo PDF: cada una necesita el suyo.")
            pdf_owner[pdf] = vid
        variant = {"id": vid, "keywords": _str_list(item.get("keywords")), "resume": resume, "pdf": pdf}
        if item.get("default_pdf_name") is not None:  # el CV base tal como aparece en `uploadcv`: se restaura como por defecto al terminar
            name = item["default_pdf_name"]
            if not isinstance(name, str) or not name.strip():
                raise RuntimeError(f"'default_pdf_name' de la variante '{vid}' en {path} tiene que ser el nombre del PDF, como en la página de CV de Computrabajo.")
            variant["default_pdf_name"] = name.strip()
        if item.get("truths"):  # opcional: hechos ciertos que el CV a medida puede usar (el programa nunca lo escribe)
            variant["truths"] = _load_truths(path.parent / str(item["truths"]), str(item["truths"]), vid)
        variants.append(variant)
    return {
        "variants": variants,
        "exclude_companies": _str_list(data.get("exclude_companies")),
        "exclude_title_words": _str_list(data.get("exclude_title_words")),
        "apply": _apply_settings(data.get("apply"), path),
        "run": _run_settings(data.get("run"), path),
        "answers": _answers(data.get("answers"), path),
        "zones": _zones(data.get("zones"), path),
    }


def _text(value) -> str:
    return " ".join(str(value).split()) if value is not None else ""


def _join(parts, sep: str) -> str:
    return sep.join(p for p in map(_text, parts) if p)


def _entries(resume: dict, key: str) -> list[dict]:
    return [e for e in resume.get(key) or [] if isinstance(e, dict)]


def _experience(e: dict) -> str:
    extra = _join([e.get("employment_period"), e.get("industry")], ", ")
    lines = ["- " + _join([_join([e.get("position"), e.get("company")], " en "), f"({extra})" if extra else ""], " ")]
    for r in e.get("key_responsibilities") or []:  # cada ítem es {responsibility_N: texto}
        lines += [f"  * {_text(v)}" for v in (r.values() if isinstance(r, dict) else [r])]
    skills = _join(_str_list(e.get("skills_acquired")), ", ")
    if skills:
        lines.append(f"  Habilidades: {skills}")
    return "\n".join(lines)


def _education(e: dict) -> str:
    years = _join([e.get("start_date"), e.get("year_of_completion")], "-")
    what = _join([_join([e.get("education_level"), e.get("field_of_study")], ", "), e.get("institution")], " - ")
    return "- " + _join([what, f"({years})" if years else ""], " ")


def _yes_no(value) -> str:
    return ("Sí" if value else "No") if isinstance(value, bool) else _text(value)


def technical_skills(resume: dict) -> list[str]:
    """Los textos de `technical_skills` del CV, en el orden del CV base.

    Acepta una lista de textos, una lista de mapas (`{nombre: nivel}` o `{name/skill, level}` -> «nombre: nivel») o un
    mapa `{categoría: [textos]}`. Un item vacío o que no es texto se ignora; no se agrega nada.
    """
    def flat(value) -> list[str]:
        if isinstance(value, str):
            return [value.strip()] if value.strip() else []
        if isinstance(value, list):
            return [text for item in value for text in flat(item)]
        if not isinstance(value, dict):
            return []
        name, level = value.get("name") or value.get("skill"), value.get("level")
        if isinstance(name, str) and name.strip() and set(value) <= {"name", "skill", "level"}:
            level = _text(level) if isinstance(level, (str, int, float)) and not isinstance(level, bool) else ""
            return [f"{name.strip()}: {level}" if level else name.strip()]
        found = []
        for key, item in value.items():
            if isinstance(item, (list, dict)):
                found += flat(item)  # {categoría: [textos]}: se queda con los textos
            elif isinstance(item, str) and item.strip() or isinstance(item, (int, float)) and not isinstance(item, bool):
                found.append(f"{_text(key)}: {_text(item)}")  # {nombre: nivel}
        return found

    return flat(resume.get("technical_skills"))


def resume_for_llm(resume: dict) -> str:
    """Texto plano del CV para el LLM. Solo toma las secciones permitidas: nada de datos personales."""
    prefs = resume.get("work_preferences") if isinstance(resume.get("work_preferences"), dict) else {}
    sections = {
        "Experiencia": [_experience(e) for e in _entries(resume, "experience_details")],
        # Sin `link`: la URL suele traer el usuario de GitHub.
        "Conocimientos técnicos": ["- " + text for text in technical_skills(resume)],
        "Proyectos": ["- " + _join([p.get("name"), p.get("description")], ": ") for p in _entries(resume, "projects")],
        "Estudios": [_education(e) for e in _entries(resume, "education_details")],
        "Certificaciones": [
            "- " + _join([c.get("name"), c.get("description")], ": ") for c in _entries(resume, "certifications")
        ],
        "Idiomas": ["- " + _join([l.get("language"), l.get("proficiency")], ": ") for l in _entries(resume, "languages")],
        "Intereses": [_join(_str_list(resume.get("interests")), ", ")],
        "Modalidad": [
            f"- {label}: {_yes_no(prefs[key])}"
            for key, label in (("remote_work", "Trabajo remoto"), ("in_person_work", "Trabajo presencial"))
            if prefs.get(key) is not None
        ],
    }
    return "\n\n".join(f"{title}:\n" + "\n".join(lines) for title, lines in sections.items() if any(lines))
