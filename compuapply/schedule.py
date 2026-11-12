"""Corrida diaria programada: una tarea de Windows que corre `compuapply run` (siempre en modo seco, nunca `--live`)."""
import json
import re
import subprocess
from datetime import datetime
from pathlib import Path

TASK_NAME = "compuapply-run"
DESCRIPTION = "compuapply: corrida diaria en modo seco"
NEVER_RAN = 267011  # LastTaskResult de una tarea que todavía no corrió (SCHED_S_TASK_HAS_NOT_RUN)
RUNNING = 267009  # LastTaskResult de una tarea que está corriendo (SCHED_S_TASK_RUNNING)
CREATE_NO_WINDOW = 0x08000000
POWERSHELL = ["powershell", "-NoProfile", "-NonInteractive", "-Command"]
# Sin esto, un error de un cmdlet no cambia el código de salida de PowerShell y `apply` no se enteraría.
STOP_ON_ERROR = "$ErrorActionPreference = 'Stop'; "
# `powershell -Command` sale con 1 si lo último que corrió falló en silencio (`Get-ScheduledTask -ErrorAction
# SilentlyContinue` sin resultado): con `exit 0` explícito, «la tarea no existe» no se toma por un error.
EXIT_OK = "; exit 0"
REMOVED, NOT_THERE = "quitada", "no-existia"  # lo que imprime `unregister_command`


def _quoted(value) -> str:
    """Texto como literal de PowerShell entre comillas simples (la comilla simple se duplica)."""
    return "'" + str(value).replace("'", "''") + "'"


def valid_time(text: str) -> str:
    """`HH:MM` (24 horas) a partir de lo que escribió el usuario (`9:05` da `09:05`); RuntimeError si no es una hora."""
    found = re.fullmatch(r"([0-9]{1,2}):([0-9]{2})", text.strip())
    if not found or int(found[1]) > 23 or int(found[2]) > 59:
        raise RuntimeError(
            f"La hora «{text.strip()}» no es válida: escribila como HH:MM, de 00:00 a 23:59 (por ejemplo 09:00).")
    return f"{int(found[1]):02d}:{found[2]}"


def register_command(pythonw: Path, project: Path, at: str) -> list[str]:
    """Comando de PowerShell que crea (o reemplaza) la tarea diaria: `pythonw -m compuapply run` en `project`, a las `at`.

    La acción es siempre `run` sin `--live`. Corre con el usuario actual, sin permisos de administrador y también
    a batería (la PC del usuario es una notebook).
    """
    script = (
        f"{STOP_ON_ERROR}"
        f"$action = New-ScheduledTaskAction -Execute {_quoted(pythonw)} -Argument '-m compuapply run' "
        f"-WorkingDirectory {_quoted(project)}; "
        f"$trigger = New-ScheduledTaskTrigger -Daily -At {_quoted(valid_time(at))}; "
        "$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries "
        "-DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 2); "
        f"Register-ScheduledTask -TaskName {_quoted(TASK_NAME)} -Action $action -Trigger $trigger -Settings $settings "
        f"-Description {_quoted(DESCRIPTION)} -Force | Out-Null"
    )
    return [*POWERSHELL, script]


def unregister_command() -> list[str]:
    """Comando de PowerShell que quita la tarea si existe e imprime `quitada`, o `no-existia` si no estaba (sale con 0 en los dos)."""
    name = _quoted(TASK_NAME)
    script = (
        f"{STOP_ON_ERROR}if (Get-ScheduledTask -TaskName {name} -ErrorAction SilentlyContinue) "
        f"{{ Unregister-ScheduledTask -TaskName {name} -Confirm:$false; '{REMOVED}' }} else {{ '{NOT_THERE}' }}{EXIT_OK}"
    )
    return [*POWERSHELL, script]


def query_command() -> list[str]:
    """Comando de PowerShell que imprime un JSON con la hora, la última ejecución, su resultado y la próxima (nada si no existe)."""
    name = _quoted(TASK_NAME)
    # Una tarea inexistente es la categoría ObjectNotFound («No programada»); cualquier otro error corta con exit 1.
    script = (
        f"{STOP_ON_ERROR}try {{ $task = Get-ScheduledTask -TaskName {name} }} "
        "catch { if ($_.CategoryInfo.Category -eq 'ObjectNotFound') { exit 0 } else { throw } }; "
        "if ($task) { "
        f"$info = Get-ScheduledTaskInfo -TaskName {name}; "
        "$last = if ($info.LastRunTime.Year -gt 2000) { $info.LastRunTime.ToString('s') } else { $null }; "
        "$next = if ($info.NextRunTime) { $info.NextRunTime.ToString('s') } else { $null }; "
        "$at = if ($task.Triggers.Count) { [string]$task.Triggers[0].StartBoundary } else { $null }; "
        f"ConvertTo-Json -Compress @{{ at = $at; last = $last; result = [int64]$info.LastTaskResult; next = $next }} }}{EXIT_OK}"
    )
    return [*POWERSHELL, script]


def _moment(text) -> datetime | None:
    try:
        return datetime.fromisoformat(text) if text else None
    except (TypeError, ValueError):
        return None


def parse_query(stdout: str) -> dict | None:
    """El estado de la tarea, o None si no existe (la salida de `query_command` viene vacía).

    Claves: `at` (`HH:MM` o None), `last` y `next` (`datetime` locales o None), `result` (el código de la última
    ejecución: 0 es que terminó bien). Si todavía no corrió (267011), `last` es None.
    """
    text = stdout.strip()
    if not text:
        return None
    try:
        data = json.loads(text)
        result = int(data["result"])
    except (ValueError, KeyError, TypeError):
        raise RuntimeError("No se pudo leer el estado de la tarea programada.") from None
    start = _moment(data.get("at"))
    return {"at": f"{start:%H:%M}" if start else None, "result": result, "next": _moment(data.get("next")),
            "last": None if result == NEVER_RAN else _moment(data.get("last"))}


def apply(command: list[str], runner=None) -> str:
    """Corre un comando de PowerShell sin consola y devuelve su stdout; si falla, RuntimeError con la primera línea del error.

    `runner` (por defecto `subprocess.run`) existe para los tests: ninguno toca la tarea de verdad.
    """
    try:
        done = (runner or subprocess.run)(command, capture_output=True, text=True, errors="replace",
                                          creationflags=CREATE_NO_WINDOW)
    except OSError:
        raise RuntimeError("No se pudo abrir PowerShell: sin él no se puede usar el Programador de tareas.") from None
    if done.returncode != 0:
        first = next((line.strip() for line in (done.stderr or "").splitlines() if line.strip()), "sin detalle")
        raise RuntimeError(f"No se pudo usar el Programador de tareas de Windows: {first}")
    return done.stdout or ""


def describe(info: dict | None) -> str:
    """El estado en una línea: «No programada» o «Todos los días a las HH:MM · última: ... · próxima: ...»."""
    if info is None:
        return "No programada"
    when = f"{info['last']:%d/%m %H:%M}" if info["last"] else "—"
    if info["result"] == NEVER_RAN:
        last = "todavía no corrió"
    elif info["result"] == RUNNING:
        last = f"{when}, corriendo ahora"
    elif info["result"] == 0:
        last = f"{when}, terminó bien"
    else:
        last = f"{when}, frenó (código {info['result']})"
    following = f"{info['next']:%d/%m %H:%M}" if info["next"] else "—"
    return f"Todos los días a las {info['at'] or '—'} · última: {last} · próxima: {following}"


def current(runner=None) -> dict | None:
    """Consulta la tarea y devuelve su estado (`parse_query`)."""
    return parse_query(apply(query_command(), runner))


def set_daily(pythonw: Path, project: Path, at: str, runner=None) -> str:
    """Programa (o reprograma) la corrida diaria y devuelve el mensaje para el usuario."""
    at = valid_time(at)
    apply(register_command(pythonw, project, at), runner)
    return f"Corrida diaria programada: todos los días a las {at}, en modo seco (no postula)."


def remove(runner=None) -> bool:
    """Quita la tarea si existe y devuelve si había una (con un solo llamado a PowerShell, que no falla si no está)."""
    answer = apply(unregister_command(), runner).strip()
    if answer not in (REMOVED, NOT_THERE):
        raise RuntimeError("No se pudo leer el resultado de quitar la corrida programada.")
    return answer == REMOVED


def removal_message(existed: bool) -> str:
    """El mensaje para el usuario según lo que devolvió `remove`."""
    return "Corrida programada quitada." if existed else "No había ninguna corrida programada."
