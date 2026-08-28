"""
玄机阁 - AI 服务编排层

这一层负责把五件事串起来：
1. context_builder：把本地排盘整理成稳定 schema
2. prompt_builder：生成前置规范约束和用户提示
3. model_client：校验参数并调用 DeepSeek
4. validator：动态校验结构化骨架和最终正文
5. retry：对结构化骨架和网络请求做有限重试
"""
import hashlib
import json
import logging
import os
import re
import time

import requests

from ai_client import ModelClientError, iter_chat_sse_chunks, open_stream_response, post_chat_completion
from ai_context import SECTION_NAMES, build_bazi_context
from bazi_knowledge import KNOWLEDGE_VERSION
from ai_prompts import build_prompt as _build_prompt
from ai_prompts import build_structured_prompt as _build_structured_prompt
from ai_validator import validate_interpretation as _validate_interpretation
from ai_validator import validate_structured_outline as _validate_structured_outline


# DeepSeek API 配置
DEEPSEEK_API_KEY = os.environ.get('DEEPSEEK_API_KEY', '')
DEEPSEEK_API_URL = os.environ.get('DEEPSEEK_API_URL', 'https://api.deepseek.com/chat/completions')
DEEPSEEK_MODEL = os.environ.get('DEEPSEEK_MODEL', 'deepseek-v4-flash')

# 成本与生成参数
MAX_TOKENS = int(os.environ.get('DEEPSEEK_MAX_TOKENS', '6000'))
TEMPERATURE = float(os.environ.get('DEEPSEEK_TEMPERATURE', '0.7'))
THINKING_ENABLED = os.environ.get('DEEPSEEK_THINKING', '0').lower() not in ('0', 'false', 'off', 'no')
REASONING_EFFORT = os.environ.get('DEEPSEEK_REASONING_EFFORT', 'high')
STRUCTURED_GENERATION_ENABLED = os.environ.get('DEEPSEEK_STRUCTURED_GENERATION', '1').lower() not in ('0', 'false', 'off', 'no')
STRUCTURE_MAX_TOKENS = int(os.environ.get('DEEPSEEK_STRUCTURE_MAX_TOKENS', '3200'))
STRUCTURE_TEMPERATURE = float(os.environ.get('DEEPSEEK_STRUCTURE_TEMPERATURE', '0.25'))
MAX_MODEL_RETRIES = int(os.environ.get('DEEPSEEK_MAX_RETRIES', '1'))
STRUCTURE_REPAIR_RETRIES = int(os.environ.get('DEEPSEEK_STRUCTURE_REPAIR_RETRIES', '2'))
PROMPT_VERSION = 'interpretation-v7-knowledge-grounded'

# Token 价格（美元）。不同模型/时段价格会变，生产可用环境变量覆盖。
INPUT_PRICE_PER_M = float(os.environ.get('DEEPSEEK_INPUT_PRICE_PER_M_USD', '0.56'))
OUTPUT_PRICE_PER_M = float(os.environ.get('DEEPSEEK_OUTPUT_PRICE_PER_M_USD', '3.78'))

logger = logging.getLogger('xuanjige.ai')


def calc_cost(prompt_tokens, completion_tokens):
    """计算 API 调用成本（美元）。"""
    input_cost = (prompt_tokens / 1_000_000) * INPUT_PRICE_PER_M
    output_cost = (completion_tokens / 1_000_000) * OUTPUT_PRICE_PER_M
    return round(input_cost + output_cost, 6)


def _cache_identity(paipan_data):
    return f"{paipan_data.get('solar_date', '')}_{paipan_data.get('gender', '')}"


def build_cache_key(paipan_data):
    """prompt、模型或生成策略升级后自动避开旧的低质量缓存。"""
    thinking_flag = 'thinking' if THINKING_ENABLED else 'direct'
    structure_flag = 'structured' if STRUCTURED_GENERATION_ENABLED else 'direct'
    key_raw = f"{PROMPT_VERSION}_{DEEPSEEK_MODEL}_{thinking_flag}_{structure_flag}_{_cache_identity(paipan_data)}"
    return hashlib.md5(key_raw.encode()).hexdigest()


def build_legacy_cache_key(paipan_data):
    """旧版缓存 key，用于识别已扣过配额的历史解读并免费刷新。"""
    return hashlib.md5(_cache_identity(paipan_data).encode()).hexdigest()


def build_prompt(paipan_data, analysis_outline=None):
    context = build_bazi_context(paipan_data)
    return _build_prompt(paipan_data, analysis_outline, context=context)


def build_payload(system_prompt, user_prompt):
    """构造 DeepSeek Chat Completions 请求体，兼容 V4 思考模式。"""
    payload = {
        'model': DEEPSEEK_MODEL,
        'messages': [
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': user_prompt},
        ],
        'max_tokens': MAX_TOKENS,
        'stream': True,
        'stream_options': {
            'include_usage': True,
        },
    }
    if THINKING_ENABLED:
        payload['thinking'] = {'type': 'enabled'}
        payload['reasoning_effort'] = REASONING_EFFORT
    else:
        payload['thinking'] = {'type': 'disabled'}
        payload['temperature'] = TEMPERATURE
    return payload


def build_structured_prompt(paipan_data, previous_issue=''):
    context = build_bazi_context(paipan_data)
    return _build_structured_prompt(paipan_data, context=context, previous_issue=previous_issue)


def build_structured_payload(system_prompt, user_prompt):
    return {
        'model': DEEPSEEK_MODEL,
        'messages': [
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': user_prompt},
        ],
        'max_tokens': STRUCTURE_MAX_TOKENS,
        'stream': False,
        'response_format': {'type': 'json_object'},
        'thinking': {'type': 'disabled'},
        'temperature': STRUCTURE_TEMPERATURE,
    }


def _headers():
    return {
        'Authorization': f'Bearer {DEEPSEEK_API_KEY}',
        'Content-Type': 'application/json',
    }


def _extract_json_object(content):
    content = (content or '').strip()
    if content.startswith('```'):
        content = re.sub(r'^```(?:json)?\s*', '', content)
        content = re.sub(r'\s*```$', '', content)
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        start = content.find('{')
        end = content.rfind('}')
        if start >= 0 and end > start:
            return json.loads(content[start:end + 1])
        raise


def validate_structured_outline(outline, paipan_data=None):
    context = build_bazi_context(paipan_data or {}) if paipan_data is not None else None
    return _validate_structured_outline(outline, context=context)


def validate_interpretation(text, paipan_data=None, finish_reason=None):
    context = build_bazi_context(paipan_data or {}) if paipan_data is not None else None
    return _validate_interpretation(text, context=context, finish_reason=finish_reason)


def request_structured_outline(paipan_data, headers=None):
    """
    结构化骨架请求。

    这里做有限修复重试：第一次如果 JSON 解析失败或校验失败，下一次把校验问题
    明确喂回模型，只让它修结构，不扩大任务。
    """
    headers = headers or _headers()
    context = build_bazi_context(paipan_data)
    attempts = max(1, STRUCTURE_REPAIR_RETRIES + 1)
    last_issue = ''
    usage_total = {
        'prompt_tokens': 0,
        'completion_tokens': 0,
        'total_tokens': 0,
        'finish_reason': None,
        'structured_attempts': 0,
        'structured_repairs': 0,
    }

    for attempt in range(attempts):
        system_prompt, user_prompt = _build_structured_prompt(
            paipan_data,
            context=context,
            previous_issue=last_issue,
        )
        payload = build_structured_payload(system_prompt, user_prompt)
        data = post_chat_completion(
            DEEPSEEK_API_URL,
            payload,
            headers,
            timeout=45,
            max_retries=MAX_MODEL_RETRIES,
        )
        usage_total['structured_attempts'] += 1
        usage_total['structured_repairs'] = max(0, usage_total['structured_attempts'] - 1)

        choice = (data.get('choices') or [{}])[0]
        message = choice.get('message') or {}
        usage = data.get('usage') or {}
        usage_total['prompt_tokens'] += usage.get('prompt_tokens', 0)
        usage_total['completion_tokens'] += usage.get('completion_tokens', 0)
        usage_total['total_tokens'] += usage.get('total_tokens', 0)
        usage_total['finish_reason'] = choice.get('finish_reason')

        try:
            outline = _extract_json_object(message.get('content', ''))
        except json.JSONDecodeError as exc:
            last_issue = f'JSON 解析失败：{exc}'
            if attempt < attempts - 1:
                continue
            raise ValueError(last_issue)

        issues = _validate_structured_outline(outline, context=context)
        if not issues:
            return outline, usage_total

        last_issue = '；'.join(issues)
        if attempt >= attempts - 1:
            raise ValueError(last_issue)

    raise ValueError(last_issue or '结构化骨架生成失败')


def _status_event(text, progress):
    return f'data: {json.dumps({"status": text, "progress": progress}, ensure_ascii=False)}\n\n'


def _error_event(message, **extra):
    payload = {'error': message}
    payload.update(extra)
    return 'data: ' + json.dumps(payload, ensure_ascii=False) + '\n\n'


def stream_interpretation(paipan_data):
    """
    流式调用 DeepSeek API，返回 SSE 生成器，同时统计 token 用量。
    """
    started_at = time.perf_counter()
    logger.info(
        'ai_generation_start model=%s prompt_version=%s structured=%s thinking=%s',
        DEEPSEEK_MODEL,
        PROMPT_VERSION,
        STRUCTURED_GENERATION_ENABLED,
        THINKING_ENABLED,
    )
    if not DEEPSEEK_API_KEY:
        logger.warning('ai_generation_blocked reason=missing_api_key')
        yield _error_event('DeepSeek API Key未配置')
        return

    headers = _headers()
    context = build_bazi_context(paipan_data)
    analysis_outline = None
    structure_usage = {
        'prompt_tokens': 0,
        'completion_tokens': 0,
        'total_tokens': 0,
        'finish_reason': None,
        'structured_attempts': 0,
        'structured_repairs': 0,
    }
    structured_ok = False
    structured_error = ''

    if STRUCTURED_GENERATION_ENABLED:
        yield _status_event('正在先立命盘分析骨架...', 18)
        try:
            analysis_outline, structure_usage = request_structured_outline(paipan_data, headers)
            structured_ok = True
            logger.info(
                'ai_outline_ok attempts=%s repairs=%s tokens=%s',
                structure_usage.get('structured_attempts', 0),
                structure_usage.get('structured_repairs', 0),
                structure_usage.get('total_tokens', 0),
            )
            progress = 34 if structure_usage.get('structured_repairs', 0) == 0 else 38
            yield _status_event('分析骨架已通过校验，正在润色成文...', progress)
        except (ModelClientError, requests.exceptions.RequestException, ValueError, json.JSONDecodeError) as exc:
            structured_error = str(exc)
            logger.warning(
                'ai_outline_failed error_type=%s fallback=direct',
                exc.__class__.__name__,
            )
            yield _status_event('分析骨架未成，已自动改用直写保护体验...', 24)

    system_prompt, user_prompt = _build_prompt(paipan_data, analysis_outline, context=context)
    payload = build_payload(system_prompt, user_prompt)

    try:
        resp = open_stream_response(
            DEEPSEEK_API_URL,
            payload,
            headers,
            timeout=60,
            max_retries=MAX_MODEL_RETRIES,
        )

        full_text = ''
        prompt_tokens = 0
        completion_tokens = 0
        finish_reason = None

        try:
            for chunk in iter_chat_sse_chunks(resp):
                if chunk.get('choices'):
                    choice = chunk['choices'][0]
                    finish_reason = choice.get('finish_reason') or finish_reason
                    delta = choice.get('delta') or {}
                    text = delta.get('content')
                    if isinstance(text, str) and text:
                        full_text += text
                        yield f'data: {json.dumps({"text": text}, ensure_ascii=False)}\n\n'

                if chunk.get('usage'):
                    prompt_tokens = chunk['usage'].get('prompt_tokens', 0)
                    completion_tokens = chunk['usage'].get('completion_tokens', 0)
        finally:
            resp.close()

        final_prompt_tokens = prompt_tokens
        final_completion_tokens = completion_tokens
        prompt_tokens += structure_usage.get('prompt_tokens', 0)
        completion_tokens += structure_usage.get('completion_tokens', 0)
        total_tokens = prompt_tokens + completion_tokens
        cost = calc_cost(prompt_tokens, completion_tokens)
        quality = _validate_interpretation(full_text, context=context, finish_reason=finish_reason)

        if not quality['ok']:
            logger.warning(
                'ai_generation_quality_failed finish_reason=%s issue_count=%s duration_ms=%s',
                finish_reason,
                len(quality['issues']),
                int((time.perf_counter() - started_at) * 1000),
            )
            yield _error_event(
                '这次解读生成不完整，已为你保留次数，请重新开示。',
                retryable=True,
                validation_issues=quality['issues'],
            )
            return

        meta = {
            'done': True,
            'prompt_tokens': prompt_tokens,
            'completion_tokens': completion_tokens,
            'total_tokens': total_tokens,
            'cost_usd': cost,
            'finish_reason': finish_reason,
            'quality': quality,
            'prompt_version': PROMPT_VERSION,
            'model': DEEPSEEK_MODEL,
            'temperature': TEMPERATURE,
            'structured_generation': structured_ok,
            'structured_error': structured_error,
            'structured_attempts': structure_usage.get('structured_attempts', 0),
            'structured_repairs': structure_usage.get('structured_repairs', 0),
            'structure_prompt_tokens': structure_usage.get('prompt_tokens', 0),
            'structure_completion_tokens': structure_usage.get('completion_tokens', 0),
            'structure_total_tokens': structure_usage.get('total_tokens', 0),
            'structure_finish_reason': structure_usage.get('finish_reason'),
            'final_prompt_tokens': final_prompt_tokens,
            'final_completion_tokens': final_completion_tokens,
            'context_schema_version': context.get('schema_version'),
            'knowledge_version': KNOWLEDGE_VERSION,
            'current_dayun': context.get('luck', {}).get('current_dayun', {}).get('label', ''),
            'full_text': full_text,
        }
        logger.info(
            'ai_generation_done model=%s prompt_version=%s tokens=%s cost_usd=%.6f structured=%s repairs=%s finish_reason=%s duration_ms=%s',
            DEEPSEEK_MODEL,
            PROMPT_VERSION,
            total_tokens,
            cost,
            structured_ok,
            structure_usage.get('structured_repairs', 0),
            finish_reason,
            int((time.perf_counter() - started_at) * 1000),
        )
        yield f'data: {json.dumps(meta, ensure_ascii=False)}\n\n'

    except (ModelClientError, requests.exceptions.RequestException) as exc:
        logger.warning(
            'ai_generation_failed error_type=%s duration_ms=%s',
            exc.__class__.__name__,
            int((time.perf_counter() - started_at) * 1000),
        )
        yield _error_event(f'API调用失败: {str(exc)}')
