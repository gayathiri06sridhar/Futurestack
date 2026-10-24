# AI Brand Governance Assistant

A working prototype that reviews marketing-image text against your approved
brand rules. A Discord `/check` command uploads an image to a FastAPI backend;
the backend extracts text with OCR.space and asks Gemini to return a structured
review. AI recommendations are advisory; a human makes the final decision.

## Requirements

- Python 3.10 or newer
- A Discord bot token, with the `bot` and `applications.commands` scopes
- An OCR.space API key
- A Gemini API key with access to the configured model

## Setup

From the project root, create and activate a virtual environment, then install
dependencies:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
py -m pip install -r requirements.txt
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

Set `DISCORD_BOT_TOKEN`, `OCR_API_KEY`, and your Gemini API key as `LLM_API_KEY`
in `.env`. Optionally set
`DISCORD_GUILD_ID` to register the slash command immediately in a development
server; without it, the command is registered globally and may take time to
appear. `LLM_MODEL` defaults to `gemini-2.5-flash`. `BACKEND_URL` defaults to
`http://127.0.0.1:8000`.

Edit [knowledge/brand_rules.json](knowledge/brand_rules.json) with your real,
approved tone guidance, restricted terms, claims, and product facts before
using the assistant. The included rules are deliberately a blank starting point,
not factual guidance for a real brand.

## Run

Open two terminals at the project root. Start the backend in the first:

```powershell
py -m uvicorn backend.main:app --reload
```

Start the Discord bot in the second:

```powershell
py -m bot.discord_bot
```

In a server where the bot is installed, run `/check` and attach an image. The
bot returns the detected text, overall status, severity, reasoning, and
suggested changes in an ephemeral Discord response. The backend is also
available at `http://127.0.0.1:8000`, with interactive API documentation at
`http://127.0.0.1:8000/docs`.

## API response

`POST /check` accepts a multipart `file` image (up to 10 MB) and returns the
extracted text plus a review with `status`, `issues`, `severity`, `reason`, and
`suggestion`. Each issue includes its type, relevant text, severity, reason,
and suggestion. OCR and LLM provider failures are returned as explicit HTTP
errors; no placeholder or simulated results are used.

Gemini HTTP errors include the provider's error message and, when available,
the quota identifier. A new API key still uses the quotas and billing settings
of its Google Cloud project; check that project's Gemini API quota and billing
if a 429 persists.

## Security and human review

Keep `.env` private; it is excluded by `.gitignore`. Do not put API keys in
source code or commit them. Images and extracted text are sent to the configured
external OCR and LLM providers. The AI review is not an approval or a
replacement for a human brand/legal decision-maker.
