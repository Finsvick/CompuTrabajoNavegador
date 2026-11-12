"""Tests de scan.py y del comando `scan`. Sin red: httpx.MockTransport y HTML guardado."""
import re
from pathlib import Path

import httpx
import pytest

from compuapply import cli, scan

FIXTURES = Path(__file__).parent / "fixtures"
PAGE1 = (FIXTURES / "listing_page1.html").read_text(encoding="utf-8")
EMPTY = (FIXTURES / "listing_empty.html").read_text(encoding="utf-8")
RATING_CARD = (FIXTURES / "listing_rating_card.html").read_text(encoding="utf-8")

FULL_ID = "01C717D17245C0A161373E686DCF3405"


@pytest.fixture(autouse=True)
def sleeps(monkeypatch):
    """Anula la pausa entre páginas y guarda cuánto se habría dormido."""
    calls = []
    monkeypatch.setattr(scan.time, "sleep", calls.append)
    return calls


def make_client(pages: dict[int, str], requests: list | None = None) -> httpx.Client:
    """Cliente falso: devuelve pages[n] para ?p=n, o el listado vacío si no está."""
    def handler(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(request)
        return httpx.Response(200, text=pages.get(int(request.url.params["p"]), EMPTY))
    return httpx.Client(transport=httpx.MockTransport(handler))


def status_client(status: int, requests: list) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status, text="")
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_slug():
    assert scan.slug("Analista Programador") == "analista-programador"
    assert scan.slug("Técnico en Electrónica") == "tecnico-en-electronica"
    assert scan.slug("  C++ / .NET ") == "c-net"


def test_search_url():
    assert scan.search_url("programador", 2) == "https://ar.computrabajo.com/trabajo-de-programador?p=2"


def test_parse_listing_page1():
    offers = scan.parse_listing(PAGE1)
    assert len(offers) == 20
    for o in offers:
        assert len(o["id16"]) == 16 and o["data_id"].startswith(o["id16"])
        assert o["title"]
        assert o["url"].startswith("https://ar.computrabajo.com/") and "#" not in o["url"]
        assert not re.fullmatch(r"\d,\d", o["location"]), f"rating como ubicación: {o['location']!r}"
    first = offers[0]
    assert first["data_id"] == FULL_ID
    assert "Desarrollador Backend" in first["title"]
    assert first["company"].startswith("Lectus")
    assert first["location"]


def test_parse_listing_rating_card():
    [offer] = scan.parse_listing(RATING_CARD)
    assert offer["location"] == "Zárate, Buenos Aires"


def test_parse_listing_empty():
    assert scan.parse_listing(EMPTY) == []


def test_collect_page1_empty_is_error():
    with pytest.raises(RuntimeError, match="página 1.*layout o desafío"):
        scan.collect("x", 3, client=make_client({}))


def test_collect_page2_empty_is_normal_end():
    requests = []
    offers, pages_read = scan.collect("x", 3, client=make_client({1: PAGE1}, requests))
    assert len(offers) == 20
    assert len(requests) == 2  # la página 2 vacía corta, no pide la 3
    assert pages_read == 2  # la vacía que corta cuenta como leída


@pytest.mark.parametrize("status", [403, 429, 500, 503])
def test_collect_http_error_stops_without_retry(status):
    requests = []
    with pytest.raises(RuntimeError, match=str(status)):
        scan.collect("x", 3, client=status_client(status, requests))
    assert len(requests) == 1


def test_collect_does_not_follow_redirects():
    requests = []
    client = httpx.Client(transport=httpx.MockTransport(lambda r: (
        requests.append(r) or httpx.Response(302, headers={"Location": "https://candidato.ar.computrabajo.com/"}))))
    with pytest.raises(RuntimeError, match="302"):
        scan.collect("x", 3, client=client)
    assert len(requests) == 1


def test_collect_dedups_same_page_served_twice():
    offers, pages_read = scan.collect("x", 2, client=make_client({1: PAGE1, 2: PAGE1}))
    assert (len(offers), pages_read) == (20, 2)


def test_collect_sends_fixed_headers():
    requests = []
    scan.collect("x", 1, client=make_client({1: PAGE1}, requests))
    headers = requests[0].headers
    assert headers["user-agent"] == scan.HEADERS["User-Agent"]
    assert headers["accept-language"] == "es-AR,es;q=0.9"
    assert headers["accept"] == "text/html"


def test_collect_stop_file_halts_before_request(tmp_path):
    stop = tmp_path / "STOP"
    stop.write_text("")
    requests = []
    with pytest.raises(RuntimeError, match="STOP"):
        scan.collect("x", 3, client=make_client({1: PAGE1}, requests), stop_file=stop)
    assert requests == []


def test_collect_stop_file_created_midrun_halts_next_page(tmp_path, monkeypatch):
    stop = tmp_path / "STOP"
    requests = []
    client = make_client({1: PAGE1, 2: PAGE1}, requests)
    monkeypatch.setattr(scan.time, "sleep", lambda s: stop.write_text(""))
    with pytest.raises(RuntimeError, match="página 2"):
        scan.collect("x", 3, client=client, stop_file=stop)
    assert len(requests) == 1


def test_collect_sleeps_between_pages_only(sleeps):
    scan.collect("x", 3, client=make_client({1: PAGE1, 2: PAGE1, 3: PAGE1}))
    assert len(sleeps) == 2
    assert all(0.7 <= s <= 1.5 for s in sleeps)


def test_collect_rejects_bad_arguments():
    with pytest.raises(RuntimeError, match="vacía"):
        scan.collect("  ", 1, client=make_client({}))
    with pytest.raises(RuntimeError, match="--pages"):
        scan.collect("x", 0, client=make_client({}))


def test_cli_scan_prints_summary(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(scan, "collect", lambda keyword, pages, **kw: (scan.parse_listing(PAGE1), 2))
    argv = ["scan", "--keyword", "programador", "--pages", "2", "--max-details", "0", "--db", str(tmp_path / "t.db"), "--profile", str(tmp_path / "sin-perfil.yaml")]
    assert cli.main(argv) == 0
    assert capsys.readouterr().out.strip() == (
        "Páginas leídas: 2 · encontradas: 20 · nuevas: 20 · ya vistas: 0 · detalles: 0")
    assert cli.main(argv) == 0
    assert "nuevas: 0 · ya vistas: 20 · detalles: 0" in capsys.readouterr().out


def test_cli_scan_reports_pages_actually_read(monkeypatch, tmp_path, capsys):
    real_collect = scan.collect
    monkeypatch.setattr(scan, "collect", lambda keyword, pages, zones=(): real_collect(keyword, pages, client=make_client({1: PAGE1}), zones=zones))
    argv = ["scan", "--keyword", "x", "--pages", "3", "--max-details", "0", "--db", str(tmp_path / "t.db"), "--profile", str(tmp_path / "sin-perfil.yaml")]
    assert cli.main(argv) == 0
    assert capsys.readouterr().out.startswith("Páginas leídas: 2 · encontradas: 20")


def test_cli_scan_error_exits_1(monkeypatch, tmp_path, capsys):
    def boom(keyword, pages, **kw):
        raise RuntimeError("Computrabajo respondió 429")
    monkeypatch.setattr(scan, "collect", boom)
    assert cli.main(["scan", "--keyword", "x", "--db", str(tmp_path / "t.db"), "--profile", str(tmp_path / "sin-perfil.yaml")]) == 1
    err = capsys.readouterr().err
    assert "429" in err and "Detalles guardados" not in err


def test_search_url_by_zone_and_national_when_there_is_none_or_it_is_unknown():
    assert scan.search_url("Atención al cliente", 2, "capital-federal") == "https://ar.computrabajo.com/trabajo-de-atencion-al-cliente-en-capital-federal?p=2"
    assert scan.zone_slugs(["Capital Federal", "Buenos Aires-GBA"]) == ["capital-federal", "buenos-aires-gba"]
    assert scan.search_url("x", 1, "buenos-aires-gba").endswith("/trabajo-de-x-en-buenos-aires-gba?p=1")
    assert scan.zone_slugs([]) == [None] and scan.zone_slugs(["Capital Federal", "Córdoba"]) == [None]
    assert scan.search_url("x", 1) == "https://ar.computrabajo.com/trabajo-de-x?p=1"


def test_collect_reads_each_zone(monkeypatch):
    monkeypatch.setattr(scan.time, "sleep", lambda s: None)
    asked = []
    client = httpx.Client(transport=httpx.MockTransport(lambda r: asked.append(str(r.url)) or httpx.Response(200, text=PAGE1)))
    offers, pages_read = scan.collect("mozo", 1, client=client, zones=["Capital Federal", "Buenos Aires-GBA"])
    assert pages_read == 2 and [u.split("/")[-1] for u in asked] == ["trabajo-de-mozo-en-capital-federal?p=1", "trabajo-de-mozo-en-buenos-aires-gba?p=1"]
