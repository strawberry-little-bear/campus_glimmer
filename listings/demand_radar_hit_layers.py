# -*- coding: utf-8 -*-
"""Break the radar's hit rate down by category and by location.

`build_demand_radar_hit_trend` answers one number for the whole radar: of the
tasks raised in this window, what share closed their gap. That number is the
right thing to put on an alert, and it is the wrong thing to act on. A 60% hit
rate is either "every category is around 60%" or "one category is at 95% and
another is at 25%", and those need opposite responses - the first says the
scoring weights are roughly calibrated and need patience, the second says the
weights are wrong in one direction and the effort is going where it does not
work. An operator who only sees the average will keep spending the same effort
everywhere, which is how a 25% category stays at 25%.

This module therefore cuts the same task rows by the two dimensions a task
actually carries - category and location, both foreign keys on the task itself -
and reports each layer's own hit rate. Each layer is scored with exactly the
arithmetic `build_demand_radar_hit_trend` uses, over exactly the rows that fall
inside it, so a layer's rate is comparable with the overall rate and with every
other layer.

Four boundaries keep the breakdown honest.

The rate is read inside a layer, never across layers. "This category's hit rate
is 80%" is a statement about the tasks in that category; "this category is 20%
of all tasks" is a statement about how the calendar distributed them, and it
moves whenever the radar happens to raise more tasks somewhere. Only the first
question is answered here. The task count is reported next to each rate so a
reader can see how much evidence the rate stands on, but it is never divided by
the total.

Every layer needs its own minimum number of judged tasks. The overall floor
`build_demand_radar_hit_trend` uses is three, and it is reused here unchanged.
The temptation with a breakdown is to lower it, because eight categories each
holding three tasks already needs twenty-four tasks and a quiet month has fewer
than that - a table of dashes looks like the module failed. It has not failed:
a hit rate computed from two tasks moves in steps of fifty points, so calling a
direction on it would be arithmetic dressed as a finding. Layers below the floor
are listed with their counts and no direction, and the panel says how many are
waiting. The floor is never lowered to make the table look fuller.

Layers are compared with the overall rate only as context, and the comparison
never becomes a recommendation. A layer sitting ten points below the overall
rate is not thereby "the problem": with four tasks in the layer, ten points is
one task changing its mind. The gap is reported next to the layer so a reader
can see it, and the module says in its summary that a gap on a thin layer is
not a finding. Nothing here ranks layers into a to-do list.

Nothing here adjusts anything. The score weights, the opportunity score, the
task's own level and its status are all left exactly as they are. Ranking
layers by hit rate and quietly raising the score of the worst-performing one
would be the same mistake `build_demand_radar_hit_trend` refuses to make at the
top level, one layer down: a radar that learns to stop reporting the gaps it
cannot prove it fixes is worse than one that reports them. This module reports
the breakdown so a human can decide where to look.
"""

from datetime import timedelta

from django.utils import timezone

from .demand_radar_hit_trend import MIN_JUDGED_TASKS
from .demand_radar_outcome import DEFAULT_WINDOW_DAYS, build_task_outcome


# The two dimensions a task is cut by. Both are foreign keys on the task
# itself, which is what makes the breakdown cheap and honest: the layer a task
# belongs to is the layer it was filed under, not one inferred afterwards from
# its text. A third dimension - the term - is deliberately not cut by, because
# a layer holding one task is not a layer.
LAYER_FACETS = ('category', 'location')

LAYER_FACET_LABELS = {
    'category': '\u5206\u7c7b',
    'location': '\u5730\u70b9',
}

# How a layer with no category or no location is labelled. A task can legally
# carry neither, because the radar raises opportunities from searches that did
# not filter, and calling that layer "unknown" keeps it visible instead of
# dropping it. Dropping it would quietly shrink the denominator of the overall
# rate the layers are supposed to explain.
UNASSIGNED_LABEL = '\u672a\u5206\u7ec7'

# A layer whose hit rate sits this many points away from the overall rate is
# flagged for attention. The flag carries no action and no ranking - see the
# module docstring - and it is shown only on layers that have enough evidence
# for the comparison to mean anything.
LAYER_GAP_POINTS = 15


def _rate(converged, judged):
    return round(converged / judged * 100, 1) if judged else None


def _summarise(rows):
    """The same arithmetic the overall trend uses, over one layer's rows."""
    scored = [row for row in rows if row['outcome'] != 'pending']
    converged = sum(row['outcome'] == 'converged' for row in scored)
    insufficient = sum(row['outcome'] == 'insufficient' for row in scored)
    judged = len(scored) - insufficient
    deltas = [row['delta_points'] for row in scored if row['delta_points'] is not None]
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
        'task_count': len(rows),
        'pending_count': sum(row['outcome'] == 'pending' for row in rows),
        'judged_count': judged,
        'converged_count': converged,
        'flat_count': sum(row['outcome'] == 'flat' for row in scored),
        'diverged_count': sum(row['outcome'] == 'diverged' for row in scored),
        'insufficient_count': insufficient,
        'hit_rate': _rate(converged, judged),
        'median_delta_points': median_delta,
    }


def _layer_label(facet, row):
    """Name a layer from the task's own foreign key.

    A task whose category was deleted after the fact is reported under the
    unassigned label rather than dropped: the task still happened, and the
    layer still owes an explanation for it.
    """
    if facet == 'category':
        related = row.get('category')
        name = getattr(related, 'name', None) if related is not None else None
    else:
        related = row.get('location')
        name = getattr(related, 'name', None) if related is not None else None
    return name or UNASSIGNED_LABEL


def _layer_rows(tasks, facet):
    """Group the raw tasks by one facet into named buckets.

    The grouping happens on the task objects rather than on the scored rows,
    because a scored row does not carry the category object - only its id, and
    an id with a deleted category behind it would render as a number.
    """
    buckets = {}
    for task in tasks:
        label = _layer_label(facet, {'category': task.category, 'location': task.location})
        buckets.setdefault(label, []).append(task)
    return buckets


def _scored_rows_by_task(tasks, *, days, window_days):
    """Score every task, keyed by task id.

    `build_demand_radar_outcomes` returns the rows it chose to show, limited
    to `limit`, which is not the same set this module needs: a breakdown has
    to account for every task in the window or its layers will not add up
    to the overall rate. The task set is therefore taken first, in full, and
    each task is scored on its own.

    Each task is judged at the moment its own observation window closed, not
    at `now`, which is the same rule `build_demand_radar_hit_trend` applies
    to the rows it compares. Judging at `now` would mark every task raised
    in the last two weeks pending, and those are exactly the tasks an
    operator wants to see cut by category - a breakdown that silently omits
    the newest work is not the same breakdown the overall trend describes.
    The two panels then describe the same tasks under the same verdicts, and
    the layers add up to the number the trend reports.

    A task whose window still has not closed by its own maturity moment
    stays pending and is counted as such rather than dropped.
    """
    window = window_days or DEFAULT_WINDOW_DAYS

    scored = {}
    for task in tasks:
        judged_at = task.created_at + timedelta(days=window)
        row = build_task_outcome(task, now=judged_at, window_days=window)
        # The breakdown reports on tasks created inside one period, so the
        # attribution must not move while the evidence does.
        row["created_at"] = task.created_at
        scored[task.id] = row
    return scored


def _build_layer(label, rows, *, overall_rate, min_judged_tasks):
    """One layer's figures plus its position relative to the overall rate."""
    figures = _summarise(rows)
    figures['label'] = label
    figures['has_sample'] = figures['judged_count'] >= min_judged_tasks
    gap = None
    if figures['hit_rate'] is not None and overall_rate is not None:
        gap = round(figures['hit_rate'] - overall_rate, 1)
    figures['gap_points'] = gap
    # A gap is only worth flagging on a layer that has enough evidence for the
    # subtraction to mean anything. On a two-task layer a fifteen-point gap is
    # one task changing its mind, and flagging it would send an operator to
    # look at noise.
    figures['is_flagged'] = bool(
        figures['has_sample'] and gap is not None and abs(gap) >= LAYER_GAP_POINTS
    )
    return figures


def _conclusion(layers, *, overall, has_data, flagged_count):
    """One sentence naming what the breakdown actually shows."""
    if not has_data:
        return '\u8fd9\u4e2a\u5468\u671f\u5185\u8fd8\u6ca1\u6709\u521b\u5efa\u8ddf\u8fdb\u4efb\u52a1\uff0c\u5206\u5c42\u547d\u4e2d\u7387\u9700\u8981\u5148\u6709\u4efb\u52a1\u53d1\u751f\u3002'

    waiting = sum(1 for layer in layers if not layer['has_sample'])
    if waiting == len(layers):
        return (
            '\u6bcf\u4e2a\u5206\u5c42\u7684\u6709\u8bc1\u636e\u4efb\u52a1\u6570\u90fd\u8fbe\u4e0d\u5230\u95e8\u69db\uff0c'
            '\u5206\u5c42\u4e4b\u95f4\u6682\u65f6\u65e0\u6cd5\u6bd4\u8f83\uff1b\u6574\u4f53\u547d\u4e2d\u7387'
            + (
                f'\u4e3a {overall["hit_rate"]}%\u3002' if overall['hit_rate'] is not None
                else '\u540c\u6837\u7b49\u5f85\u8db3\u591f\u8bc1\u636e\u3002'
            )
        )

    if not flagged_count:
        return (
            '\u5404\u5206\u5c42\u7684\u547d\u4e2d\u7387\u4e0e\u6574\u4f53\u76f8\u5dee\u90fd\u5728'
            f'{LAYER_GAP_POINTS} \u4e2a\u767e\u5206\u70b9\u4ee5\u5185\uff0c'
            '\u672c\u5468\u671f\u770b\u4e0d\u51fa\u660e\u663e\u62d6\u540e\u817f\u7684\u5206\u5c42\u3002'
        )

    return (
        f'\u6709 {flagged_count} \u4e2a\u5206\u5c42\u4e0e\u6574\u4f53\u547d\u4e2d\u7387\u76f8\u5dee\u8d85\u8fc7'
        f'{LAYER_GAP_POINTS} \u4e2a\u767e\u5206\u70b9\uff1b'
        '\u5206\u5c42\u4e0a\u7684\u5dee\u503c\u53ea\u8bf4\u660e\u8d70\u5411\uff0c'
        '\u4efb\u52a1\u6570\u5c11\u7684\u5206\u5c42\u4e0a\u7684\u5dee\u503c\u4e0d\u7b97\u7ed3\u8bba\uff0c'
        '\u8fd9\u91cc\u4e5f\u4e0d\u6392\u5e8f\u3001\u4e0d\u6539\u673a\u4f1a\u5206\u3002'
    )


def build_demand_radar_hit_layers(
    days=30, *, now=None, window_days=None, limit=100, min_judged_tasks=MIN_JUDGED_TASKS,
):
    """Cut this period's follow-up tasks by category and by location.

    `days` selects the tasks being reported, the same way
    `build_demand_radar_hit_trend` uses it: how far back they were created.
    `window_days` is passed straight through to control each task's own
    measurement span, and is left at the outcome module's default when not
    given so the layers and the trend cannot drift apart.
    """
    from .models import DemandOpportunityTask

    now = now or timezone.now()
    days = max(1, int(days))
    tasks = list(
        DemandOpportunityTask.objects.filter(
            created_at__gte=now - timedelta(days=days), created_at__lte=now,
        ).select_related('category', 'location', 'created_by', 'assigned_to')[
            :max(limit, 1)
        ]
    )
    scored = _scored_rows_by_task(
        tasks, days=days, window_days=window_days,
    )
    overall = _summarise([scored[task.id] for task in tasks])

    layers = {}
    for facet in LAYER_FACETS:
        rows_for_facet = []
        for label, bucket in sorted(
            _layer_rows(tasks, facet).items(),
            key=lambda item: (-len(item[1]), item[0]),
        ):
            rows_for_facet.append(
                _build_layer(
                    label, [scored[task.id] for task in bucket],
                    overall_rate=overall['hit_rate'],
                    min_judged_tasks=min_judged_tasks,
                )
            )
        layers[facet] = rows_for_facet

    flagged_count = sum(
        1 for facet in LAYER_FACETS for layer in layers[facet] if layer['is_flagged']
    )

    return {
        'days': days,
        'window_days': window_days,
        'facets': list(LAYER_FACETS),
        'facet_labels': dict(LAYER_FACET_LABELS),
        'unassigned_label': UNASSIGNED_LABEL,
        'min_judged_tasks': min_judged_tasks,
        'gap_points': LAYER_GAP_POINTS,
        'overall': overall,
        'layers': layers,
        'layer_counts': {
            facet: len(layers[facet]) for facet in LAYER_FACETS
        },
        # The panel cannot index a dict by a loop variable, so each facet is
        # shipped as one block carrying its own label and its own layers. This
        # is presentation plumbing, not a second copy of the figures: the same
        # layer dicts are referenced, so a layer cannot read one way in the
        # table and another in the summary.
        'facet_groups': [
            {'key': facet, 'label': LAYER_FACET_LABELS[facet], 'layers': layers[facet]}
            for facet in LAYER_FACETS
        ],
        'flagged_count': flagged_count,
        'waiting_layer_count': sum(
            1 for facet in LAYER_FACETS for layer in layers[facet]
            if not layer['has_sample']
        ),
        'has_data': bool(tasks),
        'has_sample': overall['judged_count'] >= min_judged_tasks,
        'summary': _conclusion(
            [layer for facet in LAYER_FACETS for layer in layers[facet]],
            overall=overall,
            has_data=bool(tasks),
            flagged_count=flagged_count,
        ),
    }
