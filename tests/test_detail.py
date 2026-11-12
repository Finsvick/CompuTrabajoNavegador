"""Tests de parse_detail, fetch_details y del tramo de detalles del CLI. Sin red."""
import hashlib
import json
import re
import sqlite3
from pathlib import Path

import httpx
import pytest

from compuapply import cli, db, scan

FIXTURES = Path(__file__).parent / "fixtures"
DETAIL_REAL = (FIXTURES / "detail_real.html").read_text(encoding="utf-8")
DETAIL_CONFIDENTIAL = (FIXTURES / "detail_confidencial.html").read_text(encoding="utf-8")
PAGE1 = (FIXTURES / "listing_page1.html").read_text(encoding="utf-8")
PREFIX = "https://ar.computrabajo.com/ofertas-de-trabajo/"


@pytest.fixture(autouse=True)
def sleeps(monkeypatch):
    """Anula la pausa entre detalles y guarda cuánto se habría dormido."""
    calls = []
    monkeypatch.setattr(scan.time, "sleep", calls.append)
    return calls


def ld_json_page(graph: list[dict]) -> str:
    data = {"@context": "https://schema.org", "@graph": graph}
    return f'<html><head><script type="application/ld+json">{json.dumps(data)}</script></head></html>'


def posting_page(description: str = "Buscamos analista.<br/>Requisitos: Python") -> str:
    return ld_json_page([
        {"@type": "WebPage", "description": "no es esta"},
        {"@type": "JobPosting", "description": description, "datePosted": "2026-09-30",
         "validThrough": "2026-11-29", "employmentType": "FULL_TIME"},
    ])


def confidential_page(*blocks: str, graph: list[dict] | None = None) -> str:
    """Detalle de empresa confidencial: ld+json sin `JobPosting` y la descripción en `div[div-link="oferta"]`."""
    data = {"@context": "https://schema.org",
            "@graph": graph if graph is not None else [{"@type": "Organization"}, {"@type": "WebPage"}, {"@type": "ItemList"}]}
    return (f'<html><head><script type="application/ld+json">{json.dumps(data)}</script></head><body><h1>Oferta</h1>'
            f'<div div-link="oferta">{"".join(blocks)}</div></body></html>')


# Detalle que responde 200 pero sin JobPosting ni bloque de la oferta.
BARE_PAGE = ld_json_page([{"@type": "WebPage"}])


def offer(n: int, url: str | None = None) -> dict:
    return {
        "id16": f"{n:016X}", "data_id": f"{n:032X}", "title": f"Oferta {n}", "company": "Empresa Falsa SA",
        "location": "Rosario, Santa Fe", "url": f"{PREFIX}oferta-{n}-{n:032X}" if url is None else url,
    }


def seeded(tmp_path, offers: list[dict]):
    conn = db.connect(tmp_path / "t.db")
    db.upsert_offers(conn, offers, "programador")
    return conn


def pending(conn) -> list[str]:
    return [r[0] for r in conn.execute("SELECT id16 FROM offers WHERE detail_fetched_at IS NULL ORDER BY rowid")]


def client_for(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def ok_client(requests: list) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(200, text=posting_page())
    return client_for(handler)


def sequence_client(requests: list, *pages: str) -> httpx.Client:
    """Responde 200 con `pages` en orden de pedido y, después, con un detalle bueno."""
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(200, text=pages[len(requests) - 1] if len(requests) <= len(pages) else posting_page())
    return client_for(handler)


# ── parse_detail ─────────────────────────────────────────────────────────────

def test_parse_detail_real_fixture():
    detail = scan.parse_detail(DETAIL_REAL)
    assert "Power BI" in detail["description"] and "Requisitos" in detail["description"]
    assert "<br" not in detail["description"]
    assert "\n\n\n" not in detail["description"]
    assert all(line == line.strip() for line in detail["description"].splitlines())
    assert detail["date_posted"] == "2026-09-30"
    assert detail["valid_through"] == "2026-11-29"
    assert detail["employment_type"] == "FULL_TIME"


def test_parse_detail_real_fixture_is_exactly_what_it_was_before_confidential_offers():
    detail = scan.parse_detail(DETAIL_REAL)
    assert len(detail["description"]) == 1913
    assert hashlib.sha256(detail["description"].encode("utf-8")).hexdigest() == (
        "4fefdb82a6f340ce34e9bcc67852e7700d951e91189681377b052879d75f7dbd")  # sacado del parser anterior
    assert (detail["date_posted"], detail["valid_through"], detail["employment_type"]) == (
        "2026-09-30", "2026-11-29", "FULL_TIME")


def test_parse_detail_confidential_fixture_takes_the_description_from_the_html():
    detail = scan.parse_detail(DETAIL_CONFIDENTIAL)
    description = detail["description"]
    assert "Full Stack Developer" in description and "Jornada completa" in description
    assert description.splitlines()[0] != "Descripción de la oferta"
    assert (detail["date_posted"], detail["valid_through"], detail["employment_type"]) == ("", "", "")
    assert "<" not in description and "\n\n\n" not in description
    assert all(line == line.strip() for line in description.splitlines())


def test_parse_detail_html_block_is_cleaned_like_the_json_ld_one():
    page = confidential_page('<h2>Descripción de la oferta</h2><span> A convenir </span>', '<p class="mbB">Texto<br>con salto</p>',
                             " \n \n ", "<ul><li>Uno</li><li>Dos</li></ul>")  # los blancos de en medio quedan en una línea vacía
    assert scan.parse_detail(page) == {
        "description": "A convenir\nTexto\ncon salto\n\nUno\nDos", "date_posted": "", "valid_through": "",
        "employment_type": ""}


@pytest.mark.parametrize("first_block, expected", [
    ("<h2>Descripción de la oferta</h2><p>Texto</p>", "Texto"),  # el encabezado es la primera línea: se saca
    ("<h2>Otro título</h2><p>Texto</p>", "Otro título\nTexto"),  # otro encabezado: se queda
    ("<p>Texto</p><h2>Descripción de la oferta</h2>", "Texto\nDescripción de la oferta"),  # no es la primera línea
    ("<h2>Descripción de la oferta y más</h2><p>Texto</p>", "Descripción de la oferta y más\nTexto"),  # no es igual
])
def test_parse_detail_drops_the_heading_only_when_it_is_the_first_line(first_block, expected):
    assert scan.parse_detail(confidential_page(first_block))["description"] == expected


@pytest.mark.parametrize("html", [
    confidential_page("<h2>Descripción de la oferta</h2><p>Texto</p>").replace('<script type="application/ld+json">', "<!--").replace("</script>", "-->"),  # sin ld+json
    confidential_page("<p>Texto</p>").replace('"@graph"', "{rota"),  # JSON roto
    confidential_page("<p>Texto</p>", graph=[{"@type": "Organization"}]),  # sin JobPosting
    confidential_page("<p>Texto</p>", graph=[{"@type": "JobPosting", "description": "  <br/> ", "datePosted": "2026-09-30"}]),
    confidential_page("<p>Texto</p>", graph=[{"@type": "JobPosting"}]),  # JobPosting sin descripción
])
def test_parse_detail_falls_back_to_the_html_block(html):
    detail = scan.parse_detail(html)
    assert detail["description"].endswith("Texto")
    assert (detail["date_posted"], detail["valid_through"], detail["employment_type"]) == ("", "", "")


def test_parse_detail_prefers_the_job_posting_over_the_html_block():
    page = confidential_page("<p>Texto del bloque</p>", graph=[
        {"@type": "JobPosting", "description": "Texto del JSON", "datePosted": "2026-09-30", "employmentType": "FULL_TIME"}])
    assert scan.parse_detail(page) == {
        "description": "Texto del JSON", "date_posted": "2026-09-30", "valid_through": "", "employment_type": "FULL_TIME"}


def test_parse_detail_cleans_description_lines():
    detail = scan.parse_detail(posting_page("  Uno  <br/> <br/>\n\n <br/>  Dos<br/>"))
    assert detail["description"] == "Uno\n\nDos"


def test_parse_detail_optional_fields_can_be_empty():
    page = ld_json_page([{"@type": "JobPosting", "description": "Solo texto"}])
    assert scan.parse_detail(page) == {
        "description": "Solo texto", "date_posted": "", "valid_through": "", "employment_type": ""}


@pytest.mark.parametrize("html", [
    "<html><body>sin datos</body></html>",
    '<script type="application/ld+json">{esto no es json</script>',
    ld_json_page([{"@type": "WebPage", "description": "no es una oferta"}]),
    ld_json_page([{"@type": "JobPosting", "description": "  <br/> "}]),
    ld_json_page([{"@type": "JobPosting"}]),
    "<html><body><div div-link=\"oferta\"></div></body></html>",  # el bloque existe pero está vacío
    "<html><body><div div-link=\"oferta\">  <br/>\n </div></body></html>",
    confidential_page("<h2>Descripción de la oferta</h2>"),  # solo el encabezado
    '<html><body><div div-link="otra">Texto</div></body></html>',  # otro div: no es el bloque de la oferta
])
def test_parse_detail_without_description_anywhere_raises_detail_missing(html):
    with pytest.raises(scan.DetailMissing, match="posible cambio de layout"):
        scan.parse_detail(html)


def test_detail_missing_is_a_runtime_error():
    assert issubclass(scan.DetailMissing, RuntimeError)


# ── fetch_details ────────────────────────────────────────────────────────────

def test_fetch_details_respects_limit_and_resumes(tmp_path):
    conn = seeded(tmp_path, [offer(1), offer(2), offer(3)])
    requests = []
    assert scan.fetch_details(conn, 2, client=ok_client(requests), stop_file=tmp_path / "STOP") == 2
    assert requests == [offer(1)["url"], offer(2)["url"]]
    assert pending(conn) == [offer(3)["id16"]]
    requests.clear()
    assert scan.fetch_details(conn, 2, client=ok_client(requests), stop_file=tmp_path / "STOP") == 1
    assert requests == [offer(3)["url"]]
    description, posted = conn.execute("SELECT description, date_posted FROM offers").fetchone()
    assert description == "Buscamos analista.\nRequisitos: Python" and posted == "2026-09-30"


def test_fetch_details_sleeps_before_each_detail_including_first(tmp_path, monkeypatch):
    conn = seeded(tmp_path, [offer(1), offer(2), offer(3)])
    events = []
    monkeypatch.setattr(scan.time, "sleep", lambda s: events.append(("sleep", s)))
    client = client_for(lambda r: events.append(("request", str(r.url))) or httpx.Response(200, text=posting_page()))
    scan.fetch_details(conn, 3, client=client, stop_file=tmp_path / "STOP")
    assert [kind for kind, _ in events] == ["sleep", "request"] * 3
    assert all(1.0 <= value <= 2.5 for kind, value in events if kind == "sleep")


def test_fetch_details_limit_zero_requests_nothing(tmp_path):
    conn = seeded(tmp_path, [offer(1)])
    requests = []
    assert scan.fetch_details(conn, 0, client=ok_client(requests), stop_file=tmp_path / "STOP") == 0
    assert requests == []


def test_fetch_details_rejects_negative_limit(tmp_path):
    conn = seeded(tmp_path, [offer(1)])
    with pytest.raises(RuntimeError, match="negativo"):
        scan.fetch_details(conn, -1, client=ok_client([]), stop_file=tmp_path / "STOP")


def test_fetch_details_429_keeps_first_and_stops(tmp_path):
    conn = seeded(tmp_path, [offer(1), offer(2), offer(3)])
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if len(requests) == 2:
            return httpx.Response(429, text="")
        return httpx.Response(200, text=posting_page())

    with pytest.raises(RuntimeError, match="429"):
        scan.fetch_details(conn, 3, client=client_for(handler), stop_file=tmp_path / "STOP")
    assert requests == [offer(1)["url"], offer(2)["url"]]  # una sola request al segundo, ninguna al tercero
    assert pending(conn) == [offer(2)["id16"], offer(3)["id16"]]


def test_fetch_details_pages_without_anything_only_stop_the_run_on_the_third_in_a_row(tmp_path):
    conn = seeded(tmp_path, [offer(1), offer(2), offer(3), offer(4)])
    requests = []
    client = client_for(lambda r: requests.append(r) or httpx.Response(200, text="<html></html>"))
    with pytest.raises(RuntimeError, match="layout"):
        scan.fetch_details(conn, 4, client=client, stop_file=tmp_path / "STOP")
    assert len(requests) == 3 and statuses(conn) == ["no_detail", "no_detail", "no_detail", "new"]


def test_fetch_details_does_not_follow_redirects_and_marks_the_offer_gone(tmp_path):
    conn = seeded(tmp_path, [offer(1)])
    requests = []
    client = client_for(lambda r: requests.append(r) or httpx.Response(
        302, headers={"Location": "https://candidato.ar.computrabajo.com/"}))
    assert scan.fetch_details(conn, 1, client=client, stop_file=tmp_path / "STOP") == 0
    assert len(requests) == 1 and statuses(conn) == ["gone"]


def redirect_then_ok(requests, *codes):
    def handler(request):
        requests.append(str(request.url))
        code = codes[len(requests) - 1] if len(requests) <= len(codes) else 200
        return httpx.Response(code, text=posting_page(), headers={"Location": "https://ar.computrabajo.com/"} if code != 200 else {})
    return client_for(handler)


def test_a_301_detail_is_gone_and_the_scan_goes_on_without_counting_for_the_limit(tmp_path):
    conn = seeded(tmp_path, [offer(1), offer(2), offer(3)])
    requests = []
    assert scan.fetch_details(conn, 2, client=redirect_then_ok(requests, 301, 200), stop_file=tmp_path / "STOP") == 2
    assert len(requests) == 3 and statuses(conn) == ["gone", "new", "new"]


def test_three_redirects_in_a_row_halt_but_a_good_detail_resets_the_count(tmp_path):
    conn = seeded(tmp_path, [offer(n) for n in range(1, 6)])
    with pytest.raises(RuntimeError, match="posible bloqueo o cambio del sitio"):
        scan.fetch_details(conn, 10, client=redirect_then_ok([], 301, 302, 301), stop_file=tmp_path / "STOP")
    assert statuses(conn) == ["gone", "gone", "gone", "new", "new"]


def test_two_redirects_a_good_one_and_a_redirect_do_not_halt(tmp_path):
    conn = seeded(tmp_path, [offer(n) for n in range(1, 6)])
    assert scan.fetch_details(conn, 10, client=redirect_then_ok([], 301, 301, 200, 302), stop_file=tmp_path / "STOP") == 2
    assert statuses(conn).count("gone") == 3 and statuses(conn).count("new") == 2


def test_a_gone_offer_is_not_asked_again(tmp_path):
    conn = seeded(tmp_path, [offer(1)])
    scan.fetch_details(conn, 1, client=redirect_then_ok([], 301), stop_file=tmp_path / "STOP")
    requests = []
    assert scan.fetch_details(conn, 1, client=redirect_then_ok(requests), stop_file=tmp_path / "STOP") == 0 and requests == []


BAD_URLS = [
    "https://candidato.ar.computrabajo.com/candidate/apply/?oi=ABC",
    "https://ar.computrabajo.com/trabajo-de-programador?p=1",
    "http://ar.computrabajo.com/ofertas-de-trabajo/oferta-x",
    "",
]


def statuses(conn) -> list[str]:
    return [r[0] for r in conn.execute("SELECT status FROM offers ORDER BY rowid")]


@pytest.mark.parametrize("url", BAD_URLS)
def test_fetch_details_skips_bad_url_and_continues(tmp_path, url):
    conn = seeded(tmp_path, [offer(1, url=url), offer(2)])
    requests = []
    stop = tmp_path / "STOP"
    assert scan.fetch_details(conn, 5, client=ok_client(requests), stop_file=stop) == 1
    assert requests == [offer(2)["url"]]  # la mala no se pide
    assert statuses(conn) == ["bad_url", "new"]
    assert pending(conn) == [offer(1)["id16"]]  # la mala no tiene detalle...
    requests.clear()
    assert scan.fetch_details(conn, 5, client=ok_client(requests), stop_file=stop) == 0
    assert requests == []  # ...pero tampoco vuelve a la cola


def test_fetch_details_skipped_offer_has_no_sleep_and_does_not_use_limit(tmp_path, sleeps):
    conn = seeded(tmp_path, [offer(1, url=""), offer(2)])
    assert scan.fetch_details(conn, 1, client=ok_client([]), stop_file=tmp_path / "STOP") == 1
    assert len(sleeps) == 1  # solo la oferta que hace request


def test_fetch_details_three_bad_urls_in_a_row_stop_the_run(tmp_path):
    conn = seeded(tmp_path, [offer(1, url=""), offer(2, url=""), offer(3, url=""), offer(4)])
    requests = []
    with pytest.raises(RuntimeError, match="3 URLs inválidas seguidas"):
        scan.fetch_details(conn, 5, client=ok_client(requests), stop_file=tmp_path / "STOP")
    assert requests == []
    assert statuses(conn) == ["bad_url", "bad_url", "bad_url", "new"]


def test_fetch_details_good_offer_resets_bad_url_counter(tmp_path):
    conn = seeded(tmp_path, [offer(1, url=""), offer(2, url=""), offer(3), offer(4, url=""), offer(5, url=""), offer(6)])
    assert scan.fetch_details(conn, 10, client=ok_client([]), stop_file=tmp_path / "STOP") == 2
    assert statuses(conn) == ["bad_url", "bad_url", "new", "bad_url", "bad_url", "new"]


def test_fetch_details_marks_no_detail_commits_it_and_never_asks_again(tmp_path):
    conn = seeded(tmp_path, [offer(1), offer(2), offer(3)])
    requests = []
    stop = tmp_path / "STOP"
    assert scan.fetch_details(conn, 10, client=sequence_client(requests, posting_page(), BARE_PAGE), stop_file=stop) == 2
    assert len(requests) == 3 and statuses(conn) == ["new", "no_detail", "new"]  # sin freno
    assert pending(conn) == [offer(2)["id16"]]  # sin detalle...
    other = sqlite3.connect(tmp_path / "t.db")  # ...y marcada de verdad en la base, no solo en esta conexión
    try:
        assert other.execute("SELECT status, description, detail_fetched_at FROM offers WHERE id16 = ?",
                             (offer(2)["id16"],)).fetchone() == ("no_detail", None, None)
    finally:
        other.close()
    requests.clear()
    assert scan.fetch_details(conn, 10, client=sequence_client(requests), stop_file=stop) == 0
    assert requests == []  # ...pero tampoco vuelve a la cola


def test_fetch_details_no_detail_does_not_spend_the_limit(tmp_path):
    conn = seeded(tmp_path, [offer(1), offer(2), offer(3), offer(4)])
    requests = []
    client = sequence_client(requests, posting_page(), BARE_PAGE)
    assert scan.fetch_details(conn, 2, client=client, stop_file=tmp_path / "STOP") == 2
    assert len(requests) == 3  # la 4 no se pide: ya guardó 2
    assert statuses(conn) == ["new", "no_detail", "new", "new"] and pending(conn) == [offer(2)["id16"], offer(4)["id16"]]


def test_fetch_details_saves_a_confidential_offer_from_the_html_block(tmp_path):
    conn = seeded(tmp_path, [offer(1), offer(2), offer(3)])
    client = sequence_client([], posting_page(), DETAIL_CONFIDENTIAL)
    assert scan.fetch_details(conn, 10, client=client, stop_file=tmp_path / "STOP") == 3
    description, posted, kind, status, fetched = conn.execute(
        "SELECT description, date_posted, employment_type, status, detail_fetched_at FROM offers WHERE id16 = ?",
        (offer(2)["id16"],)).fetchone()
    assert "Full Stack Developer" in description and (posted, kind, status) == ("", "", "new") and fetched


def test_fetch_details_three_without_description_in_a_row_stop_the_run(tmp_path):
    conn = seeded(tmp_path, [offer(1), offer(2), offer(3), offer(4)])
    requests = []
    client = sequence_client(requests, BARE_PAGE, BARE_PAGE, BARE_PAGE)
    with pytest.raises(RuntimeError, match="3 ofertas seguidas sin descripción: posible cambio de layout"):
        scan.fetch_details(conn, 10, client=client, stop_file=tmp_path / "STOP")
    assert len(requests) == 3 and statuses(conn) == ["no_detail", "no_detail", "no_detail", "new"]


def test_fetch_details_good_offer_resets_the_no_detail_counter(tmp_path):
    conn = seeded(tmp_path, [offer(n) for n in range(1, 7)])
    client = sequence_client([], BARE_PAGE, BARE_PAGE, posting_page(), BARE_PAGE, BARE_PAGE, posting_page())
    assert scan.fetch_details(conn, 10, client=client, stop_file=tmp_path / "STOP") == 2
    assert statuses(conn) == ["no_detail", "no_detail", "new", "no_detail", "no_detail", "new"]


def test_fetch_details_bad_url_and_no_detail_counters_are_independent(tmp_path):
    conn = seeded(tmp_path, [offer(1, url=""), offer(2), offer(3, url=""), offer(4), offer(5)])
    client = sequence_client([], BARE_PAGE, BARE_PAGE, posting_page())
    assert scan.fetch_details(conn, 10, client=client, stop_file=tmp_path / "STOP") == 1
    assert statuses(conn) == ["bad_url", "no_detail", "bad_url", "no_detail", "new"]  # dos de cada una: ninguna llega a 3


def test_fetch_details_http_error_after_a_no_detail_still_stops_and_keeps_it(tmp_path):
    conn = seeded(tmp_path, [offer(1), offer(2), offer(3)])
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, text=BARE_PAGE) if len(requests) == 1 else httpx.Response(429, text="")

    with pytest.raises(RuntimeError, match="429"):
        scan.fetch_details(conn, 5, client=client_for(handler), stop_file=tmp_path / "STOP")
    assert len(requests) == 2 and statuses(conn) == ["no_detail", "new", "new"]


def test_fetch_details_stop_file_still_halts_after_a_no_detail(tmp_path, monkeypatch):
    conn = seeded(tmp_path, [offer(1), offer(2)])
    stop = tmp_path / "STOP"
    sleeps_made = []
    monkeypatch.setattr(scan.time, "sleep", lambda s: sleeps_made.append(s) or (len(sleeps_made) == 2 and stop.write_text("")))
    requests = []
    with pytest.raises(RuntimeError, match="STOP"):
        scan.fetch_details(conn, 5, client=sequence_client(requests, BARE_PAGE), stop_file=stop)
    assert len(requests) == 1 and statuses(conn) == ["no_detail", "new"]


def test_fetch_details_ignores_non_new_status(tmp_path):
    conn = seeded(tmp_path, [offer(1), offer(2)])
    db.mark_status(conn, offer(1)["id16"], "skipped")
    requests = []
    assert scan.fetch_details(conn, 5, client=ok_client(requests), stop_file=tmp_path / "STOP") == 1
    assert requests == [offer(2)["url"]]


def test_fetch_details_stop_file_halts_before_request(tmp_path):
    conn = seeded(tmp_path, [offer(1), offer(2)])
    stop = tmp_path / "STOP"
    stop.write_text("")
    requests = []
    with pytest.raises(RuntimeError, match="STOP"):
        scan.fetch_details(conn, 2, client=ok_client(requests), stop_file=stop)
    assert requests == []


def test_fetch_details_stop_file_midrun_keeps_first(tmp_path, monkeypatch):
    conn = seeded(tmp_path, [offer(1), offer(2)])
    stop = tmp_path / "STOP"
    sleeps_made = []
    # El STOP aparece durante la segunda pausa: el primer detalle ya estaba bajado.
    monkeypatch.setattr(scan.time, "sleep", lambda s: sleeps_made.append(s) or (len(sleeps_made) == 2 and stop.write_text("")))
    requests = []
    with pytest.raises(RuntimeError, match="STOP"):
        scan.fetch_details(conn, 2, client=ok_client(requests), stop_file=stop)
    assert len(requests) == 1 and pending(conn) == [offer(2)["id16"]]


# ── CLI ──────────────────────────────────────────────────────────────────────

def cli_args(tmp_path, *extra: str) -> list[str]:
    return ["scan", "--keyword", "x", "--pages", "1", "--db", str(tmp_path / "t.db"), "--profile", str(tmp_path / "sin-perfil.yaml"), *extra]


def patch_network(monkeypatch, tmp_path, detail_handler):
    """collect sirve PAGE1; fetch_details usa `detail_handler` y un STOP que no existe."""
    real_collect, real_fetch = scan.collect, scan.fetch_details
    listing = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=PAGE1)))
    monkeypatch.setattr(scan, "collect", lambda kw, pages, zones=(): real_collect(
        kw, pages, client=listing, stop_file=tmp_path / "STOP", zones=zones))
    monkeypatch.setattr(scan, "fetch_details", lambda conn, limit, zones=(): real_fetch(
        conn, limit, client=client_for(detail_handler), stop_file=tmp_path / "STOP", zones=zones))


def test_cli_downloads_details_up_to_max(monkeypatch, tmp_path, capsys):
    patch_network(monkeypatch, tmp_path, lambda r: httpx.Response(200, text=posting_page()))
    assert cli.main(cli_args(tmp_path, "--max-details", "5")) == 0
    assert capsys.readouterr().out.strip() == (
        "Páginas leídas: 1 · encontradas: 20 · nuevas: 20 · ya vistas: 0 · detalles: 5")
    assert cli.main(cli_args(tmp_path, "--max-details", "0")) == 0  # 0 = no baja nada
    assert "ya vistas: 20 · detalles: 0" in capsys.readouterr().out
    assert len(pending(db.connect(tmp_path / "t.db"))) == 15


def test_cli_reports_bad_url_only_when_there_are_some(monkeypatch, tmp_path, capsys):
    conn = db.connect(tmp_path / "t.db")
    db.upsert_offers(conn, [offer(98, url=""), offer(99, url="https://otro.sitio/x")], "x")
    conn.execute("UPDATE offers SET first_seen = '9999-12-31T00:00:00+00:00'")  # las más nuevas de su keyword: van primero
    conn.commit()
    conn.close()
    patch_network(monkeypatch, tmp_path, lambda r: httpx.Response(200, text=posting_page()))
    assert cli.main(cli_args(tmp_path, "--max-details", "2")) == 0
    assert capsys.readouterr().out.strip().endswith("· detalles: 2 · URL inválida: 2")


def first_bare_then_ok(calls: list):
    """Handler de detalles: el primero no trae descripción; los demás, un `JobPosting`."""
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, text=BARE_PAGE if len(calls) == 1 else posting_page())
    return handler


def test_cli_reports_no_detail_only_when_there_are_some(monkeypatch, tmp_path, capsys):
    patch_network(monkeypatch, tmp_path, first_bare_then_ok([]))
    assert cli.main(cli_args(tmp_path, "--max-details", "3")) == 0
    assert capsys.readouterr().out.strip() == (
        "Páginas leídas: 1 · encontradas: 20 · nuevas: 20 · ya vistas: 0 · detalles: 3 · sin descripción: 1")
    assert cli.main(cli_args(tmp_path, "--max-details", "2")) == 0  # las 3 de antes ya están: ninguna sin descripción nueva
    assert "sin descripción" not in capsys.readouterr().out


def test_cli_reports_bad_url_before_no_detail(monkeypatch, tmp_path, capsys):
    conn = db.connect(tmp_path / "t.db")
    db.upsert_offers(conn, [offer(98, url=""), offer(99, url="https://otro.sitio/x")], "x")
    conn.execute("UPDATE offers SET first_seen = '9999-12-31T00:00:00+00:00'")  # las más nuevas de su keyword: van primero
    conn.commit()
    conn.close()
    patch_network(monkeypatch, tmp_path, first_bare_then_ok([]))
    assert cli.main(cli_args(tmp_path, "--max-details", "2")) == 0
    assert capsys.readouterr().out.strip().endswith("· detalles: 2 · URL inválida: 2 · sin descripción: 1")


def test_cli_three_without_description_exit_1_and_report_them(monkeypatch, tmp_path, capsys):
    patch_network(monkeypatch, tmp_path, lambda r: httpx.Response(200, text=BARE_PAGE))
    assert cli.main(cli_args(tmp_path)) == 1
    err = capsys.readouterr().err
    assert "Error: 3 ofertas seguidas sin descripción: posible cambio de layout" in err
    assert "Detalles guardados antes del error: 0 · sin descripción: 3" in err


def test_cli_three_bad_urls_exit_1_and_report_them(monkeypatch, tmp_path, capsys):
    conn = db.connect(tmp_path / "t.db")
    db.upsert_offers(conn, [offer(n, url="") for n in (97, 98, 99)], "x")
    conn.execute("UPDATE offers SET first_seen = '9999-12-31T00:00:00+00:00'")  # las más nuevas de su keyword: van primero
    conn.commit()
    conn.close()
    patch_network(monkeypatch, tmp_path, lambda r: httpx.Response(200, text=posting_page()))
    assert cli.main(cli_args(tmp_path)) == 1
    err = capsys.readouterr().err
    assert "3 URLs inválidas seguidas" in err and "Detalles guardados antes del error: 0 · URL inválida: 3" in err


def test_cli_details_error_reports_saved_and_exits_1(monkeypatch, tmp_path, capsys):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(429 if len(calls) == 2 else 200, text=posting_page())

    patch_network(monkeypatch, tmp_path, handler)
    assert cli.main(cli_args(tmp_path)) == 1
    captured = capsys.readouterr()
    assert "429" in captured.err and "Detalles guardados antes del error: 1" in captured.err
    assert len(pending(db.connect(tmp_path / "t.db"))) == 19


# ── TAREA-033: el límite de detalles se reparte entre las keywords ───────────

def seeded_by_keyword(tmp_path, per_keyword: dict[str, list[int]], first_seen: dict[int, str] | None = None):
    conn = db.connect(tmp_path / "t.db")
    for keyword, numbers in per_keyword.items():
        db.upsert_offers(conn, [offer(n) for n in numbers], keyword)
    for n, when in (first_seen or {}).items():
        conn.execute("UPDATE offers SET first_seen = ? WHERE id16 = ?", (when, f"{n:016X}"))
    conn.commit()
    return conn


def fetched(requests: list) -> list[int]:
    return [int(re.search(r"oferta-(\d+)-", url).group(1)) for url in requests]


def test_fetch_details_splits_the_limit_among_the_keywords_newest_first(tmp_path):
    groups = {"dev": list(range(1, 11)), "ventas": list(range(11, 21)), "": list(range(21, 31))}
    # dentro de cada keyword, el first_seen más nuevo es el número más alto
    conn = seeded_by_keyword(tmp_path, groups, {n: f"2026-11-02T10:{n:02d}:00+00:00" for n in range(1, 31)})
    requests = []
    assert scan.fetch_details(conn, 6, client=ok_client(requests), stop_file=tmp_path / "STOP") == 6
    got = fetched(requests)
    assert sorted(got) == [9, 10, 19, 20, 29, 30]  # 2 de cada una, las más nuevas
    assert {n for n in got if n <= 10} == {9, 10} and {n for n in got if 11 <= n <= 20} == {19, 20} and {n for n in got if n > 20} == {29, 30}


def test_fetch_details_goes_one_offer_per_keyword_per_round(tmp_path):
    conn = seeded_by_keyword(tmp_path, {"dev": [1, 2, 3], "ventas": [4, 5, 6]},
                             {n: f"2026-11-02T10:{n:02d}:00+00:00" for n in range(1, 7)})
    requests = []
    scan.fetch_details(conn, 4, client=ok_client(requests), stop_file=tmp_path / "STOP")
    got = fetched(requests)
    assert [n <= 3 for n in got] in ([True, False, True, False], [False, True, False, True])  # una de cada una por vuelta
    assert set(got) == {3, 2, 6, 5}


def test_fetch_details_gives_the_leftover_to_the_keywords_that_still_have_pending(tmp_path):
    conn = seeded_by_keyword(tmp_path, {"dev": [1], "ventas": [2, 3, 4, 5]})
    requests = []
    assert scan.fetch_details(conn, 4, client=ok_client(requests), stop_file=tmp_path / "STOP") == 4
    assert len(set(fetched(requests))) == 4 and 1 in fetched(requests)


def test_fetch_details_keywords_compare_without_case_or_extra_spaces(tmp_path):
    conn = seeded_by_keyword(tmp_path, {"Dev  Web": [1, 2], "dev web": [3, 4], "ventas": [5, 6]})
    requests = []
    scan.fetch_details(conn, 2, client=ok_client(requests), stop_file=tmp_path / "STOP")
    assert sorted(n > 4 for n in fetched(requests)) == [False, True]  # dos keywords: una oferta de cada una


def test_fetch_details_bad_urls_do_not_use_the_turn_of_the_limit(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.upsert_offers(conn, [offer(1, url=""), offer(2)], "dev")
    db.upsert_offers(conn, [offer(3), offer(4)], "ventas")
    conn.commit()
    requests = []
    assert scan.fetch_details(conn, 2, client=ok_client(requests), stop_file=tmp_path / "STOP") == 2
    assert sorted(fetched(requests)) == [3, 4]  # ventas (la más nueva) va primera; la URL inválida no cuenta para el límite
    assert conn.execute("SELECT status FROM offers WHERE id16 = ?", (f"{1:016X}",)).fetchone()[0] == "bad_url"
