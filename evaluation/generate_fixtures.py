from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


FIXTURE_DIR = Path(__file__).with_name("fixtures")


def create_reference_logo() -> Image.Image:
    image = Image.new("RGB", (160, 100), "#ffffff")
    draw = ImageDraw.Draw(image)
    draw.polygon(
        [(12, 86), (44, 18), (76, 86), (60, 75), (44, 43), (28, 75)],
        fill="#174d38",
    )
    draw.rectangle((82, 25, 94, 85), fill="#174d38")
    draw.rectangle((82, 15, 110, 27), fill="#174d38")
    return image


def generate() -> None:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    logo = create_reference_logo()
    logo.save(FIXTURE_DIR / "approved_logo.png")

    compliant = Image.new("RGB", (640, 420), "#174d38")
    compliant.paste(logo, (440, 290))
    compliant.save(FIXTURE_DIR / "compliant_creative.png")

    misplaced = Image.new("RGB", (640, 420), "#174d38")
    misplaced.paste(logo, (20, 20))
    misplaced.save(FIXTURE_DIR / "misplaced_logo.png")

    wrong_palette = Image.new("RGB", (640, 420), "#d92736")
    draw = ImageDraw.Draw(wrong_palette)
    draw.rectangle((40, 50, 600, 370), outline="#f4b942", width=18)
    wrong_palette.save(FIXTURE_DIR / "wrong_palette.png")

    missing_logo = Image.new("RGB", (640, 420), "#174d38")
    draw = ImageDraw.Draw(missing_logo)
    draw.rectangle((80, 90, 560, 330), outline="#ffffff", width=5)
    missing_logo.save(FIXTURE_DIR / "missing_logo.png")

    low_contrast = Image.new("RGB", (640, 420), "#ffffff")
    draw = ImageDraw.Draw(low_contrast)
    try:
        font = ImageFont.truetype("arial.ttf", 62)
    except OSError:
        font = ImageFont.load_default(size=62)
    draw.text((40, 150), "Everpine campaign", fill="#a7cfb1", font=font)
    low_contrast.save(FIXTURE_DIR / "low_contrast.png")


if __name__ == "__main__":
    generate()
