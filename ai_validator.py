"""
AI 输出校验层。

它不是替代人工命理评测，而是挡住最明显的产品事故：
缺板块、证据空泛、当前大运没回扣、危险承诺、医疗化表达、伪造古籍引用。
"""
import re

from ai_context import SECTION_NAMES, build_bazi_context
from bazi_knowledge import ALLOWED_CLASSIC_BOOKS


RISKY_PHRASES = [
    '必定发财', '稳赚不赔', '包赚', '保证发财', '一定暴富',
    '一定离婚', '必然离婚', '必有大病', '诊断为', '你患有', '肯定患有',
    '必须离职', '必须分手', '一定死亡',
]


def _ensure_context(paipan_data=None, context=None):
    if context is not None:
        return context
    if paipan_data is not None:
        return build_bazi_context(paipan_data)
    return None


def _non_empty_list(value):
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _evidence_hits(text, context):
    if not context:
        return []
    content = str(text or '')
    hits = []
    for term in context.get('evidence_terms') or []:
        if len(term) >= 2 and term in content:
            hits.append(term)
    return list(dict.fromkeys(hits))


def validate_structured_outline(outline, paipan_data=None, context=None):
    issues = []
    context = _ensure_context(paipan_data, context)

    if not isinstance(outline, dict):
        return ['结构化骨架不是 JSON object']

    if not str(outline.get('summary', '')).strip():
        issues.append('结构化骨架缺少 summary')

    sections = outline.get('sections')
    if not isinstance(sections, list):
        return ['结构化骨架缺少 sections 数组']

    names = [section.get('name') for section in sections if isinstance(section, dict)]
    if names[:len(SECTION_NAMES)] != SECTION_NAMES:
        issues.append('结构化骨架板块顺序不正确')
    if len(sections) != len(SECTION_NAMES):
        issues.append('结构化骨架板块数量不正确')

    for expected_name, section in zip(SECTION_NAMES, sections):
        if not isinstance(section, dict):
            issues.append(f'{expected_name}板块不是对象')
            continue

        evidence = _non_empty_list(section.get('evidence'))
        mapping = _non_empty_list(section.get('real_world_mapping'))
        advice = _non_empty_list(section.get('advice'))
        classic_hint = section.get('classic_hint') or {}

        if section.get('name') != expected_name:
            issues.append(f'{expected_name}板块名称不匹配')
        if not str(section.get('claim', '')).strip():
            issues.append(f'{expected_name}缺少核心判断')
        if len(evidence) < 2:
            issues.append(f'{expected_name}证据不足')
        elif context and len(_evidence_hits(' '.join(evidence), context)) < 1:
            issues.append(f'{expected_name}证据未命中命盘关键词')
        if len(mapping) < 2:
            issues.append(f'{expected_name}现实映射不足')
        if len(advice) < 2:
            issues.append(f'{expected_name}建议不足')
        if not isinstance(classic_hint, dict) or not str(classic_hint.get('book', '')).strip():
            issues.append(f'{expected_name}缺少古籍义理提示')
        elif str(classic_hint.get('book', '')).strip() not in ALLOWED_CLASSIC_BOOKS:
            issues.append(f'{expected_name}古籍来源不在知识库')
        elif not str(classic_hint.get('meaning', '')).strip():
            issues.append(f'{expected_name}缺少古籍义理内容')

    current_dayun = (context or {}).get('luck', {}).get('current_dayun', {}).get('label', '')
    outline_dayun = outline.get('current_dayun') or {}
    if current_dayun and isinstance(outline_dayun, dict):
        label = str(outline_dayun.get('label', '') or '')
        focus = str(outline_dayun.get('focus', '') or '')
        if current_dayun not in (label + focus):
            issues.append('结构化骨架未绑定当前大运')
    elif current_dayun:
        issues.append('结构化骨架缺少 current_dayun 对象')

    return issues


def split_sections(content):
    content = (content or '').strip()
    matches = list(re.finditer(r'【(性格|财运|婚姻|健康|大运|总评)】', content))
    sections = {}
    for idx, match in enumerate(matches):
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(content)
        sections[match.group(1)] = content[start:end].strip()
    return matches, sections


def validate_interpretation(text, paipan_data=None, context=None, finish_reason=None):
    issues = []
    context = _ensure_context(paipan_data, context)
    content = (text or '').strip()

    if finish_reason == 'length':
        issues.append('输出被模型截断')
    if len(content) < 900:
        issues.append('解读正文过短')

    section_matches, sections = split_sections(content)
    found_sections = [m.group(1) for m in section_matches]
    missing_sections = [name for name in SECTION_NAMES if name not in found_sections]
    if missing_sections:
        issues.append('缺少板块：' + '、'.join(missing_sections))
    elif found_sections[:len(SECTION_NAMES)] != SECTION_NAMES:
        issues.append('六个板块顺序不正确')

    for name in SECTION_NAMES:
        if name in sections and len(sections[name]) < 80:
            issues.append(f'{name}板块内容过短')

    dayun_text = sections.get('大运', '')
    current_dayun = (context or {}).get('luck', {}).get('current_dayun', {}).get('label', '')
    if dayun_text and current_dayun and current_dayun not in dayun_text and '当前大运' not in dayun_text:
        issues.append('大运板块未明确回扣当前大运')

    evidence_hits = _evidence_hits(content, context)
    if context and len(evidence_hits) < 3:
        issues.append('正文命盘证据引用不足')

    classic_books = re.findall(r'——([^：:\n]{2,12})云[：:]', content)
    unknown_books = [book for book in classic_books if book not in ALLOWED_CLASSIC_BOOKS]
    if unknown_books:
        issues.append('含知识库外古籍引用：' + '、'.join(list(dict.fromkeys(unknown_books))[:3]))

    hit_risks = [phrase for phrase in RISKY_PHRASES if phrase in content]
    if hit_risks:
        issues.append('含绝对化或医疗化表述：' + '、'.join(hit_risks[:3]))

    return {
        'ok': len(issues) == 0,
        'issues': issues,
        'evidence_hits': evidence_hits[:12],
    }
