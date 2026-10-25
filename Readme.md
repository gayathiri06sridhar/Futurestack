# Futurestack

**An AI brand-governance framework for reviewing marketing content against a
team's own knowledge base.**

This judging prototype demonstrates the review flow: upload an image, extract
its text with OCR.space, and get advisory feedback from Gemini against the
locally configured rules in `knowledge/brand_rules.json`.

The broader framework is designed to run in a team's working environment and
respond to commands in tools such as Discord and Microsoft Teams. This prototype
includes a web upload page and a Discord `/check` bot; Microsoft Teams support
is not implemented.

## Try it locally

Requires Python 3.10+, an OCR.space API key, a Gemini API key, and (for Discord)
a Discord bot token.

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
py -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Add `OCR_API_KEY`, `LLM_API_KEY`, and `DISCORD_BOT_TOKEN` to `.env`. Set
`DISCORD_GUILD_ID` if you want the Discord command registered to a development
server. Update `knowledge/brand_rules.json` with your approved brand rules and
product facts.

Start the backend:

```powershell
py -m uvicorn backend.main:app --reload
```

Open `http://127.0.0.1:8000` for the web demo. To also run the Discord bot, open
a second terminal and run:

```powershell
py -m bot.discord_bot
```

Use `/check` in a server where the bot is installed. Both the web page and bot
send images to the same backend review endpoint.

## Hosting

Deploy the FastAPI app with the `frontend` and `knowledge` directories. Set
`OCR_API_KEY` and `LLM_API_KEY` as private environment variables, and start the
service with the host's required port, for example:

```text
uvicorn backend.main:app --host 0.0.0.0 --port $PORT
```

`index.py` exports the app for hosts that look for a root-level FastAPI
entrypoint. Set the Discord bot's `BACKEND_URL` to the deployed service URL if
the bot should use that deployment.

> **Before making a deployment public:** `/check` has no authentication or
> request limits. Add access controls to avoid unexpected provider usage or
> costs. Images and extracted text are sent to external OCR and AI providers;
> all review feedback is advisory and requires human judgment.
