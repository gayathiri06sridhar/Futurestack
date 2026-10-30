import hashlib
import hmac
import json
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import TracebackType
from typing import Any
from uuid import uuid4


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _default_database_path() -> Path:
    configured_path = os.getenv("FUTURESTACK_DATABASE", "").strip()
    if configured_path:
        return Path(configured_path)
    if os.getenv("VERCEL") == "1":
        return Path("/tmp/futurestack.sqlite3")
    return PROJECT_ROOT / "data" / "futurestack.sqlite3"


DATABASE_PATH = _default_database_path()


class _ClosingConnection(sqlite3.Connection):
    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def _connect() -> sqlite3.Connection:
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(
        DATABASE_PATH, timeout=30, factory=_ClosingConnection
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA secure_delete = ON")
    connection.execute("PRAGMA busy_timeout = 30000")
    return connection


def _migration_columns(
    connection: sqlite3.Connection, table: str, definitions: dict[str, str]
) -> None:
    existing = {
        row["name"] for row in connection.execute(f"PRAGMA table_info({table})")
    }
    for column, definition in definitions.items():
        if column not in existing:
            connection.execute(
                f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
            )


def _audit_key() -> bytes:
    configured = os.getenv("AUDIT_HMAC_KEY")
    if configured:
        return configured.encode("utf-8")
    key_path = DATABASE_PATH.with_name(DATABASE_PATH.name + ".audit-key")
    try:
        return key_path.read_bytes()
    except FileNotFoundError:
        key_path.parent.mkdir(parents=True, exist_ok=True)
        key = os.urandom(32)
        try:
            with key_path.open("xb") as key_file:
                key_file.write(key)
            os.chmod(key_path, 0o600)
        except FileExistsError:
            return key_path.read_bytes()
        return key


def _audit_payload(row: sqlite3.Row | dict[str, Any]) -> bytes:
    values = {
        "event_id": row["event_id"],
        "occurred_at": row["occurred_at"],
        "actor": row["actor"],
        "action": row["action"],
        "entity_id": row["entity_id"],
        "details_json": row["details_json"],
        "previous_hash": row["previous_hash"],
    }
    return json.dumps(values, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _audit_digest(payload: bytes) -> str:
    return hmac.new(_audit_key(), payload, hashlib.sha256).hexdigest()


def _append_audit(
    connection: sqlite3.Connection,
    actor: str,
    action: str,
    entity_id: str,
    details: dict[str, Any] | None = None,
    occurred_at: str | None = None,
) -> None:
    prior = connection.execute(
        "SELECT sequence, event_hash FROM audit_log ORDER BY sequence DESC LIMIT 1"
    ).fetchone()
    previous_hash = prior["event_hash"] if prior else ""
    sequence = prior["sequence"] + 1 if prior else 1
    row: dict[str, Any] = {
        "event_id": str(uuid4()),
        "occurred_at": occurred_at or datetime.now(timezone.utc).isoformat(),
        "actor": actor,
        "action": action,
        "entity_id": entity_id,
        "details_json": json.dumps(details or {}, ensure_ascii=False, sort_keys=True),
        "previous_hash": previous_hash,
    }
    connection.execute(
        """
        INSERT INTO audit_log
            (sequence, event_id, occurred_at, actor, action, entity_id, details_json,
             previous_hash, event_hash)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            sequence,
            row["event_id"],
            row["occurred_at"],
            actor,
            action,
            entity_id,
            row["details_json"],
            previous_hash,
            _audit_digest(_audit_payload(row)),
        ),
    )


def initialize_database() -> None:
    with _connect() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS reviews (
                review_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                filename TEXT NOT NULL,
                content_type TEXT NOT NULL,
                submitted_text TEXT NOT NULL,
                extracted_text TEXT NOT NULL,
                review_json TEXT NOT NULL,
                decision TEXT NOT NULL DEFAULT 'pending'
                    CHECK (decision IN ('pending', 'approved', 'changes_requested', 'rejected')),
                reviewer TEXT,
                decision_reason TEXT,
                decided_at TEXT
            );
            CREATE TABLE IF NOT EXISTS knowledge_documents (
                document_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                source TEXT NOT NULL,
                content TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS knowledge_chunks (
                chunk_id TEXT PRIMARY KEY,
                document_id TEXT NOT NULL REFERENCES knowledge_documents(document_id)
                    ON DELETE CASCADE,
                chunk_index INTEGER NOT NULL,
                content TEXT NOT NULL,
                embedding_json TEXT,
                UNIQUE(document_id, chunk_index)
            );
            CREATE TABLE IF NOT EXISTS audit_log (
                event_id TEXT PRIMARY KEY,
                occurred_at TEXT NOT NULL,
                actor TEXT NOT NULL,
                action TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                details_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS reviews_created_at_idx ON reviews(created_at);
            CREATE INDEX IF NOT EXISTS reviews_decision_idx ON reviews(decision);
            CREATE INDEX IF NOT EXISTS audit_occurred_at_idx ON audit_log(occurred_at);
            """
        )
        applied = {
            row["version"]
            for row in connection.execute("SELECT version FROM schema_migrations")
        }

        if 1 not in applied:
            _migration_columns(
                connection,
                "audit_log",
                {
                    "sequence": "INTEGER",
                    "previous_hash": "TEXT",
                    "event_hash": "TEXT",
                },
            )
            _rehash_legacy_events(connection)
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS audit_sequence_idx ON audit_log(sequence)"
            )
            connection.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (1, datetime.now(timezone.utc).isoformat()),
            )

        if 2 not in applied:
            connection.executescript(
                """
            CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY,
                email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL CHECK (role IN ('admin', 'reviewer', 'creator')),
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                token_hash TEXT NOT NULL UNIQUE,
                csrf_hash TEXT NOT NULL,
                user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS persistent_rate_limits (
                bucket TEXT NOT NULL,
                subject TEXT NOT NULL,
                window_start INTEGER NOT NULL,
                hits INTEGER NOT NULL,
                PRIMARY KEY(bucket, subject, window_start)
            );
            CREATE TABLE IF NOT EXISTS visual_assets (
                asset_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                asset_type TEXT NOT NULL CHECK (asset_type IN ('logo', 'reference')),
                mime_type TEXT NOT NULL,
                image_bytes BLOB NOT NULL,
                created_at TEXT NOT NULL,
                created_by TEXT REFERENCES users(user_id)
            );
            CREATE TABLE IF NOT EXISTS app_settings (
                setting_key TEXT PRIMARY KEY,
                setting_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
                """
            )
            _migration_columns(
                connection,
                "reviews",
                {
                    "owner_id": "TEXT REFERENCES users(user_id)",
                    "root_review_id": "TEXT",
                    "parent_review_id": "TEXT",
                    "revision_number": "INTEGER NOT NULL DEFAULT 1",
                },
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS reviews_owner_idx
                ON reviews(owner_id, created_at)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS reviews_root_idx
                ON reviews(root_review_id, revision_number)
                """
            )
            connection.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (2, datetime.now(timezone.utc).isoformat()),
            )


def _rehash_legacy_events(connection: sqlite3.Connection) -> None:
    rows = connection.execute(
        """
        SELECT event_id, occurred_at, actor, action, entity_id, details_json,
               previous_hash, event_hash, sequence
        FROM audit_log ORDER BY occurred_at, event_id
        """
    ).fetchall()
    previous_hash = ""
    for sequence, original in enumerate(rows, start=1):
        row = dict(original)
        row["previous_hash"] = previous_hash
        digest = _audit_digest(_audit_payload(row))
        if original["event_hash"] is None or original["sequence"] is None:
            connection.execute(
                """
                UPDATE audit_log SET sequence = ?, previous_hash = ?, event_hash = ?
                WHERE event_id = ?
                """,
                (sequence, previous_hash, digest, original["event_id"]),
            )
        previous_hash = original["event_hash"] or digest


def create_user(email: str, password_hash: str, role: str = "creator") -> str:
    if role not in {"admin", "reviewer", "creator"}:
        raise ValueError("Invalid user role.")
    user_id = str(uuid4())
    created_at = datetime.now(timezone.utc).isoformat()
    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO users(user_id, email, password_hash, role, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (user_id, email.strip().lower(), password_hash, role, created_at),
        )
        _append_audit(connection, "system", "user_created", user_id, {"role": role})
    return user_id


def get_user_by_email(email: str) -> dict[str, Any] | None:
    with _connect() as connection:
        row = connection.execute(
            "SELECT * FROM users WHERE email = ? COLLATE NOCASE",
            (email.strip(),),
        ).fetchone()
    return dict(row) if row else None


def get_user(user_id: str) -> dict[str, Any] | None:
    with _connect() as connection:
        row = connection.execute(
            """
            SELECT user_id, email, role, is_active, created_at
            FROM users WHERE user_id = ?
            """,
            (user_id,),
        ).fetchone()
    return dict(row) if row else None


def list_users() -> list[dict[str, Any]]:
    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT user_id, email, role, is_active, created_at
            FROM users ORDER BY created_at, email
            """
        ).fetchall()
    return [dict(row) for row in rows]


def update_user(
    user_id: str, role: str | None = None, is_active: bool | None = None
) -> bool:
    if role is not None and role not in {"admin", "reviewer", "creator"}:
        raise ValueError("Invalid user role.")
    updates: list[str] = []
    values: list[Any] = []
    if role is not None:
        updates.append("role = ?")
        values.append(role)
    if is_active is not None:
        updates.append("is_active = ?")
        values.append(int(is_active))
    if not updates:
        return False
    values.append(user_id)
    with _connect() as connection:
        cursor = connection.execute(
            f"UPDATE users SET {', '.join(updates)} WHERE user_id = ?", values
        )
        return cursor.rowcount > 0


def count_users() -> int:
    with _connect() as connection:
        return int(connection.execute("SELECT COUNT(*) FROM users").fetchone()[0])


def create_session(
    token_hash: str, csrf_hash: str, user_id: str, lifetime_hours: int = 12
) -> str:
    session_id = str(uuid4())
    created = datetime.now(timezone.utc)
    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO sessions(session_id, token_hash, csrf_hash, user_id, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                token_hash,
                csrf_hash,
                user_id,
                created.isoformat(),
                (created + timedelta(hours=lifetime_hours)).isoformat(),
            ),
        )
        connection.execute(
            "DELETE FROM sessions WHERE expires_at < ?",
            (created.isoformat(),),
        )
    return session_id


def get_session(token_hash: str) -> dict[str, Any] | None:
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as connection:
        row = connection.execute(
            """
            SELECT s.session_id, s.csrf_hash, s.expires_at, u.user_id, u.email,
                   u.role, u.is_active
            FROM sessions s JOIN users u USING(user_id)
            WHERE s.token_hash = ? AND s.expires_at > ?
            """,
            (token_hash, now),
        ).fetchone()
    return dict(row) if row and row["is_active"] else None


def rotate_csrf(session_id: str, csrf_hash: str) -> bool:
    with _connect() as connection:
        cursor = connection.execute(
            "UPDATE sessions SET csrf_hash = ? WHERE session_id = ?",
            (csrf_hash, session_id),
        )
        return cursor.rowcount > 0


def delete_session(token_hash: str) -> bool:
    with _connect() as connection:
        cursor = connection.execute(
            "DELETE FROM sessions WHERE token_hash = ?", (token_hash,)
        )
        return cursor.rowcount > 0


def check_rate_limit(
    bucket: str, subject: str, limit: int, window_seconds: int = 60
) -> bool:
    now = int(time.time())
    window_start = now - now % window_seconds
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """
            SELECT hits FROM persistent_rate_limits
            WHERE bucket = ? AND subject = ? AND window_start = ?
            """,
            (bucket, subject, window_start),
        ).fetchone()
        if row and row["hits"] >= limit:
            return False
        connection.execute(
            """
            INSERT INTO persistent_rate_limits(bucket, subject, window_start, hits)
            VALUES (?, ?, ?, 1)
            ON CONFLICT(bucket, subject, window_start)
            DO UPDATE SET hits = hits + 1
            """,
            (bucket, subject, window_start),
        )
        connection.execute(
            "DELETE FROM persistent_rate_limits WHERE window_start < ?",
            (window_start - window_seconds * 2,),
        )
    return True


def record_audit(
    actor: str, action: str, entity_id: str, details: dict[str, Any] | None = None
) -> None:
    with _connect() as connection:
        _append_audit(connection, actor, action, entity_id, details)


def save_review(
    filename: str,
    content_type: str,
    submitted_text: str,
    extracted_text: str,
    review: dict[str, Any],
    owner_id: str | None = None,
    parent_review_id: str | None = None,
) -> str:
    review_id = str(uuid4())
    created_at = datetime.now(timezone.utc).isoformat()
    root_review_id = review_id
    revision_number = 1
    if parent_review_id:
        with _connect() as connection:
            parent = connection.execute(
                """
                SELECT review_id, root_review_id, owner_id, revision_number, decision
                FROM reviews WHERE review_id = ?
                """,
                (parent_review_id,),
            ).fetchone()
        if parent is None:
            raise ValueError("Parent submission was not found.")
        if parent["owner_id"] != owner_id:
            raise PermissionError("The submission does not belong to this user.")
        if parent["decision"] != "changes_requested":
            raise ValueError("Only submissions with requested changes can be revised.")
        root_review_id = parent["root_review_id"] or parent["review_id"]
        with _connect() as connection:
            latest = connection.execute(
                """
                SELECT review_id FROM reviews
                WHERE root_review_id = ? ORDER BY revision_number DESC LIMIT 1
                """,
                (root_review_id,),
            ).fetchone()
        if latest is None or latest["review_id"] != parent_review_id:
            raise ValueError("Only the latest requested revision can be resubmitted.")
        revision_number = parent["revision_number"] + 1

    review["review_id"] = review_id
    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO reviews
                (review_id, created_at, filename, content_type, submitted_text,
                 extracted_text, review_json, owner_id, root_review_id,
                 parent_review_id, revision_number)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                review_id,
                created_at,
                filename,
                content_type,
                submitted_text,
                extracted_text,
                json.dumps(review, ensure_ascii=False),
                owner_id,
                root_review_id,
                parent_review_id,
                revision_number,
            ),
        )
        actor = owner_id or "discord-service"
        _append_audit(
            connection,
            actor,
            "content_submitted" if revision_number == 1 else "content_resubmitted",
            review_id,
            {"root_review_id": root_review_id, "revision_number": revision_number},
            created_at,
        )
        _append_audit(
            connection,
            "system",
            "review_completed",
            review_id,
            {"root_review_id": root_review_id, "revision_number": revision_number},
            created_at,
        )
    return review_id


def get_review(review_id: str) -> dict[str, Any] | None:
    with _connect() as connection:
        row = connection.execute(
            """
            SELECT r.*, u.email AS owner_email
            FROM reviews r LEFT JOIN users u ON u.user_id = r.owner_id
            WHERE r.review_id = ?
            """,
            (review_id,),
        ).fetchone()
        if row is None:
            return None
        revisions = connection.execute(
            """
            SELECT review_id, created_at, decision, reviewer, decision_reason,
                   decided_at, revision_number, parent_review_id, review_json,
                   submitted_text
            FROM reviews WHERE root_review_id = ?
            ORDER BY revision_number
            """,
            (row["root_review_id"] or review_id,),
        ).fetchall()
    return {
        "review_id": row["review_id"],
        "created_at": row["created_at"],
        "filename": row["filename"],
        "content_type": row["content_type"],
        "submitted_text": row["submitted_text"],
        "extracted_text": row["extracted_text"],
        "review": json.loads(row["review_json"]),
        "decision": row["decision"],
        "reviewer": row["reviewer"],
        "decision_reason": row["decision_reason"],
        "decided_at": row["decided_at"],
        "owner_id": row["owner_id"],
        "owner_email": row["owner_email"],
        "root_review_id": row["root_review_id"] or row["review_id"],
        "parent_review_id": row["parent_review_id"],
        "revision_number": row["revision_number"],
        "revision_history": [
            {
                "review_id": revision["review_id"],
                "created_at": revision["created_at"],
                "decision": revision["decision"],
                "reviewer": revision["reviewer"],
                "decision_reason": revision["decision_reason"],
                "decided_at": revision["decided_at"],
                "revision_number": revision["revision_number"],
                "parent_review_id": revision["parent_review_id"],
                "submitted_text": revision["submitted_text"],
                "review": json.loads(revision["review_json"]),
            }
            for revision in revisions
        ],
    }


def list_reviews(
    limit: int = 50, owner_id: str | None = None, pending_only: bool = False
) -> list[dict[str, Any]]:
    conditions: list[str] = []
    values: list[Any] = []
    if owner_id is not None:
        conditions.append("owner_id = ?")
        values.append(owner_id)
    if pending_only:
        conditions.append("decision = 'pending'")
    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    values.append(limit)
    with _connect() as connection:
        rows = connection.execute(
            f"""
            SELECT r.review_id, r.created_at, r.filename, r.content_type, r.review_json,
                   r.decision, r.reviewer, r.decision_reason, r.decided_at, r.owner_id,
                   r.root_review_id, r.parent_review_id, r.revision_number, r.submitted_text,
                   u.email AS owner_email
            FROM reviews r LEFT JOIN users u ON u.user_id = r.owner_id {where_clause}
            ORDER BY r.created_at DESC LIMIT ?
            """,
            values,
        ).fetchall()
    return [
        {
            "review_id": row["review_id"],
            "created_at": row["created_at"],
            "filename": row["filename"],
            "content_type": row["content_type"],
            "review": json.loads(row["review_json"]),
            "decision": row["decision"],
            "reviewer": row["reviewer"],
            "decision_reason": row["decision_reason"],
            "decided_at": row["decided_at"],
            "owner_id": row["owner_id"],
            "root_review_id": row["root_review_id"] or row["review_id"],
            "parent_review_id": row["parent_review_id"],
            "revision_number": row["revision_number"],
            "submitted_text": row["submitted_text"],
            "owner_email": row["owner_email"],
        }
        for row in rows
    ]


def update_decision(
    review_id: str,
    decision: str,
    reviewer_id: str,
    reviewer_email: str,
    reason: str,
) -> bool:
    decided_at = datetime.now(timezone.utc).isoformat()
    with _connect() as connection:
        cursor = connection.execute(
            """
            UPDATE reviews
            SET decision = ?, reviewer = ?, decision_reason = ?, decided_at = ?
            WHERE review_id = ? AND decision = 'pending'
            """,
            (decision, reviewer_email, reason, decided_at, review_id),
        )
        if cursor.rowcount:
            _append_audit(
                connection,
                reviewer_id,
                decision,
                review_id,
                {"reason": reason, "reviewer_email": reviewer_email},
                decided_at,
            )
        return cursor.rowcount > 0


def dashboard_counts(owner_id: str | None = None) -> dict[str, int]:
    with _connect() as connection:
        if owner_id is None:
            rows = connection.execute(
                "SELECT decision, COUNT(*) AS count FROM reviews GROUP BY decision"
            ).fetchall()
            high_risk = connection.execute(
                "SELECT COUNT(*) FROM reviews WHERE json_extract(review_json, '$.severity') = 'high'"
            ).fetchone()[0]
            total = connection.execute("SELECT COUNT(*) FROM reviews").fetchone()[0]
        else:
            rows = connection.execute(
                "SELECT decision, COUNT(*) AS count FROM reviews WHERE owner_id = ? GROUP BY decision",
                (owner_id,),
            ).fetchall()
            high_risk = connection.execute(
                "SELECT COUNT(*) FROM reviews WHERE owner_id = ? AND json_extract(review_json, '$.severity') = 'high'",
                (owner_id,),
            ).fetchone()[0]
            total = connection.execute(
                "SELECT COUNT(*) FROM reviews WHERE owner_id = ?", (owner_id,)
            ).fetchone()[0]
    counts = {row["decision"]: row["count"] for row in rows}
    return {
        "total": total,
        "pending": counts.get("pending", 0),
        "approved": counts.get("approved", 0),
        "changes_requested": counts.get("changes_requested", 0),
        "rejected": counts.get("rejected", 0),
        "high_risk": high_risk,
    }


def list_audit_events(limit: int = 100) -> list[dict[str, Any]]:
    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT occurred_at, actor, action, entity_id, details_json,
                   previous_hash, event_hash, sequence
            FROM audit_log ORDER BY sequence DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [
        {
            "occurred_at": row["occurred_at"],
            "actor": row["actor"],
            "action": row["action"],
            "entity_id": row["entity_id"],
            "details": json.loads(row["details_json"]),
            "previous_hash": row["previous_hash"],
            "event_hash": row["event_hash"],
            "sequence": row["sequence"],
        }
        for row in rows
    ]


def verify_audit_chain() -> dict[str, Any]:
    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT event_id, occurred_at, actor, action, entity_id, details_json,
                   previous_hash, event_hash, sequence
            FROM audit_log ORDER BY sequence
            """
        ).fetchall()
    previous_hash = ""
    for expected_sequence, original in enumerate(rows, start=1):
        row = dict(original)
        if row["sequence"] != expected_sequence:
            return {
                "valid": False,
                "events_checked": expected_sequence - 1,
                "invalid_event_id": row["event_id"],
                "reason": "sequence_gap",
            }
        if row["previous_hash"] != previous_hash:
            return {
                "valid": False,
                "events_checked": expected_sequence - 1,
                "invalid_event_id": row["event_id"],
                "reason": "previous_hash_mismatch",
            }
        if not hmac.compare_digest(_audit_digest(_audit_payload(row)), row["event_hash"] or ""):
            return {
                "valid": False,
                "events_checked": expected_sequence - 1,
                "invalid_event_id": row["event_id"],
                "reason": "event_hash_mismatch",
            }
        previous_hash = row["event_hash"]
    return {"valid": True, "events_checked": len(rows)}


def purge_reviews_older_than(retention_days: int) -> int:
    if retention_days < 1:
        raise ValueError("Retention must be at least one day.")
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
    with _connect() as connection:
        root_ids = [
            row["root_review_id"] or row["review_id"]
            for row in connection.execute(
                """
                SELECT review_id, root_review_id FROM reviews WHERE created_at < ?
                """,
                (cutoff,),
            ).fetchall()
        ]
        deleted = 0
        for root_id in set(root_ids):
            cursor = connection.execute(
                "DELETE FROM reviews WHERE root_review_id = ? OR review_id = ?",
                (root_id, root_id),
            )
            deleted += cursor.rowcount
        if deleted:
            _append_audit(
                connection,
                "system",
                "review_retention_purge",
                "retention",
                {"deleted_review_count": deleted, "retention_days": retention_days},
            )
    return deleted


def delete_review_thread(review_id: str, actor_id: str, owner_id: str | None = None) -> int:
    with _connect() as connection:
        row = connection.execute(
            """
            SELECT root_review_id, owner_id FROM reviews WHERE review_id = ?
            """,
            (review_id,),
        ).fetchone()
        if row is None or (owner_id is not None and row["owner_id"] != owner_id):
            return 0
        root_id = row["root_review_id"] or review_id
        cursor = connection.execute(
            "DELETE FROM reviews WHERE root_review_id = ? OR review_id = ?",
            (root_id, root_id),
        )
        _append_audit(
            connection,
            actor_id,
            "submission_content_deleted",
            root_id,
            {"deleted_review_count": cursor.rowcount},
        )
        return cursor.rowcount


def migration_versions() -> list[int]:
    with _connect() as connection:
        rows = connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
    return [int(row["version"]) for row in rows]


def set_setting(key: str, value: dict[str, Any]) -> None:
    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO app_settings(setting_key, setting_json, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(setting_key) DO UPDATE SET
                setting_json = excluded.setting_json, updated_at = excluded.updated_at
            """,
            (
                key,
                json.dumps(value, ensure_ascii=False),
                datetime.now(timezone.utc).isoformat(),
            ),
        )


def get_setting(key: str, default: dict[str, Any] | None = None) -> dict[str, Any]:
    with _connect() as connection:
        row = connection.execute(
            "SELECT setting_json FROM app_settings WHERE setting_key = ?", (key,)
        ).fetchone()
    return json.loads(row["setting_json"]) if row else (default or {})


def save_visual_asset(
    name: str,
    asset_type: str,
    mime_type: str,
    image_bytes: bytes,
    created_by: str,
) -> str:
    asset_id = str(uuid4())
    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO visual_assets
                (asset_id, name, asset_type, mime_type, image_bytes, created_at, created_by)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                asset_id,
                name,
                asset_type,
                mime_type,
                image_bytes,
                datetime.now(timezone.utc).isoformat(),
                created_by,
            ),
        )
        _append_audit(
            connection,
            created_by,
            "visual_asset_added",
            asset_id,
            {"name": name, "asset_type": asset_type},
        )
    return asset_id


def list_visual_assets() -> list[dict[str, Any]]:
    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT asset_id, name, asset_type, mime_type, created_at
            FROM visual_assets ORDER BY created_at DESC
            """
        ).fetchall()
    return [dict(row) for row in rows]


def get_visual_assets(asset_type: str | None = None) -> list[dict[str, Any]]:
    with _connect() as connection:
        if asset_type:
            rows = connection.execute(
                "SELECT * FROM visual_assets WHERE asset_type = ? ORDER BY created_at DESC",
                (asset_type,),
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM visual_assets ORDER BY created_at DESC"
            ).fetchall()
    return [dict(row) for row in rows]


def delete_visual_asset(asset_id: str, actor_id: str) -> bool:
    with _connect() as connection:
        row = connection.execute(
            "SELECT name, asset_type FROM visual_assets WHERE asset_id = ?",
            (asset_id,),
        ).fetchone()
        if not row:
            return False
        connection.execute("DELETE FROM visual_assets WHERE asset_id = ?", (asset_id,))
        _append_audit(
            connection,
            actor_id,
            "visual_asset_removed",
            asset_id,
            {"name": row["name"], "asset_type": row["asset_type"]},
        )
    return True
