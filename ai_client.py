"""
DeepSeek/OpenAI 兼容 Chat Completions 客户端。

这里集中做参数校验和有限网络重试，避免调用层到处手写 requests.post。
"""
import json
import time

import requests


RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


class ModelClientError(RuntimeError):
    pass


def validate_chat_payload(payload):
    issues = []
    if not isinstance(payload, dict):
        return ['payload 必须是 dict']

    model = payload.get('model')
    if not isinstance(model, str) or not model.strip():
        issues.append('model 不能为空')

    messages = payload.get('messages')
    if not isinstance(messages, list) or not messages:
        issues.append('messages 必须是非空数组')
    else:
        for idx, msg in enumerate(messages):
            if not isinstance(msg, dict):
                issues.append(f'messages[{idx}] 必须是对象')
                continue
            if msg.get('role') not in {'system', 'user', 'assistant', 'tool'}:
                issues.append(f'messages[{idx}].role 不合法')
            content = msg.get('content')
            if not isinstance(content, str) or not content.strip():
                issues.append(f'messages[{idx}].content 不能为空')

    max_tokens = payload.get('max_tokens')
    if not isinstance(max_tokens, int) or max_tokens <= 0:
        issues.append('max_tokens 必须是正整数')

    if 'temperature' in payload:
        temperature = payload.get('temperature')
        if not isinstance(temperature, (int, float)) or temperature < 0 or temperature > 2:
            issues.append('temperature 必须在 0-2 之间')

    stream = payload.get('stream')
    if not isinstance(stream, bool):
        issues.append('stream 必须是布尔值')

    response_format = payload.get('response_format')
    if response_format is not None:
        if not isinstance(response_format, dict) or response_format.get('type') != 'json_object':
            issues.append('response_format 目前只支持 {"type": "json_object"}')

    thinking = payload.get('thinking')
    if thinking is not None:
        if not isinstance(thinking, dict) or thinking.get('type') not in {'enabled', 'disabled'}:
            issues.append('thinking.type 必须是 enabled 或 disabled')

    return issues


def ensure_valid_payload(payload):
    issues = validate_chat_payload(payload)
    if issues:
        raise ModelClientError('模型请求参数不合法：' + '；'.join(issues))


def _should_retry_response(resp):
    return resp.status_code in RETRYABLE_STATUS


def post_chat_completion(url, payload, headers, timeout=45, max_retries=1):
    ensure_valid_payload(payload)
    last_error = None
    attempts = max(1, int(max_retries) + 1)

    for attempt in range(attempts):
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
            if _should_retry_response(resp) and attempt < attempts - 1:
                time.sleep(0.6 * (attempt + 1))
                continue
            resp.raise_for_status()
            return resp.json()
        except (requests.exceptions.RequestException, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt < attempts - 1:
                time.sleep(0.6 * (attempt + 1))
                continue
            raise

    raise ModelClientError(f'模型请求失败：{last_error}')


def open_stream_response(url, payload, headers, timeout=60, max_retries=1):
    ensure_valid_payload(payload)
    last_error = None
    attempts = max(1, int(max_retries) + 1)

    for attempt in range(attempts):
        try:
            resp = requests.post(
                url,
                json=payload,
                headers=headers,
                stream=True,
                timeout=timeout,
            )
            if _should_retry_response(resp) and attempt < attempts - 1:
                resp.close()
                time.sleep(0.6 * (attempt + 1))
                continue
            resp.raise_for_status()
            return resp
        except requests.exceptions.RequestException as exc:
            last_error = exc
            if attempt < attempts - 1:
                time.sleep(0.6 * (attempt + 1))
                continue
            raise

    raise ModelClientError(f'模型流式请求失败：{last_error}')


def iter_chat_sse_chunks(resp):
    for line in resp.iter_lines():
        if not line:
            continue

        line_str = line.decode('utf-8')
        if not line_str.startswith('data: '):
            continue

        data_str = line_str[6:]
        if data_str.strip() == '[DONE]':
            break

        try:
            yield json.loads(data_str)
        except json.JSONDecodeError:
            continue
