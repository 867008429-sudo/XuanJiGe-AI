"""
Lightweight Bazi knowledge layer.

This is intentionally a rules table, not a vector database. The goal is to
give the model traceable interpretation hints without letting it invent
classic-book citations, medical claims, or unverifiable doctrine.
"""
import re


KNOWLEDGE_VERSION = 'bazi-knowledge-v1'
ALLOWED_CLASSIC_BOOKS = ['滴天髓', '子平真诠', '三命通会', '渊海子平', '穷通宝鉴']

TEN_GOD_HINTS = {
    '比肩': {
        'topic': '自我与同辈',
        'meaning': '比肩重自我、同辈、竞争与独立，宜看是否为喜用以及是否过旺。',
        'advice': '表达主见时保留协作空间，避免把所有事都变成硬碰硬。',
        'book': '渊海子平',
    },
    '劫财': {
        'topic': '资源竞争',
        'meaning': '劫财主分夺、行动力和圈层资源，成败常在边界感与财务纪律。',
        'advice': '合作、借贷、合伙项目要先定规则，再谈义气。',
        'book': '三命通会',
    },
    '食神': {
        'topic': '表达与产出',
        'meaning': '食神偏稳定输出、技术表达、审美与口碑，喜见有序发挥。',
        'advice': '把才华做成作品、流程或可复用能力，比临场发挥更稳。',
        'book': '子平真诠',
    },
    '伤官': {
        'topic': '才气与规则',
        'meaning': '伤官主表达、锋芒、突破规则；见官杀时要分清制化与冲突。',
        'advice': '才华要落到解决问题，不要只落到反感约束。',
        'book': '子平真诠',
    },
    '正财': {
        'topic': '稳定财富',
        'meaning': '正财重秩序、现金流、责任与可持续收入，忌空谈暴富。',
        'advice': '优先经营稳定现金流和清晰账目，少做无验证的重押。',
        'book': '渊海子平',
    },
    '偏财': {
        'topic': '机会财富',
        'meaning': '偏财重机会、资源调度、项目收益，也更考验风险边界。',
        'advice': '机会来时先算成本、周期和退出条件，不把运气当能力。',
        'book': '三命通会',
    },
    '正官': {
        'topic': '秩序与责任',
        'meaning': '正官重规则、名誉、职位和责任，喜清正有制，忌被伤官冲破。',
        'advice': '在制度清楚的平台中积累信用，比频繁换赛道更有利。',
        'book': '子平真诠',
    },
    '七杀': {
        'topic': '压力与决断',
        'meaning': '七杀主压力、竞争、决断和外部约束；有印化杀则压力可变资质。',
        'advice': '把压力转成证书、流程、作品和专业壁垒，少用硬扛解决一切。',
        'book': '滴天髓',
    },
    '正印': {
        'topic': '保护与学习',
        'meaning': '正印主学习、保护、资质、贵人和稳定支持，身弱时尤其重要。',
        'advice': '遇到压力先补知识、资质和节奏，让体系托住发挥。',
        'book': '滴天髓',
    },
    '偏印': {
        'topic': '灵感与偏门',
        'meaning': '偏印主灵感、非标知识、独处研究，也可能带来想多和节奏不稳。',
        'advice': '适合深钻冷门能力，但要用输出和反馈防止闭门造车。',
        'book': '穷通宝鉴',
    },
}

STRENGTH_HINTS = {
    '身弱': {
        'topic': '旺衰取用',
        'meaning': '身弱先看印比生扶，再看财官食伤是否形成压力。',
        'advice': '现实中先补体力、资源、贵人和流程，再追求扩张。',
        'book': '滴天髓',
    },
    '身强': {
        'topic': '旺衰取用',
        'meaning': '身强宜看财官食伤能否疏泄与成事，忌一味堆自我。',
        'advice': '把能量导向结果、规则和商业闭环，少陷入自我消耗。',
        'book': '子平真诠',
    },
}

WUXING_HINTS = {
    '金': {
        'topic': '金性',
        'balanced': '金主规则、判断、边界和执行。',
        'too_high': '金偏旺时容易锋利、挑剔、紧绷，宜增加柔性沟通。',
        'too_low': '金偏弱时规则感和决断力不足，宜建立清单、复盘和边界。',
        'book': '穷通宝鉴',
    },
    '木': {
        'topic': '木性',
        'balanced': '木主生发、规划、学习和成长。',
        'too_high': '木偏旺时想法多、压力牵引强，宜先定优先级。',
        'too_low': '木偏弱时成长动力不足，宜用目标拆解和环境推动。',
        'book': '穷通宝鉴',
    },
    '水': {
        'topic': '水性',
        'balanced': '水主流动、信息、财源、适应和思考。',
        'too_high': '水偏旺时易多虑、漂移或财务边界弱，宜做预算和节奏管理。',
        'too_low': '水偏弱时信息流和弹性不足，宜增加学习、沟通和资源流动。',
        'book': '穷通宝鉴',
    },
    '火': {
        'topic': '火性',
        'balanced': '火主表达、热度、名声、动力和照见。',
        'too_high': '火偏旺时易急躁外放，宜慢下来做验证。',
        'too_low': '火偏弱时动力和表达不足，宜用作品展示与稳定作息补能。',
        'book': '穷通宝鉴',
    },
    '土': {
        'topic': '土性',
        'balanced': '土主承载、稳定、信用、消化和中轴。',
        'too_high': '土偏旺时易滞重、保守或过度担责，宜引入流动和反馈。',
        'too_low': '土偏弱时承载力和稳定性不足，宜先建流程、作息和资源底盘。',
        'book': '穷通宝鉴',
    },
}

RELATION_HINTS = {
    '冲': {
        'topic': '地支冲',
        'meaning': '冲主动、变、迁移和关系拉扯，未必是坏事，要看喜忌与落点。',
        'advice': '遇到冲动之象，现实中适合提前做预案，少临时拍板。',
        'book': '三命通会',
    },
    '刑': {
        'topic': '地支刑',
        'meaning': '刑主内耗、规矩摩擦和反复牵制，常体现为压力感与边界问题。',
        'advice': '把含混关系写成规则，把反复事项拆成流程。',
        'book': '三命通会',
    },
    '合': {
        'topic': '地支合',
        'meaning': '合主牵连、合作、吸引与资源绑定，需看合化是否得力。',
        'advice': '合作关系要看是否共同增益，不只看表面和气。',
        'book': '渊海子平',
    },
    '害': {
        'topic': '地支害',
        'meaning': '害多指暗处不顺、误会和消耗，宜看具体宫位与十神。',
        'advice': '少做隐性承诺，重要事项留痕确认。',
        'book': '三命通会',
    },
}

GEJU_HINTS = {
    '伤官格': {
        'topic': '格局',
        'meaning': '伤官格要看才气如何被印、财、官杀引导，最忌只有锋芒没有落点。',
        'advice': '把表达力变成作品、产品、销售或专业方法论。',
        'book': '子平真诠',
    },
    '正官格': {
        'topic': '格局',
        'meaning': '正官格重清正、规则和责任，成局时利于制度平台与长期信用。',
        'advice': '守住专业形象与履约能力，少因短期情绪破坏秩序。',
        'book': '子平真诠',
    },
    '七杀格': {
        'topic': '格局',
        'meaning': '七杀格重压力、竞争和决断，贵在有制有化。',
        'advice': '用学习、资质和流程把压力转成战斗力。',
        'book': '滴天髓',
    },
    '正印格': {
        'topic': '格局',
        'meaning': '印格重学习、保护和资质，宜看能否把知识化为实际产出。',
        'advice': '不要只积累安全感，要把知识变成作品和结果。',
        'book': '滴天髓',
    },
}


def _split_text(value):
    return [part for part in re.split(r'[、,，;；\s/()（）]+', str(value or '')) if part]


def _add(items, seen, item_id, trigger, hint):
    if item_id in seen:
        return
    seen.add(item_id)
    items.append({
        'id': item_id,
        'trigger': trigger,
        'topic': hint['topic'],
        'meaning': hint['meaning'],
        'advice': hint.get('advice', ''),
        'book': hint['book'],
    })


def _ten_gods_from_context(context):
    terms = []
    for pillar in context.get('pillars') or []:
        terms.extend(_split_text(pillar.get('gan_shishen')))
        terms.extend(_split_text(pillar.get('zhi_shishen')))
    current_dayun = (context.get('luck') or {}).get('current_dayun') or {}
    terms.extend(_split_text(current_dayun.get('gan_shishen')))
    terms.extend(_split_text(current_dayun.get('zhi_shishen')))
    return terms


def build_knowledge_packet(context, limit=14):
    """Select a small, prompt-safe knowledge packet for one chart context."""
    context = context or {}
    items = []
    seen = set()
    core = context.get('core') or {}

    strength = str(core.get('shenwang') or '').strip()
    if strength in STRENGTH_HINTS:
        _add(items, seen, f'strength:{strength}', strength, STRENGTH_HINTS[strength])

    geju = str(core.get('geju') or '').strip()
    if geju in GEJU_HINTS:
        _add(items, seen, f'geju:{geju}', geju, GEJU_HINTS[geju])

    for ten_god in _ten_gods_from_context(context):
        if ten_god in TEN_GOD_HINTS:
            _add(items, seen, f'ten_god:{ten_god}', ten_god, TEN_GOD_HINTS[ten_god])

    wuxing_percent = (context.get('wuxing') or {}).get('percent') or {}
    for element, raw_percent in sorted(wuxing_percent.items(), key=lambda kv: kv[1], reverse=True):
        if element not in WUXING_HINTS:
            continue
        try:
            percent = float(raw_percent)
        except (TypeError, ValueError):
            continue
        hint = WUXING_HINTS[element]
        if percent >= 35:
            item = {
                'topic': hint['topic'],
                'meaning': hint['too_high'],
                'advice': '只作五行倾向和生活节奏提醒，不作疾病判断。',
                'book': hint['book'],
            }
            _add(items, seen, f'wuxing:{element}:high', f'{element}{percent:g}%', item)
        elif percent <= 8:
            item = {
                'topic': hint['topic'],
                'meaning': hint['too_low'],
                'advice': '只作五行倾向和生活节奏提醒，不作疾病判断。',
                'book': hint['book'],
            }
            _add(items, seen, f'wuxing:{element}:low', f'{element}{percent:g}%', item)

    relation_text = str((context.get('relations') or {}).get('zhi_relations') or '')
    for key, hint in RELATION_HINTS.items():
        if key in relation_text:
            _add(items, seen, f'relation:{key}', relation_text, hint)

    return {
        'version': KNOWLEDGE_VERSION,
        'allowed_books': ALLOWED_CLASSIC_BOOKS,
        'items': items[:limit],
    }


def knowledge_to_prompt_text(packet):
    """Render selected knowledge as compact prompt text."""
    items = packet.get('items') or []
    allowed = '、'.join(packet.get('allowed_books') or ALLOWED_CLASSIC_BOOKS)
    lines = [
        '可引用知识片段：',
        f"知识库版本：{packet.get('version', KNOWLEDGE_VERSION)}",
        f'允许引用书名：{allowed}',
        '使用规则：这些内容是义理参考，不是逐字古籍原文；只能转述义理，不要编造卷页、原句或作者信息。',
    ]
    for idx, item in enumerate(items, 1):
        advice = f"；现实建议：{item['advice']}" if item.get('advice') else ''
        lines.append(
            f"{idx}. 触发={item['trigger']}；主题={item['topic']}；"
            f"义理={item['meaning']}{advice}；可署书名={item['book']}"
        )
    if not items:
        lines.append('本命盘未命中特定知识片段，只可依据命盘资料做克制分析。')
    return '\n'.join(lines)
