"""Tests de score.py y del comando `score`. Cliente falso: nunca se llama a la API de OpenAI."""
import json
import os
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import openai
import pytest
import yaml

from compuapply import apply, cli, db, review, run as run_module, score, ui_logic

ROOT = Path(__file__).parent.parent
EXAMPLE = ROOT / "profile.example.yaml"
REQUEST = httpx.Request("POST", "https://api.openai.com/v1/responses")
COST_LUNA = (600 * 0.10 + 400 * 0.01 + 200 * 0.50) / 1_000_000  # 1000 entrada (400 cacheada) y 200 salida

PROFILE = {
    "variants": [
        {"id": "backend", "keywords": ["x"], "resume": {"experience_details": [{"position": "Dev", "company": "Acme"}]}},
        {"id": "data", "keywords": ["y"], "resume": {"projects": [{"name": "Tablero", "description": "Power BI"}]}},
    ],
    "exclude_companies": [],
    "exclude_title_words": [],
}


def payload(backend=85, data=40, best="backend", **over) -> dict:
    return {"best_variant": best, "scores": [{"variant_id": "backend", "score": backend},
                                              {"variant_id": "data", "score": data}],
            "reasons": ["Encaja con backend"], "missing": ["Inglés avanzado"], **over}


def response(data=None, *, text=None, status="completed", output=(), tokens=(1000, 400, 200)):
    return SimpleNamespace(
        output_text=json.dumps(payload() if data is None else data) if text is None else text,
        output=list(output), status=status,
        usage=SimpleNamespace(input_tokens=tokens[0], input_tokens_details=SimpleNamespace(cached_tokens=tokens[1]),
                              output_tokens=tokens[2]),
    )


def requested_ids(kwargs) -> list[str]:
    return kwargs["text"]["format"]["schema"]["properties"]["scores"]["items"]["properties"]["variant_id"]["enum"]


def narrowed(item, kwargs):
    """Cada pedido lleva un solo CV: una respuesta armada con las dos variantes se reduce a las pedidas (la mejor entre ellas)."""
    try:
        data = json.loads(item.output_text)
        ids = requested_ids(kwargs)
        scores = data["scores"]
        keep = [s for s in scores if s["variant_id"] in ids]
    except (AttributeError, TypeError, ValueError, KeyError):
        return item
    both = [s["variant_id"] for s in scores] == ["backend", "data"]
    if not keep or len(keep) == len(scores) or not both or data.get("best_variant") != max(scores, key=lambda s: s["score"])["variant_id"]:
        return item  # solo se reduce una respuesta válida de las dos variantes; las armadas a propósito para fallar quedan como están
    data = {**data, "scores": keep, "best_variant": keep[0]["variant_id"]}
    return SimpleNamespace(**{**vars(item), "output_text": json.dumps(data)})


class FakeClient:
    """Devuelve las respuestas en orden (la última se repite); una excepción se lanza."""

    def __init__(self, *responses, on_call=None):
        self.calls, self._responses, self._on_call = [], list(responses) or [response()], on_call
        self.responses = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        if self._on_call:
            self._on_call()
        item = self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]
        if isinstance(item, Exception):
            raise item
        return narrowed(item, kwargs)


def status_error(code: int) -> openai.APIStatusError:
    return openai.APIStatusError("clave sk-secreta-123 mala", response=httpx.Response(code, request=REQUEST), body=None)


def add_offer(conn, n: int, *, title=None, company="Empresa Falsa SA", description="Buscamos analista.",
              status="new") -> str:
    id16 = f"{n:016X}"
    db.upsert_offers(conn, [{"id16": id16, "data_id": f"{n:032X}", "title": title or f"Oferta {n}", "company": company,
                             "location": "Rosario", "url": f"https://ar.computrabajo.com/ofertas-de-trabajo/o-{n}"}], "x")
    if description:
        db.save_detail(conn, id16, {"description": description, "date_posted": "", "valid_through": "",
                                    "employment_type": "FULL_TIME"})
    if status != "new":
        db.mark_status(conn, id16, status)
    return id16


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / "t.db")
    yield connection
    connection.close()


def run(conn, client, tmp_path, *, model="gpt-6-luna", limit=20, max_cost=10.0, profile=PROFILE):
    return score.score_pending(conn, profile, client, model, limit, max_cost, stop_file=tmp_path / "STOP")


def statuses(conn) -> list[str]:
    return [r[0] for r in conn.execute("SELECT status FROM offers ORDER BY rowid")]


# ── filtros ──────────────────────────────────────────────────────────────────

def test_excluded_by_company_and_title_do_not_call_client(conn, tmp_path):
    add_offer(conn, 1, company="Acme  SA")
    add_offer(conn, 2, title="Ejecutivo de Ventas")
    add_offer(conn, 3, title="Prevenciones y entregas")  # "venta" no es palabra entera de "Prevenciones"
    profile = {**PROFILE, "exclude_companies": ["  acme sa "], "exclude_title_words": ["VENTAS", "venta"]}
    client = FakeClient()
    result = run(conn, client, tmp_path, profile=profile, limit=0)  # los filtros se aplican aunque no se puntúe
    assert client.calls == [] and result["excluded"] == 2
    assert statuses(conn) == ["excluded", "excluded", "new"]
    result = run(conn, client, tmp_path, profile=profile)
    assert result["excluded"] == 0 and result["scored"] == 1 and len(client.calls) == 1


def test_changing_filters_requeues_and_excludes_without_calling_client(conn, tmp_path):
    add_offer(conn, 1, company="Acme SA")
    add_offer(conn, 2, title="Ejecutivo de Ventas")
    add_offer(conn, 3)
    client = FakeClient()
    filtered = {**PROFILE, "exclude_companies": ["Acme SA"], "exclude_title_words": ["ventas"]}
    assert run(conn, client, tmp_path, profile=filtered, limit=0)["excluded"] == 2
    assert statuses(conn) == ["excluded", "excluded", "new"]
    # Se quita la empresa del filtro: su oferta vuelve a la cola. Ninguna llamada por eso (limit 0).
    only_title = {**PROFILE, "exclude_title_words": ["ventas"]}
    assert run(conn, client, tmp_path, profile=only_title, limit=0)["excluded"] == 0
    assert statuses(conn) == ["new", "excluded", "new"]
    # Se agregan empresas: las dos ofertas en cola pasan a excluded, también sin llamadas.
    other = {**PROFILE, "exclude_companies": ["Empresa Falsa SA", "Acme SA"], "exclude_title_words": ["ventas"]}
    assert run(conn, client, tmp_path, profile=other, limit=0)["excluded"] == 2
    assert statuses(conn) == ["excluded", "excluded", "excluded"]
    assert client.calls == []
    # Sin filtros, las tres vuelven y se puntúan.
    assert run(conn, client, tmp_path)["scored"] == 3 and len(client.calls) == 3


def test_requeued_excluded_offer_with_a_score_is_not_scored_twice(conn, tmp_path):
    add_offer(conn, 1, company="Acme SA")
    client = FakeClient()
    run(conn, client, tmp_path)  # puntuada
    run(conn, client, tmp_path, profile={**PROFILE, "exclude_companies": ["Acme SA"]})
    assert statuses(conn) == ["excluded"]
    run(conn, client, tmp_path)  # vuelve a 'new' pero ya tiene puntaje para este modelo
    assert statuses(conn) == ["new"] and len(client.calls) == 1


def test_retry_errors_requeues_score_error_only_with_the_flag(conn, tmp_path):
    add_offer(conn, 1)
    client = FakeClient(response(text="roto"), response())
    assert run(conn, client, tmp_path)["errors"] == 1
    assert statuses(conn) == ["score_error"]
    assert run(conn, client, tmp_path)["scored"] == 0 and len(client.calls) == 1  # sin el flag, no se toca
    result = score.score_pending(conn, PROFILE, client, "gpt-6-luna", 20, 10.0, stop_file=tmp_path / "STOP",
                                 retry_errors=True)
    assert result["scored"] == 1 and len(client.calls) == 2
    assert statuses(conn) == ["new"]


def test_blank_exclusion_entries_exclude_nothing(conn, tmp_path):
    add_offer(conn, 1, company="")
    profile = {**PROFILE, "exclude_companies": [""], "exclude_title_words": ["", "  "]}
    assert run(conn, FakeClient(), tmp_path, profile=profile)["excluded"] == 0


def test_only_new_offers_with_description_are_pending(conn, tmp_path):
    add_offer(conn, 1, description=None)
    add_offer(conn, 2, status="bad_url")
    add_offer(conn, 3, status="skipped")
    add_offer(conn, 4)
    client = FakeClient()
    assert run(conn, client, tmp_path)["scored"] == 1
    assert len(client.calls) == 1


# ── llamada ──────────────────────────────────────────────────────────────────

def test_request_arguments(conn, tmp_path):
    add_offer(conn, 1, description="x" * 7000)
    client = FakeClient()
    run(conn, client, tmp_path)
    [call] = client.calls
    assert call["model"] == "gpt-6-luna" and call["reasoning"] == {"effort": "low"} and call["max_output_tokens"] == 2000
    system, user = call["input"]
    assert (system["role"], user["role"]) == ("system", "user")
    assert system["content"].startswith(score.SYSTEM + "\n\n### CV backend\n")
    assert "### CV data" not in system["content"] and "Power BI" not in system["content"]  # un solo CV por pedido
    assert "Dev en Acme" in system["content"]
    assert "Título: Oferta 1" in user["content"] and "Empresa: Empresa Falsa SA" in user["content"]
    assert "Ubicación: Rosario" in user["content"] and "Tipo de empleo: FULL_TIME" in user["content"]
    assert "x" * 6000 in user["content"] and "x" * 6001 not in user["content"]
    fmt = call["text"]["format"]
    assert (fmt["type"], fmt["name"], fmt["strict"]) == ("json_schema", "puntaje", True)
    assert fmt["schema"]["properties"]["best_variant"]["enum"] == ["backend"]
    assert fmt["schema"]["properties"]["scores"]["items"]["properties"]["variant_id"]["enum"] == ["backend"]
    assert fmt["schema"]["additionalProperties"] is False and fmt["schema"]["required"] == [
        "best_variant", "scores", "reasons", "missing", "unmet_required"]
    assert fmt["schema"]["properties"]["unmet_required"] == {"type": "array", "items": {"type": "string"}}


def test_first_message_is_identical_between_offers(conn, tmp_path):
    add_offer(conn, 1)
    add_offer(conn, 2, description="Otra descripción")
    client = FakeClient()
    run(conn, client, tmp_path)
    first, second = (c["input"] for c in client.calls)
    assert first[0] == second[0]
    assert first[1] != second[1]


# ── guardado y costo ─────────────────────────────────────────────────────────

def test_valid_response_saves_row_with_exact_cost(conn, tmp_path):
    id16 = add_offer(conn, 1)
    client = FakeClient(response(payload(backend=85, data=40)))
    result = run(conn, client, tmp_path)
    assert (result["scored"], result["excluded"], result["errors"], result["stopped_by_cost"]) == (1, 0, 0, False)
    assert (result["input_tokens"], result["output_tokens"]) == (1000, 200)
    assert result["cost_usd"] == pytest.approx(COST_LUNA) == pytest.approx(0.000164)
    row = conn.execute("SELECT * FROM scores").fetchone()
    assert row[:8] == (id16, "gpt-6-luna", "v5", "backend", 85, '{"backend": 85}',
                       '["Encaja con backend"]', '["Inglés avanzado"]')
    assert row[8:11] == (1000, 400, 200) and row[11] == pytest.approx(0.000164) and row[12]
    assert statuses(conn) == ["new"]


def test_cost_formula_for_other_models():
    assert score.cost_usd("gpt-4o-mini", 1000, 400, 200) == pytest.approx((600 * 0.15 + 400 * 0.075 + 200 * 0.60) / 1e6)
    assert score.cost_usd("gpt-5-nano", 1000, 0, 0) == pytest.approx(1000 * 0.05 / 1e6)


def test_already_scored_is_not_scored_again_but_new_model_is(conn, tmp_path):
    add_offer(conn, 1)
    client = FakeClient()
    run(conn, client, tmp_path)
    assert run(conn, client, tmp_path)["scored"] == 0 and len(client.calls) == 1
    assert run(conn, client, tmp_path, model="gpt-4o-mini")["scored"] == 1 and len(client.calls) == 2
    assert conn.execute("SELECT COUNT(*) FROM scores").fetchone()[0] == 2


def test_unknown_model_fails_before_calling(conn, tmp_path):
    add_offer(conn, 1)
    client = FakeClient()
    with pytest.raises(RuntimeError, match="score.PRICES"):
        run(conn, client, tmp_path, model="gpt-inventado")
    assert client.calls == []


def test_limit_counts_api_calls(conn, tmp_path):
    for n in (1, 2, 3):
        add_offer(conn, n)
    client = FakeClient()
    assert run(conn, client, tmp_path, limit=2)["scored"] == 2
    assert len(client.calls) == 2


# ── respuestas inválidas ─────────────────────────────────────────────────────

BAD_RESPONSES = {
    "json roto": lambda: response(text="{no es json"),
    "no es un objeto": lambda: response(text="[1, 2]"),
    "score 101": lambda: response(payload(backend=101)),
    "score negativo": lambda: response(payload(backend=-1)),
    "score no entero": lambda: response(payload(backend=85.5)),
    "score booleano": lambda: response(payload(backend=True)),
    "variante desconocida": lambda: response({**payload(), "scores": [
        {"variant_id": "backend", "score": 85}, {"variant_id": "otra", "score": 40}]}),
    "falta la variante pedida": lambda: response({**payload(), "scores": []}),
    "variante repetida": lambda: response({**payload(), "scores": [
        {"variant_id": "backend", "score": 85}, {"variant_id": "backend", "score": 85}]}),
    "best_variant no es el mayor": lambda: response(payload(backend=85, data=40, best="data")),
    "razones que no son texto": lambda: response(payload(reasons=[1])),
    "refusal como ítem": lambda: response(output=[SimpleNamespace(type="refusal", refusal="no puedo")]),
    "refusal dentro del mensaje": lambda: response(output=[SimpleNamespace(
        type="message", content=[SimpleNamespace(type="refusal", refusal="no puedo")])]),
    "status incomplete": lambda: response(status="incomplete"),
}


@pytest.mark.parametrize("name", BAD_RESPONSES)
def test_invalid_response_marks_score_error_and_continues(conn, tmp_path, name):
    first, second = add_offer(conn, 1), add_offer(conn, 2)
    client = FakeClient(BAD_RESPONSES[name](), response())
    result = run(conn, client, tmp_path)
    assert (result["scored"], result["errors"]) == (1, 1)
    assert statuses(conn) == ["score_error", "new"]
    assert [r[0] for r in conn.execute("SELECT id16 FROM scores")] == [second]
    assert result["cost_usd"] == pytest.approx(2 * COST_LUNA)  # la respuesta mala también gastó tokens
    assert first != second


def test_three_invalid_responses_in_a_row_stop_the_run(conn, tmp_path):
    for n in (1, 2, 3, 4):
        add_offer(conn, n)
    client = FakeClient(response(text="roto"))
    with pytest.raises(RuntimeError, match="3 respuestas inválidas seguidas"):
        run(conn, client, tmp_path)
    assert len(client.calls) == 3
    assert statuses(conn) == ["score_error"] * 3 + ["new"]


def test_a_valid_response_resets_the_error_counter(conn, tmp_path):
    for n in range(1, 6):
        add_offer(conn, n)
    bad = response(text="roto")
    client = FakeClient(bad, bad, response(), bad, bad)
    result = run(conn, client, tmp_path)
    assert (result["scored"], result["errors"]) == (1, 4)


# ── errores de la API, tope de gasto y STOP ──────────────────────────────────

def test_api_status_error_stops_and_keeps_previous(conn, tmp_path):
    first = add_offer(conn, 1)
    add_offer(conn, 2)
    add_offer(conn, 3)
    client = FakeClient(response(), status_error(429))
    with pytest.raises(RuntimeError, match="429") as err:
        run(conn, client, tmp_path)
    assert "sk-secreta" not in str(err.value)  # no se cita el mensaje de la API
    assert len(client.calls) == 2
    assert [r[0] for r in conn.execute("SELECT id16 FROM scores")] == [first]
    assert statuses(conn) == ["new", "new", "new"]


def test_api_connection_error_stops(conn, tmp_path):
    add_offer(conn, 1)
    client = FakeClient(openai.APIConnectionError(request=REQUEST))
    with pytest.raises(RuntimeError, match="conectar"):
        run(conn, client, tmp_path)


def test_max_cost_stops_before_the_offer_that_would_exceed_it(conn, tmp_path):
    for n in (1, 2, 3):
        add_offer(conn, n)
    client = FakeClient()
    result = run(conn, client, tmp_path, max_cost=1.5 * COST_LUNA)  # después de la 2.ª se pasó
    assert (result["scored"], result["stopped_by_cost"]) == (2, True)
    assert len(client.calls) == 2


def test_max_cost_zero_calls_nothing(conn, tmp_path):
    add_offer(conn, 1)
    client = FakeClient()
    result = run(conn, client, tmp_path, max_cost=0)
    assert client.calls == [] and result["stopped_by_cost"] is True


def test_stop_file_halts_before_calling(conn, tmp_path):
    add_offer(conn, 1)
    (tmp_path / "STOP").write_text("")
    client = FakeClient()
    with pytest.raises(RuntimeError, match="STOP"):
        run(conn, client, tmp_path)
    assert client.calls == []


def test_stop_file_created_midrun_halts_next_offer(conn, tmp_path):
    for n in (1, 2):
        add_offer(conn, n)
    client = FakeClient(on_call=lambda: (tmp_path / "STOP").write_text(""))
    with pytest.raises(RuntimeError, match="STOP"):
        run(conn, client, tmp_path)
    assert len(client.calls) == 1
    assert conn.execute("SELECT COUNT(*) FROM scores").fetchone()[0] == 1


# ── .env y configuración ─────────────────────────────────────────────────────

def own_vars(env: dict) -> dict:
    """El entorno sin la variable con la que pytest anota el test en curso."""
    return {k: v for k, v in env.items() if k != "PYTEST_CURRENT_TEST"}


@pytest.fixture
def clean_env(monkeypatch):
    """Entorno aislado: load_env escribe en os.environ y no tiene que contaminar otros tests."""
    env = {}
    monkeypatch.setattr(os, "environ", env)
    return env


def test_load_env_parses_lines_and_does_not_override(tmp_path, clean_env):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# comentario\n\nA=uno\nB = \"dos\"\nC='tres'\nD=con=igual\nE=\nsin igual\nYA_DEFINIDA=del archivo\n",
        encoding="utf-8")
    clean_env["YA_DEFINIDA"] = "del entorno"
    score.load_env(env_file)
    assert own_vars(clean_env) == {"A": "uno", "B": "dos", "C": "tres", "D": "con=igual", "E": "",
                                   "YA_DEFINIDA": "del entorno"}


def test_load_env_without_file_is_a_noop(tmp_path, clean_env):
    score.load_env(tmp_path / "no-existe.env")
    assert own_vars(clean_env) == {}


def test_load_settings_requires_key(tmp_path, clean_env):
    with pytest.raises(RuntimeError, match=r"Falta OPENAI_API_KEY.*\.env"):
        score.load_settings(tmp_path / ".env")


def test_load_settings_reads_key_and_model(tmp_path, clean_env):
    env_file = tmp_path / ".env"
    env_file.write_text("OPENAI_API_KEY=sk-falsa\n", encoding="utf-8")
    assert score.load_settings(env_file) == "gpt-6-luna"
    clean_env["OPENAI_MODEL"] = "gpt-4o-mini"
    assert score.load_settings(env_file) == "gpt-4o-mini"


# ── CLI ──────────────────────────────────────────────────────────────────────

@pytest.fixture
def cli_env(tmp_path, monkeypatch, clean_env):
    """CLI aislado: cwd vacío (no se lee ningún .env real), clave falsa y OpenAI() reemplazado."""
    monkeypatch.chdir(tmp_path)
    clean_env["OPENAI_API_KEY"] = "sk-test-NUNCA-SE-IMPRIME"
    holder = SimpleNamespace(client=FakeClient())
    monkeypatch.setattr(cli.openai, "OpenAI", lambda: holder.client)
    return holder


def cli_args(tmp_path, *extra: str) -> list[str]:
    return ["score", "--profile", str(EXAMPLE), "--db", str(tmp_path / "t.db"), *extra]


def seed_cli_db(tmp_path, n: int) -> None:
    connection = db.connect(tmp_path / "t.db")
    for i in range(1, n + 1):
        add_offer(connection, i, title=f"Puesto {i}", company=f"Empresa {i}")
    connection.close()


def example_response(points: int):
    return response({"best_variant": "backend", "scores": [{"variant_id": "backend", "score": points}],
                     "reasons": [], "missing": []})


def test_cli_score_prints_summary_and_top_lines(tmp_path, cli_env, capsys):
    seed_cli_db(tmp_path, 7)
    cli_env.client = FakeClient(*(example_response(p) for p in (50, 90, 70, 60, 80, 55, 99)))
    assert cli.main(cli_args(tmp_path, "--limit", "7")) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert lines[0] == "Puntuadas: 7 · excluidas: 0 · con error: 0 · costo: US$ 0,0011 · tokens: entrada 7000 / salida 1400"
    assert lines[1:6] == ["99 · Puesto 7 · Empresa 7 · backend", "90 · Puesto 2 · Empresa 2 · backend",
                          "80 · Puesto 5 · Empresa 5 · backend", "70 · Puesto 3 · Empresa 3 · backend",
                          "60 · Puesto 4 · Empresa 4 · backend"]
    assert lines[6:] == ["Gasto del mes: US$ 0,0011 de US$ 2,00"]  # lo gastado este mes, con esta corrida, y el tope


def test_cli_score_reports_cost_stop(tmp_path, cli_env, capsys):
    seed_cli_db(tmp_path, 3)
    cli_env.client = FakeClient(example_response(80))
    assert cli.main(cli_args(tmp_path, "--max-cost", "0.0001")) == 0
    out = capsys.readouterr().out
    assert "Puntuadas: 1" in out and "Se llegó al tope de gasto (--max-cost US$ 0,00)" in out


def spend_this_month(tmp_path, cost: float, *, months_ago: int = 0, n: int = 90) -> None:
    """Deja en la base un puntaje viejo de la oferta `n` que costó `cost` (de este mes, o de hace `months_ago` meses)."""
    connection = db.connect(tmp_path / "t.db")
    id16 = add_offer(connection, n, title=f"Vieja {n}", description=None)  # sin descripción: no vuelve a la cola
    db.save_score(connection, id16, "gpt-6-luna", "v0", {"best_variant": "backend", "score": 70, "all_scores": {"backend": 70},
                                                          "reasons": [], "missing": []}, (1, 0, 1), cost)
    if months_ago:
        month_start = datetime.now(timezone.utc).replace(day=1)
        for _ in range(months_ago):
            month_start = (month_start - timedelta(days=1)).replace(day=1)
        connection.execute("UPDATE scores SET scored_at = ?", (month_start.replace(day=15).isoformat(),))
        connection.commit()
    connection.close()


def profile_with_run(tmp_path: Path, run_section: dict) -> Path:
    """El perfil de ejemplo (rutas absolutas a sus archivos) con la sección `run` pedida."""
    data = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    for variant in data["variants"]:
        variant["resume"], variant["pdf"] = str(ROOT / variant["resume"]), str(ROOT / variant["pdf"])
    data["run"] = run_section
    path = tmp_path / "profile.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return path


def test_cli_score_with_the_monthly_cap_reached_does_not_call_the_api(tmp_path, cli_env, capsys):
    seed_cli_db(tmp_path, 3)
    spend_this_month(tmp_path, 2.5)
    assert cli.main(cli_args(tmp_path)) == 0
    captured = capsys.readouterr()
    assert captured.out == "Tope mensual alcanzado: US$ 2,5000 de US$ 2,00\n" and captured.err == ""
    assert cli_env.client.calls == []
    conn = db.connect(tmp_path / "t.db")
    assert conn.execute("SELECT COUNT(*) FROM scores").fetchone()[0] == 1  # nada nuevo
    conn.close()


def test_cli_score_with_exactly_the_cap_spent_does_not_call_the_api(tmp_path, cli_env, capsys):
    seed_cli_db(tmp_path, 1)
    spend_this_month(tmp_path, 2.0)
    assert cli.main(cli_args(tmp_path)) == 0
    assert capsys.readouterr().out == "Tope mensual alcanzado: US$ 2,0000 de US$ 2,00\n" and cli_env.client.calls == []


def test_cli_score_does_not_count_the_spend_of_other_months(tmp_path, cli_env, capsys):
    seed_cli_db(tmp_path, 1)
    spend_this_month(tmp_path, 50.0, months_ago=1)
    cli_env.client = FakeClient(example_response(80))
    assert cli.main(cli_args(tmp_path)) == 0
    out = capsys.readouterr().out
    assert len(cli_env.client.calls) == 1 and "Tope mensual" not in out and out.endswith("Gasto del mes: US$ 0,0002 de US$ 2,00\n")


def test_cli_score_uses_what_is_left_of_the_month_as_the_budget(tmp_path, cli_env, capsys):
    seed_cli_db(tmp_path, 6)
    spend_this_month(tmp_path, 2.0 - 3.5 * COST_LUNA)  # quedan 3,5 llamadas; --max-cost 0.50 no es el que corta
    cli_env.client = FakeClient(example_response(80))
    assert cli.main(cli_args(tmp_path)) == 0
    out = capsys.readouterr().out
    assert len(cli_env.client.calls) == 4  # se corta antes de la llamada que sigue a haber gastado 3,5 (la 4.ª cruza el tope)
    assert f"Se llegó al tope de gasto (lo que quedaba del tope mensual, {score.money(3.5 * COST_LUNA)})" in out
    assert out.endswith(f"Gasto del mes: {score.money(2.0 - 3.5 * COST_LUNA + 4 * COST_LUNA)} de US$ 2,00\n")


def test_cli_score_max_cost_below_what_is_left_still_cuts_with_its_own_message(tmp_path, cli_env, capsys):
    seed_cli_db(tmp_path, 3)
    spend_this_month(tmp_path, 1.0)
    cli_env.client = FakeClient(example_response(80))
    assert cli.main(cli_args(tmp_path, "--max-cost", "0.0001")) == 0
    out = capsys.readouterr().out
    assert "Se llegó al tope de gasto (--max-cost US$ 0,00)" in out and "tope mensual," not in out
    assert "Gasto del mes: US$ 1,0002 de US$ 2,00" in out


def test_cli_score_reads_the_monthly_cap_from_the_profile(tmp_path, cli_env, capsys):
    seed_cli_db(tmp_path, 2)
    spend_this_month(tmp_path, 1.2)
    profile_path = profile_with_run(tmp_path, {"max_cost_month": 1.0})
    args = ["score", "--profile", str(profile_path), "--db", str(tmp_path / "t.db")]
    assert cli.main(args) == 0
    assert capsys.readouterr().out == "Tope mensual alcanzado: US$ 1,2000 de US$ 1,00\n" and cli_env.client.calls == []


@pytest.mark.parametrize("cap", [0, -1, "2", True])
def test_cli_score_invalid_monthly_cap_exits_1_with_a_clear_error_and_no_call(tmp_path, cli_env, capsys, cap):
    seed_cli_db(tmp_path, 1)
    profile_path = profile_with_run(tmp_path, {"max_cost_month": cap})
    assert cli.main(["score", "--profile", str(profile_path), "--db", str(tmp_path / "t.db")]) == 1
    captured = capsys.readouterr()
    assert "'run.max_cost_month'" in captured.err and "número mayor que 0" in captured.err and captured.out == ""
    assert cli_env.client.calls == []


def test_month_spent_counts_only_the_utc_month_of_each_score(conn):
    # (fecha UTC del puntaje, costo): cada borde de mes cae de un lado u otro.
    rows = [("2026-10-31T23:59:59+00:00", 0.1), ("2026-11-01T00:00:00+00:00", 0.2), ("2026-11-15T12:00:00.123456+00:00", 0.4),
            ("2026-11-30T23:59:59.999999+00:00", 0.8), ("2026-12-01T00:00:00+00:00", 1.6), ("2025-11-15T12:00:00+00:00", 3.2)]
    for n, (stamp, cost) in enumerate(rows, 1):
        id16 = add_offer(conn, n, description=None)
        db.save_score(conn, id16, "gpt-6-luna", "v5", {"best_variant": "a", "score": 70, "all_scores": {"a": 70},
                                                       "reasons": [], "missing": []}, (1, 0, 1), cost)
        conn.execute("UPDATE scores SET scored_at = ? WHERE id16 = ?", (stamp, id16))
    conn.commit()
    assert score.month_spent(conn, date(2026, 11, 1)) == pytest.approx(0.2 + 0.4 + 0.8)
    assert score.month_spent(conn, date(2026, 11, 30)) == pytest.approx(1.4)  # el día no importa: cuenta el mes
    assert score.month_spent(conn, date(2026, 10, 31)) == pytest.approx(0.1)
    assert score.month_spent(conn, date(2026, 12, 1)) == pytest.approx(1.6)
    assert score.month_spent(conn, date(2025, 11, 1)) == pytest.approx(3.2)  # el mismo mes de otro año es otro mes
    assert score.month_spent(conn, date(2027, 1, 1)) == 0.0


def test_month_spent_of_an_empty_base_is_a_float_zero_and_ignores_missing_costs(conn):
    assert score.month_spent(conn, date(2026, 11, 2)) == 0.0 and type(score.month_spent(conn, date(2026, 11, 2))) is float
    id16 = add_offer(conn, 1, description=None)
    db.save_score(conn, id16, "gpt-6-luna", "v5", {"best_variant": "a", "score": 70, "all_scores": {"a": 70},
                                                   "reasons": [], "missing": []}, (1, 0, 1), 0.5)
    conn.execute("UPDATE scores SET cost_usd = NULL")
    conn.commit()
    assert score.month_spent(conn, datetime.now(timezone.utc).date()) == 0.0


def test_amounts_shows_what_was_spent_with_four_decimals_and_the_cap_with_two():
    assert score.amounts(0.00123, 2.0) == "US$ 0,0012 de US$ 2,00"
    assert score.amounts(2.5, 1.5) == "US$ 2,5000 de US$ 1,50"


def test_cli_score_api_error_exits_1_and_reports_saved(tmp_path, cli_env, capsys):
    seed_cli_db(tmp_path, 3)
    cli_env.client = FakeClient(example_response(80), status_error(401))
    assert cli.main(cli_args(tmp_path)) == 1
    captured = capsys.readouterr()
    assert "401" in captured.err and "Antes del error: puntuadas 1" in captured.err
    assert captured.out == ""
    assert "sk-test" not in captured.err + captured.out and "sk-secreta" not in captured.err


def test_cli_score_retry_errors_flag(tmp_path, cli_env, capsys):
    seed_cli_db(tmp_path, 1)
    cli_env.client = FakeClient(response(text="roto"), example_response(80))
    assert cli.main(cli_args(tmp_path)) == 0
    assert "Puntuadas: 0 · excluidas: 0 · con error: 1" in capsys.readouterr().out
    assert cli.main(cli_args(tmp_path)) == 0
    assert "Puntuadas: 0 · excluidas: 0 · con error: 0" in capsys.readouterr().out
    assert cli.main(cli_args(tmp_path, "--retry-errors")) == 0
    assert "Puntuadas: 1 · excluidas: 0 · con error: 0" in capsys.readouterr().out


def test_cli_score_without_key_exits_1(tmp_path, cli_env, clean_env, capsys):
    del clean_env["OPENAI_API_KEY"]
    assert cli.main(cli_args(tmp_path)) == 1
    assert "Falta OPENAI_API_KEY" in capsys.readouterr().err


def test_cli_score_missing_profile_exits_1(tmp_path, cli_env, capsys):
    assert cli.main(["score", "--profile", str(tmp_path / "nada.yaml"), "--db", str(tmp_path / "t.db")]) == 1
    assert "No existe el perfil" in capsys.readouterr().err


# ── prueba de puntaje (probe) ────────────────────────────────────────────────

def count_rows(conn) -> tuple[int, int]:
    return tuple(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in ("offers", "scores"))


def test_probe_offer_is_a_fixed_invented_offer_with_six_to_eight_requirements():
    offer = score.PROBE_OFFER
    assert offer["title"] == "Desarrollador/a Frontend React" and offer["company"] == "Empresa de ejemplo"
    assert "Buenos Aires" in offer["location"] and "íbrido" in offer["employment_type"]
    assert 6 <= sum(1 for line in offer["description"].splitlines() if line.startswith("- ")) <= 8
    assert "@" not in str(offer)


def test_probe_returns_the_score_the_tokens_and_the_exact_cost(conn):
    add_offer(conn, 1)
    before = count_rows(conn)
    client = FakeClient(response(payload(backend=82, data=35, reasons=["Sabe React", "Sabe Git"])))
    info = score.probe(client, "gpt-6-luna", PROFILE)
    assert info == {"best_variant": "backend", "score": 82, "all_scores": {"backend": 82, "data": 35},
                    "reasons": ["Sabe React", "Sabe Git"], "input_tokens": 2000, "cached_tokens": 800, "output_tokens": 400,
                    "cost_usd": pytest.approx(2 * COST_LUNA)}
    assert len(client.calls) == 2  # un pedido por variante, cada uno con su CV
    assert "### CV backend" in client.calls[0]["input"][0]["content"] and "### CV data" not in client.calls[0]["input"][0]["content"]
    assert "### CV data" in client.calls[1]["input"][0]["content"] and "### CV backend" not in client.calls[1]["input"][0]["content"]
    assert count_rows(conn) == before  # no escribe en la base (ni siquiera recibe la conexión)


def test_probe_builds_the_same_request_as_score_pending_except_for_the_offer_text(conn, tmp_path):
    add_offer(conn, 1, title="Otro puesto", description="Otra descripción.")
    pending = FakeClient()
    run(conn, pending, tmp_path)
    probing = FakeClient()
    score.probe(probing, "gpt-6-luna", PROFILE)
    real, test = pending.calls[0], probing.calls[0]
    assert real.keys() == test.keys()
    assert {k: v for k, v in real.items() if k != "input"} == {k: v for k, v in test.items() if k != "input"}
    assert real["input"][0] == test["input"][0]  # las instrucciones y los CV son idénticos
    assert real["input"][1] != test["input"][1] and test["input"][1]["role"] == "user"
    assert "Desarrollador/a Frontend React" in test["input"][1]["content"] and "Empresa de ejemplo" in test["input"][1]["content"]
    assert test["reasoning"] == {"effort": "low"} and test["text"]["format"]["strict"] is True


@pytest.mark.parametrize("bad, message", [
    (response(text="esto no es json"), "formato JSON"),
    (response(payload(best="data")), "formato JSON"),  # el mejor no es el de mayor puntaje
    (response(output=[SimpleNamespace(type="message", content=[SimpleNamespace(type="refusal")])]), "rechazó"),
    (response(status="incomplete"), "'incomplete'"),
])
def test_probe_invalid_responses_are_a_clear_error(bad, message):
    with pytest.raises(RuntimeError) as err:
        score.probe(FakeClient(bad), "gpt-6-luna", PROFILE)
    assert message in str(err.value) and str(err.value).startswith("La prueba de puntaje falló con gpt-6-luna")


@pytest.mark.parametrize("error, text", [
    (status_error(401), "respondió 401 (APIStatusError)"),
    (status_error(500), "respondió 500"),
    (openai.APIConnectionError(request=REQUEST), "No se pudo conectar con la API de OpenAI (APIConnectionError)"),
])
def test_probe_api_errors_show_the_code_and_class_but_never_the_text(error, text):
    with pytest.raises(RuntimeError) as err:
        score.probe(FakeClient(error), "gpt-6-luna", PROFILE)
    assert text in str(err.value) and "sk-secreta" not in str(err.value) and "Se frena la corrida" not in str(err.value)


def test_probe_with_a_model_without_price_does_not_call_the_api():
    client = FakeClient()
    with pytest.raises(RuntimeError, match="No tengo el precio del modelo 'gpt-x'"):
        score.probe(client, "gpt-x", PROFILE)
    assert client.calls == []


def test_cli_score_probe_prints_one_ok_line_and_writes_no_database(tmp_path, cli_env, capsys):
    cli_env.client = FakeClient(example_response(77))
    assert cli.main(cli_args(tmp_path, "--probe")) == 0
    captured = capsys.readouterr()
    assert captured.out == ("Prueba OK con gpt-6-luna: puntaje 77 para backend · tokens: entrada 1000 / salida 200 "
                            "· costo: US$ 0,0002\n") and captured.err == ""
    assert len(cli_env.client.calls) == 1 and not (tmp_path / "t.db").exists() and not (tmp_path / "data").exists()
    assert "sk-test" not in captured.out


def test_cli_score_probe_ignores_limit_and_errors_exit_1(tmp_path, cli_env, capsys):
    cli_env.client = FakeClient(status_error(401))
    assert cli.main(cli_args(tmp_path, "--probe", "--limit", "5")) == 1
    captured = capsys.readouterr()
    assert "Error: La API de OpenAI respondió 401" in captured.err and captured.out == ""
    assert "sk-secreta" not in captured.err and not (tmp_path / "t.db").exists()


def test_cli_score_probe_invalid_answer_exits_1(tmp_path, cli_env, capsys):
    cli_env.client = FakeClient(response(text="{}"))
    assert cli.main(cli_args(tmp_path, "--probe")) == 1
    assert "La prueba de puntaje falló con gpt-6-luna" in capsys.readouterr().err


def test_cli_score_probe_without_key_exits_1(tmp_path, cli_env, clean_env, capsys):
    del clean_env["OPENAI_API_KEY"]
    assert cli.main(cli_args(tmp_path, "--probe")) == 1
    assert "Falta OPENAI_API_KEY" in capsys.readouterr().err and cli_env.client.calls == []


# ── TAREA-021: modelos sin razonamiento, errores de la API y montos ─────────────────────────

def api_failure(cls, status: int, **body):
    """Error de la API como llega de verdad: el texto libre trae parte de una clave; `body` son los campos."""
    return cls("Incorrect API key provided: sk-test-SECRETO", response=httpx.Response(status, request=REQUEST),
               body={"message": "Incorrect API key provided: sk-test-SECRETO", **body})


@pytest.mark.parametrize("model, reasoning", [
    ("gpt-4o-mini", None), ("gpt-6-luna", {"effort": "low"}), ("gpt-5-nano", {"effort": "low"})])
def test_reasoning_is_sent_only_to_reasoning_models(conn, tmp_path, model, reasoning):
    add_offer(conn, 1)
    pending, probing = FakeClient(), FakeClient()
    score.score_pending(conn, PROFILE, pending, model, 1, 1.0, stop_file=tmp_path / "STOP")
    score.probe(probing, model, PROFILE)
    for call in (pending.calls[0], probing.calls[0]):
        assert call.get("reasoning") == reasoning and ("reasoning" in call) == bool(reasoning)
    assert pending.calls[0].keys() == probing.calls[0].keys()  # el mismo pedido, con o sin razonamiento
    assert pending.calls[0]["text"] == probing.calls[0]["text"] and pending.calls[0]["input"][0] == probing.calls[0]["input"][0]


def test_reasoning_models_are_the_documented_ones_and_all_have_a_price():
    assert score.REASONING_MODELS == {"gpt-6-luna", "gpt-5-nano"} and "gpt-4o-mini" in score.PRICES


def test_a_bad_request_shows_code_and_param_but_never_the_free_text(conn, tmp_path):
    error = api_failure(openai.BadRequestError, 400, code="unsupported_parameter", param="reasoning.effort",
                        type="invalid_request_error")
    with pytest.raises(RuntimeError) as err:
        score.probe(FakeClient(error), "gpt-4o-mini", PROFILE)
    assert str(err.value) == "La API de OpenAI respondió 400 (BadRequestError): unsupported_parameter en reasoning.effort."
    add_offer(conn, 1)
    with pytest.raises(RuntimeError) as err:
        score.score_pending(conn, PROFILE, FakeClient(error), "gpt-4o-mini", 1, 1.0, stop_file=tmp_path / "STOP")
    assert str(err.value).startswith("La API de OpenAI respondió 400 (BadRequestError): unsupported_parameter en reasoning.effort.")
    assert str(err.value).endswith("Se frena la corrida; lo ya puntuado quedó guardado.")


@pytest.mark.parametrize("body, detail", [
    ({"code": "invalid_api_key", "type": "invalid_request_error"}, ": invalid_api_key."),  # el type solo si no hay code
    ({"type": "invalid_request_error", "param": "input"}, ": invalid_request_error en input."),
    ({"code": 7, "param": 3}, "."),  # campos que no son texto no se muestran
    ({}, "."),
])
def test_api_error_detail_uses_only_structured_fields(body, detail):
    error = api_failure(openai.BadRequestError, 400, **{"code": None, "type": None, "param": None, **body})
    with pytest.raises(RuntimeError) as err:
        score.probe(FakeClient(error), "gpt-6-luna", PROFILE)
    assert str(err.value) == f"La API de OpenAI respondió 400 (BadRequestError){detail}"


def test_an_authentication_error_never_shows_the_text_of_the_message_in_any_path(conn, tmp_path):
    error = api_failure(openai.AuthenticationError, 401, code="invalid_api_key")
    add_offer(conn, 1)
    messages = []
    for call in (lambda: score.probe(FakeClient(error), "gpt-6-luna", PROFILE),
                 lambda: score.score_pending(conn, PROFILE, FakeClient(error), "gpt-6-luna", 1, 1.0, stop_file=tmp_path / "STOP")):
        with pytest.raises(RuntimeError) as err:
            call()
        messages.append(str(err.value))
    assert all("invalid_api_key" in m and "sk-test-SECRETO" not in m and "Incorrect API key" not in m for m in messages)


def test_money_uses_the_decimal_comma():
    assert score.money(0.0004) == "US$ 0,0004" and score.money(2, 2) == "US$ 2,00"
    assert score.money(0) == "US$ 0,0000" and score.money(1.23456) == "US$ 1,2346"
    assert score.amounts(0.5, 2) == "US$ 0,5000 de US$ 2,00"


def test_no_amount_of_the_cli_has_a_decimal_point(tmp_path, cli_env, capsys):
    cli_env.client = FakeClient(example_response(80))
    assert cli.main(cli_args(tmp_path, "--probe")) == 0
    assert re.search(r"US\$ \d+\.\d", capsys.readouterr().out) is None


# ── TAREA-022: ofertas duplicadas y puntaje estable ───────────────────────────────────────

def add_copy(conn, n: int, *, title="Analista de datos", company="Acme SA", description="Misma descripción.") -> str:
    return add_offer(conn, n, title=title, company=company, description=description)


def dup_state(conn) -> list[tuple]:
    return conn.execute("SELECT id16, status, dup_of FROM offers ORDER BY rowid").fetchall()


def test_nine_equal_offers_leave_the_oldest_new_and_eight_duplicates(conn):
    companies = ["Acme SA", "ACME SA", "  acme   sa ", "Acme  SA"] * 3
    ids = [add_copy(conn, n, company=companies[n - 1]) for n in range(1, 10)]
    assert score.refresh_duplicates(conn) == 8
    assert dup_state(conn) == [(ids[0], "new", None), *[(i, "duplicate", ids[0]) for i in ids[1:]]]
    before = dup_state(conn)
    assert score.refresh_duplicates(conn) == 0 and dup_state(conn) == before  # correr de nuevo no cambia nada


def test_same_company_and_title_with_another_description_or_without_one_are_not_duplicates(conn):
    add_copy(conn, 1)
    add_copy(conn, 2, description="Otra descripción.")
    add_copy(conn, 3, title="Otro título")
    add_copy(conn, 4, company="Otra SA")
    add_offer(conn, 5, title="Analista de datos", company="Acme SA", description=None)
    assert score.refresh_duplicates(conn) == 0 and statuses(conn) == ["new"] * 5


def test_the_tie_on_first_seen_is_broken_by_rowid(conn):
    first, second = add_copy(conn, 1), add_copy(conn, 2)
    conn.execute("UPDATE offers SET first_seen = '2026-01-01T00:00:00+00:00'")
    assert score.refresh_duplicates(conn) == 1 and dup_state(conn) == [(first, "new", None), (second, "duplicate", first)]


def test_an_older_first_seen_wins_over_the_rowid(conn):
    early, late = add_copy(conn, 1), add_copy(conn, 2)
    conn.execute("UPDATE offers SET first_seen = '2026-12-01T00:00:00+00:00' WHERE id16 = ?", (early,))
    score.refresh_duplicates(conn)
    assert dup_state(conn) == [(early, "duplicate", late), (late, "new", None)]


def test_a_duplicate_goes_back_to_new_when_its_original_changes_or_disappears(conn):
    a, b, c = add_copy(conn, 1), add_copy(conn, 2), add_copy(conn, 3)
    score.refresh_duplicates(conn)
    db.save_detail(conn, a, {"description": "Cambió.", "date_posted": "", "valid_through": "", "employment_type": ""})
    assert score.refresh_duplicates(conn) == 0
    assert dup_state(conn) == [(a, "new", None), (b, "new", None), (c, "duplicate", b)]  # b pasa a ser la original de c
    conn.execute("DELETE FROM offers WHERE id16 = ?", (b,))
    score.refresh_duplicates(conn)
    assert dup_state(conn) == [(a, "new", None), (c, "new", None)]


def test_refresh_duplicates_only_touches_new_offers(conn):
    a, b, c = add_copy(conn, 1), add_copy(conn, 2), add_copy(conn, 3)
    db.mark_status(conn, b, "excluded")
    db.mark_status(conn, c, "applied")
    assert score.refresh_duplicates(conn) == 0 and dup_state(conn) == [(a, "new", None), (b, "excluded", None), (c, "applied", None)]


def test_score_pending_with_three_equal_offers_calls_the_client_once(conn, tmp_path):
    first = add_copy(conn, 1)
    add_copy(conn, 2)
    add_copy(conn, 3)
    client = FakeClient()
    totals = run(conn, client, tmp_path)
    assert len(client.calls) == 1 and (totals["scored"], totals["duplicates"]) == (1, 2)
    assert [r[0] for r in conn.execute("SELECT id16 FROM scores")] == [first]
    assert run(conn, client, tmp_path)["duplicates"] == 0 and len(client.calls) == 1


def test_duplicates_are_not_in_the_plan_nor_in_calibrar_nor_in_results(conn):
    first, second, third = add_copy(conn, 1), add_copy(conn, 2), add_copy(conn, 3)
    for id16 in (first, second, third):  # puntuadas antes de saberse repetidas
        db.save_score(conn, id16, "gpt-6-luna", score.PROMPT_VERSION,
                      {"best_variant": "backend", "score": 90, "all_scores": {"backend": 90}, "reasons": [], "missing": []},
                      (1, 0, 1), 0.0)
    score.refresh_duplicates(conn)
    profile = {"variants": PROFILE["variants"], "apply": {"threshold": 75, "daily_cap": 10}}
    plan = apply.plan(conn, profile, "gpt-6-luna", date(2026, 11, 2))
    assert [item["id16"] for item in plan["send"]] == [first] and plan["skip"] == []
    assert [c["id16"] for c in review.label_candidates(conn, 10)] == [first]
    assert [r["id16"] for r in ui_logic.results(conn, "gpt-6-luna")] == [first]


@pytest.mark.parametrize("old_version", ["v1", "v2", "v3", "v4"])
def test_prompt_version_is_v5_and_older_scores_are_neither_reused_nor_planned(conn, tmp_path, old_version):
    assert score.PROMPT_VERSION == "v5"
    old = add_copy(conn, 1)
    db.save_score(conn, old, "gpt-6-luna", old_version,
                  {"best_variant": "backend", "score": 95, "all_scores": {"backend": 95}, "reasons": [], "missing": []},
                  (1, 0, 1), 0.0)
    profile = {"variants": PROFILE["variants"], "apply": {"threshold": 75, "daily_cap": 10}}
    assert apply.plan(conn, profile, "gpt-6-luna", date(2026, 11, 2))["send"] == []
    client = FakeClient()
    assert run(conn, client, tmp_path)["scored"] == 1 and len(client.calls) == 1  # vuelve a la cola


@pytest.mark.parametrize("model, expected", [
    ("gpt-4o-mini", {"temperature": 0}),
    ("gpt-6-luna", {"reasoning": {"effort": "low"}}),
    ("gpt-5-nano", {"reasoning": {"effort": "low"}}),
])
def test_temperature_zero_goes_only_to_models_without_reasoning(conn, tmp_path, model, expected):
    add_offer(conn, 1)
    pending, probing = FakeClient(), FakeClient()
    run(conn, pending, tmp_path, model=model)
    score.probe(probing, model, PROFILE)
    for call in (pending.calls[0], probing.calls[0]):
        assert {k: call[k] for k in ("temperature", "reasoning") if k in call} == expected
    assert pending.calls[0].keys() == probing.calls[0].keys()


def test_cli_score_and_run_report_say_duplicadas_only_when_there_are(tmp_path, cli_env, capsys):
    connection = db.connect(tmp_path / "t.db")
    for n in (1, 2, 3):
        add_copy(connection, n)
    connection.close()
    cli_env.client = FakeClient(example_response(80))
    assert cli.main(cli_args(tmp_path)) == 0
    assert capsys.readouterr().out.splitlines()[0].startswith("Puntuadas: 1 · excluidas: 0 · duplicadas: 2 · con error: 0")
    assert cli.main(cli_args(tmp_path)) == 0
    assert "duplicadas" not in capsys.readouterr().out


def test_run_summary_and_report_add_duplicadas_only_when_there_are():
    totals = {"scored": 1, "excluded": 0, "duplicates": 2, "errors": 0, "cost_usd": 0.0, "input_tokens": 1, "output_tokens": 1,
              "stopped_by_cost": False}
    base = {"scan": [], "details": {"ran": False, "saved": 0, "bad_url": 0, "no_detail": 0}, "score_requested": True,
            "month": None, "plan": None, "stopped": None}
    assert "excluidas: 0 · duplicadas: 2 · con error: 0" in run_module.summary_lines({**base, "score": totals})[-1]
    assert "duplicadas" not in run_module.summary_lines({**base, "score": {**totals, "duplicates": 0}})[-1]


# ── TAREA-027: excluyentes que el CV no cumple ───────────────────────────────

def stored_unmet(conn) -> list[tuple]:
    return conn.execute("SELECT score, unmet_required FROM scores ORDER BY rowid").fetchall()


def test_unmet_required_is_saved_next_to_an_unchanged_score(conn, tmp_path):
    add_offer(conn, 1)
    add_offer(conn, 2, title="Otra", company="Otra SA", description="Otra oferta.")
    client = FakeClient(response(payload(backend=70, data=40, unmet_required=["2 años de experiencia web", "Node.js"])),
                        response(payload(backend=70, data=40, unmet_required=[])))
    assert run(conn, client, tmp_path)["scored"] == 2
    assert stored_unmet(conn) == [(70, '["2 años de experiencia web", "Node.js"]'), (70, "[]")]  # el puntaje es el del modelo


def test_a_response_without_the_list_is_read_as_empty_and_a_bad_list_is_invalid(conn, tmp_path):
    add_offer(conn, 1)
    data = payload()
    assert "unmet_required" not in data
    assert run(conn, FakeClient(response(data)), tmp_path)["scored"] == 1 and stored_unmet(conn) == [(85, "[]")]
    for bad in ("Node.js", [1], None):
        other = SimpleNamespace(**vars(response(payload(unmet_required=bad))))
        assert score._validate(other, ["backend", "data"]) is None


def test_the_system_prompt_explains_what_counts_as_required_and_probe_asks_the_same(conn, tmp_path):
    for phrase in ("excluyentes, obligatorios o «requisito indispensable»", "lista vacía", "no cambia el puntaje"):
        assert phrase in score.SYSTEM
    pending, probing = FakeClient(), FakeClient()
    add_offer(conn, 1)
    run(conn, pending, tmp_path)
    info = score.probe(probing, "gpt-6-luna", PROFILE)
    assert pending.calls[0]["text"] == probing.calls[0]["text"] and "unmet_required" in probing.calls[0]["text"]["format"]["schema"]["required"]
    assert info["score"] == 85


# ── TAREA-033: un pedido por variante candidata ──────────────────────────────

def cv_of(call) -> str:
    return call["input"][0]["content"]


def both_responses(backend_points, data_points):
    """Las dos respuestas de una oferta con las dos variantes candidatas (primero backend, después data)."""
    def one(variant, points):
        return response({"best_variant": variant, "scores": [{"variant_id": variant, "score": points}],
                         "reasons": [f"Razón de {variant}"], "missing": [f"Falta de {variant}"],
                         "unmet_required": [f"Excluyente de {variant}"]})
    return one("backend", backend_points), one("data", data_points)


def set_keyword(conn, id16, keyword):
    conn.execute("UPDATE offers SET keyword = ? WHERE id16 = ?", (keyword, id16))
    conn.commit()
    return id16


def test_an_offer_found_by_a_keyword_of_one_variant_is_scored_with_only_that_cv(conn, tmp_path):
    add_offer(conn, 1)
    set_keyword(conn, f"{1:016X}", "y")  # la keyword de data
    client = FakeClient(both_responses(0, 77)[1])
    result = run(conn, client, tmp_path)
    assert result["scored"] == 1 and len(client.calls) == 1
    assert "### CV data" in cv_of(client.calls[0]) and "### CV backend" not in cv_of(client.calls[0])
    row = conn.execute("SELECT best_variant, score, all_scores FROM scores").fetchone()
    assert row == ("data", 77, '{"data": 77}')


@pytest.mark.parametrize("keyword", ["X", " x ", "Programación"])
def test_the_keyword_is_compared_without_case_accents_or_extra_spaces(conn, tmp_path, keyword):
    add_offer(conn, 1)
    set_keyword(conn, f"{1:016X}", keyword)
    profile = {**PROFILE, "variants": [{**PROFILE["variants"][0], "keywords": ["programacion", "x"]}, PROFILE["variants"][1]]}
    client = FakeClient()
    run(conn, client, tmp_path, profile=profile)
    assert len(client.calls) == 1 and "### CV backend" in cv_of(client.calls[0])


@pytest.mark.parametrize("keyword", ["ya no está en el perfil", "", None])
def test_with_a_keyword_no_variant_has_every_variant_is_asked_one_request_each_with_one_cv(conn, tmp_path, keyword):
    add_offer(conn, 1)
    set_keyword(conn, f"{1:016X}", keyword)
    client = FakeClient(*both_responses(40, 85))
    result = run(conn, client, tmp_path)
    assert result["scored"] == 1 and len(client.calls) == 2
    assert "### CV backend" in cv_of(client.calls[0]) and "### CV data" not in cv_of(client.calls[0])
    assert "### CV data" in cv_of(client.calls[1]) and "### CV backend" not in cv_of(client.calls[1])
    assert client.calls[0]["input"][1] == client.calls[1]["input"][1]  # la misma oferta
    row = conn.execute("SELECT best_variant, score, all_scores, reasons, missing, unmet_required, input_tokens, cached_tokens, "
                       "output_tokens, cost_usd FROM scores").fetchone()
    assert row[:6] == ("data", 85, '{"backend": 40, "data": 85}', '["Razón de data"]', '["Falta de data"]', '["Excluyente de data"]')
    assert row[6:9] == (2000, 800, 400) and row[9] == pytest.approx(2 * COST_LUNA)  # suma los dos pedidos
    assert result["cost_usd"] == pytest.approx(2 * COST_LUNA) and (result["input_tokens"], result["output_tokens"]) == (2000, 400)


def test_a_tie_keeps_the_first_variant_of_the_profile(conn, tmp_path):
    set_keyword(conn, add_offer(conn, 1), "otra")
    run(conn, FakeClient(*both_responses(70, 70)), tmp_path)
    assert conn.execute("SELECT best_variant, score FROM scores").fetchone() == ("backend", 70)


def test_the_prefix_of_each_variant_is_fixed_between_offers(conn, tmp_path):
    for n in (1, 2):
        add_offer(conn, n, description=f"Distinta {n}")
        set_keyword(conn, f"{n:016X}", "otra")
    client = FakeClient(*both_responses(50, 60) * 2)
    run(conn, client, tmp_path)
    assert len(client.calls) == 4 and cv_of(client.calls[0]) == cv_of(client.calls[2]) and cv_of(client.calls[1]) == cv_of(client.calls[3])
    assert cv_of(client.calls[0]) != cv_of(client.calls[1])


def spend(conn):
    return conn.execute("SELECT kind, id16, model, ok, input_tokens, output_tokens FROM api_spend ORDER BY rowid").fetchall()


def test_max_cost_reached_between_the_two_requests_cuts_before_the_second_and_leaves_the_offer_in_the_queue(conn, tmp_path):
    first, second = add_offer(conn, 1), add_offer(conn, 2)
    for id16 in (first, second):
        set_keyword(conn, id16, "otra")
    client = FakeClient(*both_responses(40, 85))
    result = run(conn, client, tmp_path, max_cost=COST_LUNA * 0.5)  # alcanza para un pedido, no para dos
    assert len(client.calls) == 1 and result["scored"] == 0 and result["stopped_by_cost"] is True
    assert result["cost_usd"] == pytest.approx(COST_LUNA)
    assert conn.execute("SELECT COUNT(*) FROM scores").fetchone()[0] == 0 and statuses(conn) == ["new", "new"]  # sin puntaje a medias
    assert spend(conn) == [("score", first, "gpt-6-luna", 0, 1000, 200)]  # lo pagado cuenta para el tope mensual
    assert score.month_spent(conn, datetime.now(timezone.utc).date()) == pytest.approx(COST_LUNA)


def test_max_cost_reached_after_a_whole_offer_stops_before_the_next_one_without_recording_anything(conn, tmp_path):
    first, second = add_offer(conn, 1), add_offer(conn, 2)
    for id16 in (first, second):
        set_keyword(conn, id16, "otra")
    client = FakeClient(*both_responses(40, 85))
    result = run(conn, client, tmp_path, max_cost=COST_LUNA * 1.5)  # la primera oferta (2 pedidos) pasa el tope
    assert len(client.calls) == 2 and result["scored"] == 1 and result["stopped_by_cost"] is True
    assert spend(conn) == [] and statuses(conn) == ["new", "new"]


def test_an_invalid_second_response_marks_score_error_without_a_half_score_and_records_both_paid_requests(conn, tmp_path):
    set_keyword(conn, add_offer(conn, 1), "otra")
    client = FakeClient(both_responses(60, 0)[0], response(text="roto"))
    result = run(conn, client, tmp_path)
    assert (result["scored"], result["errors"]) == (0, 1) and statuses(conn) == ["score_error"]
    assert conn.execute("SELECT COUNT(*) FROM scores").fetchone()[0] == 0
    assert [row[3] for row in spend(conn)] == [0, 0]
    assert result["cost_usd"] == pytest.approx(2 * COST_LUNA)


def test_an_api_error_on_the_second_request_stops_the_run_and_keeps_the_first_cost(conn, tmp_path):
    first = set_keyword(conn, add_offer(conn, 1), "otra")
    with pytest.raises(RuntimeError, match="respondió 500"):
        run(conn, FakeClient(both_responses(60, 0)[0], status_error(500)), tmp_path)
    assert conn.execute("SELECT COUNT(*) FROM scores").fetchone()[0] == 0 and statuses(conn) == ["new"]
    assert spend(conn) == [("score", first, "gpt-6-luna", 0, 1000, 200)]


def test_the_stop_file_between_the_two_requests_halts_the_run_and_records_the_first(conn, tmp_path):
    set_keyword(conn, add_offer(conn, 1), "otra")
    client = FakeClient(*both_responses(60, 80), on_call=lambda: (tmp_path / "STOP").write_text("x"))
    with pytest.raises(RuntimeError, match="Existe el archivo"):
        run(conn, client, tmp_path)
    assert len(client.calls) == 1 and conn.execute("SELECT COUNT(*) FROM scores").fetchone()[0] == 0
    assert len(spend(conn)) == 1


def test_three_offers_with_an_invalid_response_in_a_row_still_stop_the_run(conn, tmp_path):
    for n in (1, 2, 3, 4):
        set_keyword(conn, add_offer(conn, n), "otra")
    client = FakeClient(response(text="roto"))
    with pytest.raises(RuntimeError, match="3 respuestas inválidas seguidas"):
        run(conn, client, tmp_path)
    assert len(client.calls) == 3  # el primer pedido de cada oferta ya falla: no se pide el segundo


def test_limit_counts_offers_not_requests(conn, tmp_path):
    for n in (1, 2, 3):
        set_keyword(conn, add_offer(conn, n), "otra")
    client = FakeClient(*both_responses(50, 60) * 3)
    assert run(conn, client, tmp_path, limit=2)["scored"] == 2 and len(client.calls) == 4


def test_the_system_prompt_speaks_of_one_cv_and_the_variants_no_longer_compete():
    assert "el CV de un candidato" in score.SYSTEM and "de 1 a 3 CVs" not in score.SYSTEM and "Elegí el CV de mayor" not in score.SYSTEM
    assert "no des por cumplido un requisito de la oferta que el CV no muestra" in score.SYSTEM


def test_candidate_variants_with_and_without_a_matching_keyword():
    def ids(profile, keyword):
        return [v["id"] for v in score.candidate_variants(profile, keyword)]

    assert ids(PROFILE, "x") == ["backend"] and ids(PROFILE, "Y") == ["data"]
    assert ids(PROFILE, "zzz") == ["backend", "data"] and ids(PROFILE, None) == ["backend", "data"]
    both = {"variants": [{**v, "keywords": ["común", "x"]} for v in PROFILE["variants"]]}
    assert ids(both, "COMUN") == ["backend", "data"]
