#!/usr/bin/env python3
"""
玄机阁 - 八字排盘 + AI解读
架构: 引擎层(bazi_engine) → 服务层(db + ai_service) → API层(Flask) → 前端

功能:
  - 八字排盘（精确到节气，sxtwl天文历）
  - AI流式解读（DeepSeek SSE）
  - 用户配额管理（5次免费 + 充值）
  - 结果缓存（相同生辰不重复调用API）
  - Token用量统计与成本控制

部署:
  export DEEPSEEK_API_KEY="your-key"
  python3 app.py
  # 或 Docker: docker build -t xuanjige . && docker run -p 8888:8888 xuanjige
"""

from flask import Flask, request, jsonify, Response, make_response
from flask_cors import CORS
import os
import json
import time as _time

def _load_env_fallback(path='.env'):
    """Load simple KEY=VALUE pairs when python-dotenv is unavailable."""
    if not os.path.exists(path):
        return
    with open(path, 'r', encoding='utf-8') as env_file:
        for raw_line in env_file:
            line = raw_line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, value = line.split('=', 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    _load_env_fallback()

import db
import ai_service
from bazi_engine import paipan

app = Flask(__name__)
CORS(app)


def _csv_env(name):
    """Read comma-separated env values without leaking secrets into the page."""
    raw = os.environ.get(name, '')
    return [item.strip() for item in raw.split(',') if item.strip()]


REGISTRATION_INVITE_CODES = (
    _csv_env('REGISTRATION_INVITE_CODES')
    or _csv_env('REGISTRATION_INVITE_CODE')
)
REGISTRATION_RATE_WINDOW_MINUTES = int(os.environ.get('REGISTRATION_RATE_WINDOW_MINUTES', '60'))
REGISTRATION_RATE_MAX_ATTEMPTS = int(os.environ.get('REGISTRATION_RATE_MAX_ATTEMPTS', '8'))
REGISTRATION_DAILY_MAX_PER_IP = int(os.environ.get('REGISTRATION_DAILY_MAX_PER_IP', '2'))
REGISTRATION_DAILY_MAX_PER_CLIENT = int(os.environ.get('REGISTRATION_DAILY_MAX_PER_CLIENT', '1'))


# ==================== 用户指纹辅助 ====================

def get_auth_token(req):
    """从请求中获取登录token"""
    # 1. 从 header 拿
    token = req.headers.get('Authorization', '').replace('Bearer ', '').strip()
    if token:
        return token
    # 2. 从 cookie 拿
    token = req.cookies.get('auth_token', '')
    if token:
        return token
    return ''


def get_client_ip(req):
    """Get the best-effort client IP, supporting future reverse proxy deployment."""
    forwarded = req.headers.get('X-Forwarded-For', '')
    if forwarded:
        return forwarded.split(',')[0].strip()
    return req.remote_addr or 'unknown'


def _client_fingerprint(req):
    """根据 X-Client-Id 计算匿名设备指纹（与 get_fingerprint 中逻辑一致）"""
    import hashlib
    client_id = req.headers.get('X-Client-Id', '')
    if client_id:
        return hashlib.sha256(f"cid:{client_id}".encode()).hexdigest()[:32]
    return ''


def get_fingerprint(req):
    """
    用户指纹优先级：
    1. 登录token → 账号指纹（最可靠，跨设备跨网络）
    2. client_id（localStorage，同设备稳定）
    3. IP+UA（兜底，不太可靠）
    """
    import hashlib
    # 1. 优先检查登录token
    token = get_auth_token(req)
    if token:
        acct_fp = db.get_account_fingerprint(token)
        if acct_fp:
            return acct_fp
    # 2. 其次用 client_id
    client_id = req.headers.get('X-Client-Id', '')
    if not client_id and req.is_json:
        try:
            body = req.get_json(silent=True) or {}
            client_id = body.get('client_id', '')
        except Exception:
            pass
    if client_id:
        return hashlib.sha256(f"cid:{client_id}".encode()).hexdigest()[:32]
    # 3. 回退到 IP+UA
    ip = get_client_ip(req)
    ua = req.headers.get('User-Agent', 'unknown')
    return db.get_fingerprint(ip, ua)


# ==================== 页面路由 ====================

@app.route('/')
def index():
    """主页 - 禁止缓存，确保用户每次拿到最新页面"""
    resp = make_response(INDEX_HTML)
    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['Expires'] = '0'
    return resp


# ==================== API路由 ====================

@app.route('/health')
def health():
    """健康检查端点（用于负载均衡/Docker健康检查）"""
    return jsonify({
        'status': 'ok',
        'service': 'xuanjige',
        'version': '1.0.0',
        'ai_enabled': bool(ai_service.DEEPSEEK_API_KEY)
    })


@app.route('/api/paipan', methods=['POST'])
def api_paipan():
    """八字排盘API（纯计算，不消耗配额），自动保存到历史记录"""
    try:
        data = request.json
        # 后端表单验证
        required = ['year', 'month', 'day', 'hour', 'minute', 'gender']
        for field in required:
            val = data.get(field)
            if val is None or val == '':
                return jsonify({'error': f'字段 {field} 不能为空'}), 400
        year = int(data['year'])
        month = int(data['month'])
        day_val = int(data['day'])
        hour = int(data['hour'])
        minute = int(data['minute'])
        gender = data['gender']
        if year < 1900 or year > 2100:
            return jsonify({'error': '年份需在1900-2100之间'}), 400
        if month < 1 or month > 12:
            return jsonify({'error': '月份需在1-12之间'}), 400
        if day_val < 1 or day_val > 31:
            return jsonify({'error': '日期需在1-31之间'}), 400
        if hour < 0 or hour > 23:
            return jsonify({'error': '时辰需在0-23之间'}), 400
        if minute < 0 or minute > 59:
            return jsonify({'error': '分钟需在0-59之间'}), 400
        if gender not in ('male', 'female'):
            return jsonify({'error': '性别不合法'}), 400

        # 校验真实存在的日期（如2月30日）
        import datetime as _dt
        try:
            _dt.date(year, month, day_val)
        except ValueError:
            return jsonify({'error': f'{year}年{month}月{day_val}日不是有效日期，请检查'}), 400

        result = paipan(year, month, day_val, hour, minute, gender)

        # 保存到历史记录
        fingerprint = get_fingerprint(request)
        name = data.get('name', '')
        db.save_history(
            fingerprint, name, data['gender'],
            result['solar_date'],
            json.dumps(result, ensure_ascii=False),
            has_ai=False
        )

        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/interpret', methods=['POST'])
def api_interpret():
    """
    AI解读API（消耗配额）
    流式SSE输出

    逻辑:
    1. 识别用户指纹（必须已登录）
    2. 查缓存 → 命中则直接回放（永远免费，不消耗配额）
    3. 未命中 → 检查配额 → 消耗配额 → 流式调用DeepSeek → 缓存结果
    """
    # --- 用户识别 ---
    fingerprint = get_fingerprint(request)
    ip = get_client_ip(request)
    ua = request.headers.get('User-Agent', 'unknown')

    # 检查是否登录
    auth_token = get_auth_token(request)
    is_logged_in = bool(auth_token and db.get_account_fingerprint(auth_token))

    # --- 必须登录才能使用AI解读 ---
    if not is_logged_in:
        return jsonify({
            'error': '请先登录',
            'message': 'AI解读需要登录账号后才能使用，新注册用户有5次免费解读机会',
            'need_login': True
        }), 401

    # --- 获取排盘数据 ---
    paipan_data = request.json.get('paipan', {})

    # --- 查缓存（按账号隔离：该账号算过的八字，永远免费回放，即使配额用完也能看） ---
    cache_key = fingerprint + ':' + ai_service.build_cache_key(paipan_data)
    cached = db.get_cache(cache_key)
    legacy_cache_key = fingerprint + ':' + ai_service.build_legacy_cache_key(paipan_data)
    legacy_cached = None if cached else db.get_cache(legacy_cache_key)

    if cached:
        # 命盘已有解读存档（该账号算过的八字）：不消耗配额，直接回放
        db.log_usage(fingerprint, '/api/interpret', True, 0, 0)
        db.mark_history_has_ai(
            fingerprint,
            paipan_data.get('gender', ''),
            paipan_data.get('solar_date', '')
        )

        def cached_stream():
            import time
            text = cached['interpretation']
            # 按句子分段输出，模拟流式打字效果
            chunks = []
            current = ''
            for char in text:
                current += char
                if char in '。！？\n' or len(current) >= 8:
                    chunks.append(current)
                    current = ''
            if current:
                chunks.append(current)

            for chunk_text in chunks:
                yield f'data: {json.dumps({"text": chunk_text}, ensure_ascii=False)}\n\n'
                time.sleep(0.03)  # 30ms间隔，模拟打字

            # 发送meta信息
            meta = {
                'done': True,
                'prompt_tokens': cached['prompt_tokens'],
                'completion_tokens': cached['completion_tokens'],
                'total_tokens': cached['total_tokens'],
                'cost_usd': cached['cost_usd'],
                'cached': True,
            }
            yield f'data: {json.dumps(meta, ensure_ascii=False)}\n\n'

        return Response(
            cached_stream(),
            mimetype='text/event-stream',
            headers={
                'Cache-Control': 'no-cache',
                'X-Accel-Buffering': 'no',
                'X-Cache-Hit': 'true',
            }
        )

    # --- 未命中新版缓存：旧版缓存存在则免费刷新，否则检查配额 ---
    legacy_refresh = legacy_cached is not None
    quota_result = db.check_quota_account(auth_token)
    if quota_result is None:
        return jsonify({'error': '登录已过期，请重新登录', 'need_login': True}), 401
    can_use, free_remaining, is_free = quota_result

    if not can_use and not legacy_refresh:
        return jsonify({
            'error': '配额已用完',
            'message': '您的5次免费解读机会已用完',
            'need_recharge': True
        }), 403

    # --- 在流式成功且通过质量校验后消耗配额 ---
    def generate():
        full_text = ''
        meta = {}

        for chunk in ai_service.stream_interpretation(paipan_data):
            yield chunk

            # 解析chunk获取token信息
            if chunk.startswith('data: ') and chunk.endswith('\n\n'):
                try:
                    data = json.loads(chunk[6:].strip())
                    if data.get('done'):
                        full_text = data.get('full_text', '')
                        meta = data
                    if data.get('error'):
                        # AI调用失败，不消耗配额
                        return
                except json.JSONDecodeError:
                    pass

        # 保存到缓存
        if full_text and meta.get('total_tokens'):
            if not legacy_refresh:
                db.consume_quota_account(auth_token, is_free)
            paipan_json = json.dumps(paipan_data, ensure_ascii=False)
            db.save_cache(
                cache_key, paipan_json, full_text,
                meta.get('prompt_tokens', 0),
                meta.get('completion_tokens', 0),
                meta.get('total_tokens', 0),
                ai_service.DEEPSEEK_MODEL,
                meta.get('cost_usd', 0)
            )
            # 记录日志
            db.log_usage(
                fingerprint, '/api/interpret',
                False, meta.get('total_tokens', 0),
                meta.get('cost_usd', 0)
            )
            # 标记历史记录为已解读
            db.mark_history_has_ai(
                fingerprint,
                paipan_data.get('gender', ''),
                paipan_data.get('solar_date', '')
            )

    return Response(
        generate(),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',  # Nginx不缓冲
            'X-Free-Remaining': str(free_remaining),
            'X-Is-Free': str(is_free or legacy_refresh).lower(),
            'X-Cache-Refresh': str(legacy_refresh).lower(),
        }
    )


@app.route('/api/quota', methods=['GET'])
def api_quota():
    """查询当前用户配额"""
    # 优先检查登录状态
    auth_token = get_auth_token(request)
    account = db.get_account_by_token(auth_token)
    if account:
        free_remaining = max(0, 5 - account['free_trials_used'])
        return jsonify({
            'can_use': free_remaining > 0 or (account['credits'] or 0) > 0,
            'free_remaining': free_remaining,
            'free_total': 5,
            'is_free': free_remaining > 0,
            'logged_in': True,
            'username': account['username'],
            'credits': account['credits'] or 0,
        })
    # 未登录，用指纹配额
    fingerprint = get_fingerprint(request)
    can_use, free_remaining, is_free = db.check_quota(fingerprint)
    return jsonify({
        'can_use': can_use,
        'free_remaining': free_remaining,
        'free_total': 5,
        'is_free': is_free,
        'logged_in': False,
    })


# ==================== 账号系统 ====================

@app.route('/api/auth-config', methods=['GET'])
def api_auth_config():
    """Expose safe auth UX flags without exposing invite codes."""
    return jsonify({
        'invite_required': bool(REGISTRATION_INVITE_CODES),
    })


@app.route('/api/register', methods=['POST'])
def api_register():
    """注册"""
    data = request.json or {}
    username = (data.get('username') or '').strip()
    password = data.get('password', '')
    invite_code = (data.get('invite_code') or '').strip()
    client_id = (data.get('client_id') or request.headers.get('X-Client-Id', '') or '').strip()[:128]
    client_ip = get_client_ip(request)

    allowed, gate_error = db.check_registration_gate(
        client_ip,
        client_id,
        username,
        REGISTRATION_RATE_WINDOW_MINUTES,
        REGISTRATION_RATE_MAX_ATTEMPTS,
        REGISTRATION_DAILY_MAX_PER_IP,
        REGISTRATION_DAILY_MAX_PER_CLIENT,
    )
    if not allowed:
        db.record_registration_attempt(client_ip, client_id, username, False, gate_error)
        return jsonify({'error': gate_error}), 429

    if REGISTRATION_INVITE_CODES and invite_code not in REGISTRATION_INVITE_CODES:
        db.record_registration_attempt(client_ip, client_id, username, False, '邀请码错误')
        return jsonify({'error': '体验码不正确，请确认后再注册'}), 403

    token, error = db.register(username, password)
    if error:
        db.record_registration_attempt(client_ip, client_id, username, False, error)
        return jsonify({'error': error}), 400
    db.record_registration_attempt(client_ip, client_id, username, True, '')
    # 访客先排过的盘，注册后迁移到账号名下
    acct_fp = db.get_account_fingerprint(token)
    if acct_fp:
        client_fp = _client_fingerprint(request)
        db.migrate_history(client_fp, acct_fp)
    resp = make_response(jsonify({'token': token, 'username': username}))
    resp.set_cookie('auth_token', token, max_age=365*24*3600, httponly=True, samesite='Lax')
    return resp


@app.route('/api/login', methods=['POST'])
def api_login():
    """登录"""
    data = request.json or {}
    username = (data.get('username') or '').strip()
    password = data.get('password', '')
    token, error = db.login(username, password)
    if error:
        return jsonify({'error': error}), 401
    # 访客先排过的盘，登录后迁移到账号名下
    acct_fp = db.get_account_fingerprint(token)
    if acct_fp:
        client_fp = _client_fingerprint(request)
        db.migrate_history(client_fp, acct_fp)
    resp = make_response(jsonify({'token': token, 'username': username}))
    resp.set_cookie('auth_token', token, max_age=365*24*3600, httponly=True, samesite='Lax')
    return resp


@app.route('/api/logout', methods=['POST'])
def api_logout():
    """注销"""
    token = get_auth_token(request)
    if token:
        db.logout(token)
    resp = make_response(jsonify({'ok': True}))
    resp.delete_cookie('auth_token')
    return resp


@app.route('/api/me', methods=['GET'])
def api_me():
    """获取当前登录用户信息"""
    token = get_auth_token(request)
    account = db.get_account_by_token(token)
    if account:
        return jsonify({
            'logged_in': True,
            'username': account['username'],
            'free_remaining': max(0, 5 - account['free_trials_used']),
            'free_total': 5,
            'credits': account['credits'] or 0,
        })
    # 未登录，返回匿名用户配额
    fingerprint = get_fingerprint(request)
    can_use, free_remaining, is_free = db.check_quota(fingerprint)
    return jsonify({
        'logged_in': False,
        'free_remaining': free_remaining,
        'free_total': 5,
    })


# ==================== 其他 ====================

@app.route('/api/stats', methods=['GET'])
def api_stats():
    """系统统计（管理用）"""
    return jsonify(db.get_stats())


@app.route('/api/history', methods=['GET'])
def api_history_list():
    """获取用户历史记录列表"""
    fingerprint = get_fingerprint(request)
    history = db.get_history(fingerprint)
    return jsonify(history)


@app.route('/api/history/<int:hid>', methods=['GET'])
def api_history_detail(hid):
    """获取某条历史详情（重新查看排盘）"""
    fingerprint = get_fingerprint(request)
    detail = db.get_history_detail(hid, fingerprint)
    if detail:
        import json as _json
        detail['paipan'] = _json.loads(detail['paipan_json'])
        return jsonify(detail)
    return jsonify({'error': '记录不存在'}), 404


@app.route('/api/history/<int:hid>', methods=['DELETE'])
def api_history_delete(hid):
    """删除某条历史"""
    fingerprint = get_fingerprint(request)
    db.delete_history(hid, fingerprint)
    return jsonify({'ok': True})


# ==================== 前端HTML ====================

INDEX_HTML = r'''
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover, maximum-scale=1.0, user-scalable=no">
    <meta name="theme-color" content="#0a0a0f">
    <meta name="apple-mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
    <title>玄机阁 · 八字排盘</title>
    <script>
        (function() {
            try {
                var savedTheme = localStorage.getItem('xjg_theme');
                var prefersLight = window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches;
                var theme = savedTheme === 'light' || savedTheme === 'dark' ? savedTheme : (prefersLight ? 'light' : 'dark');
                document.documentElement.dataset.theme = theme;
            } catch (e) {
                document.documentElement.dataset.theme = 'dark';
            }
        })();
    </script>
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Noto+Serif+SC:wght@400;700&display=swap');
        * { margin: 0; padding: 0; box-sizing: border-box; }
        :root {
            color-scheme: dark;
            --bg-dark: #0a0a0f; --bg-card: #15151f; --bg-card-hover: #1a1a28;
            --border: #2a2a3a; --gold: #c9a84c; --gold-bright: #e8c870;
            --gold-dim: #8a7030; --text: #d4c8a8; --text-dim: #6a6a7a;
            --red: #8b2020; --red-bright: #c94040; --green: #4a9a4a;
            --button-text: #0a0a0f; --header-bg: rgba(10,10,15,0.82);
            --gold-tint: rgba(201,168,76,0.12); --gold-tint-strong: rgba(201,168,76,0.18);
            --gold-border: rgba(201,168,76,0.32); --gold-shadow: rgba(201,168,76,0.3);
            --red-tint: rgba(201,64,64,0.12); --red-border: rgba(201,64,64,0.75);
            --body-glow-a: rgba(201,168,76,0.05); --body-glow-b: rgba(139,32,32,0.05);
            --sigil-ink: rgba(232,200,112,0.26); --sigil-ink-strong: rgba(232,200,112,0.4);
            --scripture-ink: rgba(232,200,112,0.24); --cinnabar-ink: rgba(201,64,64,0.22);
            --modal-scrim: rgba(0,0,0,0.7); --overlay-bg: rgba(10,10,15,0.95);
            --theme-toggle-bg: rgba(201,168,76,0.1); --theme-toggle-hover: rgba(201,168,76,0.18);
        }
        html[data-theme="light"] {
            color-scheme: light;
            --bg-dark: #f6eedf; --bg-card: #fffaf0; --bg-card-hover: #fff2d2;
            --border: #d8c491; --gold: #8a5d18; --gold-bright: #704a12;
            --gold-dim: #7a5314; --text: #34291b; --text-dim: #75634a;
            --red: #9d2e2e; --red-bright: #b33a32; --green: #347b43;
            --button-text: #fffaf0; --header-bg: rgba(255,250,240,0.88);
            --gold-tint: rgba(138,93,24,0.12); --gold-tint-strong: rgba(138,93,24,0.18);
            --gold-border: rgba(138,93,24,0.34); --gold-shadow: rgba(138,93,24,0.24);
            --red-tint: rgba(179,58,50,0.11); --red-border: rgba(179,58,50,0.65);
            --body-glow-a: rgba(138,93,24,0.12); --body-glow-b: rgba(179,58,50,0.08);
            --sigil-ink: rgba(112,74,18,0.22); --sigil-ink-strong: rgba(112,74,18,0.34);
            --scripture-ink: rgba(112,74,18,0.22); --cinnabar-ink: rgba(157,46,46,0.18);
            --modal-scrim: rgba(41,31,19,0.48); --overlay-bg: rgba(246,238,223,0.96);
            --theme-toggle-bg: rgba(138,93,24,0.1); --theme-toggle-hover: rgba(138,93,24,0.18);
        }
        body {
            font-family: 'Noto Serif SC', serif;
            background: var(--bg-dark); color: var(--text); min-height: 100vh;
            padding-top: env(safe-area-inset-top);
            padding-bottom: env(safe-area-inset-bottom);
            -webkit-text-size-adjust: 100%;
            -webkit-tap-highlight-color: transparent;
            position: relative;
            background-image:
                radial-gradient(ellipse at 20% 0%, var(--body-glow-a) 0%, transparent 50%),
                radial-gradient(ellipse at 80% 100%, var(--body-glow-b) 0%, transparent 50%);
            transition: background-color 0.25s ease, color 0.25s ease;
        }
        body::before, body::after {
            content: ''; position: fixed; pointer-events: none; z-index: 0;
        }
        body::before {
            inset: -22% -12%;
            opacity: 0.5;
            background:
                repeating-radial-gradient(circle at 22% 22%, transparent 0 42px, var(--gold-tint) 43px 44px, transparent 45px 96px),
                repeating-radial-gradient(circle at 78% 70%, transparent 0 54px, var(--red-tint) 55px 56px, transparent 57px 118px),
                conic-gradient(from 22deg at 74% 24%, transparent 0 8deg, var(--gold-tint) 9deg 12deg, transparent 13deg 34deg, var(--gold-tint) 35deg 37deg, transparent 38deg 70deg);
            mask-image: radial-gradient(ellipse at center, #000 0%, #000 62%, transparent 84%);
            animation: dao-light-drift 34s ease-in-out infinite alternate;
        }
        body::after {
            inset: 0;
            opacity: 0.42;
            background:
                radial-gradient(circle at 24% 34%, transparent 0 74px, var(--gold-tint) 75px 77px, transparent 78px 118px, var(--gold-tint) 119px 120px, transparent 121px),
                radial-gradient(circle at 76% 18%, transparent 0 52px, var(--gold-tint) 53px 55px, transparent 56px 92px, var(--red-tint) 93px 94px, transparent 95px),
                radial-gradient(circle at 68% 78%, transparent 0 88px, var(--red-tint) 89px 91px, transparent 92px 138px, var(--gold-tint) 139px 140px, transparent 141px);
            animation: ripple-field 18s ease-in-out infinite alternate;
        }
        html[data-theme="light"] body::before { opacity: 0.32; }
        html[data-theme="light"] body::after { opacity: 0.3; }
        @keyframes dao-light-drift {
            from { transform: translate3d(-1.5%, -1%, 0) rotate(0deg) scale(1); }
            to { transform: translate3d(1.5%, 1%, 0) rotate(4deg) scale(1.03); }
        }
        @keyframes ripple-field {
            from { transform: translate3d(-0.8%, -0.8%, 0) scale(0.992); }
            to { transform: translate3d(0.8%, 1.1%, 0) scale(1.018); }
        }
        .ambient-bg {
            position: fixed; inset: 0; overflow: hidden; pointer-events: none; z-index: 0;
            color: var(--gold); contain: strict;
        }
        .dao-sigil {
            position: absolute; width: clamp(230px, 30vw, 390px); aspect-ratio: 1;
            border-radius: 50%; opacity: 0.34; color: var(--gold-bright);
            border: 1px solid var(--sigil-ink-strong);
            background:
                radial-gradient(circle, transparent 0 23%, var(--sigil-ink) 24% 25%, transparent 26% 43%, var(--sigil-ink) 44% 45%, transparent 46%),
                repeating-conic-gradient(from -4deg, transparent 0 15deg, var(--sigil-ink-strong) 16deg 18deg, transparent 19deg 45deg);
            box-shadow: inset 0 0 52px var(--gold-tint-strong), 0 0 44px rgba(201,168,76,0.14);
            animation: sigil-breathe 12s ease-in-out infinite;
        }
        .dao-sigil::before, .dao-sigil::after {
            content: ''; position: absolute; inset: 16%; border-radius: 50%;
            border: 1px solid var(--sigil-ink-strong);
        }
        .dao-sigil::after { inset: 34%; opacity: 0.85; }
        .dao-sigil-a { --sigil-rotate: -10deg; left: -52px; top: 82px; transform: rotate(var(--sigil-rotate)); }
        .dao-sigil-b { --sigil-rotate: 18deg; right: -64px; bottom: 34px; transform: rotate(var(--sigil-rotate)); animation-delay: -5s; }
        .dao-trigram {
            position: absolute;
            font-size: clamp(0.88rem, 1.8vw, 1.18rem); font-weight: 700;
            letter-spacing: 0; text-shadow: 0 0 14px var(--gold-shadow), 0 0 1px currentColor;
        }
        .dao-trigram:nth-child(1) { left: 50%; top: 8%; transform: translate(-50%, -50%); }
        .dao-trigram:nth-child(2) { right: 17%; top: 17%; transform: translate(50%, -50%); }
        .dao-trigram:nth-child(3) { right: 8%; top: 50%; transform: translate(50%, -50%); }
        .dao-trigram:nth-child(4) { right: 17%; bottom: 17%; transform: translate(50%, 50%); }
        .dao-trigram:nth-child(5) { left: 50%; bottom: 8%; transform: translate(-50%, 50%); }
        .dao-trigram:nth-child(6) { left: 17%; bottom: 17%; transform: translate(-50%, 50%); }
        .dao-trigram:nth-child(7) { left: 8%; top: 50%; transform: translate(-50%, -50%); }
        .dao-trigram:nth-child(8) { left: 17%; top: 17%; transform: translate(-50%, -50%); }
        .dao-talisman {
            position: absolute; width: clamp(74px, 8vw, 118px); min-height: clamp(230px, 28vw, 340px);
            border: 1px solid var(--cinnabar-ink); border-radius: 48px;
            color: var(--red-bright); opacity: 0.22;
            display: flex; flex-direction: column; align-items: center; justify-content: space-around;
            padding: 1.2rem 0.35rem; background: linear-gradient(180deg, transparent, var(--red-tint), transparent);
            box-shadow: inset 0 0 28px var(--red-tint), 0 0 22px rgba(201,64,64,0.08);
            animation: talisman-float 13s ease-in-out infinite;
        }
        .dao-talisman span {
            writing-mode: vertical-rl; font-size: clamp(0.8rem, 1.25vw, 1.05rem);
            line-height: 1; letter-spacing: 0.28em; text-shadow: 0 0 12px var(--red-tint);
        }
        .dao-talisman-a { --talisman-rotate: 8deg; right: 6vw; top: 33vh; }
        .dao-talisman-b { --talisman-rotate: -7deg; left: 4vw; bottom: 7vh; animation-delay: -6s; }
        .scripture-flow {
            position: absolute; max-width: 12.5rem;
            font-size: clamp(0.58rem, 0.82vw, 0.78rem); line-height: 1.85;
            letter-spacing: 0.18em; color: var(--gold-bright);
            opacity: 0.2; text-shadow: 0 0 14px var(--gold-shadow), 0 0 1px var(--scripture-ink);
            filter: blur(0.05px); animation: scripture-ripple 9s ease-in-out infinite;
        }
        .scripture-flow::after {
            content: ''; position: absolute; left: 50%; top: 50%; width: 155%; aspect-ratio: 1;
            border-radius: 50%; border: 1px solid currentColor; opacity: 0.22;
            transform: translate(-50%, -50%) scale(0.72);
            animation: scripture-ring 5.8s ease-out infinite;
        }
        .scripture-a { left: 7vw; top: 18vh; animation-delay: -1s; }
        .scripture-b { right: 8vw; top: 15vh; max-width: 13rem; animation-delay: -4s; }
        .scripture-c { left: 10vw; bottom: 13vh; max-width: 14rem; animation-delay: -6s; }
        .scripture-d { right: 16vw; bottom: 22vh; max-width: 15rem; animation-delay: -2.5s; }
        .scripture-e { left: 42vw; top: 8vh; max-width: 12rem; animation-delay: -7.2s; }
        .scripture-f { left: 52vw; bottom: 8vh; max-width: 13rem; animation-delay: -3.4s; }
        .scripture-g { right: 29vw; top: 35vh; max-width: 10rem; animation-delay: -5.1s; }
        .scripture-h { left: 28vw; bottom: 30vh; max-width: 10rem; animation-delay: -8s; }
        .scripture-i { right: 6vw; bottom: 48vh; max-width: 9rem; animation-delay: -2s; }
        .scripture-j { left: 3vw; top: 49vh; max-width: 9rem; animation-delay: -6.8s; }
        @keyframes sigil-breathe {
            0%, 100% { opacity: 0.24; transform: translate3d(0,0,0) rotate(var(--sigil-rotate, 0deg)) scale(0.98); }
            50% { opacity: 0.42; transform: translate3d(0,-6px,0) rotate(var(--sigil-rotate, 0deg)) scale(1.03); }
        }
        @keyframes talisman-float {
            0%, 100% { opacity: 0.16; transform: translate3d(0,0,0) rotate(var(--talisman-rotate, 0deg)); }
            50% { opacity: 0.28; transform: translate3d(0,-10px,0) rotate(var(--talisman-rotate, 0deg)); }
        }
        @keyframes scripture-ripple {
            0%, 100% { opacity: 0.12; transform: translate3d(0, 0, 0) scale(0.982); }
            45% { opacity: 0.28; transform: translate3d(10px, -8px, 0) scale(1.018); }
            70% { opacity: 0.17; transform: translate3d(-7px, 6px, 0) scale(1.002); }
        }
        @keyframes scripture-ring {
            0% { opacity: 0.18; transform: translate(-50%, -50%) scale(0.62); }
            100% { opacity: 0; transform: translate(-50%, -50%) scale(1.25); }
        }
        .header {
            border-bottom: 1px solid var(--border); padding: 0.5rem 0.75rem;
            padding-top: calc(0.5rem + env(safe-area-inset-top, 0px));
            display: flex; align-items: center; justify-content: space-between;
            background: var(--header-bg); backdrop-filter: blur(10px);
            position: sticky; top: 0; z-index: 100;
        }
        .logo { display: flex; align-items: center; gap: 0.5rem; min-width: 0; }
        .logo-taiji {
            width: 28px; height: 28px; border-radius: 50%;
            background: linear-gradient(135deg, var(--text) 50%, var(--bg-dark) 50%);
            border: 2px solid var(--gold); position: relative;
            animation: spin 8s linear infinite;
        }
        .logo-taiji::after {
            content: ''; position: absolute; top: 50%; left: 50%;
            width: 4px; height: 4px; border-radius: 50%;
            background: var(--gold); transform: translate(-50%,-50%);
        }
        @keyframes spin { to { transform: rotate(360deg); } }
        .logo-text { font-size: 1.1rem; font-weight: 700; color: var(--gold); letter-spacing: 0.15em; }
        .header-sub { font-size: 0.7rem; color: var(--text-dim); }
        .header-actions { display: flex; align-items: center; gap: 0.65rem; flex-shrink: 0; }
        .theme-toggle {
            width: 44px; height: 44px; min-width: 44px; border-radius: 50%;
            border: 1px solid var(--gold-border); background: var(--theme-toggle-bg);
            color: var(--gold-bright); cursor: pointer; display: inline-flex;
            align-items: center; justify-content: center; transition: background 0.2s ease, border-color 0.2s ease, transform 0.2s ease;
            -webkit-appearance: none; touch-action: manipulation;
        }
        .theme-toggle:hover { background: var(--theme-toggle-hover); border-color: var(--gold); }
        .theme-toggle:active { transform: scale(0.96); }
        .theme-toggle:focus-visible { outline: 2px solid var(--gold-bright); outline-offset: 3px; }
        .theme-toggle-icon { position: relative; width: 22px; height: 22px; display: block; }
        .theme-toggle-icon::before {
            content: ''; position: absolute; inset: 3px; border-radius: 50%;
            border: 2px solid currentColor; box-shadow: inset -5px -3px 0 0 currentColor;
            transition: all 0.2s ease;
        }
        html[data-theme="light"] .theme-toggle-icon::before {
            inset: 5px; background: currentColor; border: 0;
            box-shadow:
                0 -8px 0 -5px currentColor,
                0 8px 0 -5px currentColor,
                8px 0 0 -5px currentColor,
                -8px 0 0 -5px currentColor,
                6px 6px 0 -5px currentColor,
                -6px -6px 0 -5px currentColor,
                6px -6px 0 -5px currentColor,
                -6px 6px 0 -5px currentColor;
        }
        .quota-badge {
            background: var(--gold-tint-strong); border: 1px solid var(--gold-border);
            border-radius: 20px; padding: 0.2rem 0.6rem; min-height: 44px;
            display: inline-flex; align-items: center; justify-content: center;
            font-size: 0.7rem; color: var(--gold); white-space: nowrap; cursor: pointer;
        }
        .container { max-width: 900px; margin: 0 auto; padding: 0.75rem; padding-bottom: calc(0.75rem + env(safe-area-inset-bottom, 0px)); position: relative; z-index: 1; }

        /* 输入卡片 */
        .input-card {
            background: var(--bg-card); border: 1px solid var(--border);
            border-radius: 12px; padding: 1rem; margin-bottom: 1rem; position: relative;
        }
        .input-card::before {
            content: ''; position: absolute; top: 0; left: 0; right: 0; height: 2px;
            background: linear-gradient(90deg, transparent, var(--gold), transparent);
            border-radius: 12px 12px 0 0;
        }
        .card-title { font-size: 1.1rem; color: var(--gold); margin-bottom: 0.75rem; text-align: center; letter-spacing: 0.1em; }
        .form-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 0.6rem; min-width: 0; }
        .form-group { display: flex; flex-direction: column; gap: 0.3rem; min-width: 0; }
        .form-group label { font-size: 0.8rem; color: var(--text-dim); }
        .form-group input, .form-group select {
            background: var(--bg-dark); border: 1px solid var(--border);
            border-radius: 8px; padding: 0.65rem 0.5rem; color: var(--text);
            font-family: inherit; font-size: 1rem; transition: border-color 0.3s;
            min-height: 44px; -webkit-appearance: none; width: 100%; box-sizing: border-box;
        }
        .form-group input:focus, .form-group select:focus { outline: none; border-color: var(--gold); }
        .birth-picker-trigger {
            width: 100%; min-height: 56px; padding: 0.75rem 0.85rem;
            border: 1px solid var(--border); border-radius: 10px; background: var(--bg-dark);
            color: var(--text); font-family: inherit; text-align: left; cursor: pointer;
            display: flex; align-items: center; justify-content: space-between; gap: 1rem;
            transition: border-color 0.22s ease, background 0.22s ease, transform 0.18s ease;
            touch-action: manipulation;
        }
        .birth-picker-trigger:hover { border-color: var(--gold-border); background: var(--bg-card-hover); }
        .birth-picker-trigger:active { transform: scale(0.99); }
        .birth-picker-trigger:focus-visible { outline: 2px solid var(--gold-bright); outline-offset: 3px; }
        .birth-picker-value { color: var(--gold-bright); font-size: 1rem; line-height: 1.4; }
        .birth-picker-trigger.is-empty .birth-picker-value { color: var(--text-dim); }
        .birth-picker-hint { color: var(--text-dim); font-size: 0.75rem; white-space: nowrap; }
        .birth-picker-overlay {
            display: none; position: fixed; inset: 0; z-index: 260; background: var(--modal-scrim);
            align-items: flex-start; justify-content: center;
            padding: clamp(4.5rem, 12vh, 7.5rem) 1rem 1rem;
            backdrop-filter: blur(8px);
        }
        .birth-picker-overlay.active { display: flex; }
        .birth-picker-sheet {
            width: 100%; max-width: 560px; background: var(--bg-card);
            border: 1px solid var(--border);
            border-radius: 18px; padding: 0.85rem 0.85rem 1rem;
            box-shadow: 0 24px 70px rgba(0,0,0,0.32), 0 0 0 1px var(--gold-tint);
            animation: birth-sheet-in 0.3s cubic-bezier(.2,.85,.25,1) both;
            max-height: min(82dvh, 460px);
            overflow: hidden;
        }
        .birth-picker-toolbar { display: flex; align-items: center; justify-content: space-between; gap: 1rem; margin-bottom: 0.75rem; }
        .birth-picker-title { color: var(--gold-bright); font-size: 1rem; letter-spacing: 0.08em; }
        .birth-picker-btn {
            min-width: 56px; min-height: 44px; border: 0; border-radius: 999px;
            background: transparent; color: var(--text-dim); font-family: inherit; font-size: 0.9rem;
            cursor: pointer; touch-action: manipulation;
        }
        .birth-picker-btn.confirm { color: var(--button-text); background: linear-gradient(135deg, var(--gold-dim), var(--gold)); font-weight: 700; }
        .birth-picker-btn:focus-visible { outline: 2px solid var(--gold-bright); outline-offset: 2px; }
        .birth-picker-grid { display: grid; grid-template-columns: 1.35fr repeat(4, 1fr); gap: 0.35rem; }
        .birth-wheel-wrap { position: relative; min-width: 0; }
        .birth-wheel-label { text-align: center; color: var(--text-dim); font-size: 0.72rem; margin-bottom: 0.25rem; }
        .birth-wheel-wrap::after {
            content: ''; pointer-events: none; position: absolute; left: 0; right: 0; top: 96px; height: 38px;
            border-top: 1px solid var(--gold-border); border-bottom: 1px solid var(--gold-border);
            background: var(--gold-tint); border-radius: 8px;
        }
        .birth-wheel {
            height: 188px; overflow-y: auto; scroll-snap-type: y mandatory; padding: 72px 0;
            scrollbar-width: none; -webkit-overflow-scrolling: touch;
            mask-image: linear-gradient(to bottom, transparent, #000 18%, #000 82%, transparent);
            cursor: grab; user-select: none; overscroll-behavior: contain;
        }
        .birth-wheel.is-dragging { cursor: grabbing; scroll-snap-type: none; }
        .birth-wheel::-webkit-scrollbar { display: none; }
        .birth-wheel-item {
            height: 38px; width: 100%; border: 0; background: transparent; color: var(--text-dim);
            display: flex; align-items: center; justify-content: center; font-family: inherit;
            font-size: 0.95rem; scroll-snap-align: center; cursor: pointer; position: relative; z-index: 1;
            transition: color 0.18s ease, transform 0.18s ease;
        }
        .birth-wheel-item.is-selected { color: var(--gold-bright); font-weight: 700; transform: scale(1.06); }
        @keyframes birth-sheet-in { from { opacity: 0; transform: translateY(-8px) scale(0.98); } to { opacity: 1; transform: translateY(0) scale(1); } }
        .btn-divine {
            display: block; width: 100%; padding: 0.8rem; margin-top: 0.75rem;
            background: linear-gradient(135deg, var(--gold-dim), var(--gold));
            border: none; border-radius: 8px; color: var(--button-text);
            font-family: inherit; font-size: 1.05rem; font-weight: 700;
            cursor: pointer; letter-spacing: 0.15em; transition: all 0.3s;
            min-height: 46px; -webkit-appearance: none;
        }
        .btn-divine:hover {
            background: linear-gradient(135deg, var(--gold), var(--gold-bright));
            box-shadow: 0 4px 20px var(--gold-shadow);
        }
        .btn-divine:disabled { opacity: 0.5; cursor: not-allowed; }

        /* 结果区 */
        .result-section { display: none; margin-bottom: 2rem; }
        .result-section.active { display: block; }

        /* 八字表 */
        .bazi-table {
            background: var(--bg-card); border: 1px solid var(--border);
            border-radius: 12px; overflow-x: auto; -webkit-overflow-scrolling: touch; margin-bottom: 1.5rem;
        }
        .bazi-table table { width: 100%; border-collapse: collapse; min-width: 320px; }
        .bazi-table th {
            background: var(--gold-tint); color: var(--gold); padding: 0.8rem;
            font-weight: 700; border-bottom: 1px solid var(--border); font-size: 0.9rem;
        }
        .bazi-table td { padding: 0.8rem; text-align: center; border-bottom: 1px solid var(--border); color: var(--text); }
        .bazi-table tr:last-child td { border-bottom: none; }
        .bazi-gan-zhi { font-size: 1.5rem; font-weight: 700; color: var(--gold-bright); }

        /* 信息卡片 */
        .info-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 1rem; margin-bottom: 1.5rem; }
        .info-grid > * { min-width: 0; }
        .info-card { background: var(--bg-card); border: 1px solid var(--border); border-radius: 8px; padding: 1.2rem; }
        .info-card-title { font-size: 0.85rem; color: var(--text-dim); margin-bottom: 0.5rem; }
        .info-card-value { font-size: 1.1rem; color: var(--gold-bright); font-weight: 700; }
        .info-card-detail { font-size: 0.85rem; color: var(--text); margin-top: 0.4rem; line-height: 1.6; }

        /* 五行条 */
        .wuxing-bars { display: flex; gap: 0.8rem; margin-top: 0.5rem; }
        .wuxing-bars > * { min-width: 0; }
        .wuxing-bar { flex: 1; text-align: center; }
        .wuxing-bar-label { font-size: 0.8rem; margin-bottom: 0.3rem; }
        .wuxing-bar-track { height: 8px; background: var(--bg-dark); border-radius: 4px; overflow: hidden; }
        .wuxing-bar-fill { height: 100%; border-radius: 4px; transition: width 0.8s ease; }
        .wuxing-metal { background: #c0c0c0; }
        .wuxing-wood { background: #4a8a3a; }
        .wuxing-water { background: #3a6ac0; }
        .wuxing-fire { background: #c04a3a; }
        .wuxing-earth { background: #8a7a4a; }

        /* 大运表 */
        .dayun-table { background: var(--bg-card); border: 1px solid var(--border); border-radius: 12px; overflow-x: auto; -webkit-overflow-scrolling: touch; margin-bottom: 1.5rem; }
        .dayun-table table { width: 100%; border-collapse: collapse; min-width: 360px; }
        .dayun-table th { background: var(--gold-tint); color: var(--gold); padding: 0.6rem; font-size: 0.85rem; border-bottom: 1px solid var(--border); }
        .dayun-table td { padding: 0.6rem; text-align: center; border-bottom: 1px solid var(--border); font-size: 0.9rem; }
        .dayun-current { background: var(--gold-tint); }

        /* AI解读 */
        .ai-section {
            background: var(--bg-card); border: 1px solid var(--border);
            border-radius: 12px; padding: 1.2rem; margin-bottom: 1.5rem; position: relative;
        }
        .ai-section::before {
            content: ''; position: absolute; top: 0; left: 0; right: 0; height: 2px;
            background: linear-gradient(90deg, transparent, var(--red-bright), transparent);
            border-radius: 12px 12px 0 0;
        }
        .ai-title { color: var(--red-bright); font-size: 1.2rem; margin-bottom: 1rem; text-align: center; }
        .ai-summary-card {
            margin-bottom: 1rem; padding: 0.85rem 0.9rem; border: 1px solid var(--gold-border);
            border-radius: 10px; background: linear-gradient(135deg, var(--gold-tint), transparent);
            animation: ai-rise 0.34s cubic-bezier(.2,.85,.25,1) both;
        }
        .ai-summary-kicker { font-size: 0.76rem; color: var(--text-dim); margin-bottom: 0.35rem; }
        .ai-summary-main { color: var(--gold-bright); line-height: 1.75; font-size: 0.95rem; }
        .ai-summary-pills { display: flex; flex-wrap: wrap; gap: 0.45rem; margin-top: 0.65rem; }
        .ai-summary-pill {
            border: 1px solid var(--border); border-radius: 999px; padding: 0.25rem 0.55rem;
            color: var(--text); background: var(--bg-card-hover); font-size: 0.75rem; line-height: 1.4;
        }
        .ai-progress-panel {
            margin-bottom: 1rem; padding: 0.85rem; border: 1px solid var(--border);
            border-radius: 10px; background: var(--gold-tint); animation: ai-rise 0.34s cubic-bezier(.2,.85,.25,1) both;
        }
        .ai-status-row { display: flex; justify-content: space-between; gap: 1rem; align-items: center; margin-bottom: 0.65rem; }
        .ai-status-text { color: var(--gold-bright); font-size: 0.86rem; }
        .ai-progress-percent { color: var(--text-dim); font-size: 0.75rem; font-variant-numeric: tabular-nums; }
        .ai-progress-track { height: 4px; background: var(--bg-dark); border-radius: 999px; overflow: hidden; }
        .ai-progress-fill {
            width: 0%; height: 100%; border-radius: 999px;
            background: linear-gradient(90deg, var(--gold-dim), var(--gold-bright));
            transition: width 0.45s cubic-bezier(.2,.85,.25,1);
        }
        .ai-step-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 0.45rem; margin-top: 0.75rem; }
        .ai-step {
            display: flex; align-items: center; gap: 0.4rem; min-height: 38px; padding: 0.35rem 0.45rem;
            border: 1px solid var(--border); border-radius: 8px; color: var(--text-dim);
            background: var(--bg-card); font-size: 0.74rem; transition: transform 0.22s ease, border-color 0.22s ease, color 0.22s ease, background 0.22s ease;
        }
        .ai-step-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--border); flex: 0 0 auto; }
        .ai-step[data-state="active"] { color: var(--gold-bright); border-color: var(--gold-border); background: var(--gold-tint); transform: translateY(-1px); }
        .ai-step[data-state="active"] .ai-step-dot { background: var(--gold-bright); animation: ai-pulse 1.35s ease-in-out infinite; }
        .ai-step[data-state="done"] { color: var(--gold); border-color: var(--gold-border); }
        .ai-step[data-state="done"] .ai-step-dot { background: var(--green); box-shadow: 0 0 0 3px rgba(74,154,74,0.12); }
        .ai-quality-note {
            margin: 0 0 1rem; padding: 0.75rem 0.85rem; border-radius: 9px;
            border: 1px solid var(--gold-border); background: var(--gold-tint); color: var(--text);
            font-size: 0.84rem; line-height: 1.65;
        }
        .ai-quality-note.error { border-color: var(--red-border); background: var(--red-tint); color: var(--red-bright); }
        .ai-quality-note.success { color: var(--gold-bright); }
        .ai-loading-card { color: var(--text-dim); font-size: 0.86rem; line-height: 1.7; }
        .ai-skeleton { display: grid; gap: 0.5rem; margin-top: 0.65rem; }
        .ai-skeleton-line {
            position: relative; height: 10px; overflow: hidden; border-radius: 999px;
            background: var(--bg-card-hover);
        }
        .ai-skeleton-line::after {
            content: ''; position: absolute; inset: 0;
            background: linear-gradient(90deg, transparent, var(--gold-tint-strong), transparent);
            transform: translateX(-100%); animation: ai-shimmer 1.35s ease-in-out infinite;
        }
        @keyframes ai-rise { from { opacity: 0; transform: translateY(8px) scale(0.985); } to { opacity: 1; transform: translateY(0) scale(1); } }
        @keyframes ai-pulse { 0%, 100% { box-shadow: 0 0 0 0 var(--gold-shadow); } 50% { box-shadow: 0 0 0 5px transparent; } }
        @keyframes ai-shimmer { to { transform: translateX(100%); } }

        /* Tab切换 */
        .ai-tabs {
            display: flex; gap: 0; margin-bottom: 1rem;
            border-bottom: 1px solid var(--border); overflow-x: auto;
        }
        .ai-tab {
            padding: 0.6rem 1rem; font-size: 0.85rem; color: var(--text-dim);
            cursor: pointer; border: 0; border-bottom: 2px solid transparent; transition: all 0.2s;
            white-space: nowrap; min-height: 44px; display: inline-flex; align-items: center;
            background: transparent; font-family: inherit;
        }
        .ai-tab.active { color: var(--gold-bright); border-bottom-color: var(--gold); }
        .ai-tab:hover { color: var(--gold); }
        .ai-tab:focus-visible { outline: 2px solid var(--gold-bright); outline-offset: -2px; }
        .ai-tab-content { display: none; }
        .ai-tab-content.active { display: block; }
        .ai-content { line-height: 1.9; color: var(--text); font-size: 0.95rem; min-height: 4rem; }
        .ai-para { margin: 0 0 0.55rem 0; }
        .ai-quote {
            margin: 0.5rem 0 0.85rem 0; padding: 0.45rem 0.75rem;
            background: var(--gold-tint); border-left: 3px solid var(--gold-dim);
            border-radius: 0 6px 6px 0; color: var(--gold);
            font-size: 0.86rem; font-style: italic; line-height: 1.8;
        }
        .ai-waiting { color: var(--text-dim); font-size: 0.85rem; }
        .ai-seal {
            display: none; margin: 1.4rem auto 0.2rem; width: 72px; height: 72px;
            border: 2px solid var(--red-border); border-radius: 8px;
            color: var(--red-bright); font-size: 0.95rem; font-weight: 700;
            align-items: center; justify-content: center; text-align: center;
            transform: rotate(-6deg); line-height: 1.35; letter-spacing: 0.15em;
            box-shadow: inset 0 0 12px var(--red-tint);
        }

        .ai-meta { display: flex; gap: 1rem; margin-top: 1rem; padding-top: 1rem; border-top: 1px solid var(--border); flex-wrap: wrap; }
        .ai-meta-item { font-size: 0.75rem; color: var(--text-dim); }
        .ai-meta-item span { color: var(--gold); }
        .ai-cursor { display: inline-block; width: 8px; height: 1.2em; background: var(--gold); animation: blink 1s infinite; vertical-align: text-bottom; }
        @keyframes blink { 0%, 50% { opacity: 1; } 51%, 100% { opacity: 0; } }

        /* 参考典籍 */
        .classics-section {
            background: var(--bg-card); border: 1px solid var(--border);
            border-radius: 12px; padding: 1.5rem; margin-bottom: 1.5rem;
        }
        .classics-title { font-size: 1rem; color: var(--gold); margin-bottom: 0.8rem; }
        .classics-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 0.6rem; }
        .classics-grid > * { min-width: 0; }
        .classic-item {
            background: var(--bg-dark); border: 1px solid var(--border);
            border-radius: 6px; padding: 0.6rem; text-align: center; transition: all 0.2s;
        }
        .classic-item:hover { border-color: var(--gold-dim); }
        .classic-name { font-size: 0.85rem; color: var(--gold-bright); font-weight: 700; margin-bottom: 0.2rem; }
        .classic-author { font-size: 0.7rem; color: var(--text-dim); }
        .classics-note { font-size: 0.75rem; color: var(--text-dim); margin-top: 0.6rem; line-height: 1.5; }

        /* 神煞标签 */
        .shensha-tags { display: flex; flex-wrap: wrap; gap: 0.5rem; margin-top: 0.5rem; }
        .shensha-tag {
            background: var(--gold-tint-strong); border: 1px solid var(--gold-border);
            border-radius: 4px; padding: 0.2rem 0.6rem; font-size: 0.8rem; color: var(--gold);
        }

        /* 付费弹窗 */
        .modal-overlay {
            display: flex; position: fixed; top: 0; left: 0; right: 0; bottom: 0;
            background: var(--modal-scrim); z-index: 200;
            justify-content: center; align-items: center;
            opacity: 0; visibility: hidden; pointer-events: none;
            transition: opacity 0.22s ease, visibility 0s linear 0.22s;
            will-change: opacity;
            padding: 1rem;
        }
        .modal-overlay.active { opacity: 1; visibility: visible; pointer-events: auto; transition-delay: 0s; }
        .modal {
            background: var(--bg-card); border: 1px solid var(--gold);
            border-radius: 12px; padding: 2rem; max-width: 400px; text-align: center;
            opacity: 0; transform: translateY(18px) scale(0.965);
            transition: opacity 0.24s ease, transform 0.34s cubic-bezier(.2,.9,.2,1);
            will-change: opacity, transform;
            box-shadow: 0 24px 70px rgba(0,0,0,0.28);
        }
        .modal-overlay.active .modal { opacity: 1; transform: translateY(0) scale(1); }
        .modal-title { color: var(--gold); font-size: 1.2rem; margin-bottom: 1rem; }
        .modal-text { color: var(--text); margin-bottom: 1.5rem; line-height: 1.6; }
        .modal-btn { background: linear-gradient(135deg, var(--gold-dim), var(--gold)); border: none; border-radius: 8px; padding: 0.8rem 2rem; color: var(--button-text); font-family: inherit; font-weight: 700; cursor: pointer; }

        /* 登录注册弹窗 */
        .auth-overlay { z-index: 220; }
        .auth-modal-card { width: min(400px, 100%); overflow: hidden; }
        .auth-flow {
            transform: translateX(0); opacity: 1;
            transition: opacity 0.18s ease, transform 0.22s cubic-bezier(.2,.85,.25,1);
        }
        .auth-flow.is-leaving { opacity: 0; transform: translateX(var(--auth-slide-out, -10px)); }
        .auth-flow.is-entering { opacity: 0; transform: translateX(var(--auth-slide-in, 10px)); }
        .auth-field { display: block; margin-bottom: 0.78rem; text-align: left; }
        .auth-label { display: block; margin-bottom: 0.32rem; color: var(--text-dim); font-size: 0.78rem; }
        .auth-input {
            background: var(--bg-dark); border: 1px solid var(--border); border-radius: 8px;
            padding: 0.7rem; color: var(--text); font-family: inherit; font-size: 1rem;
            width: 100%; box-sizing: border-box; margin-bottom: 0; min-height: 44px;
        }
        .auth-input:focus { outline: none; border-color: var(--gold); }
        .auth-invite-field {
            max-height: 0; opacity: 0; overflow: hidden; margin-bottom: 0;
            transform: translateY(-6px);
            transition: max-height 0.28s cubic-bezier(.2,.85,.25,1), opacity 0.18s ease, transform 0.22s ease, margin-bottom 0.22s ease;
        }
        .auth-invite-field.visible { max-height: 92px; opacity: 1; margin-bottom: 0.78rem; transform: translateY(0); }
        .auth-help {
            color: var(--text-dim); font-size: 0.76rem; line-height: 1.55; text-align: left;
            max-height: 0; opacity: 0; overflow: hidden; margin-bottom: 0;
            transition: max-height 0.24s ease, opacity 0.18s ease, margin-bottom 0.2s ease;
        }
        .auth-help.visible { max-height: 44px; opacity: 1; margin-bottom: 0.8rem; }
        .auth-error { color: var(--red-bright); font-size: 0.85rem; margin-bottom: 0.8rem; min-height: 1.2rem; }
        .auth-switch { color: var(--gold-dim); font-size: 0.85rem; cursor: pointer; text-decoration: underline; margin-top: 0.5rem; display: block; }
        .auth-btn { width: 100%; box-sizing: border-box; }
        .user-badge { font-size: 0.8rem; color: var(--gold-bright); cursor: pointer; min-height: 44px; display: inline-flex; align-items: center; }

        /* 历史记录 */
        .history-section {
            background: var(--bg-card); border: 1px solid var(--border);
            border-radius: 12px; padding: 1.5rem; margin-bottom: 2rem; display: none;
        }
        .history-section.active { display: block; }
        .history-title { font-size: 1rem; color: var(--gold); margin-bottom: 1rem; display: flex; align-items: center; justify-content: space-between; }
        .history-list { display: flex; flex-direction: column; gap: 0.6rem; }
        .history-item {
            display: flex; align-items: center; justify-content: space-between;
            background: var(--bg-dark); border: 1px solid var(--border);
            border-radius: 8px; padding: 0.8rem 1rem; cursor: pointer; transition: all 0.2s;
        }
        .history-item:hover { border-color: var(--gold); background: var(--bg-card-hover); }
        .history-item-info { display: flex; align-items: center; gap: 0.8rem; flex: 1; }
        .history-item-name { font-weight: 700; color: var(--gold-bright); font-size: 0.95rem; }
        .history-item-date { font-size: 0.8rem; color: var(--text-dim); }
        .history-item-tag { font-size: 0.75rem; color: var(--gold-dim); border: 1px solid var(--gold-dim); border-radius: 4px; padding: 0.1rem 0.4rem; }
        .history-item-ai { font-size: 0.7rem; color: var(--red-bright); }
        .history-delete {
            background: none; border: none; color: var(--text-dim);
            cursor: pointer; font-size: 1rem; padding: 0.2rem 0.4rem; border-radius: 4px;
        }
        .history-delete:hover { color: var(--red-bright); }
        .history-empty { text-align: center; color: var(--text-dim); padding: 1rem; font-size: 0.9rem; }

        /* 演算动画遮罩 */
        .divine-overlay {
            display: none; position: fixed; top: 0; left: 0; right: 0; bottom: 0;
            background: var(--overlay-bg); z-index: 300;
            justify-content: center; align-items: center; flex-direction: column;
            backdrop-filter: blur(8px);
        }
        .divine-overlay.active { display: flex; }
        .divine-overlay.fade-out { opacity: 0; transition: opacity 0.5s; pointer-events: none; }

        .divine-taiji {
            width: 120px; height: 120px; border-radius: 50%;
            background: linear-gradient(135deg, var(--text) 50%, var(--bg-dark) 50%);
            border: 3px solid var(--gold);
            position: relative; z-index: 1;
            animation: divine-spin 1.2s linear infinite;
            box-shadow: 0 0 40px var(--gold-shadow);
        }
        .divine-taiji::before {
            content: ''; position: absolute; top: 25%; left: 50%;
            width: 60px; height: 60px; border-radius: 50%;
            background: var(--bg-dark);
            transform: translateX(-50%);
        }
        .divine-taiji::after {
            content: ''; position: absolute; bottom: 25%; left: 50%;
            width: 60px; height: 60px; border-radius: 50%;
            background: var(--text);
            transform: translateX(-50%);
        }
        .divine-taiji-inner {
            position: absolute; top: 50%; left: 50%; transform: translate(-50%,-50%);
            width: 100%; height: 100%; display: flex; align-items: center; justify-content: center;
            z-index: 2; font-size: 2rem; color: var(--gold-bright);
            text-shadow: 0 0 20px var(--gold-shadow);
        }
        @keyframes divine-spin { to { transform: rotate(360deg); } }

        .divine-steps {
            margin-top: 2rem; text-align: center;
            font-size: 1.1rem; color: var(--gold); letter-spacing: 0.1em;
            min-height: 2rem; transition: opacity 0.3s;
            position: relative; z-index: 1;
        }
        .divine-steps .step-done { color: var(--text-dim); text-decoration: line-through; opacity: 0.5; }
        .divine-progress {
            margin-top: 1rem; width: 240px; height: 2px;
            background: var(--border); border-radius: 1px; overflow: hidden;
            position: relative; z-index: 1;
        }
        .divine-progress-fill {
            height: 100%; width: 0%;
            background: linear-gradient(90deg, var(--gold-dim), var(--gold-bright));
            transition: width 0.3s ease;
        }
        .divine-ring {
            position: absolute; top: 50%; left: 50%;
            margin-top: -100px; margin-left: -100px;
            width: 200px; height: 200px;
            border: 1px solid var(--gold-tint-strong); border-radius: 50%;
            animation: ring-pulse 2s ease-in-out infinite;
            z-index: 0;
        }
        .divine-ring:nth-child(2) { width: 280px; height: 280px; margin-top: -140px; margin-left: -140px; animation-delay: 0.5s; }
        @keyframes ring-pulse {
            0%, 100% { transform: scale(1); opacity: 0.3; }
            50% { transform: scale(1.1); opacity: 0.1; }
        }

        /* 桌面端放大 - 使用clamp实现平滑过渡，避免任何宽度下溢出 */
        @media (min-width: 601px) {
            .container { padding: clamp(0.75rem, 3vw, 2rem); }
            .input-card { padding: clamp(1rem, 3vw, 2rem); margin-bottom: clamp(1rem, 2vw, 2rem); }
            .ai-section { padding: clamp(1rem, 3vw, 2rem); }
            .header { padding: clamp(0.5rem, 2vw, 1rem) clamp(0.75rem, 3vw, 2rem); padding-top: clamp(0.5rem, 2vw, 1rem); }
            .logo { gap: clamp(0.5rem, 1vw, 0.8rem); }
            .logo-taiji { width: clamp(28px, 4vw, 36px); height: clamp(28px, 4vw, 36px); }
            .logo-text { font-size: clamp(1.1rem, 2vw, 1.5rem); }
            .header-sub { font-size: clamp(0.7rem, 1vw, 0.85rem); }
            .quota-badge { padding: clamp(0.2rem, 0.5vw, 0.3rem) clamp(0.5rem, 1vw, 0.8rem); font-size: clamp(0.7rem, 1vw, 0.8rem); }
            .card-title { font-size: clamp(1.1rem, 1.5vw, 1.2rem); margin-bottom: clamp(0.75rem, 1.5vw, 1.5rem); }
            .form-grid { gap: clamp(0.6rem, 1vw, 1rem); }
            .form-group { gap: clamp(0.3rem, 0.5vw, 0.4rem); }
            .form-group label { font-size: clamp(0.8rem, 0.8vw, 0.85rem); }
            .form-group input, .form-group select { padding: clamp(0.65rem, 0.8vw, 0.7rem) clamp(0.5rem, 1vw, 1rem); min-height: auto; }
            .btn-divine { padding: clamp(0.8rem, 1vw, 1rem); font-size: clamp(1.05rem, 1vw, 1.1rem); margin-top: clamp(0.75rem, 1.5vw, 1.5rem); min-height: 50px; }
            .info-grid { grid-template-columns: repeat(2, 1fr); }
        }

        /* 移动端动画优化 */
        @media (max-width: 600px) {
            .header { gap: 0.5rem; }
            .header-actions { gap: 0.45rem; }
            .logo-text { letter-spacing: 0.1em; }
            .header-sub { font-size: 0.65rem; }
            .user-badge, .quota-badge {
                max-width: 6.8rem; overflow: hidden; text-overflow: ellipsis;
            }
            .ai-step-grid { grid-template-columns: repeat(2, 1fr); }
            .ai-status-row { align-items: flex-start; }
            .divine-taiji { width: 90px; height: 90px; }
            .divine-taiji::before, .divine-taiji::after { width: 45px; height: 45px; }
            .divine-taiji-inner { font-size: 1.5rem; }
            .divine-steps { font-size: 0.95rem; margin-top: 1.5rem; }
            .divine-progress { width: 180px; }
            .divine-ring { width: 160px; height: 160px; margin-top: -80px; margin-left: -80px; }
            .divine-ring:nth-child(2) { width: 220px; height: 220px; margin-top: -110px; margin-left: -110px; }
            .birth-picker-overlay { align-items: flex-end; padding: 0 0 env(safe-area-inset-bottom, 0px); }
            .birth-picker-sheet {
                max-width: none; max-height: 64dvh;
                border-radius: 18px 18px 0 0; border-bottom: 0;
                padding: 0.75rem 0.55rem calc(0.85rem + env(safe-area-inset-bottom, 0px));
                animation-name: birth-sheet-up;
            }
            .birth-picker-grid { grid-template-columns: 1.25fr repeat(4, minmax(0, 1fr)); gap: 0.18rem; }
            .birth-wheel-label { font-size: 0.68rem; }
            .birth-wheel-item { font-size: 0.88rem; }
            .birth-picker-value { font-size: 0.95rem; }
            .dao-sigil { width: 225px; opacity: 0.24; }
            .dao-sigil-a { left: -102px; top: 112px; }
            .dao-sigil-b { right: -106px; bottom: 32px; }
            .dao-talisman { width: 54px; min-height: 170px; opacity: 0.16; }
            .dao-talisman-a { right: 1.4rem; top: 42vh; }
            .dao-talisman-b { left: 0.8rem; bottom: 8vh; }
            .scripture-flow { font-size: 0.56rem; max-width: 8.6rem; opacity: 0.15; letter-spacing: 0.12em; }
            .scripture-b, .scripture-e, .scripture-i { display: none; }
            .auth-overlay { align-items: flex-end; padding: 0; }
            .auth-modal-card {
                width: 100%; max-width: none; border-radius: 18px 18px 0 0; border-bottom: 0;
                padding: 1.4rem 1.1rem calc(1.2rem + env(safe-area-inset-bottom, 0px));
                transform: translateY(28px) scale(1);
            }
            .auth-overlay.active .auth-modal-card { transform: translateY(0) scale(1); }
        }
        @keyframes birth-sheet-up { from { opacity: 0; transform: translateY(16px); } to { opacity: 1; transform: translateY(0); } }
        @media (prefers-reduced-motion: reduce) {
            .ai-summary-card, .ai-progress-panel, .ai-step, .ai-progress-fill,
            .ai-step[data-state="active"] .ai-step-dot, .ai-skeleton-line::after,
            .birth-picker-trigger, .birth-picker-sheet, .birth-wheel-item,
            .modal-overlay, .modal, .auth-flow,
            body::before, body::after,
            .ambient-bg, .ambient-bg * {
                animation: none; transition: none;
            }
        }
    </style>
</head>
<body>
    <div class="ambient-bg" aria-hidden="true">
        <div class="dao-sigil dao-sigil-a">
            <span class="dao-trigram">乾</span><span class="dao-trigram">兑</span><span class="dao-trigram">离</span><span class="dao-trigram">震</span>
            <span class="dao-trigram">巽</span><span class="dao-trigram">坎</span><span class="dao-trigram">艮</span><span class="dao-trigram">坤</span>
        </div>
        <div class="dao-sigil dao-sigil-b">
            <span class="dao-trigram">乾</span><span class="dao-trigram">兑</span><span class="dao-trigram">离</span><span class="dao-trigram">震</span>
            <span class="dao-trigram">巽</span><span class="dao-trigram">坎</span><span class="dao-trigram">艮</span><span class="dao-trigram">坤</span>
        </div>
        <div class="dao-talisman dao-talisman-a"><span>太上</span><span>玄门</span><span>敕令</span></div>
        <div class="dao-talisman dao-talisman-b"><span>阴阳</span><span>五行</span><span>归藏</span></div>
        <div class="scripture-flow scripture-a">道可道，非常道。名可名，非常名。</div>
        <div class="scripture-flow scripture-b">天地定位，山泽通气，雷风相薄。</div>
        <div class="scripture-flow scripture-c">甲乙丙丁戊己庚辛，壬癸循环。</div>
        <div class="scripture-flow scripture-d">乾坤坎离，震巽艮兑，阴阳消息。</div>
        <div class="scripture-flow scripture-e">一阴一阳之谓道，顺逆之间见机。</div>
        <div class="scripture-flow scripture-f">子丑寅卯，辰巳午未，申酉戌亥。</div>
        <div class="scripture-flow scripture-g">天干地支，各循其时。</div>
        <div class="scripture-flow scripture-h">观象玩辞，知来藏往。</div>
        <div class="scripture-flow scripture-i">河图洛书，数起中宫。</div>
        <div class="scripture-flow scripture-j">三元九运，气随象转。</div>
    </div>
    <div class="header">
        <div class="logo">
            <div class="logo-taiji"></div>
            <div>
                <div class="logo-text">玄机阁</div>
                <div class="header-sub">八字排盘 · AI解读</div>
            </div>
        </div>
        <div class="header-actions">
            <button class="theme-toggle" id="themeToggle" type="button" onclick="toggleTheme()" aria-label="切换为浅色模式" title="切换为浅色模式">
                <span class="theme-toggle-icon" aria-hidden="true"></span>
            </button>
            <span class="user-badge" id="userBadge" onclick="showAuthModal()" style="display:none;"></span>
            <div class="quota-badge" id="quotaBadge" onclick="showAuthModal('login')">登录领5次解读</div>
        </div>
    </div>

    <div class="container">
        <div class="input-card">
            <div class="card-title">输入生辰信息</div>
            <div class="form-grid">
                <div class="form-group" style="grid-column: 1 / -1;"><label>姓名/备注（选填）</label><input type="text" id="name" placeholder="如：张三"></div>
                <div class="form-group" style="grid-column: 1 / -1;">
                    <label>出生时间</label>
                    <button class="birth-picker-trigger is-empty" id="birthPickerTrigger" type="button" onclick="openBirthPicker()" aria-haspopup="dialog" aria-label="选择出生年月日时分">
                        <span class="birth-picker-value" id="birthPickerText">请选择出生时间</span>
                        <span class="birth-picker-hint">滚动/拖动</span>
                    </button>
                    <input type="hidden" id="year">
                    <input type="hidden" id="month">
                    <input type="hidden" id="day">
                    <input type="hidden" id="hour">
                    <input type="hidden" id="minute">
                </div>
                <div class="form-group" style="grid-column: 1 / -1;"><label>性别</label><select id="gender"><option value="male">男</option><option value="female">女</option></select></div>
            </div>
            <button class="btn-divine" id="divineBtn" onclick="divine()">排盘推演</button>
        </div>

        <!-- 历史记录 -->
        <div class="history-section" id="historySection">
            <div class="history-title">
                <span>历史记录</span>
                <span style="font-size:0.8rem;color:var(--text-dim);cursor:pointer;" onclick="loadHistory()">刷新</span>
            </div>
            <div class="history-list" id="historyList"></div>
        </div>

        <div class="result-section" id="resultSection">
            <div class="info-grid">
                <div class="info-card"><div class="info-card-title">公历</div><div class="info-card-value" id="r_solar"></div></div>
                <div class="info-card"><div class="info-card-title">农历</div><div class="info-card-value" id="r_lunar"></div></div>
                <div class="info-card"><div class="info-card-title">日主</div><div class="info-card-value" id="r_daymaster"></div></div>
                <div class="info-card"><div class="info-card-title">格局</div><div class="info-card-value" id="r_geju"></div></div>
                <div class="info-card"><div class="info-card-title">身旺/身弱</div><div class="info-card-value" id="r_shenwang"></div></div>
                <div class="info-card"><div class="info-card-title">起运岁数</div><div class="info-card-value" id="r_qiyun"></div></div>
            </div>

            <div class="bazi-table">
                <table>
                    <tr><th></th><th>年柱</th><th>月柱</th><th>日柱</th><th>时柱</th></tr>
                    <tr><td>天干</td><td class="bazi-gan-zhi" id="r_yg"></td><td class="bazi-gan-zhi" id="r_mg"></td><td class="bazi-gan-zhi" id="r_dg"></td><td class="bazi-gan-zhi" id="r_hg"></td></tr>
                    <tr><td>地支</td><td class="bazi-gan-zhi" id="r_yz"></td><td class="bazi-gan-zhi" id="r_mz"></td><td class="bazi-gan-zhi" id="r_dz"></td><td class="bazi-gan-zhi" id="r_hz"></td></tr>
                    <tr><td>十神</td><td id="r_yss"></td><td id="r_mss"></td><td>日主</td><td id="r_hss"></td></tr>
                    <tr><td>纳音</td><td id="r_yn"></td><td id="r_mn"></td><td id="r_dn"></td><td id="r_hn"></td></tr>
                    <tr><td>藏干</td><td id="r_ycg"></td><td id="r_mcg"></td><td id="r_dcg"></td><td id="r_hcg"></td></tr>
                </table>
            </div>

            <div class="info-grid">
                <div class="info-card"><div class="info-card-title">五行分布</div><div id="r_wuxing_bars"></div></div>
                <div class="info-card"><div class="info-card-title">喜用神 / 忌神</div><div class="info-card-value" id="r_xiyong"></div><div class="info-card-detail" id="r_jishen"></div></div>
            </div>

            <div class="info-card" style="margin-bottom: 1.5rem;">
                <div class="info-card-title">神煞</div><div class="shensha-tags" id="r_shensha"></div>
            </div>
            <div class="info-card" style="margin-bottom: 1.5rem;">
                <div class="info-card-title">地支关系</div><div class="info-card-detail" id="r_zhi_rel"></div>
            </div>

            <div class="dayun-table">
                <table><tr><th>年龄</th><th>大运</th><th>天干十神</th><th>地支十神</th><th>纳音</th></tr><tbody id="r_dayun"></tbody></table>
            </div>

            <!-- 参考典籍 -->
            <div class="classics-section">
                <div class="classics-title">命理依据 · 参考典籍</div>
                <div class="classics-grid">
                    <div class="classic-item"><div class="classic-name">滴天髓</div><div class="classic-author">京图·刘伯温注</div></div>
                    <div class="classic-item"><div class="classic-name">子平真诠</div><div class="classic-author">沈孝瞻</div></div>
                    <div class="classic-item"><div class="classic-name">穷通宝鉴</div><div class="classic-author">余春台</div></div>
                    <div class="classic-item"><div class="classic-name">三命通会</div><div class="classic-author">万民英</div></div>
                    <div class="classic-item"><div class="classic-name">渊海子平</div><div class="classic-author">徐子升</div></div>
                    <div class="classic-item"><div class="classic-name">神峰通考</div><div class="classic-author">张神峰</div></div>
                </div>
                <div class="classics-note">排盘基于寿星天文历(sxtwl)精确计算，以立春分年、节气分月。十神、格局、神煞依据《子平真诠》取格法，大运推排遵循《滴天髓》阳顺阴逆之理。AI解读引用上述典籍原文，辅助理解命盘。</div>
            </div>

            <div class="ai-section">
                <div class="ai-title">道长解读</div>
                <button class="btn-divine" id="aiGetBtn" style="display:none; margin-bottom:1rem; padding:0.6rem; font-size:0.95rem;" onclick="getAIReading(currentResultData)">请道长开示</button>
                <div class="ai-summary-card" id="aiSummaryCard" style="display:none;"></div>
                <div class="ai-progress-panel" id="aiProgressPanel" style="display:none;" aria-live="polite">
                    <div class="ai-status-row">
                        <div class="ai-status-text" id="aiStatusText">正在展卷校验命盘...</div>
                        <div class="ai-progress-percent" id="aiProgressPercent">0%</div>
                    </div>
                    <div class="ai-progress-track"><div class="ai-progress-fill" id="aiProgressFill"></div></div>
                    <div class="ai-step-grid" id="aiProgressSteps"></div>
                </div>
                <div class="ai-quality-note" id="aiQualityNote" style="display:none;" role="status"></div>
                <div class="ai-tabs" id="aiTabs" style="display:none;" role="tablist">
                    <button class="ai-tab active" type="button" role="tab" aria-selected="true" onclick="switchTab('性格', this)">性格</button>
                    <button class="ai-tab" type="button" role="tab" aria-selected="false" onclick="switchTab('财运', this)">财运</button>
                    <button class="ai-tab" type="button" role="tab" aria-selected="false" onclick="switchTab('婚姻', this)">婚姻</button>
                    <button class="ai-tab" type="button" role="tab" aria-selected="false" onclick="switchTab('健康', this)">健康</button>
                    <button class="ai-tab" type="button" role="tab" aria-selected="false" onclick="switchTab('大运', this)">大运</button>
                    <button class="ai-tab" type="button" role="tab" aria-selected="false" onclick="switchTab('总评', this)">总评</button>
                </div>
                <div class="ai-tab-content active" id="tab_性格"><div class="ai-content" id="ai_性格"></div></div>
                <div class="ai-tab-content" id="tab_财运"><div class="ai-content" id="ai_财运"></div></div>
                <div class="ai-tab-content" id="tab_婚姻"><div class="ai-content" id="ai_婚姻"></div></div>
                <div class="ai-tab-content" id="tab_健康"><div class="ai-content" id="ai_健康"></div></div>
                <div class="ai-tab-content" id="tab_大运"><div class="ai-content" id="ai_大运"></div></div>
                <div class="ai-tab-content" id="tab_总评"><div class="ai-content" id="ai_总评"></div></div>
                <div class="ai-seal" id="aiSeal">玄机<br>阁印</div>
                <div class="ai-meta" id="r_ai_meta" style="display:none;">
                    <div class="ai-meta-item" id="r_source_tag" style="display:none;">引据典籍: <span></span></div>
                </div>
            </div>
        </div>
    </div>

    <!-- 演算动画遮罩 -->
    <div class="divine-overlay" id="divineOverlay">
        <div class="divine-ring"></div>
        <div class="divine-ring"></div>
        <div class="divine-taiji"><div class="divine-taiji-inner">☯</div></div>
        <div class="divine-steps" id="divineSteps">推演天干地支...</div>
        <div class="divine-progress"><div class="divine-progress-fill" id="divineProgress"></div></div>
    </div>

    <!-- 付费弹窗 -->
    <div class="modal-overlay" id="rechargeModal">
        <div class="modal">
            <div class="modal-title">免费次数已用完</div>
            <div class="modal-text">您的5次免费AI解读机会已用完。<br>八字排盘功能永久免费，AI解读充值通道即将开放，<br>感谢您体验玄机阁。</div>
            <button class="modal-btn" onclick="document.getElementById('rechargeModal').classList.remove('active')">知道了</button>
        </div>
    </div>

    <!-- 登录注册弹窗 -->
    <div class="modal-overlay auth-overlay" id="authModal" role="dialog" aria-modal="true" aria-labelledby="authTitle" onclick="handleAuthBackdrop(event)">
        <div class="modal auth-modal-card" onclick="event.stopPropagation()">
            <div class="auth-flow" id="authFlow">
            <div class="modal-title" id="authTitle">登录</div>
            <label class="auth-field" for="authUsername">
                <span class="auth-label">用户名</span>
                <input type="text" class="auth-input" id="authUsername" placeholder="2-20字符" maxlength="20" autocomplete="username">
            </label>
            <label class="auth-field" for="authPassword">
                <span class="auth-label">密码</span>
                <input type="password" class="auth-input" id="authPassword" placeholder="至少4位" autocomplete="current-password" style="-webkit-text-security:disc;">
            </label>
            <label class="auth-field auth-invite-field" id="authInviteField" for="authInviteCode">
                <span class="auth-label">体验码</span>
                <input type="text" class="auth-input" id="authInviteCode" placeholder="请输入注册体验码" maxlength="48" autocomplete="off">
            </label>
            <div class="auth-help" id="authHelp">注册体验码用于控制测试名额，避免机器人批量注册消耗AI额度。</div>
            <div class="auth-error" id="authError"></div>
            <button class="modal-btn auth-btn" id="authSubmitBtn" onclick="handleAuth()">登录</button>
            <span class="auth-switch" id="authSwitch" onclick="switchAuthMode()">没有账号？去注册</span>
            <span class="auth-switch" onclick="closeAuthModal()" style="margin-top:0.8rem;">取消</span>
            </div>
        </div>
    </div>

    <!-- 出生时间滚轮选择 -->
    <div class="birth-picker-overlay" id="birthPickerOverlay" role="dialog" aria-modal="true" aria-labelledby="birthPickerTitle" onclick="handleBirthPickerBackdrop(event)">
        <div class="birth-picker-sheet">
            <div class="birth-picker-toolbar">
                <button class="birth-picker-btn" type="button" onclick="closeBirthPicker(false)">取消</button>
                <div class="birth-picker-title" id="birthPickerTitle">选择出生时间</div>
                <button class="birth-picker-btn confirm" type="button" onclick="closeBirthPicker(true)">完成</button>
            </div>
            <div class="birth-picker-grid" id="birthPickerWheels"></div>
        </div>
    </div>

    <script>
        function getCurrentTheme() {
            return document.documentElement.dataset.theme === 'light' ? 'light' : 'dark';
        }

        function updateThemeToggle(theme) {
            const toggle = document.getElementById('themeToggle');
            const metaTheme = document.querySelector('meta[name="theme-color"]');
            const isLight = theme === 'light';
            if (metaTheme) {
                metaTheme.setAttribute('content', isLight ? '#f6eedf' : '#0a0a0f');
            }
            if (toggle) {
                const label = isLight ? '切换为深色模式' : '切换为浅色模式';
                toggle.setAttribute('aria-label', label);
                toggle.setAttribute('title', label);
            }
        }

        function setTheme(theme) {
            const nextTheme = theme === 'light' ? 'light' : 'dark';
            document.documentElement.dataset.theme = nextTheme;
            try {
                localStorage.setItem('xjg_theme', nextTheme);
            } catch (e) {}
            updateThemeToggle(nextTheme);
        }

        function toggleTheme() {
            setTheme(getCurrentTheme() === 'light' ? 'dark' : 'light');
        }

        document.addEventListener('DOMContentLoaded', function() {
            updateThemeToggle(getCurrentTheme());
        });

        // === 客户端唯一ID（存 localStorage，跨会话稳定，不随IP变化） ===
        function getClientId() {
            let cid = localStorage.getItem('xjg_client_id');
            if (!cid) {
                cid = 'c' + Date.now() + Math.random().toString(36).slice(2, 10);
                localStorage.setItem('xjg_client_id', cid);
            }
            return cid;
        }
        const CLIENT_ID = getClientId();

        // 统一带 client_id 的 fetch 封装
        function apiFetch(url, opts) {
            opts = opts || {};
            opts.headers = Object.assign({}, opts.headers || {}, {
                'X-Client-Id': CLIENT_ID
            });
            // 带上登录token
            const token = localStorage.getItem('xjg_auth_token');
            if (token) {
                opts.headers['Authorization'] = 'Bearer ' + token;
            }
            return fetch(url, opts);
        }

        let currentResultData = null;  // 全局变量，保存当前排盘结果
        let isLoggedIn = false;        // 登录状态
        let lastQuota = null;          // 最近一次配额数据
        let pendingAIReading = null;   // 登录成功后待续的解读请求
        let authInviteRequired = false; // 后端配置决定注册是否需要体验码

        async function loadAuthConfig() {
            try {
                const res = await apiFetch('/api/auth-config');
                const data = await res.json();
                authInviteRequired = !!data.invite_required;
                updateAuthUI();
            } catch(e) {}
        }

        // 命理典籍库（用于展示引据来源）
        const CLASSIC_BOOKS = ['滴天髓','子平真诠','穷通宝鉴','三命通会','渊海子平','神峰通考','命理约言','李虚中命书','黄金策','黄帝内经'];
        const AI_TABS = ['性格','财运','婚姻','健康','大运','总评'];
        const AI_WAIT_MESSAGES = [
            '正在校验四柱与月令...',
            '正在对照十神与藏干...',
            '正在审当前大运落点...',
            '正在整理六个解读板块...',
            '正在润色为可读建议...'
        ];
        let aiUserSelectedTab = false;
        let aiWaitTimer = null;

        function escapeHtml(s) {
            return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
        }

        function valueText(value, fallback) {
            if (fallback === undefined) fallback = '无';
            if (Array.isArray(value)) return value.length ? value.join('、') : fallback;
            if (value === null || value === undefined) return fallback;
            const text = String(value).trim();
            return text || fallback;
        }

        function getCurrentDayun(data) {
            if (!data || !data.solar_date || !Array.isArray(data.dayun)) return null;
            const match = String(data.solar_date).match(/(\d+)年/);
            if (!match) return null;
            const currentAge = new Date().getFullYear() - parseInt(match[1], 10);
            for (const dy of data.dayun) {
                if (currentAge >= dy.start_age && currentAge <= dy.end_age) return dy;
            }
            return null;
        }

        function getTopWuxing(data) {
            const pct = data && data.wuxing_percent ? data.wuxing_percent : {};
            return ['金','木','水','火','土']
                .map(wx => ({wx, value: Number(pct[wx] || 0)}))
                .sort((a, b) => b.value - a.value)
                .slice(0, 2)
                .map(item => item.wx + item.value + '%')
                .join('、') || '五行待校验';
        }

        function showAISummary(data) {
            const el = document.getElementById('aiSummaryCard');
            if (!el || !data) return;
            const currentDy = getCurrentDayun(data);
            const dayunText = currentDy ? currentDy.gan + currentDy.zhi + '运（' + currentDy.start_age + '-' + currentDy.end_age + '岁）' : '当前大运待校验';
            const main = valueText(data.day_master) + '日主 · ' + valueText(data.shenwang) + ' · ' + valueText(data.geju) + '。本地排盘已完成，AI将围绕月令、十神、五行与' + dayunText + '展开。';
            const pills = [
                '五行偏向：' + getTopWuxing(data),
                '喜用：' + valueText(data.xiyong),
                '忌神：' + valueText(data.jishen),
                '当前：' + dayunText
            ];
            el.innerHTML = '<div class="ai-summary-kicker">命盘摘要 · 先看结论脉络</div>'
                + '<div class="ai-summary-main">' + escapeHtml(main) + '</div>'
                + '<div class="ai-summary-pills">' + pills.map(p => '<span class="ai-summary-pill">' + escapeHtml(p) + '</span>').join('') + '</div>';
            el.style.display = 'block';
        }

        function setAIQualityNote(message, type, details) {
            const el = document.getElementById('aiQualityNote');
            if (!el) return;
            if (!message) {
                el.style.display = 'none';
                el.innerHTML = '';
                return;
            }
            const detailList = Array.isArray(details) ? details : (details ? [details] : []);
            const extra = detailList.length ? '<br><span>' + detailList.map(escapeHtml).join('；') + '</span>' : '';
            el.className = 'ai-quality-note' + (type ? ' ' + type : '');
            el.innerHTML = escapeHtml(message) + extra;
            el.style.display = 'block';
        }

        function setAIStatus(text, percent) {
            const statusEl = document.getElementById('aiStatusText');
            const percentEl = document.getElementById('aiProgressPercent');
            const fillEl = document.getElementById('aiProgressFill');
            if (statusEl) statusEl.textContent = text;
            if (percentEl) percentEl.textContent = Math.max(0, Math.min(100, percent)) + '%';
            if (fillEl) fillEl.style.width = Math.max(0, Math.min(100, percent)) + '%';
        }

        function initAIProgress(tabs) {
            const panel = document.getElementById('aiProgressPanel');
            const steps = document.getElementById('aiProgressSteps');
            if (!panel || !steps) return;
            steps.innerHTML = tabs.map(t => '<div class="ai-step" data-ai-step="' + t + '" data-state="pending"><span class="ai-step-dot"></span><span>' + t + '</span></div>').join('');
            panel.style.display = 'block';
            setAIStatus('正在展卷校验命盘...', 6);
        }

        function updateAIProgress(tabContents, tabs, done) {
            let currentIndex = -1;
            for (let i = 0; i < tabs.length; i++) {
                if (tabContents[tabs[i]] && tabContents[tabs[i]].trim()) currentIndex = i;
            }
            const percent = done ? 100 : Math.min(92, Math.round(((Math.max(currentIndex, 0) + (currentIndex >= 0 ? 0.45 : 0.15)) / tabs.length) * 100));
            const activeName = currentIndex >= 0 ? tabs[currentIndex] : tabs[0];
            tabs.forEach((t, i) => {
                const step = document.querySelector('[data-ai-step="' + t + '"]');
                if (!step) return;
                const state = done || (currentIndex >= 0 && i < currentIndex) ? 'done' : (i === Math.max(currentIndex, 0) ? 'active' : 'pending');
                step.setAttribute('data-state', state);
            });
            setAIStatus(done ? '六个板块已完成，正在落印归档。' : '正在推演【' + activeName + '】...', percent);
        }

        function clearAIWaitTimer() {
            if (aiWaitTimer) {
                clearInterval(aiWaitTimer);
                aiWaitTimer = null;
            }
        }

        function startAIWaitTimer() {
            clearAIWaitTimer();
            let idx = 0;
            aiWaitTimer = setInterval(() => {
                setAIStatus(AI_WAIT_MESSAGES[idx % AI_WAIT_MESSAGES.length], Math.min(88, 10 + idx * 6));
                idx += 1;
            }, 1500);
        }

        function getActiveAITab() {
            const active = document.querySelector('.ai-tab.active');
            return active ? active.textContent.trim() : '性格';
        }

        function aiPlaceholder(name) {
            return '<div class="ai-loading-card">【' + escapeHtml(name) + '】正在等候推演...'
                + '<div class="ai-skeleton"><div class="ai-skeleton-line"></div><div class="ai-skeleton-line" style="width:86%;"></div><div class="ai-skeleton-line" style="width:64%;"></div></div></div>';
        }

        function renderAISections(tabContents, tabs, done) {
            const activeName = getActiveAITab();
            tabs.forEach(t => {
                const el = document.getElementById('ai_' + t);
                if (!el) return;
                const content = tabContents[t];
                if (content) {
                    el.innerHTML = formatInterpretation(content) + (!done && t === activeName ? '<span class="ai-cursor"></span>' : '');
                } else {
                    el.innerHTML = done ? '<span class="ai-waiting">此板块未生成完整内容。</span>' : aiPlaceholder(t);
                }
            });
        }

        function looksLikeCitation(seg) {
            for (const b of CLASSIC_BOOKS) { if (seg.indexOf(b) !== -1) return true; }
            // 流式输出中途：片段可能是书名开头（如"滴天"是"滴天髓"的前缀）
            if (seg.length >= 2) {
                for (const b of CLASSIC_BOOKS) { if (b.indexOf(seg) === 0) return true; }
            }
            return /云[：:]/.test(seg) || /曰[：:]/.test(seg);
        }

        // 把解读文本渲染成生动的排版：正文段落 + 古籍引用块
        function formatInterpretation(text) {
            if (!text) return '';
            const paras = String(text).split(/\n+/);
            let html = '';
            for (const para of paras) {
                const segs = para.split('——');
                for (let i = 0; i < segs.length; i++) {
                    const seg = segs[i].trim();
                    if (!seg) continue;
                    if (i > 0 && looksLikeCitation(seg)) {
                        html += '<div class="ai-quote">——' + escapeHtml(seg) + '</div>';
                    } else {
                        html += '<p class="ai-para">' + escapeHtml(seg) + '</p>';
                    }
                }
            }
            return html;
        }

        // 从解读文本中提炼实际引用过的典籍
        function detectClassics(text) {
            const found = [];
            for (const b of CLASSIC_BOOKS) {
                if (text && text.indexOf(b) !== -1) found.push(b);
            }
            return found;
        }

        // 根据登录/配额状态更新"请道长开示"按钮文案
        function updateAIGetBtn() {
            const btn = document.getElementById('aiGetBtn');
            if (!btn) return;
            if (isLoggedIn) {
                btn.textContent = (lastQuota && lastQuota.can_use === false) ? '免费次数已用完 · 点击查看' : '请道长开示';
            } else {
                btn.textContent = '登录领5次免费解读';
            }
        }

        // === 账号登录注册 ===
        let authMode = 'login'; // 'login' or 'register'
        let authSwitchTimer = null;

        function showAuthModal(mode) {
            clearTimeout(authSwitchTimer);
            authMode = mode || 'login';
            document.getElementById('authError').textContent = '';
            document.getElementById('authUsername').value = '';
            document.getElementById('authPassword').value = '';
            document.getElementById('authInviteCode').value = '';
            document.getElementById('authFlow').classList.remove('is-leaving', 'is-entering');
            updateAuthUI();
            document.getElementById('authModal').classList.add('active');
            requestAnimationFrame(() => {
                document.getElementById('authUsername').focus({preventScroll: true});
            });
        }

        function closeAuthModal() {
            document.getElementById('authModal').classList.remove('active');
        }

        function handleAuthBackdrop(event) {
            if (event.target && event.target.id === 'authModal') {
                closeAuthModal();
            }
        }

        function switchAuthMode() {
            const nextMode = authMode === 'login' ? 'register' : 'login';
            const flow = document.getElementById('authFlow');
            clearTimeout(authSwitchTimer);
            document.getElementById('authError').textContent = '';
            if (!flow || shouldReduceMotion()) {
                authMode = nextMode;
                updateAuthUI();
                return;
            }
            const direction = nextMode === 'register' ? 1 : -1;
            flow.style.setProperty('--auth-slide-out', (direction * -12) + 'px');
            flow.style.setProperty('--auth-slide-in', (direction * 12) + 'px');
            flow.classList.remove('is-entering');
            flow.classList.add('is-leaving');
            authSwitchTimer = setTimeout(() => {
                authMode = nextMode;
                updateAuthUI();
                flow.classList.remove('is-leaving');
                flow.classList.add('is-entering');
                requestAnimationFrame(() => {
                    flow.classList.remove('is-entering');
                    document.getElementById('authUsername').focus({preventScroll: true});
                });
            }, 150);
        }

        function updateAuthUI() {
            const isRegister = authMode === 'register';
            document.getElementById('authTitle').textContent = authMode === 'login' ? '登录' : '注册';
            document.getElementById('authSubmitBtn').textContent = authMode === 'login' ? '登录' : '注册';
            document.getElementById('authSwitch').textContent = authMode === 'login' ? '没有账号？去注册' : '已有账号？去登录';
            const inviteField = document.getElementById('authInviteField');
            const inviteInput = document.getElementById('authInviteCode');
            const help = document.getElementById('authHelp');
            const password = document.getElementById('authPassword');
            if (password) password.setAttribute('autocomplete', isRegister ? 'new-password' : 'current-password');
            if (inviteField) inviteField.classList.toggle('visible', isRegister && authInviteRequired);
            if (inviteInput) inviteInput.required = isRegister && authInviteRequired;
            if (help) help.classList.toggle('visible', isRegister && authInviteRequired);
        }

        async function handleAuth() {
            const username = document.getElementById('authUsername').value.trim();
            const password = document.getElementById('authPassword').value;
            const inviteCode = document.getElementById('authInviteCode').value.trim();
            const errEl = document.getElementById('authError');
            errEl.textContent = '';
            if (!username || !password) {
                errEl.textContent = '请填写用户名和密码';
                return;
            }
            if (authMode === 'register' && authInviteRequired && !inviteCode) {
                errEl.textContent = '请填写注册体验码';
                document.getElementById('authInviteCode').focus({preventScroll: true});
                return;
            }
            const btn = document.getElementById('authSubmitBtn');
            const oldText = btn.textContent;
            btn.textContent = '处理中...';
            btn.disabled = true;
            try {
                const endpoint = authMode === 'login' ? '/api/login' : '/api/register';
                const res = await fetch(endpoint, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json', 'X-Client-Id': CLIENT_ID},
                    body: JSON.stringify({username, password, invite_code: inviteCode, client_id: CLIENT_ID})
                });
                const data = await res.json();
                if (data.error) {
                    errEl.textContent = data.error;
                    btn.textContent = oldText;
                    btn.disabled = false;
                    return;
                }
                // 成功
                localStorage.setItem('xjg_auth_token', data.token);
                localStorage.setItem('xjg_username', data.username);
                closeAuthModal();
                updateQuota();
                loadHistory();  // 换了账号身份，历史记录也要切换
                btn.textContent = oldText;
                btn.disabled = false;
                // 登录前如果有待解读的命盘，自动继续请道长开示
                if (pendingAIReading) {
                    const pending = pendingAIReading;
                    pendingAIReading = null;
                    getAIReading(pending);
                }
            } catch(e) {
                errEl.textContent = '请求失败: ' + e.message;
                btn.textContent = oldText;
                btn.disabled = false;
            }
        }

        async function handleLogout() {
            const token = localStorage.getItem('xjg_auth_token');
            if (token) {
                await fetch('/api/logout', {method: 'POST', headers: {'Authorization': 'Bearer ' + token}});
            }
            localStorage.removeItem('xjg_auth_token');
            localStorage.removeItem('xjg_username');
            updateQuota();
            loadHistory();  // 退出后回到匿名身份的历史记录
        }

        // 查询配额
        async function updateQuota() {
            try {
                const res = await apiFetch('/api/quota');
                const data = await res.json();
                lastQuota = data;
                isLoggedIn = !!data.logged_in;
                const badge = document.getElementById('quotaBadge');
                const userBadge = document.getElementById('userBadge');
                if (data.logged_in) {
                    badge.textContent = `免费: ${data.free_remaining}/${data.free_total}`;
                    userBadge.textContent = data.username + ' | 退出';
                    userBadge.onclick = handleLogout;
                    userBadge.style.display = 'block';
                    badge.onclick = null;
                } else {
                    badge.textContent = '登录领5次解读';
                    userBadge.style.display = 'none';
                    badge.onclick = showAuthModal.bind(null, 'login');
                }
                updateAIGetBtn();
            } catch(e) {}
        }
        loadAuthConfig();
        updateQuota();

        // === 历史记录 ===
        async function loadHistory() {
            try {
                const res = await apiFetch('/api/history');
                const list = await res.json();
                const section = document.getElementById('historySection');
                const listEl = document.getElementById('historyList');

                if (list.length === 0) {
                    section.classList.remove('active');
                    return;
                }

                section.classList.add('active');
                listEl.innerHTML = list.map(item => {
                    const name = item.name ? item.name.replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;') : '未命名';
                    const gender = item.gender === 'male' ? '男' : '女';
                    const aiTag = item.has_ai ? '<span class="history-item-ai">已解读</span>' : '';
                    return `<div class="history-item" onclick="viewHistory(${item.id})">
                        <div class="history-item-info">
                            <span class="history-item-name">${name}</span>
                            <span class="history-item-date">${item.solar_date}</span>
                            <span class="history-item-tag">${gender}</span>
                            ${aiTag}
                        </div>
                        <button class="history-delete" onclick="deleteHistory(${item.id}, event)" title="删除">×</button>
                    </div>`;
                }).join('');
            } catch(e) {}
        }

        async function viewHistory(hid) {
            try {
                const res = await apiFetch('/api/history/' + hid);
                const detail = await res.json();
                if (detail.error) { alert(detail.error); return; }

                // 显示排盘结果
                displayResult(detail.paipan);

                // 清空AI解读区，显示获取按钮
                const tabs = AI_TABS;
                tabs.forEach(t => { document.getElementById('ai_' + t).textContent = ''; });
                document.getElementById('aiTabs').style.display = 'none';
                document.getElementById('aiProgressPanel').style.display = 'none';
                setAIQualityNote('', '');
                document.getElementById('r_ai_meta').style.display = 'none';
                document.getElementById('r_source_tag').style.display = 'none';
                document.getElementById('aiSeal').style.display = 'none';
                currentResultData = detail.paipan;
                updateAIGetBtn();
                document.getElementById('aiGetBtn').style.display = 'block';

                // 滚动到结果区
                document.getElementById('resultSection').scrollIntoView({behavior:'smooth'});
            } catch(e) {
                alert('加载失败: ' + e.message);
            }
        }

        // 独立的AI解读函数，可从排盘或历史记录调用
        async function getAIReading(paipanData) {
            const btn = document.getElementById('aiGetBtn');
            if (btn) { btn.style.display = 'none'; }

            const tabs = AI_TABS;
            aiUserSelectedTab = false;
            showAISummary(paipanData);
            setAIQualityNote('', '');
            initAIProgress(tabs);
            startAIWaitTimer();

            tabs.forEach(t => { document.getElementById('ai_' + t).innerHTML = ''; });
            document.getElementById('r_ai_meta').style.display = 'none';
            document.getElementById('r_source_tag').style.display = 'none';
            document.getElementById('aiSeal').style.display = 'none';
            document.getElementById('aiTabs').style.display = 'flex';
            switchTab('性格', null, false);
            let tabContents = {}; tabs.forEach(t => tabContents[t] = '');
            renderAISections(tabContents, tabs, false);

            try {
                const aiRes = await apiFetch('/api/interpret', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({paipan: paipanData})
                });

                if (aiRes.status === 401) {
                    const errData = await aiRes.json();
                    if (errData.need_login) {
                        pendingAIReading = paipanData;
                        setAIQualityNote('登录后即可请道长开示，新用户有5次免费解读。', 'error');
                        document.getElementById('ai_性格').textContent = '登录后即可继续生成。';
                        showAuthModal('login');
                    }
                    clearAIWaitTimer();
                    document.getElementById('aiProgressPanel').style.display = 'none';
                    updateAIGetBtn();
                    if (btn) { btn.style.display = 'block'; }
                    return;
                }

                if (aiRes.status === 403) {
                    const errData = await aiRes.json();
                    if (errData.need_recharge) {
                        document.getElementById('rechargeModal').classList.add('active');
                        setAIQualityNote('免费次数已用完，已生成过的命盘仍可回看。', 'error');
                        document.getElementById('ai_性格').textContent = '免费次数已用完。';
                    }
                    clearAIWaitTimer();
                    document.getElementById('aiProgressPanel').style.display = 'none';
                    updateAIGetBtn();
                    if (btn) { btn.style.display = 'block'; }
                    return;
                }

                if (!aiRes.ok || !aiRes.body) {
                    throw new Error('AI服务暂时没有返回可读取的流');
                }

                // SSE流式读取
                const reader = aiRes.body.getReader();
                const decoder = new TextDecoder();
                let fullText = '';
                let buffer = '';
                let currentTab = '性格';
                let hadAIError = false;

                while (true) {
                    const {done, value} = await reader.read();
                    if (done) break;
                    buffer += decoder.decode(value, {stream: true});

                    const lines = buffer.split('\n');
                    buffer = lines.pop();

                    for (const line of lines) {
                        if (!line.startsWith('data: ')) continue;
                        try {
                            const chunk = JSON.parse(line.slice(6));
                            if (chunk.status) {
                                clearAIWaitTimer();
                                setAIStatus(chunk.status, typeof chunk.progress === 'number' ? chunk.progress : 12);
                            }
                            if (chunk.text) {
                                clearAIWaitTimer();
                                fullText += chunk.text;
                                renderTabs(fullText, tabContents, tabs);
                                updateAIProgress(tabContents, tabs, false);
                                currentTab = getActiveAITab();
                                if (!aiUserSelectedTab) {
                                    let activeTab = currentTab;
                                    for (const t of tabs) {
                                        if (tabContents[t].length > 0) activeTab = t;
                                    }
                                    if (activeTab !== currentTab) {
                                        currentTab = activeTab;
                                        switchTab(currentTab, null, false);
                                    }
                                }
                                document.getElementById('aiTabs').style.display = 'flex';
                                renderAISections(tabContents, tabs, false);
                            }
                            if (chunk.done) {
                                clearAIWaitTimer();
                                renderTabs(fullText, tabContents, tabs);
                                updateAIProgress(tabContents, tabs, true);
                                renderAISections(tabContents, tabs, true);
                                setAIQualityNote(chunk.cached ? '已展开历史解读，不消耗次数。' : '解读已通过完整性校验，已为你保存。', 'success');
                                document.getElementById('aiTabs').style.display = 'flex';
                                document.getElementById('r_ai_meta').style.display = 'flex';
                                // 从解读文本中提炼实际引据的典籍，作为来源展示
                                const books = detectClassics(fullText);
                                const srcEl = document.getElementById('r_source_tag');
                                if (books.length) {
                                    srcEl.innerHTML = '引据典籍: ' + books.map(b => '<span>《' + b + '》</span>').join(' ');
                                } else {
                                    srcEl.innerHTML = '引据典籍: <span>《滴天髓》《子平真诠》等命理经典</span>';
                                }
                                srcEl.style.display = 'block';
                                document.getElementById('aiSeal').style.display = 'flex';
                            }
                            if (chunk.error) {
                                clearAIWaitTimer();
                                hadAIError = true;
                                setAIStatus('本次生成未通过，次数已保留。', 0);
                                setAIQualityNote(chunk.error, 'error', chunk.validation_issues || []);
                                document.getElementById('ai_性格').textContent = chunk.retryable ? '这次开示没有成文，点击按钮可重新生成。' : '解读失败: ' + chunk.error;
                                document.getElementById('aiTabs').style.display = 'flex';
                                if (btn) { btn.style.display = 'block'; }
                            }
                        } catch(e) {}
                    }
                }
                updateQuota();
                if (!hadAIError) loadHistory();  // 刷新历史列表的"已解读"标记
            } catch(e) {
                clearAIWaitTimer();
                setAIQualityNote('请求失败: ' + e.message + '。请稍后重试。', 'error');
                if (btn) { btn.style.display = 'block'; }
            }
        }

        async function deleteHistory(hid, e) {
            e.stopPropagation();
            if (!confirm('确定删除这条记录？')) return;
            try {
                await apiFetch('/api/history/' + hid, {method: 'DELETE'});
                loadHistory();
            } catch(err) {}
        }

        const BIRTH_PICKER_FIELDS = [
            {key:'year', label:'年', min:1900, max:2100},
            {key:'month', label:'月', min:1, max:12},
            {key:'day', label:'日', min:1, max:31},
            {key:'hour', label:'时', min:0, max:23},
            {key:'minute', label:'分', min:0, max:59},
        ];
        let birthPickerDraft = null;
        let birthWheelTimers = {};
        let birthWheelScrollLocks = {};
        let birthWheelDragState = null;

        function hasBirthSelection() {
            return BIRTH_PICKER_FIELDS.every(f => document.getElementById(f.key).value !== '');
        }

        function pad2(n) {
            return String(n).padStart(2, '0');
        }

        function daysInMonth(year, month) {
            return new Date(year, month, 0).getDate();
        }

        function clampNumber(value, min, max, fallback) {
            const n = parseInt(value, 10);
            if (isNaN(n)) return fallback;
            return Math.max(min, Math.min(max, n));
        }

        function normalizeBirthState(state) {
            const next = Object.assign({year:2000, month:1, day:1, hour:12, minute:0}, state || {});
            next.year = clampNumber(next.year, 1900, 2100, 2000);
            next.month = clampNumber(next.month, 1, 12, 1);
            next.hour = clampNumber(next.hour, 0, 23, 12);
            next.minute = clampNumber(next.minute, 0, 59, 0);
            next.day = clampNumber(next.day, 1, daysInMonth(next.year, next.month), 1);
            return next;
        }

        function readBirthFields() {
            return normalizeBirthState({
                year: document.getElementById('year').value,
                month: document.getElementById('month').value,
                day: document.getElementById('day').value,
                hour: document.getElementById('hour').value,
                minute: document.getElementById('minute').value,
            });
        }

        function formatBirthState(state) {
            return state.year + '年' + state.month + '月' + state.day + '日 ' + pad2(state.hour) + ':' + pad2(state.minute);
        }

        function setBirthPlaceholder() {
            const trigger = document.getElementById('birthPickerTrigger');
            document.getElementById('birthPickerText').textContent = '请选择出生时间';
            trigger.classList.add('is-empty');
            trigger.setAttribute('aria-label', '选择出生年月日时分');
        }

        function syncBirthFields(state) {
            const next = normalizeBirthState(state);
            const trigger = document.getElementById('birthPickerTrigger');
            document.getElementById('year').value = next.year;
            document.getElementById('month').value = next.month;
            document.getElementById('day').value = next.day;
            document.getElementById('hour').value = next.hour;
            document.getElementById('minute').value = next.minute;
            document.getElementById('birthPickerText').textContent = formatBirthState(next);
            trigger.classList.remove('is-empty');
            trigger.setAttribute('aria-label', '当前出生时间：' + formatBirthState(next) + '，轻触选择');
        }

        function getBirthFieldConfig(key) {
            const config = BIRTH_PICKER_FIELDS.find(f => f.key === key);
            if (!config) return null;
            if (key === 'day') {
                return Object.assign({}, config, {max: daysInMonth(birthPickerDraft.year, birthPickerDraft.month)});
            }
            return config;
        }

        function displayBirthValue(key, value) {
            if (key === 'hour' || key === 'minute') return pad2(value);
            return String(value);
        }

        function renderBirthWheel(key) {
            const config = getBirthFieldConfig(key);
            const wheel = document.getElementById('birthWheel_' + key);
            if (!config || !wheel) return;
            let html = '';
            for (let value = config.min; value <= config.max; value++) {
                const selected = value === birthPickerDraft[key] ? ' is-selected' : '';
                const ariaSelected = selected ? 'true' : 'false';
                html += '<button class="birth-wheel-item' + selected + '" type="button" role="option" aria-selected="' + ariaSelected + '" data-value="' + value + '" onclick="selectBirthValue(\'' + key + '\',' + value + ')">' + displayBirthValue(key, value) + '</button>';
            }
            wheel.innerHTML = html;
            wheel.onscroll = function() { handleBirthWheelScroll(key); };
            attachBirthWheelDrag(wheel, key);
        }

        function attachBirthWheelDrag(wheel, key) {
            if (!wheel || wheel.dataset.dragReady === '1') return;
            wheel.dataset.dragReady = '1';
            wheel.addEventListener('pointerdown', function(event) {
                if (event.button !== 0 || event.pointerType === 'touch') return;
                birthWheelDragState = {
                    key: key,
                    wheel: wheel,
                    startY: event.clientY,
                    startScrollTop: wheel.scrollTop,
                    moved: false,
                };
                wheel.classList.add('is-dragging');
                wheel.setPointerCapture(event.pointerId);
            });
            wheel.addEventListener('pointermove', function(event) {
                if (!birthWheelDragState || birthWheelDragState.wheel !== wheel) return;
                const deltaY = event.clientY - birthWheelDragState.startY;
                if (Math.abs(deltaY) > 3) birthWheelDragState.moved = true;
                wheel.scrollTop = birthWheelDragState.startScrollTop - deltaY;
                if (birthWheelDragState.moved) event.preventDefault();
            });
            function finishDrag(event) {
                if (!birthWheelDragState || birthWheelDragState.wheel !== wheel) return;
                const moved = birthWheelDragState.moved;
                birthWheelDragState = null;
                wheel.classList.remove('is-dragging');
                if (event && wheel.hasPointerCapture && wheel.hasPointerCapture(event.pointerId)) {
                    wheel.releasePointerCapture(event.pointerId);
                }
                if (moved) {
                    wheel.dataset.dragged = '1';
                    handleBirthWheelScroll(key);
                    setTimeout(() => { wheel.dataset.dragged = '0'; }, 0);
                }
            }
            wheel.addEventListener('pointerup', finishDrag);
            wheel.addEventListener('pointercancel', finishDrag);
            wheel.addEventListener('lostpointercapture', finishDrag);
            wheel.addEventListener('click', function(event) {
                if (wheel.dataset.dragged === '1') {
                    event.preventDefault();
                    event.stopPropagation();
                }
            }, true);
        }

        function renderBirthPickerWheels() {
            const root = document.getElementById('birthPickerWheels');
            root.innerHTML = BIRTH_PICKER_FIELDS.map(f =>
                '<div class="birth-wheel-wrap">'
                    + '<div class="birth-wheel-label">' + f.label + '</div>'
                    + '<div class="birth-wheel" id="birthWheel_' + f.key + '" data-key="' + f.key + '" role="listbox" aria-label="' + f.label + '选择"></div>'
                + '</div>'
            ).join('');
            BIRTH_PICKER_FIELDS.forEach(f => renderBirthWheel(f.key));
            requestAnimationFrame(() => BIRTH_PICKER_FIELDS.forEach(f => scrollBirthWheelToSelected(f.key, false)));
        }

        function updateBirthWheelSelection(key) {
            const wheel = document.getElementById('birthWheel_' + key);
            if (!wheel) return;
            wheel.querySelectorAll('.birth-wheel-item').forEach(item => {
                const selected = parseInt(item.dataset.value, 10) === birthPickerDraft[key];
                item.classList.toggle('is-selected', selected);
                item.setAttribute('aria-selected', selected ? 'true' : 'false');
            });
        }

        function shouldReduceMotion() {
            return window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
        }

        function scrollBirthWheelToSelected(key, smooth) {
            const wheel = document.getElementById('birthWheel_' + key);
            if (!wheel) return;
            const selected = wheel.querySelector('.birth-wheel-item.is-selected');
            if (!selected) return;
            const relativeTop = selected.offsetTop - wheel.offsetTop;
            const top = relativeTop - (wheel.clientHeight - selected.offsetHeight) / 2;
            clearTimeout(birthWheelScrollLocks[key]);
            birthWheelScrollLocks[key] = setTimeout(() => {
                birthWheelScrollLocks[key] = null;
            }, smooth && !shouldReduceMotion() ? 320 : 40);
            wheel.scrollTo({top, behavior: smooth && !shouldReduceMotion() ? 'smooth' : 'auto'});
        }

        function selectBirthValue(key, value) {
            birthPickerDraft[key] = parseInt(value, 10);
            const beforeDay = birthPickerDraft.day;
            birthPickerDraft = normalizeBirthState(birthPickerDraft);
            updateBirthWheelSelection(key);
            scrollBirthWheelToSelected(key, true);
            if (key === 'year' || key === 'month') {
                renderBirthWheel('day');
                updateBirthWheelSelection('day');
                scrollBirthWheelToSelected('day', beforeDay !== birthPickerDraft.day);
            }
        }

        function handleBirthWheelScroll(key) {
            if (birthWheelScrollLocks[key]) return;
            if (birthWheelDragState && birthWheelDragState.key === key) return;
            clearTimeout(birthWheelTimers[key]);
            birthWheelTimers[key] = setTimeout(() => {
                if (birthWheelScrollLocks[key]) return;
                if (birthWheelDragState && birthWheelDragState.key === key) return;
                const wheel = document.getElementById('birthWheel_' + key);
                if (!wheel) return;
                const center = wheel.scrollTop + wheel.clientHeight / 2;
                let nearest = null;
                let nearestDist = Infinity;
                wheel.querySelectorAll('.birth-wheel-item').forEach(item => {
                    const itemCenter = item.offsetTop - wheel.offsetTop + item.offsetHeight / 2;
                    const dist = Math.abs(itemCenter - center);
                    if (dist < nearestDist) {
                        nearestDist = dist;
                        nearest = item;
                    }
                });
                if (!nearest) return;
                const value = parseInt(nearest.dataset.value, 10);
                if (value !== birthPickerDraft[key]) {
                    selectBirthValue(key, value);
                } else {
                    scrollBirthWheelToSelected(key, true);
                }
            }, 90);
        }

        function openBirthPicker() {
            birthPickerDraft = hasBirthSelection() ? readBirthFields() : normalizeBirthState();
            renderBirthPickerWheels();
            document.getElementById('birthPickerOverlay').classList.add('active');
        }

        function closeBirthPicker(apply) {
            if (apply && birthPickerDraft) {
                syncBirthFields(birthPickerDraft);
            }
            document.getElementById('birthPickerOverlay').classList.remove('active');
            document.getElementById('birthPickerTrigger').focus({preventScroll: true});
        }

        function handleBirthPickerBackdrop(event) {
            if (event.target && event.target.id === 'birthPickerOverlay') {
                closeBirthPicker(false);
            }
        }

        function initBirthPicker() {
            if (hasBirthSelection()) {
                syncBirthFields(readBirthFields());
            } else {
                setBirthPlaceholder();
            }
        }

        document.addEventListener('keydown', function(event) {
            const authOverlay = document.getElementById('authModal');
            if (event.key === 'Escape' && authOverlay.classList.contains('active')) {
                closeAuthModal();
                return;
            }
            const overlay = document.getElementById('birthPickerOverlay');
            if (event.key === 'Escape' && overlay.classList.contains('active')) {
                closeBirthPicker(false);
            }
        });

        initBirthPicker();
        loadHistory();

        // === 演算动画 ===
        const divineSteps = [
            '推演天干地支...',
            '排列四柱八字...',
            '推算五行生克...',
            '安神定煞...',
            '推排大运流年...',
            '命盘已成。',
        ];

        function showDivineOverlay() {
            const overlay = document.getElementById('divineOverlay');
            const stepsEl = document.getElementById('divineSteps');
            const progressEl = document.getElementById('divineProgress');
            overlay.classList.remove('fade-out');
            overlay.classList.add('active');
            stepsEl.textContent = divineSteps[0];
            progressEl.style.width = '10%';

            return new Promise(resolve => {
                let stepIdx = 0;
                const totalSteps = divineSteps.length;
                const stepInterval = 450; // 每步450ms

                const timer = setInterval(() => {
                    stepIdx++;
                    if (stepIdx < totalSteps) {
                        stepsEl.style.opacity = '0';
                        setTimeout(() => {
                            stepsEl.textContent = divineSteps[stepIdx];
                            stepsEl.style.opacity = '1';
                        }, 150);
                        progressEl.style.width = Math.round((stepIdx + 1) / totalSteps * 100) + '%';
                    } else {
                        clearInterval(timer);
                        // 等待最后一句话显示
                        setTimeout(() => {
                            overlay.classList.add('fade-out');
                            setTimeout(() => {
                                overlay.classList.remove('active', 'fade-out');
                                resolve();
                            }, 500);
                        }, 400);
                    }
                }, stepInterval);
            });
        }

        async function divine() {
            const btn = document.getElementById('divineBtn');
            btn.disabled = true;
            btn.textContent = '排盘中...';

            if (!hasBirthSelection()) {
                alert('请先选择出生时间');
                btn.disabled = false; btn.textContent = '排盘推演';
                document.getElementById('birthPickerTrigger').focus();
                return;
            }

            const data = {
                name: document.getElementById('name').value,
                year: parseInt(document.getElementById('year').value),
                month: parseInt(document.getElementById('month').value),
                day: parseInt(document.getElementById('day').value),
                hour: parseInt(document.getElementById('hour').value),
                minute: parseInt(document.getElementById('minute').value),
                gender: document.getElementById('gender').value
            };

            // 前端表单验证
            if (isNaN(data.year) || data.year < 1900 || data.year > 2100) {
                alert('请输入有效的年份（1900-2100）');
                btn.disabled = false; btn.textContent = '排盘推演';
                return;
            }
            if (isNaN(data.month) || data.month < 1 || data.month > 12) {
                alert('请输入有效的月份（1-12）');
                btn.disabled = false; btn.textContent = '排盘推演';
                return;
            }
            if (isNaN(data.day) || data.day < 1 || data.day > 31) {
                alert('请输入有效的日期（1-31）');
                btn.disabled = false; btn.textContent = '排盘推演';
                return;
            }
            if (isNaN(data.hour) || data.hour < 0 || data.hour > 23) {
                alert('请输入有效的小时（0-23）');
                btn.disabled = false; btn.textContent = '排盘推演';
                return;
            }
            if (isNaN(data.minute) || data.minute < 0 || data.minute > 59) {
                alert('请输入有效的分钟（0-59）');
                btn.disabled = false; btn.textContent = '排盘推演';
                return;
            }

            try {
                // 1. 先调排盘API（后台并行计算）
                const paipanPromise = apiFetch('/api/paipan', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(data)
                }).then(r => r.json());

                // 2. 同时播放演算动画
                await showDivineOverlay();

                // 3. 动画结束后，获取排盘结果
                const result = await paipanPromise;
                if (result.error) { alert('排盘错误: ' + result.error); return; }

                // 4. 显示结果并滚动到结果区
                displayResult(result);
                currentResultData = result;
                loadHistory();

                // 滚动引导
                setTimeout(() => {
                    document.getElementById('resultSection').scrollIntoView({behavior:'smooth', block:'start'});
                }, 100);

                // 5. AI流式解读：已登录且有配额则自动开示；否则显示按钮引导（不弹窗打扰）
                if (isLoggedIn && lastQuota && lastQuota.can_use) {
                    await getAIReading(result);
                } else {
                    updateAIGetBtn();
                    document.getElementById('aiGetBtn').style.display = 'block';
                }

            } catch(e) {
                alert('请求失败: ' + e.message);
            } finally {
                btn.disabled = false;
                btn.textContent = '排盘推演';
            }
        }

        function switchTab(name, el, userAction) {
            if (userAction !== false) aiUserSelectedTab = true;
            document.querySelectorAll('.ai-tab').forEach(t => {
                t.classList.remove('active');
                t.setAttribute('aria-selected', 'false');
            });
            document.querySelectorAll('.ai-tab-content').forEach(c => c.classList.remove('active'));
            if (!el) {
                const idx = AI_TABS.indexOf(name);
                el = idx >= 0 ? document.querySelector('.ai-tab:nth-child(' + (idx + 1) + ')') : null;
            }
            if (el) {
                el.classList.add('active');
                el.setAttribute('aria-selected', 'true');
            }
            document.getElementById('tab_' + name).classList.add('active');
        }

        function renderTabs(text, tabContents, tabs) {
            // 重置所有板块
            tabs.forEach(t => { tabContents[t] = ''; });

            // 按【板块名】分割文本
            const regex = /【(性格|财运|婚姻|健康|大运|总评)】/g;
            let lastIdx = 0;
            let currentName = null;
            let match;
            const parts = [];

            while ((match = regex.exec(text)) !== null) {
                if (currentName) {
                    parts.push({ name: currentName, content: text.slice(lastIdx, match.index) });
                }
                currentName = match[1];
                lastIdx = match.index + match[0].length;
            }
            if (currentName) {
                parts.push({ name: currentName, content: text.slice(lastIdx) });
            }

            // 如果没解析到板块标记，全部放到性格
            if (parts.length === 0) {
                tabContents['性格'] = text;
            } else {
                for (const p of parts) {
                    tabContents[p.name] = p.content.trim();
                }
            }
        }

        function displayResult(r) {
            document.getElementById('resultSection').classList.add('active');
            clearAIWaitTimer();
            aiUserSelectedTab = false;
            setAIQualityNote('', '');
            AI_TABS.forEach(t => { document.getElementById('ai_' + t).innerHTML = ''; });
            document.getElementById('aiTabs').style.display = 'none';
            document.getElementById('aiProgressPanel').style.display = 'none';
            document.getElementById('r_ai_meta').style.display = 'none';
            document.getElementById('r_source_tag').style.display = 'none';
            document.getElementById('aiSeal').style.display = 'none';
            document.getElementById('aiGetBtn').style.display = 'none';
            switchTab('性格', null, false);
            showAISummary(r);
            const fp = r.four_pillars;
            document.getElementById('r_solar').textContent = r.solar_date;
            document.getElementById('r_lunar').textContent = r.lunar_date;
            document.getElementById('r_daymaster').textContent = r.day_master + '(' + r.day_master_wuxing + ')';
            document.getElementById('r_geju').textContent = r.geju;
            document.getElementById('r_shenwang').textContent = r.shenwang;
            document.getElementById('r_qiyun').textContent = r.qiyun_age + '岁 (' + (r.forward ? '顺行' : '逆行') + ')';
            document.getElementById('r_yg').textContent = fp.year.gan;
            document.getElementById('r_mg').textContent = fp.month.gan;
            document.getElementById('r_dg').textContent = fp.day.gan;
            document.getElementById('r_hg').textContent = fp.hour.gan;
            document.getElementById('r_yz').textContent = fp.year.zhi;
            document.getElementById('r_mz').textContent = fp.month.zhi;
            document.getElementById('r_dz').textContent = fp.day.zhi;
            document.getElementById('r_hz').textContent = fp.hour.zhi;
            document.getElementById('r_yss').textContent = fp.year.gan_shishen;
            document.getElementById('r_mss').textContent = fp.month.gan_shishen;
            document.getElementById('r_hss').textContent = fp.hour.gan_shishen;
            document.getElementById('r_yn').textContent = fp.year.nayin;
            document.getElementById('r_mn').textContent = fp.month.nayin;
            document.getElementById('r_dn').textContent = fp.day.nayin;
            document.getElementById('r_hn').textContent = fp.hour.nayin;
            document.getElementById('r_ycg').textContent = fp.year.zhi_canggan.join(' ');
            document.getElementById('r_mcg').textContent = fp.month.zhi_canggan.join(' ');
            document.getElementById('r_dcg').textContent = fp.day.zhi_canggan.join(' ');
            document.getElementById('r_hcg').textContent = fp.hour.zhi_canggan.join(' ');

            const wxColors = {'金':'metal','木':'wood','水':'water','火':'fire','土':'earth'};
            let wxHtml = '';
            for (const wx of ['金','木','水','火','土']) {
                const pct = r.wuxing_percent[wx];
                wxHtml += '<div class="wuxing-bar"><div class="wuxing-bar-label">' + wx + ' ' + r.wuxing_count[wx] + '(' + pct + '%)</div><div class="wuxing-bar-track"><div class="wuxing-bar-fill wuxing-' + wxColors[wx] + '" style="width:' + pct + '%"></div></div></div>';
            }
            document.getElementById('r_wuxing_bars').innerHTML = wxHtml;
            document.getElementById('r_xiyong').textContent = r.xiyong.join('、');
            document.getElementById('r_jishen').textContent = '忌: ' + r.jishen;
            document.getElementById('r_shensha').innerHTML = r.shensha.length ? r.shensha.map(s => '<span class="shensha-tag">' + s + '</span>').join('') : '<span class="shensha-tag">无</span>';
            document.getElementById('r_zhi_rel').textContent = r.zhi_relations.length ? r.zhi_relations.join('、') : '无特殊关系';

            const currentYear = new Date().getFullYear();
            const birthYear = parseInt(r.solar_date.match(/(\d+)年/)[1]);
            const currentAge = currentYear - birthYear;
            let dyHtml = '';
            for (const dy of r.dayun) {
                const isCurrent = currentAge >= dy.start_age && currentAge <= dy.end_age;
                const yearStr = (dy.start_year && dy.end_year) ? (dy.start_year + '-' + dy.end_year + '年') : '';
                dyHtml += '<tr ' + (isCurrent ? 'class="dayun-current"' : '') + '>'
                    + '<td>' + dy.start_age + '-' + dy.end_age + '岁'
                    + (yearStr ? '<br><span style="font-size:0.75rem;color:var(--text-dim)">' + yearStr + '</span>' : '')
                    + '</td>'
                    + '<td><b>' + dy.gan + dy.zhi + '</b></td>'
                    + '<td>' + dy.gan_shishen + '</td>'
                    + '<td>' + dy.zhi_shishen + '</td>'
                    + '<td>' + dy.nayin + '</td></tr>';
            }
            document.getElementById('r_dayun').innerHTML = dyHtml;
        }
    </script>
</body>
</html>
'''

if __name__ == '__main__':
    import os
    port = int(os.environ.get('PORT', 8888))
    debug = os.environ.get('FLASK_DEBUG', '0') == '1'
    print('玄机阁启动中...')
    print(f'DeepSeek API Key: {"已配置" if ai_service.DEEPSEEK_API_KEY else "未配置（AI解读不可用，排盘正常）"}')
    print(f'数据库: {db.DB_PATH}')
    print(f'模式: {"开发(debug)" if debug else "生产"}')
    app.run(host='0.0.0.0', port=port, debug=debug)
