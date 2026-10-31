# checkerBot

checkerBot is an advisory brand-governance application for marketing copy,
images, and text-based documents. It combines retrieved brand guidance and AI
findings with measurable visual checks and a persisted human review process.
AI findings are never a final approval.

## Features

- Individual creator, reviewer, and administrator accounts. Passwords are
  PBKDF2-HMAC-SHA256 hashes; opaque session tokens are stored as hashes and
  sent in HttpOnly cookies. Mutating requests use a CSRF token.
- Submission ownership, reviewer decisions, creator revisions/resubmissions,
  reviewer comments and timestamps, and a complete revision history.
- SQLite persistence with versioned schema migrations, persistent fixed-window
  rate limits, configurable review-content retention, and audit events protected
  by an application-level HMAC hash chain.
- Chunked knowledge retrieval with optional Gemini embeddings and an explicitly
  labelled lexical fallback.
- Separate model-assisted AI findings and local visual measurements:
  perceptual-hash logo screening, dominant-colour extraction with CIE76
  colour-distance comparisons, and WCAG contrast ratios for configured
  foreground/background pairs. Administrators can configure an expected logo
  placement zone and receive an objective zone-screening result when a logo
  match is detected.
- Admin tools for managing users, brand guidance, knowledge documents, visual
  rules and reference logos, audit verification, retention, and saved evaluation
  reports; reviewer and creator workspaces for their respective workflows.
- Discord image and text review routes use the same review engine. The bot
  receives no administrative or reviewer privileges.

Visual metrics are screening measurements, not certification. Logo matching is
a crop-grid perceptual-hash heuristic; dominant-colour checks cannot isolate
logos or text; contrast is calculated only for administrator-configured colour
pairs, not detected text pixels.

## Requirements and installation

- Python 3.10 or later.
- Gemini API key (`LLM_API_KEY`) for actual AI reviews and live Gemini
  evaluation. The default model is `gemini-3.8-flash`, called through the
  Interactions API with schema-constrained JSON output. The application does
  not fabricate a review if the service is unavailable. Gemini's response-read
  timeout defaults to 180 seconds (`GEMINI_READ_TIMEOUT_SECONDS`); connect,
  write, and pool timeouts remain 90 seconds.
- OCR.space API key (`OCR_API_KEY`) for OCR on images.
- Discord bot token only if the Discord integration is used.

From the project directory in PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Set credentials and configuration in `.env`. For a local account, set
`INITIAL_ADMIN_EMAIL` and `INITIAL_ADMIN_PASSWORD` (at least 12 characters);
the first application startup creates that administrator only if the database
has no users. Creators can register through the sign-in page when
`ALLOW_REGISTRATION=true`. Administrators can create reviewer and other
accounts. Shared admin/reviewer keys have been removed.

## Run locally

```powershell
python -m uvicorn backend.main:app --reload
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000). API documentation is at
[http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs), and `/health` reports
configured providers and applied migration versions.

The default SQLite file is `data/futurestack.sqlite3`. Set `FUTURESTACK_DATABASE`
to use another file. Set `FUTURESTACK_BRAND_RULES` to point at an alternate
brand-rules JSON file; the demo uses this setting to keep sample rules separate
from an organisation's rules.

Supported uploads are PNG, JPEG, GIF, WebP, PDF, TXT, MD, CSV, and JSON, up to
10 MB. Text is limited to 100 KB and PDFs to 100 pages. Scanned PDFs do not
receive OCR. Images and submitted text may be sent to configured external AI
and OCR providers; only use data approved for those services.

## Production configuration and limitations

Before deploying, use HTTPS and set `ENVIRONMENT=production`,
`ALLOW_REGISTRATION=false`, an explicit comma-separated `CORS_ALLOWED_ORIGINS`,
a unique `AUDIT_HMAC_KEY` of at least 32 characters, and administrator
bootstrap credentials for a new database. Production startup rejects a missing
audit key, wildcard/unset CORS origins, or public registration. Production
cookies are marked Secure; configure TLS at the application or trusted
reverse-proxy boundary. Set `DISCORD_API_KEY` only when the bot is enabled.

On Vercel, the default SQLite database is placed under `/tmp` because the
deployment directory is read-only. Vercel's temporary filesystem is not
persistent across deployments or function instances, so this setting only
allows the demo to start and does not provide durable storage. Use a persistent
database service for production data; setting `FUTURESTACK_DATABASE` to another
path does not make Vercel's filesystem persistent.

`MALWARE_SCANNER=disabled` is the default and means uploads are **not** malware
scanned. To enable the ClamAV adapter, install and maintain ClamAV, set
`MALWARE_SCANNER=clamav` and optionally `CLAMSCAN_PATH`, then verify the
executable and definitions are current. Uploads fail closed if the configured
scanner is missing or errors.

The audit chain detects modification while the database and signing key remain
separately controlled. It is not tamper-proof: a host administrator able to
change the database and `AUDIT_HMAC_KEY` can rewrite the chain. Retention and
deletion remove review records and request SQLite secure deletion, but cannot
guarantee erasure from SSD wear-levelling, external backups, or copied files.
Back up the database and audit key securely and together; protect the key
separately from database write access.

Persistent rate limits are stored in SQLite and shared by application workers
using that database. They are fixed-window limits and do not replace edge
rate-limiting, account lockout, monitoring, MFA, or deployment-level
availability controls. Behind a proxy, configure Uvicorn's trusted proxy
handling and rate limit at the trusted edge as well.

Database migrations are applied automatically at application startup. Back up
the database before an upgrade, then start the application and check
`/health` → `database_migrations`. To back up a running SQLite database safely
using Python's online backup API:

```powershell
python -c "import sqlite3; source=sqlite3.connect(r'data\futurestack.sqlite3'); target=sqlite3.connect(r'data\futurestack-backup.sqlite3'); source.backup(target); target.close(); source.close()"
```

For recovery, stop the application, preserve the current database for
investigation, restore the verified backup to the configured database path, and
restart. Also restore the matching audit HMAC key; changing or losing it makes
previous audit hashes unverifiable. Test recovery procedures before
production use.

## Evaluation

`evaluation/cases.json` contains synthetic text and visual cases. The runner
measures actual Gemini outputs only when `LLM_API_KEY` is configured. Without
that key it records the exact skip reason and leaves live-model predictions
empty; performance metrics are `null` when no model cases complete, not
zero-valued scores. Deterministic visual measurements and local retrieval
relevance still run.

```powershell
# Uses Gemini if LLM_API_KEY is set; otherwise records live model cases as skipped.
python -m evaluation.run

# Choose an alternate append-only report path.
python -m evaluation.run --output evaluation\demo-results.jsonl
```

Reports are timestamped JSON Lines saved to `evaluation/results.jsonl` by
default and can be loaded by an administrator. All examples and labels are
synthetic: scores describe this fixture set only, not real-world model
performance.

## Tests

```powershell
python -m pytest -q
python -m compileall -q backend bot evaluation tests
```

Tests use temporary databases and generated visual fixtures. They do not make
real Gemini, OCR.space, or Discord calls.

## Competition demonstration

Use the isolated sample database and brand rules in [demo/DEMO_GUIDE.md](demo/DEMO_GUIDE.md).
The guide walks through sign-in, a review grounded in sample brand knowledge,
request changes, creator resubmission, reviewer approval, audit verification,
and the evaluation report. Sample content must not be mixed with real
organisational data.

## Discord

With `DISCORD_BOT_TOKEN` configured, start the existing adapter:

```powershell
python -m bot.discord_bot
```

Image and text checks call protected backend review routes and share the web
review engine. Discord users cannot access review queues, decisions, accounts,
or administrative operations.
