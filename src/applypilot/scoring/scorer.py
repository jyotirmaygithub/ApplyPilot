"""Job fit scoring: LLM-powered evaluation of candidate-job match quality.

Scores jobs on a 1-10 scale by comparing the user's resume against each
job description. All personal data is loaded at runtime from the user's
profile and resume file.
"""

import logging
import re
import time
from datetime import UTC, datetime

from applypilot.config import RESUME_PATH, load_profile
from applypilot.database import get_connection, get_jobs_by_stage
from applypilot.llm import get_client
from applypilot.progress import BatchProgress

log = logging.getLogger(__name__)


# ── Scoring Prompt ────────────────────────────────────────────────────────

SCORE_PROMPT = """You are a job fit evaluator. Given a candidate's resume and a job description, score how well the candidate fits the role.

SCORING CRITERIA:
- 9-10: Perfect match. Candidate has direct experience in nearly all required skills and qualifications.
- 7-8: Strong match. Candidate has most required skills, minor gaps easily bridged.
- 5-6: Moderate match. Candidate has some relevant skills but missing key requirements.
- 3-4: Weak match. Significant skill gaps, would need substantial ramp-up.
- 1-2: Poor match. Completely different field or experience level.

IMPORTANT FACTORS:
- Weight technical skills heavily (programming languages, frameworks, tools)
- Consider transferable experience (automation, scripting, API work)
- Factor in the candidate's project experience
- Be realistic about experience level vs. job requirements (years of experience, seniority)

RESPOND IN EXACTLY THIS FORMAT (no other text):
SCORE: [1-10]
KEYWORDS: [comma-separated ATS keywords from the job description that match or could match the candidate]
REASONING: [2-3 sentences explaining the score]"""


def _parse_score_response(response: str) -> dict:
    """Parse the LLM's score response into structured data.

    Args:
        response: Raw LLM response text.

    Returns:
        {"score": int, "keywords": str, "reasoning": str}
    """
    score = 0
    keywords = ""
    reasoning = response

    for line in response.split("\n"):
        line = line.strip()
        if line.startswith("SCORE:"):
            try:
                score = int(re.search(r"\d+", line).group())
                score = max(1, min(10, score))
            except (AttributeError, ValueError):
                score = 0
        elif line.startswith("KEYWORDS:"):
            keywords = line.replace("KEYWORDS:", "").strip()
        elif line.startswith("REASONING:"):
            reasoning = line.replace("REASONING:", "").strip()

    return {"score": score, "keywords": keywords, "reasoning": reasoning}


def score_job(resume_text: str, job: dict) -> dict:
    """Score a single job against the resume.

    Args:
        resume_text: The candidate's full resume text.
        job: Job dict with keys: title, site, location, full_description.

    Returns:
        {"score": int, "keywords": str, "reasoning": str}
    """
    job_text = (
        f"TITLE: {job['title']}\n"
        f"COMPANY: {job['site']}\n"
        f"LOCATION: {job.get('location', 'N/A')}\n\n"
        f"DESCRIPTION:\n{(job.get('full_description') or '')[:6000]}"
    )

    import json
    try:
        profile = load_profile()
    except FileNotFoundError:
        profile = {}
    preferences = profile.get('job_preferences', {})
    preference_prompt = ''
    if preferences:
        country = preferences.get('work_location_country') or profile.get('personal', {}).get('country', '')
        if preferences.get('remote_only'):
            location_rule = (
                f'Remote-only is a hard requirement for work performable from {country or "the applicant country"}. '
                'Onsite, hybrid, required relocation, or remote jobs restricted to countries that exclude the applicant '
                'must score 1 regardless of technical fit. Unclear remote/geographic eligibility must score at most 4. '
            )
        else:
            cities = preferences.get('acceptable_cities') or preferences.get('preferred_locations') or []
            city_hint = ', '.join(cities) if isinstance(cities, list) and cities else 'listed preferred cities'
            location_rule = (
                f'Applicant is based in {country or "their country"}. '
                f'Remote roles eligible from that country, plus hybrid/onsite in these areas, are acceptable: {city_hint}. '
                'Roles requiring relocation outside the applicant country must score 1. '
                'Unclear geographic eligibility must score at most 4. '
            )
        preference_prompt = (
            '\nAPPLICANT PREFERENCES:\n' + json.dumps(preferences)
            + '\nEXPERIENCE BREAKDOWN:\n' + json.dumps(profile.get('experience', {}))
            + '\n' + location_rule
            + 'Full-time, part-time and contracts are eligible when permitted. No salary floor means do not penalize low pay. '
            'Keep internship experience separate from full-time experience. Do not treat 6 months full-time as 3 years full-time. '
            'Judge seniority requirements honestly; do not inflate qualifications.'
        )
    messages = [
        {"role": "system", "content": SCORE_PROMPT + preference_prompt},
        {"role": "user", "content": f"RESUME:\n{resume_text}\n\n---\n\nJOB POSTING:\n{job_text}"},
    ]

    try:
        client = get_client()
        response = client.chat(messages, max_tokens=512, temperature=0.2)
        return _parse_score_response(response)
    except Exception as e:
        log.debug("scorer: operation failed", exc_info=True)
        log.error("LLM error scoring job '%s': %s", job.get("title", "?"), e)
        return {"score": 0, "keywords": "", "reasoning": f"LLM error: {e}"}


def run_scoring(limit: int = 0, rescore: bool = False) -> dict:
    """Score unscored jobs that have full descriptions.

    Args:
        limit: Maximum number of jobs to score in this run.
        rescore: If True, re-score all jobs (not just unscored ones).

    Returns:
        {"scored": int, "errors": int, "elapsed": float, "distribution": list}
    """
    resume_text = RESUME_PATH.read_text(encoding="utf-8")
    conn = get_connection()

    if rescore:
        query = "SELECT * FROM jobs WHERE full_description IS NOT NULL"
        if limit > 0:
            query += f" LIMIT {limit}"
        jobs = conn.execute(query).fetchall()
    else:
        jobs = get_jobs_by_stage(conn=conn, stage="pending_score", limit=limit)

    if not jobs:
        log.info("No unscored jobs with descriptions found.")
        return {"scored": 0, "errors": 0, "elapsed": 0.0, "distribution": []}

    # Convert sqlite3.Row to dicts if needed
    if jobs and not isinstance(jobs[0], dict):
        columns = jobs[0].keys()
        jobs = [dict(zip(columns, row)) for row in jobs]

    log.info("Scoring %d jobs sequentially...", len(jobs))
    progress = BatchProgress('Scoring', 'scored', len(jobs))
    t0 = time.time()
    errors = 0
    results: list[dict] = []

    for job in jobs:
        progress.start_job(job)
        result = score_job(resume_text, job)
        result["url"] = job["url"]

        if result["score"] == 0:
            errors += 1

        results.append(result)
        now = datetime.now(UTC).isoformat()
        conn.execute(
            "UPDATE jobs SET fit_score = ?, score_reasoning = ?, scored_at = ? WHERE url = ?",
            (result['score'], f"{result['keywords']}\n{result['reasoning']}", now, result['url']),
        )
        conn.commit()
        progress.record({**job, 'fit_score': result['score']}, success=result['score'] > 0)

    elapsed = time.time() - t0
    log.info("Done: %d scored in %.1fs (%.1f jobs/sec)", len(results), elapsed, len(results) / elapsed if elapsed > 0 else 0)

    # Score distribution
    dist = conn.execute("""
        SELECT fit_score, COUNT(*) FROM jobs
        WHERE fit_score IS NOT NULL
        GROUP BY fit_score ORDER BY fit_score DESC
    """).fetchall()
    distribution = [(row[0], row[1]) for row in dist]

    return {
        "scored": len(results),
        "errors": errors,
        "elapsed": elapsed,
        "distribution": distribution,
    }
