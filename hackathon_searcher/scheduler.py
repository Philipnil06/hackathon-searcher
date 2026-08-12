"""Small Windows Task Scheduler integration for daily background runs.

The public CLI deliberately supports Windows only.  It uses the standard
``ScheduledTasks`` module, updates one well-known task instead of creating
duplicates, and never asks for a Windows password.
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
from pathlib import Path
from typing import Any

TASK_NAME = "Hackathon Searcher Daily"


def is_windows() -> bool:
    return platform.system().lower() == "windows"


def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def virtualenv_python() -> Path:
    """Return the project virtual-environment interpreter or raise clearly."""
    executable = "pythonw.exe" if is_windows() else "python"
    candidate = project_root() / ".venv" / "Scripts" / executable
    if candidate.is_file():
        return candidate
    fallback = project_root() / ".venv" / "Scripts" / "python.exe"
    if fallback.is_file():
        return fallback
    raise RuntimeError(
        "Project virtual environment not found. Run `python -m venv .venv` and "
        "install the requirements before setting up scheduling."
    )


def _powershell(script: str) -> str:
    if not is_windows():
        raise RuntimeError("Scheduling is currently supported on Windows only.")
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        message = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"Windows Task Scheduler could not complete the request: {message}")
    return result.stdout.strip()


def _quote(value: str) -> str:
    """PowerShell single-quoted string escaping."""
    return value.replace("'", "''")


def scheduler_status() -> dict[str, Any]:
    """Read the actual Task Scheduler state without changing it."""
    if not is_windows():
        return {"supported": False, "exists": False, "message": "Scheduling is currently supported on Windows only."}
    task = _quote(TASK_NAME)
    script = f"""
$task = Get-ScheduledTask -TaskName '{task}' -ErrorAction SilentlyContinue
if ($null -eq $task) {{ '{{"exists":false}}' }} else {{
  $info = Get-ScheduledTaskInfo -TaskName '{task}'
  $action = $task.Actions | Select-Object -First 1
  [pscustomobject]@{{
    exists=$true; state=[string]$task.State; task_name=$task.TaskName;
    next_run_time=[string]$info.NextRunTime; last_run_time=[string]$info.LastRunTime;
    last_run_result=('0x{{0:X8}}' -f [uint32]$info.LastTaskResult);
    executable=[string]$action.Execute; arguments=[string]$action.Arguments;
    working_directory=[string]$action.WorkingDirectory;
    start_when_available=[bool]$task.Settings.StartWhenAvailable;
    multiple_instances=[string]$task.Settings.MultipleInstances;
    run_as_user=[string]$task.Principal.UserId; logon_type=[string]$task.Principal.LogonType
  }} | ConvertTo-Json -Compress
}}
"""
    raw = _powershell(script)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Task Scheduler returned an unreadable status response.") from exc
    data["supported"] = True
    return data


def setup_scheduler() -> dict[str, Any]:
    """Create or update the one daily task in the current user's account."""
    interpreter = virtualenv_python()
    root = project_root()
    task = _quote(TASK_NAME)
    exe = _quote(str(interpreter))
    workdir = _quote(str(root))
    # InteractiveToken uses the current account without storing its password.
    # pythonw.exe prevents a distracting console window for scheduled runs.
    script = f"""
$action = New-ScheduledTaskAction -Execute '{exe}' -Argument '-m hackathon_searcher.cli daily' -WorkingDirectory '{workdir}'
$trigger = New-ScheduledTaskTrigger -Daily -At 9:00AM
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
$task = New-ScheduledTask -Action $action -Trigger $trigger -Settings $settings -Principal $principal
Register-ScheduledTask -TaskName '{task}' -InputObject $task -Force | Out-Null
"""
    _powershell(script)
    return scheduler_status()


def remove_scheduler(*, confirmed: bool = False) -> bool:
    """Remove only this project's task after explicit CLI confirmation."""
    if not confirmed:
        raise RuntimeError("Refusing to remove the scheduled task without `schedule remove --yes`.")
    status = scheduler_status()
    if not status.get("exists"):
        return False
    _powershell(f"Unregister-ScheduledTask -TaskName '{_quote(TASK_NAME)}' -Confirm:$false")
    return True
