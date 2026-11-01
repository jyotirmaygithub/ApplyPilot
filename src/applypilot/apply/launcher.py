"""Apply orchestration: acquire jobs, spawn Claude Code sessions, track results.

This is the main entry point for the apply pipeline. It pulls jobs from
the database, launches Chrome + Claude Code for each one, parses the
result, and updates the database. Supports parallel workers via --workers.
"""

import json
import logging
import signal
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path

from rich.console import Console
from rich.live import Live

from applypilot import config
from applypilot.apply import prompt as prompt_mod
from applypilot.apply.chrome import (
    BASE_CDP_PORT,
    ChromeStartupError,
    cleanup_worker,
    kill_all_chrome,
    launch_chrome,
    reset_worker_dir,
)
from applypilot.apply.dashboard import (
    add_event,
    get_state,
    get_totals,
    init_worker,
    render_full,
    update_state,
)
from applypilot.database import application_queue_filter, get_connection, get_stats
from applypilot.progress import ApplicationProgress, job_details, log_saved_counts, model_details

logger = logging.getLogger(__name__)

# Blocked sites loaded from config/sites.yaml
def _load_blocked():
    from applypilot.config import load_blocked_sites
    return load_blocked_sites()

# How often to poll the DB when the queue is empty (seconds)
POLL_INTERVAL = config.DEFAULTS["poll_interval"]

# Thread-safe shutdown coordination
_stop_event = threading.Event()
_skipped_urls: set[str] = set()
_skipped_lock = threading.Lock()

# ---------------------------------------------------------------------------
# MCP config
# ---------------------------------------------------------------------------

def _make_mcp_config(cdp_port: int) -> dict:
    """Build MCP config dict for a specific CDP port."""
    return {
        "mcpServers": {
            "playwright": {
                "command": "npx",
                "args": [
                    "-y", "@playwright/mcp@latest",
                    f"--cdp-endpoint=http://localhost:{cdp_port}",
                    f"--viewport-size={config.DEFAULTS['viewport']}",
                ],
            },
            "gmail": {
                "command": "npx",
                "args": ["-y", "@gongrzhe/server-gmail-autoauth-mcp"],
            },
        }
    }


# ---------------------------------------------------------------------------
# Database operations
# ---------------------------------------------------------------------------

def acquire_job(target_url: str | None = None, min_score: int = 7,
                worker_id: int = 0, dry_run: bool = False) -> dict | None:
    """Atomically acquire the next job to apply to.

    Args:
        target_url: Apply to a specific URL instead of picking from queue.
        min_score: Minimum fit_score threshold.
        worker_id: Worker claiming this job (for tracking).

    Returns:
        Job dict or None if the queue is empty.
    """
    conn = get_connection()
    with _skipped_lock:
        skipped = list(_skipped_urls)
    if target_url in skipped:
        return None
    try:
        conn.execute("BEGIN IMMEDIATE")

        if target_url:
            like = f"%{target_url.split('?')[0].rstrip('/')}%"
            row = conn.execute("""
                SELECT url, title, site, application_url, tailored_resume_path,
                       fit_score, location, full_description, cover_letter_path, apply_status
                FROM jobs
                WHERE (url = ? OR application_url = ? OR application_url LIKE ? OR url LIKE ?)
                  AND tailored_resume_path IS NOT NULL
                  AND COALESCE(apply_status, '') != 'in_progress'
                  AND applied_at IS NULL
                LIMIT 1
            """, (target_url, target_url, like, like)).fetchone()
        else:
            queue_where, params = application_queue_filter(
                min_score=min_score, dry_run=dry_run, skipped_urls=skipped, blocked=_load_blocked(),
            )
            row = conn.execute(f"""
                SELECT url, title, site, application_url, tailored_resume_path,
                       fit_score, location, full_description, cover_letter_path, apply_status
                FROM jobs
                WHERE {queue_where}
                ORDER BY fit_score DESC, url
                LIMIT 1
            """, params).fetchone()

        if not row:
            conn.rollback()
            return None

        # Skip manual ATS sites (unsolvable CAPTCHAs)
        from applypilot.config import is_manual_ats
        apply_url = row["application_url"] or row["url"]
        if is_manual_ats(apply_url):
            conn.execute(
                "UPDATE jobs SET apply_status = 'manual', apply_error = 'manual ATS' WHERE url = ?",
                (row["url"],),
            )
            conn.commit()
            logger.info("Skipping manual ATS: %s", row["url"][:80])
            return None

        now = datetime.now(UTC).isoformat()
        conn.execute("""
            UPDATE jobs SET apply_status = 'in_progress',
                           agent_id = ?,
                           last_attempted_at = ?
            WHERE url = ?
        """, (f"worker-{worker_id}", now, row["url"]))
        conn.commit()

        return dict(row)
    except Exception:
        conn.rollback()
        raise


def mark_result(url: str, status: str, error: str | None = None,
                permanent: bool = False, duration_ms: int | None = None,
                task_id: str | None = None) -> None:
    """Update a job's apply status in the database."""
    conn = get_connection()
    now = datetime.now(UTC).isoformat()
    if status == "applied":
        conn.execute("""
            UPDATE jobs SET apply_status = 'applied', applied_at = ?,
                           apply_error = NULL, agent_id = NULL,
                           apply_duration_ms = ?, apply_task_id = ?
            WHERE url = ?
        """, (now, duration_ms, task_id, url))
        try:
            from applypilot.tracking import append_daily_application

            row = conn.execute(
                "SELECT title, site, location, fit_score, application_url FROM jobs WHERE url = ?",
                (url,),
            ).fetchone()
            if row:
                append_daily_application(
                    company=row["site"] or "",
                    role=row["title"] or "",
                    portal=row["site"] or "",
                    job_url=row["application_url"] or url,
                    location=row["location"] or "",
                    match_notes=f"ApplyPilot fit_score={row['fit_score']}",
                    status="Submitted — confirmed",
                    evidence=f"ApplyPilot applied_at={now}",
                )
        except Exception:
            logger.exception("Failed to append applied job to Daily Excel tracker")
    elif status == "previewed":
        conn.execute("""
            UPDATE jobs SET apply_status = 'previewed', apply_error = NULL,
                            agent_id = NULL, apply_duration_ms = ?, apply_task_id = ?
            WHERE url = ?
        """, (duration_ms, task_id, url))
    else:
        attempts = 99 if permanent else "COALESCE(apply_attempts, 0) + 1"
        conn.execute(f"""
            UPDATE jobs SET apply_status = ?, apply_error = ?,
                           apply_attempts = {attempts}, agent_id = NULL,
                           apply_duration_ms = ?, apply_task_id = ?
            WHERE url = ?
        """, (status, error or "unknown", duration_ms, task_id, url))
    conn.commit()


def release_lock(url: str, previous_status: str | None = None) -> None:
    """Release the in_progress lock without changing status."""
    conn = get_connection()
    conn.execute(
        "UPDATE jobs SET apply_status = ?, agent_id = NULL WHERE url = ? AND apply_status = 'in_progress'",
        (previous_status, url),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Utility modes (--gen, --mark-applied, --mark-failed, --reset-failed)
# ---------------------------------------------------------------------------

def gen_prompt(target_url: str, min_score: int = 7,
               model: str = "haiku", worker_id: int = 0, dry_run: bool = True) -> Path | None:
    """Generate a prompt file and print the Claude CLI command for manual debugging.

    Returns:
        Path to the generated prompt file, or None if no job found.
    """
    job = acquire_job(target_url=target_url, min_score=min_score, worker_id=worker_id)
    if not job:
        return None

    # Read resume text
    resume_path = job.get("tailored_resume_path")
    txt_path = Path(resume_path).with_suffix(".txt") if resume_path else None
    resume_text = ""
    if txt_path and txt_path.exists():
        resume_text = txt_path.read_text(encoding="utf-8")

    prompt = prompt_mod.build_prompt(job=job, tailored_resume=resume_text, dry_run=dry_run,
                                     upload_dir=reset_worker_dir(worker_id) / "uploads")

    # Release the lock so the job stays available
    release_lock(job["url"], job.get("apply_status"))

    # Write prompt file
    config.ensure_dirs()
    site_slug = (job.get("site") or "unknown")[:20].replace(" ", "_")
    prompt_file = config.LOG_DIR / f"prompt_{site_slug}_{job['title'][:30].replace(' ', '_')}.txt"
    prompt_file.write_text(prompt, encoding="utf-8")

    # Write MCP config for reference
    port = BASE_CDP_PORT + worker_id
    mcp_path = config.APP_DIR / f".mcp-apply-{worker_id}.json"
    mcp_path.write_text(json.dumps(_make_mcp_config(port)), encoding="utf-8")

    return prompt_file


def mark_job(url: str, status: str, reason: str | None = None) -> None:
    """Manually mark a job's apply status in the database.

    Args:
        url: Job URL to mark.
        status: Either 'applied' or 'failed'.
        reason: Failure reason (only for status='failed').
    """
    conn = get_connection()
    now = datetime.now(UTC).isoformat()
    if status == "applied":
        conn.execute("""
            UPDATE jobs SET apply_status = 'applied', applied_at = ?,
                           apply_error = NULL, agent_id = NULL
            WHERE url = ?
        """, (now, url))
    else:
        conn.execute("""
            UPDATE jobs SET apply_status = 'failed', apply_error = ?,
                           apply_attempts = 99, agent_id = NULL
            WHERE url = ?
        """, (reason or "manual", url))
    conn.commit()


def reset_failed() -> int:
    """Reset all failed jobs so they can be retried.

    Returns:
        Number of jobs reset.
    """
    conn = get_connection()
    cursor = conn.execute("""
        UPDATE jobs SET apply_status = NULL, apply_error = NULL,
                       apply_attempts = 0, agent_id = NULL
        WHERE applied_at IS NULL
          AND apply_status NOT IN ('applied', 'previewed', 'in_progress')
    """)
    conn.commit()
    return cursor.rowcount


# ---------------------------------------------------------------------------
# Per-job execution
# ---------------------------------------------------------------------------

def _capture_review(port: int, destination: Path) -> None:
    """Save the final browser view before worker cleanup."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}", timeout=10000)
        pages = [page for context in browser.contexts for page in context.pages if page.url != "about:blank"]
        if not pages:
            raise RuntimeError("No application page available for review.")
        pages[-1].screenshot(path=str(destination), full_page=True, timeout=10000)
        browser.close()


def run_job(job: dict, port: int, worker_id: int = 0,
            model: str = "haiku", dry_run: bool = False,
            agent: str = "claude") -> tuple[str, int]:
    """Run one browser agent and classify only its terminal result."""
    from applypilot.apply.agents import build_command, result_status, stream_agent
    from applypilot.providers import redact

    start = time.monotonic()
    worker_dir = reset_worker_dir(worker_id)
    resume_path = Path(job["tailored_resume_path"]).with_suffix(".txt")
    resume_text = resume_path.read_text(encoding="utf-8") if resume_path.exists() else ""
    instructions = prompt_mod.build_prompt(
        job=job, tailored_resume=resume_text, dry_run=dry_run, upload_dir=worker_dir / "uploads",
    )
    mcp_path = config.APP_DIR / f".mcp-apply-{worker_id}.json"
    mcp_path.write_text(json.dumps(_make_mcp_config(port)), encoding="utf-8")
    command = build_command(agent, model, mcp_path, dry_run)
    update_state(worker_id, status="applying", job_title=job["title"],
                 company=job.get("site", ""), score=job.get("fit_score", 0),
                 start_time=time.time(), actions=0, last_action="starting")
    add_event(f"[W{worker_id}] Starting: {job['title'][:40]}")
    artifact_dir = config.LOG_DIR / f"{agent}_{time.time_ns()}_w{worker_id}"
    artifact_dir.mkdir(parents=True)
    terminal = None
    status = "failed:no_result_line"
    screenshot = None
    try:
        with (artifact_dir / "agent.log").open("w", encoding="utf-8") as logfile:
            for event in stream_agent(command, instructions, worker_dir, worker_id, config.DEFAULTS["apply_timeout"]):
                event_type = event.get("type")
                if event_type == "cancelled":
                    status = "skipped"
                    terminal = None
                    break
                if event_type == "assistant":
                    for block in event.get("message", {}).get("content", []):
                        if block.get("type") == "text":
                            logfile.write(redact(block.get("text", "")) + ("" if event.get("delta") else "\n"))
                        elif block.get("type") == "tool_use":
                            name = block.get("name", "").replace("mcp__playwright__", "")
                            state = get_state(worker_id)
                            update_state(worker_id, actions=(state.actions if state else 0) + 1,
                                         last_action=name[:35])
                            logfile.write(f"  >> {name}\n")
                elif event_type == "result":
                    terminal = event
        if terminal is not None:
            if terminal.get("is_error"):
                status = "failed:agent_error"
                logger.error("Agent failed: %s", redact(str(terminal.get("error", "unknown error"))))
            else:
                status = result_status(terminal.get("result", ""), dry_run)
            if terminal.get("total_cost_usd") is not None:
                state = get_state(worker_id)
                update_state(worker_id, total_cost=(state.total_cost if state else 0) + terminal["total_cost_usd"])
            else:
                update_state(worker_id, cost_available=False)
            if status in ("previewed", "applied"):
                screenshot = artifact_dir / "review.png"
                try:
                    _capture_review(port, screenshot)
                except Exception:
                    screenshot = None
                    logger.exception("Could not capture application review")
                    if dry_run:
                        status = "failed:review_capture"
    except subprocess.TimeoutExpired:
        status = "failed:timeout"
    except Exception as exc:
        logger.debug("launcher: operation failed", exc_info=True)
        logger.error("Browser agent failed: %s", redact(str(exc)))
        status = "failed:agent_error"
    finally:
        duration = int((time.monotonic() - start) * 1000)
        summary = {
            "agent": agent, "model": model, "status": status, "dry_run": dry_run,
            "duration_ms": duration, "url": job.get("application_url") or job["url"],
            "screenshot": str(screenshot) if screenshot else None,
            "usage": terminal.get("usage", {}) if terminal else {},
            "task_id": terminal.get("task_id") if terminal else None,
        }
        (artifact_dir / "summary.json").write_text(redact(json.dumps(summary, indent=2)), encoding="utf-8")
    add_event(f"[W{worker_id}] {status.upper()} ({duration // 1000}s)")
    update_state(worker_id, status=status.split(":", 1)[0], last_action=status[:35], log_file=artifact_dir)
    return status, duration


# ---------------------------------------------------------------------------
# Permanent failure classification
# ---------------------------------------------------------------------------

PERMANENT_FAILURES: set[str] = {
    "expired", "captcha", "login_issue",
    "not_eligible_location", "not_eligible_salary",
    "already_applied", "account_required",
    "not_a_job_application", "unsafe_permissions",
    "unsafe_verification", "sso_required",
    "site_blocked", "cloudflare_blocked", "blocked_by_cloudflare",
}

PERMANENT_PREFIXES: tuple[str, ...] = ("site_blocked", "cloudflare", "blocked_by")


def _is_permanent_failure(result: str) -> bool:
    """Determine if a failure should never be retried."""
    reason = result.split(":", 1)[-1] if ":" in result else result
    return (
        result in PERMANENT_FAILURES
        or reason in PERMANENT_FAILURES
        or any(reason.startswith(p) for p in PERMANENT_PREFIXES)
    )


# ---------------------------------------------------------------------------
# Worker loop
# ---------------------------------------------------------------------------

def worker_loop(worker_id: int = 0, limit: int = 1,
                target_url: str | None = None,
                min_score: int = 7, headless: bool = False,
                model: str = "haiku", dry_run: bool = False, agent: str = "claude",
                progress: ApplicationProgress | None = None) -> tuple[int, int]:
    """Run jobs sequentially until limit is reached or queue is empty.

    Args:
        worker_id: Numeric worker identifier.
        limit: Max jobs to process (0 = continuous).
        target_url: Apply to a specific URL.
        min_score: Minimum fit_score threshold.
        headless: Run Chrome headless.
        model: Claude model name.
        dry_run: Don't click Submit.

    Returns:
        Tuple of (applied_count, failed_count).
    """
    applied = 0
    failed = 0
    previewed = 0
    continuous = limit == 0
    jobs_done = 0
    empty_polls = 0
    port = BASE_CDP_PORT + worker_id
    if progress is None:
        progress = ApplicationProgress()

    while not _stop_event.is_set():
        if not continuous and jobs_done >= limit:
            break

        update_state(worker_id, status="idle", job_title="", company="",
                     last_action="waiting for job", actions=0)

        job = acquire_job(target_url=target_url, min_score=min_score,
                          worker_id=worker_id, dry_run=dry_run)
        if not job:
            if not continuous:
                add_event(f"[W{worker_id}] Queue empty")
                update_state(worker_id, status="done", last_action="queue empty")
                break
            empty_polls += 1
            update_state(worker_id, status="idle",
                         last_action=f"polling ({empty_polls})")
            if empty_polls == 1:
                add_event(f"[W{worker_id}] Queue empty, polling every {POLL_INTERVAL}s...")
            # Use Event.wait for interruptible sleep
            if _stop_event.wait(timeout=POLL_INTERVAL):
                break  # Stop was requested during wait
            continue

        empty_polls = 0

        chrome_proc = None
        try:
            logger.info('[W%d] %s START | %s', worker_id, 'PREVIEW' if dry_run else 'APPLY', job_details(job))
            add_event(f"[W{worker_id}] Launching Chrome...")
            chrome_proc = launch_chrome(worker_id, port=port, headless=headless)

            result, duration_ms = run_job(job, port=port, worker_id=worker_id,
                                            model=model, dry_run=dry_run, agent=agent)

            if result == "skipped":
                with _skipped_lock:
                    _skipped_urls.add(job["url"])
                release_lock(job["url"], job.get("apply_status"))
                progress.record('skipped', job, worker_id)
                add_event(f"[W{worker_id}] Skipped: {job['title'][:30]}")
                continue
            elif result == "previewed":
                mark_result(job["url"], "previewed", duration_ms=duration_ms)
                previewed += 1
                update_state(worker_id, jobs_previewed=previewed, jobs_done=applied + failed + previewed)
            elif result == "applied":
                mark_result(job["url"], "applied", duration_ms=duration_ms)
                applied += 1
                update_state(worker_id, jobs_applied=applied,
                             jobs_done=applied + failed + previewed)
            else:
                reason = result.split(":", 1)[-1] if ":" in result else result
                mark_result(job["url"], "failed", reason,
                            permanent=not dry_run or _is_permanent_failure(result),
                            duration_ms=duration_ms)
                failed += 1
                update_state(worker_id, jobs_failed=failed,
                             jobs_done=applied + failed + previewed)
            progress.record(result, job, worker_id)

        except KeyboardInterrupt:
            with _skipped_lock:
                _skipped_urls.add(job["url"])
            release_lock(job["url"], job.get("apply_status"))
            progress.record('skipped', job, worker_id)
            if _stop_event.is_set():
                break
            add_event(f"[W{worker_id}] Job skipped (Ctrl+C)")
            continue
        except ChromeStartupError as e:
            logger.error('[W%d] Browser startup failed: %s', worker_id, e)
            add_event(f'[W{worker_id}] Browser startup failed; batch stopped')
            release_lock(job['url'], job.get('apply_status'))
            break
        except Exception as e:
            logger.exception("Worker %d launcher error", worker_id)
            add_event(f"[W{worker_id}] Launcher error: {str(e)[:40]}")
            release_lock(job["url"], job.get("apply_status"))
            failed += 1
            update_state(worker_id, jobs_failed=failed)
            progress.record('failed:launcher_error', job, worker_id)
        finally:
            if chrome_proc:
                cleanup_worker(worker_id, chrome_proc)

        jobs_done += 1
        if target_url:
            break

    update_state(worker_id, status="done", last_action="finished")
    return applied, failed


# ---------------------------------------------------------------------------
# Main entry point (called from cli.py)
# ---------------------------------------------------------------------------

def main(limit: int = 1, target_url: str | None = None,
         min_score: int = 7, headless: bool = False, model: str = "haiku",
         dry_run: bool = False, continuous: bool = False,
         poll_interval: int = 60, workers: int = 1, agent: str = "claude") -> None:
    """Launch the apply pipeline.

    Args:
        limit: Max jobs to apply to (0 or with continuous=True means run forever).
        target_url: Apply to a specific URL.
        min_score: Minimum fit_score threshold.
        headless: Run Chrome in headless mode.
        model: Claude model name.
        dry_run: Don't click Submit.
        continuous: Run forever, polling for new jobs.
        poll_interval: Seconds between DB polls when queue is empty.
        workers: Number of parallel workers (default 1).
    """
    from applypilot.apply.agents import cancel_all

    global POLL_INTERVAL
    POLL_INTERVAL = poll_interval
    _stop_event.clear()
    with _skipped_lock:
        _skipped_urls.clear()

    if agent == "cursor":
        from applypilot.cursor_client import CursorLLMClient
        CursorLLMClient(model).validate()

    config.ensure_dirs()
    console = Console()

    # Skip URLs already submitted in the Daily Excel tracker (duplicate guard)
    try:
        from applypilot.tracking import import_applied_into_db, skip_urls_for_apply

        imported = import_applied_into_db()
        tracker_skips = skip_urls_for_apply()
        with _skipped_lock:
            _skipped_urls.update(tracker_skips)
        console.print(
            f"[dim]Daily tracker: {imported.get('tracker_applied', 0)} submitted URLs loaded; "
            f"DB updated {imported.get('updated', 0)}[/dim]"
        )
    except Exception as exc:
        logger.warning("Could not load Daily Excel tracker skips: %s", exc)

    if continuous:
        effective_limit = 0
        mode_label = "continuous"
    else:
        effective_limit = limit
        mode_label = f"{limit} jobs"

    if workers < 1 or (not continuous and limit < 1):
        raise ValueError("workers and limit must be positive")
    if target_url and workers > 1:
        raise ValueError("--url requires one worker")
    workers = min(workers, effective_limit) if effective_limit else workers

    # Initialize dashboard for all workers
    for i in range(workers):
        init_worker(i)

    worker_label = f"{workers} worker{'s' if workers > 1 else ''}"
    console.print(f"Launching apply pipeline ({mode_label}, {worker_label}, poll every {POLL_INTERVAL}s)...")
    console.print("[dim]Ctrl+C = skip current job(s) | Ctrl+C x2 = stop[/dim]")
    logger.info('Model: %s', model_details(agent, model))
    logger.info('Mode: %s', 'preview only; no applications will be submitted' if dry_run else 'submission enabled')
    log_saved_counts(get_stats())
    progress = ApplicationProgress()

    # Double Ctrl+C handler
    _ctrl_c_count = 0

    def _sigint_handler(sig, frame):
        nonlocal _ctrl_c_count
        _ctrl_c_count += 1
        if _ctrl_c_count == 1:
            console.print("\n[yellow]Skipping current job(s)... (Ctrl+C again to STOP)[/yellow]")
            from applypilot.apply.agents import cancel_all
            cancel_all()
        else:
            console.print("\n[red bold]STOPPING[/red bold]")
            _stop_event.set()
            from applypilot.apply.agents import cancel_all
            cancel_all()
            kill_all_chrome()
            raise KeyboardInterrupt

    previous_sigint = signal.signal(signal.SIGINT, _sigint_handler)
    previous_sigterm = signal.signal(signal.SIGTERM, lambda *_: (_stop_event.set(), cancel_all()))

    try:
        with Live(render_full(), console=console, refresh_per_second=2) as live:
            # Daemon thread for display refresh only (no business logic)
            _dashboard_running = True

            def _refresh():
                while _dashboard_running:
                    live.update(render_full())
                    time.sleep(0.5)

            refresh_thread = threading.Thread(target=_refresh, daemon=True)
            refresh_thread.start()

            if workers == 1:
                # Single worker — run directly in main thread
                total_applied, total_failed = worker_loop(
                    worker_id=0,
                    limit=effective_limit,
                    target_url=target_url,
                    min_score=min_score,
                    headless=headless,
                    model=model,
                    dry_run=dry_run,
                    agent=agent,
                    progress=progress,
                )
            else:
                # Multi-worker — distribute limit across workers
                if effective_limit:
                    base = effective_limit // workers
                    extra = effective_limit % workers
                    limits = [base + (1 if i < extra else 0)
                              for i in range(workers)]
                else:
                    limits = [0] * workers  # continuous mode

                with ThreadPoolExecutor(max_workers=workers,
                                        thread_name_prefix="apply-worker") as executor:
                    futures = {
                        executor.submit(
                            worker_loop,
                            worker_id=i,
                            limit=limits[i],
                            target_url=target_url,
                            min_score=min_score,
                            headless=headless,
                            model=model,
                            dry_run=dry_run,
                            agent=agent,
                            progress=progress,
                        ): i
                        for i in range(workers)
                    }

                    results: list[tuple[int, int]] = []
                    for future in as_completed(futures):
                        wid = futures[future]
                        try:
                            results.append(future.result())
                        except Exception:
                            logger.exception("Worker %d crashed", wid)
                            results.append((0, 0))

                total_applied = sum(r[0] for r in results)
                total_failed = sum(r[1] for r in results)

            _dashboard_running = False
            refresh_thread.join(timeout=2)
            live.update(render_full())

        totals = get_totals()
        console.print(
            f"\n[bold]Done: {total_applied} applied, {totals['previewed']} previewed, {total_failed} failed[/bold]"
        )
        console.print(f"Logs: {config.LOG_DIR}")

    except KeyboardInterrupt:
        pass
    finally:
        _dashboard_running = False
        _stop_event.set()
        cancel_all()
        kill_all_chrome()
        signal.signal(signal.SIGINT, previous_sigint)
        signal.signal(signal.SIGTERM, previous_sigterm)
