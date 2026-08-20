"""
玄机阁 - AI服务层
DeepSeek API调用：流式输出(SSE) + token计数 + 缓存 + 成本控制

成本优化策略:
1. 流式输出(SSE) - 用户即时看到结果，体验好
2. 结果缓存 - 相同生辰不重复调用API
3. max_tokens限制 - 控制输出长度
4. 结构化命盘喂料 - 让模型依据盘面证据解读
5. 使用 deepseek-v4-flash - 适合中文长文本输出
6. 分段解读 - 用户可选看哪个方面，避免一次长输出
"""
import os
import json
import hashlib
import requests
from flask import Response

# DeepSeek API配置
DEEPSEEK_API_KEY = os.environ.get('DEEPSEEK_API_KEY', '')
DEEPSEEK_API_URL = os.environ.get('DEEPSEEK_API_URL', 'https://api.deepseek.com/chat/completions')
DEEPSEEK_MODEL = os.environ.get('DEEPSEEK_MODEL', 'deepseek-v4-flash')  # 适合中文长文本输出，可用环境变量切换

# 成本控制参数
MAX_TOKENS = int(os.environ.get('DEEPSEEK_MAX_TOKENS', '6000'))  # 限制输出长度，控制成本
TEMPERATURE = float(os.environ.get('DEEPSEEK_TEMPERATURE', '0.7'))
THINKING_ENABLED = os.environ.get('DEEPSEEK_THINKING', '0').lower() not in ('0', 'false', 'off', 'no')
REASONING_EFFORT = os.environ.get('DEEPSEEK_REASONING_EFFORT', 'high')
PROMPT_VERSION = 'interpretation-v4-flash'

# Token价格（美元）。不同模型/时段价格会变，生产可用环境变量覆盖。
INPUT_PRICE_PER_M = float(os.environ.get('DEEPSEEK_INPUT_PRICE_PER_M_USD', '0.56'))
OUTPUT_PRICE_PER_M = float(os.environ.get('DEEPSEEK_OUTPUT_PRICE_PER_M_USD', '3.78'))


def calc_cost(prompt_tokens, completion_tokens):
    """计算API调用成本（美元）"""
    input_cost = (prompt_tokens / 1_000_000) * INPUT_PRICE_PER_M
    output_cost = (completion_tokens / 1_000_000) * OUTPUT_PRICE_PER_M
    return round(input_cost + output_cost, 6)


def _cache_identity(paipan_data):
    """
    根据排盘数据生成命盘身份。
    相同的年月日时性别 → 相同的命盘身份
    """
    return f"{paipan_data.get('solar_date', '')}_{paipan_data.get('gender', '')}"


def build_cache_key(paipan_data):
    """
    根据排盘数据、prompt版本和生成策略生成缓存key。
    prompt或模型策略升级后自动避开旧的低质量解读缓存。
    """
    thinking_flag = 'thinking' if THINKING_ENABLED else 'direct'
    key_raw = f"{PROMPT_VERSION}_{DEEPSEEK_MODEL}_{thinking_flag}_{_cache_identity(paipan_data)}"
    return hashlib.md5(key_raw.encode()).hexdigest()


def build_legacy_cache_key(paipan_data):
    """旧版缓存key，用于识别已扣过配额的历史解读并免费刷新。"""
    key_raw = _cache_identity(paipan_data)
    return hashlib.md5(key_raw.encode()).hexdigest()


def _text(value, default='无'):
    if value is None:
        return default
    if isinstance(value, (list, tuple)):
        return '、'.join(str(v) for v in value) if value else default
    value = str(value).strip()
    return value if value else default


def _pillar_line(label, pillar):
    gan = pillar.get('gan', '')
    zhi = pillar.get('zhi', '')
    return (
        f"{label}：{gan}{zhi}，天干十神={_text(pillar.get('gan_shishen'))}，"
        f"地支主气十神={_text(pillar.get('zhi_shishen'))}，"
        f"藏干={_text(pillar.get('zhi_canggan'))}，纳音={_text(pillar.get('nayin'))}"
    )


def _gender_label(gender):
    return {'male': '男命', 'female': '女命'}.get(gender, '未注明')


def _wuxing_line(paipan_data):
    count = paipan_data.get('wuxing_count', {})
    percent = paipan_data.get('wuxing_percent', {})
    return '；'.join(
        f"{wx}{count.get(wx, 0)}个/{percent.get(wx, 0)}%"
        for wx in ['金', '木', '水', '火', '土']
    )


def build_prompt(paipan_data):
    """
    构造发给DeepSeek的prompt
    设计思路:
    - system: 角色设定 + 读盘方法 + 输出质量约束
    - user: 结构化排盘数据 + 当前大运 + 每个板块的分析抓手
    """
    fp = paipan_data.get('four_pillars', {})

    system = (
        '你是玄机阁的老道长，任务不是泛泛安慰，而是根据给定命盘做具体、克制、有证据的八字解读。'
        '排盘结果已经由本地历法引擎算好，你不得重新排盘、不得改四柱、不得质疑日期，只能依据给定数据分析。'
        '你的口吻要有古典命理味，但要说人话：先抓盘面矛盾，再落到性格、求财、感情、健康、行运上的具体现象。'
        '\n\n读盘方法：'
        '1. 每个结论必须能回扣至少两个盘面证据，例如月令、日主强弱、十神、藏干、五行偏枯、地支冲合、当前大运。'
        '2. 身旺喜克泄耗，身弱喜生扶；不要只背规则，要说清楚为什么这个盘如此取用。'
        '3. 财运要区分财星是否透出、是否有根、是否为喜忌、适合稳定收入还是项目经营，不许只写"财运不错"。'
        '4. 婚姻男命重点看财星与日支，女命重点看官杀与日支，同时看冲合刑害；要讲相处模式，不作绝对断语。'
        '5. 健康只做养生提醒，不作疾病诊断；五行偏旺偏弱要落到作息、饮食、压力管理等可执行建议。'
        '6. 大运必须先分析标记为当前大运的那一步，再顺带看未来2-3步，不得把未来大运说成当下。'
        '\n\n质量要求：'
        '1. 每个板块采用"盘面抓手 → 现实映射 → 建议提醒"的结构，但不要写成列表。'
        '2. 多用具体场景和动作，例如"适合在规则清楚的平台里凭专业吃饭"，少用空词，例如"比较顺利""贵人相助"。'
        '3. 可以直言短板，但要留改运空间；避免恐吓、宿命化、医疗/投资/婚姻绝对建议。'
        '4. 古籍引用要短，作为义理点睛；不要编造书名之外的版本、卷页、作者生平。'
        '\n\n格式要求：'
        '1. 只输出正文，不要寒暄，不要markdown，不要编号列表。'
        '2. 必须严格按这六个板块输出且顺序不变：【性格】【财运】【婚姻】【健康】【大运】【总评】。'
        '3. 每个板块3-5段，每段2-4句；每个板块末尾用2句"——书名云：短句/义理"格式引用不同古籍。'
        '4. 总字数控制在2600-3800字，宁可少而准，不要为了字数重复。'
    )

    import datetime as _dt
    current_year = _dt.datetime.now().year
    solar_date_str = paipan_data.get('solar_date', '')
    import re as _re
    year_match = _re.search(r'(\d+)年', solar_date_str)
    birth_year = int(year_match.group(1)) if year_match else current_year
    current_age = current_year - birth_year

    dayun_lines = []
    for dy in paipan_data.get('dayun', []):
        is_current = current_age >= dy['start_age'] and current_age <= dy['end_age']
        marker = ' ← 当前大运' if is_current else ''
        year_info = f"({dy.get('start_year','')}-{dy.get('end_year','')}年)" if dy.get('start_year') else ''
        dayun_lines.append(
            f"{dy['start_age']}-{dy['end_age']}岁 {year_info} "
            f"{dy['gan']}{dy['zhi']}，天干={dy.get('gan_shishen','')}，"
            f"地支={dy.get('zhi_shishen','')}，纳音={dy.get('nayin','')}{marker}"
        )

    user = f"""命盘资料：
性别：{_gender_label(paipan_data.get('gender'))}
公历：{paipan_data.get('solar_date', '')}
农历：{paipan_data.get('lunar_date', '')}
日主：{paipan_data.get('day_master', '')}（{paipan_data.get('day_master_wuxing', '')}）
格局：{paipan_data.get('geju', '')}
旺弱：{paipan_data.get('shenwang', '')}
起运：{paipan_data.get('qiyun_age', '')}岁，{'顺行' if paipan_data.get('forward') else '逆行'}

四柱细节：
{_pillar_line('年柱', fp.get('year', {}))}
{_pillar_line('月柱', fp.get('month', {}))}
{_pillar_line('日柱', fp.get('day', {}))}
{_pillar_line('时柱', fp.get('hour', {}))}

五行分布：{_wuxing_line(paipan_data)}
喜用：{_text(paipan_data.get('xiyong'))}
忌神：{_text(paipan_data.get('jishen'))}
神煞：{_text(paipan_data.get('shensha'))}
地支关系：{_text(paipan_data.get('zhi_relations'), '无特殊冲合刑害')}

大运列表：
{chr(10).join(dayun_lines)}

当前年龄：{current_age}岁（{birth_year}年生，{current_year}年）
当前大运已用"← 当前大运"标记，大运板块必须围绕这一运展开。

请输出六个板块：
【性格】抓日主、月令、比劫/食伤/官杀/印星组合，讲处事风格、优点、盲区。
【财运】抓财星、食伤生财、官杀制身、喜忌和大运，讲赚钱路径、风险点、适合的工作/项目形态。
【婚姻】按性别取六亲，结合日支、财官、冲合，讲亲密关系中的吸引点、摩擦点、相处建议。
【健康】结合五行偏枯和火土金木水强弱，只给养生级建议，不做疾病诊断。
【大运】先讲当前大运，再讲未来2-3步趋势；每一步都要说明干支十神与喜忌的关系。
【总评】提炼命格主线、成事方式、最该修的短板和一句道长赠言。"""
    return system, user


def build_payload(system_prompt, user_prompt):
    """构造DeepSeek Chat Completions请求体，兼容V4思考模式。"""
    payload = {
        'model': DEEPSEEK_MODEL,
        'messages': [
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': user_prompt}
        ],
        'max_tokens': MAX_TOKENS,
        'stream': True,
        'stream_options': {
            'include_usage': True
        }
    }
    if THINKING_ENABLED:
        payload['thinking'] = {'type': 'enabled'}
        payload['reasoning_effort'] = REASONING_EFFORT
    else:
        payload['thinking'] = {'type': 'disabled'}
        payload['temperature'] = TEMPERATURE
    return payload


def stream_interpretation(paipan_data):
    """
    流式调用DeepSeek API，返回SSE生成器
    同时统计token用量
    """
    if not DEEPSEEK_API_KEY:
        yield 'data: {"error": "DeepSeek API Key未配置"}\n\n'
        return

    system_prompt, user_prompt = build_prompt(paipan_data)

    headers = {
        'Authorization': f'Bearer {DEEPSEEK_API_KEY}',
        'Content-Type': 'application/json'
    }
    payload = build_payload(system_prompt, user_prompt)

    try:
        resp = requests.post(
            DEEPSEEK_API_URL,
            json=payload,
            headers=headers,
            stream=True,  # 流式HTTP
            timeout=60
        )
        resp.raise_for_status()

        full_text = ''
        prompt_tokens = 0
        completion_tokens = 0

        for line in resp.iter_lines():
            if not line:
                continue

            line_str = line.decode('utf-8')
            if not line_str.startswith('data: '):
                continue

            data_str = line_str[6:]  # 去掉 'data: '
            if data_str.strip() == '[DONE]':
                break

            try:
                chunk = json.loads(data_str)

                # 提取最终正文。V4思考模式可能返回 reasoning_content 或 content=None，这些不推给前端。
                if 'choices' in chunk and chunk['choices']:
                    delta = chunk['choices'][0].get('delta', {})
                    text = delta.get('content')
                    if isinstance(text, str) and text:
                        full_text += text
                        # SSE格式发送给前端
                        yield f'data: {json.dumps({"text": text}, ensure_ascii=False)}\n\n'

                # 提取token用量（在最后一个chunk中）
                if 'usage' in chunk and chunk['usage']:
                    prompt_tokens = chunk['usage'].get('prompt_tokens', 0)
                    completion_tokens = chunk['usage'].get('completion_tokens', 0)

            except json.JSONDecodeError:
                continue

        # 发送token统计信息
        total_tokens = prompt_tokens + completion_tokens
        cost = calc_cost(prompt_tokens, completion_tokens)

        meta = {
            'done': True,
            'prompt_tokens': prompt_tokens,
            'completion_tokens': completion_tokens,
            'total_tokens': total_tokens,
            'cost_usd': cost,
            'full_text': full_text  # 用于缓存
        }
        yield f'data: {json.dumps(meta, ensure_ascii=False)}\n\n'

    except requests.exceptions.RequestException as e:
        yield f'data: {json.dumps({"error": f"API调用失败: {str(e)}"}, ensure_ascii=False)}\n\n'
