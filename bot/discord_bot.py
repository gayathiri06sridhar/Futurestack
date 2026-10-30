import os
from pathlib import Path
from typing import Any

import discord
import httpx
from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")
MAX_IMAGE_SIZE = 10 * 1024 * 1024
BACKEND_URL = os.getenv("BACKEND_URL", "http://127.0.0.1:8000").rstrip("/")
DISCORD_API_KEY = os.getenv("DISCORD_API_KEY", "")


def clipped(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


def review_embed(payload: dict[str, Any]) -> discord.Embed:
    review = payload["review"]
    no_issues = review["status"] == "approved"
    embed = discord.Embed(
        title="AI findings: " + ("No issues detected" if no_issues else "Review needed"),
        description=(
            f"**Overall severity:** {review['severity'].title()}\n"
            f"**Assessment:** {clipped(review['reason'], 700)}\n"
            f"**Recommended next step:** {clipped(review['suggestion'], 500)}"
        ),
        color=discord.Color.green() if no_issues else discord.Color.orange(),
    )
    if review.get("review_id"):
        embed.add_field(name="Review ID", value=review["review_id"], inline=False)
    if review.get("objective_visual"):
        visual = review["objective_visual"]
        embed.add_field(
            name="Objective visual measurements",
            value=(
                f"Palette deviation: {visual['colour_check']['out_of_palette_fraction']}\n"
                f"Logo similarity: {visual['logo_check']['similarity']}\n"
                "Measurements are screening aids, not proof of compliance."
            ),
            inline=False,
        )
    extracted_text = payload.get("extracted_text", "").strip()
    if extracted_text:
        embed.add_field(
            name="Text detected in image",
            value=clipped(extracted_text, 700),
            inline=False,
        )

    issues = review.get("issues", [])
    for index, issue in enumerate(issues[:4], start=1):
        details = (
            f"**Severity:** {issue['severity'].title()}\n"
            f"**Text:** {clipped(issue['text'], 220)}\n"
            f"**Why:** {clipped(issue['reason'], 260)}\n"
            f"**Suggestion:** {clipped(issue['suggestion'], 260)}"
        )
        embed.add_field(
            name=f"Issue {index}: {issue['type']}", value=details, inline=False
        )
    if len(issues) > 4:
        embed.add_field(
            name="More issues",
            value=f"{len(issues) - 4} additional issue(s) omitted from this summary.",
            inline=False,
        )

    embed.set_footer(
        text="AI recommendations are advisory. A human makes the final decision."
    )
    return embed


class BrandGovernanceBot(discord.Client):
    def __init__(self) -> None:
        super().__init__(intents=discord.Intents.default())
        self.tree = discord.app_commands.CommandTree(self)

    async def setup_hook(self) -> None:
        guild_id = os.getenv("DISCORD_GUILD_ID")
        if guild_id:
            guild = discord.Object(id=int(guild_id))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()


bot = BrandGovernanceBot()


@bot.tree.command(name="check", description="Review an image against the brand rules")
@discord.app_commands.describe(image="The marketing image to review (maximum 10 MB)")
async def check(interaction: discord.Interaction, image: discord.Attachment) -> None:
    if not image.content_type or not image.content_type.startswith("image/"):
        await interaction.response.send_message(
            "Please attach an image file.", ephemeral=True
        )
        return
    if image.size > MAX_IMAGE_SIZE:
        await interaction.response.send_message(
            "Image is too large. The maximum upload size is 10 MB.", ephemeral=True
        )
        return

    await interaction.response.defer(thinking=True, ephemeral=True)
    try:
        image_bytes = await image.read()
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(
                f"{BACKEND_URL}/check",
                headers={"X-Discord-Key": DISCORD_API_KEY},
                files={
                    "file": (
                        image.filename,
                        image_bytes,
                        image.content_type,
                    )
                },
            )
    except discord.HTTPException:
        await interaction.followup.send(
            "Could not download the attached image from Discord.", ephemeral=True
        )
        return
    except httpx.RequestError:
        await interaction.followup.send(
            "Could not reach the review backend. Make sure the FastAPI service is running.",
            ephemeral=True,
        )
        return

    if not response.is_success:
        try:
            error_payload = response.json()
            detail = (
                error_payload.get("detail", "The review request failed.")
                if isinstance(error_payload, dict)
                else "The review backend returned an invalid error response."
            )
        except ValueError:
            detail = "The review backend returned an invalid error response."
        await interaction.followup.send(
            f"Review failed: {clipped(str(detail), 1200)}", ephemeral=True
        )
        return

    try:
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("review"), dict):
            raise ValueError("Invalid review response.")
        embed = review_embed(payload)
    except (ValueError, KeyError, TypeError):
        await interaction.followup.send(
            "The review backend returned an invalid response.", ephemeral=True
        )
        return
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="check-text", description="Review marketing copy against the brand rules")
@discord.app_commands.describe(content="Marketing text to review (maximum 2,000 characters)")
async def check_text(interaction: discord.Interaction, content: str) -> None:
    if not content.strip():
        await interaction.response.send_message(
            "Provide text content to review.", ephemeral=True
        )
        return
    await interaction.response.defer(thinking=True, ephemeral=True)
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(
                f"{BACKEND_URL}/discord/review-text",
                headers={"X-Discord-Key": DISCORD_API_KEY},
                json={"text": content.strip(), "content_category": "discord"},
            )
    except httpx.RequestError:
        await interaction.followup.send(
            "Could not reach the review backend.", ephemeral=True
        )
        return
    if not response.is_success:
        try:
            payload = response.json()
            detail = payload.get("detail", "Review failed.")
        except ValueError:
            detail = "The review backend returned an invalid response."
        await interaction.followup.send(
            f"Review failed: {clipped(str(detail), 1000)}", ephemeral=True
        )
        return
    try:
        payload = response.json()
        embed = review_embed(payload)
    except (ValueError, KeyError, TypeError):
        await interaction.followup.send(
            "The review backend returned an invalid response.", ephemeral=True
        )
        return
    await interaction.followup.send(embed=embed, ephemeral=True)


def main() -> None:
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_BOT_TOKEN is not configured.")
    if not DISCORD_API_KEY:
        raise RuntimeError("DISCORD_API_KEY is not configured.")
    bot.run(token)


if __name__ == "__main__":
    main()
