"""
AI 链路本地预检脚本。

不调用 DeepSeek，不消耗 token。它验证 20 个固定命盘样本是否能完成：
- 本地排盘
- 命盘上下文 schema 构建
- 知识库片段命中
- 当前大运识别
- Prompt 构建
- DeepSeek 请求参数校验

用法：
    python tools/eval_ai_pipeline.py
"""
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import ai_service
from ai_client import validate_chat_payload
from ai_context import SECTION_NAMES, build_bazi_context
from bazi_engine import paipan
from bazi_knowledge import KNOWLEDGE_VERSION, build_knowledge_packet


SAMPLES = [
    ('样本01', 'male', 1984, 2, 4, 23, 30),
    ('样本02', 'female', 1986, 6, 12, 8, 15),
    ('样本03', 'male', 1988, 9, 1, 14, 5),
    ('样本04', 'female', 1990, 1, 27, 3, 40),
    ('样本05', 'male', 1991, 11, 8, 19, 20),
    ('样本06', 'female', 1992, 5, 20, 6, 50),
    ('样本07', 'male', 1993, 12, 31, 22, 10),
    ('样本08', 'female', 1994, 7, 7, 12, 0),
    ('样本09', 'male', 1995, 8, 16, 10, 30),
    ('样本10', 'female', 1996, 2, 29, 0, 45),
    ('样本11', 'male', 1997, 4, 18, 16, 25),
    ('样本12', 'female', 1998, 10, 3, 21, 55),
    ('样本13', 'male', 1999, 3, 9, 9, 5),
    ('样本14', 'female', 2000, 6, 6, 13, 35),
    ('样本15', 'male', 2001, 1, 1, 5, 5),
    ('样本16', 'female', 2002, 8, 24, 18, 18),
    ('样本17', 'male', 2003, 11, 15, 11, 11),
    ('样本18', 'female', 2004, 2, 29, 7, 45),
    ('样本19', 'male', 2005, 9, 30, 15, 15),
    ('样本20', 'female', 2006, 12, 12, 2, 20),
]


SCENARIO_EXPECTATIONS = {
    '样本01': ['strength:身弱', 'geju:七杀格', 'ten_god:正财'],
    '样本03': ['strength:身弱', 'geju:伤官格', 'ten_god:伤官'],
    '样本09': ['strength:身弱', 'geju:伤官格', 'ten_god:正官', 'ten_god:七杀', 'ten_god:正印'],
    '样本13': ['ten_god:正财', 'wuxing:金:high'],
    '样本15': ['strength:身弱', 'geju:正印格', 'ten_god:七杀'],
    '样本19': ['strength:身弱', 'ten_god:偏财', 'relation:刑'],
}


def check_sample(sample):
    name, gender, year, month, day, hour, minute = sample
    result = paipan(year, month, day, hour, minute, gender)
    context = build_bazi_context(result)

    issues = []
    if len(context.get('pillars') or []) != 4:
        issues.append('四柱 schema 不完整')
    if not context.get('luck', {}).get('current_dayun'):
        issues.append('未识别当前大运')
    if len(context.get('evidence_terms') or []) < 8:
        issues.append('命盘证据关键词过少')

    knowledge = build_knowledge_packet(context)
    if knowledge.get('version') != KNOWLEDGE_VERSION:
        issues.append('知识库版本缺失')
    if len(knowledge.get('items') or []) < 3:
        issues.append('命中知识片段过少')
    knowledge_ids = {item['id'] for item in knowledge.get('items') or []}
    for expected_id in SCENARIO_EXPECTATIONS.get(name, []):
        if expected_id not in knowledge_ids:
            issues.append(f'场景期望未命中：{expected_id}')

    system_prompt, user_prompt = ai_service.build_prompt(result)
    if '不得重新排盘' not in system_prompt:
        issues.append('Prompt 缺少不得重新排盘约束')
    if '当前大运' not in user_prompt:
        issues.append('Prompt 缺少当前大运上下文')
    if KNOWLEDGE_VERSION not in user_prompt or '可引用知识片段' not in user_prompt:
        issues.append('Prompt 缺少知识库片段')
    for section in SECTION_NAMES:
        if f'【{section}】' not in user_prompt:
            issues.append(f'Prompt 缺少 {section} 板块要求')

    payload = ai_service.build_payload(system_prompt, user_prompt)
    payload_issues = validate_chat_payload(payload)
    if payload_issues:
        issues.extend(payload_issues)

    s_system, s_user = ai_service.build_structured_prompt(result)
    if KNOWLEDGE_VERSION not in s_user or '允许引用书名' not in s_user:
        issues.append('结构化 Prompt 缺少知识库片段')
    s_payload = ai_service.build_structured_payload(s_system, s_user)
    s_payload_issues = validate_chat_payload(s_payload)
    if s_payload_issues:
        issues.extend([f'结构化请求：{issue}' for issue in s_payload_issues])

    return {
        'name': name,
        'solar_date': result.get('solar_date'),
        'day_master': result.get('day_master'),
        'current_dayun': context.get('luck', {}).get('current_dayun', {}).get('label', ''),
        'knowledge_items': len(knowledge.get('items') or []),
        'issues': issues,
    }


def main():
    rows = [check_sample(sample) for sample in SAMPLES]
    failed = [row for row in rows if row['issues']]
    summary = {
        'total': len(rows),
        'passed': len(rows) - len(failed),
        'failed': len(failed),
        'model': ai_service.DEEPSEEK_MODEL,
        'prompt_version': ai_service.PROMPT_VERSION,
        'knowledge_version': KNOWLEDGE_VERSION,
    }

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    for row in rows:
        status = 'OK' if not row['issues'] else 'FAIL'
        print(
            f"{status} {row['name']} {row['solar_date']} "
            f"日主={row['day_master']} 当前大运={row['current_dayun']} "
            f"知识片段={row['knowledge_items']}"
        )
        for issue in row['issues']:
            print(f"  - {issue}")

    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
