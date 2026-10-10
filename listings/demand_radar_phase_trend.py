# -*- coding: utf-8 -*-
"""Ask whether the demand radar holds up across terms, not just across months.

`build_demand_radar_hit_trend` compares this thirty days with the thirty before
it, which is the right question for a radar that is being tuned right now. It is
the wrong question for a radar that has been running for a year. Campus demand
is seasonal in a way a month cannot see: the first fortnight of a term brings
textbook and dorm searches, exam fortnight brings revision material, graduation
brings a supply dump. A radar at 60% hit rate in September and a radar at 60% in
November are not the same radar, and comparing either with its neighbour tells
an operator nothing about which one they are looking at.

So this module files every follow-up task under the academic phase it was
created in and compares the same phase across the terms that have one. The unit
of comparison is therefore "this term's exam fortnight against last term's exam
fortnight", which is the only pairing where the demand being described is
comparable. A phase that appears in only one term is reported on its own and is
never given a direction: there is no earlier phase of the same kind to subtract
from, and the previous thirty days are a different season.

Three boundaries keep the comparison honest.

The phase is resolved from the task's own creation date, using the same
`academic_calendar` helper the dashboard and the digests use, so a task cannot
land in a different phase here than the one the rest of the product says it is
in. Tasks the calendar cannot place - created before any term was configured, or
in a stretch of term left unlabelled - are counted in their own buckets and
stated in the summary, exactly as `borrow_rhythm` does. Dropping them would
shrink the denominator and make every phase's hit rate quietly larger.

Each phase in each term is scored with the arithmetic
`build_demand_radar_hit_trend` uses, over exactly the tasks created inside it,
judged at each task's own maturity moment. The floor is the same
`MIN_JUDGED_TASKS` and it is applied to every phase-term cell separately: a
fortnight that raised three tasks produces a rate that moves in steps of a
third, and calling a direction on it would be arithmetic dressed as a finding.

Nothing here adjusts anything. The phase comparison is a fact for a human
weighing whether to revisit the scoring weights before the next term starts, not
a licence to move them. A radar that learns to weight the phases it happens to
score well on is worse than one that reports them all, because the phases it
stops reporting are exactly the ones that need attention.
"""

from datetime import timedelta

from django.utils import timezone

from .academic_calendar import _term_for_day
from .demand_radar_hit_trend import MIN_JUDGED_TASKS
from .demand_radar_outcome import (
    DEFAULT_WINDOW_DAYS, build_task_outcome,
)
from .models import DemandOpportunityTask


# Phases in the order the term runs, taken from the academic calendar's own
# choices rather than duplicated here: the same phase must not be called one
# thing on the calendar panel and another thing on this one.
PHASE_ORDER = ('registration', 'regular', 'exam', 'graduation', 'holiday')

# A task created inside a term but in a gap between its phases. That is a
# configuration state rather than a data problem - an operator may deliberately
# leave a stretch of term unlabelled - so it gets a cell instead of a warning.
UNASSIGNED_PHASE_KEY = 'unassigned'
UNASSIGNED_PHASE_LABEL = '未划分阶段'

# A task created before any term was configured, or after the last one ends.
NO_TERM_KEY = 'no_term'
NO_TERM_LABEL = '无学期日历'

# Keys that describe the calendar's coverage rather than a rhythm. They are
# reported but never compete for the cross-term comparison sentence.
SYNTHETIC_PHASE_KEYS = (NO_TERM_KEY, UNASSIGNED_PHASE_KEY)

# How far back tasks are considered at all. The comparison is per phase across
# terms, so the window has to be wide enough to hold more than one term; the
# dashboard's own period selector is deliberately not reused, because a 30-day
# window cannot contain two of the same phase.
DEFAULT_LOOKBACK_DAYS = 420

# A phase whose hit rate sits this many points away from the same phase in the
# previous term is flagged for attention. The flag carries no action and no
# ranking, and it is shown only on cells that have enough evidence for the
# subtraction to mean anything.
PHASE_GAP_POINTS = 15


def _rate(converged, judged):
    return round(converged / judged * 100, 1) if judged else None


def _summarise(rows):
    """The same arithmetic the cross-period trend uses, over one cell's rows.

    Repeated rather than imported because `build_demand_radar_hit_trend` takes
    its window as a number of days back from "now", and a phase is bounded by
    the calendar, which cannot be expressed that way - the two boundaries would
    drift by however far into the day "now" happens to fall.
    """
    scored = [row for row in rows if row["outcome"] != "pending"]
    converged = sum(row["outcome"] == "converged" for row in scored)
    insufficient = sum(row["outcome"] == "insufficient" for row in scored)
    judged = len(scored) - insufficient
    deltas = [row["delta_points"] for row in scored if row["delta_points"] is not None]
    median_delta = None
    if deltas:
        ordered = sorted(deltas)
        middle = len(ordered) // 2
        median_delta = (
            ordered[middle]
            if len(ordered) % 2
            else round((ordered[middle - 1] + ordered[middle]) / 2, 1)
        )
    return {
        "task_count": len(rows),
        "pending_count": sum(row["outcome"] == "pending" for row in rows),
        "judged_count": judged,
        "converged_count": converged,
        "flat_count": sum(row["outcome"] == "flat" for row in scored),
        "diverged_count": sum(row["outcome"] == "diverged" for row in scored),
        "insufficient_count": insufficient,
        "hit_rate": _rate(converged, judged),
        "median_delta_points": median_delta,
        "supply_added_total": sum(max(0, row["supply_delta"]) for row in scored),
    }


def _phase_slot(term, day):
    """The (term, phase) slot a single local date belongs to.

    Resolution goes through the calendar's own helper rather than being
    reimplemented, so a task cannot be filed under one phase here and another
    everywhere else. Both synthetic buckets are returned as slots rather than
    dropped: a task that happened before the calendar existed is still a task
    the radar raised, and the phases are supposed to account for it.
    """
    if term is None:
        return NO_TERM_KEY, NO_TERM_LABEL, None, None
    for phase in term.phases.all():
        if phase.contains(day):
            return phase.phase, phase.get_phase_display(), term.name, term.starts_on
    return UNASSIGNED_PHASE_KEY, UNASSIGNED_PHASE_LABEL, term.name, term.starts_on


def _file_tasks_by_phase(tasks, *, terms):
    """Group tasks into (phase, term) cells keyed by creation date.

    The term list is resolved once and reused for every task, which is what
    makes the filing deterministic: `_term_for_day` prefers the latest start
    when two terms overlap, and resolving per task against a fresh query would
    let a term added mid-computation move earlier tasks into it.
    """
    cells = {}
    for task in tasks:
        day = timezone.localdate(task.created_at)
        term, terms = _term_for_day(day, terms)
        phase_key, phase_label, term_name, term_starts_on = _phase_slot(term, day)
        slot = (phase_key, term_starts_on)
        cell = cells.setdefault(slot, {
            "phase_key": phase_key,
            "phase_label": phase_label,
            "term_name": term_name,
            "term_starts_on": term_starts_on,
            "is_synthetic": phase_key in SYNTHETIC_PHASE_KEYS,
            "tasks": [],
        })
        cell["tasks"].append(task)
    return cells


def _score_cell_tasks(tasks, *, window_days):
    """Score one cell's tasks, each at its own maturity moment.

    Judging at "now" would mark every task from an earlier term pending, and
    those are exactly the tasks the cross-term comparison exists to read. The
    moment used is therefore `created_at + window_days` and nothing else: the
    same rule the cross-period trend and the layer breakdown apply, so a task
    carries one verdict across all three panels.
    """
    window = window_days or DEFAULT_WINDOW_DAYS
    rows = []
    for task in tasks:
        judged_at = task.created_at + timedelta(days=window)
        row = build_task_outcome(task, now=judged_at, window_days=window)
        # The comparison reports on tasks created inside one phase of one term,
        # so the attribution must not move while the evidence does.
        row["created_at"] = task.created_at
        rows.append(row)
    return rows


def _build_cell(cell, *, window_days, min_judged_tasks):
    figures = _summarise(_score_cell_tasks(cell["tasks"], window_days=window_days))
    figures.update({
        "phase_key": cell["phase_key"],
        "phase_label": cell["phase_label"],
        "term_name": cell["term_name"],
        "term_starts_on": cell["term_starts_on"],
        "is_synthetic": cell["is_synthetic"],
        "has_sample": figures["judged_count"] >= min_judged_tasks,
    })
    return figures


def _cell_sort_key(cell):
    """Phases run in calendar order; terms newest first; synthetic buckets last.

    The phase order comes from the calendar's own plan so the table reads the
    way the term runs, and the term order is newest first because the question
    an operator asks is "is this term worse than the last one", which needs the
    current term at the top of its group.
    """
    phase_key = cell["phase_key"]
    if phase_key in SYNTHETIC_PHASE_KEYS:
        phase_rank = len(PHASE_ORDER) + (1 if phase_key == UNASSIGNED_PHASE_KEY else 2)
    elif phase_key in PHASE_ORDER:
        phase_rank = PHASE_ORDER.index(phase_key)
    else:
        phase_rank = len(PHASE_ORDER)
    starts_on = cell["term_starts_on"]
    return (
        phase_rank,
        -(starts_on.toordinal() if starts_on else 0),
        cell["term_name"] or "",
    )


def _gap_points(current, previous):
    if current["hit_rate"] is None or previous["hit_rate"] is None:
        return None
    return round(current["hit_rate"] - previous["hit_rate"], 1)


def _build_comparisons(cells, *, min_judged_tasks):
    """Pair each phase's newest term with the term before it.

    Only the two newest terms holding the same phase are compared. Pairing a
    phase with every earlier term would produce a table of overlapping
    subtractions, each computed from a different amount of evidence, and the
    reader would have no way to tell which one answers their question. The
    newest pair is the one that says whether the radar is currently drifting.

    A phase present in only one term is reported with no gap: there is nothing
    of the same kind to subtract from, and the neighbouring thirty days are a
    different season.
    """
    by_phase = {}
    for cell in cells:
        if cell["is_synthetic"]:
            continue
        by_phase.setdefault(cell["phase_key"], []).append(cell)

    comparisons = []
    for phase_key in PHASE_ORDER + (UNASSIGNED_PHASE_KEY,):
        group = by_phase.get(phase_key)
        if not group:
            continue
        group = sorted(group, key=_cell_sort_key)
        current = group[0]
        previous = group[1] if len(group) > 1 else None
        gap = _gap_points(current, previous) if previous else None
        # A gap is only worth flagging when both sides have enough evidence for
        # the subtraction to mean anything. On a three-task cell a fifteen-point
        # gap is one task changing its mind.
        has_sample = bool(
            current["has_sample"] and (previous is None or previous["has_sample"])
        )
        comparisons.append({
            "phase_key": phase_key,
            "phase_label": current["phase_label"],
            "current": current,
            "previous": previous,
            "gap_points": gap,
            "has_sample": has_sample,
            "is_flagged": bool(
                has_sample and previous is not None
                and gap is not None and abs(gap) >= PHASE_GAP_POINTS
            ),
        })
    comparisons.sort(key=lambda item: PHASE_ORDER.index(item["phase_key"])
                     if item["phase_key"] in PHASE_ORDER else len(PHASE_ORDER))
    return comparisons


def _conclusion(comparisons, *, cells, has_any_sample, flagged_count):
    """One sentence naming what the cross-term comparison actually shows."""
    if not cells:
        return (
            "这个回看期内没有创建跟进任务，学期纵向对比需要先有任务发生。"
        )

    placed = [cell for cell in cells if not cell["is_synthetic"]]
    unplaced = len(cells) - len(placed)
    coverage = ""
    if unplaced:
        coverage = (
            f"另有 {unplaced} 个任务落在无学期日历或未划分阶段，已单独计数，没有被丢掉。"
        )

    if not has_any_sample:
        return (
            "各学期阶段的有证据任务数都达不到门槛，学期纵向对比暂时无法给出方向。"
            + coverage
        )

    if not flagged_count:
        return (
            f"同学期同阶段的命中率相差都在 {PHASE_GAP_POINTS} 个百分点以内，"
            "这个回看期看不出某个阶段在跨学期漂移。" + coverage
        )

    parts = []
    for item in comparisons:
        if not item["is_flagged"]:
            continue
        direction = "高于" if item["gap_points"] > 0 else "低于"
        parts.append(
            f"{item["phase_label"]}{direction}上一学期 {abs(item["gap_points"])} 个百分点"
        )
    return (
        "有 " + str(flagged_count) + " 个阶段与上一学期同阶段相差超过 "
        + str(PHASE_GAP_POINTS) + " 个百分点：" + "，".join(parts[:3])
        + "。阶段上的差值只说明走向，任务数少的阶段上的差值不算结论；"
        "这里也不排序、不改机会分。" + coverage
    )


def build_demand_radar_phase_trend(
    days=DEFAULT_LOOKBACK_DAYS, *, now=None, window_days=None,
    min_judged_tasks=MIN_JUDGED_TASKS,
):
    """Compare each academic phase's radar hit rate with the same phase last term.

    `days` selects how far back tasks are considered. It is deliberately much
    wider than the dashboard's period selector, because a 30-day window cannot
    hold two of the same phase and the comparison would collapse into the
    neighbouring-period one. `window_days` is passed straight through to
    control each task's own measurement span, and is left at the outcome
    module's default when not given so this panel and the cross-period one
    cannot drift apart.
    """
    from .models import AcademicTerm

    now = now or timezone.now()
    days = max(1, int(days))
    terms = list(
        AcademicTerm.objects.filter(is_active=True).prefetch_related("phases")
    )
    tasks = list(
        DemandOpportunityTask.objects.filter(
            created_at__gte=now - timedelta(days=days), created_at__lte=now,
        ).select_related("category", "location", "created_by", "assigned_to")
    )

    cells_by_slot = _file_tasks_by_phase(tasks, terms=terms)
    cells = [
        _build_cell(cell, window_days=window_days, min_judged_tasks=min_judged_tasks)
        for cell in cells_by_slot.values()
    ]
    cells.sort(key=_cell_sort_key)

    comparisons = _build_comparisons(cells, min_judged_tasks=min_judged_tasks)
    # The panel cannot index a dict by a loop variable, so each phase is shipped
    # as one block carrying its own label and its own cells, newest term first.
    # This is presentation plumbing, not a second copy of the figures: the same
    # cell dicts are referenced, so a cell cannot read one way in the table and
    # another in the comparison.
    phase_groups = []
    for phase_key in PHASE_ORDER + (UNASSIGNED_PHASE_KEY, NO_TERM_KEY):
        group_cells = [cell for cell in cells if cell["phase_key"] == phase_key]
        if not group_cells:
            continue
        phase_groups.append({
            "key": phase_key,
            "label": group_cells[0]["phase_label"],
            "is_synthetic": phase_key in SYNTHETIC_PHASE_KEYS,
            "cells": group_cells,
        })
    flagged_count = sum(1 for item in comparisons if item["is_flagged"])
    has_any_sample = any(cell["has_sample"] for cell in cells)
    waiting_cell_count = sum(1 for cell in cells if not cell["has_sample"])
    term_names = []
    for cell in cells:
        if cell["term_name"] and cell["term_name"] not in term_names:
            term_names.append(cell["term_name"])

    return {
        "days": days,
        "window_days": window_days,
        "min_judged_tasks": min_judged_tasks,
        "gap_points": PHASE_GAP_POINTS,
        "phase_order": list(PHASE_ORDER),
        "no_term_label": NO_TERM_LABEL,
        "unassigned_phase_label": UNASSIGNED_PHASE_LABEL,
        "cells": cells,
        "phase_groups": phase_groups,
        "comparisons": comparisons,
        "term_names": term_names,
        "term_count": len(term_names),
        "flagged_count": flagged_count,
        "waiting_cell_count": waiting_cell_count,
        "unplaced_cell_count": sum(1 for cell in cells if cell["is_synthetic"]),
        "has_data": bool(cells),
        "has_sample": has_any_sample,
        "summary": _conclusion(
            comparisons, cells=cells, has_any_sample=has_any_sample,
            flagged_count=flagged_count,
        ),
    }
