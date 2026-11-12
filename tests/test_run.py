"""Tests de run.py y del comando `run`. Sin red (httpx.MockTransport), sin OpenAI (cliente falso) y sin dormir."""
import json
import os
import re
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import openai
import pytest
import yaml

from compuapply import cli, db, run, scan
from compuapply.score import money

ROOT = Path(__file__).parent.parent
EXAMPLE = ROOT / "profile.example.yaml"
TODAY = date(2026, 11, 2)
REQUEST = httpx.Request("POST", "https://api.openai.com/v1/responses")
COST = (600 * 0.10 + 400 * 0.01 + 200 * 0.50) / 1_000_000  # 1000 entrada (400 cacheada) y 200 salida
LIVE_MESSAGE = "El envío real de `run` todavía no está implementado: postulá con `python -m compuapply apply --live`."
META = {"now": datetime(2026, 11, 2, 14, 3, 5), "model": "gpt-6-luna", "threshold": 75, "daily_cap": 10,
        "applied_today": 1}


@pytest.fixture(autouse=True)
def sleeps(monkeypatch):
    """Anula todas las pausas y guarda cuánto se habría dormido."""
    calls = []
    monkeypatch.setattr(scan.time, "sleep", calls.append)
    return calls


# ── sitio falso ──────────────────────────────────────────────────────────────

def title(n: int) -> str:
    return "Ejecutivo de Ventas" if n == 4 else f"Oferta {n}"


def data_id(n: int) -> str:
    """32 hex cuyos primeros 16 (el id16 de la oferta) son distintos para cada `n`."""
    return f"{n:016X}" + "0" * 16


def listing(ns: list[int]) -> str:
    cards = "".join(
        f'<article data-id="{data_id(n)}"><h2><a href="/ofertas-de-trabajo/oferta-{n}-{data_id(n)}">{title(n)}</a></h2>'
        f'<p><a class="t_ellipsis">Empresa {n}</a></p><span class="mr10">Rosario</span></article>' for n in ns)
    return f"<html><body>{cards}</body></html>"


def posting_page() -> str:
    data = {"@context": "https://schema.org", "@graph": [
        {"@type": "JobPosting", "description": "Buscamos analista.<br/>Requisitos: Python", "datePosted": "2026-10-30",
         "validThrough": "2026-12-31", "employmentType": "FULL_TIME"}]}
    return f'<html><head><script type="application/ld+json">{json.dumps(data)}</script></head></html>'


# Detalle que responde 200 pero sin JobPosting ni bloque de la oferta (como una oferta confidencial rota).
BARE_DETAIL = ('<html><head><script type="application/ld+json">{"@graph": [{"@type": "WebPage"}]}</script></head>'
               '<body><h1>Oferta</h1></body></html>')


def make_site(listings: dict[str, list[list[int]]], fail: dict | None = None, fail_detail_at: int | None = None,
              no_detail: frozenset | set = frozenset()):
    """Cliente httpx falso. `listings[slug][p-1]` son las ofertas de la página p; `fail[(slug, p)]` es un status;
    `fail_detail_at` = número (desde 1) del pedido de detalle que responde 429; `no_detail` = números de oferta
    cuyo detalle no trae descripción."""
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if path.startswith("/trabajo-de-"):
            slug, page = path.removeprefix("/trabajo-de-"), int(request.url.params["p"])
            if (fail or {}).get((slug, page)):
                return httpx.Response(fail[(slug, page)], text="")
            pages = listings.get(slug, [])
            return httpx.Response(200, text=listing(pages[page - 1] if page <= len(pages) else []))
        if path.startswith("/ofertas-de-trabajo/"):
            number = sum(1 for r in requests if r.url.path.startswith("/ofertas-de-trabajo/"))
            if number == fail_detail_at:
                return httpx.Response(429, text="")
            bare = int(re.search(r"oferta-(\d+)-", path).group(1)) in no_detail
            return httpx.Response(200, text=BARE_DETAIL if bare else posting_page())
        return httpx.Response(404, text="")

    return httpx.Client(transport=httpx.MockTransport(handler)), requests


def listing_requests(requests) -> list[str]:
    return [f"{r.url.path.removeprefix('/trabajo-de-')}?p={r.url.params['p']}"
            for r in requests if r.url.path.startswith("/trabajo-de-")]


def detail_requests(requests) -> list:
    return [r for r in requests if r.url.path.startswith("/ofertas-de-trabajo/")]


# ── OpenAI falso ─────────────────────────────────────────────────────────────

def llm_response(points: int, variants=("backend", "data")):
    data = {"best_variant": "backend",
            "scores": [{"variant_id": v, "score": points if v == "backend" else 30} for v in variants],
            "reasons": ["Encaja"], "missing": []}
    return SimpleNamespace(
        output_text=json.dumps(data), output=[], status="completed",
        usage=SimpleNamespace(input_tokens=1000, input_tokens_details=SimpleNamespace(cached_tokens=400),
                              output_tokens=200))


def narrow(item, kwargs):
    """Cada pedido lleva un solo CV: una respuesta armada con las dos variantes se reduce a las pedidas."""
    try:
        data = json.loads(item.output_text)
        ids = kwargs["text"]["format"]["schema"]["properties"]["scores"]["items"]["properties"]["variant_id"]["enum"]
        scores = data["scores"]
        keep = [s for s in scores if s["variant_id"] in ids]
    except (AttributeError, TypeError, ValueError, KeyError):
        return item
    if not keep or len(keep) == len(scores) or data.get("best_variant") != max(scores, key=lambda s: s["score"])["variant_id"]:
        return item
    return SimpleNamespace(**{**vars(item), "output_text": json.dumps({**data, "scores": keep, "best_variant": keep[0]["variant_id"]})})


class FakeLLM:
    """Devuelve las respuestas en orden (la última se repite); una excepción se lanza."""

    def __init__(self, *responses):
        self.calls, self._responses = [], list(responses) or [llm_response(80)]
        self.responses = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        item = self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]
        if isinstance(item, Exception):
            raise item
        return narrow(item, kwargs)


def status_error(code: int) -> openai.APIStatusError:
    return openai.APIStatusError("clave sk-secreta-123 mala", response=httpx.Response(code, request=REQUEST), body=None)


# ── perfil y base ────────────────────────────────────────────────────────────

def profile_dict(keywords_a=("python", "Python", "backend"), keywords_b=("backend",), exclude=("ventas",)) -> dict:
    return {
        "variants": [
            {"id": "backend", "keywords": list(keywords_a),
             "resume": {"experience_details": [{"position": "Dev", "company": "Acme"}]}},
            {"id": "data", "keywords": list(keywords_b),
             "resume": {"projects": [{"name": "Tablero", "description": "Power BI"}]}},
        ],
        "exclude_companies": [], "exclude_title_words": list(exclude),
        "apply": {"threshold": 75, "daily_cap": 10, "calibrated_with": "gpt-6-luna v5"},
    }


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / "t.db")
    yield connection
    connection.close()


def go(conn, tmp_path, http, llm, *, profile=None, pages=2, max_details=20, score_limit=20, max_cost=10.0):
    return run.run_all(conn, profile or profile_dict(), http, llm, "gpt-6-luna", pages, max_details, score_limit,
                       max_cost, TODAY, stop_file=tmp_path / "STOP")


HAPPY_SITE = {"python": [[1, 2, 3]], "backend": [[3, 4]]}  # la 3 aparece en las dos keywords; la 4 es de ventas


def happy(conn, tmp_path):
    http, requests = make_site(HAPPY_SITE)
    llm = FakeLLM(llm_response(85), llm_response(60), llm_response(90))
    return go(conn, tmp_path, http, llm), requests, llm


def section(text: str, name: str) -> str:
    return text.split(f"\n## {name}", 1)[1].split("\n## ", 1)[0]


# ── keywords ─────────────────────────────────────────────────────────────────

def test_keywords_are_the_union_without_repeats_in_order():
    assert run.keywords(profile_dict()) == ["python", "backend"]


def test_keywords_compare_by_slug_and_drop_unsearchable_ones():
    profile = profile_dict(keywords_a=("Análisis de Datos", "!!!"), keywords_b=("analisis de datos", "SQL", "sql "))
    assert run.keywords(profile) == ["Análisis de Datos", "SQL"]


@pytest.mark.parametrize("keywords", [(), ("",), ("!!!", "   ")])
def test_profile_without_keywords_fails_before_asking_anything(conn, tmp_path, keywords):
    http, requests = make_site({})
    with pytest.raises(RuntimeError, match="keywords"):
        go(conn, tmp_path, http, FakeLLM(), profile=profile_dict(keywords_a=keywords, keywords_b=()))
    assert requests == [] and conn.execute("SELECT COUNT(*) FROM offers").fetchone()[0] == 0


# ── corrida feliz ────────────────────────────────────────────────────────────

def test_two_variants_ask_only_two_keywords_in_order(conn, tmp_path):
    _, requests, _ = happy(conn, tmp_path)
    assert listing_requests(requests) == ["python?p=1", "python?p=2", "backend?p=1", "backend?p=2"]


def test_happy_run_returns_every_stage(conn, tmp_path):
    result, requests, llm = happy(conn, tmp_path)
    assert result["stopped"] is None
    assert result["scan"] == [
        {"keyword": "python", "pages_read": 2, "found": 3, "new": 3, "seen": 0},
        {"keyword": "backend", "pages_read": 2, "found": 2, "new": 1, "seen": 1},
    ]
    assert result["details"] == {"ran": True, "saved": 4, "bad_url": 0, "no_detail": 0, "pending": 0}
    assert len(detail_requests(requests)) == 4
    points = result["score"]
    assert (points["scored"], points["excluded"], points["errors"], points["stopped_by_cost"]) == (3, 1, 0, False)
    assert (points["input_tokens"], points["output_tokens"]) == (3000, 600)
    assert points["cost_usd"] == pytest.approx(3 * COST) and len(llm.calls) == 3
    plan = result["plan"]
    assert [(i["title"], i["score"]) for i in plan["send"]] == [("Oferta 3", 90), ("Oferta 1", 85)]
    assert [(i["title"], i["reason"]) for i in plan["skip"]] == [("Oferta 2", "puntaje bajo (60 < 75)")]


def test_stages_run_in_order_with_a_pause_between_keywords(conn, tmp_path, monkeypatch):
    events = []
    monkeypatch.setattr(scan.time, "sleep", lambda s: events.append(("sleep", s)))
    http, requests = make_site(HAPPY_SITE)
    original = http._transport.handler
    http._transport = httpx.MockTransport(lambda r: events.append(("get", r.url.path.split("/")[1])) or original(r))
    go(conn, tmp_path, http, FakeLLM())
    first_detail = next(i for i, e in enumerate(events) if e == ("get", "ofertas-de-trabajo"))
    scan_phase = events[:first_detail - 1]  # todo lo anterior a la pausa que precede al primer detalle
    # python p1, pausa entre páginas, python p2, pausa entre keywords, backend p1, pausa entre páginas, backend p2
    assert [kind for kind, _ in scan_phase] == ["get", "sleep", "get", "sleep", "get", "sleep", "get"]
    assert [what for kind, what in scan_phase if kind == "get"] == ["trabajo-de-python"] * 2 + ["trabajo-de-backend"] * 2
    assert all(0.7 <= value <= 1.5 for kind, value in scan_phase if kind == "sleep")
    kind, value = events[first_detail - 1]  # los detalles empiezan con su propia pausa (1.0 a 2.5 s)
    assert kind == "sleep" and 1.0 <= value <= 2.5
    assert not any(kind == "get" and what.startswith("trabajo-de-") for kind, what in events[first_detail:])


def test_run_all_writes_no_files(conn, tmp_path):
    happy(conn, tmp_path)
    assert list(tmp_path.rglob("*.md")) == [] and not (tmp_path / "data").exists()


# ── frenos ───────────────────────────────────────────────────────────────────

def test_429_on_the_second_keyword_stops_everything_after_scan(conn, tmp_path):
    http, requests = make_site(HAPPY_SITE, fail={("backend", 1): 429})
    llm = FakeLLM()
    result = go(conn, tmp_path, http, llm)
    assert result["stopped"]["stage"] == "scan" and "429" in result["stopped"]["message"]
    assert [r["keyword"] for r in result["scan"]] == ["python"]
    assert result["details"]["ran"] is False and result["score"] is None and result["plan"] is None
    assert detail_requests(requests) == [] and llm.calls == []
    assert listing_requests(requests) == ["python?p=1", "python?p=2", "backend?p=1"]  # sin reintento
    assert conn.execute("SELECT COUNT(*) FROM offers").fetchone()[0] == 3  # lo de la primera keyword quedó


def test_429_on_a_detail_keeps_the_ones_already_saved_and_skips_score_and_plan(conn, tmp_path):
    http, requests = make_site(HAPPY_SITE, fail_detail_at=2)
    llm = FakeLLM()
    result = go(conn, tmp_path, http, llm)
    assert result["stopped"]["stage"] == "details" and "429" in result["stopped"]["message"]
    assert result["details"] == {"ran": True, "saved": 1, "bad_url": 0, "no_detail": 0, "pending": 3}
    assert result["score"] is None and result["plan"] is None and llm.calls == []
    assert len(detail_requests(requests)) == 2


def test_openai_error_keeps_what_was_scored_and_skips_the_plan(conn, tmp_path):
    http, _ = make_site(HAPPY_SITE)
    llm = FakeLLM(llm_response(85), status_error(401))
    result = go(conn, tmp_path, http, llm)
    stopped = result["stopped"]
    assert stopped["stage"] == "score" and "401" in stopped["message"] and "sk-secreta" not in stopped["message"]
    assert (stopped["scored"], stopped["cost_usd"]) == (1, pytest.approx(COST))
    assert result["score"] is None and result["plan"] is None
    assert result["details"]["saved"] == 4 and len(llm.calls) == 2
    assert conn.execute("SELECT COUNT(*) FROM scores").fetchone()[0] == 1


def test_stop_file_before_the_first_keyword_asks_nothing(conn, tmp_path):
    (tmp_path / "STOP").write_text("")
    http, requests = make_site(HAPPY_SITE)
    llm = FakeLLM()
    result = go(conn, tmp_path, http, llm)
    assert requests == [] and llm.calls == []
    assert result["stopped"]["stage"] == "scan" and "STOP" in result["stopped"]["message"]
    assert result["scan"] == [] and result["plan"] is None


def test_stop_file_created_between_keywords_stops_before_the_next_one(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(scan.time, "sleep", lambda s: (tmp_path / "STOP").write_text(""))
    http, requests = make_site(HAPPY_SITE)
    result = go(conn, tmp_path, http, FakeLLM(), pages=1)  # una página por keyword: la única pausa es entre keywords
    assert listing_requests(requests) == ["python?p=1"]
    assert result["stopped"]["stage"] == "scan" and "«backend»" in result["stopped"]["message"]
    assert [r["keyword"] for r in result["scan"]] == ["python"] and result["details"]["ran"] is False


def test_score_stops_on_the_cost_cap_without_being_an_error(conn, tmp_path):
    http, _ = make_site(HAPPY_SITE)
    result = go(conn, tmp_path, http, FakeLLM(), max_cost=0)
    assert result["stopped"] is None and result["score"]["stopped_by_cost"] is True and result["score"]["scored"] == 0
    assert result["plan"]["send"] == [] and result["plan"]["skip"] == []


def test_bad_urls_are_counted_in_details(conn, tmp_path):
    http, _ = make_site({"python": [[1, 2]]})
    db.upsert_offers(conn, [{"id16": f"{9:016X}", "data_id": f"{9:032X}", "title": "Sin link", "company": "X",
                             "location": "Y", "url": ""}], "python")
    result = go(conn, tmp_path, http, FakeLLM(), profile=profile_dict(keywords_a=("python",), keywords_b=()))
    assert result["details"] == {"ran": True, "saved": 2, "bad_url": 1, "no_detail": 0, "pending": 0}


# ── ofertas sin descripción ──────────────────────────────────────────────────

def test_offers_without_description_are_counted_marked_and_never_scored(conn, tmp_path):
    http, requests = make_site(HAPPY_SITE, no_detail={2})
    llm = FakeLLM(llm_response(85), llm_response(90))
    result = go(conn, tmp_path, http, llm)
    assert result["stopped"] is None
    assert result["details"] == {"ran": True, "saved": 3, "bad_url": 0, "no_detail": 1, "pending": 0}
    points = result["score"]
    assert (points["scored"], points["excluded"]) == (2, 1) and len(llm.calls) == 2  # la 2 no entra en la cola de puntaje
    assert conn.execute("SELECT status FROM offers WHERE id16 = ?", (f"{2:016X}",)).fetchone() == ("no_detail",)
    assert conn.execute("SELECT COUNT(*) FROM scores WHERE id16 = ?", (f"{2:016X}",)).fetchone() == (0,)
    plan = result["plan"]
    assert sorted(i["title"] for i in plan["send"] + plan["skip"]) == ["Oferta 1", "Oferta 3"]
    seen_requests = len(requests)
    second = go(conn, tmp_path, http, llm)  # con la misma base: ni vuelve a la cola de detalles ni a la de puntaje
    assert second["details"] == {"ran": True, "saved": 0, "bad_url": 0, "no_detail": 0, "pending": 0}
    assert detail_requests(requests[seen_requests:]) == [] and len(llm.calls) == 2 and second["score"]["scored"] == 0


def test_three_offers_without_description_in_a_row_stop_the_details_stage(conn, tmp_path):
    http, requests = make_site({"python": [[1, 2, 3, 5]]}, no_detail={1, 2, 3})
    llm = FakeLLM()
    result = go(conn, tmp_path, http, llm, profile=profile_dict(keywords_a=("python",), keywords_b=()))
    assert result["stopped"] == {"stage": "details",
                                 "message": "3 ofertas seguidas sin descripción: posible cambio de layout"}
    assert result["details"] == {"ran": True, "saved": 0, "bad_url": 0, "no_detail": 3, "pending": 1}
    assert result["score"] is None and result["plan"] is None and llm.calls == []
    assert len(detail_requests(requests)) == 3


def test_summary_and_report_show_offers_without_description_after_the_invalid_urls(conn, tmp_path):
    http, _ = make_site({"python": [[1, 2, 3]]}, no_detail={2})
    db.upsert_offers(conn, [{"id16": f"{9:016X}", "data_id": f"{9:032X}", "title": "Sin link", "company": "X",
                             "location": "Y", "url": ""}], "python")
    result = go(conn, tmp_path, http, FakeLLM(), profile=profile_dict(keywords_a=("python",), keywords_b=()))
    assert result["details"] == {"ran": True, "saved": 2, "bad_url": 1, "no_detail": 1, "pending": 0}
    assert run.summary_lines(result)[1] == "Detalles: guardados: 2 · URL inválida: 1 · sin descripción: 1"
    details = section(run.write_report(result, META, tmp_path).read_text(encoding="utf-8"), "Detalles")
    assert "- Guardados: 2" in details and "- URL inválida: 1 · sin descripción: 1" in details


# ── informe ──────────────────────────────────────────────────────────────────

def test_report_of_a_complete_run_has_the_four_sections_and_no_freno(conn, tmp_path):
    result, _, _ = happy(conn, tmp_path)
    path = run.write_report(result, META, tmp_path / "runs" / "nuevo")
    assert path == tmp_path / "runs" / "nuevo" / "20261102-140305-run.md"
    text = path.read_text(encoding="utf-8")
    assert [m for m in re.findall(r"^## (.+)$", text, re.M)] == ["Scan", "Detalles", "Puntaje", "Plan (modo seco)"]
    for expected in ("# Informe de run (modo seco)", "Fecha: 2026-11-02 14:03:05", "Modo: seco", "Modelo: gpt-6-luna",
                     "Umbral: 75", "Tope diario: 10 (ya postuladas hoy: 1)", "Estado: completa"):
        assert expected in text, expected
    assert "| python | 2 | 3 | 3 | 0 |" in section(text, "Scan") and "| backend | 2 | 2 | 1 | 1 |" in section(text, "Scan")
    assert "- Guardados: 4" in section(text, "Detalles") and "- URL inválida: 0" in section(text, "Detalles")
    assert "sin descripción" not in text  # con 0 no aparece
    points = section(text, "Puntaje")
    for expected in ("- Puntuadas: 3", "- Excluidas: 1", "- Con error: 0", "- Costo: US$ 0,0005",
                     "- Tokens: entrada 3000 / salida 600", "- Cortó por tope de gasto: no",
                     "- Gasto del mes: US$ 0,0005 de US$ 2,00"):
        assert expected in points, expected
    plan = section(text, "Plan (modo seco)")
    assert "Las preguntas del formulario se verifican al postular (Fase 4)." in plan
    assert "### Enviaría (2)" in plan and "### Saltaría (1)" in plan
    assert plan.count("| oferta | empresa | puntaje | CV | motivo | url |") == 2
    assert "| Oferta 3 | Empresa 3 | 90 | backend | cumple las reglas |" in plan
    assert "puntaje bajo (60 < 75)" in plan


def test_report_after_a_scan_stop_has_freno_and_the_first_keyword(conn, tmp_path):
    http, _ = make_site(HAPPY_SITE, fail={("backend", 1): 429})
    result = go(conn, tmp_path, http, FakeLLM())
    text = run.write_report(result, META, tmp_path).read_text(encoding="utf-8")
    assert "| python | 2 | 3 | 3 | 0 |" in section(text, "Scan") and "| backend |" not in text
    assert "frenó en la etapa scan (ver «Freno»)" in text
    assert "No se corrió" in section(text, "Detalles") and "No se corrió" in section(text, "Puntaje")
    assert "No se armó" in section(text, "Plan (modo seco)")
    freno = section(text, "Freno")
    assert "- Etapa: scan" in freno and "429" in freno
    assert text.rstrip().endswith(freno.rstrip())  # Freno es la última sección


def test_report_after_an_openai_error_keeps_the_partial_score(conn, tmp_path):
    http, _ = make_site(HAPPY_SITE)
    result = go(conn, tmp_path, http, FakeLLM(llm_response(85), status_error(401)))
    text = run.write_report(result, META, tmp_path).read_text(encoding="utf-8")
    assert "Puntuadas y guardadas antes del freno: 1" in section(text, "Puntaje")
    assert "- Etapa: score" in section(text, "Freno") and "No se armó" in section(text, "Plan (modo seco)")
    assert "sk-secreta" not in text


def test_report_with_a_stop_before_anything_says_no_keyword_was_read(conn, tmp_path):
    (tmp_path / "STOP").write_text("")
    http, _ = make_site(HAPPY_SITE)
    text = run.write_report(go(conn, tmp_path, http, FakeLLM()), META, tmp_path).read_text(encoding="utf-8")
    assert "_Ninguna keyword llegó a leerse._" in text and "- Etapa: scan" in section(text, "Freno")


def test_report_marks_the_cost_cap_and_escapes_keywords(conn, tmp_path):
    http, _ = make_site({"a-b": [[1]]})
    result = go(conn, tmp_path, http, FakeLLM(), max_cost=0, profile=profile_dict(keywords_a=("a|b",), keywords_b=()))
    text = run.write_report(result, META, tmp_path).read_text(encoding="utf-8")
    assert "- Cortó por tope de gasto: sí" in section(text, "Puntaje")
    assert "| a\\|b | 2 | 1 | 1 | 0 |" in section(text, "Scan")  # 2 páginas leídas: la segunda, vacía, corta


def test_summary_lines_match_the_report_numbers(conn, tmp_path):
    result, _, _ = happy(conn, tmp_path)
    assert run.summary_lines(result) == [
        "Scan: keywords: 2 · páginas leídas: 4 · encontradas: 5 · nuevas: 4 · ya vistas: 1",
        "Detalles: guardados: 4",
        "Puntaje: puntuadas: 3 · excluidas: 1 · con error: 0 · costo: US$ 0,0005 · tokens: entrada 3000 / salida 600",
        "Gasto del mes: US$ 0,0005 de US$ 2,00",
        "Plan (modo seco): Enviaría: 2 · saltaría: 1 (ya postulada: 0 · vencida: 0 · puntaje bajo: 1 · tope diario: 0)",
    ]


def test_summary_lines_after_a_stop_only_show_the_stages_that_ran(conn, tmp_path):
    http, _ = make_site(HAPPY_SITE, fail={("backend", 1): 429})
    lines = run.summary_lines(go(conn, tmp_path, http, FakeLLM()))
    assert lines == ["Scan: keywords: 1 · páginas leídas: 2 · encontradas: 3 · nuevas: 3 · ya vistas: 0"]


# ── tope mensual ─────────────────────────────────────────────────────────────

CAP_ONE = {"run": {"max_cost_month": 1.0}}  # el perfil de estos tests: un dólar por mes


def spend_before(conn, cost: float, stamp: str | None = None) -> None:
    """Deja un puntaje viejo (de otra oferta, ya con descripción) que costó `cost`; `stamp` es su fecha UTC (por defecto, ahora)."""
    id16 = f"{99:016X}"
    db.upsert_offers(conn, [{"id16": id16, "data_id": data_id(99), "title": "Oferta vieja", "company": "Empresa 99",
                             "location": "Rosario", "url": f"https://ar.computrabajo.com/ofertas-de-trabajo/oferta-99-{data_id(99)}"}],
                     "python")
    db.save_detail(conn, id16, {"description": "Descripción vieja.", "date_posted": "", "valid_through": "", "employment_type": ""})
    db.save_score(conn, id16, "gpt-6-luna", "v5", {"best_variant": "backend", "score": 90, "all_scores": {"backend": 90},
                                                   "reasons": [], "missing": []}, (1, 0, 1), cost)
    if stamp:
        conn.execute("UPDATE scores SET scored_at = ? WHERE id16 = ?", (stamp, id16))
        conn.commit()


@pytest.mark.parametrize("spent", [1.5, 1.0])  # pasado el tope, y justo en el tope
def test_monthly_cap_reached_skips_scoring_without_calling_the_api_and_still_builds_the_plan(conn, tmp_path, spent):
    spend_before(conn, spent)
    http, requests = make_site(HAPPY_SITE)
    llm = FakeLLM()
    result = go(conn, tmp_path, http, llm, profile={**profile_dict(), **CAP_ONE})
    assert llm.calls == [] and result["score"] is None and result["stopped"] is None
    assert result["month"] == {"spent": pytest.approx(spent), "cap": 1.0, "reached": True}
    assert result["details"]["saved"] == 4 and len(detail_requests(requests)) == 4  # scan y detalles corrieron igual
    assert [item["title"] for item in result["plan"]["send"]] == ["Oferta vieja"]  # el plan sale de lo ya puntuado
    lines = run.summary_lines(result)
    assert lines[2] == f"Tope mensual alcanzado: {money(spent)} de US$ 1,00" and not any(l.startswith("Puntaje") for l in lines)
    assert lines[3].startswith("Plan (modo seco): Enviaría: 1")
    text = run.write_report(result, META, tmp_path).read_text(encoding="utf-8")
    assert f"- Tope mensual alcanzado: {money(spent)} de US$ 1,00" in section(text, "Puntaje")
    assert "No se corrió" not in section(text, "Puntaje") and "Estado: completa" in text
    assert "| Oferta vieja |" in section(text, "Plan (modo seco)")


def test_the_score_budget_is_what_is_left_of_the_month_when_it_is_less_than_max_cost(conn, tmp_path):
    spend_before(conn, 1.0 - 1.5 * COST)  # quedan 1,5 llamadas
    http, _ = make_site(HAPPY_SITE)
    llm = FakeLLM(llm_response(85), llm_response(60), llm_response(90))
    result = go(conn, tmp_path, http, llm, profile={**profile_dict(), **CAP_ONE}, max_cost=10.0)
    assert len(llm.calls) == 2 and result["score"]["scored"] == 2 and result["score"]["stopped_by_cost"] is True
    assert result["month"] == {"spent": pytest.approx(1.0 + 0.5 * COST), "cap": 1.0, "reached": False}
    assert run.summary_lines(result)[3] == f"Gasto del mes: {money(1.0 + 0.5 * COST)} de US$ 1,00"


def test_max_cost_still_cuts_first_when_it_is_less_than_what_is_left_of_the_month(conn, tmp_path):
    spend_before(conn, 0.1)
    http, _ = make_site(HAPPY_SITE)
    llm = FakeLLM()
    result = go(conn, tmp_path, http, llm, profile={**profile_dict(), **CAP_ONE}, max_cost=1.5 * COST)
    assert len(llm.calls) == 2 and result["score"]["stopped_by_cost"] is True and result["month"]["reached"] is False


def test_a_profile_without_a_run_section_uses_the_default_monthly_cap(conn, tmp_path):
    http, _ = make_site({"python": [[1]]})
    llm = FakeLLM()
    spend_before(conn, 2.5)  # más que los 2.00 por defecto
    result = go(conn, tmp_path, http, llm, profile=profile_dict(keywords_a=("python",), keywords_b=()))
    assert llm.calls == [] and result["month"] == {"spent": pytest.approx(2.5), "cap": 2.0, "reached": True}


@pytest.mark.parametrize("today_utc, capped", [(date(2026, 11, 30), True), (date(2026, 12, 1), False), (date(2026, 10, 31), False)])
def test_the_month_is_the_utc_month_of_each_score(conn, tmp_path, today_utc, capped):
    spend_before(conn, 5.0, stamp="2026-11-30T23:59:59+00:00")  # el último segundo de noviembre en UTC
    http, _ = make_site({"python": [[1]]})
    llm = FakeLLM()
    profile = {**profile_dict(keywords_a=("python",), keywords_b=()), **CAP_ONE}
    result = run.run_all(conn, profile, http, llm, "gpt-6-luna", 2, 20, 20, 10.0, TODAY, stop_file=tmp_path / "STOP",
                         today_utc=today_utc)
    assert (result["month"]["reached"], bool(llm.calls)) == (capped, not capped)


def test_the_month_is_also_reported_when_scoring_stops_with_an_error(conn, tmp_path):
    spend_before(conn, 0.25)
    http, _ = make_site(HAPPY_SITE)
    result = go(conn, tmp_path, http, FakeLLM(llm_response(85), status_error(401)), profile={**profile_dict(), **CAP_ONE})
    assert result["stopped"]["stage"] == "score" and result["plan"] is None
    assert result["month"] == {"spent": pytest.approx(0.25 + COST), "cap": 1.0, "reached": False}
    assert run.summary_lines(result)[-1] == f"Gasto del mes: {money(0.25 + COST)} de US$ 1,00"
    text = run.write_report(result, META, tmp_path).read_text(encoding="utf-8")
    assert f"- Gasto del mes: {money(0.25 + COST)} de US$ 1,00" in section(text, "Puntaje")


def test_no_month_line_when_the_run_stopped_before_scoring(conn, tmp_path):
    http, _ = make_site(HAPPY_SITE, fail={("backend", 1): 429})
    result = go(conn, tmp_path, http, FakeLLM())
    assert result["month"] is None
    text = run.write_report(result, META, tmp_path).read_text(encoding="utf-8")
    assert "mes" not in section(text, "Puntaje") and "No se corrió" in section(text, "Puntaje")


# ── CLI ──────────────────────────────────────────────────────────────────────

@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    """CLI aislado: cwd vacío (ningún .env real; el informe cae en tmp_path/data), clave falsa y todo reemplazado."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(os, "environ", {"OPENAI_API_KEY": "sk-test-NUNCA-SE-IMPRIME"})
    holder = SimpleNamespace(llm=FakeLLM(), http=None, requests=None)
    monkeypatch.setattr(cli.openai, "OpenAI", lambda: holder.llm)

    def install_site(*args, **kwargs):
        client, holder.requests = make_site(*args, **kwargs)
        monkeypatch.setattr(cli.httpx, "Client", lambda **kw: client)

    holder.install_site = install_site
    return holder


def cli_args(tmp_path, *extra: str) -> list[str]:
    return ["run", "--pages", "1", "--profile", str(EXAMPLE), "--db", str(tmp_path / "t.db"), *extra]


def example_llm(points: int):
    data = {"best_variant": "backend", "scores": [{"variant_id": "backend", "score": points}], "reasons": [],
            "missing": []}
    return SimpleNamespace(output_text=json.dumps(data), output=[], status="completed",
                           usage=SimpleNamespace(input_tokens=1000, input_tokens_details=SimpleNamespace(cached_tokens=400),
                                                 output_tokens=200))


EXAMPLE_SITE = {"python": [[1, 2]], "backend": [[2, 3]], "api": [[5]]}  # keywords de profile.example.yaml


def test_cli_run_live_exits_1_with_the_exact_message_and_touches_nothing(cli_env, tmp_path, capsys):
    assert cli.main(["run", "--live", "--profile", str(tmp_path / "no-existe.yaml"), "--db", str(tmp_path / "t.db")]) == 1
    captured = capsys.readouterr()
    assert captured.err.strip() == LIVE_MESSAGE and captured.out == ""
    assert not (tmp_path / "t.db").exists() and not (tmp_path / "data").exists()


def test_cli_run_prints_one_line_per_stage_and_writes_one_report(cli_env, tmp_path, capsys):
    cli_env.llm = FakeLLM(example_llm(80))
    cli_env.install_site(EXAMPLE_SITE)
    assert cli.main(cli_args(tmp_path)) == 0
    captured = capsys.readouterr()
    lines = captured.out.strip().splitlines()
    assert lines[:5] == [
        "Scan: keywords: 3 · páginas leídas: 3 · encontradas: 5 · nuevas: 4 · ya vistas: 1",
        "Detalles: guardados: 4",
        "Puntaje: puntuadas: 4 · excluidas: 0 · con error: 0 · costo: US$ 0,0007 · tokens: entrada 4000 / salida 800",
        "Gasto del mes: US$ 0,0007 de US$ 2,00",
        "Plan (modo seco): Enviaría: 4 · saltaría: 0 (ya postulada: 0 · vencida: 0 · puntaje bajo: 0 · tope diario: 0)",
    ]
    assert lines[5] == UNCALIBRATED  # profile.example.yaml no declara de dónde salió el umbral
    assert re.fullmatch(r"Informe: data[\\/]runs[\\/]\d{8}-\d{6}-run\.md", lines[6]) and captured.err == ""
    [report] = (tmp_path / "data" / "runs").glob("*-run.md")
    text = report.read_text(encoding="utf-8")
    assert UNCALIBRATED in section(text, "Plan (modo seco)")
    assert "## Freno" not in text and "Modelo: gpt-6-luna" in text
    assert "sk-test" not in captured.out + text  # ni la clave ni datos del CV
    assert "Senior Software Engineer" not in text and "TechSolutions" not in text
    assert db.connect(tmp_path / "t.db").execute("SELECT COUNT(*) FROM applications").fetchone()[0] == 0


def test_cli_run_shows_offers_without_description_in_the_terminal_and_the_report(cli_env, tmp_path, capsys):
    cli_env.llm = FakeLLM(example_llm(80))
    cli_env.install_site(EXAMPLE_SITE, no_detail={2})
    assert cli.main(cli_args(tmp_path)) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[1] == "Detalles: guardados: 3 · sin descripción: 1" and lines[2].startswith("Puntaje: puntuadas: 3 ")
    [report] = (tmp_path / "data" / "runs").glob("*-run.md")
    assert "- URL inválida: 0 · sin descripción: 1" in report.read_text(encoding="utf-8")


def test_cli_run_stage_stop_still_writes_the_report_and_exits_1(cli_env, tmp_path, capsys):
    cli_env.install_site(EXAMPLE_SITE, fail={("backend", 1): 429})
    assert cli.main(cli_args(tmp_path)) == 1
    captured = capsys.readouterr()
    assert captured.out.splitlines()[0] == "Scan: keywords: 1 · páginas leídas: 1 · encontradas: 2 · nuevas: 2 · ya vistas: 0"
    assert "Freno en la etapa scan:" in captured.err and "429" in captured.err
    assert "Informe: " in captured.out
    [report] = (tmp_path / "data" / "runs").glob("*-run.md")
    assert "- Etapa: scan" in report.read_text(encoding="utf-8")
    assert cli_env.llm.calls == []


def test_cli_run_openai_error_exits_1_with_partial_report(cli_env, tmp_path, capsys):
    cli_env.llm = FakeLLM(example_llm(80), status_error(401))
    cli_env.install_site(EXAMPLE_SITE)
    assert cli.main(cli_args(tmp_path)) == 1
    captured = capsys.readouterr()
    assert "Freno en la etapa score:" in captured.err and "401" in captured.err
    assert "sk-test" not in captured.out + captured.err and "sk-secreta" not in captured.err
    [report] = (tmp_path / "data" / "runs").glob("*-run.md")
    assert "Puntuadas y guardadas antes del freno: 1" in report.read_text(encoding="utf-8")


NO_KEY = "Falta la clave de OpenAI: cargala en la pestaña Estado (o con python -m compuapply setup env)"
NO_SCORE = "Puntaje: no se pidió (score_limit 0)"
EXAMPLE_SCAN = "Scan: keywords: 3 · páginas leídas: 3 · encontradas: 5 · nuevas: 4 · ya vistas: 1"
UNCALIBRATED = "El umbral 75 no salió de la calibración: etiquetá en Calibrar y usá el umbral sugerido."
EMPTY_PLAN = "Plan (modo seco): Enviaría: 0 · saltaría: 0 (ya postulada: 0 · vencida: 0 · puntaje bajo: 0 · tope diario: 0)"
NO_SCORES_NOTE = ("Todavía no hay ofertas puntuadas con gpt-6-luna: puntuá primero (Correr → Puntuar pendientes, "
                  "o una corrida con score_limit mayor que 0).")


@pytest.fixture
def no_key(cli_env, monkeypatch):
    """`cli_env` sin clave: el entorno queda vacío y armar el cliente de OpenAI (que sin clave fallaría) rompe el test."""
    def forbidden():
        raise AssertionError("sin clave no se tiene que armar el cliente de OpenAI")

    monkeypatch.setattr(os, "environ", {})
    monkeypatch.setattr(cli.openai, "OpenAI", forbidden)
    return cli_env


def test_cli_run_without_a_key_and_score_limit_0_searches_builds_the_plan_and_exits_0(no_key, tmp_path, capsys):
    no_key.install_site(EXAMPLE_SITE)
    assert cli.main(cli_args(tmp_path, "--score-limit", "0")) == 0
    captured = capsys.readouterr()
    lines = captured.out.strip().splitlines()
    assert lines[:5] == [EXAMPLE_SCAN, "Detalles: guardados: 4", NO_SCORE, "Gasto del mes: US$ 0,0000 de US$ 2,00", EMPTY_PLAN]
    assert lines[5:7] == [NO_SCORES_NOTE, UNCALIBRATED] and lines[7].startswith("Informe: ") and captured.err == ""
    assert len(listing_requests(no_key.requests)) == 3 and len(detail_requests(no_key.requests)) == 4  # buscó y bajó detalles
    [report] = (tmp_path / "data" / "runs").glob("*-run.md")
    text = report.read_text(encoding="utf-8")
    assert "## Freno" not in text and "Estado: completa" in text
    assert NO_SCORES_NOTE in section(text, "Plan (modo seco)")  # también en el informe
    assert f"- {NO_SCORE}" in section(text, "Puntaje") and "No se corrió" not in section(text, "Puntaje")
    assert NO_KEY not in captured.out + text and "sk-test" not in captured.out + text


def test_cli_run_without_a_key_and_scoring_asked_searches_then_stops_in_score_and_exits_1(no_key, tmp_path, capsys):
    no_key.install_site(EXAMPLE_SITE)
    assert cli.main(cli_args(tmp_path)) == 1
    captured = capsys.readouterr()
    lines = captured.out.strip().splitlines()
    assert lines[:3] == [EXAMPLE_SCAN, "Detalles: guardados: 4", "Gasto del mes: US$ 0,0000 de US$ 2,00"]
    assert lines[3].startswith("Informe: ") and len(lines) == 4  # ni Puntaje ni Plan: la etapa frenó
    assert captured.err == f"Freno en la etapa score: {NO_KEY}\n"
    assert len(listing_requests(no_key.requests)) == 3 and len(detail_requests(no_key.requests)) == 4
    [report] = (tmp_path / "data" / "runs").glob("*-run.md")  # el informe se escribe igual
    text = report.read_text(encoding="utf-8")
    assert "Estado: frenó en la etapa score (ver «Freno»)" in text
    freno = section(text, "Freno")
    assert "- Etapa: score" in freno and f"- Mensaje: {NO_KEY}" in freno and text.rstrip().endswith(freno.rstrip())
    assert "No se armó" in section(text, "Plan (modo seco)")


def test_cli_run_with_the_monthly_cap_reached_does_not_ask_for_the_key_either(no_key, tmp_path, capsys):
    conn = db.connect(tmp_path / "t.db")
    spend_before(conn, 3.0)
    conn.close()
    no_key.install_site(EXAMPLE_SITE)
    assert cli.main(cli_args(tmp_path)) == 0
    captured = capsys.readouterr()
    assert "Tope mensual alcanzado: US$ 3,0000 de US$ 2,00" in captured.out.splitlines() and captured.err == ""
    [report] = (tmp_path / "data" / "runs").glob("*-run.md")
    assert "## Freno" not in report.read_text(encoding="utf-8")


def test_cli_run_without_a_key_still_takes_the_model_from_the_env_file(no_key, tmp_path):
    (tmp_path / ".env").write_text("OPENAI_MODEL=gpt-4o-mini\n", encoding="utf-8")
    no_key.install_site(EXAMPLE_SITE)
    assert cli.main(cli_args(tmp_path, "--score-limit", "0")) == 0
    [report] = (tmp_path / "data" / "runs").glob("*-run.md")
    assert "Modelo: gpt-4o-mini" in report.read_text(encoding="utf-8")


def test_cli_run_takes_the_key_from_the_env_file(cli_env, tmp_path, monkeypatch):
    monkeypatch.setattr(os, "environ", {})
    (tmp_path / ".env").write_text("OPENAI_API_KEY=sk-test-DEL-ARCHIVO\n", encoding="utf-8")
    cli_env.llm = FakeLLM(example_llm(80))
    cli_env.install_site(EXAMPLE_SITE)
    assert cli.main(cli_args(tmp_path)) == 0
    assert len(cli_env.llm.calls) == 4


# ── sin cliente de OpenAI (sin clave) ────────────────────────────────────────

def test_score_limit_0_needs_no_client_and_the_plan_comes_from_the_scores_already_there(conn, tmp_path):
    spend_before(conn, 0.1)  # una oferta vieja ya puntuada con 90
    http, requests = make_site(HAPPY_SITE)
    result = go(conn, tmp_path, http, None, score_limit=0)
    assert result["score"] is None and result["score_requested"] is False and result["stopped"] is None
    assert result["month"] == {"spent": pytest.approx(0.1), "cap": 2.0, "reached": False}
    assert result["details"]["saved"] == 4 and len(detail_requests(requests)) == 4  # scan y detalles corrieron
    assert [item["title"] for item in result["plan"]["send"]] == ["Oferta vieja"]
    assert run.summary_lines(result)[2:4] == [NO_SCORE, "Gasto del mes: US$ 0,1000 de US$ 2,00"]
    text = run.write_report(result, META, tmp_path).read_text(encoding="utf-8")
    assert f"- {NO_SCORE}" in section(text, "Puntaje") and "No se corrió" not in section(text, "Puntaje")
    assert "## Freno" not in text and "Estado: completa" in text and "| Oferta vieja |" in section(text, "Plan (modo seco)")


def test_score_limit_0_with_a_client_does_not_call_it_either(conn, tmp_path):
    http, _ = make_site(HAPPY_SITE)
    llm = FakeLLM()
    result = go(conn, tmp_path, http, llm, score_limit=0)
    assert llm.calls == [] and result["score"] is None and result["score_requested"] is False and result["stopped"] is None
    assert NO_SCORE in run.summary_lines(result) and not any(l.startswith("Puntaje: puntuadas") for l in run.summary_lines(result))


def test_score_limit_0_still_reapplies_the_exclusions_so_the_plan_never_lists_an_excluded_company(conn, tmp_path):
    spend_before(conn, 0.1)  # «Oferta vieja», de «Empresa 99», con 90 puntos: se postularía
    http, _ = make_site({"python": [[1]]})
    profile = {**profile_dict(keywords_a=("python",), keywords_b=()), "exclude_companies": ["empresa  99"]}
    result = go(conn, tmp_path, http, None, score_limit=0, profile=profile)
    assert result["plan"]["send"] == [] and result["plan"]["skip"] == []
    assert conn.execute("SELECT status FROM offers WHERE id16 = ?", (f"{99:016X}",)).fetchone()[0] == "excluded"


def test_a_positive_score_limit_is_reported_as_requested(conn, tmp_path):
    result, _, _ = happy(conn, tmp_path)
    assert result["score_requested"] is True and NO_SCORE not in run.summary_lines(result)


def test_without_a_client_and_scoring_asked_scan_and_details_run_and_score_stops_with_the_key_message(conn, tmp_path):
    http, requests = make_site(HAPPY_SITE)
    result = go(conn, tmp_path, http, None)
    assert result["details"]["saved"] == 4 and len(detail_requests(requests)) == 4
    assert result["stopped"] == {"stage": "score", "message": NO_KEY, "scored": 0, "cost_usd": 0}
    assert result["score"] is None and result["plan"] is None and result["score_requested"] is True
    assert result["month"] == {"spent": 0.0, "cap": 2.0, "reached": False}
    assert run.summary_lines(result) == [
        "Scan: keywords: 2 · páginas leídas: 4 · encontradas: 5 · nuevas: 4 · ya vistas: 1", "Detalles: guardados: 4",
        "Gasto del mes: US$ 0,0000 de US$ 2,00"]
    text = run.write_report(result, META, tmp_path).read_text(encoding="utf-8")
    assert "frenó en la etapa score" in text and f"- Mensaje: {NO_KEY}" in section(text, "Freno")
    assert "Puntuadas y guardadas antes del freno: 0" in section(text, "Puntaje")


def test_without_a_client_the_stop_file_still_wins_over_the_missing_key(conn, tmp_path):
    http, _ = make_site(HAPPY_SITE)
    (tmp_path / "STOP").write_text("")
    result = go(conn, tmp_path, http, None)
    assert result["stopped"]["stage"] == "scan" and "STOP" in result["stopped"]["message"]


@pytest.mark.parametrize("score_limit", [20, 0])
def test_with_the_monthly_cap_reached_a_missing_client_is_not_a_problem(conn, tmp_path, score_limit):
    spend_before(conn, 1.5)
    http, _ = make_site(HAPPY_SITE)
    result = go(conn, tmp_path, http, None, profile={**profile_dict(), **CAP_ONE}, score_limit=score_limit)
    assert result["stopped"] is None and result["score"] is None and result["month"]["reached"] is True
    assert [item["title"] for item in result["plan"]["send"]] == ["Oferta vieja"]
    lines = run.summary_lines(result)
    assert "Tope mensual alcanzado: US$ 1,5000 de US$ 1,00" in lines and (NO_SCORE in lines) == (score_limit == 0)


def test_cli_run_profile_without_keywords_exits_1_and_asks_nothing(cli_env, tmp_path, capsys):
    (tmp_path / "examples").mkdir()
    for name in ("resume.example.yaml", "cv.example.pdf"):
        (tmp_path / "examples" / name).write_bytes((ROOT / "examples" / name).read_bytes())
    (tmp_path / "profile.yaml").write_text(
        "variants:\n  - id: backend\n    resume: examples/resume.example.yaml\n    pdf: examples/cv.example.pdf\n",
        encoding="utf-8")
    cli_env.install_site(EXAMPLE_SITE)
    assert cli.main(["run", "--profile", str(tmp_path / "profile.yaml"), "--db", str(tmp_path / "t.db")]) == 1
    assert "keywords" in capsys.readouterr().err
    assert cli_env.requests == [] and not (tmp_path / "data").exists()


def test_cli_run_defaults(monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "_run", lambda args: seen.append(args) or 0)
    assert cli.main(["run"]) == 0
    [args] = seen
    # Sin flags quedan en None: los valores salen de la sección `run` del perfil.
    assert (args.pages, args.max_details, args.score_limit, args.max_cost, args.live) == (None, None, None, None, False)
    assert args.profile == Path("profile.yaml") and args.db == Path("data/compuapply.db")


# ── sección `run` del perfil ─────────────────────────────────────────────────

def run_profile(tmp_path: Path, run_section: dict) -> Path:
    """El perfil de ejemplo (con rutas absolutas a sus archivos) y la sección `run` pedida."""
    data = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    for variant in data["variants"]:
        variant["resume"], variant["pdf"] = str(ROOT / variant["resume"]), str(ROOT / variant["pdf"])
    data["run"] = run_section
    path = tmp_path / "profile.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return path


def run_args(tmp_path: Path, profile_path: Path, *flags: str) -> list[str]:
    return ["run", "--profile", str(profile_path), "--db", str(tmp_path / "t.db"), *flags]


@pytest.mark.parametrize("section, observed, expected", [
    ({"pages": 1}, lambda env: len(listing_requests(env.requests)), 3),  # una página por keyword; con el default, 6
    ({"max_details": 1}, lambda env: len(detail_requests(env.requests)), 1),  # con el default se bajan las 4
    ({"score_limit": 1}, lambda env: len(env.llm.calls), 1),  # con el default se puntúan las 4
    ({"max_cost": 0.0001}, lambda env: len(env.llm.calls), 1),  # el primer puntaje ya pasa el tope
])
def test_cli_run_without_flags_takes_every_value_from_the_run_section(cli_env, tmp_path, capsys, section, observed, expected):
    cli_env.llm = FakeLLM(example_llm(80))
    cli_env.install_site(EXAMPLE_SITE)
    assert cli.main(run_args(tmp_path, run_profile(tmp_path, section))) == 0
    assert observed(cli_env) == expected
    assert ("cortó por tope de gasto" in capsys.readouterr().out) == ("max_cost" in section)  # no es un error: se avisa


def test_cli_run_without_a_run_section_uses_the_defaults(cli_env, tmp_path):
    cli_env.llm = FakeLLM(example_llm(80))
    cli_env.install_site(EXAMPLE_SITE)
    assert cli.main(["run", "--profile", str(EXAMPLE), "--db", str(tmp_path / "t.db")]) == 0
    assert len(listing_requests(cli_env.requests)) == 6 and len(detail_requests(cli_env.requests)) == 4
    assert len(cli_env.llm.calls) == 4


def test_cli_run_an_explicit_flag_wins_over_the_run_section(cli_env, tmp_path):
    profile_path = run_profile(tmp_path, {"pages": 1, "max_details": 1, "score_limit": 1, "max_cost": 0.0001})
    cli_env.llm = FakeLLM(example_llm(80))
    cli_env.install_site(EXAMPLE_SITE)
    assert cli.main(run_args(tmp_path, profile_path, "--pages", "2", "--max-details", "3", "--score-limit", "2",
                             "--max-cost", "5")) == 0
    assert len(listing_requests(cli_env.requests)) == 6 and len(detail_requests(cli_env.requests)) == 3
    assert len(cli_env.llm.calls) == 2


def test_cli_run_with_the_monthly_cap_reached_exits_0_says_so_and_still_writes_the_plan(cli_env, tmp_path, capsys):
    conn = db.connect(tmp_path / "t.db")
    spend_before(conn, 3.0)  # más que los 2.00 del perfil de ejemplo
    conn.close()
    cli_env.install_site(EXAMPLE_SITE)
    assert cli.main(cli_args(tmp_path)) == 0
    captured = capsys.readouterr()
    lines = captured.out.strip().splitlines()
    assert "Tope mensual alcanzado: US$ 3,0000 de US$ 2,00" in lines and not any(l.startswith("Puntaje") for l in lines)
    assert lines[-3].startswith("Plan (modo seco): Enviaría: 1") and lines[-2] == UNCALIBRATED and captured.err == ""
    assert cli_env.llm.calls == [] and len(detail_requests(cli_env.requests)) == 4  # lo demás corrió
    [report] = (tmp_path / "data" / "runs").glob("*-run.md")
    text = report.read_text(encoding="utf-8")
    assert "- Tope mensual alcanzado: US$ 3,0000 de US$ 2,00" in section(text, "Puntaje") and "## Freno" not in text


def test_cli_run_takes_the_monthly_cap_from_the_run_section(cli_env, tmp_path, capsys):
    conn = db.connect(tmp_path / "t.db")
    spend_before(conn, 0.5)
    conn.close()
    cli_env.install_site(EXAMPLE_SITE)
    assert cli.main(run_args(tmp_path, run_profile(tmp_path, {"max_cost_month": 0.4}))) == 0
    out = capsys.readouterr().out
    assert "Tope mensual alcanzado: US$ 0,5000 de US$ 0,40" in out and cli_env.llm.calls == []


@pytest.mark.parametrize("flag, details, calls", [("--max-details", 0, 0), ("--score-limit", 4, 0)])
def test_cli_run_an_explicit_zero_is_not_replaced_by_the_profile(cli_env, tmp_path, flag, details, calls):
    profile_path = run_profile(tmp_path, {"max_details": 5, "score_limit": 5})
    cli_env.llm = FakeLLM(example_llm(80))
    cli_env.install_site(EXAMPLE_SITE)
    assert cli.main(run_args(tmp_path, profile_path, flag, "0")) == 0
    assert (len(detail_requests(cli_env.requests)), len(cli_env.llm.calls)) == (details, calls)


@pytest.mark.parametrize("section, message", [
    ({"pages": 0}, r"'run\.pages'.*entero de 1 o más"),
    ({"max_details": -1}, r"'run\.max_details'.*entero de 0 o más"),
    ({"score_limit": "x"}, r"'run\.score_limit'.*entero de 0 o más"),
    ({"max_cost": 0}, r"'run\.max_cost'.*número mayor que 0"),
    ({"max_cost_month": 0}, r"'run\.max_cost_month'.*número mayor que 0"),
    ({"max_cost_month": "mucho"}, r"'run\.max_cost_month'.*número mayor que 0"),
])
def test_cli_run_invalid_run_section_exits_1_before_asking_anything(cli_env, tmp_path, capsys, section, message):
    cli_env.install_site(EXAMPLE_SITE)
    assert cli.main(run_args(tmp_path, run_profile(tmp_path, section))) == 1
    captured = capsys.readouterr()
    assert re.search(message, captured.err) and captured.out == ""
    assert cli_env.requests == [] and cli_env.llm.calls == [] and not (tmp_path / "data").exists()


# ── variantes sin PDF ────────────────────────────────────────────────────────

def test_report_no_longer_says_that_a_pdf_is_needed_to_apply(conn, tmp_path):
    http, _ = make_site({"python": [[1]]})
    result = go(conn, tmp_path, http, FakeLLM(), profile=profile_dict(keywords_a=("python",), keywords_b=()))
    text = run.write_report(result, {**META, "variants_without_pdf": ["backend", "data"]}, tmp_path / "a").read_text(encoding="utf-8")
    assert "sin PDF" not in text and "hace falta para postular" not in text


def test_cli_run_report_has_no_note_about_variants_without_pdf(cli_env, tmp_path, capsys):
    (tmp_path / "examples").mkdir()
    (tmp_path / "examples" / "resume.example.yaml").write_bytes((ROOT / "examples" / "resume.example.yaml").read_bytes())
    (tmp_path / "profile.yaml").write_text(
        "variants:\n  - id: backend\n    resume: examples/resume.example.yaml\n    keywords: [python]\n", encoding="utf-8")
    cli_env.llm = FakeLLM(example_llm(80))
    cli_env.install_site({"python": [[1, 2]]})
    assert cli.main(["run", "--pages", "1", "--profile", str(tmp_path / "profile.yaml"),
                     "--db", str(tmp_path / "t.db")]) == 0
    [report] = (tmp_path / "data" / "runs").glob("*-run.md")
    assert "sin PDF" not in report.read_text(encoding="utf-8")
    assert capsys.readouterr().out.splitlines()[0].startswith("Scan: keywords: 1")


def test_with_the_monthly_cap_reached_the_exclusions_are_still_reapplied_and_the_client_is_not_called(conn, tmp_path):
    spend_before(conn, 1.5)  # «Oferta vieja», de «Empresa 99», ya puntuada con 90: se postularía
    http, _ = make_site({"python": [[1]]})
    llm = FakeLLM()
    open_plan = go(conn, tmp_path, http, llm, profile={**profile_dict(keywords_a=("python",), keywords_b=()), **CAP_ONE})
    assert [item["title"] for item in open_plan["plan"]["send"]] == ["Oferta vieja"]  # sin exclusión, el plan la lista
    http, _ = make_site({"python": [[1]]})
    excluding = {**profile_dict(keywords_a=("python",), keywords_b=()), **CAP_ONE, "exclude_companies": ["empresa 99"]}
    result = go(conn, tmp_path, http, llm, profile=excluding)
    assert result["month"]["reached"] is True and result["plan"]["send"] == [] and result["plan"]["skip"] == []
    assert llm.calls == [] and conn.execute("SELECT status FROM offers WHERE id16 = ?", (f"{99:016X}",)).fetchone()[0] == "excluded"
    # y si después se quita la exclusión, vuelve a la cola (como en `score`)
    http, _ = make_site({"python": [[1]]})
    again = go(conn, tmp_path, http, llm, profile={**profile_dict(keywords_a=("python",), keywords_b=()), **CAP_ONE})
    assert [item["title"] for item in again["plan"]["send"]] == ["Oferta vieja"] and llm.calls == []


def test_plan_without_scores_adds_the_note_to_the_terminal_and_the_report(conn, tmp_path):
    http, _ = make_site(HAPPY_SITE)
    result = go(conn, tmp_path, http, FakeLLM(), score_limit=0)
    assert result["plan"]["scored"] == 0 and run.summary_lines(result)[-1] == NO_SCORES_NOTE
    text = run.write_report(result, META, tmp_path).read_text(encoding="utf-8")
    assert NO_SCORES_NOTE in section(text, "Plan (modo seco)")


def test_plan_with_scores_has_no_note(conn, tmp_path):
    result, _, _ = happy(conn, tmp_path)
    assert result["plan"]["scored"] == 3
    assert NO_SCORES_NOTE not in run.summary_lines(result)
    assert "Todavía no hay ofertas puntuadas" not in run.write_report(result, META, tmp_path).read_text(encoding="utf-8")


def test_with_the_monthly_cap_reached_a_repeated_offer_is_not_in_the_plan(conn, tmp_path):
    spend_before(conn, 1.5)
    copy = f"{98:016X}"  # la misma oferta publicada otra vez: ya puntuada, pero repetida
    db.upsert_offers(conn, [{"id16": copy, "data_id": data_id(98), "title": "Oferta vieja", "company": "EMPRESA  99",
                             "location": "Rosario", "url": f"https://ar.computrabajo.com/ofertas-de-trabajo/oferta-98-{data_id(98)}"}],
                     "python")
    db.save_detail(conn, copy, {"description": "Descripción vieja.", "date_posted": "", "valid_through": "", "employment_type": ""})
    db.save_score(conn, copy, "gpt-6-luna", "v5", {"best_variant": "backend", "score": 90, "all_scores": {"backend": 90},
                                                   "reasons": [], "missing": []}, (1, 0, 1), 0.0)
    http, _ = make_site({"python": [[1]]})
    llm = FakeLLM()
    result = go(conn, tmp_path, http, llm, profile={**profile_dict(keywords_a=("python",), keywords_b=()), **CAP_ONE})
    assert [item["id16"] for item in result["plan"]["send"]] == [f"{99:016X}"] and llm.calls == []
    assert conn.execute("SELECT status, dup_of FROM offers WHERE id16 = ?", (copy,)).fetchone() == ("duplicate", f"{99:016X}")


def test_report_adds_the_duplicates_line_only_when_there_are(conn, tmp_path):
    result, _, _ = happy(conn, tmp_path)
    assert "Duplicadas" not in section(run.write_report(result, META, tmp_path).read_text(encoding="utf-8"), "Puntaje")
    result["score"]["duplicates"] = 2
    assert "- Duplicadas: 2" in section(run.write_report(result, META, tmp_path).read_text(encoding="utf-8"), "Puntaje")
    assert "excluidas: 1 · duplicadas: 2 · con error: 0" in run.summary_lines(result)[2]


# ── TAREA-033: «sin detalle todavía» ─────────────────────────────────────────

def pending_run(conn, tmp_path, max_details):
    http, requests = make_site({"python": [[1, 2, 3, 5]], "backend": [[6, 7]]})
    result = go(conn, tmp_path, http, FakeLLM(llm_response(85)), max_details=max_details)
    return result


def test_run_says_how_many_offers_are_still_without_detail(conn, tmp_path):
    result = pending_run(conn, tmp_path, 3)
    assert result["details"]["saved"] == 3 and result["details"]["pending"] == 3
    lines = run.summary_lines(result)
    assert any(line.startswith("Detalles: guardados: 3") and line.endswith(" · sin detalle todavía: 3") for line in lines)
    report = run.write_report(result, META, tmp_path / "runs").read_text(encoding="utf-8")
    assert "- Sin detalle todavía: 3" in section(report, "Detalles")


def test_run_does_not_mention_pending_details_when_there_are_none(conn, tmp_path):
    result = pending_run(conn, tmp_path, 20)
    assert result["details"]["pending"] == 0
    assert all("sin detalle todavía" not in line for line in run.summary_lines(result))
    assert "Sin detalle todavía" not in run.write_report(result, META, tmp_path / "runs").read_text(encoding="utf-8")


def test_run_spreads_the_details_among_the_keywords(conn, tmp_path):
    http, requests = make_site({"python": [[1, 2, 3, 5]], "backend": [[6, 7]]})
    go(conn, tmp_path, http, FakeLLM(llm_response(85)), max_details=2)
    saved = {row[0] for row in conn.execute("SELECT keyword FROM offers WHERE detail_fetched_at IS NOT NULL")}
    assert saved == {"python", "backend"}  # una de cada keyword, no las dos primeras de python
