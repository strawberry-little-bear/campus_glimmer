"""Explainable matching helpers for campus lost-and-found posts."""

import re
from datetime import timedelta

from django.utils import timezone

from .models import LostFoundPost


def _tokens(value):
    """Return small, language-agnostic tokens for lightweight matching."""
    value = (value or '').lower()
    tokens = set(re.findall(r'[a-z0-9]+', value))
    for block in re.findall(r'[\u4e00-\u9fff]+', value):
        tokens.update(block[index:index + 2] for index in range(len(block) - 1))
    return tokens


def score_lost_found_posts(source, candidate):
    """Return ``(score, reasons)`` for two opposite-type records.

    The score is intentionally rule-based and explainable so users can see why
    a possible match was surfaced. It is not a claim that two records refer to
    the same physical item.
    """
    if source.pk and candidate.pk and source.pk == candidate.pk:
        return 0, []
    if source.post_type == candidate.post_type:
        return 0, []

    score = 0
    reasons = []
    if source.category_id and source.category_id == candidate.category_id:
        score += 25
        reasons.append('物品分类一致')
    if source.location_id and source.location_id == candidate.location_id:
        score += 25
        reasons.append('发生地点一致')

    if source.occurred_at and candidate.occurred_at:
        delta = abs(source.occurred_at - candidate.occurred_at)
        if delta <= timedelta(days=1):
            score += 25
            reasons.append('发生时间相差 1 天内')
        elif delta <= timedelta(days=3):
            score += 18
            reasons.append('发生时间相差 3 天内')
        elif delta <= timedelta(days=7):
            score += 10
            reasons.append('发生时间相差 7 天内')

    source_text = ' '.join((source.title, source.description, source.identifying_features))
    candidate_text = ' '.join((candidate.title, candidate.description, candidate.identifying_features))
    shared_tokens = _tokens(source_text) & _tokens(candidate_text)
    source_tokens = _tokens(source_text)
    candidate_tokens = _tokens(candidate_text)
    if shared_tokens:
        ratio = len(shared_tokens) / max(1, min(len(source_tokens), len(candidate_tokens)))
        text_score = min(25, max(5, round(25 * ratio)))
        score += text_score
        examples = '、'.join(sorted(shared_tokens, key=lambda token: (-len(token), token))[:3])
        reasons.append(f'描述存在相似关键词：{examples}')

    return min(score, 100), reasons


def find_lost_found_matches(post, *, limit=6, minimum_score=30):
    """Find and rank active opposite-type records for a post."""
    if not post or post.status != 'active':
        return []
    candidates = LostFoundPost.objects.filter(
        status='active',
        post_type='found' if post.post_type == 'lost' else 'lost',
    ).exclude(pk=post.pk).exclude(reporter_id=post.reporter_id).select_related(
        'reporter', 'category', 'location',
    )[:300]
    matches = []
    for candidate in candidates:
        score, reasons = score_lost_found_posts(post, candidate)
        if score >= minimum_score:
            matches.append({
                'post': candidate,
                'score': score,
                'reasons': reasons,
                'reason_text': '、'.join(reasons),
            })
    matches.sort(key=lambda value: (value['score'], value['post'].created_at), reverse=True)
    return matches[:limit]


def expire_lost_found_posts():
    """Hide records whose optional public window has elapsed."""
    return LostFoundPost.objects.filter(
        status='active', expires_at__isnull=False, expires_at__lte=timezone.now(),
    ).update(status='expired')
