from __future__ import annotations

import re
from collections import namedtuple

from .models import ModerationEvent


# Rules stay explicit and explainable so operators can understand every score.
# risk_tier is a review cadence, while risk_score remains an explainable sum of
# rule weights. Keeping both avoids turning a normal link into a critical event.
ModerationRule = namedtuple('ModerationRule', ('label', 'pattern', 'weight', 'risk_tier'))

RISK_TIER_RANK = {
    'caution': 0,
    'elevated': 1,
    'critical': 2,
}

_RULES = (
    ModerationRule(
        '微信导流',
        re.compile(r'(?:加|留|发|私)\s*(?:我\s*)?(?:微信|vx|v信)|微信号', re.IGNORECASE),
        40, 'elevated',
    ),
    ModerationRule(
        '外部链接', re.compile(r'(?:https?://|www\.)', re.IGNORECASE), 20, 'caution',
    ),
    ModerationRule(
        '手机号', re.compile(r'(?<!\d)1[3-9]\d{9}(?!\d)'), 35, 'elevated',
    ),
    ModerationRule(
        '高风险交易', re.compile(r'押金|预付款|私下转账|刷单|博彩|裸聊'), 50, 'critical',
    ),
)


def risk_tier_for_labels(labels):
    """Return the highest configured tier for historical or matched labels."""
    label_set = set(labels or ())
    tiers = {
        rule.risk_tier for rule in _RULES
        if rule.label in label_set
    }
    return max(tiers, key=RISK_TIER_RANK.get) if tiers else 'caution'


def analyze_content(content: str) -> dict:
    """Return explainable rule hits, a capped score, and a review priority."""
    normalized = (content or '').strip()
    hits = [
        {'label': rule.label, 'weight': rule.weight, 'risk_tier': rule.risk_tier}
        for rule in _RULES
        if rule.pattern.search(normalized)
    ]
    score = min(sum(hit['weight'] for hit in hits), 100)
    if score >= 60:
        level = 'high'
    elif score >= 30:
        level = 'medium'
    else:
        level = 'low'
    risk_tier = risk_tier_for_labels(hit['label'] for hit in hits) if hits else None
    return {
        'matched_terms': [hit['label'] for hit in hits],
        'risk_score': score,
        'risk_level': level,
        'risk_tier': risk_tier,
    }


def moderate_submission(content: str, *, author, channel: str, item=None, receiver=None):
    """Create an audit event and return it when content should be blocked."""
    analysis = analyze_content(content)
    if not analysis['matched_terms']:
        return None
    return ModerationEvent.objects.create(
        channel=channel,
        author=author,
        item=item,
        receiver=receiver if channel == 'private_message' else None,
        # 放行时要按原文恢复，不能在这里先截断；输入长度由表单限制。
        content=(content or '').strip(),
        matched_terms='、'.join(analysis['matched_terms']),
        risk_score=analysis['risk_score'],
        risk_level=analysis['risk_level'],
        risk_tier=analysis['risk_tier'],
    )
