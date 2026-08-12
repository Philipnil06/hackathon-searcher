# Security and privacy

## Local data boundary

Applicant profiles, answer libraries, `.env`, SQLite data, reports, snapshots, browser profiles, and captured images are local runtime data. They are excluded from version control by `.gitignore` and must never be added with `git add -f`.

## Secrets

Set API keys only in a local `.env` file or through the process environment. Use empty values in examples and immediately rotate any credential that may have appeared in a terminal, screenshot, commit, or shared document.

## Human-assisted Luma flow

The extension talks only to `127.0.0.1`. It fills selected prepared fields and does not perform the final irreversible Luma submission. It does not bypass browser verification, CAPTCHA, or provider authentication controls.

## Responsible automation

Keep dry-run enabled until you have reviewed the prepared applications. Respect event terms, eligibility requirements, rate limits, consent choices, and duplicate-protection signals. Do not retry uncertain submissions automatically.

## Reporting a concern

Do not open a public issue containing personal data or credentials. Contact the repository maintainer privately and include only the minimum evidence needed to reproduce the concern.
