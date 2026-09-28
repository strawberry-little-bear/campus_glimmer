from __future__ import annotations

import re

from .models import ModerationEvent


# These are intentionally small, explainable rules. A later iteration can replace
# them with a configurable rule table without changing the request flow.
_RULES = (
    ('微信导流', re.compile(r'(?:加|留|发|私)\s*(?:我\s*)?(?:微信|vx|v信)|微信号', re.IGNORECASE)),
    ('外部链接', re.compile(r'(?:https?://|www\.)', re.IGNORECASE)),
    ('手机号', re.compile(r'(?<!\d)1[3-9]\d{9}(?!\d)')),
    ('高风险交易', re.compile(r'押金|预付款|私下转账|刷单|博彩|裸聊')),
)


def scan_content(content: str) -> list[str]:
    """Return stable, human-readable rule labels for risky content."""
    normalized = (content or '').strip()
    return [label for label, pattern in _RULES if pattern.search(normalized)]


def moderate_submission(content: str, *, author, channel: str, item=None):
    """Create an audit event and return it when content should be blocked."""
    matched_terms = scan_content(content)
    if not matched_terms:
        return None
    return ModerationEvent.objects.create(
        channel=channel,
        author=author,
        item=item,
        content=(content or '').strip()[:4000],
        matched_terms='、'.join(matched_terms),
    )
