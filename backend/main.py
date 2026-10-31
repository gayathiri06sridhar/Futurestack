import io
import json
import logging
import os
import re
import hmac
import secrets
import hashlib
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Awaitable, Callable

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from starlette.responses import JSONResponse
from starlette.middleware.cors import CORSMiddleware

PROJECT_ROOT = Path(__file__).resolve().parents[1]
WEB_PAGE = PROJECT_ROOT / "frontend" / "index.html"
load_dotenv(PROJECT_ROOT / ".env")

from backend.knowledge_base import RULES_PATH, load_brand_rules
from backend.ocr import OCRServiceError, extract_text
from backend.retrieval import (
    delete_document,
    index_document,
    list_documents,
    retrieve_evidence,
)
from backend.reviewer import ReviewServiceError, review_text
from backend.security import (
    MalwareDetectedError,
    MalwareScannerError,
    hash_password,
    new_session_tokens,
    scan_uploaded_bytes,
    valid_csrf_token,
    verify_password,
)
from backend.storage import (
    check_rate_limit,
    count_users,
    create_session,
    create_user,
    delete_visual_asset,
    delete_session,
    dashboard_counts,
    get_review,
    get_session,
    get_user,
    get_user_by_email,
    get_setting,
    get_visual_assets,
    initialize_database,
    list_users,
    list_audit_events,
    list_visual_assets,
    list_reviews,
    purge_reviews_older_than,
    delete_review_thread,
    migration_versions,
    record_audit,
    rotate_csrf,
    save_visual_asset,
    set_setting,
    save_review,
    update_user,
    update_decision,
    verify_audit_chain,
)
from backend.visual import analyze_visual_content, decode_image


MAX_UPLOAD_SIZE = 10 * 1024 * 1024
MAX_TEXT_SIZE = 100_000
logger = logging.getLogger("futurestack")
RATE_LIMIT_PER_MINUTE = int(os.getenv("RATE_LIMIT_REQUESTS_PER_MINUTE", "20"))
if RATE_LIMIT_PER_MINUTE < 1:
    raise ValueError("RATE_LIMIT_REQUESTS_PER_MINUTE must be a positive integer.")
ENVIRONMENT = os.getenv("ENVIRONMENT", "development").strip().lower()
ALLOW_REGISTRATION = os.getenv("ALLOW_REGISTRATION", "true").strip().lower() == "true"
SESSION_COOKIE = "futurestack_session"


@asynccontextmanager
async def lifespan(_: FastAPI):
    if ENVIRONMENT == "production":
        if not os.getenv("AUDIT_HMAC_KEY") or len(os.getenv("AUDIT_HMAC_KEY", "")) < 32:
            raise RuntimeError("Production requires AUDIT_HMAC_KEY with at least 32 characters.")
        configured_origins = os.getenv("CORS_ALLOWED_ORIGINS", "").strip()
        if not configured_origins or configured_origins == "*":
            raise RuntimeError("Production requires explicit CORS_ALLOWED_ORIGINS.")
        if ALLOW_REGISTRATION:
            raise RuntimeError("Production requires ALLOW_REGISTRATION=false.")
    initialize_database()
    bootstrap_email = os.getenv("INITIAL_ADMIN_EMAIL", "").strip()
    bootstrap_password = os.getenv("INITIAL_ADMIN_PASSWORD", "")
    if bootstrap_email and bootstrap_password and count_users() == 0:
        create_user(bootstrap_email, hash_password(bootstrap_password), "admin")
    elif bool(bootstrap_email) != bool(bootstrap_password) and count_users() == 0:
        raise RuntimeError(
            "Set both INITIAL_ADMIN_EMAIL and INITIAL_ADMIN_PASSWORD to bootstrap the first admin."
        )
    purge_reviews_older_than(int(os.getenv("RETENTION_DAYS", "365")))
    rules = load_brand_rules()
    await index_document(
        "brand-rules",
        str(rules.get("brand_name", "Brand rules")),
        "knowledge/brand_rules.json",
        json.dumps(rules, ensure_ascii=False, indent=2),
    )
    yield


app = FastAPI(
    title="AI Brand Governance Assistant",
    description=(
        "Evidence-grounded advisory brand reviews. AI recommendations do not "
        "replace a human approval decision."
    ),
    version="2.0.0",
    lifespan=lifespan,
)
cors_origins = [
    origin.strip()
    for origin in os.getenv("CORS_ALLOWED_ORIGINS", "http://127.0.0.1:8000,http://localhost:8000").split(",")
    if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=["Content-Type", "X-CSRF-Token", "X-Discord-Key"],
)


@app.middleware("http")
async def limit_submission_rate(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    if request.url.path not in {
        "/auth/login",
        "/auth/register",
        "/check",
        "/discord/review-text",
        "/review",
        "/review-text",
        "/review-document",
    }:
        return await call_next(request)
    client_ip = request.client.host if request.client else "unknown"
    bucket = "login" if request.url.path == "/auth/login" else request.url.path
    rate = 8 if bucket == "login" else RATE_LIMIT_PER_MINUTE
    if not check_rate_limit(bucket, client_ip, rate):
        return JSONResponse(
            status_code=429,
            content={"detail": "Request rate limit exceeded; try again shortly."},
        )
    return await call_next(request)


class TextSubmission(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_TEXT_SIZE)
    content_category: str = Field(default="marketing", max_length=80)


class DecisionSubmission(BaseModel):
    decision: str
    reason: str = Field(default="", max_length=2000)


class RegisterRequest(BaseModel):
    email: str = Field(min_length=5, max_length=254)
    password: str = Field(min_length=12, max_length=256)


class LoginRequest(BaseModel):
    email: str = Field(min_length=5, max_length=254)
    password: str = Field(min_length=1, max_length=256)


class UserUpdate(BaseModel):
    role: str | None = None
    is_active: bool | None = None


class AdminUserRequest(RegisterRequest):
    role: str


class VisualSettings(BaseModel):
    approved_colours: list[str] = Field(default_factory=list, max_length=20)
    thresholds: dict[str, float] = Field(default_factory=dict)
    contrast_pairs: list[dict[str, str]] = Field(default_factory=list, max_length=30)
    expected_logo_zone: str = "any"


class KnowledgeDocument(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=MAX_TEXT_SIZE)
    source: str = Field(default="admin-upload", max_length=200)


def _safe_filename(filename: str | None) -> str:
    normalized = (filename or "submission").replace("\\", "/").split("/")[-1]
    normalized = re.sub(r"[\x00-\x1f\x7f]", "", normalized).strip()
    return normalized[:180] or "submission"


def _scan_upload(data: bytes) -> dict[str, str]:
    try:
        return scan_uploaded_bytes(data)
    except MalwareDetectedError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except MalwareScannerError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _sniff_upload(data: bytes, filename: str) -> tuple[str, str]:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", "image"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", "image"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif", "image"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", "image"
    if data.startswith(b"%PDF-"):
        return "application/pdf", "pdf"

    extension = Path(filename).suffix.lower()
    if extension in {".txt", ".md", ".csv", ".json"}:
        if b"\x00" in data:
            raise HTTPException(status_code=400, detail="The document is not valid text.")
        return "text/plain", "text"
    raise HTTPException(
        status_code=400,
        detail="Unsupported file content. Upload PNG, JPEG, GIF, WebP, PDF, TXT, MD, CSV or JSON.",
    )


async def _extract_document(data: bytes, kind: str) -> str:
    if kind == "text":
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise HTTPException(
                status_code=400, detail="Text documents must use UTF-8 encoding."
            ) from exc
    elif kind == "pdf":
        try:
            from pypdf.errors import PyPdfError
            from pypdf import PdfReader

            reader = PdfReader(io.BytesIO(data), strict=True)
            if len(reader.pages) > 100:
                raise HTTPException(
                    status_code=413, detail="PDFs are limited to 100 pages."
                )
            text = "\n\n".join(page.extract_text() or "" for page in reader.pages)
        except HTTPException:
            raise
        except PyPdfError as exc:
            logger.info("Rejected PDF that could not be parsed.")
            raise HTTPException(
                status_code=400, detail="The PDF could not be read."
            ) from exc
        except (ValueError, OSError) as exc:
            logger.info("Rejected PDF that could not be parsed.")
            raise HTTPException(
                status_code=400, detail="The PDF could not be read."
            ) from exc
    else:
        raise ValueError(f"Unsupported document kind: {kind}")

    text = text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="No readable text was found.")
    if len(text) > MAX_TEXT_SIZE:
        raise HTTPException(
            status_code=413, detail="Extracted document text exceeds the 100 KB limit."
        )
    return text


async def _create_review(
    submitted_text: str,
    content_type: str,
    filename: str,
    image_bytes: bytes | None = None,
    owner_id: str | None = None,
    parent_review_id: str | None = None,
) -> dict[str, Any]:
    if len(submitted_text) > MAX_TEXT_SIZE:
        raise HTTPException(status_code=413, detail="Submitted text exceeds 100 KB.")
    rules = load_brand_rules()
    retrieved = await retrieve_evidence(submitted_text)
    objective_visual = None
    if image_bytes is not None:
        settings = get_setting("visual_settings")
        logos = [asset["image_bytes"] for asset in get_visual_assets("logo")]
        try:
            objective_visual = analyze_visual_content(
                image_bytes,
                approved_colours=settings.get("approved_colours", []),
                approved_logos=logos,
                contrast_pairs=settings.get("contrast_pairs", []),
                thresholds=settings.get("thresholds", {}),
                expected_logo_zone=settings.get("expected_logo_zone", "any"),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        result = await review_text(
            submitted_text,
            rules,
            evidence=retrieved["evidence"],
            image_bytes=image_bytes,
            image_type=content_type if image_bytes is not None else None,
            retrieval_mode=retrieved["mode"],
        )
    except ReviewServiceError:
        raise
    review_data = result.model_dump(mode="json")
    if objective_visual is not None:
        review_data["objective_visual"] = objective_visual
    review_id = save_review(
        filename=filename,
        content_type=content_type,
        submitted_text=submitted_text,
        extracted_text=submitted_text,
        review=review_data,
        owner_id=owner_id,
        parent_review_id=parent_review_id,
    )
    review_data["review_id"] = review_id
    return {
        "filename": filename,
        "extracted_text": submitted_text,
        "review": review_data,
        "retrieval": {
            "mode": retrieved["mode"],
            "sources": [
                {
                    "title": item["title"],
                    "source": item["source"],
                    "relevance_score": item["relevance_score"],
                }
                for item in retrieved["evidence"]
            ],
        },
        "objective_visual": objective_visual,
    }


def _public_user(user: dict[str, Any]) -> dict[str, Any]:
    return {
        "user_id": user["user_id"],
        "email": user["email"],
        "role": user["role"],
        "is_active": bool(user.get("is_active", True)),
    }


def get_current_user(request: Request) -> dict[str, Any]:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise HTTPException(status_code=401, detail="Please log in.")
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    session = get_session(token_hash)
    if session is None:
        raise HTTPException(status_code=401, detail="Session expired. Please log in again.")
    return session


def get_optional_current_user(request: Request) -> dict[str, Any] | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    session = get_session(hashlib.sha256(token.encode()).hexdigest())
    if session is None:
        raise HTTPException(status_code=401, detail="Session expired. Please log in again.")
    return session


def require_csrf(
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    if request.method in {"POST", "PUT", "PATCH", "DELETE"} and not valid_csrf_token(
        request.headers.get("X-CSRF-Token"), user["csrf_hash"]
    ):
        raise HTTPException(status_code=403, detail="CSRF token is missing or invalid.")
    return user


def require_role(*roles: str):
    def dependency(
        user: dict[str, Any] = Depends(get_current_user),
    ) -> dict[str, Any]:
        if user["role"] not in roles:
            raise HTTPException(status_code=403, detail="Your role cannot access this operation.")
        return user

    return dependency


def require_admin(
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Administrator access is required.")
    return user


def require_reviewer(
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    if user["role"] not in {"admin", "reviewer"}:
        raise HTTPException(status_code=403, detail="Reviewer access is required.")
    return user


def require_creator(
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    if user["role"] != "creator":
        raise HTTPException(status_code=403, detail="Creator access is required.")
    return user


def require_discord_key(request: Request) -> str:
    expected = os.getenv("DISCORD_API_KEY")
    supplied = request.headers.get("X-Discord-Key")
    if not expected or not supplied or not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="Discord service authorization failed.")
    return "discord-service"


@app.get("/")
async def root() -> FileResponse:
    return FileResponse(WEB_PAGE, media_type="text/html")


@app.post("/auth/register", status_code=201)
async def register_account(registration: RegisterRequest) -> dict[str, Any]:
    if not ALLOW_REGISTRATION:
        raise HTTPException(status_code=403, detail="Registration is disabled.")
    email = registration.email.strip().lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise HTTPException(status_code=422, detail="Enter a valid email address.")
    try:
        password_hash = hash_password(registration.password)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        user_id = create_user(email, password_hash, "creator")
    except sqlite3.IntegrityError as exc:
        raise HTTPException(status_code=409, detail="An account with this email already exists.") from exc
    record_audit(user_id, "registration_completed", user_id)
    return {"user_id": user_id, "email": email, "role": "creator"}


@app.post("/auth/login")
async def login(credentials: LoginRequest, response: Response) -> dict[str, Any]:
    user = get_user_by_email(credentials.email)
    password_hash = user["password_hash"] if user else (
        "pbkdf2_sha256$600000$00000000000000000000000000000000$"
        + ("0" * 64)
    )
    password_valid = verify_password(credentials.password, password_hash)
    if user is None or not user["is_active"] or not password_valid:
        record_audit(
            "anonymous",
            "authentication_failed",
            credentials.email.strip().lower()[:254],
        )
        raise HTTPException(status_code=401, detail="Email or password is incorrect.")
    token, token_hash, csrf_token, csrf_hash = new_session_tokens()
    create_session(token_hash, csrf_hash, user["user_id"])
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=12 * 60 * 60,
        httponly=True,
        secure=ENVIRONMENT == "production",
        samesite="strict",
        path="/",
    )
    record_audit(user["user_id"], "authentication_succeeded", user["user_id"])
    return {"user": _public_user(user), "csrf_token": csrf_token}


@app.get("/auth/me")
async def current_account(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    csrf_token = secrets.token_urlsafe(32)
    csrf_hash = hashlib.sha256(csrf_token.encode()).hexdigest()
    rotate_csrf(user["session_id"], csrf_hash)
    return {"user": _public_user(user), "csrf_token": csrf_token}


@app.post("/auth/logout")
async def logout(
    request: Request,
    response: Response,
    user: dict[str, Any] = Depends(require_csrf),
) -> dict[str, str]:
    token = request.cookies.get(SESSION_COOKIE, "")
    delete_session(hashlib.sha256(token.encode()).hexdigest())
    response.delete_cookie(
        SESSION_COOKIE,
        httponly=True,
        secure=ENVIRONMENT == "production",
        samesite="strict",
        path="/",
    )
    record_audit(user["user_id"], "authentication_logout", user["user_id"])
    return {"status": "logged_out"}


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "healthy",
        "database": "sqlite",
        "ai_provider_configured": bool(os.getenv("LLM_API_KEY")),
        "ocr_provider_configured": bool(os.getenv("OCR_API_KEY")),
        "embedding_provider_configured": bool(os.getenv("LLM_API_KEY"))
        and os.getenv("EMBEDDING_PROVIDER", "gemini").lower() == "gemini",
        "malware_scanner": os.getenv("MALWARE_SCANNER", "disabled").lower(),
        "production_mode": ENVIRONMENT == "production",
        "database_migrations": migration_versions(),
    }


@app.post("/check")
async def check_content(
    file: UploadFile = File(...),
    _: str = Depends(require_discord_key),
) -> dict[str, Any]:
    filename = _safe_filename(file.filename)
    data = await file.read(MAX_UPLOAD_SIZE + 1)
    await file.close()
    if not data:
        raise HTTPException(status_code=400, detail="The uploaded file is empty.")
    if len(data) > MAX_UPLOAD_SIZE:
        raise HTTPException(
            status_code=413, detail="Upload is too large. The maximum is 10 MB."
        )
    _scan_upload(data)

    content_type, kind = _sniff_upload(data, filename)
    if kind != "image":
        raise HTTPException(
            status_code=400, detail="The /check endpoint accepts image files only."
        )
    try:
        extracted_text = await extract_text(data, filename, content_type)
    except OCRServiceError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    try:
        return await _create_review(
            extracted_text or "[No readable text detected in image; assess visible elements.]",
            content_type,
            filename,
            image_bytes=data,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except ReviewServiceError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/discord/review-text")
async def discord_review_text(
    submission: TextSubmission,
    _: str = Depends(require_discord_key),
) -> dict[str, Any]:
    try:
        return await _create_review(
            submission.text.strip(),
            "text/plain",
            f"discord-{submission.content_category.strip() or 'marketing'}",
        )
    except ReviewServiceError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/review-text")
async def check_text(
    submission: TextSubmission,
    actor: dict[str, Any] = Depends(require_creator),
    _: dict[str, Any] = Depends(require_csrf),
) -> dict[str, Any]:
    try:
        return await _create_review(
            submission.text.strip(),
            "text/plain",
            f"{submission.content_category.strip() or 'marketing'}-content",
            owner_id=actor["user_id"],
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except ReviewServiceError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/review-document")
async def check_document(
    file: UploadFile = File(...),
    actor: dict[str, Any] = Depends(require_creator),
    _: dict[str, Any] = Depends(require_csrf),
) -> dict[str, Any]:
    filename = _safe_filename(file.filename)
    data = await file.read(MAX_UPLOAD_SIZE + 1)
    await file.close()
    if not data:
        raise HTTPException(status_code=400, detail="The uploaded file is empty.")
    if len(data) > MAX_UPLOAD_SIZE:
        raise HTTPException(
            status_code=413, detail="Upload is too large. The maximum is 10 MB."
        )
    _scan_upload(data)
    content_type, kind = _sniff_upload(data, filename)
    if kind == "image":
        raise HTTPException(
            status_code=400, detail="Use /check for image files."
        )
    text = await _extract_document(data, kind)
    try:
        return await _create_review(
            text, content_type, filename, owner_id=actor["user_id"]
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except ReviewServiceError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/review")
async def review_submission(
    request: Request,
    text: str = Form(default=""),
    file: UploadFile | None = File(default=None),
    parent_review_id: str | None = Form(default=None),
    actor: dict[str, Any] | None = Depends(get_optional_current_user),
) -> dict[str, Any]:
    if actor is not None and not valid_csrf_token(
        request.headers.get("X-CSRF-Token"), actor["csrf_hash"]
    ):
        raise HTTPException(status_code=403, detail="CSRF token is missing or invalid.")
    if parent_review_id and actor is None:
        raise HTTPException(status_code=401, detail="Sign in to revise a submission.")

    pasted_text = text.strip()
    if file is not None:
        filename = _safe_filename(file.filename)
        data = await file.read(MAX_UPLOAD_SIZE + 1)
        await file.close()
        if not data:
            raise HTTPException(status_code=400, detail="The uploaded file is empty.")
        if len(data) > MAX_UPLOAD_SIZE:
            raise HTTPException(
                status_code=413, detail="Upload is too large. The maximum is 10 MB."
            )
        _scan_upload(data)
        content_type, kind = _sniff_upload(data, filename)
        if kind == "image":
            try:
                extracted = await extract_text(data, filename, content_type)
            except OCRServiceError as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from exc
            portions = [pasted_text]
            if extracted:
                portions.append(f"Text detected in image:\n{extracted}")
            text = "\n\n".join(portions)
            text = text or "[No readable text detected in image; assess visible elements.]"
            image_bytes = data
        else:
            extracted = await _extract_document(data, kind)
            text = "\n\n".join(part for part in (pasted_text, extracted) if part)
            image_bytes = None
    else:
        filename = "text-submission"
        content_type = "text/plain"
        image_bytes = None
        text = pasted_text

    text = text.strip()
    if not text:
        raise HTTPException(
            status_code=400, detail="Provide text or upload a supported file."
        )
    if len(text) > MAX_TEXT_SIZE:
        raise HTTPException(status_code=413, detail="Submitted text exceeds 100 KB.")
    try:
        return await _create_review(
            text,
            content_type,
            filename,
            image_bytes,
            owner_id=actor["user_id"] if actor is not None else None,
            parent_review_id=parent_review_id,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except ReviewServiceError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/reviews")
async def reviews(
    limit: int = Query(default=50, ge=1, le=200),
    decision: str | None = Query(default=None),
    severity: str | None = Query(default=None),
    reviewer: dict[str, Any] = Depends(require_reviewer),
) -> list[dict[str, Any]]:
    entries = list_reviews(limit, pending_only=decision == "pending")
    if decision and decision != "pending":
        entries = [entry for entry in entries if entry["decision"] == decision]
    if severity:
        entries = [
            entry
            for entry in entries
            if entry["review"].get("severity") == severity
        ]
    return entries


@app.get("/reviews/{review_id}")
async def review_detail(review_id: str, _: dict[str, Any] = Depends(require_reviewer)) -> dict[str, Any]:
    result = get_review(review_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Review not found.")
    return result


@app.post("/reviews/{review_id}/decision")
async def make_decision(
    review_id: str,
    decision: DecisionSubmission,
    reviewer: dict[str, Any] = Depends(require_csrf),
) -> dict[str, str]:
    if reviewer["role"] not in {"admin", "reviewer"}:
        raise HTTPException(status_code=403, detail="Reviewer access is required.")
    allowed = {"approved", "changes_requested", "rejected"}
    if decision.decision not in allowed:
        raise HTTPException(
            status_code=422,
            detail="Decision must be approved, changes_requested or rejected.",
        )
    if decision.decision in {"rejected", "changes_requested"} and not decision.reason.strip():
        raise HTTPException(
            status_code=422, detail="A reviewer comment is required for this decision."
        )
    target = get_review(review_id)
    if target is None:
        raise HTTPException(status_code=404, detail="Review not found.")
    if not update_decision(
        review_id,
        decision.decision,
        reviewer["user_id"],
        reviewer["email"],
        decision.reason.strip(),
    ):
        raise HTTPException(status_code=409, detail="This submission already has a human decision.")
    return {"review_id": review_id, "decision": decision.decision}


@app.get("/dashboard")
async def dashboard(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, int]:
    if user["role"] == "creator":
        return dashboard_counts(user["user_id"])
    return dashboard_counts()


@app.get("/my/submissions")
async def my_submissions(
    user: dict[str, Any] = Depends(get_current_user),
) -> list[dict[str, Any]]:
    return list_reviews(200, owner_id=user["user_id"])


@app.get("/my/submissions/{review_id}")
async def my_submission_detail(
    review_id: str,
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    result = get_review(review_id)
    if result is None or result["owner_id"] != user["user_id"]:
        raise HTTPException(status_code=404, detail="Submission not found.")
    return result


@app.delete("/my/submissions/{review_id}")
async def delete_my_submission(
    review_id: str,
    user: dict[str, Any] = Depends(require_csrf),
) -> dict[str, str]:
    if user["role"] != "creator":
        raise HTTPException(status_code=403, detail="Creator access is required.")
    if not delete_review_thread(review_id, user["user_id"], owner_id=user["user_id"]):
        raise HTTPException(status_code=404, detail="Submission not found.")
    return {"status": "content_deleted"}


@app.delete("/admin/submissions/{review_id}")
async def admin_delete_submission(
    review_id: str,
    actor: dict[str, Any] = Depends(require_csrf),
) -> dict[str, str]:
    if actor["role"] != "admin":
        raise HTTPException(status_code=403, detail="Administrator access is required.")
    if settings.expected_logo_zone not in {
        "any",
        "top_left",
        "top_right",
        "bottom_left",
        "bottom_right",
        "center",
    }:
        raise HTTPException(status_code=422, detail="Unsupported expected logo zone.")
    if not delete_review_thread(review_id, actor["user_id"]):
        raise HTTPException(status_code=404, detail="Submission not found.")
    return {"status": "content_deleted"}


@app.get("/audit")
async def audit(
    limit: int = Query(default=100, ge=1, le=500),
    _: dict[str, Any] = Depends(require_admin),
) -> list[dict[str, Any]]:
    return list_audit_events(limit)


@app.get("/knowledge/documents")
async def knowledge_documents(
    _: dict[str, Any] = Depends(require_admin),
) -> list[dict[str, str]]:
    return list_documents()


@app.post("/knowledge/documents")
async def add_knowledge_document(
    document: KnowledgeDocument,
    actor: dict[str, Any] = Depends(require_csrf),
) -> dict[str, str]:
    if actor["role"] != "admin":
        raise HTTPException(status_code=403, detail="Administrator access is required.")
    import hashlib

    document_id = hashlib.sha256(
        f"{document.source}\0{document.title}".encode("utf-8")
    ).hexdigest()
    await index_document(
        document_id, document.title, document.source, document.content
    )
    record_audit(
        actor["user_id"],
        "knowledge_document_updated",
        document_id,
        {"title": document.title, "source": document.source},
    )
    return {"document_id": document_id, "title": document.title}


@app.delete("/knowledge/documents/{document_id}")
async def remove_knowledge_document(
    document_id: str,
    actor: dict[str, Any] = Depends(require_csrf),
) -> dict[str, str]:
    if actor["role"] != "admin":
        raise HTTPException(status_code=403, detail="Administrator access is required.")
    if document_id == "brand-rules":
        raise HTTPException(
            status_code=400, detail="The configured brand-rules document cannot be removed."
        )
    if not delete_document(document_id):
        raise HTTPException(status_code=404, detail="Knowledge document not found.")
    record_audit(actor["user_id"], "knowledge_document_removed", document_id)
    return {"document_id": document_id, "status": "removed"}


@app.get("/admin/users")
async def admin_users(
    _: dict[str, Any] = Depends(require_admin),
) -> list[dict[str, Any]]:
    return list_users()


@app.post("/admin/users", status_code=201)
async def admin_create_user(
    request: AdminUserRequest,
    actor: dict[str, Any] = Depends(require_csrf),
) -> dict[str, Any]:
    if actor["role"] != "admin":
        raise HTTPException(status_code=403, detail="Administrator access is required.")
    if request.role not in {"admin", "reviewer", "creator"}:
        raise HTTPException(status_code=422, detail="Role must be admin, reviewer or creator.")
    email = request.email.strip().lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise HTTPException(status_code=422, detail="Enter a valid email address.")
    try:
        password_hash = hash_password(request.password)
        user_id = create_user(email, password_hash, request.role)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except sqlite3.IntegrityError as exc:
        raise HTTPException(status_code=409, detail="An account with this email already exists.") from exc
    record_audit(actor["user_id"], "user_created", user_id, {"role": request.role})
    return {"user_id": user_id, "email": email, "role": request.role}


@app.patch("/admin/users/{user_id}")
async def admin_update_user(
    user_id: str,
    update: UserUpdate,
    actor: dict[str, Any] = Depends(require_csrf),
) -> dict[str, str]:
    if actor["role"] != "admin":
        raise HTTPException(status_code=403, detail="Administrator access is required.")
    if update.role is None and update.is_active is None:
        raise HTTPException(status_code=422, detail="Provide a role or active-state update.")
    target = get_user(user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found.")
    if target["role"] == "admin" and (
        update.role not in (None, "admin") or update.is_active is False
    ):
        active_admins = sum(
            user["role"] == "admin" and user["is_active"] for user in list_users()
        )
        if active_admins <= 1:
            raise HTTPException(status_code=409, detail="Cannot disable or demote the last active administrator.")
    if not update_user(user_id, update.role, update.is_active):
        raise HTTPException(status_code=404, detail="User could not be updated.")
    record_audit(actor["user_id"], "user_permissions_updated", user_id, update.model_dump())
    return {"user_id": user_id, "status": "updated"}


@app.get("/admin/brand-rules")
async def admin_get_brand_rules(
    _: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    return load_brand_rules()


@app.put("/admin/brand-rules")
async def admin_update_brand_rules(
    rules: dict[str, Any],
    actor: dict[str, Any] = Depends(require_csrf),
) -> dict[str, str]:
    if actor["role"] != "admin":
        raise HTTPException(status_code=403, detail="Administrator access is required.")
    if len(json.dumps(rules, ensure_ascii=False)) > MAX_TEXT_SIZE:
        raise HTTPException(status_code=413, detail="Brand rules exceed the 100 KB limit.")
    if not isinstance(rules.get("brand_name"), str) or not rules["brand_name"].strip():
        raise HTTPException(status_code=422, detail="Brand rules require a brand_name.")
    rules_path = RULES_PATH
    temporary_path = rules_path.with_suffix(".json.tmp")
    temporary_path.write_text(
        json.dumps(rules, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary_path.replace(rules_path)
    await index_document(
        "brand-rules",
        rules["brand_name"],
        "knowledge/brand_rules.json",
        json.dumps(rules, ensure_ascii=False, indent=2),
    )
    record_audit(actor["user_id"], "brand_rules_updated", "brand-rules")
    return {"status": "updated"}


@app.get("/admin/visual")
async def admin_visual_settings(
    _: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    return {
        "settings": get_setting("visual_settings"),
        "assets": list_visual_assets(),
    }


@app.put("/admin/visual/settings")
async def admin_update_visual_settings(
    settings: VisualSettings,
    actor: dict[str, Any] = Depends(require_csrf),
) -> dict[str, str]:
    if actor["role"] != "admin":
        raise HTTPException(status_code=403, detail="Administrator access is required.")
    for colour in settings.approved_colours:
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", colour):
            raise HTTPException(status_code=422, detail=f"Invalid hex colour: {colour}")
    allowed_thresholds = {
        "logo_similarity": (0.0, 1.0),
        "colour_delta_e": (0.0, 441.7),
        "unapproved_colour_fraction": (0.0, 1.0),
        "minimum_wcag_contrast": (1.0, 21.0),
    }
    for name, value in settings.thresholds.items():
        if name not in allowed_thresholds:
            raise HTTPException(status_code=422, detail=f"Unsupported threshold: {name}")
        minimum, maximum = allowed_thresholds[name]
        if not minimum <= value <= maximum:
            raise HTTPException(status_code=422, detail=f"Threshold {name} is out of range.")
    for pair in settings.contrast_pairs:
        for colour_key in ("foreground", "background"):
            if not re.fullmatch(r"#[0-9a-fA-F]{6}", pair.get(colour_key, "")):
                raise HTTPException(status_code=422, detail="Contrast pairs require valid foreground and background hex colours.")
        try:
            minimum_ratio = float(
                pair.get(
                    "minimum_ratio",
                    settings.thresholds.get("minimum_wcag_contrast", 4.5),
                )
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail="Contrast minimum_ratio must be a number from 1 to 21.",
            ) from exc
        if not 1 <= minimum_ratio <= 21:
            raise HTTPException(
                status_code=422,
                detail="Contrast minimum_ratio must be between 1 and 21.",
            )
    set_setting("visual_settings", settings.model_dump())
    record_audit(actor["user_id"], "visual_settings_updated", "visual-settings")
    return {"status": "updated"}


@app.post("/admin/visual/assets", status_code=201)
async def admin_upload_visual_asset(
    name: str = Form(min_length=1, max_length=160),
    asset_type: str = Form(),
    file: UploadFile = File(...),
    actor: dict[str, Any] = Depends(require_csrf),
) -> dict[str, str]:
    if actor["role"] != "admin":
        raise HTTPException(status_code=403, detail="Administrator access is required.")
    if asset_type not in {"logo", "reference"}:
        raise HTTPException(status_code=422, detail="Asset type must be logo or reference.")
    data = await file.read(5 * 1024 * 1024 + 1)
    await file.close()
    if not data or len(data) > 5 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Visual assets must be under 5 MB.")
    _scan_upload(data)
    filename = _safe_filename(file.filename)
    mime_type, kind = _sniff_upload(data, filename)
    if kind != "image":
        raise HTTPException(status_code=400, detail="Approved visual assets must be PNG, JPEG, GIF or WebP images.")
    try:
        decode_image(data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    asset_id = save_visual_asset(name.strip(), asset_type, mime_type, data, actor["user_id"])
    return {"asset_id": asset_id, "name": name.strip()}


@app.delete("/admin/visual/assets/{asset_id}")
async def admin_remove_visual_asset(
    asset_id: str,
    actor: dict[str, Any] = Depends(require_csrf),
) -> dict[str, str]:
    if actor["role"] != "admin":
        raise HTTPException(status_code=403, detail="Administrator access is required.")
    if not delete_visual_asset(asset_id, actor["user_id"]):
        raise HTTPException(status_code=404, detail="Visual asset not found.")
    return {"asset_id": asset_id, "status": "removed"}


@app.post("/admin/visual/guidelines", status_code=201)
async def admin_upload_visual_guidelines(
    file: UploadFile = File(...),
    actor: dict[str, Any] = Depends(require_csrf),
) -> dict[str, str]:
    if actor["role"] != "admin":
        raise HTTPException(status_code=403, detail="Administrator access is required.")
    filename = _safe_filename(file.filename)
    data = await file.read(MAX_UPLOAD_SIZE + 1)
    await file.close()
    if not data or len(data) > MAX_UPLOAD_SIZE:
        raise HTTPException(status_code=413, detail="Guideline documents are limited to 10 MB.")
    _scan_upload(data)
    _, kind = _sniff_upload(data, filename)
    if kind not in {"pdf", "text"}:
        raise HTTPException(status_code=400, detail="Upload a PDF or UTF-8 text guideline document.")
    text = await _extract_document(data, kind)
    document_id = hashlib.sha256(
        f"visual-guideline\0{filename}".encode("utf-8")
    ).hexdigest()
    await index_document(
        document_id, filename, f"visual-guideline/{filename}", text
    )
    record_audit(actor["user_id"], "visual_guidelines_uploaded", document_id)
    return {"document_id": document_id, "title": filename}


@app.get("/admin/audit/verify")
async def admin_verify_audit(
    _: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    return verify_audit_chain()


@app.post("/admin/retention/purge")
async def admin_purge_retention(
    actor: dict[str, Any] = Depends(require_csrf),
) -> dict[str, int]:
    if actor["role"] != "admin":
        raise HTTPException(status_code=403, detail="Administrator access is required.")
    retention_days = int(os.getenv("RETENTION_DAYS", "365"))
    deleted = purge_reviews_older_than(retention_days)
    record_audit(actor["user_id"], "manual_retention_purge", "retention", {"deleted": deleted})
    return {"deleted_reviews": deleted, "retention_days": retention_days}


@app.get("/admin/evaluation/latest")
async def admin_latest_evaluation(
    _: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    report_path = PROJECT_ROOT / "evaluation" / "results.jsonl"
    if not report_path.exists():
        return {"available": False, "detail": "No live evaluation report has been saved yet."}
    lines = report_path.read_text(encoding="utf-8").splitlines()
    if not lines:
        return {"available": False, "detail": "The evaluation report file is empty."}
    return {"available": True, "report": json.loads(lines[-1])}
