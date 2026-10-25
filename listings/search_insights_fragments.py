# -*- coding: utf-8 -*-
"""搜索洞察分区的按需取回表：锚点 -> 渲染这个分区需要什么。

``search_insights.html`` 是全仓库最大的模板（43KB / 458 行），视图一次 eager
调九个构建器后整页吐出。它的分区不像运营看板那样能靠「挑面板」裁剪——九个
构建器里有八个各喂一个分区，剩下的全部挤在 ``build_search_insights`` 这一个
builder 里。所以这里比运营看板多一列：分区除了要点名自己要的面板，还要点名
自己要的统计块（``parts``），由 ``build_search_insights`` 自己在内部按块短路。

于是这张表有三列而不是两列：

* ``parts``——``build_search_insights`` 内部要跑的统计块。表里只写直接依赖，
  传递闭包由 ``analytics._close_search_insight_parts`` 补：``metrics`` 要
  ``terms/counts/clicks``，``clicks`` 要 ``counts``，``insights`` 要 ``terms``。
  闭包放在 builder 一侧，是为了让直接调 ``build_search_insights(parts=...)``
  的人（比如测试）不用先手工把依赖摊平；
* ``panels``——要跑的独立面板构建器键，空元组表示一个都不跑。

表的顺序就是页面顺序，由 ``test_search_insights_fragments`` 反查盯着：拿一次
真实渲染插桩，看每个分区到底读了哪些顶层键，再和这张表对。模板新增一个键而
表里没登记，那条测试就红——不会出现「分区滚到了才发现少了一半数据」。

首屏只有两个分区：八张指标卡加搜索质量分。它们合起来就是这页的记分卡，都在
折叠线以上；剩下的十三个分区滚到再取。
"""

# (锚点, partial, 需要的统计块, 需要的独立面板键)。
# 顺序即页面顺序。
SEARCH_INSIGHTS_SECTION_FRAGMENTS = (
    ('si-metrics', '_metrics.html', ('metrics',), ()),
    ('si-quality', '_quality.html', ('quality',), ()),
    ('si-trend', '_trend.html', (), ('search_trend', 'combo_shift')),
    ('si-period-comparison', '_period_comparison.html', ('comparison',), ()),
    ('si-rhythm', '_rhythm.html', ('rhythm',), ()),
    ('si-terms', '_terms.html', ('terms',), ()),
    ('si-items', '_items.html', ('metrics',), ()),
    ('si-facet-gaps', '_facet_gaps.html', ('facets',), ()),
    ('si-rewrites', '_rewrites.html', (), ('rewrite_candidates',)),
    ('si-synonym-effects', '_synonym_effects.html', (), ('synonym_effects',)),
    ('si-rejection-stats', '_rejection_stats.html', (), ('rejection_stats',)),
    ('si-rejection-recurrence', '_rejection_recurrence.html', (), ('rejection_recurrence',)),
    ('si-threshold', '_threshold.html', (), ('threshold_feedback',)),
    ('si-position-quality', '_position_quality.html', (), ('position_quality',)),
    ('si-insights', '_insights.html', ('insights',), ()),
)

# 锚点 -> 那一行。分区是顺序结构，查找却全是按键，所以两份都要留。
SEARCH_INSIGHTS_SECTION_FRAGMENT_MAP = {
    anchor: (partial, parts, panels)
    for anchor, partial, parts, panels in SEARCH_INSIGHTS_SECTION_FRAGMENTS
}

# 不带 ``si_lazy`` cookie 的请求也先渲染的两个分区：它们在折叠线以上，先渲染
# 出来用户第一眼就有东西看，不必等一次取回往返。
SEARCH_INSIGHTS_FIRST_SCREEN = frozenset({'si-metrics', 'si-quality'})


def section_anchors():
    """按页面顺序返回全部分区锚点。"""
    return tuple(anchor for anchor, _p, _t, _k in SEARCH_INSIGHTS_SECTION_FRAGMENTS)


def section_partial(anchor):
    """分区对应的 partial 文件名；锚点不存在时返回 None。"""
    row = SEARCH_INSIGHTS_SECTION_FRAGMENT_MAP.get(anchor)
    return row[0] if row else None


def section_core_parts(anchors):
    """这些分区一共要跑哪些统计块。

    返回 None 表示「全跑」——``anchors`` 为空或为 None 时就是这样：整页渲染和
    CSV 导出都要完整一份，此时不该做任何裁剪。
    """
    if not anchors:
        return None
    wanted = set()
    for anchor in anchors:
        row = SEARCH_INSIGHTS_SECTION_FRAGMENT_MAP.get(anchor)
        if row:
            wanted.update(row[1])
    return wanted


def section_panel_keys(anchors):
    """这些分区需要哪些独立面板的键。None 表示全部。"""
    if not anchors:
        return None
    wanted = set()
    for anchor in anchors:
        row = SEARCH_INSIGHTS_SECTION_FRAGMENT_MAP.get(anchor)
        if row:
            wanted.update(row[2])
    return wanted
