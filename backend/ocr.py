import os

import httpx


class OCRServiceError(Exception):
    pass


async def extract_text(image_bytes: bytes, filename: str, content_type: str) -> str:
    api_key = os.getenv("OCR_API_KEY")
    if not api_key:
        raise OCRServiceError("OCR_API_KEY is not configured.")

    endpoint = os.getenv("OCR_API_URL", "https://api.ocr.space/parse/image")
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                endpoint,
                data={"apikey": api_key, "language": "eng", "isOverlayRequired": "false"},
                files={"file": (filename, image_bytes, content_type)},
            )
            response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise OCRServiceError(
            f"OCR provider returned HTTP {exc.response.status_code}."
        ) from exc
    except httpx.RequestError as exc:
        raise OCRServiceError("Could not reach the OCR provider.") from exc

    try:
        payload = response.json()
    except ValueError as exc:
        raise OCRServiceError("OCR provider returned invalid JSON.") from exc

    if not isinstance(payload, dict):
        raise OCRServiceError("OCR provider returned an invalid response.")
    if payload.get("IsErroredOnProcessing"):
        errors = payload.get("ErrorMessage", "Image could not be processed.")
        if isinstance(errors, list):
            errors = "; ".join(str(error) for error in errors)
        raise OCRServiceError(f"OCR could not process the image: {errors}")

    results = payload.get("ParsedResults")
    if not isinstance(results, list):
        raise OCRServiceError("OCR provider returned no parsed results.")

    text_parts = []
    for result in results:
        if not isinstance(result, dict):
            continue
        parsed_text = result.get("ParsedText")
        if isinstance(parsed_text, str) and parsed_text.strip():
            text_parts.append(parsed_text.strip())
    extracted_text = "\n".join(text_parts)
    return extracted_text
