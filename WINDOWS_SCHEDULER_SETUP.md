# Windows scheduling

Use the CLI instead of editing Task Scheduler manually:

```powershell
python -m hackathon_searcher.cli schedule setup
python -m hackathon_searcher.cli schedule status
```

It creates or updates exactly one task, **Hackathon Searcher Daily**, for the current Windows user. The task uses the project's `.venv\Scripts\pythonw.exe`, so daily runs happen in the background without an open PowerShell window. It runs at 09:00 local time, starts as soon as possible after a missed start, and ignores a new instance while an earlier run is active.

The task runs the normal `-m hackathon_searcher.cli daily` command and therefore respects the local `.env` value of `DRY_RUN`. Keep `DRY_RUN=true` until you deliberately review and enable a live workflow.

To remove only this task:

```powershell
python -m hackathon_searcher.cli schedule remove --yes
```

Scheduling is currently implemented for Windows only.
