from __future__ import annotations

import os
import uuid

import database


USER_SESSION_KEYS = {
    "analysis", "application_id", "analysis_jd", "active_base_resume_id", "deep_dive",
    "deep_dive_case", "rematch_delta", "rematch_error", "polish_strategy",
    "polish_strategy_signature", "resume_draft", "draft_review_text", "manage_resume_id",
    "confirm_delete_resume", "jd_text", "resume_flash",
    "pending_application_id",
}


def resolve_user_id(candidate: str | None, app_mode: str | None = None) -> str | None:
    mode = (app_mode or os.getenv("APP_MODE", "dev")).strip().lower()
    if mode != "beta":
        return database.DEV_USER_ID
    try:
        canonical = str(uuid.UUID((candidate or "").strip()))
    except (ValueError, AttributeError):
        return None
    return canonical if database.get_user(canonical) else None


def clear_user_session_state(session_state, previous_user_id: str | None, new_user_id: str) -> None:
    if previous_user_id and previous_user_id != new_user_id:
        for key in USER_SESSION_KEYS:
            session_state.pop(key, None)
    session_state["current_user_id"] = new_user_id
