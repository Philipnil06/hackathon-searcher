# Chrome autofill extension

The bundled Manifest V3 extension fills prepared Luma application fields from a loopback-only local bridge. It does not bypass Luma verification or CAPTCHA. In fill-only mode it never clicks the final submission button; the optional auto-submit mode does so only when explicitly enabled in live mode.

## One-time installation

Run this first:

```powershell
python -m hackathon_searcher.cli browser setup
```

It detects Google Chrome, creates a separate persistent Hackathon Searcher profile outside the repository, opens `chrome://extensions`, and opens this extension directory in your file explorer. It never uses your normal Chrome profile.

1. Open `chrome://extensions` in Chrome.
2. Enable **Developer mode**.
3. Select **Load unpacked**.
4. Select this repository's `chrome_extension` directory.
5. Keep **Hackathon Searcher Autofill** enabled.

> **After editing any file in `chrome_extension/`**, reload the extension in
> `chrome://extensions` (the circular reload button) or restart Chrome. Chrome
> caches the extension's service worker; a stale worker silently drops the
> submission result and lets the system re-apply to an event it already
> submitted to.

Then confirm the connection:

```powershell
python -m hackathon_searcher.cli browser verify
```

## Use

```powershell
python -m hackathon_searcher.cli human-assist open <event_id>
```

The command starts a bridge on `127.0.0.1:8765` and opens one tab per prepared team member in the dedicated local Hackathon Searcher Chrome profile. Each URL contains an application binding; the bridge returns only the explicitly selected event/application payload for that tab. The dedicated profile is persistent so the manually loaded extension remains available, but it is separate from normal Chrome cookies and accounts.

The extension opens the initial, non-irreversible registration dialog and fills the known fields. If any field is unmatched, it reports "Needs attention" and stops; it never submits when anything needs attention. If `LUMA_SESSION_PRESENT` is shown, log out of Luma in the dedicated profile before continuing.

### Automatic submission (optional)

When `DRY_RUN=false` **and** `LUMA_AUTO_SUBMIT=true` in `.env`, the extension also:

1. clicks Luma's final submit button once every field is filled,
2. watches for the confirmation screen (confirmation text, a success/thank URL, or the dialog closing),
3. reports the result back to the bridge.

The bridge records the application as `APPLIED` (or `SUBMISSION_STATUS_UNKNOWN` if the click happened but no confirmation was detected) and writes a full snapshot under `submission_snapshots/`. An uncertain submit is never retried automatically.

```env
DRY_RUN=false
LUMA_AUTO_SUBMIT=true
```

Keep `LUMA_AUTO_SUBMIT=false` (the default) to stay in fill-only mode, where the extension prepares the form and you click the final submit yourself, then run:

```powershell
python -m hackathon_searcher.cli human-assist mark-submitted <event_id>
```

To reopen one member's prepared application:

```powershell
python -m hackathon_searcher.cli human-assist open <event_id> <applicant_id>
```
