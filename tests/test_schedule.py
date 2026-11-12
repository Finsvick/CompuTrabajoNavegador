"""Tests de schedule.py y del comando `schedule`. Un PowerShell falso: ningún test crea, quita ni consulta la tarea de verdad."""
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from compuapply import cli, schedule

PYTHONW = Path("C:/Python313/pythonw.exe")
PROJECT = Path("C:/Dev/compuapply")
FULL = '{"at":"2026-11-02T09:00:00","last":"2026-11-03T09:00:12","result":0,"next":"2026-11-04T09:00:00"}'


class FakePowerShell:
    """Corre en lugar de PowerShell: anota cada comando y responde `(stdout, stderr, código)` en orden (si no hay, éxito vacío)."""

    def __init__(self, *answers):
        self.calls, self._answers = [], list(answers)

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        stdout, stderr, code = self._answers.pop(0) if self._answers else ("", "", 0)
        return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=code)

    @property
    def scripts(self) -> list[str]:
        return [command[-1] for command, _ in self.calls]


@pytest.fixture(autouse=True)
def never_touch_the_real_task(monkeypatch):
    """Si un test se olvida del PowerShell falso, falla en vez de tocar la tarea de Windows."""
    def forbidden(*args, **kwargs):
        raise AssertionError("este test no tiene que tocar la tarea programada de verdad")

    monkeypatch.setattr(schedule.subprocess, "run", forbidden)


@pytest.fixture
def powershell(monkeypatch):
    def install(*answers) -> FakePowerShell:
        fake = FakePowerShell(*answers)
        monkeypatch.setattr(schedule.subprocess, "run", fake)
        return fake

    return install


def test_the_guard_blocks_the_real_runner():
    with pytest.raises(AssertionError, match="no tiene que tocar la tarea programada de verdad"):
        schedule.apply(["powershell", "-Command", "Get-ScheduledTask"])


# ── valid_time ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text, expected", [
    ("9:05", "09:05"), ("09:00", "09:00"), ("0:00", "00:00"), ("00:00", "00:00"), ("23:59", "23:59"),
    ("12:00", "12:00"), (" 7:30 ", "07:30"), ("\t18:45\n", "18:45"),
])
def test_valid_time_accepts_hours_and_pads_them(text, expected):
    assert schedule.valid_time(text) == expected


@pytest.mark.parametrize("text", [
    "24:00", "12:60", "abc", "", "9", "9:5", "09:00:00", "-1:00", "1:2:3", "12.30", "12h30", "99:99", "١٢:٣٠",  # dígitos árabes
    "09:00'; Remove-Item x; '", "9 :05", ":30", "009:00",
])
def test_valid_time_rejects_everything_else(text):
    with pytest.raises(RuntimeError, match="HH:MM"):
        schedule.valid_time(text)


# ── comandos ─────────────────────────────────────────────────────────────────

def test_task_name():
    assert schedule.TASK_NAME == "compuapply-run"


def test_register_command_runs_run_daily_in_the_project_without_live_or_admin():
    command = schedule.register_command(PYTHONW, PROJECT, "09:00")
    assert command[:4] == ["powershell", "-NoProfile", "-NonInteractive", "-Command"] and len(command) == 5
    script = command[4]
    assert script.startswith("$ErrorActionPreference = 'Stop'; ")
    assert f"-Execute '{PYTHONW}' -Argument '-m compuapply run' -WorkingDirectory '{PROJECT}'" in script
    assert "New-ScheduledTaskTrigger -Daily -At '09:00'" in script
    assert ("New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries "
            "-ExecutionTimeLimit (New-TimeSpan -Hours 2)") in script
    assert ("Register-ScheduledTask -TaskName 'compuapply-run' -Action $action -Trigger $trigger -Settings $settings "
            "-Description 'compuapply: corrida diaria en modo seco' -Force") in script
    assert "--live" not in script and "-RunLevel" not in script and "-Verb" not in script  # sin administrador


def test_register_command_lets_the_task_start_and_keep_running_on_battery():
    script = schedule.register_command(PYTHONW, PROJECT, "09:00")[4]
    settings = script.split("$settings = ", 1)[1].split(";", 1)[0]
    assert "-AllowStartIfOnBatteries -DontStopIfGoingOnBatteries" in settings and "--live" not in script
    assert "-StartWhenAvailable" in settings and "(New-TimeSpan -Hours 2)" in settings  # lo de antes sigue


def test_register_command_normalizes_the_time():
    assert "-At '09:05'" in schedule.register_command(PYTHONW, PROJECT, " 9:05 ")[4]


@pytest.mark.parametrize("at", ["24:00", "12:60", "abc", "", "09:00'; Remove-Item C:\\; '"])
def test_register_command_refuses_a_bad_time_so_nothing_can_be_injected(at):
    with pytest.raises(RuntimeError, match="HH:MM"):
        schedule.register_command(PYTHONW, PROJECT, at)


def test_register_command_doubles_single_quotes_in_the_paths():
    script = schedule.register_command(Path("C:/Users/O'Brien/Python/pythonw.exe"), Path("C:/Dev/it's here"), "09:00")[4]
    assert "'C:\\Users\\O''Brien\\Python\\pythonw.exe'" in script and "'C:\\Dev\\it''s here'" in script
    assert script.count("'") % 2 == 0  # las comillas siguen balanceadas


def test_register_command_keeps_dollar_signs_and_semicolons_of_a_path_inside_the_quotes():
    script = schedule.register_command(PYTHONW, Path("C:/Dev/$x; y"), "09:00")[4]
    assert "-WorkingDirectory 'C:\\Dev\\$x; y'" in script  # entre comillas simples PowerShell no interpreta nada


def test_unregister_command_only_removes_a_task_that_exists_and_says_which_case_it_was():
    command = schedule.unregister_command()
    assert command[:4] == ["powershell", "-NoProfile", "-NonInteractive", "-Command"] and len(command) == 5
    assert command[4] == (
        "$ErrorActionPreference = 'Stop'; if (Get-ScheduledTask -TaskName 'compuapply-run' -ErrorAction SilentlyContinue) "
        "{ Unregister-ScheduledTask -TaskName 'compuapply-run' -Confirm:$false; 'quitada' } else { 'no-existia' }; exit 0")


def test_query_command_asks_for_the_task_and_its_info_as_json():
    command = schedule.query_command()
    assert command[:4] == ["powershell", "-NoProfile", "-NonInteractive", "-Command"]
    script = command[4]
    assert "Get-ScheduledTaskInfo -TaskName 'compuapply-run'" in script and "ConvertTo-Json -Compress" in script
    assert all(name in script for name in ("at =", "last =", "result =", "next ="))


@pytest.mark.parametrize("command", [schedule.query_command(), schedule.unregister_command()], ids=["query", "unregister"])
def test_query_and_unregister_end_with_exit_0_so_a_missing_task_is_not_an_error(command):
    # `powershell -Command` sale con 1 si lo último que corrió falló en silencio (la tarea no existe).
    assert command[4].endswith("; exit 0")


def test_query_command_stops_on_real_errors_and_treats_only_object_not_found_as_no_task():
    script = schedule.query_command()[4]
    assert script.startswith("$ErrorActionPreference = 'Stop'; try { $task = Get-ScheduledTask -TaskName 'compuapply-run' } "
                             "catch { if ($_.CategoryInfo.Category -eq 'ObjectNotFound') { exit 0 } else { throw } }; if ($task) { ")
    assert "SilentlyContinue" not in script  # ningún error se traga: solo el ObjectNotFound se atrapa
    assert script.count("ConvertTo-Json") == 1 and "Write-" not in script  # solo imprime dentro del if


def test_cli_schedule_show_without_a_task_is_not_an_error_and_another_error_is(powershell, capsys):
    powershell(("", "", 0), ("", "Get-ScheduledTask : El servicio no responde.\nEn línea: 1", 1))
    assert cli.main(["schedule", "show"]) == 0
    assert capsys.readouterr().out == "No programada\n"
    assert cli.main(["schedule", "show"]) == 1
    assert capsys.readouterr().err == ("Error: No se pudo usar el Programador de tareas de Windows: "
                                       "Get-ScheduledTask : El servicio no responde.\n")


def test_no_command_ever_adds_live_or_touches_anything_but_the_task():
    for command in (schedule.register_command(PYTHONW, PROJECT, "09:00"), schedule.unregister_command(), schedule.query_command()):
        assert "--live" not in " ".join(command) and "Remove-Item" not in " ".join(command)


# ── parse_query ──────────────────────────────────────────────────────────────

def test_parse_query_full_json():
    assert schedule.parse_query(FULL) == {
        "at": "09:00", "last": datetime(2026, 11, 3, 9, 0, 12), "result": 0, "next": datetime(2026, 11, 4, 9, 0)}


@pytest.mark.parametrize("stdout", ["", "   ", "\r\n"])
def test_parse_query_empty_output_means_the_task_does_not_exist(stdout):
    assert schedule.parse_query(stdout) is None


def test_parse_query_a_task_that_never_ran_has_no_last_run():
    stdout = '{"at":"2026-11-02T09:00:00","last":"2026-11-02T09:00:00","result":267011,"next":"2026-11-03T09:00:00"}'
    info = schedule.parse_query(stdout)
    assert info["result"] == schedule.NEVER_RAN == 267011 and info["last"] is None
    assert info["next"] == datetime(2026, 11, 3, 9, 0) and info["at"] == "09:00"


def test_parse_query_keeps_a_failure_code_and_missing_dates():
    info = schedule.parse_query('{"at":"2026-11-02T21:30:00","last":null,"result":1,"next":null}')
    assert info == {"at": "21:30", "last": None, "result": 1, "next": None}
    assert schedule.parse_query('{"result":0}') == {"at": None, "last": None, "result": 0, "next": None}


def test_parse_query_takes_the_time_as_written_even_with_an_offset():
    assert schedule.parse_query('{"at":"2026-11-02T09:00:00-03:00","result":0}')["at"] == "09:00"


def test_parse_query_unreadable_dates_become_none():
    info = schedule.parse_query('{"at":"ayer","last":"nunca","result":0,"next":12}')
    assert info == {"at": None, "last": None, "result": 0, "next": None}


@pytest.mark.parametrize("stdout", ["esto no es json", "[1, 2]", "{}", '{"result":"x"}', '{"at":"2026-11-02T09:00:00"}', "null"])
def test_parse_query_garbage_is_a_clear_error(stdout):
    with pytest.raises(RuntimeError, match="No se pudo leer el estado de la tarea programada"):
        schedule.parse_query(stdout)


# ── apply ────────────────────────────────────────────────────────────────────

def test_apply_returns_stdout_and_runs_without_a_console_window():
    fake = FakePowerShell(("salida\n", "", 0))
    assert schedule.apply(["powershell", "-Command", "x"], runner=fake) == "salida\n"
    [(command, kwargs)] = fake.calls
    assert command == ["powershell", "-Command", "x"]
    assert kwargs["creationflags"] == 0x08000000 == schedule.CREATE_NO_WINDOW
    assert kwargs["capture_output"] is True and kwargs["text"] is True and kwargs["errors"] == "replace"


def test_apply_error_shows_only_the_first_line_of_what_powershell_said():
    fake = FakePowerShell(("", "\n  Register-ScheduledTask : Acceso denegado.\nEn línea: 1 Carácter: 90\n+ CategoryInfo ...", 1))
    with pytest.raises(RuntimeError) as err:
        schedule.apply(["x"], runner=fake)
    assert str(err.value) == ("No se pudo usar el Programador de tareas de Windows: "
                              "Register-ScheduledTask : Acceso denegado.")
    assert "En línea" not in str(err.value) and "CategoryInfo" not in str(err.value)


def test_apply_error_without_details():
    with pytest.raises(RuntimeError, match="Programador de tareas de Windows: sin detalle"):
        schedule.apply(["x"], runner=FakePowerShell(("", "  \n", 1)))
    with pytest.raises(RuntimeError, match="sin detalle"):
        schedule.apply(["x"], runner=FakePowerShell(("", None, 5)))


def test_apply_without_powershell_is_a_clear_error():
    def missing(command, **kwargs):
        raise FileNotFoundError("powershell")

    with pytest.raises(RuntimeError, match="No se pudo abrir PowerShell"):
        schedule.apply(["x"], runner=missing)


def test_apply_with_no_stdout_gives_an_empty_string():
    assert schedule.apply(["x"], runner=FakePowerShell((None, "", 0))) == ""


def test_apply_without_a_runner_uses_subprocess_run_at_call_time(powershell):
    fake = powershell(("listo", "", 0))
    assert schedule.apply(["x"]) == "listo" and len(fake.calls) == 1


# ── describe ─────────────────────────────────────────────────────────────────

def info(**over) -> dict:
    return {"at": "09:00", "last": datetime(2026, 11, 3, 9, 0, 12), "result": 0, "next": datetime(2026, 11, 4, 9, 0), **over}


@pytest.mark.parametrize("data, expected", [
    (None, "No programada"),
    (info(), "Todos los días a las 09:00 · última: 03/11 09:00, terminó bien · próxima: 04/11 09:00"),
    (info(result=1), "Todos los días a las 09:00 · última: 03/11 09:00, frenó (código 1) · próxima: 04/11 09:00"),
    (info(result=2147942402), "Todos los días a las 09:00 · última: 03/11 09:00, frenó (código 2147942402) · próxima: 04/11 09:00"),
    (info(result=schedule.NEVER_RAN, last=None), "Todos los días a las 09:00 · última: todavía no corrió · próxima: 04/11 09:00"),
    (info(result=schedule.RUNNING), "Todos los días a las 09:00 · última: 03/11 09:00, corriendo ahora · próxima: 04/11 09:00"),
    (info(next=None, at=None), "Todos los días a las — · última: 03/11 09:00, terminó bien · próxima: —"),
])
def test_describe(data, expected):
    assert schedule.describe(data) == expected


# ── set_daily, remove y current ──────────────────────────────────────────────

def test_set_daily_registers_the_task_and_says_so():
    fake = FakePowerShell()
    message = schedule.set_daily(PYTHONW, PROJECT, "9:05", runner=fake)
    assert message == "Corrida diaria programada: todos los días a las 09:05, en modo seco (no postula)."
    assert fake.calls[0][0] == schedule.register_command(PYTHONW, PROJECT, "09:05") and len(fake.calls) == 1


def test_set_daily_with_a_bad_time_does_not_call_powershell():
    fake = FakePowerShell()
    with pytest.raises(RuntimeError, match="HH:MM"):
        schedule.set_daily(PYTHONW, PROJECT, "25:00", runner=fake)
    assert fake.calls == []


def test_set_daily_error_comes_from_the_first_line_of_powershell():
    with pytest.raises(RuntimeError, match="Acceso denegado"):
        schedule.set_daily(PYTHONW, PROJECT, "09:00", runner=FakePowerShell(("", "Acceso denegado\notra línea", 1)))


@pytest.mark.parametrize("stdout", ["no-existia", "no-existia\r\n", "  no-existia \n"])
def test_remove_when_there_was_none_says_so_with_a_single_powershell_call(stdout):
    fake = FakePowerShell((stdout, "", 0))
    assert schedule.remove(runner=fake) is False
    assert [c[0] for c in fake.calls] == [schedule.unregister_command()]


@pytest.mark.parametrize("stdout", ["quitada", "quitada\r\n"])
def test_remove_when_there_was_one_says_so_with_a_single_powershell_call(stdout):
    fake = FakePowerShell((stdout, "", 0))
    assert schedule.remove(runner=fake) is True
    assert [c[0] for c in fake.calls] == [schedule.unregister_command()]


def test_remove_failure_is_an_error_with_only_the_first_line():
    with pytest.raises(RuntimeError) as err:
        schedule.remove(runner=FakePowerShell(("", "Unregister-ScheduledTask : Acceso denegado.\nmás detalle", 1)))
    assert str(err.value) == ("No se pudo usar el Programador de tareas de Windows: "
                              "Unregister-ScheduledTask : Acceso denegado.")


@pytest.mark.parametrize("stdout", ["", "\r\n", "ok", "quitada no-existia", "Quitada"])
def test_remove_with_an_output_it_does_not_understand_is_an_error(stdout):
    with pytest.raises(RuntimeError, match="No se pudo leer el resultado de quitar la corrida programada"):
        schedule.remove(runner=FakePowerShell((stdout, "", 0)))


def test_removal_message():
    assert schedule.removal_message(True) == "Corrida programada quitada."
    assert schedule.removal_message(False) == "No había ninguna corrida programada."


def test_current_returns_the_parsed_state():
    assert schedule.current(runner=FakePowerShell((FULL, "", 0)))["at"] == "09:00"
    assert schedule.current(runner=FakePowerShell(("", "", 0))) is None


# ── CLI: schedule set / off / show ───────────────────────────────────────────

@pytest.fixture
def pythonw_next_to_python(tmp_path, monkeypatch):
    """Un `pythonw.exe` falso junto al `python.exe` falso: la CLI lo busca ahí."""
    (tmp_path / "python.exe").write_bytes(b"")
    (tmp_path / "pythonw.exe").write_bytes(b"")
    monkeypatch.setattr(sys, "executable", str(tmp_path / "python.exe"))
    return tmp_path / "pythonw.exe"


def test_cli_schedule_set_registers_a_dry_run_of_run_in_the_project_folder(powershell, pythonw_next_to_python, capsys):
    fake = powershell()
    assert cli.main(["schedule", "set", "--at", "9:05"]) == 0
    captured = capsys.readouterr()
    assert captured.out == "Corrida diaria programada: todos los días a las 09:05, en modo seco (no postula).\n"
    assert captured.err == "" and len(fake.calls) == 1
    script = fake.scripts[0]
    assert f"-Execute '{pythonw_next_to_python}' -Argument '-m compuapply run' -WorkingDirectory '{cli.PROJECT}'" in script
    assert "-Daily -At '09:05'" in script and "--live" not in script
    assert (cli.PROJECT / "compuapply" / "cli.py").is_file()  # la carpeta del proyecto es la que tiene el paquete


def test_cli_schedule_set_bad_time_exits_1_and_does_not_call_powershell(powershell, pythonw_next_to_python, capsys):
    fake = powershell()
    assert cli.main(["schedule", "set", "--at", "24:00"]) == 1
    captured = capsys.readouterr()
    assert "no es válida" in captured.err and "HH:MM" in captured.err and captured.out == "" and fake.calls == []


def test_cli_schedule_set_requires_the_time(capsys):
    with pytest.raises(SystemExit) as stop:
        cli.main(["schedule", "set"])
    assert stop.value.code == 2 and "--at" in capsys.readouterr().err


def test_cli_schedule_set_failure_exits_1_with_the_first_line_only(powershell, pythonw_next_to_python, capsys):
    powershell(("", "Register-ScheduledTask : Acceso denegado.\nEn línea: 1", 1))
    assert cli.main(["schedule", "set", "--at", "09:00"]) == 1
    captured = capsys.readouterr()
    assert captured.err == ("Error: No se pudo usar el Programador de tareas de Windows: "
                            "Register-ScheduledTask : Acceso denegado.\n") and captured.out == ""


def test_cli_schedule_set_without_pythonw_exits_1_with_a_clear_error(powershell, tmp_path, monkeypatch, capsys):
    (tmp_path / "python.exe").write_bytes(b"")
    monkeypatch.setattr(sys, "executable", str(tmp_path / "python.exe"))
    fake = powershell()
    assert cli.main(["schedule", "set", "--at", "09:00"]) == 1
    assert "pythonw.exe" in capsys.readouterr().err and fake.calls == []


def test_cli_schedule_show_without_a_task(powershell, capsys):
    fake = powershell(("", "", 0))
    assert cli.main(["schedule", "show"]) == 0
    assert capsys.readouterr().out == "No programada\n" and fake.calls[0][0] == schedule.query_command()


def test_cli_schedule_show_with_a_task(powershell, capsys):
    powershell((FULL, "", 0))
    assert cli.main(["schedule", "show"]) == 0
    assert capsys.readouterr().out == "Todos los días a las 09:00 · última: 03/11 09:00, terminó bien · próxima: 04/11 09:00\n"


def test_cli_schedule_show_a_run_that_stopped_and_one_that_never_ran(powershell, capsys):
    powershell(('{"at":"2026-11-02T09:00:00","last":"2026-11-03T09:00:12","result":1,"next":"2026-11-04T09:00:00"}', "", 0),
               ('{"at":"2026-11-02T09:00:00","last":null,"result":267011,"next":"2026-11-03T09:00:00"}', "", 0))
    assert cli.main(["schedule", "show"]) == 0
    assert "frenó (código 1)" in capsys.readouterr().out
    assert cli.main(["schedule", "show"]) == 0
    assert "última: todavía no corrió" in capsys.readouterr().out


def test_cli_schedule_show_garbage_exits_1(powershell, capsys):
    powershell(("¿qué es esto?", "", 0))
    assert cli.main(["schedule", "show"]) == 1
    assert "No se pudo leer el estado" in capsys.readouterr().err


def test_cli_schedule_show_error_exits_1_with_the_first_line_only(powershell, capsys):
    powershell(("", "\nGet-ScheduledTaskInfo : Acceso denegado.\nEn línea: 1", 1))
    assert cli.main(["schedule", "show"]) == 1
    captured = capsys.readouterr()
    assert captured.err == "Error: No se pudo usar el Programador de tareas de Windows: Get-ScheduledTaskInfo : Acceso denegado.\n"
    assert captured.out == ""


def test_cli_schedule_off_when_nothing_is_scheduled_says_so_and_exits_0(powershell, capsys):
    fake = powershell(("no-existia\r\n", "", 0))
    assert cli.main(["schedule", "off"]) == 0
    captured = capsys.readouterr()
    assert captured.out == "No había ninguna corrida programada.\n" and captured.err == ""
    assert [c[0] for c in fake.calls] == [schedule.unregister_command()]


def test_cli_schedule_off_removes_the_task(powershell, capsys):
    fake = powershell(("quitada\r\n", "", 0))
    assert cli.main(["schedule", "off"]) == 0
    captured = capsys.readouterr()
    assert captured.out == "Corrida programada quitada.\n" and captured.err == ""
    assert [c[0] for c in fake.calls] == [schedule.unregister_command()]


def test_cli_schedule_off_real_failure_exits_1_with_the_first_line_only(powershell, capsys):
    powershell(("", "Unregister-ScheduledTask : Acceso denegado.\nEn línea: 1", 1))
    assert cli.main(["schedule", "off"]) == 1
    captured = capsys.readouterr()
    assert captured.err == ("Error: No se pudo usar el Programador de tareas de Windows: "
                            "Unregister-ScheduledTask : Acceso denegado.\n") and captured.out == ""


def test_cli_schedule_off_with_an_output_it_does_not_understand_exits_1(powershell, capsys):
    powershell(("", "", 0))
    assert cli.main(["schedule", "off"]) == 1
    captured = capsys.readouterr()
    assert "No se pudo leer el resultado de quitar la corrida programada" in captured.err and captured.out == ""


@pytest.mark.parametrize("args, code", [(["schedule"], 2), (["schedule", "set", "--help"], 0), (["schedule", "off", "--help"], 0),
                                        (["schedule", "show", "--help"], 0)])
def test_cli_schedule_help_and_required_subcommand(args, code, capsys):
    with pytest.raises(SystemExit) as stop:
        cli.main(args)
    assert stop.value.code == code
    capsys.readouterr()


def test_nothing_in_the_cli_ever_sends_live_to_the_scheduler(powershell, pythonw_next_to_python):
    fake = powershell(("", "", 0), ("quitada", "", 0), (FULL, "", 0))
    for args in (["schedule", "set", "--at", "10:00"], ["schedule", "off"], ["schedule", "show"]):
        assert cli.main(args) == 0
    assert len(fake.calls) == 3 and all("--live" not in " ".join(command) for command, _ in fake.calls)
