# -*- coding: utf-8 -*-
"""运营看板共享构建器的请求内耗时报告。

这个模块不读业务表，也不写任何运行台账。它只把当前请求里已经用
``time.perf_counter()`` 量出来的构建器耗时整理成页面和 CSV 都能读的形状。

刻意只做请求内观测：跨请求平均需要持久化存储和统一时钟，容易把开发环境、
CI 空库和生产流量混成一组数字；APM 又依赖部署形态。当前问题更小也更具体：
这一次请求慢在共享构建的哪一段，以及有没有同一个构建器被重复调用。

分位使用最近秩而不是插值。插值会造出一次从未发生过的耗时；这里每个数字
都来自一次真实的函数调用。样本只有一个时 P50 和 P90 都是那一条，不伪装成
两个样本；没有样本时保持 ``None``。
"""

from collections import Counter


def _nearest_rank(values, percentile):
    """Return the nearest-rank percentile, or None for an empty sample."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, -(-len(ordered) * percentile // 100))
    return ordered[rank - 1]


def build_dashboard_build_performance(runs):
    """Summarize request-local dashboard builder timings.

    ``runs`` is a list of mappings with ``key``, ``label`` and ``elapsed_ms``.
    The report is intentionally stateless: callers may render it, export it, or
    drop it with the request. Nothing here is persisted or averaged across
    requests.
    """
    prepared = []
    for index, run in enumerate(runs):
        elapsed_ms = float(run['elapsed_ms'])
        if elapsed_ms < 0:
            raise ValueError('构建耗时不能是负数')
        prepared.append({
            'key': str(run['key']),
            'label': str(run['label']),
            'elapsed_ms': elapsed_ms,
            '_index': index,
        })

    prepared.sort(key=lambda row: (-row['elapsed_ms'], row['_index'], row['key']))
    total_elapsed_ms = sum(row['elapsed_ms'] for row in prepared)
    key_counts = Counter(row['key'] for row in prepared)
    duplicate_key_total = sum(count - 1 for count in key_counts.values() if count > 1)

    rows = []
    cumulative_ms = 0.0
    for position, row in enumerate(prepared):
        cumulative_ms += row['elapsed_ms']
        rows.append({
            'key': row['key'],
            'label': row['label'],
            'elapsed_ms': round(row['elapsed_ms'], 3),
            'share_percent': (
                round(row['elapsed_ms'] / total_elapsed_ms * 100, 1)
                if total_elapsed_ms else None
            ),
            'cumulative_percent': (
                round(cumulative_ms / total_elapsed_ms * 100, 1)
                if total_elapsed_ms else None
            ),
            'is_slowest': position == 0,
            'is_duplicate': key_counts[row['key']] > 1,
        })

    elapsed_values = [row['elapsed_ms'] for row in prepared]
    slowest = prepared[0] if prepared else None
    if not prepared:
        summary = '当前请求没有可读的共享构建器耗时。'
    else:
        summary = (
            f"{len(prepared)} 个共享构建器，本次计入耗时 "
            f"{total_elapsed_ms:.3f} ms；最慢是{slowest['label']}。"
        )

    return {
        'has_data': bool(prepared),
        'summary': summary,
        'metrics': {
            'builder_total': len(prepared),
            'total_elapsed_ms': round(total_elapsed_ms, 3),
            'p50_ms': (
                round(_nearest_rank(elapsed_values, 50), 3)
                if elapsed_values else None
            ),
            'p90_ms': (
                round(_nearest_rank(elapsed_values, 90), 3)
                if elapsed_values else None
            ),
            'slowest_key': slowest['key'] if slowest else '',
            'slowest_label': slowest['label'] if slowest else '',
            'duplicate_key_total': duplicate_key_total,
        },
        'rows': rows,
        'note': (
            '只统计本次请求的共享看板构建器，不含页面渲染、CSV 序列化、'
            '请求筛选和浏览器下载；耗时来自 perf_counter，不落库、不做跨请求平均。'
        ),
    }