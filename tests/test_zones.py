"""Tests de `zones`: ofertas limitadas a las provincias del usuario. Base falsa, cliente falso y sin red."""
import random
from datetime import date, datetime
from pathlib import Path

import httpx
import pytest
import yaml

from compuapply import apply, cli, db, profile, review, run, scan, score, ui_logic
from test_apply_live import write_profile_with
from test_detail import cli_args, ok_client, patch_network, posting_page
from test_score import FakeClient
from test_score import PROFILE as SCORE_PROFILE

ZONES = ["Capital Federal", "Buenos Aires-GBA"]
INSIDE = ["Villa Martelli, Buenos Aires-GBA", "Retiro, Capital Federal"]
OUTSIDE = ["Mar del Plata, Buenos Aires", "Córdoba, Córdoba"]
MODEL = "gpt-6-luna"
TODAY = date(2026, 11, 4)


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / "t.db")
    yield connection
    connection.close()


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(scan.time, "sleep", lambda seconds: None)


def add_offer(conn, n: int, location: str | None, *, detail: bool = False, points: int | None = None) -> str:
    """Oferta `n` en `location`; con `detail` trae descripción y con `points` ya tiene puntaje."""
    id16 = f"{n:016X}"
    db.upsert_offers(conn, [{"id16": id16, "data_id": f"{n:032X}", "title": f"Oferta {n}", "company": f"Empresa {n}",
                             "location": location, "url": f"https://ar.computrabajo.com/ofertas-de-trabajo/oferta-{n}-{n:032X}"}], "programador")
    if detail or points is not None:
        db.save_detail(conn, id16, {"description": "Buscamos analista.", "date_posted": "", "valid_through": "", "employment_type": "FULL_TIME"})
    if points is not None:
        db.save_score(conn, id16, MODEL, score.PROMPT_VERSION,
                      {"best_variant": "backend", "score": points, "all_scores": {"backend": points}, "reasons": [], "missing": []}, (1, 0, 1), 0.0)
    return id16


def seed(conn, **kwargs) -> tuple[list[str], list[str]]:
    """Dos ofertas dentro de la zona (1 y 2) y dos fuera (3 y 4)."""
    inside = [add_offer(conn, n, where, **kwargs) for n, where in enumerate(INSIDE, 1)]
    outside = [add_offer(conn, n, where, **kwargs) for n, where in enumerate(OUTSIDE, 3)]
    return inside, outside


# ── in_zone ──────────────────────────────────────────────────────────────────

def test_in_zone_looks_at_the_province_after_the_last_comma():
    assert profile.in_zone("Villa Martelli, Buenos Aires-GBA", ZONES) and profile.in_zone("Retiro, Capital Federal", ZONES)
    assert not profile.in_zone("Mar del Plata, Buenos Aires", ZONES) and not profile.in_zone("Córdoba, Córdoba", ZONES)


def test_in_zone_ignores_case_accents_and_extra_spaces():
    zones = ["  CAPITAL   federal ", "buenos aires-gba", "cordoba"]
    assert profile.in_zone("Retiro,capital FEDERAL", zones) and profile.in_zone("Villa Martelli,   BUENOS AIRES-GBA  ", zones)
    assert profile.in_zone("Córdoba, Córdoba", zones) and profile.in_zone("Cosquín, CÓRDOBA", ["Córdoba"])
    assert not profile.in_zone("Mar del Plata, Buenos Aires", zones)


def test_in_zone_uses_the_whole_text_when_there_is_no_comma_and_the_last_part_when_there_are_several():
    assert profile.in_zone("Capital Federal", ZONES) and not profile.in_zone("Córdoba", ZONES)
    assert profile.in_zone("Palermo, Barrio Norte, Capital Federal", ZONES)
    assert profile.province("Palermo, Barrio Norte,  Capital Federal ") == "Capital Federal" and profile.province("Córdoba") == "Córdoba"


@pytest.mark.parametrize("zones", [None, [], ()])
def test_without_zones_everything_is_in_even_an_offer_without_location(zones):
    assert profile.in_zone("Córdoba, Córdoba", zones) and profile.in_zone("", zones) and profile.in_zone(None, zones)


@pytest.mark.parametrize("location", [None, "", "   "])
def test_with_zones_an_offer_without_location_is_out(location):
    assert not profile.in_zone(location, ZONES)


# ── perfil ───────────────────────────────────────────────────────────────────

def test_the_profile_loads_zones_as_a_clean_list_and_defaults_to_none(tmp_path):
    assert profile.load_profile(write_profile_with(tmp_path, zones=[" Capital   Federal ", "Buenos Aires-GBA"]))["zones"] == ZONES
    assert profile.load_profile(write_profile_with(tmp_path))["zones"] == []
    assert profile.load_profile(write_profile_with(tmp_path, zones=[]))["zones"] == []
    assert profile.load_zones(write_profile_with(tmp_path, zones=ZONES)) == ZONES and profile.load_zones(tmp_path / "no-existe.yaml") == []


@pytest.mark.parametrize("zones", ["Capital Federal", {"a": 1}, 5, ["Capital Federal", ""], ["  "], [1], [["Capital Federal"]], [None]])
def test_zones_that_are_not_a_list_of_non_empty_texts_give_a_clear_error(tmp_path, zones):
    path = write_profile_with(tmp_path, zones=zones)
    for load in (profile.load_profile, profile.load_zones):
        with pytest.raises(RuntimeError, match=r"'zones' en .* tiene que ser una lista de provincias"):
            load(path)


# ── fetch_details ────────────────────────────────────────────────────────────

def pending_details(conn) -> list[str]:
    return [r[0] for r in conn.execute("SELECT id16 FROM offers WHERE detail_fetched_at IS NULL ORDER BY rowid")]


def test_fetch_details_only_touches_the_offers_inside_the_zones(conn):
    inside, outside = seed(conn)
    requests = []
    assert scan.fetch_details(conn, 10, client=ok_client(requests), stop_file=Path("no-existe-STOP"), zones=ZONES) == 2
    assert sorted(requests) == sorted(f"https://ar.computrabajo.com/ofertas-de-trabajo/oferta-{n}-{n:032X}" for n in (1, 2))
    assert pending_details(conn) == outside and [r[0] for r in conn.execute("SELECT status FROM offers WHERE id16 IN (?, ?)", outside)] == ["new", "new"]


def test_fetch_details_without_zones_fetches_them_all_as_before(conn):
    seed(conn)
    requests = []
    assert scan.fetch_details(conn, 10, client=ok_client(requests), stop_file=Path("no-existe-STOP")) == 4 and len(requests) == 4


def test_the_limit_is_spent_only_on_offers_inside_the_zones(conn):
    inside, outside = seed(conn)
    requests = []
    assert scan.fetch_details(conn, 1, client=ok_client(requests), stop_file=Path("no-existe-STOP"), zones=ZONES) == 1 and len(requests) == 1
    assert len(pending_details(conn)) == 3 and set(outside) <= set(pending_details(conn))


def test_run_does_not_count_the_offers_outside_the_zones_as_waiting_for_detail(conn):
    inside, outside = seed(conn)
    assert run._pending_details(conn, ZONES) == 2 and run._pending_details(conn, []) == 4 and run._pending_details(conn, None) == 4
    scan.fetch_details(conn, 10, client=ok_client([]), stop_file=Path("no-existe-STOP"), zones=ZONES)
    assert run._pending_details(conn, ZONES) == 0 and run._pending_details(conn, []) == 2


def write_zones_profile(tmp_path, zones) -> Path:
    path = tmp_path / "perfil.yaml"
    path.write_text(yaml.safe_dump({"zones": zones}), encoding="utf-8")
    return path


def test_cli_scan_does_not_download_the_detail_of_other_zones(monkeypatch, tmp_path, capsys):
    patch_network(monkeypatch, tmp_path, lambda request: httpx.Response(200, text=posting_page()))
    argv = cli_args(tmp_path, "--max-details", "5", "--profile", str(write_zones_profile(tmp_path, ["Zona Que No Existe"])))
    assert cli.main(argv) == 0 and capsys.readouterr().out.strip().endswith("detalles: 0")
    assert cli.main(cli_args(tmp_path, "--max-details", "5")) == 0 and "detalles: 5" in capsys.readouterr().out


# ── score_pending ────────────────────────────────────────────────────────────

def test_score_pending_only_scores_the_offers_inside_the_zones(conn, tmp_path):
    inside, outside = seed(conn, detail=True)
    client = FakeClient()
    result = score.score_pending(conn, {**SCORE_PROFILE, "zones": ZONES}, client, MODEL, 20, 10.0, stop_file=tmp_path / "STOP")
    assert result["scored"] == 2 and result["errors"] == 0
    assert {r[0] for r in conn.execute("SELECT id16 FROM scores")} == set(inside)
    assert [r[0] for r in conn.execute("SELECT status FROM offers WHERE id16 IN (?, ?)", outside)] == ["new", "new"]


def test_score_pending_without_zones_scores_them_all(conn, tmp_path):
    seed(conn, detail=True)
    assert score.score_pending(conn, SCORE_PROFILE, FakeClient(), MODEL, 20, 10.0, stop_file=tmp_path / "STOP")["scored"] == 4


def test_out_of_zone_offers_cost_nothing_and_do_not_use_the_limit(conn, tmp_path):
    inside, _ = seed(conn, detail=True)
    asked = FakeClient()
    result = score.score_pending(conn, {**SCORE_PROFILE, "zones": ["Córdoba"]}, asked, MODEL, 1, 10.0, stop_file=tmp_path / "STOP")
    assert result["scored"] == 1 and {r[0] for r in conn.execute("SELECT id16 FROM scores")} == {f"{4:016X}"}
    nothing = FakeClient()
    result = score.score_pending(conn, {**SCORE_PROFILE, "zones": ["Salta"]}, nothing, MODEL, 20, 10.0, stop_file=tmp_path / "STOP")
    assert result["scored"] == 0 and nothing.calls == []


# ── label_candidates ─────────────────────────────────────────────────────────

def test_label_candidates_only_offers_the_offers_inside_the_zones(conn):
    inside, outside = seed(conn, points=80)
    offered = review.label_candidates(conn, 10, random.Random(1), ZONES)
    assert sorted(o["id16"] for o in offered) == sorted(inside)
    assert sorted(o["id16"] for o in review.label_candidates(conn, 10, random.Random(1))) == sorted(inside + outside)
    assert sorted(o["id16"] for o in review.label_candidates(conn, 10, random.Random(1), zones=[])) == sorted(inside + outside)


def test_a_label_session_only_asks_about_the_zones(conn):
    inside, outside = seed(conn, points=80)
    asked = []
    review.label_session(conn, 10, ask=lambda prompt: "s", out=asked.append, rng=random.Random(1), zones=ZONES)
    assert {r[0] for r in conn.execute("SELECT id16 FROM labels")} == set(inside)


# ── plan ─────────────────────────────────────────────────────────────────────

def plan_for(conn, zones, **extra):
    loaded = {"variants": [{"id": "backend", "keywords": [], "resume": {}}],
              "apply": {"threshold": 75, "daily_cap": 10, "calibrated_with": f"{MODEL} {score.PROMPT_VERSION}"}, "zones": zones, **extra}
    return apply.plan(conn, loaded, MODEL, TODAY)


def test_the_plan_skips_a_scored_offer_from_cordoba_as_out_of_zone(conn):
    inside, outside = seed(conn, points=90)
    plan = plan_for(conn, ZONES)
    assert [i["id16"] for i in plan["send"]] == inside
    reasons = {i["id16"]: i["reason"] for i in plan["skip"]}
    assert reasons == {outside[0]: "fuera de zona (Buenos Aires)", outside[1]: "fuera de zona (Córdoba)"}


def test_out_of_zone_comes_before_the_score_and_a_missing_location_is_out(conn):
    low = add_offer(conn, 1, "Córdoba, Córdoba", points=10)
    nowhere = add_offer(conn, 2, "", points=95)
    plan = plan_for(conn, ZONES)
    assert plan["send"] == [] and {i["id16"]: i["reason"] for i in plan["skip"]} == {low: "fuera de zona (Córdoba)", nowhere: "fuera de zona (sin lugar)"}


def test_the_plan_counts_all_the_new_offers_outside_even_the_unscored_ones(conn):
    seed(conn, points=90)
    add_offer(conn, 9, "Salta, Salta")  # sin detalle ni puntaje
    plan = plan_for(conn, ZONES)
    assert plan["out_of_zone"] == 3 and plan["zones"] == ZONES
    other = add_offer(conn, 10, "Salta, Salta")
    db.mark_status(conn, other, "duplicate")  # una duplicada no cuenta
    assert plan_for(conn, ZONES)["out_of_zone"] == 3


def test_without_zones_the_plan_does_not_filter_nor_mention_zones(conn):
    inside, outside = seed(conn, points=90)
    for zones in ([], None):
        plan = plan_for(conn, zones)
        assert [i["id16"] for i in plan["send"]] == inside + outside and plan["out_of_zone"] == 0
        assert not any("zona" in note for note in apply.plan_notes(plan)) and "fuera de zona" not in apply.summary(plan)
    assert apply.plan(conn, {"variants": [{"id": "backend"}], "apply": {"threshold": 75, "daily_cap": 10}}, MODEL, TODAY)["zones"] == []


def test_the_summary_and_the_reports_say_how_many_are_out_of_zone(conn, tmp_path):
    seed(conn, points=90)
    plan = plan_for(conn, ZONES)
    assert "fuera de zona: 2" in apply.summary(plan)
    [note] = [n for n in apply.plan_notes(plan) if "fuera de zona" in n]
    assert note.startswith("Ofertas nuevas fuera de zona: 2 (zonas: Capital Federal, Buenos Aires-GBA)")
    meta = {"now": datetime(2026, 11, 4, 9, 0), "threshold": 75, "daily_cap": 10, "applied_today": 0, "model": MODEL}
    assert note in apply.write_report(plan, meta, tmp_path / "a").read_text(encoding="utf-8")
    result = {"scan": [], "details": {"ran": True, "saved": 0, "bad_url": 0, "no_detail": 0, "pending": 0}, "score": None,
              "score_requested": False, "month": None, "plan": plan, "stopped": None}
    assert note in run.write_report(result, meta, tmp_path / "b").read_text(encoding="utf-8")
    assert note in run.summary_lines(result)


def test_the_apply_report_has_no_note_about_variants_without_pdf(conn, tmp_path):
    seed(conn, points=90)
    meta = {"now": datetime(2026, 11, 4, 9, 0), "threshold": 75, "daily_cap": 10, "applied_today": 0, "model": MODEL, "variants_without_pdf": ["backend"]}
    assert "sin PDF" not in apply.write_report(plan_for(conn, ZONES), meta, tmp_path).read_text(encoding="utf-8")


# ── formulario de Parámetros ─────────────────────────────────────────────────

def test_saving_two_zones_in_the_form_keeps_them_when_the_profile_is_reloaded(tmp_path):
    path = write_profile_with(tmp_path)
    form = ui_logic.profile_to_form(path)
    assert form["zones"] == ""
    form["zones"] = "Capital Federal\n  Buenos Aires-GBA \n\n"
    assert ui_logic.form_to_profile(form)["zones"] == ZONES
    ui_logic.save_profile(ui_logic.form_to_profile(form), path)
    assert yaml.safe_load(path.read_text(encoding="utf-8"))["zones"] == ZONES
    assert ui_logic.profile_to_form(path)["zones"] == "Capital Federal\nBuenos Aires-GBA" and profile.load_profile(path)["zones"] == ZONES
    form["zones"] = "  \n"  # vacío = todas: la clave desaparece
    ui_logic.save_profile(ui_logic.form_to_profile(form), path)
    assert "zones" not in yaml.safe_load(path.read_text(encoding="utf-8")) and profile.load_profile(path)["zones"] == []


def test_the_empty_form_has_an_empty_zones_box():
    assert ui_logic.empty_form()["zones"] == "" and "zones" not in ui_logic.form_to_profile({**ui_logic.empty_form(), "variants": []})


def test_a_text_instead_of_a_list_in_the_file_is_a_clear_error_when_loading_the_profile(tmp_path):
    path = write_profile_with(tmp_path, zones="Capital Federal")
    with pytest.raises(RuntimeError, match="'zones' en .* tiene que ser una lista de provincias"):
        profile.load_profile(path)


def test_the_example_profile_shows_zones_with_an_empty_list_and_an_example_in_the_comment():
    example = (Path(__file__).parent.parent / "profile.example.yaml")
    assert profile.load_profile(example)["zones"] == []
    text = example.read_text(encoding="utf-8")
    assert "zones: []" in text and "Capital Federal" in text and "Buenos Aires-GBA" in text


# ── Resultados y Calibrar (TAREA-047) ────────────────────────────────────────

def two_places(conn):
    flores = add_offer(conn, 1, "Flores, Capital Federal", points=80)
    rosario = add_offer(conn, 2, "Rosario, Santa Fe", points=90)
    return flores, rosario


def test_results_only_lists_the_offers_in_the_zones_and_counts_the_rest(conn):
    flores, rosario = two_places(conn)
    rows, outside = ui_logic.results_split(conn, MODEL, None, ["Capital Federal"])
    assert [r["id16"] for r in rows] == [flores] and outside == 1
    assert [r["id16"] for r in ui_logic.results(conn, MODEL, zones=["Capital Federal"])] == [flores]
    assert "location" not in rows[0]


def test_results_without_zones_lists_both(conn):
    flores, rosario = two_places(conn)
    assert [r["id16"] for r in ui_logic.results(conn, MODEL)] == [rosario, flores]
    assert ui_logic.results_split(conn, MODEL, None, [])[1] == 0


def test_the_outside_count_respects_the_threshold(conn):
    two_places(conn)
    add_offer(conn, 3, "Córdoba, Córdoba", points=40)
    assert ui_logic.results_split(conn, MODEL, 75, ["Capital Federal"])[1] == 1  # la de 40 no llega al umbral, no se cuenta


def test_threshold_table_only_uses_the_labels_in_the_zones(conn):
    flores, rosario = two_places(conn)
    review.save_label(conn, flores, True)
    review.save_label(conn, rosario, False)  # etiqueta vieja de otra zona
    assert len(review.db_labels(conn, MODEL)) == 2
    assert review.db_labels(conn, MODEL, ["Capital Federal"]) == [{"score": 80, "label": True}]
    assert conn.execute("SELECT COUNT(*) FROM labels").fetchone()[0] == 2  # no se borra
    table, _ = review.threshold_table(review.db_labels(conn, MODEL, ["Capital Federal"]))
    assert next(r for r in table if r["threshold"] == 85)["send"] == 0  # la de 90 de Rosario no cuenta


def test_calibrate_candidates_leave_out_rosario(conn):
    flores, rosario = two_places(conn)
    assert [o["id16"] for o in review.label_candidates(conn, 10, random.Random(1), ["Capital Federal"])] == [flores]
