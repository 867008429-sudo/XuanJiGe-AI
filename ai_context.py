"""
命盘上下文构建层。

这里做的事情很朴素：把后端已经算好的排盘结果整理成稳定 schema。
模型只读这个 schema，不重新排盘，这样 AI 负责表达，程序负责事实。
"""
import datetime as _dt
import re


SECTION_NAMES = ['性格', '财运', '婚姻', '健康', '大运', '总评']
PILLAR_KEYS = [
    ('year', '年柱'),
    ('month', '月柱'),
    ('day', '日柱'),
    ('hour', '时柱'),
]
WUXING_ORDER = ['金', '木', '水', '火', '土']


def text_value(value, default='无'):
    if value is None:
        return default
    if isinstance(value, (list, tuple)):
        values = [str(v).strip() for v in value if str(v).strip()]
        return '、'.join(values) if values else default
    value = str(value).strip()
    return value if value else default


def gender_label(gender):
    return {'male': '男命', 'female': '女命'}.get(gender, '未注明')


def _birth_year(solar_date):
    match = re.search(r'(\d{4})年', solar_date or '')
    return int(match.group(1)) if match else None


def current_age(paipan_data, current_year=None):
    current_year = current_year or _dt.datetime.now().year
    birth_year = _birth_year(paipan_data.get('solar_date', ''))
    if not birth_year:
        return 0, current_year, current_year
    return max(0, current_year - birth_year), birth_year, current_year


def _pillar_context(role, label, pillar):
    pillar = pillar or {}
    gan = str(pillar.get('gan', '') or '')
    zhi = str(pillar.get('zhi', '') or '')
    return {
        'role': role,
        'label': label,
        'ganzhi': f'{gan}{zhi}',
        'gan': gan,
        'zhi': zhi,
        'gan_shishen': text_value(pillar.get('gan_shishen')),
        'zhi_shishen': text_value(pillar.get('zhi_shishen')),
        'canggan': text_value(pillar.get('zhi_canggan')),
        'nayin': text_value(pillar.get('nayin')),
    }


def wuxing_summary(paipan_data):
    count = paipan_data.get('wuxing_count') or {}
    percent = paipan_data.get('wuxing_percent') or {}
    return '；'.join(
        f"{wx}{count.get(wx, 0)}个/{percent.get(wx, 0)}%"
        for wx in WUXING_ORDER
    )


def _dayun_items(paipan_data, age):
    items = []
    for raw in paipan_data.get('dayun') or []:
        start_age = raw.get('start_age', 999)
        end_age = raw.get('end_age', -1)
        gan = str(raw.get('gan', '') or '')
        zhi = str(raw.get('zhi', '') or '')
        items.append({
            'label': f'{gan}{zhi}',
            'gan': gan,
            'zhi': zhi,
            'start_age': start_age,
            'end_age': end_age,
            'start_year': raw.get('start_year', ''),
            'end_year': raw.get('end_year', ''),
            'gan_shishen': text_value(raw.get('gan_shishen')),
            'zhi_shishen': text_value(raw.get('zhi_shishen')),
            'nayin': text_value(raw.get('nayin')),
            'is_current': age >= start_age and age <= end_age,
        })
    return items


def build_bazi_context(paipan_data, current_year=None):
    """返回稳定 schema，供 Prompt、校验器和缓存元信息复用。"""
    paipan_data = paipan_data or {}
    age, birth_year, now_year = current_age(paipan_data, current_year)
    fp = paipan_data.get('four_pillars') or {}
    pillars = [
        _pillar_context(role, label, fp.get(role) or {})
        for role, label in PILLAR_KEYS
    ]
    dayun = _dayun_items(paipan_data, age)
    current_dayun = next((item for item in dayun if item['is_current']), None)
    context = {
        'schema_version': 'bazi-context-v1',
        'birth': {
            'gender': paipan_data.get('gender', ''),
            'gender_label': gender_label(paipan_data.get('gender')),
            'solar_date': paipan_data.get('solar_date', ''),
            'lunar_date': paipan_data.get('lunar_date', ''),
            'birth_year': birth_year,
            'current_year': now_year,
            'current_age': age,
        },
        'pillars': pillars,
        'core': {
            'day_master': paipan_data.get('day_master', ''),
            'day_master_wuxing': paipan_data.get('day_master_wuxing', ''),
            'geju': paipan_data.get('geju', ''),
            'shenwang': paipan_data.get('shenwang', ''),
            'xiyong': text_value(paipan_data.get('xiyong')),
            'jishen': text_value(paipan_data.get('jishen')),
        },
        'wuxing': {
            'count': paipan_data.get('wuxing_count') or {},
            'percent': paipan_data.get('wuxing_percent') or {},
            'summary': wuxing_summary(paipan_data),
        },
        'relations': {
            'shensha': text_value(paipan_data.get('shensha')),
            'zhi_relations': text_value(paipan_data.get('zhi_relations'), '无特殊冲合刑害'),
        },
        'luck': {
            'qiyun_age': paipan_data.get('qiyun_age', ''),
            'direction': '顺行' if paipan_data.get('forward') else '逆行',
            'current_dayun': current_dayun or {},
            'dayun': dayun,
        },
    }
    context['evidence_terms'] = evidence_terms(context)
    return context


def current_dayun_label(paipan_data, current_year=None):
    context = build_bazi_context(paipan_data, current_year)
    return context['luck'].get('current_dayun', {}).get('label', '')


def evidence_terms(context):
    terms = set()

    core = context.get('core') or {}
    day_master = str(core.get('day_master', '') or '').strip()
    day_master_wuxing = str(core.get('day_master_wuxing', '') or '').strip()
    if day_master and day_master_wuxing:
        terms.add(f'{day_master}{day_master_wuxing}')

    for key in ('day_master', 'day_master_wuxing', 'geju', 'shenwang'):
        value = str(core.get(key, '') or '').strip()
        if value:
            terms.add(value)

    for value in (core.get('xiyong'), core.get('jishen')):
        for part in re.split(r'[、,，;；\s()（）]+', str(value or '')):
            if part:
                terms.add(part)

    for pillar in context.get('pillars') or []:
        for key in ('ganzhi', 'gan', 'zhi', 'gan_shishen', 'zhi_shishen', 'canggan'):
            value = str(pillar.get(key, '') or '').strip()
            if value and value != '无':
                terms.add(value)

    current_dayun = (context.get('luck') or {}).get('current_dayun') or {}
    for key in ('label', 'gan', 'zhi', 'gan_shishen', 'zhi_shishen'):
        value = str(current_dayun.get(key, '') or '').strip()
        if value and value != '无':
            terms.add(value)

    return sorted(terms, key=len, reverse=True)


def context_to_prompt_text(context):
    birth = context['birth']
    core = context['core']
    pillars = context['pillars']
    luck = context['luck']

    pillar_lines = []
    for pillar in pillars:
        pillar_lines.append(
            f"{pillar['label']}：{pillar['ganzhi']}，天干十神={pillar['gan_shishen']}，"
            f"地支主气十神={pillar['zhi_shishen']}，藏干={pillar['canggan']}，纳音={pillar['nayin']}"
        )

    dayun_lines = []
    for dy in luck.get('dayun') or []:
        year_info = f"({dy.get('start_year')}-{dy.get('end_year')}年)" if dy.get('start_year') else ''
        marker = ' ← 当前大运' if dy.get('is_current') else ''
        dayun_lines.append(
            f"{dy.get('start_age')}-{dy.get('end_age')}岁 {year_info} "
            f"{dy.get('label')}，天干={dy.get('gan_shishen')}，"
            f"地支={dy.get('zhi_shishen')}，纳音={dy.get('nayin')}{marker}"
        )

    return f"""命盘资料：
性别：{birth['gender_label']}
公历：{birth['solar_date']}
农历：{birth['lunar_date']}
日主：{core['day_master']}（{core['day_master_wuxing']}）
格局：{core['geju']}
旺弱：{core['shenwang']}
起运：{luck.get('qiyun_age')}岁，{luck.get('direction')}

四柱细节：
{chr(10).join(pillar_lines)}

五行分布：{context['wuxing']['summary']}
喜用：{core['xiyong']}
忌神：{core['jishen']}
神煞：{context['relations']['shensha']}
地支关系：{context['relations']['zhi_relations']}

大运列表：
{chr(10).join(dayun_lines)}

当前年龄：{birth['current_age']}岁（{birth['birth_year']}年生，{birth['current_year']}年）
当前大运：{luck.get('current_dayun', {}).get('label', '未识别')}
当前大运已用"← 当前大运"标记，大运板块必须围绕这一运展开。
"""
