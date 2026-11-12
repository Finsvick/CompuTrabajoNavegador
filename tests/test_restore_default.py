"""TAREA-081: restaurar el CV por defecto con reintentos, `session restore-default`, rangos y motivación."""
from datetime import date
from types import SimpleNamespace

import pytest
from playwright.sync_api import TimeoutError as PWTimeout

from test_apply_live import MODEL, FakeClient, make_profile, reply
from compuapply import apply, browser, cli

TODAY = date(2026, 11, 12)


class Ctx:
    def __init__(self):
        self.opened = 0

    def new_page(self):
        self.opened += 1
        return SimpleNamespace(context=self, fresh=True)


@pytest.fixture(autouse=True)
def no_wait(monkeypatch):
    monkeypatch.setattr(apply, "RESTORE_WAIT_S", 0)


def prof():
    p = make_profile()
    p["variants"][0]["default_pdf_name"] = "CV-Base.pdf"
    return p


def result():
    return {"halted": None, "restore": None, "last_uploaded": "CV-Medida.pdf"}


def test_restore_fails_twice_then_works(monkeypatch):
    calls = []

    def flaky(page, name):
        calls.append(page)
        if len(calls) < 3:
            raise PWTimeout("Page.goto: Timeout 30000ms exceeded.")

    monkeypatch.setattr(browser, "set_default", flaky)
    ctx = Ctx()
    res = result()
    apply._restore_default(SimpleNamespace(context=ctx), prof(), res)
    assert res["restore"]["ok"] is True and res["halted"] is None and len(calls) == 3 and ctx.opened == 2
    assert getattr(calls[1], "fresh", False)  # los reintentos van en una página nueva


def test_restore_fails_three_times_the_first_line_names_the_command(monkeypatch):
    def always(page, name):
        raise PWTimeout("Page.goto: Timeout 30000ms exceeded.")

    monkeypatch.setattr(browser, "set_default", always)
    res = result()
    apply._restore_default(SimpleNamespace(context=Ctx()), prof(), res)
    assert res["restore"]["ok"] is False
    assert res["halted"].splitlines()[0].startswith("el CV por defecto quedó en «CV-Medida.pdf»: corré `python -m compuapply session restore-default`")


def test_a_non_timeout_error_is_not_retried(monkeypatch):
    calls = []

    def boom(page, name):
        calls.append(1)
        raise RuntimeError("El CV «x» no está en la cuenta")

    monkeypatch.setattr(browser, "set_default", boom)
    res = result()
    apply._restore_default(SimpleNamespace(context=Ctx()), prof(), res)
    assert len(calls) == 1 and res["restore"]["ok"] is False


def test_the_final_verification_retries_too(monkeypatch):
    calls = []

    def read(page, pages, want):
        calls.append(1)
        if len(calls) < 3:
            raise PWTimeout("Page.goto: Timeout 30000ms exceeded.")
        return [{"title": "T", "company": "C", "im": "1", "status": "Postulado", "location": ""}]

    monkeypatch.setattr(browser, "read_applications", read)
    out = apply._verify_sent(SimpleNamespace(context=Ctx()), [{"title": "T", "company": "C", "im": "1"}])
    assert out["error"] == "" and out["read"] == 1 and len(calls) == 3


# ── session restore-default ──────────────────────────────────────────────────

def test_restore_default_command_changes_only_the_default(monkeypatch, capsys):
    state = {"default": "CV-Medida.pdf", "calls": []}
    monkeypatch.setattr(browser, "account_cvs", lambda page: [{"name": n, "default": n == state["default"], "id": n} for n in ("CV-Base.pdf", "CV-Medida.pdf")])

    def set_default(page, name):
        state["calls"].append(name)
        state["default"] = name

    monkeypatch.setattr(browser, "set_default", set_default)
    for forbidden in ("upload_cv", "delete_cv", "apply_one"):
        monkeypatch.setattr(browser, forbidden, lambda *a, **k: pytest.fail("no debe llamarse"), raising=False)
    monkeypatch.setattr(browser, "is_logged_in", lambda page: True)
    monkeypatch.setattr(browser, "_page", lambda ctx: object())
    monkeypatch.setattr(cli, "_run_browser", lambda action, pw=False: action(None))
    monkeypatch.setattr(cli.profile, "load_profile", lambda path: prof())
    assert cli.main(["session", "restore-default", "--variant", "dev"]) == 0
    assert state["calls"] == ["CV-Base.pdf"]
    out = capsys.readouterr().out
    assert "CV-Medida.pdf" in out and "CV-Base.pdf" in out


# ── rangos ───────────────────────────────────────────────────────────────────

RANGE = {"text": "¿Cuántos años de experiencia tenés en ventas?", "kind": "closed", "options": [
    "Más de 5 años", "De 4 a 5 años", "De 2 a 3 años", "De 1 a 2 años", "No poseo experiencia en ventas, pero busco aprender!"]}


def test_no_poseo_is_the_no_experience_option_and_17_months_pick_1_to_2():
    assert apply._option_rules(RANGE) is not None and apply._option_rules(RANGE)[4] == ("none",)
    assert apply.experience_option(RANGE, 17) == 3
    assert apply.experience_option(RANGE, 0) == 4


# ── motivación ───────────────────────────────────────────────────────────────

def go(text):
    prof_ = make_profile()
    [entry] = apply.answer_questions([{"text": text, "kind": "open", "options": []}], prof_, FakeClient(reply()), MODEL, "dev", [], today=TODAY,
                                     offer={"title": "Asesor Inmobiliario (Zona Norte)", "keyword": "x", "company": "RE/MAX Vincit"})
    return entry


@pytest.mark.parametrize("text", ["¿Por qué te gustaría ser parte del equipo RE/MAX Vincit?", "¿Cuál es tu motivación para sumarte a RE/MAX Vincit?"])
def test_motivation_questions_are_answered_with_interest(text):
    entry = go(text)
    assert entry["source"] in ("answers", "actitud") and entry["reason"] is None
    assert "RE/MAX Vincit" in entry["text"] and len(entry["text"]) <= 500 and "(" not in entry["text"]


# ── dump del freno ───────────────────────────────────────────────────────────

def test_the_freno_dump_is_clean_and_names_offer_and_step(tmp_path):
    page = SimpleNamespace(url="https://empresas.example/x", inner_text=lambda sel: "Pantalla\nMi mail es a@b.com\n\n  texto")
    saved = apply.save_freno(tmp_path, "20261112-100000", page, {"id16": "X1", "title": "Vendedor", "company": "Acme"},
                             PWTimeout("ElementHandle.click: Timeout 30000ms exceeded."), [])
    body = saved["path"].read_text(encoding="utf-8")
    assert saved["path"].name == "20261112-100000-freno.txt" and saved["step"] == "unknown"
    assert "Vendedor" in body and "Acme" in body and "a@b.com" not in body and "Timeout 30000ms" in body
