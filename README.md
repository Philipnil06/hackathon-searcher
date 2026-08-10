# Hackathon Searcher

An autonomous agent that continuously discovers high-quality hackathons, determines whether travel support is available, ranks events, and automatically submits complete applications.

Built for hackers who want to spend time building, not filling out forms.

## What It Does

Every 24 hours, the agent:

1. Scrapes [Hackathon Hub](https://hackathonhub.eu/) for new and updated hackathons
2. Visits each event's external website to research travel reimbursement, sponsors, and logistics
3. Scores every event from 0–100 across six dimensions
4. Generates personalized, truthful application answers from your verified profile
5. Automatically fills and submits applications via Playwright
6. Produces a daily report and maintains a searchable dashboard

## Priority System

The agent optimizes for *quality over quantity*:

1. **Flight credits / travel reimbursement** — the strongest signal. Events offering travel support skip the normal score threshold.
2. **Exceptional hackathons** — strong sponsors (AI companies, VCs, top dev-tool companies), respected judges, valuable prizes
3. **Technical alignment** — AI, robotics, hardware, autonomy, drones, fintech, devtools, startups
4. **High-value opportunities** — even without explicit travel support if the event is strong enough

## Quick Start

```bash
# Clone
git clone https://github.com/YOUR_USER/hackathon-searcher.git
cd hackathon-searcher

# Install dependencies
pip install -r requirements.txt

# Install browser (for form automation)
python -m playwright install chromium

# Create your profile
cp profile.example.json profile.json
# Edit profile.json with your details

# Create your answer library
cp answer_library.example.json answer_library.json
# Customize answers to match your experience

# Initialize database
python -m hackathon_searcher.cli init

# View dashboard
python -m hackathon_searcher.cli dashboard

# Run in dry-run mode (fills forms but doesn't submit)
python -m hackathon_searcher.cli run
```

## Configuration

Copy `.env.example` to `.env` and customize:

| Setting | Default | Description |
|---------|---------|-------------|
| `DRY_RUN` | `true` | Set to `false` to enable real submissions |
| `AUTO_APPLY` | `true` | Automatically submit applications |
| `MIN_APPLICATION_SCORE` | `55` | Minimum score to apply (0-100) |
| `AUTO_APPLY_FLIGHT_SUPPORT` | `true` | Always apply if flights are reimbursed |
| `HOME_CITY` | `Stockholm` | Your home city |
| `HOME_COUNTRY` | `Sweden` | Your home country |
| `HEADLESS` | `true` | Run browser in headless mode |

## CLI Commands

```bash
python -m hackathon_searcher.cli run         # Full daily pipeline
python -m hackathon_searcher.cli dashboard   # Overview dashboard
python -m hackathon_searcher.cli events      # List all events
python -m hackathon_searcher.cli show ID     # Event details + application
python -m hackathon_searcher.cli profile     # View current profile
python -m hackathon_searcher.cli settings    # View current settings
python -m hackathon_searcher.cli report      # Last daily report
```

## Architecture

```
hackathon_searcher/
├── scraper.py           # Hackathon Hub discovery (JSON → HTML fallback)
├── event_research.py    # External website research (travel, sponsors)
├── scoring.py           # 0–100 ranking engine with overrides
├── forms.py             # Form field extraction & answer generation
├── browser.py           # Playwright automation for form filling
├── agent.py             # Orchestrator: discover → research → score → apply
├── database.py          # SQLite layer, fingerprinting, audit log
├── models.py            # Pydantic schemas for structured data
├── profile.py           # Profile loader with semantic question matching
├── dashboard.py         # Terminal dashboard & reports
├── settings.py          # Centralized configuration
└── cli.py               # Command-line interface
```

## How It Works

### Discovery
The scraper first tries to extract embedded JSON data (Next.js `__NEXT_DATA__`, `__INITIAL_STATE__`, JSON-LD). Falls back to HTML parsing if no structured data is found.

### Duplicate Prevention
Events are fingerprinted using `SHA-256(normalize(name) + start_date + organizer + city)`. The agent checks the fingerprint before processing any event and never submits twice.

### Scoring
Every event is scored on six axes:

| Component | Weight | What It Measures |
|-----------|--------|-----------------|
| Travel support | 0–40 | Flight credits, reimbursement, stipends, accommodation |
| Sponsor quality | 0–20 | Reputation of sponsoring companies |
| Technical relevance | 0–15 | Alignment with AI, robotics, hardware, fintech, etc. |
| Event quality | 0–10 | Organizer reputation, judges, venue, community |
| Prizes | 0–10 | Cash prizes, hardware, credits, opportunities |
| Logistics | 0–5 | Distance from home, physical vs. online, duration |

### Answer Generation
Form questions are matched against an answer library using string similarity. Answers are tailored to the specific event (AI hackathon → emphasizes AI projects, hardware hackathon → emphasizes robotics background). The agent **never fabricates** facts — if a question can't be answered from the verified profile, it's marked as `UNKNOWN_REQUIRED_FIELD` and the application is blocked rather than submitted with made-up data.

### Safety
- **Dry-run mode** by default — fills forms but never clicks submit
- **CAPTCHA detection** — blocks submission if a CAPTCHA is present
- **Sensitive question detection** — never guesses answers to citizenship, visa, criminal history, medical, or legal questions
- **Per-event isolation** — failure on one event never kills the run
- **Idempotent** — running twice produces no duplicates

## Adding Multiple People

The profile system supports multiple applicants. Create separate profiles:

```bash
cp profile.example.json profile_philip.json
cp profile.example.json profile_michelle.json
# Edit each with their details
```

Then set `PROFILE_PATH=profile_philip.json` in `.env` or extend the agent to iterate over profiles.

## Scheduling

Run daily via cron:

```bash
# Every day at 8 AM
0 8 * * * cd /path/to/hackathon-searcher && python -m hackathon_searcher.cli run >> logs/daily.log 2>&1
```

Or via GitHub Actions (see `.github/workflows/daily.yml` for an example).

## Requirements

- Python 3.10+
- Playwright (for browser automation)
- SQLite (bundled with Python)

## Philosophy

This is not a mass form spammer. It's a personal autonomous hackathon agent.

Its behavior should approximate: "If you saw every European hackathon as soon as it appeared, researched whether attending was worthwhile, checked whether you could get there cheaply or through flight credits, wrote a strong truthful application tailored to the event, and submitted it immediately."

Optimize for that outcome.

## License

MIT
