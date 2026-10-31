import asyncio
import io
import json
import sqlite3
import ssl
from datetime import datetime, timedelta, timezone
import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from backend import main, retrieval, storage, reviewer
from backend.security import hash_password, verify_password
from backend.visual import analyze_visual_content, logo_similarity, wcag_contrast_ratio
from backend.reviewer import (
    ClaimCheck,
    ReviewIssue,
    ReviewResult,
    ReviewStatus,
    Severity,
    VerificationOutcome,
    _finalize_result,
)
from evaluation import run as evaluation_run


def test_upload_content_is_checked_not_browser_mime_type():
    assert main._sniff_upload(b"\x89PNG\r\n\x1a\nimage", "creative.png") == (
        "image/png",
        "image",
    )
    with pytest.raises(main.HTTPException) as error:
        main._sniff_upload(b"<svg></svg>", "creative.png")
    assert error.value.status_code == 400


def test_filename_discards_paths_and_control_characters():
    assert main._safe_filename("..\\..\\campaign\\x\x00.png") == "x.png"


def test_chunking_preserves_document_content():
    content = "First paragraph.\n\n" + ("brand guidance " * 120)
    chunks = retrieval.split_into_chunks(content, max_chars=90)
    assert chunks
    assert "".join(chunks).replace("\n", "").replace(" ", "") == (
        content.replace("\n", "").replace(" ", "")
    )
    assert all(len(chunk) <= 90 for chunk in chunks)


def test_local_retrieval_returns_relevant_source(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DATABASE_PATH", tmp_path / "retrieval.sqlite3")
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.setenv("EMBEDDING_PROVIDER", "local")
    asyncio.run(
        retrieval.index_document(
            "doc-1",
            "Approved product guide",
            "sample-guide.txt",
            "The Moss bottle capacity is 500 mL.",
        )
    )
    result = asyncio.run(retrieval.retrieve_evidence("Moss bottle capacity"))
    assert result["mode"] == "lexical_fallback"
    assert result["evidence"][0]["title"] == "Approved product guide"


def test_vercel_database_defaults_to_writable_temporary_directory(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.delenv("FUTURESTACK_DATABASE", raising=False)
    assert storage._default_database_path().as_posix() == "/tmp/futurestack.sqlite3"

    configured_path = tmp_path / "configured.sqlite3"
    monkeypatch.setenv("FUTURESTACK_DATABASE", str(configured_path))
    assert storage._default_database_path() == configured_path


def test_review_history_decision_and_dashboard_are_persistent(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DATABASE_PATH", tmp_path / "reviews.sqlite3")
    storage.initialize_database()
    review_id = storage.save_review(
        "campaign.txt",
        "text/plain",
        "Sample campaign",
        "Sample campaign",
        {
            "review_id": "",
            "status": "needs_changes",
            "severity": "high",
            "issues": [],
        },
    )
    reviewer_id = storage.create_user(
        "reviewer@example.test", hash_password("safe-reviewer-password"), "reviewer"
    )
    assert storage.update_decision(
        review_id,
        "changes_requested",
        reviewer_id,
        "reviewer@example.test",
        "Check claim",
    )
    record = storage.get_review(review_id)
    assert record is not None
    assert record["decision"] == "changes_requested"
    assert record["reviewer"] == "reviewer@example.test"
    assert storage.dashboard_counts() == {
        "total": 1,
        "pending": 0,
        "approved": 0,
        "changes_requested": 1,
        "rejected": 0,
        "high_risk": 1,
    }
    actions = [event["action"] for event in storage.list_audit_events()]
    assert actions[0] == "changes_requested"
    assert {"review_completed", "content_submitted", "user_created"}.issubset(actions)


def _register_and_login(client, email, password):
    registration = client.post(
        "/auth/register", json={"email": email, "password": password}
    )
    assert registration.status_code == 201
    login = client.post("/auth/login", json={"email": email, "password": password})
    assert login.status_code == 200
    return login.json()["csrf_token"], registration.json()["user_id"]


def test_password_hashing_and_individual_session_authentication(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DATABASE_PATH", tmp_path / "accounts.sqlite3")
    password = "correct-horse-battery-staple"
    password_hash = hash_password(password)
    assert password_hash != password
    assert verify_password(password, password_hash)
    assert not verify_password("not-the-password", password_hash)
    with pytest.raises(ValueError):
        hash_password("short")

    with TestClient(main.app) as client:
        response = client.post(
            "/auth/register",
            json={"email": "creator@example.test", "password": password},
        )
        assert response.status_code == 201
        assert response.json()["role"] == "creator"
        login = client.post(
            "/auth/login",
            json={"email": "creator@example.test", "password": password},
        )
        assert login.status_code == 200
        assert "httponly" in login.headers["set-cookie"].lower()
        assert client.get("/auth/me").status_code == 200
        assert client.get("/admin/users").status_code == 403
        assert client.post("/auth/logout").status_code == 403
        csrf = client.get("/auth/me").json()["csrf_token"]
        logout = client.post("/auth/logout", headers={"X-CSRF-Token": csrf})
        assert logout.status_code == 200
        assert client.get("/auth/me").status_code == 401


def test_pdf_content_sniffing_uses_file_signature():
    assert main._sniff_upload(b"%PDF-1.7\n", "notes.pdf") == (
        "application/pdf",
        "pdf",
    )


def test_text_submission_uses_shared_review_engine(monkeypatch, tmp_path):
    monkeypatch.setattr(storage, "DATABASE_PATH", tmp_path / "text-review.sqlite3")
    async def fake_create_review(
        text, content_type, filename, image_bytes=None, owner_id=None, parent_review_id=None
    ):
        return {
            "filename": filename,
            "extracted_text": text,
            "review": {"status": "approved"},
            "retrieval": {"mode": "lexical_fallback", "sources": []},
            "owner_id": owner_id,
        }

    monkeypatch.setattr(main, "_create_review", fake_create_review)
    with TestClient(main.app) as client:
        csrf, user_id = _register_and_login(
            client, "copywriter@example.test", "a-safe-account-password"
        )
        response = client.post(
            "/review",
            data={"text": "An on-brand sample.", "parent_review_id": ""},
            headers={"X-CSRF-Token": csrf},
        )
        assert response.status_code == 200
        assert response.json()["extracted_text"] == "An on-brand sample."
        assert response.json()["owner_id"] == user_id


def test_text_submission_is_available_without_login(monkeypatch, tmp_path):
    monkeypatch.setattr(storage, "DATABASE_PATH", tmp_path / "public-review.sqlite3")

    async def fake_create_review(
        text, content_type, filename, image_bytes=None, owner_id=None, parent_review_id=None
    ):
        return {
            "filename": filename,
            "extracted_text": text,
            "review": {"status": "approved"},
            "owner_id": owner_id,
        }

    monkeypatch.setattr(main, "_create_review", fake_create_review)
    with TestClient(main.app) as client:
        response = client.post("/review", data={"text": "Public sample."})
        revision = client.post(
            "/review",
            data={"text": "Attempted revision.", "parent_review_id": "another-review"},
        )

    assert response.status_code == 200
    assert response.json()["extracted_text"] == "Public sample."
    assert response.json()["owner_id"] is None
    assert revision.status_code == 401


def test_document_submission_validates_real_file_content(monkeypatch, tmp_path):
    monkeypatch.setattr(storage, "DATABASE_PATH", tmp_path / "document-review.sqlite3")
    async def fake_create_review(
        text, content_type, filename, image_bytes=None, owner_id=None, parent_review_id=None
    ):
        return {"filename": filename, "extracted_text": text}

    monkeypatch.setattr(main, "_create_review", fake_create_review)
    with TestClient(main.app) as client:
        csrf, _ = _register_and_login(
            client, "documenter@example.test", "a-safe-document-password"
        )
        response = client.post(
            "/review",
            data={"text": "", "parent_review_id": ""},
            files={"file": ("brief.txt", b"Approved product wording.", "application/pdf")},
            headers={"X-CSRF-Token": csrf},
        )
        assert response.status_code == 200
        assert response.json()["extracted_text"] == "Approved product wording."

        invalid = client.post(
            "/review",
            files={"file": ("forged.png", b"<script>", "image/png")},
            headers={"X-CSRF-Token": csrf},
        )
        assert invalid.status_code == 400


def test_review_decision_requires_reviewer_access_and_records_action(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(storage, "DATABASE_PATH", tmp_path / "decisions.sqlite3")
    storage.initialize_database()
    creator_id = storage.create_user(
        "owner@example.test", hash_password("owner-password-long"), "creator"
    )
    reviewer_id = storage.create_user(
        "reviewer@example.test", hash_password("reviewer-password-long"), "reviewer"
    )
    review_id = storage.save_review(
        "ad.txt",
        "text/plain",
        "An ad",
        "An ad",
        {"review_id": "", "status": "approved", "severity": "low", "issues": []},
        owner_id=creator_id,
    )
    with TestClient(main.app) as creator_client, TestClient(main.app) as reviewer_client:
        creator_login = creator_client.post(
            "/auth/login",
            json={"email": "owner@example.test", "password": "owner-password-long"},
        ).json()
        reviewer_login = reviewer_client.post(
            "/auth/login",
            json={"email": "reviewer@example.test", "password": "reviewer-password-long"},
        ).json()
        denied = creator_client.post(
            f"/reviews/{review_id}/decision",
            headers={"X-CSRF-Token": creator_login["csrf_token"]},
            json={"decision": "approved"},
        )
        assert denied.status_code == 403
        response = reviewer_client.post(
            f"/reviews/{review_id}/decision",
            headers={"X-CSRF-Token": reviewer_login["csrf_token"]},
            json={"decision": "approved"},
        )
        assert response.status_code == 200
        saved = storage.get_review(review_id)
        assert saved["decision"] == "approved"
        assert saved["reviewer"] == "reviewer@example.test"


def test_application_startup_seeds_rules_and_health_status(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(storage, "DATABASE_PATH", tmp_path / "startup.sqlite3")
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("OCR_API_KEY", raising=False)
    monkeypatch.setenv("EMBEDDING_PROVIDER", "local")
    with TestClient(main.app) as client:
        health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["status"] == "healthy"
    assert any(document["document_id"] == "brand-rules" for document in retrieval.list_documents())


def test_malformed_pdf_is_rejected_without_processing(tmp_path):
    with pytest.raises(main.HTTPException) as error:
        asyncio.run(main._extract_document(b"%PDF-1.7\nnot a real PDF", "pdf"))
    assert error.value.status_code == 400


def test_review_result_deduplicates_issues_and_computes_qualitative_risk():
    issue = ReviewIssue(
        type="incorrect_product_information",
        text="750 mL",
        severity=Severity.high,
        reason="The guide lists 500 mL.",
        suggestion="Correct the amount.",
    )
    result = ReviewResult(
        status=ReviewStatus.approved,
        issues=[issue, issue],
        severity=Severity.low,
        reason="Review",
        suggestion="Check findings.",
    )
    finalized = _finalize_result(result, "lexical_fallback")
    assert len(finalized.issues) == 1
    assert finalized.status == ReviewStatus.needs_changes
    assert finalized.severity == Severity.high
    assert finalized.risk_score == 85


def test_unverified_claim_cannot_be_returned_as_compliant():
    result = ReviewResult(
        status=ReviewStatus.approved,
        issues=[],
        severity=Severity.low,
        reason="No direct issue.",
        suggestion="Verify the claim.",
        claim_checks=[
            ClaimCheck(
                claim="A claim with no supporting reference",
                outcome=VerificationOutcome.insufficient_evidence,
                explanation="The knowledge base has no relevant source.",
            )
        ],
    )
    finalized = _finalize_result(result, "lexical_fallback")
    assert finalized.status == ReviewStatus.needs_changes
    assert finalized.severity == Severity.medium
    assert finalized.risk_score == 55


def test_gemini_interactions_request_parses_structured_review(monkeypatch):
    review_data = {
        "status": "needs_changes",
        "issues": [],
        "severity": "low",
        "reason": "The claim needs verification.",
        "suggestion": "Check the approved guide.",
        "claim_checks": [
            {
                "claim": "A factual claim",
                "outcome": "insufficient_evidence",
                "explanation": "No supporting reference was retrieved.",
            }
        ],
    }
    interactions_response = httpx.Response(
        200,
        json={
            "steps": [
                {
                    "type": "model_output",
                    "content": [{"type": "text", "text": json.dumps(review_data)}],
                }
            ]
        },
        request=httpx.Request("POST", "https://generativelanguage.googleapis.com"),
    )
    requests = []

    class MockAsyncClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, **kwargs):
            requests.append((url, kwargs))
            return interactions_response

    monkeypatch.setenv("LLM_API_KEY", "unit-test-secret")
    monkeypatch.setenv("LLM_MODEL", "gemini-3.8-flash")
    monkeypatch.setattr(reviewer.httpx, "AsyncClient", MockAsyncClient)

    result = asyncio.run(reviewer.review_text("A factual claim", {}, []))

    url, request = requests[0]
    assert url.endswith("/v1beta/interactions")
    assert request["headers"]["x-goog-api-key"] == "unit-test-secret"
    assert request["json"]["model"] == "gemini-3.8-flash"
    assert request["json"]["store"] is False
    assert request["json"]["response_format"]["mime_type"] == "application/json"
    assert request["json"]["response_format"]["schema"]["type"] == "object"
    assert result.claim_checks[0].outcome == VerificationOutcome.insufficient_evidence


def test_gemini_interactions_uploads_images_using_files_api(monkeypatch):
    review_data = {
        "status": "approved",
        "issues": [],
        "severity": "low",
        "reason": "No issues found.",
        "suggestion": "Proceed to human review.",
    }
    request = httpx.Request("POST", "https://generativelanguage.googleapis.com")
    responses = [
        httpx.Response(
            200,
            headers={"x-goog-upload-url": "https://upload.google.test/session"},
            request=request,
        ),
        httpx.Response(
            200,
            json={
                "file": {
                    "uri": "https://generativelanguage.googleapis.com/files/test-image",
                    "mimeType": "image/png",
                }
            },
            request=request,
        ),
        httpx.Response(
            200,
            json={
                "steps": [
                    {
                        "type": "model_output",
                        "content": [{"type": "text", "text": json.dumps(review_data)}],
                    }
                ]
            },
            request=request,
        ),
    ]
    requests = []

    class MockAsyncClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, **kwargs):
            requests.append((url, kwargs))
            return responses.pop(0)

    monkeypatch.setenv("LLM_API_KEY", "unit-test-secret")
    monkeypatch.setattr(reviewer.httpx, "AsyncClient", MockAsyncClient)

    asyncio.run(
        reviewer.review_text(
            "Image review",
            {},
            image_bytes=b"image-bytes",
            image_type="image/png",
        )
    )

    assert requests[0][0].endswith("/upload/v1beta/files")
    assert requests[1][0] == "https://upload.google.test/session"
    assert requests[1][1]["content"] == b"image-bytes"
    interaction = requests[2][1]["json"]
    assert interaction["input"][1] == {
        "type": "image",
        "uri": "https://generativelanguage.googleapis.com/files/test-image",
        "mime_type": "image/png",
    }


def test_gemini_interactions_error_redacts_api_key(monkeypatch):
    api_key = "private-unit-test-key"
    error_response = httpx.Response(
        403,
        json={"error": {"message": f"Rejected credential {api_key}"}},
        request=httpx.Request("POST", "https://generativelanguage.googleapis.com"),
    )

    class MockAsyncClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, _url, **_kwargs):
            return error_response

    monkeypatch.setenv("LLM_API_KEY", api_key)
    monkeypatch.setattr(reviewer.httpx, "AsyncClient", MockAsyncClient)

    with pytest.raises(reviewer.ReviewServiceError) as error:
        asyncio.run(reviewer.review_text("Review this", {}, []))
    assert api_key not in str(error.value)
    assert "[redacted]" in str(error.value)


def test_gemini_retries_transient_status_and_respects_retry_after(monkeypatch):
    review_data = {
        "status": "approved",
        "issues": [],
        "severity": "low",
        "reason": "No issues found.",
        "suggestion": "Proceed to human review.",
    }
    request = httpx.Request("POST", "https://generativelanguage.googleapis.com")
    responses = [
        httpx.Response(503, headers={"Retry-After": "2"}, request=request),
        httpx.Response(
            200,
            json={
                "steps": [
                    {
                        "type": "model_output",
                        "content": [{"type": "text", "text": json.dumps(review_data)}],
                    }
                ]
            },
            request=request,
        ),
    ]
    calls = []
    delays = []

    class MockAsyncClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, **_kwargs):
            calls.append(url)
            return responses.pop(0)

    async def record_sleep(delay):
        delays.append(delay)

    monkeypatch.setenv("LLM_API_KEY", "unit-test-secret")
    monkeypatch.setattr(reviewer.httpx, "AsyncClient", MockAsyncClient)
    monkeypatch.setattr(reviewer.asyncio, "sleep", record_sleep)

    result = asyncio.run(reviewer.review_text("Synthetic review", {}, []))

    assert result.status == ReviewStatus.approved
    assert len(calls) == 2
    assert delays == [2.0]


def test_gemini_does_not_retry_permanent_http_errors(monkeypatch):
    request = httpx.Request("POST", "https://generativelanguage.googleapis.com")
    response = httpx.Response(
        401,
        json={"error": {"message": "Invalid credential"}},
        request=request,
    )
    calls = []

    class MockAsyncClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, _url, **_kwargs):
            calls.append(1)
            return response

    monkeypatch.setenv("LLM_API_KEY", "unit-test-secret")
    monkeypatch.setattr(reviewer.httpx, "AsyncClient", MockAsyncClient)

    with pytest.raises(reviewer.ReviewServiceError, match="HTTP 401"):
        asyncio.run(reviewer.review_text("Synthetic review", {}, []))
    assert calls == [1]


def test_gemini_read_timeout_is_configurable_without_changing_other_timeouts(
    monkeypatch,
):
    monkeypatch.delenv("GEMINI_READ_TIMEOUT_SECONDS", raising=False)
    default_timeout = reviewer._gemini_timeout()
    assert default_timeout.connect == 90
    assert default_timeout.read == 180
    assert default_timeout.write == 90
    assert default_timeout.pool == 90

    monkeypatch.setenv("GEMINI_READ_TIMEOUT_SECONDS", "240")
    configured_timeout = reviewer._gemini_timeout()
    assert configured_timeout.connect == 90
    assert configured_timeout.read == 240
    assert configured_timeout.write == 90
    assert configured_timeout.pool == 90


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "not-a-number"])
def test_gemini_read_timeout_rejects_invalid_configuration(monkeypatch, value):
    monkeypatch.setenv("GEMINI_READ_TIMEOUT_SECONDS", value)
    with pytest.raises(
        reviewer.ReviewServiceError,
        match="GEMINI_READ_TIMEOUT_SECONDS must be a positive number.",
    ):
        reviewer._gemini_timeout()


@pytest.mark.parametrize(
    ("transport_error", "category", "cause"),
    [
        (
            httpx.ReadTimeout("sensitive timeout detail"),
            "read_timeout",
            ssl.SSLWantReadError("sensitive TLS wait detail"),
        ),
        (httpx.ConnectError("sensitive connection detail"), "connection", None),
        (
            httpx.ConnectError("sensitive TLS detail"),
            "tls",
            ssl.SSLCertVerificationError("sensitive certificate detail"),
        ),
    ],
)
def test_gemini_transport_errors_are_logged_safely(
    monkeypatch, caplog, transport_error, category, cause
):
    class MockAsyncClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, _url, **_kwargs):
            if cause is not None:
                raise transport_error from cause
            raise transport_error

    monkeypatch.setenv("LLM_API_KEY", "never-log-this-api-key")
    monkeypatch.setattr(reviewer.httpx, "AsyncClient", MockAsyncClient)

    with pytest.raises(reviewer.ReviewServiceError) as error:
        asyncio.run(reviewer.review_text("private submitted content", {}, []))

    assert str(error.value) == "Could not reach the Gemini provider."
    assert error.value.__cause__ is transport_error
    log_text = caplog.text
    assert f"category={category}" in log_text
    assert type(transport_error).__name__ in log_text
    assert "never-log-this-api-key" not in log_text
    assert "private submitted content" not in log_text
    assert "sensitive" not in log_text


def test_creator_ownership_and_complete_revision_decision_lifecycle(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(storage, "DATABASE_PATH", tmp_path / "revision-lifecycle.sqlite3")
    storage.initialize_database()

    async def persist_fake_review(
        text, content_type, filename, image_bytes=None, owner_id=None, parent_review_id=None
    ):
        review = {
            "status": "approved",
            "severity": "low",
            "reason": "No automated issue was found.",
            "issues": [],
            "evidence": [],
            "claim_checks": [],
        }
        review_id = storage.save_review(
            filename, content_type, text, text, review, owner_id, parent_review_id
        )
        return {
            "review_id": review_id,
            "filename": filename,
            "extracted_text": text,
            "review": review,
        }

    monkeypatch.setattr(main, "_create_review", persist_fake_review)
    with (
        TestClient(main.app) as owner_client,
        TestClient(main.app) as other_client,
        TestClient(main.app) as reviewer_client,
    ):
        owner_csrf, owner_id = _register_and_login(
            owner_client, "owner@example.test", "a-safe-owner-password"
        )
        other_csrf, _ = _register_and_login(
            other_client, "other@example.test", "a-safe-other-password"
        )
        reviewer_id = storage.create_user(
            "workflow-reviewer@example.test",
            hash_password("a-safe-reviewer-password"),
            "reviewer",
        )
        reviewer_login = reviewer_client.post(
            "/auth/login",
            json={
                "email": "workflow-reviewer@example.test",
                "password": "a-safe-reviewer-password",
            },
        ).json()

        submitted = owner_client.post(
            "/review",
            data={"text": "Draft copy", "parent_review_id": ""},
            headers={"X-CSRF-Token": owner_csrf},
        )
        assert submitted.status_code == 200
        original_id = submitted.json()["review_id"]
        assert storage.get_review(original_id)["decision"] == "pending"

        assert other_client.get(f"/my/submissions/{original_id}").status_code == 404
        assert other_client.get("/my/submissions").json() == []
        forged_revision = other_client.post(
            "/review",
            data={"text": "Not the owner's copy", "parent_review_id": original_id},
            headers={"X-CSRF-Token": other_csrf},
        )
        assert forged_revision.status_code == 403

        changes_requested = reviewer_client.post(
            f"/reviews/{original_id}/decision",
            headers={"X-CSRF-Token": reviewer_login["csrf_token"]},
            json={"decision": "changes_requested", "reason": "Correct the claim."},
        )
        assert changes_requested.status_code == 200
        resubmitted = owner_client.post(
            "/review",
            data={"text": "Corrected copy", "parent_review_id": original_id},
            headers={"X-CSRF-Token": owner_csrf},
        )
        assert resubmitted.status_code == 200
        revision_id = resubmitted.json()["review_id"]

        revision = storage.get_review(revision_id)
        assert revision["revision_number"] == 2
        assert revision["revision_history"][0]["submitted_text"] == "Draft copy"
        assert revision["revision_history"][1]["submitted_text"] == "Corrected copy"
        assert revision["revision_history"][0]["reviewer"] == "workflow-reviewer@example.test"
        assert revision["revision_history"][0]["decided_at"]
        assert revision["decision"] == "pending"

        approved = reviewer_client.post(
            f"/reviews/{revision_id}/decision",
            headers={"X-CSRF-Token": reviewer_login["csrf_token"]},
            json={"decision": "approved", "reason": ""},
        )
        assert approved.status_code == 200
        assert storage.get_review(revision_id)["decision"] == "approved"
        assert storage.dashboard_counts(owner_id)["approved"] == 1
        assert storage.verify_audit_chain()["valid"]


def test_visual_palette_logo_and_wcag_measurements():
    fixture_dir = evaluation_run.FIXTURE_DIR
    approved_logo = (fixture_dir / "approved_logo.png").read_bytes()
    compliant = (fixture_dir / "compliant_creative.png").read_bytes()
    wrong_palette = (fixture_dir / "wrong_palette.png").read_bytes()
    misplaced_logo = (fixture_dir / "misplaced_logo.png").read_bytes()
    similar = analyze_visual_content(
        compliant,
        approved_colours=["#174d38", "#ffffff"],
        approved_logos=[approved_logo],
    )
    different = analyze_visual_content(
        wrong_palette,
        approved_colours=["#174d38", "#ffffff"],
        approved_logos=[approved_logo],
    )
    assert similar["logo_check"]["similarity"] >= 0.72
    assert different["logo_check"]["similarity"] < 0.72
    assert different["colour_check"]["out_of_palette_fraction"] > 0.55
    placement = analyze_visual_content(
        misplaced_logo,
        approved_colours=["#174d38", "#ffffff"],
        approved_logos=[approved_logo],
        expected_logo_zone="bottom_right",
    )
    assert placement["logo_check"]["similarity"] >= 0.72
    assert placement["logo_check"]["detected_zone"] == "top_left"
    assert placement["logo_check"]["findings"][0]["type"] == "logo_placement_violation"
    assert wcag_contrast_ratio("#000000", "#ffffff") == 21.0
    assert wcag_contrast_ratio("#a7cfb1", "#ffffff") < 4.5


def test_audit_hash_chain_detects_record_tampering(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DATABASE_PATH", tmp_path / "audit-chain.sqlite3")
    monkeypatch.setenv("AUDIT_HMAC_KEY", "unit-test-audit-key-with-sufficient-length")
    storage.initialize_database()
    storage.record_audit("test-user", "test_event", "entity-1", {"value": "original"})
    assert storage.verify_audit_chain() == {"valid": True, "events_checked": 1}
    with sqlite3.connect(storage.DATABASE_PATH) as connection:
        connection.execute(
            "UPDATE audit_log SET details_json = ?",
            (json.dumps({"value": "tampered"}),),
        )
    result = storage.verify_audit_chain()
    assert result["valid"] is False
    assert result["reason"] == "event_hash_mismatch"


def test_retention_and_user_deletion_remove_review_content_not_audit(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(storage, "DATABASE_PATH", tmp_path / "retention.sqlite3")
    storage.initialize_database()
    owner_id = storage.create_user(
        "retention@example.test", hash_password("retention-owner-password"), "creator"
    )
    review_id = storage.save_review(
        "ad.txt",
        "text/plain",
        "Sensitive campaign copy",
        "Sensitive campaign copy",
        {"status": "needs_changes", "severity": "medium", "issues": []},
        owner_id=owner_id,
    )
    with sqlite3.connect(storage.DATABASE_PATH) as connection:
        old_date = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()
        connection.execute(
            "UPDATE reviews SET created_at = ? WHERE review_id = ?", (old_date, review_id)
        )
    assert storage.purge_reviews_older_than(365) == 1
    assert storage.get_review(review_id) is None
    assert any(event["action"] == "review_retention_purge" for event in storage.list_audit_events())
    assert storage.verify_audit_chain()["valid"]

    second_review = storage.save_review(
        "another.txt",
        "text/plain",
        "Delete this content",
        "Delete this content",
        {"status": "needs_changes", "severity": "low", "issues": []},
        owner_id=owner_id,
    )
    assert storage.delete_review_thread(second_review, owner_id, owner_id) == 1
    assert storage.get_review(second_review) is None
    assert any(event["action"] == "submission_content_deleted" for event in storage.list_audit_events())


def test_persistent_rate_limit_survives_database_reopens(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DATABASE_PATH", tmp_path / "rate-limit.sqlite3")
    storage.initialize_database()
    assert storage.check_rate_limit("submit", "192.0.2.1", limit=1)
    assert not storage.check_rate_limit("submit", "192.0.2.1", limit=1)
    assert not storage.check_rate_limit("submit", "192.0.2.1", limit=1)


def test_production_startup_rejects_missing_security_configuration(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(storage, "DATABASE_PATH", tmp_path / "production-check.sqlite3")
    monkeypatch.setattr(main, "ENVIRONMENT", "production")
    monkeypatch.setattr(main, "ALLOW_REGISTRATION", False)
    cases = [
        ("", "https://futurestack.example", "AUDIT_HMAC_KEY"),
        ("a" * 40, "", "CORS_ALLOWED_ORIGINS"),
        ("a" * 40, "*", "CORS_ALLOWED_ORIGINS"),
    ]
    for audit_key, origins, message in cases:
        monkeypatch.setenv("AUDIT_HMAC_KEY", audit_key)
        monkeypatch.setenv("CORS_ALLOWED_ORIGINS", origins)
        with pytest.raises(RuntimeError, match=message):
            with TestClient(main.app):
                pass


def test_versioned_migrations_upgrade_legacy_schema(tmp_path, monkeypatch):
    database_path = tmp_path / "legacy.sqlite3"
    monkeypatch.setattr(storage, "DATABASE_PATH", database_path)
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE reviews (
                review_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, filename TEXT NOT NULL,
                content_type TEXT NOT NULL, submitted_text TEXT NOT NULL,
                extracted_text TEXT NOT NULL, review_json TEXT NOT NULL,
                decision TEXT NOT NULL DEFAULT 'pending', reviewer TEXT,
                decision_reason TEXT, decided_at TEXT
            );
            CREATE TABLE knowledge_documents (
                document_id TEXT PRIMARY KEY, title TEXT NOT NULL, source TEXT NOT NULL,
                content TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE knowledge_chunks (
                chunk_id TEXT PRIMARY KEY, document_id TEXT NOT NULL,
                chunk_index INTEGER NOT NULL, content TEXT NOT NULL, embedding_json TEXT
            );
            CREATE TABLE audit_log (
                event_id TEXT PRIMARY KEY, occurred_at TEXT NOT NULL, actor TEXT NOT NULL,
                action TEXT NOT NULL, entity_id TEXT NOT NULL, details_json TEXT NOT NULL
            );
            INSERT INTO reviews VALUES (
                'legacy-review', '2020-01-01T00:00:00+00:00', 'old.txt', 'text/plain',
                'legacy content', 'legacy content', '{"severity":"low","issues":[]}',
                'pending', NULL, NULL, NULL
            );
            INSERT INTO audit_log VALUES (
                'legacy-event', '2020-01-01T00:00:00+00:00', 'system',
                'legacy_action', 'legacy-review', '{}'
            );
            """
        )
    storage.initialize_database()
    assert storage.migration_versions() == [1, 2]
    assert storage.get_review("legacy-review")["submitted_text"] == "legacy content"
    assert storage.verify_audit_chain() == {"valid": True, "events_checked": 1}
    assert storage.list_audit_events()[0]["action"] == "legacy_action"
    storage.initialize_database()
    assert storage.migration_versions() == [1, 2]


def test_evaluation_report_is_saved_without_fabricated_live_predictions(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.setenv("EMBEDDING_PROVIDER", "local")
    output_path = tmp_path / "evaluation-results.jsonl"
    report = asyncio.run(evaluation_run._run(output_path))
    assert output_path.is_file()
    assert len(output_path.read_text(encoding="utf-8").splitlines()) == 1
    assert report["live_model_metrics"]["status"] == "not_run"
    assert report["live_model_metrics"]["metrics"]["tested"] == 0
    assert report["live_model_metrics"]["metrics"]["accuracy"] is None
    assert report["visual_measurement_metrics"]["metrics"]["tested"] == 5
    assert report["retrieval_relevance"]["tested"] > 0
