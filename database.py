from __future__ import annotations

import json
import os
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path


DB_PATH = Path("data/app.db")
DEV_USER_ID = "dev_user"


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS llm_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                operation_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                provider TEXT NOT NULL,
                operation TEXT NOT NULL,
                attempt INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                finished_at TEXT,
                error_code TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS career_facts (
                id TEXT PRIMARY KEY,
                organization TEXT NOT NULL,
                official_job_title TEXT NOT NULL,
                fact_type TEXT NOT NULL,
                statement TEXT NOT NULL,
                skills TEXT NOT NULL DEFAULT '[]',
                verification_status TEXT NOT NULL,
                source TEXT NOT NULL,
                restrictions TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS job_applications (
                id TEXT PRIMARY KEY,
                job_title TEXT NOT NULL,
                company TEXT NOT NULL,
                jd_text TEXT NOT NULL,
                analysis_json TEXT,
                resume_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS resumes (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                language TEXT NOT NULL DEFAULT 'auto',
                original_filename TEXT NOT NULL DEFAULT '',
                original_file_path TEXT NOT NULL DEFAULT '',
                content_text TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS fact_sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fact_id TEXT NOT NULL,
                resume_id TEXT,
                source_excerpt TEXT NOT NULL DEFAULT '',
                source_type TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(fact_id, resume_id, source_excerpt)
            );
            CREATE TABLE IF NOT EXISTS resume_ingestions (
                resume_id TEXT PRIMARY KEY,
                content_hash TEXT NOT NULL,
                parser_version TEXT NOT NULL,
                parsed_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS experience_cases (
                id TEXT PRIMARY KEY,
                case_type TEXT NOT NULL,
                title TEXT NOT NULL,
                summary TEXT NOT NULL,
                skills_json TEXT NOT NULL DEFAULT '[]',
                source_answer TEXT NOT NULL DEFAULT '',
                requirement_id TEXT NOT NULL DEFAULT '',
                verification_status TEXT NOT NULL DEFAULT 'confirmed',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO users (id, display_name, is_active, created_at) VALUES (?, ?, 1, ?)",
            (DEV_USER_ID, "Local Developer", datetime.now().isoformat(timespec="seconds")),
        )
        for table in ("resumes", "career_facts", "fact_sources", "resume_ingestions", "experience_cases", "job_applications"):
            _add_column_if_missing(conn, table, "user_id", f"TEXT NOT NULL DEFAULT '{DEV_USER_ID}'")
        _add_column_if_missing(conn, "users", "revoked_at", "TEXT")
        _add_column_if_missing(conn, "users", "career_revision", "INTEGER NOT NULL DEFAULT 0")
        _add_column_if_missing(conn, "career_facts", "source_resume_id", "TEXT")
        _add_column_if_missing(conn, "career_facts", "source_excerpt", "TEXT NOT NULL DEFAULT ''")
        _add_column_if_missing(conn, "career_facts", "allowed_for_generation", "INTEGER NOT NULL DEFAULT 1")
        _add_column_if_missing(conn, "career_facts", "evidence_origin", "TEXT NOT NULL DEFAULT 'user_confirmed'")
        _add_column_if_missing(conn, "career_facts", "evidence_kind", "TEXT NOT NULL DEFAULT 'direct'")
        _add_column_if_missing(conn, "career_facts", "normalized_key", "TEXT NOT NULL DEFAULT ''")
        _add_column_if_missing(conn, "career_facts", "education_json", "TEXT NOT NULL DEFAULT '{}'")
        _add_column_if_missing(conn, "career_facts", "source_valid", "INTEGER NOT NULL DEFAULT 1")
        _add_column_if_missing(conn, "career_facts", "case_id", "TEXT")
        _add_column_if_missing(conn, "career_facts", "evidence_type", "TEXT NOT NULL DEFAULT ''")
        _add_column_if_missing(conn, "career_facts", "project_name", "TEXT NOT NULL DEFAULT ''")
        _add_column_if_missing(conn, "career_facts", "role", "TEXT NOT NULL DEFAULT ''")
        _add_column_if_missing(conn, "career_facts", "date_range", "TEXT NOT NULL DEFAULT ''")
        _add_column_if_missing(conn, "career_facts", "context", "TEXT NOT NULL DEFAULT ''")
        for column in ("organization", "official_job_title", "project_name", "role", "date_range", "context"):
            _add_column_if_missing(conn, "experience_cases", column, "TEXT NOT NULL DEFAULT ''")
        _add_column_if_missing(conn, "job_applications", "base_resume_id", "TEXT")
        _add_column_if_missing(conn, "job_applications", "content_controls_json", "TEXT NOT NULL DEFAULT '{}'")
        _add_column_if_missing(conn, "job_applications", "user_preference_prompt", "TEXT NOT NULL DEFAULT ''")
        for column, definition in (
            ("jd_source_type", "TEXT NOT NULL DEFAULT 'text'"),
            ("jd_source", "TEXT NOT NULL DEFAULT ''"),
            ("requirement_state_json", "TEXT NOT NULL DEFAULT '{}'"),
            ("eligibility_state_json", "TEXT NOT NULL DEFAULT '{}'"),
            ("deep_dive_state_json", "TEXT NOT NULL DEFAULT '{}'"),
            ("strategy_json", "TEXT NOT NULL DEFAULT '{}'"),
            ("strategy_approved_at", "TEXT"),
            ("final_resume_json", "TEXT NOT NULL DEFAULT '{}'"),
            ("final_approved_at", "TEXT"),
            ("template_id", "TEXT NOT NULL DEFAULT 'ats_simple'"),
            ("preflight_json", "TEXT NOT NULL DEFAULT '{}'"),
            ("application_status", "TEXT NOT NULL DEFAULT 'NEW'"),
            ("evidence_revision", "INTEGER NOT NULL DEFAULT 0"),
            ("draft_review_json", "TEXT NOT NULL DEFAULT '{}'"),
        ):
            _add_column_if_missing(conn, "job_applications", column, definition)
        conn.execute(
            """UPDATE job_applications SET application_status = CASE
            WHEN final_resume_json != '{}' THEN 'FINAL_READY'
            WHEN resume_json IS NOT NULL AND resume_json != '' THEN 'DRAFT_READY'
            WHEN analysis_json IS NOT NULL AND analysis_json != '' THEN 'READY_FOR_STRATEGY'
            WHEN jd_text != '' THEN 'JD_READY' ELSE 'NEW' END
            WHERE application_status = 'NEW'"""
        )
        conn.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_resumes_user_updated ON resumes(user_id, updated_at);
            CREATE INDEX IF NOT EXISTS idx_facts_user_key ON career_facts(user_id, normalized_key);
            CREATE INDEX IF NOT EXISTS idx_facts_user_status ON career_facts(user_id, verification_status, source_valid);
            CREATE INDEX IF NOT EXISTS idx_sources_user_fact ON fact_sources(user_id, fact_id);
            CREATE INDEX IF NOT EXISTS idx_sources_user_resume ON fact_sources(user_id, resume_id);
            CREATE INDEX IF NOT EXISTS idx_ingestions_user_resume ON resume_ingestions(user_id, resume_id);
            CREATE INDEX IF NOT EXISTS idx_cases_user_created ON experience_cases(user_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_jobs_user_updated ON job_applications(user_id, updated_at);
            CREATE INDEX IF NOT EXISTS idx_llm_usage_user_time ON llm_usage(user_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_llm_usage_operation ON llm_usage(operation_id, event_type, attempt);
            """
        )
        for row in conn.execute("SELECT id, organization, official_job_title, statement FROM career_facts WHERE normalized_key = ''").fetchall():
            conn.execute(
                "UPDATE career_facts SET normalized_key = ? WHERE id = ?",
                (_fact_key(dict(row)), row["id"]),
            )
        for user_row in conn.execute("SELECT id FROM users").fetchall():
            _reconcile_all_fact_sources(conn, user_row["id"])
        for row in conn.execute("SELECT id, fact_type FROM career_facts WHERE evidence_type = ''").fetchall():
            conn.execute("UPDATE career_facts SET evidence_type = ? WHERE id = ?", (_infer_evidence_type(row["fact_type"]), row["id"]))


def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def create_user(display_name: str, user_id: str | None = None) -> str:
    canonical_id = str(uuid.UUID(user_id)) if user_id else str(uuid.uuid4())
    with _connect() as conn:
        conn.execute(
            "INSERT INTO users (id, display_name, is_active, created_at) VALUES (?, ?, 1, ?)",
            (canonical_id, display_name.strip() or "Beta User", datetime.now().isoformat(timespec="seconds")),
        )
    return canonical_id


def get_user(user_id: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ? AND is_active = 1", (user_id,)).fetchone()
    return dict(row) if row else None


def revoke_user(user_id: str) -> bool:
    if user_id == DEV_USER_ID:
        raise ValueError("dev_user 不能撤销")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _connect() as conn:
        changed = conn.execute(
            "UPDATE users SET is_active = 0, revoked_at = ? WHERE id = ? AND is_active = 1",
            (now, user_id),
        ).rowcount
    return bool(changed)


def _require_owned(conn: sqlite3.Connection, table: str, resource_id: str, user_id: str, id_column: str = "id") -> None:
    if not conn.execute(
        f"SELECT 1 FROM {table} WHERE {id_column} = ? AND user_id = ?", (resource_id, user_id)
    ).fetchone():
        raise PermissionError("资源不存在或不属于当前用户")


def _require_active_user(conn: sqlite3.Connection, user_id: str) -> None:
    if not conn.execute(
        "SELECT 1 FROM users WHERE id = ? AND is_active = 1", (user_id,)
    ).fetchone():
        raise PermissionError("用户不存在或已停用")


def _career_revision(conn: sqlite3.Connection, user_id: str) -> int:
    row = conn.execute("SELECT career_revision FROM users WHERE id = ?", (user_id,)).fetchone()
    return int(row[0]) if row else 0


def _touch_career_truth(conn: sqlite3.Connection, user_id: str) -> None:
    conn.execute("UPDATE users SET career_revision = career_revision + 1 WHERE id = ?", (user_id,))
    conn.execute(
        """UPDATE job_applications SET application_status = 'NEEDS_REMATCH', updated_at = ?
        WHERE user_id = ? AND application_status NOT IN ('NEW', 'JD_READY')""",
        (datetime.now().isoformat(timespec="seconds"), user_id),
    )


def _ensure_llm_usage_schema(conn: sqlite3.Connection) -> None:
    """Small rolling-deploy guard; normal startup still performs the full migration."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS llm_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL,
            operation_id TEXT NOT NULL, event_type TEXT NOT NULL, provider TEXT NOT NULL,
            operation TEXT NOT NULL, attempt INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL, created_at TEXT NOT NULL, finished_at TEXT,
            error_code TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_llm_usage_user_time ON llm_usage(user_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_llm_usage_operation ON llm_usage(operation_id, event_type, attempt);
        """
    )


def start_llm_operation(user_id: str, provider: str, operation: str) -> str:
    operation_id = f"OP-{uuid.uuid4().hex}"
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _connect() as conn:
        _ensure_llm_usage_schema(conn)
        _require_active_user(conn, user_id)
        conn.execute(
            """INSERT INTO llm_usage
            (user_id, operation_id, event_type, provider, operation, attempt, status, created_at)
            VALUES (?, ?, 'operation', ?, ?, 0, 'started', ?)""",
            (user_id, operation_id, provider, operation, now),
        )
    return operation_id


def reserve_llm_request(user_id: str, operation_id: str, provider: str, operation: str, attempt: int) -> int:
    limit = max(1, int(os.getenv("DAILY_LLM_REQUEST_LIMIT", "50")))
    day_start = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00+00:00")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        _require_active_user(conn, user_id)
        used = conn.execute(
            """SELECT COUNT(*) FROM llm_usage
            WHERE user_id = ? AND event_type = 'request' AND created_at >= ?""",
            (user_id, day_start),
        ).fetchone()[0]
        if used >= limit:
            raise PermissionError("今日 AI 调用额度已用完，请明天再试或联系管理员。")
        cursor = conn.execute(
            """INSERT INTO llm_usage
            (user_id, operation_id, event_type, provider, operation, attempt, status, created_at)
            VALUES (?, ?, 'request', ?, ?, ?, 'started', ?)""",
            (user_id, operation_id, provider, operation, attempt, now),
        )
    return int(cursor.lastrowid)


def finish_llm_usage(usage_id: int, user_id: str, status: str, error_code: str = "") -> None:
    with _connect() as conn:
        _require_active_user(conn, user_id)
        conn.execute(
            """UPDATE llm_usage SET status = ?, finished_at = ?, error_code = ?
            WHERE id = ? AND user_id = ?""",
            (status, datetime.now(timezone.utc).isoformat(timespec="seconds"), error_code, usage_id, user_id),
        )


def finish_llm_operation(operation_id: str, user_id: str, status: str, error_code: str = "") -> None:
    with _connect() as conn:
        _require_active_user(conn, user_id)
        conn.execute(
            """UPDATE llm_usage SET status = ?, finished_at = ?, error_code = ?
            WHERE operation_id = ? AND event_type = 'operation' AND user_id = ?""",
            (status, datetime.now(timezone.utc).isoformat(timespec="seconds"), error_code, operation_id, user_id),
        )


def count_llm_requests(user_id: str, since: str = "") -> int:
    with _connect() as conn:
        _require_active_user(conn, user_id)
        if since:
            return int(conn.execute(
                "SELECT COUNT(*) FROM llm_usage WHERE user_id = ? AND event_type = 'request' AND created_at >= ?",
                (user_id, since),
            ).fetchone()[0])
        return int(conn.execute(
            "SELECT COUNT(*) FROM llm_usage WHERE user_id = ? AND event_type = 'request'", (user_id,)
        ).fetchone()[0])


def add_fact(user_id: str, data: dict) -> str:
    fact_id = data.get("id") or f"CF-{uuid.uuid4().hex[:8].upper()}"
    with _connect() as conn:
        _require_active_user(conn, user_id)
        if data.get("source_resume_id"):
            _require_owned(conn, "resumes", data["source_resume_id"], user_id)
        if data.get("case_id"):
            _require_owned(conn, "experience_cases", data["case_id"], user_id)
        conn.execute(
            """INSERT INTO career_facts
            (id, user_id, organization, official_job_title, fact_type, statement, skills,
             verification_status, source, restrictions, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                fact_id,
                user_id,
                data.get("organization", "").strip(),
                data.get("official_job_title", "").strip(),
                data["fact_type"],
                data["statement"].strip(),
                json.dumps(data.get("skills", []), ensure_ascii=False),
                data.get("verification_status", "confirmed"),
                data.get("source", "用户手动录入").strip(),
                data.get("restrictions", "").strip(),
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        if data.get("source_resume_id") or data.get("source_excerpt"):
            conn.execute(
                "UPDATE career_facts SET source_resume_id = ?, source_excerpt = ? WHERE id = ? AND user_id = ?",
                (data.get("source_resume_id"), data.get("source_excerpt", ""), fact_id, user_id),
            )
        if not data.get("allowed_for_generation", True):
            conn.execute("UPDATE career_facts SET allowed_for_generation = 0 WHERE id = ? AND user_id = ?", (fact_id, user_id))
        normalized_key = data.get("normalized_key") or _fact_key(data)
        conn.execute(
            "UPDATE career_facts SET evidence_origin = ?, evidence_kind = ?, normalized_key = ? WHERE id = ? AND user_id = ?",
            (data.get("evidence_origin", "user_confirmed"), data.get("evidence_kind", "direct"), normalized_key, fact_id, user_id),
        )
        conn.execute(
            "UPDATE career_facts SET education_json = ? WHERE id = ? AND user_id = ?",
            (json.dumps(data.get("education", {}), ensure_ascii=False), fact_id, user_id),
        )
        conn.execute(
            """UPDATE career_facts SET case_id = ?, evidence_type = ?, project_name = ?, role = ?,
            date_range = ?, context = ? WHERE id = ? AND user_id = ?""",
            (
                data.get("case_id"), data.get("evidence_type") or _infer_evidence_type(data.get("fact_type", "")),
                data.get("project_name", "").strip(), data.get("role", "").strip(),
                data.get("date_range", "").strip(), data.get("context", "").strip(), fact_id, user_id,
            ),
        )
        if data.get("source_resume_id") or data.get("source_excerpt"):
            _add_fact_source(
                conn, user_id, fact_id, data.get("source_resume_id"), data.get("source_excerpt", ""),
                data.get("evidence_origin", "user_confirmed"),
            )
        _reconcile_fact_sources(conn, user_id, [fact_id])
        if data.get("verification_status", "confirmed") == "confirmed":
            _touch_career_truth(conn, user_id)
    return fact_id


def _infer_evidence_type(fact_type: str) -> str:
    value = (fact_type or "").lower()
    if any(term in value for term in ("教育", "学历")):
        return "Education"
    if "项目" in value:
        return "Project"
    if "成果" in value:
        return "Achievement"
    if any(term in value for term in ("技能", "证书", "语言")):
        return "Skill"
    if any(term in value for term in ("工作身份", "实际职责")):
        return "Employment"
    return "Other"


def add_experience_case(user_id: str, data: dict) -> str:
    case_type = data.get("case_type", "Other")
    if case_type == "Employment" and (not data.get("organization", "").strip() or not data.get("official_job_title", "").strip()):
        raise ValueError("Employment Case 必须包含公司和正式 Job Title")
    if case_type != "Employment":
        data = {**data, "organization": "", "official_job_title": ""}
    case_id = data.get("id") or f"CASE-{uuid.uuid4().hex[:8].upper()}"
    now = datetime.now().isoformat(timespec="seconds")
    with _connect() as conn:
        _require_active_user(conn, user_id)
        conn.execute(
            """INSERT INTO experience_cases
            (id, user_id, case_type, title, summary, skills_json, source_answer, requirement_id,
             verification_status, created_at, updated_at, organization, official_job_title,
             project_name, role, date_range, context)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'confirmed', ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                case_id, user_id, data.get("case_type", "Other"), data["title"].strip(), data["summary"].strip(),
                json.dumps(data.get("skills", []), ensure_ascii=False), data.get("source_answer", "").strip(),
                data.get("requirement_id", ""), now, now,
                data.get("organization", "").strip(), data.get("official_job_title", "").strip(),
                data.get("project_name", "").strip(), data.get("role", "").strip(),
                data.get("date_range", "").strip(), data.get("context", "").strip(),
            ),
        )
    return case_id


def get_experience_case(user_id: str, case_id: str) -> dict | None:
    with _connect() as conn:
        _require_active_user(conn, user_id)
        row = conn.execute("SELECT * FROM experience_cases WHERE id = ? AND user_id = ?", (case_id, user_id)).fetchone()
    if not row:
        return None
    item = dict(row)
    item["skills"] = json.loads(item.pop("skills_json") or "[]")
    return item


def _fact_key(data: dict) -> str:
    parts = [data.get("organization", ""), data.get("official_job_title", ""), data.get("statement", "")]
    return "|".join(" ".join(str(part).lower().split()) for part in parts)


def _add_fact_source(conn: sqlite3.Connection, user_id: str, fact_id: str, resume_id: str | None, excerpt: str, source_type: str) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO fact_sources
        (user_id, fact_id, resume_id, source_excerpt, source_type, created_at) VALUES (?, ?, ?, ?, ?, ?)""",
        (user_id, fact_id, resume_id, excerpt, source_type, datetime.now().isoformat(timespec="seconds")),
    )


def _fact_has_valid_source(conn: sqlite3.Connection, user_id: str, fact_id: str) -> bool:
    fact = conn.execute(
        "SELECT evidence_origin, verification_status FROM career_facts WHERE id = ? AND user_id = ?", (fact_id, user_id)
    ).fetchone()
    if not fact:
        return False
    # A fact explicitly confirmed by the user is an independent valid source.
    if fact["verification_status"] == "confirmed" and fact["evidence_origin"] == "user_confirmed":
        return True
    return bool(conn.execute(
        """SELECT 1 FROM fact_sources fs
        JOIN resumes r ON r.id = fs.resume_id
        WHERE fs.fact_id = ? AND fs.user_id = ? AND r.user_id = ? LIMIT 1""",
        (fact_id, user_id, user_id),
    ).fetchone())


def _reconcile_fact_sources(conn: sqlite3.Connection, user_id: str, fact_ids: list[str]) -> None:
    for fact_id in set(fact_ids):
        valid = int(_fact_has_valid_source(conn, user_id, fact_id))
        conn.execute("UPDATE career_facts SET source_valid = ? WHERE id = ? AND user_id = ?", (valid, fact_id, user_id))
        if not valid:
            conn.execute("UPDATE career_facts SET allowed_for_generation = 0 WHERE id = ? AND user_id = ?", (fact_id, user_id))


def _reconcile_all_fact_sources(conn: sqlite3.Connection, user_id: str) -> None:
    _reconcile_fact_sources(conn, user_id, [row["id"] for row in conn.execute(
        "SELECT id FROM career_facts WHERE user_id = ?", (user_id,)
    ).fetchall()])


def _invalidate_resume_evidence(conn: sqlite3.Connection, user_id: str, resume_id: str) -> None:
    affected = [row["fact_id"] for row in conn.execute(
        "SELECT DISTINCT fact_id FROM fact_sources WHERE resume_id = ? AND user_id = ?", (resume_id, user_id)
    ).fetchall()]
    conn.execute("DELETE FROM fact_sources WHERE resume_id = ? AND user_id = ?", (resume_id, user_id))
    conn.execute("UPDATE career_facts SET source_resume_id = NULL WHERE source_resume_id = ? AND user_id = ?", (resume_id, user_id))
    conn.execute("DELETE FROM resume_ingestions WHERE resume_id = ? AND user_id = ?", (resume_id, user_id))
    _reconcile_fact_sources(conn, user_id, affected)


def add_or_merge_resume_fact(user_id: str, data: dict, resume_id: str) -> tuple[str, bool]:
    """Merge only exact normalized identity+statement matches; never merge conflicts."""
    key = _fact_key(data)
    with _connect() as conn:
        _require_active_user(conn, user_id)
        _require_owned(conn, "resumes", resume_id, user_id)
        row = conn.execute("SELECT id FROM career_facts WHERE normalized_key = ? AND user_id = ?", (key, user_id)).fetchone()
        if row:
            _add_fact_source(conn, user_id, row["id"], resume_id, data.get("source_excerpt", ""), "resume_declared")
            conn.execute(
                """UPDATE career_facts SET source_valid = 1,
                verification_status = CASE WHEN evidence_origin = 'user_confirmed' THEN verification_status ELSE 'confirmed' END,
                allowed_for_generation = CASE WHEN evidence_kind = 'negative' THEN 0 ELSE 1 END
                WHERE id = ? AND user_id = ?""",
                (row["id"], user_id),
            )
            _touch_career_truth(conn, user_id)
            return row["id"], False
        conflict = _find_resume_fact_conflict(conn, user_id, data)
    payload = dict(data)
    payload.update(source_resume_id=resume_id, normalized_key=key)
    if conflict:
        payload.update(
            evidence_origin="ai_candidate",
            verification_status="needs_confirmation",
            allowed_for_generation=False,
            restrictions=(payload.get("restrictions", "") + f" 与 {conflict['id']} 存在潜在冲突，需用户确认。 ").strip(),
        )
    else:
        payload.update(
            evidence_origin="resume_declared",
            verification_status="confirmed",
            allowed_for_generation=True,
        )
    return add_fact(user_id, payload), True


def add_or_reuse_candidate_fact(user_id: str, data: dict, resume_id: str) -> tuple[str, bool]:
    """Reuse an identical candidate/confirmed claim instead of asking again after re-ingestion."""
    key = data.get("normalized_key") or _fact_key(data)
    with _connect() as conn:
        _require_active_user(conn, user_id)
        _require_owned(conn, "resumes", resume_id, user_id)
        excerpt = data.get("source_excerpt", "").strip()
        if excerpt:
            source_row = conn.execute(
                "SELECT fact_id FROM fact_sources WHERE resume_id = ? AND source_excerpt = ? AND user_id = ? ORDER BY id LIMIT 1",
                (resume_id, excerpt, user_id),
            ).fetchone()
            if source_row:
                return source_row["fact_id"], False
        row = conn.execute(
            "SELECT id, verification_status, evidence_origin FROM career_facts WHERE normalized_key = ? AND user_id = ?",
            (key, user_id),
        ).fetchone()
        if row:
            _add_fact_source(conn, user_id, row["id"], resume_id, data.get("source_excerpt", ""), "ai_candidate")
            _reconcile_fact_sources(conn, user_id, [row["id"]])
            _touch_career_truth(conn, user_id)
            return row["id"], False
    return add_fact(user_id, {**data, "source_resume_id": resume_id, "normalized_key": key}), True


def add_or_merge_user_confirmed_fact(user_id: str, data: dict) -> tuple[str, bool]:
    """An identical User Claim is confirmed once and then reused across every JD."""
    key = data.get("normalized_key") or _fact_key(data)
    with _connect() as conn:
        _require_active_user(conn, user_id)
        row = conn.execute("SELECT id FROM career_facts WHERE normalized_key = ? AND user_id = ?", (key, user_id)).fetchone()
        if row:
            conn.execute(
                """UPDATE career_facts SET verification_status = 'confirmed', evidence_origin = 'user_confirmed',
                source_valid = 1, allowed_for_generation = CASE WHEN evidence_kind = 'negative' THEN 0 ELSE 1 END
                WHERE id = ? AND user_id = ?""",
                (row["id"], user_id),
            )
            _touch_career_truth(conn, user_id)
            return row["id"], False
    return add_fact(user_id, {**data, "normalized_key": key, "verification_status": "confirmed",
                     "evidence_origin": "user_confirmed"}), True


def _claim_skeleton(text: str) -> str:
    """Remove values so differently stated versions of the same measurable claim can be compared."""
    return re.sub(r"\d+(?:\.\d+)?%?|[一二三四五六七八九十百千万]+", "#", "".join(text.lower().split()))


def _find_resume_fact_conflict(conn: sqlite3.Connection, user_id: str, data: dict) -> sqlite3.Row | None:
    rows = conn.execute(
        "SELECT id, organization, official_job_title, fact_type, statement FROM career_facts WHERE verification_status = 'confirmed' AND user_id = ?",
        (user_id,),
    ).fetchall()
    organization = "".join(data.get("organization", "").lower().split())
    title = "".join(data.get("official_job_title", "").lower().split())
    statement = data.get("statement", "")
    for row in rows:
        if "".join(row["organization"].lower().split()) != organization:
            continue
        old_title = "".join(row["official_job_title"].lower().split())
        if data.get("fact_type") == "工作身份" and old_title and title and old_title != title:
            return row
        old_statement = row["statement"]
        if (
            row["fact_type"] == data.get("fact_type")
            and re.search(r"\d", old_statement)
            and re.search(r"\d", statement)
            and _claim_skeleton(old_statement) == _claim_skeleton(statement)
            and old_statement != statement
        ):
            return row
    return None


def list_facts(user_id: str, confirmed_only: bool = False, generation_only: bool = False) -> list[dict]:
    sql = "SELECT * FROM career_facts"
    conditions = ["user_id = ?"]
    params: list = [user_id]
    if confirmed_only:
        conditions.append("verification_status = ?")
        params.append("confirmed")
        conditions.append("source_valid = 1")
    if generation_only:
        conditions.append("allowed_for_generation = 1")
        conditions.append("source_valid = 1")
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    sql += " ORDER BY created_at DESC"
    with _connect() as conn:
        _require_active_user(conn, user_id)
        rows = conn.execute(sql, tuple(params)).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["skills"] = json.loads(item["skills"] or "[]")
        item["education"] = json.loads(item.get("education_json") or "{}")
        with _connect() as conn:
            item["sources"] = [
                dict(source) for source in conn.execute(
                    "SELECT resume_id, source_excerpt, source_type FROM fact_sources WHERE fact_id = ? AND user_id = ? ORDER BY id",
                    (item["id"], user_id),
                ).fetchall()
            ]
        result.append(item)
    return result


def delete_fact(user_id: str, fact_id: str) -> None:
    with _connect() as conn:
        _require_active_user(conn, user_id)
        previous = conn.execute(
            "SELECT verification_status FROM career_facts WHERE id = ? AND user_id = ?", (fact_id, user_id)
        ).fetchone()
        conn.execute("DELETE FROM fact_sources WHERE fact_id = ? AND user_id = ?", (fact_id, user_id))
        if (conn.execute("DELETE FROM career_facts WHERE id = ? AND user_id = ?", (fact_id, user_id)).rowcount
                and previous and previous["verification_status"] == "confirmed"):
            _touch_career_truth(conn, user_id)


def update_fact_status(user_id: str, fact_id: str, status: str) -> None:
    if status not in {"confirmed", "needs_confirmation", "rejected"}:
        raise ValueError("Unsupported fact status")
    with _connect() as conn:
        _require_active_user(conn, user_id)
        previous = conn.execute(
            "SELECT verification_status FROM career_facts WHERE id = ? AND user_id = ?", (fact_id, user_id)
        ).fetchone()
        if (conn.execute("UPDATE career_facts SET verification_status = ? WHERE id = ? AND user_id = ?", (status, fact_id, user_id)).rowcount
                and previous and (previous["verification_status"] == "confirmed" or status == "confirmed")):
            _touch_career_truth(conn, user_id)


def confirm_edited_fact(user_id: str, fact_id: str, organization: str, official_job_title: str, statement: str, skills: list[str], restrictions: str = "") -> None:
    payload = {"organization": organization, "official_job_title": official_job_title, "statement": statement}
    with _connect() as conn:
        _require_active_user(conn, user_id)
        conn.execute(
            """UPDATE career_facts SET organization = ?, official_job_title = ?, statement = ?, skills = ?,
            restrictions = ?, verification_status = 'confirmed', evidence_origin = 'user_confirmed', normalized_key = ?,
            allowed_for_generation = CASE WHEN evidence_kind = 'negative' THEN 0 ELSE 1 END, source_valid = 1
            WHERE id = ? AND user_id = ?""",
            (
                organization.strip(), official_job_title.strip(), statement.strip(),
                json.dumps(skills, ensure_ascii=False), restrictions.strip(), _fact_key(payload), fact_id, user_id,
            ),
        )
        _touch_career_truth(conn, user_id)


def add_resume(user_id: str, name: str, content_text: str, language: str = "auto", original_filename: str = "", original_file_path: str = "") -> str:
    resume_id = f"RES-{uuid.uuid4().hex[:8].upper()}"
    now = datetime.now().isoformat(timespec="seconds")
    with _connect() as conn:
        _require_active_user(conn, user_id)
        conn.execute(
            """INSERT INTO resumes
            (id, user_id, name, language, original_filename, original_file_path, content_text, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (resume_id, user_id, name.strip(), language, original_filename, original_file_path, content_text.strip(), now, now),
        )
    return resume_id


def list_resumes(user_id: str) -> list[dict]:
    with _connect() as conn:
        _require_active_user(conn, user_id)
        return [dict(row) for row in conn.execute("SELECT * FROM resumes WHERE user_id = ? ORDER BY updated_at DESC", (user_id,)).fetchall()]


def get_resume(user_id: str, resume_id: str) -> dict | None:
    with _connect() as conn:
        _require_active_user(conn, user_id)
        row = conn.execute("SELECT * FROM resumes WHERE id = ? AND user_id = ?", (resume_id, user_id)).fetchone()
    return dict(row) if row else None


def update_resume(user_id: str, resume_id: str, name: str, content_text: str, language: str, original_filename: str | None = None, original_file_path: str | None = None) -> None:
    fields = ["name = ?", "content_text = ?", "language = ?", "updated_at = ?"]
    values: list = [name.strip(), content_text.strip(), language, datetime.now().isoformat(timespec="seconds")]
    if original_filename is not None:
        fields.append("original_filename = ?")
        values.append(original_filename)
    if original_file_path is not None:
        fields.append("original_file_path = ?")
        values.append(original_file_path)
    values.extend([resume_id, user_id])
    with _connect() as conn:
        _require_active_user(conn, user_id)
        previous = conn.execute("SELECT content_text FROM resumes WHERE id = ? AND user_id = ?", (resume_id, user_id)).fetchone()
        conn.execute(f"UPDATE resumes SET {', '.join(fields)} WHERE id = ? AND user_id = ?", values)
        if previous and previous["content_text"] != content_text.strip():
            _invalidate_resume_evidence(conn, user_id, resume_id)
            _touch_career_truth(conn, user_id)


def delete_resume(user_id: str, resume_id: str) -> None:
    with _connect() as conn:
        _require_active_user(conn, user_id)
        if not conn.execute("SELECT 1 FROM resumes WHERE id = ? AND user_id = ?", (resume_id, user_id)).fetchone():
            return
        _invalidate_resume_evidence(conn, user_id, resume_id)
        conn.execute("DELETE FROM resumes WHERE id = ? AND user_id = ?", (resume_id, user_id))
        _touch_career_truth(conn, user_id)


def resume_needs_ingestion(user_id: str, resume_id: str, content_hash: str, parser_version: str) -> bool:
    with _connect() as conn:
        _require_active_user(conn, user_id)
        row = conn.execute(
            "SELECT content_hash, parser_version FROM resume_ingestions WHERE resume_id = ? AND user_id = ?", (resume_id, user_id)
        ).fetchone()
    return not row or row["content_hash"] != content_hash or row["parser_version"] != parser_version


def mark_resume_ingested(user_id: str, resume_id: str, content_hash: str, parser_version: str) -> None:
    with _connect() as conn:
        _require_owned(conn, "resumes", resume_id, user_id)
        conn.execute(
            """INSERT INTO resume_ingestions (resume_id, user_id, content_hash, parser_version, parsed_at)
            VALUES (?, ?, ?, ?, ?) ON CONFLICT(resume_id) DO UPDATE SET
            user_id=excluded.user_id, content_hash=excluded.content_hash, parser_version=excluded.parser_version, parsed_at=excluded.parsed_at""",
            (resume_id, user_id, content_hash, parser_version, datetime.now().isoformat(timespec="seconds")),
        )


def save_application(user_id: str, job_title: str, company: str, jd_text: str, analysis: dict, base_resume_id: str | None = None) -> str:
    app_id = f"JOB-{uuid.uuid4().hex[:8].upper()}"
    now = datetime.now().isoformat(timespec="seconds")
    with _connect() as conn:
        _require_active_user(conn, user_id)
        if base_resume_id:
            _require_owned(conn, "resumes", base_resume_id, user_id)
        conn.execute(
            """INSERT INTO job_applications
            (id, user_id, job_title, company, jd_text, analysis_json, base_resume_id, created_at, updated_at,
             application_status, evidence_revision, requirement_state_json, eligibility_state_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'READY_FOR_STRATEGY', ?, ?, ?)""",
            (app_id, user_id, job_title, company, jd_text, json.dumps(analysis, ensure_ascii=False), base_resume_id,
             now, now, _career_revision(conn, user_id),
             json.dumps({"requirements": analysis.get("requirements", [])}, ensure_ascii=False),
             json.dumps({"requirements": [r for r in analysis.get("requirements", []) if r.get("nature") == "eligibility"]}, ensure_ascii=False)),
        )
    return app_id


def create_application_workspace(
    user_id: str, target_role: str, target_company: str, jd_text: str,
    base_resume_id: str, jd_source_type: str = "text", jd_source: str = "",
) -> str:
    app_id = f"JOB-{uuid.uuid4().hex[:8].upper()}"
    now = datetime.now().isoformat(timespec="seconds")
    with _connect() as conn:
        _require_active_user(conn, user_id)
        _require_owned(conn, "resumes", base_resume_id, user_id)
        conn.execute(
            """INSERT INTO job_applications
            (id, user_id, job_title, company, jd_text, base_resume_id, jd_source_type, jd_source,
             application_status, evidence_revision, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'JD_READY', ?, ?, ?)""",
            (app_id, user_id, target_role.strip() or "未命名岗位", target_company.strip() or "未填写公司",
             jd_text.strip(), base_resume_id, jd_source_type, jd_source,
             _career_revision(conn, user_id), now, now),
        )
    return app_id


def get_application(user_id: str, app_id: str) -> dict | None:
    with _connect() as conn:
        _require_active_user(conn, user_id)
        row = conn.execute("SELECT * FROM job_applications WHERE id = ? AND user_id = ?", (app_id, user_id)).fetchone()
    return dict(row) if row else None


def list_applications(user_id: str) -> list[dict]:
    with _connect() as conn:
        _require_active_user(conn, user_id)
        rows = conn.execute(
            """SELECT id, job_title, company, application_status, template_id, created_at, updated_at
            FROM job_applications WHERE user_id = ? ORDER BY updated_at DESC""", (user_id,)
        ).fetchall()
    return [dict(row) for row in rows]


_APPLICATION_JSON_FIELDS = {
    "analysis_json", "resume_json", "requirement_state_json", "eligibility_state_json",
    "deep_dive_state_json", "strategy_json", "final_resume_json", "preflight_json", "draft_review_json",
    "content_controls_json",
}
_APPLICATION_FIELDS = _APPLICATION_JSON_FIELDS | {
    "job_title", "company", "jd_source_type", "jd_source", "jd_text", "base_resume_id",
    "user_preference_prompt", "template_id", "application_status",
    "strategy_approved_at", "final_approved_at", "evidence_revision",
}


def update_application_fields(user_id: str, app_id: str, **fields) -> None:
    unknown = set(fields) - _APPLICATION_FIELDS
    if unknown:
        raise ValueError("不支持的Application字段：" + ",".join(sorted(unknown)))
    if not fields:
        return
    values = []
    assignments = []
    for key, value in fields.items():
        assignments.append(f"{key} = ?")
        values.append(json.dumps(value, ensure_ascii=False) if key in _APPLICATION_JSON_FIELDS and not isinstance(value, str) else value)
    assignments.append("updated_at = ?")
    values.extend([datetime.now().isoformat(timespec="seconds"), app_id, user_id])
    with _connect() as conn:
        _require_active_user(conn, user_id)
        if not conn.execute("SELECT 1 FROM job_applications WHERE id = ? AND user_id = ?", (app_id, user_id)).fetchone():
            raise PermissionError("Application不存在或不属于当前用户")
        conn.execute(f"UPDATE job_applications SET {', '.join(assignments)} WHERE id = ? AND user_id = ?", values)


def get_application_workspace(user_id: str, app_id: str) -> dict | None:
    item = get_application(user_id, app_id)
    if not item:
        return None
    for field in _APPLICATION_JSON_FIELDS:
        raw = item.get(field)
        try:
            item[field.removesuffix("_json")] = json.loads(raw or "{}")
        except (TypeError, json.JSONDecodeError):
            item[field.removesuffix("_json")] = {}
    return item


def save_resume(user_id: str, app_id: str, resume: dict) -> None:
    with _connect() as conn:
        _require_active_user(conn, user_id)
        conn.execute(
            "UPDATE job_applications SET resume_json = ?, updated_at = ? WHERE id = ? AND user_id = ?",
            (json.dumps(resume, ensure_ascii=False), datetime.now().isoformat(timespec="seconds"), app_id, user_id),
        )


def update_application_analysis(user_id: str, app_id: str, analysis: dict) -> None:
    with _connect() as conn:
        _require_active_user(conn, user_id)
        conn.execute(
            "UPDATE job_applications SET analysis_json = ?, updated_at = ? WHERE id = ? AND user_id = ?",
            (json.dumps(analysis, ensure_ascii=False), datetime.now().isoformat(timespec="seconds"), app_id, user_id),
        )


def update_content_controls(user_id: str, app_id: str, controls: dict) -> None:
    with _connect() as conn:
        _require_active_user(conn, user_id)
        conn.execute(
            "UPDATE job_applications SET content_controls_json = ?, updated_at = ? WHERE id = ? AND user_id = ?",
            (json.dumps(controls, ensure_ascii=False), datetime.now().isoformat(timespec="seconds"), app_id, user_id),
        )


def update_user_preference(user_id: str, app_id: str, preference: str) -> None:
    with _connect() as conn:
        _require_active_user(conn, user_id)
        conn.execute(
            "UPDATE job_applications SET user_preference_prompt = ?, updated_at = ? WHERE id = ? AND user_id = ?",
            (preference.strip(), datetime.now().isoformat(timespec="seconds"), app_id, user_id),
        )
