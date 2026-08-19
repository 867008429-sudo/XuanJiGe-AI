"""
玄机阁 - AI服务层
DeepSeek API调用：流式输出(SSE) + token计数 + 缓存 + 成本控制

成本优化策略:
1. 流式输出(SSE) - 用户即时看到结果，体验好
2. 结果缓存 - 相同生辰不重复调用API
3. max_tokens限制 - 控制输出长度
4. 精简system prompt - 减少input token
5. 使用deepseek-chat(便宜)而非deepseek-reasoner(贵)
6. 分段解读 - 用户可选看哪个方面，避免一次长输出

DeepSeek定价参考:
- deepseek-chat: input ¥0.5/百万token, output ¥1/百万token (约$0.07/$0.14)
"""
import os
import json
import hashlib
import requests
from flask import Response

# DeepSeek API配置
DEEPSEEK_API_KEY = os.environ.get('DEEPSEEK_API_KEY', '')
DEEPSEEK_API_URL = os.environ.get('DEEPSEEK_API_URL', 'https://api.deepseek.com/v1/chat/completions')
DEEPSEEK_MODEL = 'deepseek-chat'  # 便宜模型，不是reasoner

# 成本控制参数
MAX_TOKENS = 4000  # 限制输出长度，控制成本
TEMPERATURE = 0.7

# Token价格（美元）
INPUT_PRICE_PER_M = 0.07   # $0.07/百万input token
OUTPUT_PRICE_PER_M = 0.14  # $0.14/百万output token


def calc_cost(prompt_tokens, completion_tokens):
    """计算API调用成本（美元）"""
    input_cost = (prompt_tokens / 1_000_000) * INPUT_PRICE_PER_M
    output_cost = (completion_tokens / 1_000_000) * OUTPUT_PRICE_PER_M
    return round(input_cost + output_cost, 6)


def build_cache_key(paipan_data):
    """
    根据排盘数据生成缓存key
    相同的年月日时性别 → 相同的解读
    """
    key_raw = f"{paipan_data.get('solar_date', '')}_{paipan_data.get('gender', '')}"
    return hashlib.md5(key_raw.encode()).hexdigest()


def build_prompt(paipan_data):
    """
    构造发给DeepSeek的prompt
    设计思路:
    - system: 角色设定+命理分析框架+古籍引用要求
    - user: 结构化排盘数据 + 分析指引
    """
    fp = paipan_data.get('four_pillars', {})

    system = (
        '你是玄机阁的老道长，自幼出家研习命理五十载，精通《滴天髓》《子平真诠》《穷通宝鉴》《三命通会》《渊海子平》《神峰通考》《命理约言》《李虚中命书》等命理经典，阅盘无数。'
        '你解读命盘绘声绘色、有血有肉：善用意象比喻（如"命如春木逢甘霖，自有一番生机""财星深藏库中，似灯下藏金，须点灯方见"），像说书人一样娓娓道来，'
        '让善信读来如临其境、如见其人；术语随讲随用大白话点破，不堆砌名词，不说车轱辘话，不端着架子。'
        '\n\n分析原则：'
        '1. 身旺者喜克泄耗（官杀/食伤/财星），忌生扶（印星/比劫）'
        '2. 身弱者喜生扶（印星/比劫），忌克泄耗'
        '3. 偏财格主意外之财、经商之财，正财格主固定工资之财'
        '4. 七杀为用主魄力竞争，七杀为忌主灾祸压力'
        '5. 日支为配偶宫，看日支十神和冲合判断婚姻'
        '6. 五行偏旺者该五行对应脏腑需注意健康'
        '7. 大运看天干地支十神，与日主生克关系定吉凶'
        '\n\n文风要求：'
        '1. 每个板块开头先用一两句有画面感的总起，先立意象再拆解命理，如性格板块可写"善信日主甲木生于春月，恰似参天大树立于东方沃土，天生一股向上的劲头"'
        '2. 讲吉凶要具体生动：说财运就描绘求财的门路与时机，说婚姻就描绘相处模式与缘分样貌，说健康就点明起居宜忌，避免"财运不错""婚姻和谐"这类空话'
        '3. 该提醒的地方要直言不讳，该宽慰的地方也要给善信留有盼头，言之有物，吉凶有据'
        '4. 总评结尾以道长口吻赠善信一句箴言收束全篇'
        '\n\n格式要求：'
        '1. 不要使用任何markdown格式（不要用**加粗**、不要用#标题、不要用-列表）'
        '2. 每个板块用【】标记开头，如【性格】【财运】【婚姻】【健康】【大运】【总评】'
        '3. 每方面解读末尾用"——"引出所依据的古籍原文，每方面至少引用2-3部不同古籍的原句，如：——滴天髓云：旺者冲衰衰者拔；——子平真诠云：财气通门户者，未有不富者也'
        '4. 板块内直接写内容，换行分段即可'
        '5. 每方面5-8句话深入分析加2-3句古籍引用，总字数控制在2000-3500字'
        '6. 不要只引用一句古籍就结束一个板块，要综合多部古籍交叉印证'
    )

    user = f"""排盘数据：
日主：{paipan_data.get('day_master', '')}({paipan_data.get('day_master_wuxing', '')})
格局：{paipan_data.get('geju', '')}
{paipan_data.get('shenwang', '')}
四柱：{fp.get('year',{}).get('gan','')}{fp.get('year',{}).get('zhi','')} | {fp.get('month',{}).get('gan','')}{fp.get('month',{}).get('zhi','')} | {fp.get('day',{}).get('gan','')}{fp.get('day',{}).get('zhi','')} | {fp.get('hour',{}).get('gan','')}{fp.get('hour',{}).get('zhi','')}
十神：{fp.get('year',{}).get('gan_shishen','')} | {fp.get('month',{}).get('gan_shishen','')} | 日主 | {fp.get('hour',{}).get('gan_shishen','')}
日支十神：{fp.get('day',{}).get('zhi_shishen','')}
五行：金{paipan_data.get('wuxing_count',{}).get('金',0)} 木{paipan_data.get('wuxing_count',{}).get('木',0)} 水{paipan_data.get('wuxing_count',{}).get('水',0)} 火{paipan_data.get('wuxing_count',{}).get('火',0)} 土{paipan_data.get('wuxing_count',{}).get('土',0)}
喜用：{','.join(paipan_data.get('xiyong',[]))}
忌神：{paipan_data.get('jishen','')}
神煞：{', '.join(paipan_data.get('shensha',[])) if paipan_data.get('shensha') else '无'}
地支：{', '.join(paipan_data.get('zhi_relations',[])) if paipan_data.get('zhi_relations') else '无特殊'}
大运："""
    import datetime as _dt
    current_year = _dt.datetime.now().year
    solar_date_str = paipan_data.get('solar_date', '')
    import re as _re
    year_match = _re.search(r'(\d+)年', solar_date_str)
    birth_year = int(year_match.group(1)) if year_match else current_year
    current_age = current_year - birth_year

    for dy in paipan_data.get('dayun', []):
        is_current = current_age >= dy['start_age'] and current_age <= dy['end_age']
        marker = ' ← 当前大运' if is_current else ''
        year_info = f"({dy.get('start_year','')}-{dy.get('end_year','')}年)" if dy.get('start_year') else ''
        user += f"\n  {dy['start_age']}-{dy['end_age']}岁 {year_info} {dy['gan']}{dy['zhi']}({dy['gan_shishen']}){marker}"

    user += f"""

当前年龄：{current_age}岁（{birth_year}年生，{current_year}年）
请特别注意：当前大运已在上表中用"← 当前大运"标记，分析大运时必须基于正确的当前大运，不要搞错年龄和对应的大运。

请按六方面深入解读，每方面5-8句话加2-3句不同古籍的引用：
1.【性格】根据日主天干特性（甲木参天、乙木柔蔓等）、十神组合（食神秀气、七杀威权等）、综合《滴天髓》论天干、《子平真诠》论十神、《三命通会》论性情分析性格
2.【财运】根据格局高低、财星旺衰、喜用是否到位，综合《滴天髓》论财、《子平真诠》论用神、《穷通宝鉴》论调候分析财运
3.【婚姻】根据日支十神、地支冲合、财官状态，综合《渊海子平》论六亲、《三命通会》论女命分析婚姻
4.【健康】根据五行偏旺偏弱、脏腑对应，综合《黄帝内经》五行对应、《三命通会》论疾厄分析健康
5.【大运】分析当前大运（已标记）的干支十神、与日主喜忌关系、吉凶判断，以及未来2-3步大运走势，引用《滴天髓》论运、《子平真诠》论行运
6.【总评】综合以上各方面，整体评价命格层次高低、人生格局、注意事项

引用古籍范围：《滴天髓》《子平真诠》《穷通宝鉴》《三命通会》《渊海子平》《神峰通考》《命理约言》《李虚中命书》《黄金策》
每方面必须引用至少2部不同古籍的原句，交叉印证，不可只引一部"""
    return system, user


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
    payload = {
        'model': DEEPSEEK_MODEL,
        'messages': [
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': user_prompt}
        ],
        'temperature': TEMPERATURE,
        'max_tokens': MAX_TOKENS,
        'stream': True,  # 流式输出
        'stream_options': {
            'include_usage': True  # 返回token用量
        }
    }

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

                # 提取文本内容
                if 'choices' in chunk and chunk['choices']:
                    delta = chunk['choices'][0].get('delta', {})
                    if 'content' in delta:
                        text = delta['content']
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
        yield f'data: {json.dumps({"error": f"API调用失败: {str(e)}"})}\n\n'
