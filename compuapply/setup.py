"""Deja listos `.env` y `profile.yaml` sin que el usuario los edite a mano. La clave nunca se imprime."""
import getpass
import os
from pathlib import Path

import openai

from compuapply import profile as profile_module
from compuapply.score import DEFAULT_MODEL, PRICES, api_error_detail

RESUME_NAME = "plain_text_resume.yaml"
ENV_NAMES = ("OPENAI_API_KEY", "OPENAI_MODEL")


def _env_value(lines: list[str], name: str) -> str:
    """Valor de `name` en las líneas de un `.env` (el primero que aparece, sin comillas); vacío si no está."""
    for line in lines:
        key, _, value = line.strip().partition("=")
        if key.strip() == name and not line.lstrip().startswith("#"):
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            return value
    return ""


def setup_env(env_path: Path, ask=getpass.getpass, client_factory=openai.OpenAI, replace: bool = False) -> str:
    """Pide la clave de OpenAI sin mostrarla, la verifica gratis y la guarda en `env_path`; devuelve el mensaje final.

    Si el archivo ya tiene la clave no hace nada, salvo con `replace`: verifica la nueva y recién ahí reemplaza la
    línea de `OPENAI_API_KEY` sin tocar las demás. Si no verifica, no escribe. Ningún mensaje incluye la clave.
    """
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    had_key = bool(_env_value(lines, "OPENAI_API_KEY"))
    if had_key and not replace:
        return "Ya hay una clave en .env (no se muestra)"
    model = os.environ.get("OPENAI_MODEL") or _env_value(lines, "OPENAI_MODEL") or DEFAULT_MODEL
    key = ask("Pegá tu clave de OpenAI (no se va a ver): ").strip()
    if not key:
        raise RuntimeError("No pegaste ninguna clave. No se guardó nada.")
    if not key.startswith("sk-"):
        raise RuntimeError("La clave no tiene el formato esperado (empieza con «sk-»). No se guardó nada.")
    try:
        client_factory(api_key=key).models.retrieve(model)
    except openai.AuthenticationError:
        raise RuntimeError("La clave no es válida: OpenAI la rechazó. No se guardó nada.") from None
    except openai.NotFoundError:
        raise RuntimeError(f"Tu cuenta no tiene acceso a {model}. No se guardó nada.") from None
    except openai.OpenAIError as e:  # solo la clase: el texto de la API puede traer parte de la clave
        detail = f": {why}" if (why := api_error_detail(e)) else ""
        raise RuntimeError(
            f"OpenAI respondió con un error ({type(e).__name__}{detail}) y no se pudo verificar la clave. No se guardó nada."
        ) from None
    if had_key:  # la línea vieja cambia en su lugar; si la clave estaba repetida, las otras copias se van
        first = next(i for i, line in enumerate(lines) if line.partition("=")[0].strip() == "OPENAI_API_KEY")
        lines[first] = f"OPENAI_API_KEY={key}"
        lines = [line for i, line in enumerate(lines) if i == first or line.partition("=")[0].strip() != "OPENAI_API_KEY"]
        message = f"Clave reemplazada en .env y verificada con {model}"
    else:
        kept = [line for line in lines if line.partition("=")[0].strip() not in ENV_NAMES]
        lines = [*kept, f"OPENAI_API_KEY={key}", f"OPENAI_MODEL={model}"]
        message = f"Clave guardada en .env y verificada con {model}"
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return message


def set_model(env_path: Path, model: str) -> None:
    """Pone `OPENAI_MODEL=<model>` en `env_path` (reemplaza la línea o la agrega) sin tocar las demás líneas."""
    if model not in PRICES:
        raise RuntimeError(f"El modelo «{model}» no está en la tabla de precios. Elegí uno de: {', '.join(PRICES)}.")
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    new = f"OPENAI_MODEL={model}"
    for i, line in enumerate(lines):
        if line.partition("=")[0].strip() == "OPENAI_MODEL":  # las líneas comentadas no coinciden: traen el «#»
            lines[i] = new
            break
    else:
        lines.append(new)
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def list_cv_dirs(cv_dir: Path) -> list[str]:
    """Nombres de las subcarpetas de `cv_dir` que tienen un `plain_text_resume.yaml`, en orden alfabético."""
    if not cv_dir.is_dir():
        raise RuntimeError("La carpeta de --cv-dir no existe o no es una carpeta.")
    found = (p.name for p in cv_dir.iterdir() if p.is_dir() and (p / RESUME_NAME).is_file())
    return sorted(found, key=str.lower)


def _quote(text: str) -> str:
    """Texto como escalar YAML entre comillas simples (las barras de Windows quedan tal cual)."""
    return "'" + text.replace("'", "''") + "'"


def write_profile(cv_dir: Path, variants: list[str], out: Path = Path("profile.yaml")) -> Path:
    """Crea `out` con una variante por carpeta elegida y lo valida; si no pasa, lo quita. Nunca pisa uno existente."""
    available = list_cv_dirs(cv_dir)
    listing = ", ".join(available) or "ninguna"
    if not 1 <= len(variants) <= profile_module.MAX_VARIANTS:
        raise RuntimeError(
            f"Elegí de 1 a {profile_module.MAX_VARIANTS} variantes (elegiste {len(variants)}). Disponibles: {listing}."
        )
    unknown = [name for name in variants if name not in available]
    if unknown:
        raise RuntimeError(f"No hay una carpeta con {RESUME_NAME} para: {', '.join(unknown)}. Disponibles: {listing}.")
    if len(set(variants)) != len(variants):
        raise RuntimeError("Repetiste una variante: cada una va una sola vez.")
    if out.exists():
        raise RuntimeError(f"{out.name} ya existe: no lo piso")
    lines = ["# Perfil creado con `python -m compuapply setup profile`. Se puede editar a mano.", "variants:"]
    for name in variants:
        lines += [
            f"  - id: {_quote(name)}",
            f"    resume: {_quote(str((cv_dir / name / RESUME_NAME).resolve()))}",
            "    # Completá las palabras que describen este rol: `run` busca ofertas con cada una.",
            '    keywords: ["programador"]',
            "    # Completá cuando tengas el PDF de esta variante (ruta absoluta o relativa a este archivo).",
            "    # Hace falta para postular, no para buscar ni puntuar. Ejemplo: pdf: ruta/al/pdf.pdf",
        ]
    lines += ["exclude_companies: []", "exclude_title_words: []", "apply:", "  threshold: 75", "  daily_cap: 10"]
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        profile_module.load_profile(out)
    except RuntimeError as e:
        out.unlink()
        shown = str(e).replace(str(cv_dir.resolve()), "<--cv-dir>")  # las rutas del usuario no se imprimen
        raise RuntimeError(f"El perfil no pasó la validación y no se creó. {shown}") from None
    return out
