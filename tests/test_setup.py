"""Tests de setup.py y de `setup env` / `setup profile`. Claves falsas (sk-test-...), cliente falso, carpetas en tmp_path."""
import os
from pathlib import Path
from types import SimpleNamespace

import httpx
import openai
import pytest
import yaml

from compuapply import cli, profile, score, setup

KEY ="sk-test-FALSA-123"
API_TEXT = "texto de la API con sk-test-FALSA-123"
REQUEST = httpx.Request("GET", "https://api.openai.com/v1/models/x")


def api_error(cls, code: int):
    return cls(API_TEXT, response=httpx.Response(code, request=REQUEST), body=None)


def make_factory(error=None):
    """Fábrica de cliente falso: anota la clave recibida y los modelos consultados; `error` se lanza al consultar."""
    seen = SimpleNamespace(api_keys=[], models=[])

    def retrieve(model):
        seen.models.append(model)
        if error:
            raise error

    client = SimpleNamespace(models=SimpleNamespace(retrieve=retrieve))

    def factory(api_key):
        seen.api_keys.append(api_key)
        return client

    return factory, seen


def asker(answer: str = KEY):
    prompts = []

    def ask(prompt):
        prompts.append(prompt)
        return answer

    ask.prompts = prompts
    return ask


def never(*args, **kwargs):
    raise AssertionError("no tenía que llamarse")


@pytest.fixture
def clean_env(monkeypatch):
    """Entorno aislado: setup lee OPENAI_MODEL del entorno."""
    env = {}
    monkeypatch.setattr(os, "environ", env)
    return env


# ── setup_env ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("content", [
    "# nota\nOTRA=1\nOPENAI_API_KEY=sk-test-ya-estaba\n",
    'OPENAI_API_KEY="sk-test-entre-comillas"\n',
    "OPENAI_API_KEY = sk-test-con-espacios\n",
])
def test_existing_key_asks_nothing_and_touches_nothing(tmp_path, clean_env, content):
    env_file = tmp_path / ".env"
    env_file.write_text(content, encoding="utf-8")
    before = env_file.read_bytes()
    message = setup.setup_env(env_file, ask=never, client_factory=never)
    assert message == "Ya hay una clave en .env (no se muestra)"
    assert "sk-test" not in message and env_file.read_bytes() == before


@pytest.mark.parametrize("content", ["# OPENAI_API_KEY=sk-test-comentada\n", "OPENAI_API_KEY=\n", "OPENAI_API_KEY=''\n"])
def test_commented_or_empty_key_does_not_count(tmp_path, clean_env, content):
    env_file = tmp_path / ".env"
    env_file.write_text(content, encoding="utf-8")
    factory, seen = make_factory()
    setup.setup_env(env_file, ask=asker(), client_factory=factory)
    assert seen.api_keys == [KEY]
    lines = env_file.read_text(encoding="utf-8").splitlines()
    assert lines.count(f"OPENAI_API_KEY={KEY}") == 1 and "OPENAI_API_KEY=" not in lines  # sin línea vacía que tape a la buena


def test_good_key_is_verified_then_written(tmp_path, clean_env):
    env_file = tmp_path / ".env"
    factory, seen = make_factory()
    ask = asker(f"  {KEY}  \n")
    message = setup.setup_env(env_file, ask=ask, client_factory=factory)
    assert message == "Clave guardada en .env y verificada con gpt-6-luna"
    assert ask.prompts == ["Pegá tu clave de OpenAI (no se va a ver): "]
    assert seen.api_keys == [KEY] and seen.models == ["gpt-6-luna"]  # sin espacios; consulta gratis del modelo
    assert env_file.read_text(encoding="utf-8") == f"OPENAI_API_KEY={KEY}\nOPENAI_MODEL=gpt-6-luna\n"
    assert KEY not in message


def test_existing_env_without_key_keeps_the_other_lines(tmp_path, clean_env):
    env_file = tmp_path / ".env"
    env_file.write_text("# comentario\nOTRA=1\n# OPENAI_API_KEY=sk-test-comentada\nOPENAI_MODEL=gpt-4o-mini\n\nULTIMA=2\n",
                        encoding="utf-8")
    factory, seen = make_factory()
    message = setup.setup_env(env_file, ask=asker(), client_factory=factory)
    assert seen.models == ["gpt-4o-mini"]  # el modelo que ya estaba en el archivo
    assert message == "Clave guardada en .env y verificada con gpt-4o-mini"
    assert env_file.read_text(encoding="utf-8").splitlines() == [
        "# comentario", "OTRA=1", "# OPENAI_API_KEY=sk-test-comentada", "", "ULTIMA=2",
        f"OPENAI_API_KEY={KEY}", "OPENAI_MODEL=gpt-4o-mini"]


def test_model_from_the_environment_wins(tmp_path, clean_env):
    clean_env["OPENAI_MODEL"] = "gpt-5-nano"
    env_file = tmp_path / ".env"
    env_file.write_text("OPENAI_MODEL=gpt-4o-mini\n", encoding="utf-8")
    factory, seen = make_factory()
    setup.setup_env(env_file, ask=asker(), client_factory=factory)
    assert seen.models == ["gpt-5-nano"]
    assert env_file.read_text(encoding="utf-8").splitlines() == [f"OPENAI_API_KEY={KEY}", "OPENAI_MODEL=gpt-5-nano"]


@pytest.mark.parametrize("answer, message", [
    ("", "No pegaste ninguna clave"),
    ("   \n", "No pegaste ninguna clave"),
    ("abc123", "formato esperado"),
    ("pk-test-1", "formato esperado"),
])
def test_empty_or_malformed_key_is_rejected_without_calling_the_api_or_writing(tmp_path, clean_env, answer, message):
    env_file = tmp_path / ".env"
    with pytest.raises(RuntimeError, match=message) as err:
        setup.setup_env(env_file, ask=asker(answer), client_factory=never)
    assert not env_file.exists() and "abc123" not in str(err.value)


@pytest.mark.parametrize("error, message", [
    (api_error(openai.AuthenticationError, 401), "La clave no es válida"),
    (api_error(openai.NotFoundError, 404), "Tu cuenta no tiene acceso a gpt-6-luna"),
    (api_error(openai.RateLimitError, 429), "RateLimitError"),
    (api_error(openai.InternalServerError, 500), "InternalServerError"),
    (openai.APIConnectionError(request=REQUEST), "APIConnectionError"),
])
@pytest.mark.parametrize("existing", [None, "OTRA=1\n"])
def test_api_failures_do_not_write_and_do_not_leak_the_key(tmp_path, clean_env, error, message, existing):
    env_file = tmp_path / ".env"
    if existing:
        env_file.write_text(existing, encoding="utf-8")
    factory, _ = make_factory(error)
    with pytest.raises(RuntimeError, match=message) as err:
        setup.setup_env(env_file, ask=asker(), client_factory=factory)
    assert KEY not in str(err.value) and "texto de la API" not in str(err.value) and err.value.__cause__ is None
    assert (env_file.read_text(encoding="utf-8") == existing) if existing else not env_file.exists()


# ── setup env por CLI: la clave no aparece en la salida ──────────────────────

@pytest.fixture
def cli_env(tmp_path, monkeypatch, clean_env):
    monkeypatch.chdir(tmp_path)
    return tmp_path


def patch_cli(monkeypatch, *, answer=KEY, error=None):
    factory, seen = make_factory(error)
    monkeypatch.setattr(cli.getpass, "getpass", asker(answer) if not callable(answer) else answer)
    monkeypatch.setattr(cli.openai, "OpenAI", factory)
    return seen


def test_cli_setup_env_success_never_prints_the_key(cli_env, monkeypatch, capsys):
    seen = patch_cli(monkeypatch)
    assert cli.main(["setup", "env"]) == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == "Clave guardada en .env y verificada con gpt-6-luna" and captured.err == ""
    assert KEY not in captured.out + captured.err and seen.api_keys == [KEY]
    assert (cli_env / ".env").read_text(encoding="utf-8") == f"OPENAI_API_KEY={KEY}\nOPENAI_MODEL=gpt-6-luna\n"


@pytest.mark.parametrize("error, message", [
    (api_error(openai.AuthenticationError, 401), "La clave no es válida"),
    (api_error(openai.NotFoundError, 404), "Tu cuenta no tiene acceso a gpt-6-luna"),
    (api_error(openai.RateLimitError, 429), "RateLimitError"),
])
def test_cli_setup_env_failures_exit_1_without_the_key_or_the_file(cli_env, monkeypatch, capsys, error, message):
    patch_cli(monkeypatch, error=error)
    assert cli.main(["setup", "env"]) == 1
    captured = capsys.readouterr()
    assert message in captured.err and captured.out == ""
    assert KEY not in captured.out + captured.err and "texto de la API" not in captured.err
    assert not (cli_env / ".env").exists()


def test_cli_setup_env_with_a_key_already_there_asks_nothing(cli_env, monkeypatch, capsys):
    (cli_env / ".env").write_text("OPENAI_API_KEY=sk-test-ya-estaba\n", encoding="utf-8")
    monkeypatch.setattr(cli.getpass, "getpass", never)
    monkeypatch.setattr(cli.openai, "OpenAI", never)
    assert cli.main(["setup", "env"]) == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == "Ya hay una clave en .env (no se muestra)"
    assert "sk-test" not in captured.out + captured.err


@pytest.mark.parametrize("exc, message", [(EOFError, "terminal interactiva"), (KeyboardInterrupt, "Cancelado")])
def test_cli_setup_env_without_terminal_or_with_ctrl_c(cli_env, monkeypatch, capsys, exc, message):
    def interrupted(prompt):
        raise exc

    patch_cli(monkeypatch, answer=interrupted)
    assert cli.main(["setup", "env"]) == 1
    assert message in capsys.readouterr().err and not (cli_env / ".env").exists()


# ── list_cv_dirs y write_profile ─────────────────────────────────────────────

FAKE_RESUME = {"personal_information": {"name": "Ana", "email": "ana@falso.test"},
               "experience_details": [{"position": "Dev", "company": "Acme"}],
               "projects": [{"name": "Proyecto", "description": "Falso"}]}


def make_cvs(tmp_path: Path, names=("backend", "data", "ventas")) -> Path:
    cv_dir = tmp_path / "cvs"
    for name in names:
        (cv_dir / name).mkdir(parents=True)
        (cv_dir / name / "plain_text_resume.yaml").write_text(yaml.safe_dump(FAKE_RESUME), encoding="utf-8")
    (cv_dir / "sin-cv").mkdir()
    (cv_dir / "sin-cv" / "otro.yaml").write_text("x: 1", encoding="utf-8")
    (cv_dir / "suelto.yaml").write_text("x: 1", encoding="utf-8")
    return cv_dir


def test_list_cv_dirs_only_folders_with_the_resume_file_sorted(tmp_path):
    cv_dir = make_cvs(tmp_path, names=("Zeta", "alfa", "Beta"))
    assert setup.list_cv_dirs(cv_dir) == ["alfa", "Beta", "Zeta"]


def test_list_cv_dirs_empty_folder_gives_an_empty_list(tmp_path):
    (tmp_path / "vacia").mkdir()
    assert setup.list_cv_dirs(tmp_path / "vacia") == []


@pytest.mark.parametrize("target", ["no-existe", "archivo.txt"])
def test_list_cv_dirs_rejects_a_missing_folder_without_showing_the_path(tmp_path, target):
    (tmp_path / "archivo.txt").write_text("x", encoding="utf-8")
    with pytest.raises(RuntimeError, match="no existe o no es una carpeta") as err:
        setup.list_cv_dirs(tmp_path / target)
    assert str(tmp_path) not in str(err.value)


def test_write_profile_writes_a_valid_profile_with_comments(tmp_path):
    cv_dir = make_cvs(tmp_path)
    out = setup.write_profile(cv_dir, ["backend", "data"], tmp_path / "profile.yaml")
    assert out == tmp_path / "profile.yaml"
    loaded = profile.load_profile(out)
    assert [v["id"] for v in loaded["variants"]] == ["backend", "data"]
    assert [v["keywords"] for v in loaded["variants"]] == [["programador"], ["programador"]]
    assert [v["pdf"] for v in loaded["variants"]] == [None, None]
    assert loaded["exclude_companies"] == [] and loaded["exclude_title_words"] == []
    assert loaded["apply"] == {"threshold": 75, "daily_cap": 10, "company_daily_cap": 1}
    text = out.read_text(encoding="utf-8")
    assert str((cv_dir / "backend" / "plain_text_resume.yaml").resolve()) in text  # ruta absoluta al yaml
    assert "pdf" not in yaml.safe_load(text)["variants"][0]
    lines = text.splitlines()
    keywords_at = next(i for i, line in enumerate(lines) if "keywords:" in line and not line.lstrip().startswith("#"))
    assert lines[keywords_at - 1].lstrip().startswith("#") and "Completá" in lines[keywords_at - 1]
    assert lines[keywords_at + 1].lstrip().startswith("#") and "PDF" in lines[keywords_at + 1]  # comentario sobre `pdf`
    assert "Completá" in lines[keywords_at + 1]


def test_write_profile_quotes_names_with_spaces_and_quotes(tmp_path):
    cv_dir = make_cvs(tmp_path, names=("Mi CV (dev) it's", "otro"))
    out = setup.write_profile(cv_dir, ["Mi CV (dev) it's"], tmp_path / "profile.yaml")
    [variant] = profile.load_profile(out)["variants"]
    assert variant["id"] == "Mi CV (dev) it's"


@pytest.mark.parametrize("variants, message", [
    ([], r"Elegí de 1 a 3 variantes \(elegiste 0\)"),
    (["backend", "data", "ventas", "extra"], r"Elegí de 1 a 3 variantes \(elegiste 4\)"),
    (["backend", "nada"], r"No hay una carpeta con plain_text_resume.yaml para: nada\. Disponibles: backend, data, ventas\."),
    (["backend", "backend"], "Repetiste una variante"),
])
def test_write_profile_rejects_bad_choices_and_writes_nothing(tmp_path, variants, message):
    cv_dir = make_cvs(tmp_path)
    with pytest.raises(RuntimeError, match=message):
        setup.write_profile(cv_dir, variants, tmp_path / "profile.yaml")
    assert not (tmp_path / "profile.yaml").exists()


def test_write_profile_does_not_overwrite_an_existing_file(tmp_path):
    cv_dir = make_cvs(tmp_path)
    existing = tmp_path / "profile.yaml"
    existing.write_text("mío: true\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="profile.yaml ya existe: no lo piso"):
        setup.write_profile(cv_dir, ["backend"], existing)
    assert existing.read_text(encoding="utf-8") == "mío: true\n"


def test_write_profile_removes_the_file_if_validation_fails_and_hides_the_paths(tmp_path):
    cv_dir = make_cvs(tmp_path)
    (cv_dir / "data" / "plain_text_resume.yaml").write_text(yaml.safe_dump({"personal_information": {"name": "Ana"}}),
                                                            encoding="utf-8")
    out = tmp_path / "profile.yaml"
    with pytest.raises(RuntimeError, match="no pasó la validación y no se creó") as err:
        setup.write_profile(cv_dir, ["backend", "data"], out)
    assert not out.exists()
    assert str(tmp_path) not in str(err.value) and "'data'" in str(err.value) and "<--cv-dir>" in str(err.value)


# ── setup profile por CLI ────────────────────────────────────────────────────

def test_cli_setup_profile_without_variants_lists_only_the_names(cli_env, capsys):
    cv_dir = make_cvs(cli_env)
    assert cli.main(["setup", "profile", "--cv-dir", str(cv_dir)]) == 0
    captured = capsys.readouterr()
    assert captured.out.splitlines() == ["backend", "data", "ventas", "Elegí 1 a 3 con --variants"] and captured.err == ""
    assert not (cli_env / "profile.yaml").exists()


def test_cli_setup_profile_without_variants_and_without_cvs_exits_1(cli_env, capsys):
    (cli_env / "vacia").mkdir()
    assert cli.main(["setup", "profile", "--cv-dir", str(cli_env / "vacia")]) == 1
    captured = capsys.readouterr()
    assert "no hay ninguna subcarpeta con plain_text_resume.yaml" in captured.err and captured.out == ""
    assert str(cli_env) not in captured.err


def test_cli_setup_profile_writes_validates_and_prints_no_path(cli_env, capsys):
    cv_dir = make_cvs(cli_env)
    assert cli.main(["setup", "profile", "--cv-dir", str(cv_dir), "--variants", "backend, data"]) == 0
    captured = capsys.readouterr()
    lines = captured.out.strip().splitlines()
    assert lines[0] == "profile.yaml creado con 2 variante(s). Completá las keywords y, cuando los tengas, los PDF."
    assert len(lines) == 3 and captured.err == ""
    assert lines[1].startswith("backend · experiencias: 1 · proyectos: 1 · caracteres para el LLM: ")
    assert lines[1].endswith("· pdf: falta") and "hace falta para postular" not in lines[1]
    assert lines[2].startswith("data · ") and lines[2].endswith("· pdf: falta")
    everything = captured.out + captured.err
    assert str(cli_env) not in everything and "pytest-of" not in everything and "plain_text_resume" not in everything
    assert [v["id"] for v in profile.load_profile(cli_env / "profile.yaml")["variants"]] == ["backend", "data"]


def test_cli_setup_profile_custom_out(cli_env, capsys):
    cv_dir = make_cvs(cli_env)
    assert cli.main(["setup", "profile", "--cv-dir", str(cv_dir), "--variants", "ventas", "--out", str(cli_env / "otro.yaml")]) == 0
    assert capsys.readouterr().out.startswith("otro.yaml creado con 1 variante(s)")
    assert (cli_env / "otro.yaml").exists() and not (cli_env / "profile.yaml").exists()


@pytest.mark.parametrize("variants, message", [
    ("", "Elegí de 1 a 3 variantes"),
    ("a,b,c,d", "Elegí de 1 a 3 variantes"),
    ("nada", "Disponibles: backend, data, ventas"),
])
def test_cli_setup_profile_bad_variants_exit_1_and_list_the_options(cli_env, capsys, variants, message):
    cv_dir = make_cvs(cli_env)
    assert cli.main(["setup", "profile", "--cv-dir", str(cv_dir), "--variants", variants]) == 1
    captured = capsys.readouterr()
    assert message in captured.err and captured.out == "" and not (cli_env / "profile.yaml").exists()


def test_cli_setup_profile_does_not_overwrite_profile_yaml(cli_env, capsys):
    cv_dir = make_cvs(cli_env)
    (cli_env / "profile.yaml").write_text("mío: true\n", encoding="utf-8")
    assert cli.main(["setup", "profile", "--cv-dir", str(cv_dir), "--variants", "backend"]) == 1
    assert "profile.yaml ya existe: no lo piso" in capsys.readouterr().err
    assert (cli_env / "profile.yaml").read_text(encoding="utf-8") == "mío: true\n"


def test_cli_setup_profile_missing_cv_dir_exits_1_without_the_path(cli_env, capsys):
    assert cli.main(["setup", "profile", "--cv-dir", str(cli_env / "no-existe")]) == 1
    err = capsys.readouterr().err
    assert "no existe o no es una carpeta" in err and str(cli_env) not in err


def test_cli_setup_profile_invalid_resume_exits_1_and_leaves_no_file(cli_env, capsys):
    cv_dir = make_cvs(cli_env)
    (cv_dir / "data" / "plain_text_resume.yaml").write_text(yaml.safe_dump({"personal_information": {"name": "Ana"}}),
                                                            encoding="utf-8")
    assert cli.main(["setup", "profile", "--cv-dir", str(cv_dir), "--variants", "backend,data"]) == 1
    captured = capsys.readouterr()
    assert "no pasó la validación" in captured.err and str(cli_env) not in captured.err and captured.out == ""
    assert not (cli_env / "profile.yaml").exists()


# ── set_model ────────────────────────────────────────────────────────────────

def test_set_model_replaces_only_its_line_and_keeps_the_others_in_place(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(f"# nota\nOPENAI_API_KEY={KEY}\n\nOPENAI_MODEL=gpt-6-luna\nULTIMA=1\n", encoding="utf-8")
    setup.set_model(env_file, "gpt-5-nano")
    assert env_file.read_text(encoding="utf-8") == f"# nota\nOPENAI_API_KEY={KEY}\n\nOPENAI_MODEL=gpt-5-nano\nULTIMA=1\n"


def test_set_model_adds_the_line_at_the_end_when_there_is_none(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(f"OPENAI_API_KEY={KEY}\n# OPENAI_MODEL=gpt-6-luna\n", encoding="utf-8")  # la comentada no cuenta
    setup.set_model(env_file, "gpt-4o-mini")
    assert env_file.read_text(encoding="utf-8").splitlines() == [
        f"OPENAI_API_KEY={KEY}", "# OPENAI_MODEL=gpt-6-luna", "OPENAI_MODEL=gpt-4o-mini"]


def test_set_model_creates_the_file_when_it_does_not_exist(tmp_path):
    setup.set_model(tmp_path / ".env", "gpt-6-luna")
    assert (tmp_path / ".env").read_text(encoding="utf-8") == "OPENAI_MODEL=gpt-6-luna\n"


def test_set_model_only_touches_the_first_model_line_and_spaces_around_the_equal_sign(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("OPENAI_MODEL = gpt-4o-mini\nOPENAI_MODEL=gpt-5-nano\n", encoding="utf-8")
    setup.set_model(env_file, "gpt-6-luna")
    assert env_file.read_text(encoding="utf-8").splitlines() == ["OPENAI_MODEL=gpt-6-luna", "OPENAI_MODEL=gpt-5-nano"]


@pytest.mark.parametrize("model", ["gpt-6-luna", "gpt-4o-mini", "gpt-5-nano"])
def test_set_model_accepts_every_model_of_the_price_table(tmp_path, model, clean_env):
    assert model in score.PRICES
    setup.set_model(tmp_path / ".env", model)
    factory, seen = make_factory()
    setup.setup_env(tmp_path / ".env", ask=asker(), client_factory=factory)  # setup_env lee el modelo que quedó
    assert seen.models == [model]


@pytest.mark.parametrize("existing", [None, f"OPENAI_API_KEY={KEY}\nOPENAI_MODEL=gpt-6-luna\n"])
@pytest.mark.parametrize("model", ["gpt-9-inexistente", "", "GPT-6-LUNA"])
def test_set_model_rejects_a_model_outside_the_price_table_and_writes_nothing(tmp_path, existing, model):
    env_file = tmp_path / ".env"
    if existing is not None:
        env_file.write_text(existing, encoding="utf-8")
    before = env_file.read_bytes() if existing is not None else None
    with pytest.raises(RuntimeError, match="no está en la tabla de precios") as err:
        setup.set_model(env_file, model)
    assert all(name in str(err.value) for name in score.PRICES) and KEY not in str(err.value)
    assert (env_file.read_bytes() if existing is not None else None) == before and env_file.exists() == (existing is not None)


# ── setup_env con replace ────────────────────────────────────────────────────

OLD_KEY = "sk-test-VIEJA-456"
ENV_WITH_OLD_KEY = f"# nota\nOTRA=1\nOPENAI_API_KEY={OLD_KEY}\nOPENAI_MODEL=gpt-4o-mini\nULTIMA=2\n"


def test_replace_verifies_the_new_key_then_replaces_only_its_line(tmp_path, clean_env):
    env_file = tmp_path / ".env"
    env_file.write_text(ENV_WITH_OLD_KEY, encoding="utf-8")
    factory, seen = make_factory()
    ask = asker()
    message = setup.setup_env(env_file, ask=ask, client_factory=factory, replace=True)
    assert seen.api_keys == [KEY] and seen.models == ["gpt-4o-mini"]  # verifica con el modelo del archivo
    assert ask.prompts == ["Pegá tu clave de OpenAI (no se va a ver): "]
    assert env_file.read_text(encoding="utf-8") == ENV_WITH_OLD_KEY.replace(OLD_KEY, KEY)  # las demás líneas, intactas
    assert message == "Clave reemplazada en .env y verificada con gpt-4o-mini"
    assert KEY not in message and OLD_KEY not in message


def test_without_replace_an_existing_key_is_still_left_alone(tmp_path, clean_env):
    env_file = tmp_path / ".env"
    env_file.write_text(ENV_WITH_OLD_KEY, encoding="utf-8")
    assert setup.setup_env(env_file, ask=never, client_factory=never) == "Ya hay una clave en .env (no se muestra)"
    assert env_file.read_text(encoding="utf-8") == ENV_WITH_OLD_KEY


@pytest.mark.parametrize("error, message", [
    (api_error(openai.AuthenticationError, 401), "La clave no es válida"),
    (api_error(openai.NotFoundError, 404), "Tu cuenta no tiene acceso a gpt-4o-mini"),
    (api_error(openai.RateLimitError, 429), "RateLimitError"),
    (openai.APIConnectionError(request=REQUEST), "APIConnectionError"),
])
def test_replace_with_a_key_that_does_not_verify_leaves_the_env_identical(tmp_path, clean_env, error, message):
    env_file = tmp_path / ".env"
    env_file.write_text(ENV_WITH_OLD_KEY, encoding="utf-8")
    before = env_file.read_bytes()
    factory, _ = make_factory(error)
    with pytest.raises(RuntimeError, match=message) as err:
        setup.setup_env(env_file, ask=asker(), client_factory=factory, replace=True)
    assert env_file.read_bytes() == before
    assert KEY not in str(err.value) and OLD_KEY not in str(err.value) and "texto de la API" not in str(err.value)


@pytest.mark.parametrize("answer, message", [("", "No pegaste ninguna clave"), ("abc123", "formato esperado")])
def test_replace_with_an_empty_or_malformed_key_does_not_call_the_api_or_write(tmp_path, clean_env, answer, message):
    env_file = tmp_path / ".env"
    env_file.write_text(ENV_WITH_OLD_KEY, encoding="utf-8")
    with pytest.raises(RuntimeError, match=message) as err:
        setup.setup_env(env_file, ask=asker(answer), client_factory=never, replace=True)
    assert env_file.read_text(encoding="utf-8") == ENV_WITH_OLD_KEY and OLD_KEY not in str(err.value)


def test_replace_normalizes_the_line_keeps_comments_and_drops_repeated_copies_of_the_key(tmp_path, clean_env):
    env_file = tmp_path / ".env"
    env_file.write_text(f'# OPENAI_API_KEY=sk-test-comentada\nOPENAI_API_KEY = "{OLD_KEY}"\nOTRA=1\nOPENAI_API_KEY=sk-test-otra\n',
                        encoding="utf-8")
    factory, _ = make_factory()
    setup.setup_env(env_file, ask=asker(), client_factory=factory, replace=True)
    assert env_file.read_text(encoding="utf-8").splitlines() == [
        "# OPENAI_API_KEY=sk-test-comentada", f"OPENAI_API_KEY={KEY}", "OTRA=1"]


def test_replace_uses_the_model_of_the_environment_without_touching_the_file_model(tmp_path, clean_env):
    clean_env["OPENAI_MODEL"] = "gpt-5-nano"
    env_file = tmp_path / ".env"
    env_file.write_text(ENV_WITH_OLD_KEY, encoding="utf-8")
    factory, seen = make_factory()
    setup.setup_env(env_file, ask=asker(), client_factory=factory, replace=True)
    assert seen.models == ["gpt-5-nano"] and "OPENAI_MODEL=gpt-4o-mini" in env_file.read_text(encoding="utf-8")


@pytest.mark.parametrize("content", [None, "OTRA=1\n", "OPENAI_API_KEY=\n"])
def test_replace_without_a_key_in_the_file_saves_it_like_the_first_time(tmp_path, clean_env, content):
    env_file = tmp_path / ".env"
    if content is not None:
        env_file.write_text(content, encoding="utf-8")
    factory, _ = make_factory()
    message = setup.setup_env(env_file, ask=asker(), client_factory=factory, replace=True)
    assert message == "Clave guardada en .env y verificada con gpt-6-luna"
    assert env_file.read_text(encoding="utf-8").splitlines()[-2:] == [f"OPENAI_API_KEY={KEY}", "OPENAI_MODEL=gpt-6-luna"]


def test_api_failure_shows_code_and_param_but_never_the_free_text(tmp_path, clean_env):
    error = openai.BadRequestError(
        API_TEXT, response=httpx.Response(400, request=REQUEST),
        body={"message": API_TEXT, "code": "model_not_found", "param": "model", "type": "invalid_request_error"})
    factory, _ = make_factory(error)
    with pytest.raises(RuntimeError) as err:
        setup.setup_env(tmp_path / ".env", ask=asker(), client_factory=factory)
    assert str(err.value) == ("OpenAI respondió con un error (BadRequestError: model_not_found en model) y no se pudo "
                              "verificar la clave. No se guardó nada.")
    assert KEY not in str(err.value) and "texto de la API" not in str(err.value)
