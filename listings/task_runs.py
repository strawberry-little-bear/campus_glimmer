# -*- coding: utf-8 -*-
"""Persist what each scheduled management command actually did.

Seven scheduled commands used to leave no trace: each printed one summary line
and exited, so how many notices were sent, how many sellers were skipped, and
whether the run failed at all lived only in the process return value. Two
consequences followed. The operations dashboard could not answer the simplest
question - did last week's reminder actually go out - and could only describe
the current state of the supply. Worse, failures were swallowed silently: the
scheduler knew from the exit code, but nothing inside the project recorded it,
so diagnosing a missed run meant digging through scheduler logs.

Three boundaries decide what this module may claim:

1. Only comparable scalars are stored, never arbitrary JSON. The commands
   return completely different shapes, and dumping them raw would make the
   panel unable to compare one command with another. So a fixed set of metric
   fields is defined here and each command maps its own result onto them;
   details that do not map are left in the command's own output.
2. A failed run must also be recorded. The whole value of a ledger is the
   abnormal rows, so the write happens in a finally block and inside its own
   short transaction. Depending on the outer transaction would drop the record
   exactly when it matters most, namely when the business transaction failed.
3. A dry run is not a run. --dry-run sends nothing, and counting it towards a
   success rate would make "is the schedule healthy" unanswerable, so dry
   runs are marked and excluded from the health statistics by default.
"""

import time
from contextlib import contextmanager

from django.db import transaction
from django.utils import timezone

from .models import TaskRun


# The fixed metric fields. Every command maps its result onto these scalars,
# and keys that do not map are dropped. Adding a command never requires
# touching this list; it only needs to return keys that line up.
METRIC_FIELDS = ("sent", "skipped", "failed", "marked", "expired", "reminded")


def normalise_metrics(result):
    """Keep only the comparable scalars from a command result.

    A command may return anything it likes; only known numeric keys are stored.
    Booleans are rejected even though they are ints in Python, because True
    stored as 1 would be indistinguishable from a genuine count of one.
    """
    if not isinstance(result, dict):
        return {}
    metrics = {}
    for key in METRIC_FIELDS:
        value = result.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        metrics[key] = value
    return metrics


def _write_run(*, name, status, started_at, finished_at, metrics, note):
    """Write one ledger row in its own short transaction.

    The separate transaction matters: the caller may be inside a failing
    business transaction, and the run record is exactly the evidence that the
    failure happened.
    """
    with transaction.atomic():
        return TaskRun.objects.create(
            name=name,
            status=status,
            started_at=started_at,
            finished_at=finished_at,
            duration_ms=int((finished_at - started_at).total_seconds() * 1000),
            metrics=metrics,
            note=note[:255],
        )


@contextmanager
def record_task_run(name, *, dry_run=False, metrics=None):
    """Record one execution of a scheduled command, success or failure.

    Usage is deliberately boring so that adopting it in a command costs one
    line:

        with record_task_run("send_lifecycle_reminders") as run:
            result = send_lifecycle_reminders()
            run.metrics = normalise_metrics(result)

    Exceptions propagate unchanged. The record is written in a finally block
    because a crashed run is the row an operator most needs to see, and the
    command still fails loudly instead of being swallowed by the ledger.
    """
    started_at = timezone.now()
    started = time.monotonic()
    run = TaskRun(
        name=name,
        status="succeeded",
        started_at=started_at,
        finished_at=started_at,
        metrics=metrics or {},
        note="dry-run" if dry_run else "",
    )
    try:
        yield run
    except Exception as exc:
        run.status = "failed"
        run.note = f"{type(exc).__name__}: {exc}"[:255]
        raise
    finally:
        run.finished_at = timezone.now()
        run.duration_ms = int((time.monotonic() - started) * 1000)
        run.metrics = run.metrics or {}
        _write_run(
            name=run.name,
            status=run.status,
            started_at=run.started_at,
            finished_at=run.finished_at,
            metrics=run.metrics,
            note=run.note,
        )
