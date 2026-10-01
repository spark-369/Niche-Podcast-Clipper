"""Background job routes (Redis/Celery inspection and control)."""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.deps import get_current_user, rate_limit
from core.celery_app import celery_app
from db.session import get_db
from models import Clip, Episode, RenderJob, User
from models.enums import JobStatus, JobType
from schemas.clip import RenderJobRead

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/jobs", tags=["Jobs"])


@router.get("", response_model=list[RenderJobRead], summary="List background jobs")
def list_jobs(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    episode_id: Optional[int] = Query(default=None),
    clip_id: Optional[int] = Query(default=None),
    job_type: Optional[JobType] = Query(default=None),
    job_status: Optional[JobStatus] = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[RenderJobRead]:
    """Return background jobs scoped to the current user's content."""
    owned_episode_ids = select(Episode.id).where(Episode.owner_id == current_user.id)
    query = (
        select(RenderJob)
        .where(RenderJob.episode_id.in_(owned_episode_ids))
        .order_by(RenderJob.created_at.desc())
        .limit(limit)
    )
    if episode_id is not None:
        query = query.where(RenderJob.episode_id == episode_id)
    if clip_id is not None:
        query = query.where(RenderJob.clip_id == clip_id)
    if job_type is not None:
        query = query.where(RenderJob.job_type == job_type.value)
    if job_status is not None:
        query = query.where(RenderJob.status == job_status.value)

    jobs = list(db.scalars(query).all())
    return [RenderJobRead.model_validate(job) for job in jobs]


@router.get(
    "/worker/ping",
    summary="Check that a Celery worker is alive",
    dependencies=[Depends(rate_limit)],
)
def ping_worker() -> dict:
    """Send a ping through the Redis broker to every clipper worker.

    Declared *before* ``/{job_id}`` so FastAPI does not swallow the static
    path as ``job_id="worker"`` (which answered 422).
    """
    try:
        replies = celery_app.control.ping(timeout=2.0) or []
    except Exception as exc:  # pragma: no cover - broker dependent
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Celery broker unreachable: {exc}",
        )
    return {
        "workers_online": len(replies),
        "replies": replies,
        "queue": celery_app.conf.task_default_queue,
    }


@router.get("/{job_id}", response_model=RenderJobRead, summary="Job detail")
def read_job(
    job_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> RenderJobRead:
    """Return a single job, refreshing its state from the Celery result backend."""
    job = db.scalar(select(RenderJob).where(RenderJob.id == job_id))
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    # Ownership check: a job is visible through the clip that spawned it or,
    # for transcription/highlight jobs, through its episode. Both paths must
    # resolve to the caller, otherwise any authenticated user could read (and
    # leak error details from) someone else's job by iterating ids.
    if job.clip_id is not None:
        owner_id = db.scalar(select(Clip.owner_id).where(Clip.id == job.clip_id))
    elif job.episode_id is not None:
        owner_id = db.scalar(select(Episode.owner_id).where(Episode.id == job.episode_id))
    else:
        owner_id = None
    if owner_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    if job.task_id:
        async_result = celery_app.AsyncResult(job.task_id)
        if async_result.ready():
            job.status = (
                JobStatus.SUCCESS.value
                if async_result.successful()
                else JobStatus.FAILURE.value
            )
            if async_result.successful():
                job.progress = 100
            else:
                job.error_message = str(async_result.result)[:2000]
            db.add(job)
            db.commit()
            db.refresh(job)
        elif async_result.state in ("STARTED", "RECEIVED"):
            # Persist the upgrade too (e.g. a worker that died before it could
            # mark the job running), not just for this response.
            job.status = JobStatus.RUNNING.value
            db.add(job)
            db.commit()
            db.refresh(job)

    return RenderJobRead.model_validate(job)


@router.post("/{job_id}/revoke", response_model=RenderJobRead, summary="Cancel a job")
def revoke_job(
    job_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    terminate: bool = Query(default=False),
) -> RenderJobRead:
    """Ask Celery to cancel a pending or running job."""
    job = db.scalar(select(RenderJob).where(RenderJob.id == job_id))
    if job is None or (job.clip_id is None and job.episode_id is None):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    if job.clip_id is not None:
        owner_id = db.scalar(select(Clip.owner_id).where(Clip.id == job.clip_id))
    else:
        owner_id = db.scalar(select(Episode.owner_id).where(Episode.id == job.episode_id))
    if owner_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    if job.task_id:
        celery_app.control.revoke(job.task_id, terminate=terminate)
        job.status = JobStatus.FAILURE.value
        job.message = "Cancelled by the user"
        db.add(job)
        db.commit()
        db.refresh(job)

    return RenderJobRead.model_validate(job)
