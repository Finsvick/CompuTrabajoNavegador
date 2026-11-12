from pathlib import Path

import pytest

import fakesite
from compuapply import apply, browser

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def fake_clock(monkeypatch):
    """La espera del chequeo de la pantalla final usa un reloj falso que avanza con `wait_for_timeout`: ningún test espera de verdad."""
    fakesite.CLOCK.t = 0.0
    monkeypatch.setattr(browser, "_now", fakesite.CLOCK.now)


@pytest.fixture(autouse=True)
def runs_in_tmp(monkeypatch, tmp_path_factory):
    """Las pantallas guardadas van a una carpeta temporal, nunca al `data/runs` del repo; si un test pide escribir dentro de `data/` del repo,
    falla (no se lee nada de esa carpeta)."""
    monkeypatch.setattr(apply, "RUNS_DIR", tmp_path_factory.mktemp("runs"))
    monkeypatch.setattr(apply, "RESTORE_WAIT_S", 0)  # los reintentos por timeout no esperan 10 s
    original = apply.save_screen

    def guarded(runs_dir, *args, **kwargs):
        target = Path(runs_dir).resolve()
        assert (REPO / "data") not in (target, *target.parents), f"un test quiso escribir en {target}"
        return original(runs_dir, *args, **kwargs)

    monkeypatch.setattr(apply, "save_screen", guarded)
