from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, UploadFile

from backend.knowledge_base import load_brand_rules
from backend.ocr import OCRServiceError, extract_text
from backend.reviewer import ReviewServiceError, review_text


PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")
MAX_IMAGE_SIZE = 10 * 1024 * 1024

app = FastAPI(
    title="AI Brand Governance Assistant",
    description="Advisory brand-compliance reviews for marketing images.",
    version="1.0.0",
)


@app.get("/")
async def root() -> dict[str, str]:
    return {"name": "AI Brand Governance Assistant", "status": "running"}


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "healthy"}


@app.post("/check")
async def check_content(file: UploadFile = File(...)) -> dict[str, object]:
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="Please upload an image.")

    image_bytes = await file.read(MAX_IMAGE_SIZE + 1)
    if not image_bytes:
        raise HTTPException(status_code=400, detail="The uploaded image is empty.")
    if len(image_bytes) > MAX_IMAGE_SIZE:
        raise HTTPException(
            status_code=413,
            detail="Image is too large. The maximum upload size is 10 MB.",
        )

    try:
        extracted_text = await extract_text(
            image_bytes,
            file.filename or "image",
            file.content_type,
        )
    except OCRServiceError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    try:
        brand_rules = load_brand_rules()
        review = await review_text(extracted_text, brand_rules)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except ReviewServiceError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return {
        "filename": file.filename,
        "extracted_text": extracted_text,
        "review": review.model_dump(mode="json"),
    }
