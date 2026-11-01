"""Excel application trackers used as the human source of truth.

Primary apply history: Daily_Job_Application_Tracker.xlsx
LinkedIn outreach leads: LinkedIn_Hiring_Post_Outreach.xlsx
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from applypilot import config

log = logging.getLogger(__name__)

DAILY_HEADERS = [
    "Date Found",
    "Company",
    "Role",
    "Portal",
    "Job URL",
    "Work Type",
    "Location",
    "Experience Required",
    "Match Notes",
    "Status",
    "Date Submitted",
    "Confirmation / Evidence",
    "Next Action",
    "Follow-up Date",
]

# Statuses that mean: do not apply again
_DONE_PREFIXES = (
    "submitted",
    "applied",
)


def _resolve_path(configured: str | Path | None, default_name: str) -> Path:
    """Resolve a tracker path from profile config, userdata/trackers, APP_DIR, or cwd."""
    candidates: list[Path] = []
    if configured:
        raw = Path(str(configured)).expanduser()
        candidates.append(raw if raw.is_absolute() else config.APP_DIR / raw)
        candidates.append(Path.cwd() / raw)
    candidates.extend(
        [
            config.APP_DIR / "userdata" / "trackers" / default_name,
            config.APP_DIR / "userdata" / default_name,
            config.APP_DIR / default_name,
            Path.cwd() / "userdata" / "trackers" / default_name,
            Path.cwd() / "userdata" / default_name,
            Path.cwd() / default_name,
        ]
    )
    for path in candidates:
        if path.exists():
            return path.resolve()
    return (config.APP_DIR / "userdata" / "trackers" / default_name).resolve()


def daily_tracker_path() -> Path:
    try:
        profile = config.load_profile()
    except FileNotFoundError:
        profile = {}
    prefs = profile.get("job_preferences", {})
    return _resolve_path(
        prefs.get("daily_tracker_path") or prefs.get("applications_tracker_path"),
        "Daily_Job_Application_Tracker.xlsx",
    )


def linkedin_tracker_path() -> Path:
    try:
        profile = config.load_profile()
    except FileNotFoundError:
        profile = {}
    prefs = profile.get("job_preferences", {})
    return _resolve_path(
        prefs.get("linkedin_tracker_path"),
        "LinkedIn_Hiring_Post_Outreach.xlsx",
    )


def normalize_url(url: str | None) -> str:
    if not url:
        return ""
    text = str(url).strip()
    if not text:
        return ""
    parts = urlsplit(text)
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, "", ""))


def _status_means_done(status: str | None) -> bool:
    if not status:
        return False
    lowered = str(status).strip().lower()
    return any(lowered.startswith(prefix) for prefix in _DONE_PREFIXES)


def _cell_date(value) -> date | datetime | str | None:
    if value is None or value == "":
        return None
    if isinstance(value, (datetime, date)):
        return value
    return str(value)


def load_daily_rows(path: Path | None = None) -> list[dict]:
    """Load all non-empty rows from the Daily Application Tracker."""
    from openpyxl import load_workbook

    tracker = path or daily_tracker_path()
    if not tracker.exists():
        log.warning("Daily tracker not found: %s", tracker)
        return []

    wb = load_workbook(tracker, read_only=True, data_only=True)
    try:
        ws = wb["Application Tracker"] if "Application Tracker" in wb.sheetnames else wb.active
        rows = list(ws.iter_rows(values_only=True))
    finally:
        wb.close()

    if not rows:
        return []

    # Find header row (first row that contains Job URL)
    header_idx = 0
    for i, row in enumerate(rows[:10]):
        vals = [str(c).strip() if c is not None else "" for c in row]
        if "Job URL" in vals:
            header_idx = i
            break

    header = [str(c).strip() if c is not None else "" for c in rows[header_idx]]
    index = {name: i for i, name in enumerate(header) if name}

    out: list[dict] = []
    for row in rows[header_idx + 1 :]:
        if not row or not any(row):
            continue

        def get(col: str):
            i = index.get(col)
            return row[i] if i is not None and i < len(row) else None

        job_url = get("Job URL")
        if not job_url:
            continue
        out.append(
            {
                "date_found": get("Date Found"),
                "company": get("Company"),
                "role": get("Role"),
                "portal": get("Portal"),
                "job_url": str(job_url).strip(),
                "work_type": get("Work Type"),
                "location": get("Location"),
                "experience": get("Experience Required"),
                "match_notes": get("Match Notes"),
                "status": get("Status"),
                "date_submitted": get("Date Submitted"),
                "evidence": get("Confirmation / Evidence"),
                "next_action": get("Next Action"),
                "follow_up": get("Follow-up Date"),
            }
        )
    return out


def already_applied_urls(path: Path | None = None) -> set[str]:
    """URLs already submitted/applied in the Daily tracker (normalized)."""
    urls: set[str] = set()
    for row in load_daily_rows(path):
        if _status_means_done(row.get("status")):
            norm = normalize_url(row.get("job_url"))
            if norm:
                urls.add(norm)
            raw = (row.get("job_url") or "").strip()
            if raw:
                urls.add(raw)
    return urls


def all_tracked_urls(path: Path | None = None) -> set[str]:
    """All Job URLs present in the Daily tracker (any status)."""
    urls: set[str] = set()
    for row in load_daily_rows(path):
        raw = (row.get("job_url") or "").strip()
        norm = normalize_url(raw)
        if raw:
            urls.add(raw)
        if norm:
            urls.add(norm)
    return urls


def append_daily_application(
    *,
    company: str,
    role: str,
    portal: str,
    job_url: str,
    work_type: str = "",
    location: str = "",
    experience: str = "",
    match_notes: str = "",
    status: str = "Submitted — confirmed",
    evidence: str = "",
    next_action: str = "Monitor employer response",
    path: Path | None = None,
) -> bool:
    """Append one application row to the Daily tracker. Returns False if duplicate URL."""
    from openpyxl import load_workbook

    tracker = path or daily_tracker_path()
    if not tracker.exists():
        log.error("Cannot append; daily tracker missing: %s", tracker)
        return False

    norm = normalize_url(job_url)
    for row in load_daily_rows(tracker):
        if normalize_url(row.get("job_url")) == norm and _status_means_done(row.get("status")):
            log.info("Daily tracker already has submitted URL: %s", job_url[:80])
            return False

    wb = load_workbook(tracker)
    ws = wb["Application Tracker"] if "Application Tracker" in wb.sheetnames else wb.active

    # Ensure header exists
    first = [cell.value for cell in next(ws.iter_rows(min_row=1, max_row=1))]
    if "Job URL" not in [str(v) if v is not None else "" for v in first]:
        ws.append(DAILY_HEADERS)

    today = datetime.now().date()
    ws.append(
        [
            today,
            company,
            role,
            portal,
            job_url,
            work_type,
            location,
            experience,
            match_notes,
            status,
            today if status.lower().startswith(("submitted", "applied")) else None,
            evidence,
            next_action,
            None,
        ]
    )
    wb.save(tracker)
    wb.close()
    log.info("Appended to daily tracker: %s | %s", company, role)
    return True


def import_applied_into_db(path: Path | None = None) -> dict:
    """Mark matching DB jobs as applied when the Daily tracker says submitted."""
    from applypilot.database import get_connection, init_db

    init_db()
    conn = get_connection()
    applied = already_applied_urls(path)
    if not applied:
        return {"matched": 0, "updated": 0, "tracker_applied": 0}

    rows = conn.execute(
        "SELECT url, application_url, apply_status, applied_at FROM jobs"
    ).fetchall()
    updated = 0
    matched = 0
    now = datetime.now().astimezone().isoformat(timespec="seconds")

    for row in rows:
        candidates = {normalize_url(row["url"]), (row["url"] or "").strip()}
        if row["application_url"]:
            candidates.add(normalize_url(row["application_url"]))
            candidates.add(str(row["application_url"]).strip())
        candidates.discard("")
        if not candidates.intersection(applied):
            continue
        matched += 1
        if row["applied_at"] and row["apply_status"] == "applied":
            continue
        conn.execute(
            """
            UPDATE jobs
            SET apply_status = 'applied',
                applied_at = COALESCE(applied_at, ?),
                apply_error = COALESCE(apply_error, 'Imported from Daily_Job_Application_Tracker.xlsx')
            WHERE url = ?
            """,
            (now, row["url"]),
        )
        updated += 1
    conn.commit()
    return {
        "matched": matched,
        "updated": updated,
        "tracker_applied": len(applied),
    }


def skip_urls_for_apply() -> set[str]:
    """URLs ApplyPilot should not re-queue (already submitted/applied in Excel)."""
    return already_applied_urls()
