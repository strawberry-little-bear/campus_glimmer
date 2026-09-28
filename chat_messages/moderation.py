from __future__ import annotations

import re

from .models import ModerationEvent


# Rules stay explicit and explainable so operators can understand every score.
_RULES = (
    ('微信导流', re.compile(r'(?:加|留|发|私)\s*(?:我\s*)?(?:微信|vx|v信)|微信号', re.IGNORECASE), 40),
    ('外部链接', re.compile(r'(?:https?://|www\.)', re.IGNORECASE), 20),
    ('手机号', re.compile(r'(?<!\d)1[3-9]\d{9}(?!\d)'), 35),
    ('高风险交易', re.compile(r'押金|预付款|私下转账|刷单|博彩|裸聊'), 50),
)


def analyze_content(content: str) -> dict:
    """Return explainable rule hits, a capped score, and a review priority."""
    normalized = (content or '').strip()
    hits = [
        {'label': label, 'weight': weight}
        for label, pattern, weight in _RULES
        if pattern.search(normalized)
    ]
    score = min(sum(hit['weight'] for hit in hits), 100)
    if score >= 60:
        level = 'high'
    elif score >= 30:
        level = 'medium'
    else:
        level = 'low'
    return {
        'matched_terms': [hit['label'] for hit in hits],
        'risk_score': score,
        'risk_level': level,
    }


def scan_content(content: str) -> list[str]:
    """Return stable, human-readable rule labels for risky content."""
    return analyze_content(content)['matched_terms']


def moderate_submission(content: str, *, author, channel: str, item=None):
    """Create an audit event and return it when content should be blocked."""
    analysis = analyze_content(content)
    if not analysis['matched_terms']:
        return None
    return ModerationEvent.objects.create(
        channel=channel,
        author=author,
        item=item,
        content=(content or '').strip()[:4000],
        matched_terms='、'.join(analysis['matched_terms']),
        risk_score=analysis['risk_score'],
        risk_level=analysis['risk_level'],
    )
