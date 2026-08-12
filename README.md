# Hackathon Searcher

**Find better hackathons. Apply faster.**

Hackathon Searcher is an AI agent that discovers hackathons, researches travel support, scores opportunities against you or your team, prepares grounded application answers, and automates the repetitive parts of applying.

```text
Discover -> Research -> Score -> Decide -> Apply -> Track
```

Try it safely in about five minutes: the setup wizard creates local profiles and LLM settings, then `preflight` and `daily --dry-run` validate the workflow without submitting anything. It supports solo applicants and teams of 1-4; profiles, API keys, application history, browser data, and generated answers stay on your computer.

## What it does

- Discovers hackathons and researches deadlines, eligibility, event details, and travel support.
- Scores opportunities per applicant and makes a team-first decision from the average score.
- Requires every team member to pass hard eligibility and safety gates before an application is prepared.
- Generates answers only from the profile facts you provide, then checks factual and cross-profile consistency.
- Tracks duplicate protection, reports, confirmations, and submission snapshots in a local SQLite database.
- Uses autonomous provider flows only where technically supported. Luma is human-assisted: the extension fills the form; you review and make the final irreversible submission.

## Quick Start

Prerequisites: Python 3.10+, Git, and an internet connection. Google Chrome is optional until you use Luma autofill.

Clone and create a virtual environment:

```bash
git clone <repo>
cd hackathon-searcher
python -m venv .venv
```

Activate it on Windows:

```powershell
.\.venv\Scripts\Activate.ps1
```

Or on macOS/Linux:

```bash
source .venv/bin/activate
```

Install dependencies and take the safe first run:

```bash
python -m pip install -r requirements.txt
python -m playwright install chromium

python -m hackathon_searcher.cli setup
python -m hackathon_searcher.cli preflight
python -m hackathon_searcher.cli daily --dry-run
```

`setup` takes roughly 3-5 minutes. A normal user does not need to edit JSON or environment variables: the wizard asks whether you are solo or a team, creates 1-4 local profiles and a team config, collects travel preferences, and configures the LLM provider, model, and API key. Keys are saved only in the ignored local `.env` file.

`preflight` validates profiles, the team, database, LLM configuration, key paths, and optional browser/scheduler status. It does not contact an LLM and never submits. `daily --dry-run` discovers and prepares opportunities while forcing submission off.

## How it works

1. **Discover and research** official information, deadlines, rules, forms, and travel support.
2. **Score** every event for every configured applicant.
3. **Decide together:** the team score is the average member apply score. It must meet the configured threshold.
4. **Gate safely:** every member must independently pass eligibility, schedule, form, factual, cross-profile, consent, and duplicate checks.
5. **Prepare applications:** answers are tailored using verified local profile facts.
6. **Apply or assist:** supported providers use guarded automation. For Luma, the user completes the final action in Chrome.
7. **Track locally:** application state, confirmations, reports, and snapshots are retained locally.

A high score never overrides a hard blocker, including ineligibility, schedule conflicts, closed forms, unknown required fields, CAPTCHA, required unsupported login, factual conflicts, cross-profile conflicts, or an existing duplicate.

## Solo and teams

Teams can have 1-4 people. For a team opportunity, the average apply score must meet the team threshold and every member must pass their own hard gates. The system does not prepare just one person from a team opportunity because their individual score is higher.

```bash
python -m hackathon_searcher.cli profile show
python -m hackathon_searcher.cli profile validate <applicant_id>
python -m hackathon_searcher.cli profile improve <applicant_id>
python -m hackathon_searcher.cli team show
python -m hackathon_searcher.cli team add <applicant_id>
python -m hackathon_searcher.cli team remove <applicant_id>
```

Use `profile improve` later to add projects, work and startup experience, hackathon experience, awards, verified metrics, skills, communities, facts allowed in applications, and facts that must not be referenced.

## LLM providers

Setup supports OpenAI, Anthropic, Google Gemini, Mistral, and OpenAI-compatible APIs. It asks for provider, model, and API key; it asks for a base URL only for OpenAI-compatible services.

```bash
python -m hackathon_searcher.cli llm verify
```

This validates the configuration without sending a paid API request or exposing the key.

## Browser and Luma setup

Luma may use browser verification or CAPTCHA. Hackathon Searcher does not bypass either.

The optional local Chrome extension opens the non-final Luma form and fills prepared fields in a dedicated Chrome profile. The profile is isolated from normal Chrome cookies and accounts, helping prevent identity mixing between team members.

```bash
python -m hackathon_searcher.cli browser setup
python -m hackathon_searcher.cli browser verify
python -m hackathon_searcher.cli human-assist
python -m hackathon_searcher.cli human-assist open <event_id>
```

`browser setup` opens Chrome's extensions page and the extension folder. Enable Developer mode, choose **Load unpacked**, and select `chrome_extension`. This is a one-time manual Chrome step. `human-assist open` opens separately bound tabs per team member; the extension exposes only the prepared application for that tab. Review unmatched fields and click Luma's final submit button yourself. If `LUMA_SESSION_PRESENT` appears, log out in the dedicated profile before continuing.

After confirming manual submissions, record them explicitly:

```bash
python -m hackathon_searcher.cli human-assist mark-submitted <event_id>
```

See [CHROME_EXTENSION_SETUP.md](CHROME_EXTENSION_SETUP.md) for details.

## Daily scheduling (Windows)

Interactive setup needs a terminal. Scheduled runs do not: the Windows task uses this project's virtual-environment background interpreter, so no PowerShell window needs to stay open.

```powershell
python -m hackathon_searcher.cli schedule setup
python -m hackathon_searcher.cli schedule status
python -m hackathon_searcher.cli schedule remove --yes
```

`schedule setup` creates or updates one task named **Hackathon Searcher Daily**. It runs daily at 09:00 local time, starts after a missed run when Windows becomes available, and ignores a new instance while an earlier one is active. It respects your local `DRY_RUN` setting. Scheduling is currently implemented for Windows only.

## Safety and dry-run mode

Keep `DRY_RUN=true` while learning the system and reviewing prepared applications. Force safe mode for one run with:

```bash
python -m hackathon_searcher.cli daily --dry-run
```

Preflight and dry-run paths do not click final submission actions. The system also uses a run lock and database status to prevent overlap.

## Privacy

These items stay local and are ignored by Git:

- Applicant profiles, answer libraries, team configuration, and `.env` API keys.
- Databases, reports, logs, screenshots, snapshots, generated human-assist packages, and browser state.
- The dedicated Chrome profile and its extension state.

The public repository contains source code, fictional examples, templates, tests, and documentation. Add only profile facts you are comfortable using as application source material. Review `git status --ignored` before publishing a fork.

## Configuration

`setup` is the recommended route. Advanced users can inspect `.env.example`, `team.example.json`, and the fictional examples in `profiles/` and `answer_library/`, but manual JSON editing is not needed for normal onboarding.

New configurations default to `DRY_RUN=true`, `AUTO_APPLY=false`, and `LIVE_TEST_MODE=false`.

## CLI reference

```text
setup                         Interactive local onboarding
preflight                     Local readiness and safe application preflight
daily --dry-run               Discovery/research with submission forced off
profile show|validate|improve  Manage applicant profiles
team show|add|remove           Manage a 1-4 person team
llm verify                    Check provider settings without an API request
browser setup|verify|open      Manage the isolated Chrome profile
schedule setup|status|remove   Manage the Windows background task
human-assist                  List/open/confirm prepared Luma applications
dashboard | events | report   Inspect local results
```

Run `python -m hackathon_searcher.cli --help` for the built-in overview.

## Troubleshooting

- **Python is not recognized:** install Python 3.10+ and reopen the terminal after adding it to PATH.
- **A dependency is missing:** activate `.venv`, then run `python -m pip install -r requirements.txt`.
- **LLM key or model missing:** run `setup`, then `llm verify`. Never place keys in source files.
- **A profile is invalid:** run `profile show`, then `profile improve <applicant_id>` or rerun setup.
- **No applications in preflight:** a new database has none yet. Run `daily --dry-run` to research safely.
- **Chrome or extension missing:** run `browser setup`, load the unpacked extension, then run `browser verify`.
- **Luma is logged in:** log out in the dedicated profile; do not use normal Chrome for the autofill flow.
- **No scheduler task:** run `schedule setup` on Windows.

## Architecture

```text
Discovery/research -> scoring -> team and eligibility gates -> form preparation
                                                     |-> supported autonomous provider path
                                                     `-> Luma human-assisted package + local autofill
```

## Contributing

Keep fixtures fictional, preserve safety gates, add regression tests for behavior changes, and never commit credentials, real profiles, browser state, or real application material.

## License

Released under the [MIT License](LICENSE).
