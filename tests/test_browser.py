"""Tests de browser.py y de `login` / `session check`. Página y contexto falsos: nunca se abre un navegador."""
import builtins
import re
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from playwright.sync_api import Error as PlaywrightError

from compuapply import browser, cli

BROWSER_SOURCE = Path(browser.__file__)
UPLOAD_URL = browser.CHECK_URL
LOGIN_URL = "https://candidato.ar.computrabajo.com/acceso/?rfl=x"
PORTAL_URL = "https://secure.computrabajo.com/x?a=b"  # donde termina `uploadcv` sin sesión (observado el 2026-11-02)
HALT = "pantalla desconocida o desafío: revisá la ventana a mano"


REAL_FIND_CHROME = browser.find_chrome
CHROME_EXE = Path("C:/Falso/Google/Chrome/Application/chrome.exe")


@pytest.fixture(autouse=True)
def no_real_browser(monkeypatch):
    """Cualquier intento de abrir Playwright o Chrome de verdad rompe el test; por defecto Chrome no está instalado."""
    def boom(*args, **kwargs):
        raise AssertionError("un test intentó abrir un navegador real")
    monkeypatch.setattr(cli, "sync_playwright", boom)
    monkeypatch.setattr(browser.subprocess, "Popen", boom)
    monkeypatch.setattr(browser, "find_chrome", lambda: None)


@pytest.fixture
def chrome(monkeypatch):
    """Chrome «instalado» en una ruta falsa."""
    monkeypatch.setattr(browser, "find_chrome", lambda: CHROME_EXE)
    return CHROME_EXE


def fake_page(*final_urls: str, status: int | None = 200, challenge: str | None = None) -> SimpleNamespace:
    """Página falsa: cada `goto` deja en `url` la siguiente de `final_urls` (la última se repite)."""
    page = SimpleNamespace(url="about:blank", gotos=[])

    def goto(url, wait_until=None):
        page.gotos.append((url, wait_until))
        page.url = final_urls[min(len(page.gotos), len(final_urls)) - 1]
        return None if status is None else SimpleNamespace(status=status)

    page.goto = goto
    page.query_selector = lambda selector: object() if challenge and challenge in selector else None
    return page


def fake_context(page=None) -> SimpleNamespace:
    context = SimpleNamespace(pages=[] if page is None else [page], closed=0, new_pages=0)

    def new_page():
        context.new_pages += 1
        return context.fallback

    def close():
        context.closed += 1

    context.fallback = fake_page(UPLOAD_URL)
    context.new_page, context.close = new_page, close
    return context


def waiter():
    calls = []
    return calls, lambda *args: calls.append(args)


# ── open_context ─────────────────────────────────────────────────────────────

def test_open_context_is_visible_persistent_and_untouched():
    launched = []
    chromium = SimpleNamespace(launch_persistent_context=lambda *a, **k: launched.append((a, k)) or "contexto")
    assert browser.open_context(SimpleNamespace(chromium=chromium)) == "contexto"
    assert launched == [((str(Path(".browser-profile")),), {"headless": False, "locale": "es-AR"})]
    browser.open_context(SimpleNamespace(chromium=chromium), Path("otro-perfil"))
    assert launched[1][0] == (str(Path("otro-perfil")),)


def test_open_context_with_chrome_uses_its_own_profile_and_the_chrome_channel_without_hiding_the_automation(chrome):
    launched = []
    chromium = SimpleNamespace(launch_persistent_context=lambda *a, **k: launched.append((a, k)) or "contexto")
    assert browser.open_context(SimpleNamespace(chromium=chromium)) == "contexto"
    assert launched == [((str(Path(".browser-profile") / "chrome"),),
                         {"channel": "chrome", "headless": False, "locale": "es-AR"})]  # ni flags ni argumentos extra
    browser.open_context(SimpleNamespace(chromium=chromium), Path("otro-perfil"))
    assert launched[1][0] == (str(Path("otro-perfil")),)


def test_constants():
    assert browser.CHECK_URL == "https://candidato.ar.computrabajo.com/candidate/cv/uploadcv"
    assert browser.PROFILE_DIR == Path(".browser-profile")
    assert browser.CHROME_PROFILE == Path(".browser-profile") / "chrome"


# ── Chrome: dónde está, cómo se abre ─────────────────────────────────────────

def make_chrome(base: Path) -> Path:
    exe = base / "Google" / "Chrome" / "Application" / "chrome.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    return exe


@pytest.fixture
def clean_env(monkeypatch):
    for variable in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        monkeypatch.delenv(variable, raising=False)
    return monkeypatch


def test_find_chrome_looks_in_program_files_then_x86_then_local_app_data(clean_env, tmp_path):
    assert REAL_FIND_CHROME() is None
    local = make_chrome(tmp_path / "local")
    clean_env.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    assert REAL_FIND_CHROME() == local
    x86 = make_chrome(tmp_path / "x86")
    clean_env.setenv("PROGRAMFILES(X86)", str(tmp_path / "x86"))
    assert REAL_FIND_CHROME() == x86
    program = make_chrome(tmp_path / "pf")
    clean_env.setenv("PROGRAMFILES", str(tmp_path / "pf"))
    assert REAL_FIND_CHROME() == program


def test_find_chrome_ignores_a_folder_without_the_exe(clean_env, tmp_path):
    clean_env.setenv("PROGRAMFILES", str(tmp_path))
    (tmp_path / "Google" / "Chrome" / "Application").mkdir(parents=True)
    assert REAL_FIND_CHROME() is None


def test_browser_name_and_profile_follow_whether_chrome_is_installed(chrome, monkeypatch):
    assert browser.browser_name() == "Chrome" and browser.profile_dir() == Path(".browser-profile") / "chrome"
    monkeypatch.setattr(browser, "find_chrome", lambda: None)
    assert browser.browser_name() == "Chromium" and browser.profile_dir() == Path(".browser-profile")


def test_start_chrome_is_an_ordinary_process_with_its_own_profile_and_no_automation_flags(tmp_path, monkeypatch):
    started = []
    monkeypatch.setattr(browser.subprocess, "Popen", lambda *a, **k: started.append((a, k)) or "proceso")
    assert browser.start_chrome(CHROME_EXE, tmp_path / "perfil") == "proceso"
    [(positional, keywords)] = started
    [command] = positional
    assert keywords == {}  # nada de stdin/entorno/flags de creación: un proceso común
    assert command == [str(CHROME_EXE), f"--user-data-dir={(tmp_path / 'perfil').resolve()}", "--no-first-run",
                       "--no-default-browser-check", "--disable-extensions", browser.CHECK_URL]
    assert Path(command[1].split("=", 1)[1]).is_absolute()
    assert not any("remote-debugging" in part or "enable-automation" in part for part in command)  # y ningún otro flag de más


def test_start_chrome_defaults_to_the_chrome_profile_folder(monkeypatch):
    started = []
    monkeypatch.setattr(browser.subprocess, "Popen", lambda *a, **k: started.append(a[0]) or object())
    browser.start_chrome(CHROME_EXE)
    assert started[0][1] == f"--user-data-dir={browser.CHROME_PROFILE.resolve()}"


# ── is_logged_in ─────────────────────────────────────────────────────────────

def test_logged_in_when_the_check_page_stays_put():
    page = fake_page(UPLOAD_URL)
    assert browser.is_logged_in(page) is True
    assert page.gotos == [(UPLOAD_URL, "domcontentloaded")]


@pytest.mark.parametrize("url", [LOGIN_URL, "https://candidato.ar.computrabajo.com/acceso/", "https://candidato.ar.computrabajo.com/acceso"])
def test_not_logged_in_when_redirected_to_the_login_page(url):
    assert browser.is_logged_in(fake_page(url)) is False


def test_missing_response_is_judged_by_the_final_url():
    assert browser.is_logged_in(fake_page(UPLOAD_URL, status=None)) is True
    assert browser.is_logged_in(fake_page(LOGIN_URL, status=None)) is False


@pytest.mark.parametrize("challenge", ["recaptcha", "hcaptcha", "challenges.cloudflare.com"])
def test_captcha_iframe_halts_even_on_the_right_page(challenge):
    with pytest.raises(RuntimeError, match=re.escape(HALT)):
        browser.is_logged_in(fake_page(UPLOAD_URL, challenge=challenge))
    with pytest.raises(RuntimeError, match=re.escape(HALT)):
        browser.is_logged_in(fake_page(LOGIN_URL, challenge=challenge))


@pytest.mark.parametrize("status", [403, 429, 404, 500, 503])
def test_http_errors_halt_with_the_code_in_the_message(status):
    with pytest.raises(RuntimeError, match=rf"{status}.*{re.escape(HALT)}"):
        browser.is_logged_in(fake_page(UPLOAD_URL, status=status))


@pytest.mark.parametrize("url", [
    "https://ar.computrabajo.com/",
    "https://www.google.com/",
    "https://candidato.ar.computrabajo.com.otro.sitio/acceso/",
    "https://secure.computrabajo.com.otro.sitio/",
    "about:blank",
    "chrome-error://chromewebdata/",
])
def test_other_host_halts(url):
    with pytest.raises(RuntimeError, match=re.escape(HALT)):
        browser.is_logged_in(fake_page(url))


def test_other_host_message_shows_host_and_path_but_never_query_or_fragment():
    with pytest.raises(RuntimeError, match=re.escape("(www.google.com/search)")) as err:
        browser.is_logged_in(fake_page("https://www.google.com/search?token=SECRETO#x"))
    assert "SECRETO" not in str(err.value) and "token" not in str(err.value) and "#x" not in str(err.value)
    with pytest.raises(RuntimeError, match=re.escape("(sin host)")):
        browser.is_logged_in(fake_page("about:blank"))


# ── portal de acceso (secure.computrabajo.com) ───────────────────────────────

def test_login_hosts_is_the_access_portal():
    assert browser.LOGIN_HOSTS == ("secure.computrabajo.com",)


@pytest.mark.parametrize("url", [PORTAL_URL, "https://secure.computrabajo.com/", "https://SECURE.computrabajo.com/login?rfl=x#y"])
def test_access_portal_means_no_session(url):
    page = fake_page(url)
    assert browser.is_logged_in(page) is False
    assert page.gotos == [(UPLOAD_URL, "domcontentloaded")]  # solo cargó la página de verificación


@pytest.mark.parametrize("challenge", ["recaptcha", "hcaptcha", "challenges.cloudflare.com"])
def test_captcha_iframe_on_the_access_portal_does_not_halt(challenge):
    assert browser.is_logged_in(fake_page(PORTAL_URL, challenge=challenge)) is False


@pytest.mark.parametrize("status", [403, 429, 500])
def test_http_errors_on_the_access_portal_still_halt(status):
    with pytest.raises(RuntimeError, match=rf"{status}.*{re.escape(HALT)}"):
        browser.is_logged_in(fake_page(PORTAL_URL, status=status))


def test_is_logged_in_only_ever_loads_the_check_url():
    page = fake_page(UPLOAD_URL, LOGIN_URL)
    browser.is_logged_in(page)
    browser.is_logged_in(page)
    assert {url for url, _ in page.gotos} == {UPLOAD_URL}


# ── login y session_check ────────────────────────────────────────────────────

def test_login_with_active_session_does_not_wait(capsys):
    page = fake_page(UPLOAD_URL)
    calls, wait = waiter()
    assert browser.login(fake_context(page), wait=wait) is True
    assert calls == [] and len(page.gotos) == 1
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("second_url, expected", [(UPLOAD_URL, True), (LOGIN_URL, False)])
def test_login_without_session_waits_once_and_checks_again(capsys, second_url, expected):
    page = fake_page(LOGIN_URL, second_url)
    calls, wait = waiter()
    assert browser.login(fake_context(page), wait=wait) is expected
    assert len(calls) == 1 and len(page.gotos) == 2  # verifica una sola vez más
    assert capsys.readouterr().out.strip() == (
        "Iniciá sesión en la ventana de Chromium. Cuando termines, volvé acá y apretá Enter.")


def test_login_prompt_is_printed_before_waiting(capsys):
    order = []
    page = fake_page(LOGIN_URL, UPLOAD_URL)
    browser.login(fake_context(page), wait=lambda: order.append(capsys.readouterr().out.strip()))
    assert order == ["Iniciá sesión en la ventana de Chromium. Cuando termines, volvé acá y apretá Enter."]


def test_login_from_the_access_portal_waits_once_and_succeeds_after_the_user_logs_in(capsys):
    page = fake_page(PORTAL_URL, UPLOAD_URL)
    calls, wait = waiter()
    assert browser.login(fake_context(page), wait=wait) is True
    assert len(calls) == 1 and len(page.gotos) == 2
    assert "Iniciá sesión en la ventana de Chromium" in capsys.readouterr().out


def test_login_still_on_the_access_portal_returns_false_after_one_more_check():
    page = fake_page(PORTAL_URL)
    calls, wait = waiter()
    assert browser.login(fake_context(page), wait=wait) is False
    assert len(calls) == 1 and len(page.gotos) == 2


def test_login_halts_if_the_second_check_hits_a_challenge():
    calls, wait = waiter()
    page = fake_page(LOGIN_URL, "https://www.google.com/")
    with pytest.raises(RuntimeError, match=re.escape(HALT)):
        browser.login(fake_context(page), wait=wait)
    assert len(calls) == 1


def test_login_halts_before_waiting_if_the_first_check_hits_a_challenge():
    calls, wait = waiter()
    with pytest.raises(RuntimeError):
        browser.login(fake_context(fake_page(UPLOAD_URL, status=429)), wait=wait)
    assert calls == []


def test_session_check_active_and_expired():
    assert browser.session_check(fake_context(fake_page(UPLOAD_URL))) is True
    assert browser.session_check(fake_context(fake_page(LOGIN_URL))) is False


def test_session_check_on_the_access_portal_is_expired():
    assert browser.session_check(fake_context(fake_page(PORTAL_URL))) is False


def test_session_check_uses_the_first_page_or_opens_one():
    page = fake_page(UPLOAD_URL)
    context = fake_context(page)
    browser.session_check(context)
    assert len(page.gotos) == 1 and context.new_pages == 0
    empty = fake_context()
    browser.session_check(empty)
    assert empty.new_pages == 1 and len(empty.fallback.gotos) == 1


# ── lo que el módulo NO hace ─────────────────────────────────────────────────

def test_browser_source_never_touches_session_data_or_state_changing_urls():
    source = BROWSER_SOURCE.read_text(encoding="utf-8").lower()
    for forbidden in ("cookies", "storage_state", "user_agent", "candidate/apply", "headless=true", "args=",
                      "remote-debugging", "enable-automation", "disable-blink-features", "ignore_default_args"):
        assert forbidden not in source, forbidden
    assert "headless=false" in source
    # Solo se navega a la página de verificación o al detalle de la oferta: nunca a match/ ni a apply/ (las toca el botón del sitio).
    gotos = [line.strip() for line in source.splitlines() if ".goto(" in line]
    assert gotos and all("check_url" in line or "(page, url" in line or "page.goto(url" in line for line in gotos), gotos
    calls = [line.strip() for line in source.splitlines() if "_goto(page" in line and "def _goto" not in line]
    assert calls and all("upload_url" in line or 'offer["url"]' in line or "(page, url, statuses)" in line for line in calls), calls
    assert not any("match" in line and "applications" not in line for line in calls)  # nunca a match/: solo a «Mis postulaciones»


# ── CLI ──────────────────────────────────────────────────────────────────────

def patch_playwright(monkeypatch, context, *, launch_error=None, events=None) -> list:
    """Reemplaza `sync_playwright` por uno falso cuyo Chromium devuelve `context`; devuelve la lista de lanzamientos (perfil, opciones)."""
    launches = []

    def launch(profile_dir, **kwargs):
        launches.append((profile_dir, kwargs))
        if events is not None:
            events.append("playwright")
        if launch_error:
            raise launch_error
        return context

    pw = SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch))

    @contextmanager
    def fake_sync_playwright():
        yield pw

    monkeypatch.setattr(cli, "sync_playwright", fake_sync_playwright)
    return launches


def forbid_input(monkeypatch):
    def no_input(*args):
        raise AssertionError("no tenía que pedir Enter")
    monkeypatch.setattr(builtins, "input", no_input)


def test_cli_login_with_active_session(monkeypatch, capsys):
    context = fake_context(fake_page(UPLOAD_URL))
    patch_playwright(monkeypatch, context)
    forbid_input(monkeypatch)
    assert cli.main(["login"]) == 0
    assert capsys.readouterr().out.strip() == "Sesión iniciada. El perfil quedó en .browser-profile/"
    assert context.closed == 1


def test_cli_login_waits_for_enter_and_succeeds(monkeypatch, capsys):
    context = fake_context(fake_page(LOGIN_URL, UPLOAD_URL))
    patch_playwright(monkeypatch, context)
    enters = []
    monkeypatch.setattr(builtins, "input", lambda *a: enters.append(a) or "")
    assert cli.main(["login"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out == ["Iniciá sesión en la ventana de Chromium. Cuando termines, volvé acá y apretá Enter.",
                   "Sesión iniciada. El perfil quedó en .browser-profile/"]
    assert len(enters) == 1 and context.closed == 1


def test_cli_login_still_without_session_exits_1(monkeypatch, capsys):
    context = fake_context(fake_page(LOGIN_URL))
    patch_playwright(monkeypatch, context)
    monkeypatch.setattr(builtins, "input", lambda *a: "")
    assert cli.main(["login"]) == 1
    captured = capsys.readouterr()
    assert "sigue sin haber sesión" in captured.err and "Sesión iniciada" not in captured.out
    assert context.closed == 1


def test_cli_login_challenge_exits_1_and_closes_the_context(monkeypatch, capsys):
    context = fake_context(fake_page(UPLOAD_URL, challenge="recaptcha"))
    patch_playwright(monkeypatch, context)
    forbid_input(monkeypatch)
    assert cli.main(["login"]) == 1
    assert HALT in capsys.readouterr().err and context.closed == 1


def test_cli_login_without_interactive_terminal_exits_1_and_closes(monkeypatch, capsys):
    context = fake_context(fake_page(LOGIN_URL))
    patch_playwright(monkeypatch, context)

    def eof(*args):
        raise EOFError

    monkeypatch.setattr(builtins, "input", eof)
    assert cli.main(["login"]) == 1
    assert "terminal interactiva" in capsys.readouterr().err and context.closed == 1


def test_cli_login_ctrl_c_closes_the_context(monkeypatch, capsys):
    context = fake_context(fake_page(LOGIN_URL))
    patch_playwright(monkeypatch, context)

    def interrupted(*args):
        raise KeyboardInterrupt

    monkeypatch.setattr(builtins, "input", interrupted)
    assert cli.main(["login"]) == 1
    assert "Cancelado" in capsys.readouterr().err and context.closed == 1


def test_cli_playwright_error_is_reported_without_a_traceback(monkeypatch, capsys):
    patch_playwright(monkeypatch, fake_context(), launch_error=PlaywrightError("Executable doesn't exist\nCall log:\n- muchas líneas"))
    assert cli.main(["session", "check"]) == 1
    err = capsys.readouterr().err
    assert "Error: Executable doesn't exist" in err and "playwright install chromium" in err and "Call log" not in err


def test_cli_session_check_active(monkeypatch, capsys):
    context = fake_context(fake_page(UPLOAD_URL))
    patch_playwright(monkeypatch, context)
    assert cli.main(["session", "check"]) == 0
    assert capsys.readouterr().out.strip() == "Sesión activa" and context.closed == 1


def test_cli_session_check_expired(monkeypatch, capsys):
    context = fake_context(fake_page(LOGIN_URL))
    patch_playwright(monkeypatch, context)
    forbid_input(monkeypatch)  # session check nunca espera al usuario
    assert cli.main(["session", "check"]) == 1
    captured = capsys.readouterr()
    assert captured.err.strip() == "Sesión vencida: corré python -m compuapply login" and captured.out == ""
    assert context.closed == 1


def test_cli_session_check_on_the_access_portal_says_expired(monkeypatch, capsys):
    context = fake_context(fake_page(PORTAL_URL))
    patch_playwright(monkeypatch, context)
    forbid_input(monkeypatch)
    assert cli.main(["session", "check"]) == 1
    captured = capsys.readouterr()
    assert captured.err.strip() == "Sesión vencida: corré python -m compuapply login" and captured.out == ""
    assert context.closed == 1


def test_cli_login_from_the_access_portal_asks_the_user_to_log_in(monkeypatch, capsys):
    context = fake_context(fake_page(PORTAL_URL, UPLOAD_URL))
    patch_playwright(monkeypatch, context)
    enters = []
    monkeypatch.setattr(builtins, "input", lambda *a: enters.append(a) or "")
    assert cli.main(["login"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "Iniciá sesión en la ventana de Chromium. Cuando termines, volvé acá y apretá Enter.",
        "Sesión iniciada. El perfil quedó en .browser-profile/"]
    assert len(enters) == 1 and context.closed == 1


def test_cli_session_check_halts_on_http_429(monkeypatch, capsys):
    context = fake_context(fake_page(UPLOAD_URL, status=429))
    patch_playwright(monkeypatch, context)
    assert cli.main(["session", "check"]) == 1
    assert "429" in capsys.readouterr().err and context.closed == 1


# ── CLI con Chrome ───────────────────────────────────────────────────────────

class FakeChrome:
    """El proceso de Chrome: `wait()` es el momento en que el usuario cierra la ventana."""

    def __init__(self, events, interrupt=False):
        self.events, self.interrupt = events, interrupt

    def wait(self):
        self.events.append("wait")
        if self.interrupt:
            raise KeyboardInterrupt


def patch_chrome(monkeypatch, events, *, interrupt=False, start_error=None):
    started = []

    def popen(*args, **kwargs):
        if start_error:
            raise start_error
        started.append((args, kwargs))
        events.append("popen")
        return FakeChrome(events, interrupt)

    monkeypatch.setattr(browser, "find_chrome", lambda: CHROME_EXE)
    monkeypatch.setattr(browser.subprocess, "Popen", popen)
    return started


def test_cli_login_with_chrome_opens_it_waits_for_it_to_close_and_only_then_checks_with_playwright(monkeypatch, capsys):
    events, context = [], fake_context(fake_page(UPLOAD_URL))
    started = patch_chrome(monkeypatch, events)
    launches = patch_playwright(monkeypatch, context, events=events)
    forbid_input(monkeypatch)  # con Chrome no hace falta ningún Enter: se sigue solo al cerrar la ventana
    assert cli.main(["login"]) == 0
    assert events == ["popen", "wait", "playwright"]
    [(positional, keywords)] = started
    [command] = positional
    assert command[0] == str(CHROME_EXE) and command[1] == f"--user-data-dir={browser.CHROME_PROFILE.resolve()}"
    assert command[-1] == browser.CHECK_URL and keywords == {} and "--disable-extensions" in command
    assert not any("remote-debugging" in part or "enable-automation" in part for part in command)
    assert launches == [(str(browser.CHROME_PROFILE), {"channel": "chrome", "headless": False, "locale": "es-AR"})]
    out = capsys.readouterr().out.splitlines()
    assert out == ['Se abrió Chrome. Iniciá sesión en Computrabajo (con Google o con tu mail), tildá "Recordarme" si aparece y '
                   "cerrá esa ventana de Chrome.", "Sesión iniciada. El perfil quedó en .browser-profile/chrome/"]
    assert context.closed == 1


def test_cli_login_with_chrome_without_a_session_after_closing_it_exits_1(monkeypatch, capsys):
    context = fake_context(fake_page(PORTAL_URL))
    patch_chrome(monkeypatch, [])
    patch_playwright(monkeypatch, context)
    assert cli.main(["login"]) == 1
    captured = capsys.readouterr()
    assert "sigue sin haber sesión" in captured.err and "Sesión iniciada" not in captured.out and context.closed == 1


def test_cli_login_with_chrome_halts_on_a_challenge_found_by_the_check(monkeypatch, capsys):
    context = fake_context(fake_page(UPLOAD_URL, challenge="recaptcha"))
    patch_chrome(monkeypatch, [])
    patch_playwright(monkeypatch, context)
    assert cli.main(["login"]) == 1
    assert HALT in capsys.readouterr().err and context.closed == 1


def test_cli_login_with_chrome_that_does_not_start_exits_1_without_opening_playwright(monkeypatch, capsys):
    patch_chrome(monkeypatch, [], start_error=OSError("no arranca"))
    assert cli.main(["login"]) == 1  # si abriera Playwright, el autouse rompería el test
    assert "no se pudo abrir Chrome (no arranca)" in capsys.readouterr().err


def test_cli_login_with_chrome_ctrl_c_while_waiting_exits_1_without_opening_playwright(monkeypatch, capsys):
    events = []
    patch_chrome(monkeypatch, events, interrupt=True)
    assert cli.main(["login"]) == 1
    assert "Cancelado" in capsys.readouterr().err and events == ["popen", "wait"]


def test_cli_login_without_chrome_says_so_and_keeps_the_chromium_flow(monkeypatch, capsys):
    context = fake_context(fake_page(LOGIN_URL, UPLOAD_URL))
    launches = patch_playwright(monkeypatch, context)
    monkeypatch.setattr(builtins, "input", lambda *a: "")
    assert cli.main(["login"]) == 0
    captured = capsys.readouterr()
    assert captured.err.strip() == "No se encontró Chrome: se usa Chromium, con el perfil .browser-profile/."
    assert captured.out.splitlines()[0] == "Iniciá sesión en la ventana de Chromium. Cuando termines, volvé acá y apretá Enter."
    assert launches == [(str(browser.PROFILE_DIR), {"headless": False, "locale": "es-AR"})]


def test_cli_session_check_with_chrome_uses_the_chrome_channel_and_profile(monkeypatch, capsys):
    monkeypatch.setattr(browser, "find_chrome", lambda: CHROME_EXE)
    launches = patch_playwright(monkeypatch, fake_context(fake_page(UPLOAD_URL)))
    assert cli.main(["session", "check"]) == 0
    assert launches == [(str(browser.CHROME_PROFILE), {"channel": "chrome", "headless": False, "locale": "es-AR"})]


def test_cli_playwright_error_names_chrome_when_chrome_is_used(monkeypatch, capsys):
    monkeypatch.setattr(browser, "find_chrome", lambda: CHROME_EXE)
    patch_playwright(monkeypatch, fake_context(), launch_error=PlaywrightError("Chromium distribution 'chrome' is not found\nCall log:\n- x"))
    assert cli.main(["session", "check"]) == 1
    err = capsys.readouterr().err
    assert "Revisá que Chrome esté instalado" in err and ".browser-profile/chrome/" in err
    assert "playwright install chromium" not in err and "Call log" not in err
