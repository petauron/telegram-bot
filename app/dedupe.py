from __future__ import annotations

import re
from difflib import SequenceMatcher
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


TRACKING_PARAMS = {
    "fbclid",
    "gclid",
    "spm",
    "from",
    "ref",
    "source",
}
SEMANTIC_RECENT_CANDIDATES = 8
SEMANTIC_RELEVANT_CANDIDATES = 4
_EVENT_TOKEN_RE = re.compile(
    r"(?i)\b(?:CVE-\d{4}-\d+|[A-Z][A-Z0-9.+_-]{2,}|\d+(?:\.\d+){1,3}|\d+(?:元|美元|%))\b"
)


def canonical_url(url: str | None) -> str | None:
    if not url:
        return None
    candidate = url if "://" in url else f"https://{url}"
    try:
        parts = urlsplit(candidate)
    except ValueError:
        return candidate.casefold().rstrip("/")
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.casefold().startswith("utm_") and key.casefold() not in TRACKING_PARAMS
    ]
    return urlunsplit(
        (
            parts.scheme.casefold(),
            parts.netloc.casefold(),
            parts.path.rstrip("/"),
            urlencode(query),
            "",
        )
    )


def select_digest_items(
    candidates: list[dict],
    *,
    limit: int = 7,
    similarity_threshold: float = 0.84,
) -> list[dict]:
    ordered = sorted(
        candidates,
        key=lambda row: (
            int(row.get("ai_score") or 0),
            str(row.get("created_at", "")),
        ),
        reverse=True,
    )
    selected: list[dict] = []
    seen_urls: set[str] = set()
    seen_threads: set[tuple[int, int]] = set()

    for row in ordered:
        url = canonical_url(row.get("primary_url"))
        if url and url in seen_urls:
            continue

        thread_key = (int(row["chat_id"]), int(row["thread_root_id"]))
        if thread_key in seen_threads:
            continue

        normalized = str(row.get("normalized_text") or "")
        if len(normalized) >= 20 and any(
            len(str(existing.get("normalized_text") or "")) >= 20
            and SequenceMatcher(
                None,
                normalized,
                str(existing.get("normalized_text") or ""),
                autojunk=False,
            ).ratio()
            >= similarity_threshold
            for existing in selected
        ):
            continue

        selected.append(row)
        seen_threads.add(thread_key)
        if url:
            seen_urls.add(url)
        if len(selected) >= limit:
            break

    return selected


def select_semantic_candidates(
    current: dict,
    candidates_newest_first: tuple[dict, ...],
    *,
    recent_limit: int = SEMANTIC_RECENT_CANDIDATES,
    relevant_limit: int = SEMANTIC_RELEVANT_CANDIDATES,
) -> tuple[dict, ...]:
    """Keep recent events plus the most lexically relevant older candidates."""
    candidates = tuple(candidates_newest_first)
    recent = list(candidates[: max(0, recent_limit)])
    recent_ids = {int(row["id"]) for row in recent}
    current_text = str(current.get("normalized_text") or current.get("text") or "")
    current_summary = str(current.get("ai_summary") or "")
    current_tokens = {
        token.casefold()
        for token in _EVENT_TOKEN_RE.findall(
            f"{current_summary} {current.get('text') or ''}"
        )
    }

    def relevance(row: dict) -> tuple[float, str, int]:
        candidate_text = str(row.get("normalized_text") or row.get("text") or "")
        candidate_summary = str(row.get("ai_summary") or "")
        text_ratio = SequenceMatcher(
            None, current_text, candidate_text, autojunk=False
        ).ratio()
        summary_ratio = SequenceMatcher(
            None, current_summary, candidate_summary, autojunk=False
        ).ratio()
        candidate_tokens = {
            token.casefold()
            for token in _EVENT_TOKEN_RE.findall(
                f"{candidate_summary} {row.get('text') or ''}"
            )
        }
        token_bonus = 0.25 if current_tokens & candidate_tokens else 0.0
        return (
            max(text_ratio, summary_ratio) + token_bonus,
            str(row.get("ai_completed_at") or row.get("created_at") or ""),
            int(row.get("id") or 0),
        )

    older = [row for row in candidates if int(row["id"]) not in recent_ids]
    older.sort(key=relevance, reverse=True)
    selected = [*recent, *older[: max(0, relevant_limit)]]
    return tuple(selected)
