import json
import logging
import math
import os
import re
from typing import Any
from uuid import uuid4

import httpx

from backend.storage import _connect, initialize_database


logger = logging.getLogger(__name__)
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "gemini-embedding-001")
EMBEDDING_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"{EMBEDDING_MODEL}:embedContent"
)


def split_into_chunks(content: str, max_chars: int = 900) -> list[str]:
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", content) if part.strip()]
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs or [content.strip()]:
        while len(paragraph) > max_chars:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(paragraph[:max_chars])
            paragraph = paragraph[max_chars:]
        candidate = f"{current}\n\n{paragraph}".strip()
        if current and len(candidate) > max_chars:
            chunks.append(current)
            current = paragraph
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


async def _embed(text: str) -> list[float] | None:
    api_key = os.getenv("LLM_API_KEY")
    if not api_key or os.getenv("EMBEDDING_PROVIDER", "gemini").lower() != "gemini":
        return None
    body = {
        "model": f"models/{EMBEDDING_MODEL}",
        "content": {"parts": [{"text": text}]},
    }
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                EMBEDDING_URL,
                headers={"x-goog-api-key": api_key},
                json=body,
            )
            response.raise_for_status()
            payload = response.json()
        values = payload["embedding"]["values"]
        if not isinstance(values, list) or not values:
            raise ValueError("Embedding response did not contain vector values.")
        return [float(value) for value in values]
    except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
        logger.warning("Gemini embeddings unavailable; using lexical retrieval: %s", exc)
        return None


def upsert_document_record(
    document_id: str, title: str, source: str, content: str
) -> None:
    from datetime import datetime, timezone

    chunks = split_into_chunks(content)
    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO knowledge_documents(document_id, title, source, content, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(document_id) DO UPDATE SET
                title = excluded.title, source = excluded.source, content = excluded.content,
                updated_at = excluded.updated_at
            """,
            (
                document_id,
                title,
                source,
                content,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        connection.execute("DELETE FROM knowledge_chunks WHERE document_id = ?", (document_id,))
        connection.executemany(
            """
            INSERT INTO knowledge_chunks(chunk_id, document_id, chunk_index, content)
            VALUES (?, ?, ?, ?)
            """,
            [
                (str(uuid4()), document_id, index, chunk)
                for index, chunk in enumerate(chunks)
            ],
        )


async def index_document(
    document_id: str, title: str, source: str, content: str
) -> None:
    initialize_database()
    with _connect() as connection:
        existing = connection.execute(
            "SELECT title, source, content FROM knowledge_documents WHERE document_id = ?",
            (document_id,),
        ).fetchone()
        embedding_state = connection.execute(
            """
            SELECT COUNT(*) AS total, COUNT(embedding_json) AS embedded
            FROM knowledge_chunks WHERE document_id = ?
            """,
            (document_id,),
        ).fetchone()
    embedding_configured = (
        bool(os.getenv("LLM_API_KEY"))
        and os.getenv("EMBEDDING_PROVIDER", "gemini").lower() == "gemini"
    )
    if (
        existing is not None
        and existing["title"] == title
        and existing["source"] == source
        and existing["content"] == content
        and (
            not embedding_configured
            or embedding_state["total"] == embedding_state["embedded"]
        )
    ):
        return
    upsert_document_record(document_id, title, source, content)
    chunks = split_into_chunks(content)
    vectors = await asyncio_gather_embeddings(chunks)
    with _connect() as connection:
        rows = connection.execute(
            "SELECT chunk_id FROM knowledge_chunks WHERE document_id = ? ORDER BY chunk_index",
            (document_id,),
        ).fetchall()
        connection.executemany(
            "UPDATE knowledge_chunks SET embedding_json = ? WHERE chunk_id = ?",
            [
                (
                    json.dumps(vector) if vector is not None else None,
                    row["chunk_id"],
                )
                for row, vector in zip(rows, vectors)
            ],
        )


async def asyncio_gather_embeddings(chunks: list[str]) -> list[list[float] | None]:
    import asyncio

    semaphore = asyncio.Semaphore(4)

    async def bounded_embed(chunk: str) -> list[float] | None:
        async with semaphore:
            return await _embed(chunk)

    return list(await asyncio.gather(*(bounded_embed(chunk) for chunk in chunks)))


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def _lexical_score(query: str, content: str) -> float:
    query_terms = set(_tokens(query))
    content_terms = set(_tokens(content))
    if not query_terms:
        return 0.0
    return len(query_terms & content_terms) / math.sqrt(len(query_terms) * max(1, len(content_terms)))


def _cosine(left: list[float], right: list[float]) -> float:
    if len(left) != len(right):
        return 0.0
    numerator = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if not left_norm or not right_norm:
        return 0.0
    return numerator / (left_norm * right_norm)


async def retrieve_evidence(query: str, top_k: int = 5) -> dict[str, Any]:
    initialize_database()
    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT c.content, c.embedding_json, d.document_id, d.title, d.source
            FROM knowledge_chunks c
            JOIN knowledge_documents d USING(document_id)
            ORDER BY d.updated_at DESC, c.chunk_index
            """
        ).fetchall()
    query_embedding = await _embed(query)
    use_embeddings = bool(rows) and query_embedding is not None and all(
        row["embedding_json"] for row in rows
    )
    scored: list[tuple[float, Any]] = []
    for row in rows:
        if use_embeddings and row["embedding_json"]:
            score = _cosine(query_embedding, json.loads(row["embedding_json"]))
        else:
            score = _lexical_score(query, row["content"])
        if score > 0:
            scored.append((score, row))
    scored.sort(key=lambda result: result[0], reverse=True)
    return {
        "mode": "gemini_embeddings" if use_embeddings else "lexical_fallback",
        "evidence": [
            {
                "source": row["source"],
                "document_id": row["document_id"],
                "title": row["title"],
                "guideline": row["content"],
                "relevance_score": round(score, 4),
            }
            for score, row in scored[:top_k]
        ],
    }


def delete_document(document_id: str) -> bool:
    initialize_database()
    with _connect() as connection:
        cursor = connection.execute(
            "DELETE FROM knowledge_documents WHERE document_id = ?", (document_id,)
        )
        return cursor.rowcount > 0


def list_documents() -> list[dict[str, str]]:
    initialize_database()
    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT document_id, title, source, updated_at
            FROM knowledge_documents ORDER BY updated_at DESC
            """
        ).fetchall()
    return [dict(row) for row in rows]
