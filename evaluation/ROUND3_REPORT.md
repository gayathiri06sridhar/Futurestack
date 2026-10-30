# Futurestack Round 3 completion report

## Summary

The existing Futurestack project now has individual accounts, a persisted
human-decision and revision workflow, local visual screening measurements,
expanded synthetic evaluation, and additional operational controls. AI output
remains advisory: the backend initializes every submission as pending a human
decision and only an authenticated reviewer or administrator can approve,
request changes, or reject it.

This is a demonstrable prototype, not a claim of enterprise readiness.

## Implemented

- **Authentication and authorization:** Creator registration, login/logout,
  PBKDF2-HMAC-SHA256 password hashes, opaque server-validated sessions in
  HttpOnly cookies, CSRF-token validation, Admin/Reviewer/Creator roles, user
  administration, and creator ownership checks. Reviewer identity comes from
  the authenticated account. Former shared admin/reviewer keys are not used.
- **Human workflow:** Pending reviewer queue, severity filtering, AI findings
  and evidence, authenticated approval/rejection/change requests with comments
  and timestamps, creator-only revisions and resubmissions, ownership checks,
  history, and original/revised content comparison. Each resubmission returns
  to pending; AI status is separate from the human decision.
- **Visual screening:** Approved image assets, palette configuration, CIE76
  colour-distance comparison, dominant-colour reporting, fixed-grid
  perceptual-hash logo screening, configurable logo placement-zone checks, and
  WCAG contrast calculation for administrator-configured foreground/background
  pairs. Objective measurements are returned separately from model-assisted
  image observations.
- **Evaluation:** Expanded synthetic text cases cover compliant variations,
  incorrect specifications, unsupported/contradictory claims, restricted
  wording, and ambiguity (dataset version 2.1). Generated visual fixtures have
  ground truth. Reports include compliance confusion matrices and binary
  metrics, category metrics, severity confusion, lexical/vector retrieval
  hit@5, explicit skipped-case reasons, and UTC timestamps. Live Gemini results
  are recorded only when actual model requests complete.
- **Security and operations:** SQLite schema migrations (versions 1 and 2),
  persistent fixed-window rate limiting, configurable retention and deletion,
  optional fail-closed ClamAV integration, HMAC-linked audit events and
  verification, CSRF and same-site session cookies, configurable CORS, and
  production startup validation.
- **Dashboards and Discord:** Creator, reviewer, and admin workspaces use
  persisted server-side data. Admins can manage accounts, rules, knowledge,
  visual assets, audit checks, retention, and evaluation reports. Discord image
  and text checks use the shared review engine and a service key; Discord
  callers have no reviewer or administrator operations.
- **Demonstration:** `demo/DEMO_GUIDE.md` and `demo/start-demo.ps1` provide a
  fictional Everpine workflow backed by a separate demo database and rules
  file. The demonstration uses no fabricated AI output.

## Validation executed

| Validation | Result |
|---|---:|
| `python -m pytest -q` | 36 passed |
| `python -m compileall -q backend bot evaluation tests` | Passed |
| Final application startup / `/health` / OpenAPI check | Passed locally; health HTTP 200, OpenAPI HTTP 200 with 32 paths |
| Inline frontend JavaScript syntax check | Passed |
| FastAPI OpenAPI schema load | Passed; 32 paths |
| `python -m evaluation.run` | Completed at `2026-10-30T15:49:24.004779+00:00`; local measurements saved; all 11 live Gemini calls failed |
| Local Gemini configuration check | `.env` loading verified; configured model is `gemini-3.8-flash`; credential value was not displayed |
| Gemini provider unit tests | Passed; Interactions request/response parsing and API-key redaction checked with mocked responses |
| Controlled live Gemini verification | 2026-10-30T16:06:50.085Z; single synthetic request failed with `Could not reach the Gemini provider.`; no full evaluation started |
| Transport-diagnostic tests | Passed; timeout, connection, and TLS exception logs contain categories/types only and preserve the original exception as the cause |
| TLS/read-timeout investigation | 2026-10-30T16:33:15.341Z; read-phase timeout identified from outer `httpx.ReadTimeout`; no live requests made |
| Configurable Gemini response-read timeout | Default 180 seconds; connect/write/pool remain 90 seconds; no live requests made |
| Final controlled Gemini verification | 2026-10-30T16:52:05.576548+00:00; `compliant-approved-claim`, `gemini-3.8-flash`; failed with `read_timeout`; full evaluation not run |

Automated tests cover password hashing and sessions, role and ownership
boundaries, reviewer decisions, revision lifecycle, palette/logo/placement/
contrast measurements, audit-chain tamper detection, retention and deletion,
persistent rate limits, production startup validation, legacy schema upgrades,
and report generation without Gemini. Tests do not call Gemini, OCR.space, or Discord.
Frontend behavior was syntax checked; creator sign-in/dashboard was smoke
tested in a browser, but the complete submission/reviewer/resubmission flow was
not browser-tested.

## Measured evaluation results

Saved in `evaluation/results.jsonl`. The report timestamp is embedded in the
JSON record.

### Live Gemini text evaluation

The project `.env` contains `LLM_API_KEY` and selects
`LLM_MODEL=gemini-3.8-flash`. The last full `evaluation.run` attempt used this
configuration and attempted all 11 synthetic text cases through the Gemini
Interactions API. **None completed successfully.** Eight provider calls
received HTTP 503 responses reporting high model demand; three failed with a
connection error. Case-level reasons are recorded in `evaluation/results.jsonl`.

A controlled single-case verification was then attempted at
`2026-10-30T16:06:50.085Z` using the `compliant-approved-claim` synthetic case.
It failed with the exact provider diagnostic **`Could not reach the Gemini
provider.`** No HTTP response or structured review was received. In accordance
with the verification procedure, the full 11-case evaluation was not rerun
after this failure.

The final controlled review invocation started at
`2026-10-30T16:52:05.576548+00:00` for synthetic case
`compliant-approved-claim`. The project `.env` loaded successfully; the key
was configured and the model was `gemini-3.8-flash`. The configured read
timeout resolved to 180 seconds, with connect, write, and pool at 90 seconds.
The invocation failed with the generic user-facing error
`Could not reach the Gemini provider.` Safe logging classified it as
`read_timeout`, with exception types
`ReadTimeout -> ReadTimeout -> TimeoutError -> CancelledError ->
SSLWantReadError`. No HTTP response or structured review was received. This
read timeout does not trigger retries; only HTTP 429 and 503 do. No full
evaluation was run after this failure, and no Gemini predictions or metrics
were produced by this controlled test.

#### Connection failure investigation

The historical root cause remains **undetermined**. In
`backend/reviewer.py`, the API call sends a POST to
`https://generativelanguage.googleapis.com/v1beta/interactions`, with the
configured model and `x-goog-api-key` header. The API key is read from
`LLM_API_KEY`. `backend/main.py` and `evaluation/run.py` load the project `.env`
before their provider calls. HTTPX uses a configurable read timeout and
90-second connect, write, and pool timeouts. No credential value was printed
or changed.

The provider catches every `httpx.RequestError` and exposes only
`Could not reach the Gemini provider.` This includes different possible
transport failures (for example connect/read timeout, DNS/connectivity, proxy,
TLS, or protocol errors); the exception subtype and underlying cause from the
historical attempt were not recorded, so none can be selected as its cause.
The preceding live run's HTTP 503 responses are distinct server responses,
not this connection diagnostic.

Transport diagnostics now log a classified category (`read_timeout`,
`connect_timeout`, `write_timeout`, `pool_timeout`, `timeout`, `connection`,
`tls`, `protocol`, or generic `transport`) and exception class names only. Raw
exception messages and tracebacks are deliberately omitted because they can
contain request details. The raised user-facing error stays generic, and the
original HTTPX exception remains chained as `ReviewServiceError.__cause__`.
Mocked tests cover read timeout, connect failure, and TLS certificate failure;
they verify that API keys, submitted content, and exception-message text do not
appear in logs. No live Gemini request or full evaluation was made for this
diagnostic change.

##### Read-timeout follow-up (2026-10-30)

The later captured exception chain starts with `ReadTimeout` and includes
`SSLWantReadError`. HTTPX's outer `ReadTimeout` identifies the operation that
timed out as reading response data; the nested SSL wait indicates the TLS
stream was awaiting more data, but does not establish that the timeout
happened during the TLS handshake. The prior diagnostic incorrectly gave a
nested SSL exception precedence over the outer timeout. The classifier now
prioritizes HTTPX timeout subtypes and reports `read_timeout` for this chain.
Regression coverage verifies that an `SSLWantReadError` nested under
`ReadTimeout` remains a read-timeout classification.

The latest controlled synthetic request was reported with the exception chain
`ReadTimeout -> ReadTimeout -> TimeoutError -> CancelledError ->
SSLWantReadError` and no response. At that time, the earlier classifier logged
`tls` because the nested SSL exception took precedence. With the current
classifier, the same chain is categorized as `read_timeout`; the distinction
does not imply that TLS verification or the handshake failed. No additional
live request was made during this follow-up investigation.

The client uses `httpx.AsyncClient(timeout=...)`. HTTPX's scalar timeout
previously set a 90-second limit independently for connect, read, write, and
pool operations; it was not one overall request deadline. The observed
`ReadTimeout` therefore means an individual response-read wait exceeded 90
seconds.

Google's Interactions documentation describes background execution for
long-running tasks, but the reviewed documentation does not state a guaranteed
completion time or that this specific synthetic review should normally exceed
90 seconds. Increasing a timeout cannot resolve a stalled connection. Because
the application has direct evidence that this response-read wait exceeded 90
seconds, it now allows a configurable longer read window while leaving
connection, write, and pool timeouts unchanged. `GEMINI_READ_TIMEOUT_SECONDS`
defaults to 180 seconds; set it in the environment to choose another positive,
finite number. Invalid values fail explicitly. TLS verification remains at
HTTPX defaults. This is additional allowance, not a confirmed root-cause fix or
a claim that the request will finish within 180 seconds.

Regression tests verify the default and custom read values, unchanged 90-second
connect/write/pool values, and rejection of invalid timeout settings. No live
Gemini call or evaluation was made for this change.

During the static follow-up check, none of `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`,
`NO_PROXY`, or their lowercase variants were set in the process environment.
Installed versions are HTTPX 0.28.1 and httpcore 1.0.9; HTTPX declares
`httpcore==1.*`, so this pair is compatible. No version conflict or configured
environment proxy was identified. No live request was made for this follow-up.
The eventual provider or network cause of an earlier slow response remains
undetermined.

One unauthenticated connectivity probe was made at
`2026-10-30T16:09:38.158Z`: DNS resolution succeeded, HTTPS connected with TLS,
and GET on the Interactions path returned HTTP 403. No API key was sent and no
interaction was created. This establishes that DNS, network, and TLS worked
for this probe at that time, but the unauthenticated GET does not validate an
authenticated POST and does not establish what failed during the earlier
request.

Current official documentation confirms the endpoint and API-key header in
the [REST API-key example](https://ai.google.dev/gemini-api/docs/api-key),
documents the [Interactions endpoint](https://ai.google.dev/api/interactions-api),
and lists `gemini-3.8-flash` as an available model. The checked endpoint,
model, and authentication-header format are compatible with those docs; the
observed connectivity failure does not indicate a statically identifiable
URL-format error.

The run has `live_model_metrics.status` `failed` and tested count `0`.
Accuracy, precision, recall, F1, false-positive rate, false-negative rate,
category metrics, and severity classification are not calculable. The
corresponding scalar metrics are `null`; the empty confusion-matrix counts are
not performance measurements. No actual Gemini review was validated, so live
structured AI output, claim verification, risk classification, and behavioral
accuracy remain **untested**. A mocked provider test verifies that the request
uses the Interactions API with a JSON schema, that a structured response is
validated, and that provider errors do not expose the API key; it does not
substitute for a successful live call.

The provider now performs at most three retries (four total attempts) for
Interactions API HTTP 429 and 503 responses only. The final controlled failure
was a read timeout, so the retry policy did not retry that invocation. Retries
use exponential delays
starting at one second and honor `Retry-After` seconds or HTTP dates, bounded
to 30 seconds per wait. Other HTTP statuses are not retried. Automated tests
verify that a 503 honors `Retry-After` and that a 401 is attempted only once.
The final controlled live failure was a read timeout, not HTTP 429/503, so this
run did not exercise the retry path against Gemini. A prior controlled attempt
ended in a generic connection error.

The Gemini request now uses `POST /v1beta/interactions`, the configured model,
the documented `system_instruction`, and schema-constrained JSON
`response_format`. Requests set `store=false`. For image reviews, the adapter
uploads image bytes through the Gemini Files API and supplies the returned URI
to the interaction. The raw HTTPX provider architecture and response
validation are retained.

The actual evaluation command was executed with the populated local `.env`;
the report's model field and provider error responses confirm that the new
model path was selected. The API key was not printed, copied into reports, or
changed. Live evaluation must be rerun when Gemini capacity is available.
`OCR_API_KEY`, `DISCORD_BOT_TOKEN`, and `DISCORD_API_KEY` were not validated in
this run; OCR and Discord integrations were not exercised.

#### Final integration gate and local validation

The final live attempt above did not return a valid response, so the 11-case
evaluation gate was not met and no new entry was appended to
`evaluation/results.jsonl`. The latest saved full evaluation remains the
earlier run with 0 successful model cases out of 11; all 11 failures are
preserved there. The single-case live attempt is documented here only and is
not counted as a dataset prediction.

After that failure, the full local test suite passed (**36 passed**), and
`python -m compileall -q backend bot evaluation tests` passed. A local
FastAPI startup check passed; `/health` returned HTTP 200 with a healthy
status and `/openapi.json` returned HTTP 200 with 32 paths. The project `.env`
is ignored by `.gitignore`; a local scan found no literal configured-key match
in other project files. This folder has no Git metadata, so membership in a
Git index could not be checked. The API key was not printed or modified.

### Deterministic local visual measurements

Five generated visual cases were measured against the supplied synthetic ground
truth. The timestamp is recorded in the corresponding JSON Lines report.
The binary positive class is a compliant creative.

| Metric | Measured result |
|---|---:|
| Accuracy | 1.00 (5/5) |
| Precision | 1.00 |
| Recall | 1.00 |
| F1 | 1.00 |
| False-positive rate | 0.00 (0/4 non-compliant cases falsely passed) |
| False-negative rate | 0.00 (0/1 compliant case falsely failed) |
| Confusion matrix (TP/TN/FP/FN) | 1 / 4 / 0 / 0 |
| Severity accuracy | 1.00 (5/5) |

The synthetic-category precision/recall/F1 values are 1.00 for all four
included categories: `accessibility_issue`, `logo_placement_violation`,
`logo_violation`, and `visual_inconsistency`. This tiny fixture-set score is
not a real-world accuracy claim or a guarantee of visual compliance.

### Retrieval relevance

Lexical retrieval returned the expected synthetic evidence in the top five for
11/11 text cases: **hit@5 = 1.00**. This measures a small synthetic ruleset,
not production retrieval quality.

## Security and privacy notes

- `ENVIRONMENT=production` requires an explicit 32-character
  `AUDIT_HMAC_KEY`, explicit CORS origins, and `ALLOW_REGISTRATION=false`.
  Production session cookies are Secure. Use HTTPS and a trusted deployment
  boundary; built-in controls do not replace MFA, monitoring, edge protection,
  or a security assessment.
- `MALWARE_SCANNER=disabled` by default means there is no active malware
  scanning. ClamAV is only used when configured and available; configured scan
  failures reject uploads.
- The HMAC hash chain is tamper-evident only while the host administrator
  cannot alter both the SQLite database and signing secret. It is not
  tamper-proof against that administrator.
- Retention and deletion remove review rows and request SQLite secure deletion.
  They cannot guarantee erasure from SSD wear-levelling, external backups, or
  copied database files. Audit metadata is retained.
- SQLite fixed-window rate limits are persistent across workers sharing that
  file, but they are not a distributed rate-limiting service. Apply trusted
  proxy handling and edge rate limits in deployment.
- Images and text may be sent to Gemini and OCR.space when configured. Gemini
  embeddings also transmit queries and document chunks. Review privacy and
  provider data terms before using real content.
- Logo similarity is a crop-grid perceptual-hash heuristic; dominant-colour
  measurements cannot isolate text or logo regions. Contrast ratios are
  calculated for configured colour pairs, not automatically detected
  foreground/background pixels. These checks are objective but limited
  screenings, not a compliance certification.

## Startup and migration

Installation, production configuration, backup/recovery steps, test commands,
and Discord setup are documented in `Readme.md`. Database migrations run at
application startup and their applied versions are returned by `/health`.
For an isolated competition walkthrough, follow `demo/DEMO_GUIDE.md`.
