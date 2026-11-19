"""How long each kind of wait may take before the page says it is taking longer than usual.

Read-only: these come from the limits the jobs already have (timeout times attempts), so the
note never appears before the job itself could have failed or finished.
"""

from app.services import agent, godaddy, intake, jobs

SCORE_ATTEMPTS = 2  # queue_score uses enqueue's default number of attempts

WAIT_LIMITS: dict[str, int] = {
    "intake": intake.INTAKE_TIMEOUT_SECONDS,  # one attempt
    "score": jobs.DEFAULT_TIMEOUT_SECONDS * SCORE_ATTEMPTS,
    "domains": godaddy.JOB_TIMEOUT_SECONDS + godaddy.STUCK_GRACE_SECONDS,
    "agent": agent.JOB_TIMEOUT_SECONDS * agent.MAX_ATTEMPTS,
}
