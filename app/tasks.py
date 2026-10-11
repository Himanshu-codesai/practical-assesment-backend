"""How the API hands work to the background: it puts a message on the Celery queue.

Endpoints depend on `Dispatcher`, not on Celery, so the queue could be swapped (for
example for FastAPI BackgroundTasks in a small deployment) without touching them.
"""
from app.logging_config import request_id_var
from app.models import Job
from app.worker import QUEUES, run_job


class Dispatcher:
    def enqueue(self, job: Job) -> None:
        # Callers commit the job row first, so the worker can always find it.
        run_job.apply_async(args=[job.id, request_id_var.get()], queue=QUEUES[job.kind])


def get_dispatcher() -> Dispatcher:
    return Dispatcher()
