"""TAREA-062: la tanda (un CV y búsquedas elegidas) y la rotación de CVs. Todo falso: sitio, páginas, clientes y reloj."""
from datetime import datetime

import json
import httpx
import pytest
import yaml

import test_score
from fakesite import APPLICATIONS_URL, BASE_CV, standard_site
from test_apply_live import MODEL, SALARY, TODAY, FakeClient, add_offer, conn, make_profile, rows, status_of  # noqa: F401
from test_cv_slots import applied, cards, days_ago, full_site, go, own_name_provider, report  # noqa: F401
from compuapply import apply, batch, db, scan, score, ui_logic

VENTAS_PDF = "CV-Base-Ventas.pdf"


def two_variants(same_default=False, **flags):
    """El perfil con `ventas` y `dev`; con `same_default` las dos declaran el mismo `default_pdf_name`."""
    prof = make_profile({"salary": SALARY})
    dev = prof["variants"][0]
    ventas = {**dev, "id": "ventas", "keywords": ["atencion al cliente"]}
    dev["default_pdf_name"] = BASE_CV
    ventas["default_pdf_name"] = BASE_CV if same_default else VENTAS_PDF
    prof["variants"] = [dev, ventas]
    prof["apply"].update(flags)
    return prof


def scored(conn, n, ventas=90, dev=50, **kw):
    """Una oferta con el puntaje de cada variante (la mejor queda como `best_variant`)."""
    id16 = add_offer(conn, n, **kw)
    best, points = ("ventas", ventas) if ventas >= dev else ("dev", dev)
    conn.execute("DELETE FROM scores WHERE id16 = ?", (id16,))
    db.save_score(conn, id16, MODEL, score.PROMPT_VERSION,
                  {"best_variant": best, "score": points, "all_scores": {"ventas": ventas, "dev": dev}, "reasons": [], "missing": []}, (1, 0, 1), 0.0)
    return id16


# ── el plan de la tanda ──────────────────────────────────────────────────────

def test_the_plan_takes_only_the_listed_offers_with_the_score_of_the_chosen_variant(conn):
    old = scored(conn, 1, ventas=95, dev=10)  # una oferta vieja de otra palabra
    listed = scored(conn, 2, ventas=80, dev=99)  # en el listado: mejor para dev, pero la tanda es de ventas
    plan = apply.plan(conn, two_variants(), MODEL, TODAY, only={listed}, variant="ventas", cap=5)
    assert [i["id16"] for i in plan["send"]] == [listed] and plan["send"][0]["variant"] == "ventas" and plan["send"][0]["score"] == 80
    assert old not in [i["id16"] for i in plan["send"] + plan["skip"]]


def test_a_score_below_the_threshold_for_that_variant_is_skipped_with_its_reason(conn):
    low = scored(conn, 2, ventas=60, dev=99)
    plan = apply.plan(conn, two_variants(), MODEL, TODAY, only={low}, variant="ventas", cap=5)
    assert plan["send"] == [] and plan["skip"][0]["reason"] == "puntaje bajo para ventas (60 < 70)"


def test_max_replaces_the_daily_cap_and_the_company_cap_stays(conn):
    ids = [scored(conn, n, company=f"Empresa {n}") for n in (1, 2, 3)]
    plan = apply.plan(conn, two_variants(daily_cap=10), MODEL, TODAY, only=set(ids), variant="ventas", cap=2)
    assert len(plan["send"]) == 2 and [i["reason"] for i in plan["skip"]] == ["tope diario"]
    same = [scored(conn, n, company="Misma") for n in (4, 5)]
    assert len(apply.plan(conn, two_variants(), MODEL, TODAY, only=set(same), variant="ventas", cap=5)["send"]) == 1


def test_an_offer_without_a_score_for_the_variant_says_so(conn):
    id16 = add_offer(conn, 1)  # all_scores solo tiene `dev`
    plan = apply.plan(conn, two_variants(), MODEL, TODAY, only={id16}, variant="ventas", cap=5)
    assert plan["skip"][0]["reason"] == "sin puntaje para ventas"


# ── en vivo: tope y minutos ──────────────────────────────────────────────────

def live(conn, tmp_path, site, ids, *, prof=None, max_send=5, clock=None, deadline=None, provider=None):
    from fakesite import FakePage
    page = FakePage(site)
    args = {"ids": set(ids), "variant": "ventas", "max": max_send, "deadline": deadline, "clock": clock or (lambda: 0.0)}
    result = apply.run_live(conn, prof or two_variants(), MODEL, FakeClient(__import__("test_apply_live").known_reply()), page, TODAY,
                            stop_file=tmp_path / "STOP", pause=lambda: None, cv_provider=provider or own_name_provider(), batch=args)
    return result


def site_with_room():
    return standard_site(cvs=(BASE_CV, VENTAS_PDF), default=BASE_CV, pages={APPLICATIONS_URL: cards()})


def test_max_2_sends_2_of_3(conn, tmp_path):
    ids = [scored(conn, n, company=f"Empresa {n}") for n in (1, 2, 3)]
    result = live(conn, tmp_path, site_with_room(), ids, max_send=2)
    assert len(result["sent"]) == 2 and result["halted"] is None


def test_after_the_deadline_no_other_offer_is_started(conn, tmp_path):
    ids = [scored(conn, n, company=f"Empresa {n}") for n in (1, 2, 3)]
    ticks = iter([0.0, 10.0, 70.0, 200.0, 300.0])  # el reloj: la tercera mirada ya pasó los 60 s
    result = live(conn, tmp_path, site_with_room(), ids, clock=lambda: next(ticks), deadline=60.0)
    assert len(result["sent"]) == 2 and result["time_up"] is True
    assert [i["reason"] for i in result["skipped"]] == ["sin procesar: se acabó el tiempo de la tanda"]


# ── la rotación ──────────────────────────────────────────────────────────────

def test_a_full_account_rotates_the_two_oldest_sent_cvs_and_never_a_fixed_one(conn, tmp_path):
    names = [f"CV-Env-{n}.pdf" for n in range(7)]
    for n, name in enumerate(names):
        applied(conn, 30 + n, name, f"IM{30 + n}", days_ago(10 - n))  # CV-Env-0 es el más viejo
    apps = tuple((f"IM{30 + n}", f"Enviada {n}", "Acme", "Postulado") for n in range(7))
    site = full_site(VENTAS_PDF, *names, apps=apps)  # 10: el por defecto, el base de ventas, 7 enviados y 1 relleno
    ids = [scored(conn, n, company=f"Empresa {n}") for n in (1, 2)]
    result = live(conn, tmp_path, site, ids, prof=two_variants())
    assert result["halted"] is None and len(result["sent"]) == 2 and len(site.uploads) == 2
    assert site.deleted == ["CV-Env-0.pdf", "CV-Env-1.pdf"]
    assert all(d["reason"].startswith("rotación: el CV de la postulación más vieja (") for d in result["deleted"])
    assert BASE_CV in site.cvs and VENTAS_PDF in site.cvs and "Relleno-0.pdf" in site.cvs


def test_with_two_variants_sharing_the_default_pdf_name_and_nothing_to_rotate_the_offer_is_skipped_and_the_run_goes_on(conn, tmp_path):
    site = full_site()  # solo fijos y relleno
    ids = [scored(conn, n, company=f"Empresa {n}") for n in (1, 2)]
    result = live(conn, tmp_path, site, ids, prof=two_variants(same_default=True))
    assert result["halted"] is None and result["sent"] == [] and site.uploads == [] and site.deleted == []
    assert [i["reason"] for i in result["skipped"]] == ["cuenta llena y sin CV para rotar"] * 2


def test_without_a_batch_a_full_account_without_rotation_or_base_still_halts(conn, tmp_path):
    from fakesite import FakePage
    scored(conn, 1)
    result = apply.run_live(conn, two_variants(same_default=True), MODEL, FakeClient(__import__("test_apply_live").known_reply()), FakePage(full_site()),
                            TODAY, stop_file=tmp_path / "STOP", pause=lambda: None, cv_provider=own_name_provider())
    assert result["halted"] and "ninguno se puede borrar sin riesgo" in result["halted"]


# ── las etapas de la tanda ───────────────────────────────────────────────────

def test_parse_keywords_drops_empty_and_repeated():
    assert batch.parse_keywords("Atención al cliente, Mozo,, mozo ,  ") == ["Atención al cliente", "Mozo"]


def test_score_pending_with_only_and_extra_variant(conn):
    mine, other = test_score.add_offer(conn, 1), test_score.add_offer(conn, 2)
    client = test_score.FakeClient(test_score.response())
    out = score.score_pending(conn, test_score.PROFILE, client, MODEL, 10, 1.0, only={mine}, extra_variant="data")
    assert out["scored"] == 1 and status_of(conn, other) == "new"
    asked = [test_score.requested_ids(call) for call in client.calls]
    assert sorted(i for ids in asked for i in ids) == ["backend", "data"]  # la keyword «x» es de backend; data entra por la tanda
    assert conn.execute("SELECT COUNT(*) FROM scores WHERE id16 = ?", (other,)).fetchone()[0] == 0


def test_fetch_details_with_only_leaves_the_other_offers_alone(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(scan.time, "sleep", lambda s: None)
    a, b = add_offer_nodetail(conn, 1), add_offer_nodetail(conn, 2)
    asked = []
    client = httpx.Client(transport=httpx.MockTransport(lambda r: asked.append(str(r.url)) or httpx.Response(200, text="x")))
    assert scan.fetch_details(conn, 5, client=client, stop_file=tmp_path / "STOP", only={a}) == 0
    assert len(asked) == 1 and status_of(conn, a) == "no_detail" and status_of(conn, b) == "new"


def add_offer_nodetail(conn, n):
    id16 = f"{n:016X}"
    db.upsert_offers(conn, [{"id16": id16, "data_id": f"{n:032X}", "title": f"Oferta {n}", "company": "E", "location": "Rosario",
                             "url": f"https://ar.computrabajo.com/ofertas-de-trabajo/oferta-{n}-{n:032X}"}], "x")
    return id16


# ── perfil, comando y ventana ────────────────────────────────────────────────

def test_build_command_for_the_batch():
    settings = {"variant": "ventas", "keywords": "Atención al cliente, Mozo", "minutes": 60, "max": 5}
    command = ui_logic.build_command("batch_dry", settings)
    assert command[-10:-1] == ["tanda", "--variant", "ventas", "--keywords", "Atención al cliente, Mozo", "--minutes", "60", "--max", "5"]
    assert "--dry" in command and "--dry" not in ui_logic.build_command("batch", settings)


def test_last_batch_goes_to_data_json_and_the_profile_is_untouched(tmp_path):
    from test_apply_live import write_profile_with
    path = write_profile_with(tmp_path)
    path.write_text(path.read_text(encoding="utf-8") + "\n# comentario del usuario\n", encoding="utf-8")
    before = path.read_bytes()
    saved = tmp_path / "data" / "last_batch.json"
    ui_logic.remember_batch({"variant": "dev", "keywords": "Mozo, Cajero ñ", "minutes": 30, "max": 4}, saved)
    assert path.read_bytes() == before
    assert "Cajero ñ" in saved.read_text(encoding="utf-8")
    defaults = ui_logic.batch_defaults(path, saved)
    assert (defaults["variant"], defaults["keywords"], defaults["minutes"], defaults["max"]) == ("dev", "Mozo, Cajero ñ", 30, 4)


def test_last_batch_missing_or_broken_gives_the_defaults_and_the_old_profile_key_is_an_initial_value(tmp_path):
    from test_apply_live import write_profile_with
    path = write_profile_with(tmp_path)
    saved = tmp_path / "data" / "last_batch.json"
    assert ui_logic.batch_defaults(path, saved)["keywords"] == "" and ui_logic.batch_defaults(path, saved)["minutes"] == 60
    saved.parent.mkdir()
    for junk in ("{roto", "[1]", '{"minutes": "x"}'):
        saved.write_text(junk, encoding="utf-8")
        assert ui_logic.batch_defaults(path, saved)["keywords"] == "" and ui_logic.batch_defaults(path, saved)["minutes"] == 60
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["run"] = {**(data.get("run") or {}), "last_batch": {"variant": "dev", "keywords": "Viejo", "minutes": 20, "max": 2}}
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    saved.unlink()
    assert ui_logic.batch_defaults(path, saved)["keywords"] == "Viejo"
    ui_logic.remember_batch({"variant": "dev", "keywords": "Nuevo", "minutes": 5, "max": 1}, saved)
    assert ui_logic.batch_defaults(path, saved)["keywords"] == "Nuevo"


def test_the_report_of_the_batch_has_searches_variant_minutes_and_the_sent_and_skipped(conn, tmp_path):
    ids = [scored(conn, n, company=f"Empresa {n}") for n in (1, 2)]
    result = live(conn, tmp_path, site_with_room(), ids, max_send=1)
    prep = {"variant": "ventas", "keywords": ["Atención al cliente"], "ids": set(ids), "scan": [{"keyword": "Atención al cliente", "pages_read": 2,
            "found": 5, "new": 2, "seen": 3}], "details": {"saved": 2, "gone": 1, "no_detail": 0, "bad_url": 0}, "score": None, "stopped": None}
    meta = {"now": datetime(2026, 11, 6, 10, 0, 0), "minutes": 60, "minutes_used": 12.5, "max": 1, "threshold": 70, "model": MODEL, "dry": False}
    text = batch.write_report(prep, None, result, meta, tmp_path / "runs").read_text(encoding="utf-8")
    assert "# Informe de tanda (en vivo)" in text and "CV (variante): ventas" in text and "usados 12.5 de 60" in text
    assert "Atención al cliente" in text and "## Enviadas (1)" in text and "tope diario" in text and "## Respuestas" in text
    dry = batch.write_report(prep, apply.plan(conn, two_variants(), MODEL, TODAY, only=set(ids), variant="ventas", cap=1), None,
                             {**meta, "dry": True}, tmp_path / "runs2").read_text(encoding="utf-8")
    assert "# Informe de tanda (seco)" in dry and "## Enviaría" in dry


def test_the_window_builds_with_the_tanda_block(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    done = subprocess.run([sys.executable, "-m", "compuapply.gui", "--smoke"], cwd=tmp_path, capture_output=True, text=True,
                          env={**os.environ, "PYTHONPATH": str(root)})
    assert done.returncode == 0, done.stderr
    from compuapply import gui
    source = Path(gui.__file__).read_text(encoding="utf-8")
    assert "Tanda" in source and "Plan de la tanda" in source and "Postular la tanda" in source and gui.TITLES["batch_dry"] == "Plan de la tanda"


def test_a_batch_run_writes_data_last_batch_json_and_leaves_the_profile_byte_for_byte(tmp_path, monkeypatch):
    from test_apply_live import write_profile_with
    from compuapply import cli
    path = write_profile_with(tmp_path)
    path.write_text("# mi comentario\n" + path.read_text(encoding="utf-8"), encoding="utf-8")
    before = path.read_bytes()
    monkeypatch.setattr(cli.score, "load_env", lambda *a, **k: None)
    monkeypatch.setattr(cli.batch, "walk", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("corte de prueba")))
    db_path = tmp_path / "data" / "x.db"
    db_path.parent.mkdir()
    assert cli.main(["tanda", "--variant", "dev", "--keywords", "Mozo, Cajero", "--minutes", "7", "--max", "3", "--dry",
                     "--profile", str(path), "--db", str(db_path)]) == 1
    assert json.loads((tmp_path / "data" / "last_batch.json").read_text(encoding="utf-8")) == {
        "variant": "dev", "keywords": "Mozo, Cajero", "minutes": 7, "max": 3, "afin": False, "per_company": False}
    assert path.read_bytes() == before


# ── TAREA-064: las búsquedas en orden, página por página y por zona ──────────

ZONES = ["Capital Federal", "Buenos Aires-GBA"]


def listing_html(titles, start, location="Palermo, Capital Federal", company=None):
    """Una página de listado falsa: un `article` por título, con ids correlativos desde `start`."""
    articles = "".join(
        f'<article data-id="{start + n:016X}{0:016X}"><h2><a href="/ofertas-de-trabajo/oferta-{start + n}-{start + n:016X}{0:016X}">{title}</a></h2>'
        f'<p><a class="t_ellipsis" href="/e">{company or "Empresa " + str(start + n)}</a></p><span class="mr10">{location}</span></article>'
        for n, title in enumerate(titles))
    return f"<html><body>{articles}</body></html>"


class Site:
    """El sitio de listados falso: `pages[(slug de la búsqueda con zona, p)]` es el HTML; lo demás es una página sin ofertas."""

    def __init__(self, pages):
        self.pages, self.asked = pages, []

    def client(self):
        def handler(request):
            name = request.url.path.rsplit("/", 1)[-1].removeprefix("trabajo-de-")
            self.asked.append((name, int(request.url.params["p"])))
            return httpx.Response(200, text=self.pages.get(self.asked[-1], "<html><body></body></html>"))
        return httpx.Client(transport=httpx.MockTransport(handler))


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def fake_details(monkeypatch):
    def details(conn, limit, client=None, stop_file=None, zones=(), only=None):
        for id16 in only or ():
            conn.execute("UPDATE offers SET description = 'x', detail_fetched_at = '2026-11-06' WHERE id16 = ?", (id16,))
        conn.commit()
        return len(only or ())
    monkeypatch.setattr(scan, "fetch_details", details)


def fake_scores(monkeypatch, points_for):
    """`score_pending` falso: el puntaje de cada oferta (en las dos variantes) sale de `points_for(título)`."""
    def pending(conn, profile, client, model, limit, max_cost, stop_file=None, only=None, extra_variant=None, **kw):
        done = 0
        for id16, title in conn.execute("SELECT id16, title FROM offers WHERE status = 'new'").fetchall():
            if only is not None and id16 not in only:
                continue
            p = points_for(title)
            db.save_score(conn, id16, model, score.PROMPT_VERSION, {"best_variant": "ventas", "score": p, "all_scores": {"ventas": p, "dev": p},
                          "reasons": [], "missing": []}, (1, 0, 1), 0.0)
            done += 1
        return {"scored": done, "excluded": 0, "errors": 0, "cost_usd": 0.0}
    monkeypatch.setattr(score, "score_pending", pending)


def walk(conn, site, keywords, monkeypatch, *, points=lambda title: 90, opts=None, live=None, clock=None, deadline=None, zones=ZONES, **flags):
    fake_details(monkeypatch)
    fake_scores(monkeypatch, points)
    conn.execute("DELETE FROM scores")
    conn.commit()
    prof = two_variants(**flags)
    prof["zones"] = zones
    return batch.walk(conn, prof, site.client(), object(), MODEL, "ventas", keywords, TODAY, {"max": 50, **(opts or {})}, live=live,
                      pause=lambda: None, clock=clock or Clock(), deadline=deadline), prof


def only_in(site_asked, zone_slug):
    return [(n, p) for n, p in site_asked if n.endswith("-en-" + zone_slug)]


def test_each_search_is_read_page_by_page_in_both_zones_and_in_the_order_written(conn, monkeypatch):
    mozo = ["Mozo de salon", "Mozo", "Camarero mozo"]
    site = Site({("mozo-en-capital-federal", 1): listing_html(mozo, 1), ("mozo-en-buenos-aires-gba", 1): listing_html(mozo, 100)})
    out, _ = walk(conn, site, ["Mozo"], monkeypatch)
    assert site.asked[:2] == [("mozo-en-capital-federal", 1), ("mozo-en-capital-federal", 2)]
    assert ("mozo-en-buenos-aires-gba", 1) in site.asked and out["end"] == "lista"
    rows = out["prep"]["scan"]
    assert [(r["zone"], r["pages_read"], r["found"], r["reason"]) for r in rows] == [
        ("Capital Federal", 2, 3, "sin más páginas"), ("Buenos Aires-GBA", 2, 3, "sin más páginas")]


def test_it_moves_to_the_next_search_after_three_in_a_row_that_are_not_of_the_search(conn, monkeypatch):
    foreign = ["Vendedor", "Chofer", "Contador", "Atención al cliente remoto"]
    site = Site({("atencion-al-cliente-en-capital-federal", 1): listing_html(["Atención al Cliente"] + foreign, 1),
                 ("atencion-al-cliente-en-capital-federal", 2): listing_html(["Atención al cliente"], 50)})
    out, _ = walk(conn, site, ["atención al cliente", "mozo"], monkeypatch, zones=["Capital Federal"])
    first = out["prep"]["scan"][0]
    assert first["reason"] == "3 seguidas de otra cosa" and first["pages_read"] == 1 and first["matching"] == 1
    assert ("atencion-al-cliente-en-capital-federal", 2) not in site.asked and ("mozo-en-capital-federal", 1) in site.asked
    assert [r["keyword"] for r in out["prep"]["scan"]] == ["atención al cliente", "mozo"]


def test_two_foreign_in_a_row_do_not_cut_and_one_of_the_search_with_a_low_score_does_not_either(conn, monkeypatch):
    titles = ["Vendedor", "Chofer", "Atención al cliente", "Contador", "Atención al cliente senior", "Gerente", "Cajero", "Atención al cliente"]
    site = Site({("atencion-al-cliente-en-capital-federal", 1): listing_html(titles, 1), ("atencion-al-cliente-en-capital-federal", 2): listing_html(["Atención al cliente"], 50)})
    out, _ = walk(conn, site, ["atención al cliente"], monkeypatch, zones=["Capital Federal"],
                  points=lambda title: 10 if "senior" in title else 90)
    row = out["prep"]["scan"][0]
    assert row["pages_read"] == 3 and row["reason"] == "sin más páginas" and row["matching"] == 4  # la de puntaje bajo es de la búsqueda
    assert any(i["reason"].startswith("puntaje bajo para ventas") for i in out["plan"]["skip"])


def test_a_foreign_title_with_a_high_score_cuts_the_streak_only_when_the_option_is_off(conn, monkeypatch):
    titles = ["Vendedor", "Chofer", "Contador"]
    pages = {("mozo-en-capital-federal", 1): listing_html(titles, 1)}
    off, _ = walk(conn, Site(pages), ["mozo"], monkeypatch, zones=["Capital Federal"])
    assert off["prep"]["scan"][0]["reason"] == "3 seguidas de otra cosa"
    on, _ = walk(conn, Site(pages), ["mozo"], monkeypatch, zones=["Capital Federal"], opts={"afin": True})
    assert on["prep"]["scan"][0]["reason"] == "sin más páginas" and on["prep"]["scan"][0]["matching"] == 3
    low, _ = walk(conn, Site(pages), ["mozo"], monkeypatch, zones=["Capital Federal"], opts={"afin": True}, points=lambda title: 10)
    assert low["prep"]["scan"][0]["reason"] == "3 seguidas de otra cosa"


def test_without_more_pages_it_goes_on_and_when_the_list_ends_it_finishes_before_the_time_and_says_so(conn, monkeypatch, tmp_path):
    site = Site({("mozo-en-capital-federal", 1): listing_html(["Mozo"], 1), ("cajero-en-capital-federal", 1): listing_html(["Cajero"], 20)})
    clock = Clock()
    out, _ = walk(conn, site, ["mozo", "cajero"], monkeypatch, zones=["Capital Federal"], clock=clock, deadline=3600.0)
    assert out["end"] == "lista" and [r["keyword"] for r in out["prep"]["scan"]] == ["mozo", "cajero"] and ("cajero-en-capital-federal", 1) in site.asked
    meta = {"now": datetime(2026, 11, 6, 10, 0, 0), "minutes": 60, "minutes_used": 0.0, "max": None, "threshold": 70, "model": MODEL, "dry": True,
            "per_company": False, "afin": False, "end": out["end"]}
    text = batch.write_report(out["prep"], out["plan"], None, meta, tmp_path / "runs").read_text(encoding="utf-8")
    assert "Fin: se acabó la lista de búsquedas" in text and "sin tope (manda solo el tiempo)" in text and "| mozo | Capital Federal |" in text
    assert "sin más páginas" in text and "Tope por empresa: apagado" in text


def fake_live(conn, prof, per_company, record=None, clock=None, cost_time=0.0):
    """Un `run_live` falso: manda todo lo que el plan manda (y lo deja `applied` hoy), sin sitio."""
    def live(b):
        plan_ = apply.plan(conn, prof, MODEL, datetime.now().date(), only=b["ids"], variant=b["variant"], cap=b["max"], company_cap_on=b["company_cap"])
        if record is not None:
            record.append(b)
        for item in plan_["send"]:
            db.mark_status(conn, item["id16"], "applied")
            db.save_application(conn, item["id16"], item["variant"], item["score"], "applied", "ok", [], "cv.pdf", None, "x")
        if clock is not None:
            clock.now += cost_time * len(plan_["send"])
        late = b["deadline"] is not None and clock is not None and clock.now >= b["deadline"]
        return {"plan": plan_, "sent": [{**i, "file": "cv.pdf"} for i in plan_["send"]], "skipped": [], "halted": None, "time_up": late,
                "pdfs": {"before": 3, "max": 10, "uploaded": len(plan_["send"]), "reused": 0}, "requeued": 0, "completed": 0, "cost_usd": 0.0,
                "read": 0, "start_default": None, "restore": None, "deleted": [], "pending": [], "answers": []}
    return live


def test_the_cap_cuts_in_the_middle_of_a_search_and_the_rest_is_not_reached(conn, monkeypatch):
    site = Site({("mozo-en-capital-federal", 1): listing_html(["Mozo"] * 3, 1), ("mozo-en-capital-federal", 2): listing_html(["Mozo"] * 3, 10),
                 ("cajero-en-capital-federal", 1): listing_html(["Cajero"], 30)})
    prof = two_variants()
    seen = []
    out, _ = walk(conn, site, ["mozo", "cajero"], monkeypatch, zones=["Capital Federal"], opts={"max": 4}, live=fake_live(conn, prof, False, seen))
    assert out["end"] == "tope" and len(out["live"]["sent"]) == 4 and [b["max"] for b in seen] == [4, 1]
    rows = out["prep"]["scan"]
    assert rows[0]["reason"] == "tope" and rows[0]["sent"] == 4 and rows[1]["reason"] == "no se llegó (tope)"
    assert ("mozo-en-capital-federal", 3) not in site.asked and ("cajero-en-capital-federal", 1) not in site.asked


def test_the_time_cuts_in_the_middle_of_a_search_and_no_new_page_starts(conn, monkeypatch):
    clock = Clock()
    site = Site({("mozo-en-capital-federal", n): listing_html(["Mozo"] * 2, 10 * n) for n in range(1, 8)})
    prof = two_variants()
    out, _ = walk(conn, site, ["mozo"], monkeypatch, zones=["Capital Federal"], opts={"max": None}, clock=clock, deadline=100.0,
                  live=fake_live(conn, prof, False, clock=clock, cost_time=30.0))
    assert out["end"] == "tiempo" and out["prep"]["scan"][0]["reason"] == "tiempo"
    assert out["prep"]["scan"][0]["pages_read"] < 7 and len(out["live"]["sent"]) >= 2


def test_with_the_company_option_on_the_cap_holds_between_searches_and_off_all_three_go_out(conn, monkeypatch):
    def run(per_company):
        local = Site({("mozo-en-capital-federal", 1): listing_html(["Mozo", "Mozo"], 1, company="Misma SA"),
                      ("cajero-en-capital-federal", 1): listing_html(["Cajero"], 40, company="Misma SA")})
        prof = two_variants(company_daily_cap=2)
        conn.execute("DELETE FROM applications")
        conn.execute("DELETE FROM offers")
        conn.commit()
        out, _ = walk(conn, local, ["mozo", "cajero"], monkeypatch, zones=["Capital Federal"], opts={"max": None, "por_empresa": per_company},
                      live=fake_live(conn, prof, per_company), company_daily_cap=2)
        return out
    assert len(run(False)["live"]["sent"]) == 3
    on = run(True)
    assert len(on["live"]["sent"]) == 2 and [i["reason"] for i in on["live"]["plan"]["skip"]] == ["tope por empresa"]


def test_the_plan_has_no_company_cap_when_the_tanda_turns_it_off(conn):
    ids = [scored(conn, n, company="Misma") for n in (1, 2, 3)]
    off = apply.plan(conn, two_variants(), MODEL, TODAY, only=set(ids), variant="ventas", cap=5, company_cap_on=False)
    assert len(off["send"]) == 3


def test_search_words_ignore_short_words_accents_and_case():
    assert batch.search_words("Atención al CLIENTE") == ["atencion", "cliente"]
    assert batch.is_from_search("Ejecutivo/a de atención a clientes", ["atencion", "cliente"])
    assert not batch.is_from_search("Vendedor", ["atencion", "cliente"])


def test_a_halt_in_a_chunk_stops_the_walk_and_the_rest_is_marked(conn, monkeypatch):
    site = Site({("mozo-en-capital-federal", 1): listing_html(["Mozo"], 1)})
    prof = two_variants()

    def halting(b):
        return {**fake_live(conn, prof, False)(b), "halted": "CAPTCHA"}
    out, _ = walk(conn, site, ["mozo", "cajero"], monkeypatch, zones=["Capital Federal"], live=halting)
    assert out["end"] == "frenó" and out["live"]["halted"] == "CAPTCHA" and out["prep"]["scan"][1]["reason"] == "no se llegó (frenó)"


def test_the_tanda_cli_accepts_max_all_and_the_two_options(tmp_path, monkeypatch):
    from test_apply_live import write_profile_with
    from compuapply import cli
    path = write_profile_with(tmp_path)
    seen = {}
    monkeypatch.setattr(cli.score, "load_env", lambda *a, **k: None)

    def stop(conn, profile, http, llm, model, variant, keywords, today, opts, **kw):
        seen.update(opts)
        raise RuntimeError("corte de prueba")
    monkeypatch.setattr(cli.batch, "walk", stop)
    db_path = tmp_path / "data" / "x.db"
    db_path.parent.mkdir()
    base = ["tanda", "--variant", "dev", "--keywords", "Mozo", "--dry", "--profile", str(path), "--db", str(db_path)]
    assert cli.main([*base, "--max", "all", "--afin-puntaje", "--por-empresa"]) == 1
    assert seen == {"max": None, "afin": True, "por_empresa": True}
    assert json.loads((tmp_path / "data" / "last_batch.json").read_text(encoding="utf-8"))["max"] is None
    cli.main([*base, "--max", "0"])
    assert seen == {"max": 0, "afin": False, "por_empresa": False}
    cli.main(base)
    assert seen["max"] == 10  # sin --max, el tope diario del perfil
    with pytest.raises(SystemExit):
        cli.main([*base, "--max", "-3"])


def test_the_window_and_the_command_carry_the_empty_cap_and_the_two_options(tmp_path):
    settings = {"variant": "ventas", "keywords": "Mozo", "minutes": 60, "max": None, "afin": True, "per_company": True}
    command = ui_logic.build_command("batch", settings)
    assert command[command.index("--max") + 1] == "all" and "--afin-puntaje" in command and "--por-empresa" in command
    plain = ui_logic.build_command("batch", {**settings, "max": 5, "afin": False, "per_company": False})
    assert plain[plain.index("--max") + 1] == "5" and "--afin-puntaje" not in plain and "--por-empresa" not in plain
    from test_apply_live import write_profile_with
    path, saved = write_profile_with(tmp_path), tmp_path / "data" / "last_batch.json"
    ui_logic.remember_batch({"variant": "dev", "keywords": "Mozo", "minutes": 20, "max": None, "afin": True, "per_company": False}, saved)
    defaults = ui_logic.batch_defaults(path, saved)
    assert defaults["max"] is None and defaults["afin"] is True and defaults["per_company"] is False
    assert ui_logic.batch_defaults(path, tmp_path / "no.json")["afin"] is False


# ── la verificación final lee todas las páginas que hagan falta ──────────────

def test_the_final_check_follows_the_next_page_until_every_sent_offer_is_found():
    import re
    from fakesite import FakePage
    from test_apply_cv import fixture, with_next
    p2, p3 = "https://candidato.ar.computrabajo.com/candidate/match/?p=2", "https://candidato.ar.computrabajo.com/candidate/match/?p=3"
    first, second = fixture("apply_applications.html"), re.sub(r"IM0+", "IM2", fixture("apply_applications.html"))
    pages = {APPLICATIONS_URL: with_next(first, "/candidate/match/?p=2"), p2: with_next(second, "?p=3"), p3: second.replace("IM2", "IM3")}
    page = FakePage(standard_site(pages=pages))
    from compuapply import browser
    everything = browser.read_applications(FakePage(standard_site(pages=pages)), pages=10)
    assert len(everything) == 12
    last = everything[-1]
    sent = [{"title": "Otro", "company": "Otra", "im": last["im"]}]
    result = apply._verify_sent(page, sent)
    assert result["read"] == 12 and result["unconfirmed"] == [] and page.visited == [APPLICATIONS_URL, p2, p3]
    early = FakePage(standard_site(pages=pages))
    first_card = everything[0]
    apply._verify_sent(early, [{"title": "Otro", "company": "Otra", "im": first_card["im"]}])
    assert early.visited == [APPLICATIONS_URL]  # ya estaba en la primera: no sigue


def test_the_final_check_follows_the_data_path_of_the_next_span_of_the_real_pagination():
    import re
    from fakesite import FakePage
    from test_apply_cv import fixture
    p2 = "https://candidato.ar.computrabajo.com/candidate/match/?idapp=3&f=0&p=2"
    first = fixture("apply_applications.html").replace("</body>", fixture("apply_pagination.html") + "</body>")
    second = re.sub(r"IM0+", "IM2", fixture("apply_applications.html"))
    page = FakePage(standard_site(pages={APPLICATIONS_URL: first, p2: second}))
    from compuapply import browser
    result = apply._verify_sent(page, [{"title": "Otro", "company": "Otra", "im": next(a["im"] for a in browser.read_applications(FakePage(
        standard_site(pages={APPLICATIONS_URL: first, p2: second})), pages=5) if a["im"].startswith("IM2"))}])
    assert result["read"] == 8 and result["unconfirmed"] == [] and page.visited == [APPLICATIONS_URL, p2]
    alone = FakePage(standard_site(pages={APPLICATIONS_URL: fixture("apply_applications.html")}))
    assert apply._verify_sent(alone, [{"title": "Otro", "company": "Otra", "im": "NO"}])["read"] == 4 and alone.visited == [APPLICATIONS_URL]


def test_a_first_page_that_says_there_are_no_offers_goes_to_the_next_search_and_an_empty_one_without_the_text_halts(conn, monkeypatch):
    notice = "<html><body><p>Parece que no hay ofertas para el empleo que buscas</p></body></html>"
    site = Site({("zzz-en-capital-federal", 1): notice, ("cajero-en-capital-federal", 1): listing_html(["Cajero"], 20)})
    out, _ = walk(conn, site, ["zzz", "cajero"], monkeypatch, zones=["Capital Federal"])
    assert [r["reason"] for r in out["prep"]["scan"]] == ["sin ofertas", "sin más páginas"] and out["end"] == "lista" and out["prep"]["stopped"] is None
    halted, _ = walk(conn, Site({}), ["zzz", "cajero"], monkeypatch, zones=["Capital Federal"])
    assert halted["end"] == "frenó" and halted["prep"]["stopped"]["stage"] == "scan"


def test_scan_collect_skips_a_zone_with_the_no_offers_notice_but_not_an_empty_page(monkeypatch):
    monkeypatch.setattr(scan.time, "sleep", lambda s: None)
    notice = "<html><body><p>Parece que no hay ofertas para el empleo que buscas</p></body></html>"
    pages = {"trabajo-de-x-en-capital-federal": notice, "trabajo-de-x-en-buenos-aires-gba": listing_html(["X"], 1)}
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=pages.get(r.url.path.rsplit("/", 1)[-1], ""))))
    offers, pages_read = scan.collect("x", 2, client=client, zones=ZONES)
    assert len(offers) == 1 and pages_read == 3
    with pytest.raises(RuntimeError, match="página 1"):
        scan.collect("x", 2, client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=""))), zones=ZONES)


def test_a_three_chunk_tanda_reads_my_applications_once_before_and_once_at_the_end(conn, monkeypatch, tmp_path):
    from fakesite import FakePage
    from test_cv_slots import own_name_provider
    from compuapply import browser
    pages = {("mozo-en-capital-federal", n): listing_html(["Mozo"] * 2, 10 * n) for n in (1, 2, 3)}
    prof = two_variants()
    page = FakePage(standard_site(cvs=(BASE_CV,), default=BASE_CV))
    opts = {"max": None, "applications": browser.read_applications(page, pages=10)}

    def applied(page_, offer, answers_fn, cv_fn):
        return {"status": "applied", "qa": [], "cv": {"name": "cv.pdf", "version": "x"}, "im": "IMX" + offer["id16"]}

    def run(b):
        return apply.run_live(conn, prof, MODEL, FakeClient(__import__("test_apply_live").known_reply()), page, TODAY, stop_file=tmp_path / "STOP",
                              pause=lambda: None, apply_one=applied, cv_provider=own_name_provider(), batch=b)
    out, _ = walk(conn, Site(pages), ["mozo"], monkeypatch, zones=["Capital Federal"], opts=opts, live=run)
    assert len(out["live"]["sent"]) == 6 and len([r for r in out["prep"]["scan"] if r["pages_read"]]) == 1
    apply.final_check(out["live"], page)
    assert page.visited.count(APPLICATIONS_URL) == 2  # antes (una) y la verificación final (otra); los 3 tramos no la leyeron
    assert len(opts["applications"]) >= 6  # lo enviado se le sumó a la lista
