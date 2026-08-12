# Chrome autofill extension

The bundled Manifest V3 extension fills prepared Luma application fields from a loopback-only local bridge. It does not bypass Luma verification or CAPTCHA and never clicks the final submission button.

## One-time installation

Run this first:

```powershell
python -m hackathon_searcher.cli browser setup
```

It detects Google Chrome, creates a separate persistent Hackathon Searcher profile outside the repository, opens `chrome://extensions`, and opens this extension directory in your file explorer. It never uses your normal Chrome profile.

1. Open `chrome://extensions` in Chrome.
2. Enable **Developer mode**.
3. Select **Load unpacked**.
4. Select this repository’s `chrome_extension` directory.
5. Keep **Hackathon Searcher Autofill** enabled.

Then confirm the connection:

```powershell
python -m hackathon_searcher.cli browser verify
```

## Use

```powershell
python -m hackathon_searcher.cli human-assist open <event_id>
```

The command starts a bridge on `127.0.0.1:8765` and opens one tab per prepared team member in the dedicated local Hackathon Searcher Chrome profile. Each URL contains an application binding; the bridge returns only the explicitly selected event/application payload for that tab. The dedicated profile is persistent so the manually loaded extension remains available, but it is separate from normal Chrome cookies and accounts.

The extension may open the initial, non-irreversible registration dialog and fills known fields. Review any unmatched fields and then click Luma’s final submission action yourself. If `LUMA_SESSION_PRESENT` is shown, log out of Luma in the dedicated profile before continuing.

To reopen one member’s prepared application:

```powershell
python -m hackathon_searcher.cli human-assist open <event_id> <applicant_id>
```
