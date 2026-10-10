# -*- coding: utf-8 -*-
"""Group failed task runs by what kind of problem they actually were.

The run ledger already says *which* command failed and how often. That is
enough to notice a broken schedule but not enough to fix one: a digest command
that failed nine times last week and an expiry command that failed nine times
last week need completely different responses, and neither is visible in a
count. The note field holds a free-text "ExceptionName: message" string per
failure, so the shape of a failure has never been aggregated at all - which is
why an operator ends up reading nine nearly identical rows to discover they
are one problem, or nine unrelated rows and assuming they are.

This module classifies each failed run into a small, closed set of causes and
aggregates the counts. It deliberately does not decide anything from the
classification. A ConfigurationError might be a missing SMTP setting that
breaks the digest command forever, or a CommandError raised because there was
nothing to expire today - the same exception name covers both, and only the
message and the surrounding rows tell them apart. Treating a category as a
verdict would turn "this is usually a transient blip" into "this is fine to
ignore", so the categories only ever describe what a failure looked like. The
judgement stays with the operator, and the rows are kept so they can go read
the notes.

Only the columns this module needs are read, so aggregation cost does not grow
with how many runs have accumulated.
"""

from datetime import timedelta

from django.utils import timezone

from .models import TaskRun


# The categories, in the order an operator should read them. The order is a
# judgement about triage rather than about severity: external dependencies are
# listed first because they are the ones most likely to resolve themselves,
# and an operator who starts there can clear the transient noise before
# spending attention on the rows that need real work.
CAUSE_CONFIG = 'config'
CAUSE_DATA = 'data'
CAUSE_EXTERNAL = 'external'
CAUSE_UNKNOWN = 'unknown'

# Exception names mapped onto the causes above. Only the names a scheduled
# command can realistically raise are listed; anything else lands in
# CAUSE_UNKNOWN rather than being guessed into a category it may not belong to.
_EXCEPTION_CAUSES = {
    # Configuration and parameters: something about the deployment or the
    # invocation is wrong, and it will keep failing until somebody changes it.
    'CommandError': CAUSE_CONFIG,
    'ImproperlyConfigured': CAUSE_CONFIG,
    'ValidationError': CAUSE_CONFIG,
    'FieldError': CAUSE_CONFIG,
    # Data: the database is in a state the command did not expect.
    'IntegrityError': CAUSE_DATA,
    'ObjectDoesNotExist': CAUSE_DATA,
    'MultipleObjectsReturned': CAUSE_DATA,
    'DataError': CAUSE_DATA,
    'DatabaseError': CAUSE_DATA,
    'ValueError': CAUSE_DATA,
    'TypeError': CAUSE_DATA,
    'KeyError': CAUSE_DATA,
    'IndexError': CAUSE_DATA,
    # External dependencies: the database, the network, or the mail server was
    # unreachable or too slow. Retrying is the usual fix.
    'OperationalError': CAUSE_EXTERNAL,
    'InterfaceError': CAUSE_EXTERNAL,
    'ConnectionError': CAUSE_EXTERNAL,
    'TimeoutError': CAUSE_EXTERNAL,
    'OSError': CAUSE_EXTERNAL,
    'SMTPException': CAUSE_EXTERNAL,
    'RequestException': CAUSE_EXTERNAL,
    'HTTPError': CAUSE_EXTERNAL,
    'URLError': CAUSE_EXTERNAL,
}

# What each category is called on the page, and the one-line explanation that
# keeps the label honest. The wording says what the category means for the
# operator's next action, not how serious it is.
CAUSE_LABELS = {
    CAUSE_CONFIG: '配置与参数',
    CAUSE_DATA: '数据状态',
    CAUSE_EXTERNAL: '外部依赖',
    CAUSE_UNKNOWN: '未能归类',
}

CAUSE_HINTS = {
    CAUSE_CONFIG: '命令本身被拒绝执行或参数不合法，通常改配置或改调用方式即可恢复',
    CAUSE_DATA: '数据库里的数据与命令的预期不符，需要先查清是哪条记录',
    CAUSE_EXTERNAL: '数据库、网络或邮件服务不可达或超时，多数情况下重试即可恢复',
    CAUSE_UNKNOWN: '备注里没有可识别的异常名，需要直接查看原始记录',
}

CAUSE_ORDER = (CAUSE_CONFIG, CAUSE_DATA, CAUSE_EXTERNAL, CAUSE_UNKNOWN)

# How far back to look. Matches the widest period the dashboard offers so the
# aggregation can be read alongside the rest of the panel without a second
# time range to keep straight.
DEFAULT_DAYS = 30

# Only these columns are read, so the cost does not grow with run count.
PERIOD_FIELDS = ('id', 'name', 'status', 'started_at', 'note')


def classify_failure_note(note):
    """Return the cause category of one failed run, from its note.

    The ledger writes "{ExceptionName}: {message}", so the name is everything
    before the first colon. A note that is empty, or whose first token is not
    a plausible exception name, is left as CAUSE_UNKNOWN rather than matched
    loosely - a wrong category is worse than no category, because it sends the
    operator looking in the wrong place.
    """
    if not note:
        return CAUSE_UNKNOWN
    head = note.split(':', 1)[0].strip()
    if not head or ' ' in head:
        # A bare message with no exception name in front of it carries no
        # classification information.
        return CAUSE_UNKNOWN
    return _EXCEPTION_CAUSES.get(head, CAUSE_UNKNOWN)


def _failure_cause_rows(rows):
    """Aggregate failed rows into one entry per cause."""
    buckets = {}
    for row in rows:
        if row['status'] != 'failed':
            continue
        cause = classify_failure_note(row['note'])
        bucket = buckets.setdefault(cause, {
            'cause': cause,
            'label': CAUSE_LABELS[cause],
            'hint': CAUSE_HINTS[cause],
            'count': 0,
            'commands': {},
            'last_failure_at': None,
            'last_note': '',
        })
        bucket['count'] += 1
        bucket['commands'][row['name']] = bucket['commands'].get(row['name'], 0) + 1
        started_at = row['started_at']
        if bucket['last_failure_at'] is None or started_at > bucket['last_failure_at']:
            bucket['last_failure_at'] = started_at
            bucket['last_note'] = row['note'] or ''

    causes = []
    for cause in CAUSE_ORDER:
        bucket = buckets.get(cause)
        if not bucket:
            continue
        # The commands affected, most failures first, so the entry answers
        # "which command is driving this category" without a second query.
        bucket['command_rows'] = [
            {'name': name, 'count': count}
            for name, count in sorted(bucket['commands'].items(), key=lambda item: (-item[1], item[0]))
        ]
        bucket['command_count'] = len(bucket['command_rows'])
        del bucket['commands']
        causes.append(bucket)
    # Largest first, so the entry the operator reads first is the one that
    # accounts for most of the period. The fixed order above is only the
    # tie-break, which keeps two equal-sized categories in a stable reading
    # order instead of swapping places between refreshes.
    causes.sort(key=lambda row: -row['count'])
    return causes


def _summary_text(total_failed, causes):
    """One sentence for the page, leading with the largest cause."""
    if not total_failed:
        return '周期内没有失败的运行记录。'
    top = causes[0]
    if top['cause'] == CAUSE_UNKNOWN:
        # Nothing was recognised, so there is no cause to lead with. Saying so
        # is more useful than naming a category that explains nothing.
        return (
            f'周期内有 {total_failed} 次失败，但备注里没有可识别的异常名，'
            f'需要直接查看原始记录。'
        )
    return (
        f'周期内 {total_failed} 次失败中，{top["count"]} 次属于「{top["label"]}」，'
        f'涉及 {top["command_count"]} 个命令。'
    )


def build_task_run_failure_causes(days=DEFAULT_DAYS, now=None):
    """Classify and aggregate the failed runs of a period.

    Dry runs are excluded for the same reason the health module excludes them:
    a run that sends nothing cannot vouch for anything, and its failures say
    nothing about the schedule either.

    The rows are returned per cause so the operator can act on a whole class of
    failures at once instead of reading them one by one.
    """
    now = now or timezone.now()
    since = now - timedelta(days=days)

    rows = list(
        TaskRun.objects.filter(started_at__gte=since, status='failed')
        .exclude(note='dry-run')
        .order_by('-started_at')
        .values(*PERIOD_FIELDS)
    )

    causes = _failure_cause_rows(rows)
    classified = sum(cause['count'] for cause in causes if cause['cause'] != CAUSE_UNKNOWN)
    unknown = next((cause for cause in causes if cause['cause'] == CAUSE_UNKNOWN), None)

    return {
        'days': days,
        'causes': causes,
        'total_failed': len(rows),
        'classified_count': classified,
        'unclassified_count': unknown['count'] if unknown else 0,
        'has_data': bool(rows),
        'top_cause': causes[0]['cause'] if causes else None,
        'summary': _summary_text(len(rows), causes),
    }
