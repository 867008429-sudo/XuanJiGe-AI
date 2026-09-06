"""Offline chat reply audit helpers (P3).

The hot chat path only records safe replay text. This module runs out of band:
it reads reply_text locally, judges high-risk wording, then stores metadata-only
audit results so operations can track misleading-expression rate without
persisting private chat content twice.
"""
import logging
import re

import db
from ai_validator import RISKY_PHRASES
from bazi_knowledge import ALLOWED_CLASSIC_BOOKS
from chat_graph import validate_grounded_citations


JUDGE_NAME = 'heuristic-v1'
logger = logging.getLogger('xuanjige.chat.audit')

RISKY_CATEGORIES = {
    '必定发财': 'misleading_investment_claim',
    '稳赚不赔': 'misleading_investment_claim',
    '包赚': 'misleading_investment_claim',
    '保证发财': 'misleading_investment_claim',
    '一定暴富': 'misleading_investment_claim',
    '一定离婚': 'relationship_absolute_claim',
    '必然离婚': 'relationship_absolute_claim',
    '必有大病': 'medical_diagnosis_claim',
    '诊断为': 'medical_diagnosis_claim',
    '你患有': 'medical_diagnosis_claim',
    '肯定患有': 'medical_diagnosis_claim',
    '必须离职': 'career_absolute_claim',
    '必须分手': 'relationship_absolute_claim',
    '一定死亡': 'medical_diagnosis_claim',
}

MISLEADING_VARIANTS = [
    ('misleading_investment_claim', ['稳稳翻倍', '闭眼入', '躺赚', '不会亏', '只涨不跌', '翻倍没问题', '满仓', '梭哈']),
    ('relationship_absolute_claim', ['必离', '必分', '一定分手', '注定分开']),
    ('medical_diagnosis_claim', ['确诊', '一定会得病', '肯定会得病', '必得重病']),
    ('fatalistic_absolute_claim', ['命中注定无法改变', '毫无转机', '没得救']),
]


def _add_issue(issues, code):
    if code not in issues:
        issues.append(code)


def _classic_reference_issues(content, issues):
    refs = []
    refs.extend(re.findall(r'《([^》]{2,12})》', content))
    refs.extend(re.findall(r'——([^：:\n]{2,12})云[：:]', content))
    for book in refs:
        if book.strip() and book.strip() not in ALLOWED_CLASSIC_BOOKS:
            _add_issue(issues, 'unverified_classic_reference')
            return


def audit_reply_text(text, retrieved_chunk_ids=None):
    """Return metadata-safe audit result for one chat reply."""
    content = str(text or '')
    issues = []

    for phrase in RISKY_PHRASES:
        if phrase in content:
            _add_issue(issues, RISKY_CATEGORIES.get(phrase, 'risky_absolute_claim'))

    for code, phrases in MISLEADING_VARIANTS:
        if any(phrase in content for phrase in phrases):
            _add_issue(issues, code)

    _classic_reference_issues(content, issues)
    grounding = validate_grounded_citations(content, retrieved_chunk_ids)
    if not grounding['ok']:
        _add_issue(issues, 'unverified_classic_chunk_id')

    return {
        'verdict': 'fail' if issues else 'pass',
        'issue_codes': issues,
    }


def run_chat_audit(sample_rate=0.05, limit=100, min_count=1, dry_run=False,
                   judge=JUDGE_NAME):
    """Audit a deterministic sample of saved chat replies.

    The returned summary intentionally excludes reply_text. Use sample_rate=1.0
    in tests or one-off backfills when every saved reply should be judged.
    """
    if judge != JUDGE_NAME:
        raise ValueError(f'未知 chat audit judge: {judge}')

    candidates = db.list_chat_audit_candidates(
        sample_rate=sample_rate,
        limit=limit,
        min_count=min_count,
        judge=judge,
    )
    summary = {
        'judge': judge,
        'sample_rate': float(sample_rate),
        'dry_run': bool(dry_run),
        'candidates': len(candidates),
        'audited': 0,
        'passed': 0,
        'failed': 0,
        'logs_written': 0,
        'results': [],
    }

    for row in candidates:
        audit = audit_reply_text(
            row.get('reply_text', ''),
            row.get('retrieved_chunk_ids'),
        )
        summary['audited'] += 1
        if audit['verdict'] == 'fail':
            summary['failed'] += 1
        else:
            summary['passed'] += 1

        written = False
        if not dry_run:
            written = db.save_chat_audit_log(
                thread_id=row['thread_id'],
                request_id=row['request_id'],
                persona=row.get('persona', ''),
                verdict=audit['verdict'],
                issue_codes=audit['issue_codes'],
                reply_text=row.get('reply_text', ''),
                sample_rate=sample_rate,
                judge=judge,
            )
            if written:
                summary['logs_written'] += 1

        summary['results'].append({
            'thread_id': row['thread_id'],
            'request_id': row['request_id'],
            'persona': row.get('persona', ''),
            'verdict': audit['verdict'],
            'issue_codes': audit['issue_codes'],
            'written': written,
        })

    return summary
