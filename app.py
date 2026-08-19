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

import db
import ai_service
from bazi_engine import paipan

app = Flask(__name__)
CORS(app)


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
    ip = req.headers.get('X-Forwarded-For', req.remote_addr or 'unknown')
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
    ip = request.headers.get('X-Forwarded-For', request.remote_addr or 'unknown')
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

    # --- 未命中缓存：先检查配额 ---
    quota_result = db.check_quota_account(auth_token)
    if quota_result is None:
        return jsonify({'error': '登录已过期，请重新登录', 'need_login': True}), 401
    can_use, free_remaining, is_free = quota_result

    if not can_use:
        return jsonify({
            'error': '配额已用完',
            'message': '您的5次免费解读机会已用完',
            'need_recharge': True
        }), 403

    # --- 在流式生成中消耗配额（AI失败则不消耗） ---
    def generate():
        full_text = ''
        meta = {}
        quota_consumed = False

        for chunk in ai_service.stream_interpretation(paipan_data):
            yield chunk

            # 解析chunk获取token信息
            if chunk.startswith('data: ') and chunk.endswith('\n\n'):
                try:
                    data = json.loads(chunk[6:].strip())
                    if data.get('text') and not quota_consumed:
                        # 收到第一个文本chunk，消耗配额（已确保登录）
                        db.consume_quota_account(auth_token, is_free)
                        quota_consumed = True
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
            'X-Is-Free': str(is_free).lower(),
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

@app.route('/api/register', methods=['POST'])
def api_register():
    """注册"""
    data = request.json or {}
    username = (data.get('username') or '').strip()
    password = data.get('password', '')
    token, error = db.register(username, password)
    if error:
        return jsonify({'error': error}), 400
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
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Noto+Serif+SC:wght@400;700&display=swap');
        * { margin: 0; padding: 0; box-sizing: border-box; }
        :root {
            --bg-dark: #0a0a0f; --bg-card: #15151f; --bg-card-hover: #1a1a28;
            --border: #2a2a3a; --gold: #c9a84c; --gold-bright: #e8c870;
            --gold-dim: #8a7030; --text: #d4c8a8; --text-dim: #6a6a7a;
            --red: #8b2020; --red-bright: #c94040; --green: #4a9a4a;
        }
        body {
            font-family: 'Noto Serif SC', serif;
            background: var(--bg-dark); color: var(--text); min-height: 100vh;
            padding-top: env(safe-area-inset-top);
            padding-bottom: env(safe-area-inset-bottom);
            -webkit-text-size-adjust: 100%;
            -webkit-tap-highlight-color: transparent;
            background-image:
                radial-gradient(ellipse at 20% 0%, rgba(201,168,76,0.05) 0%, transparent 50%),
                radial-gradient(ellipse at 80% 100%, rgba(139,32,32,0.05) 0%, transparent 50%);
        }
        .header {
            border-bottom: 1px solid var(--border); padding: 0.5rem 0.75rem;
            padding-top: calc(0.5rem + env(safe-area-inset-top, 0px));
            display: flex; align-items: center; justify-content: space-between;
            background: rgba(10,10,15,0.8); backdrop-filter: blur(10px);
            position: sticky; top: 0; z-index: 100;
        }
        .logo { display: flex; align-items: center; gap: 0.5rem; }
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
        .quota-badge {
            background: rgba(201,168,76,0.15); border: 1px solid rgba(201,168,76,0.3);
            border-radius: 20px; padding: 0.2rem 0.5rem;
            font-size: 0.7rem; color: var(--gold); white-space: nowrap;
        }
        .container { max-width: 900px; margin: 0 auto; padding: 0.75rem; padding-bottom: calc(0.75rem + env(safe-area-inset-bottom, 0px)); }

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
        .btn-divine {
            display: block; width: 100%; padding: 0.8rem; margin-top: 0.75rem;
            background: linear-gradient(135deg, var(--gold-dim), var(--gold));
            border: none; border-radius: 8px; color: var(--bg-dark);
            font-family: inherit; font-size: 1.05rem; font-weight: 700;
            cursor: pointer; letter-spacing: 0.15em; transition: all 0.3s;
            min-height: 46px; -webkit-appearance: none;
        }
        .btn-divine:hover {
            background: linear-gradient(135deg, var(--gold), var(--gold-bright));
            box-shadow: 0 4px 20px rgba(201,168,76,0.3);
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
            background: rgba(201,168,76,0.1); color: var(--gold); padding: 0.8rem;
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
        .dayun-table th { background: rgba(201,168,76,0.1); color: var(--gold); padding: 0.6rem; font-size: 0.85rem; border-bottom: 1px solid var(--border); }
        .dayun-table td { padding: 0.6rem; text-align: center; border-bottom: 1px solid var(--border); font-size: 0.9rem; }
        .dayun-current { background: rgba(201,168,76,0.08); }

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

        /* Tab切换 */
        .ai-tabs {
            display: flex; gap: 0; margin-bottom: 1rem;
            border-bottom: 1px solid var(--border); overflow-x: auto;
        }
        .ai-tab {
            padding: 0.6rem 1rem; font-size: 0.85rem; color: var(--text-dim);
            cursor: pointer; border-bottom: 2px solid transparent; transition: all 0.2s;
            white-space: nowrap;
        }
        .ai-tab.active { color: var(--gold-bright); border-bottom-color: var(--gold); }
        .ai-tab:hover { color: var(--gold); }
        .ai-tab-content { display: none; }
        .ai-tab-content.active { display: block; }
        .ai-content { line-height: 1.9; color: var(--text); font-size: 0.95rem; min-height: 4rem; }
        .ai-para { margin: 0 0 0.55rem 0; }
        .ai-quote {
            margin: 0.5rem 0 0.85rem 0; padding: 0.45rem 0.75rem;
            background: rgba(201,168,76,0.06); border-left: 3px solid var(--gold-dim);
            border-radius: 0 6px 6px 0; color: var(--gold);
            font-size: 0.86rem; font-style: italic; line-height: 1.8;
        }
        .ai-waiting { color: var(--text-dim); font-size: 0.85rem; }
        .ai-seal {
            display: none; margin: 1.4rem auto 0.2rem; width: 72px; height: 72px;
            border: 2px solid rgba(201,64,64,0.75); border-radius: 8px;
            color: var(--red-bright); font-size: 0.95rem; font-weight: 700;
            align-items: center; justify-content: center; text-align: center;
            transform: rotate(-6deg); line-height: 1.35; letter-spacing: 0.15em;
            box-shadow: inset 0 0 12px rgba(201,64,64,0.12);
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
            background: rgba(201,168,76,0.15); border: 1px solid rgba(201,168,76,0.3);
            border-radius: 4px; padding: 0.2rem 0.6rem; font-size: 0.8rem; color: var(--gold);
        }

        /* 付费弹窗 */
        .modal-overlay {
            display: none; position: fixed; top: 0; left: 0; right: 0; bottom: 0;
            background: rgba(0,0,0,0.7); z-index: 200;
            justify-content: center; align-items: center;
        }
        .modal-overlay.active { display: flex; }
        .modal {
            background: var(--bg-card); border: 1px solid var(--gold);
            border-radius: 12px; padding: 2rem; max-width: 400px; text-align: center;
        }
        .modal-title { color: var(--gold); font-size: 1.2rem; margin-bottom: 1rem; }
        .modal-text { color: var(--text); margin-bottom: 1.5rem; line-height: 1.6; }
        .modal-btn { background: linear-gradient(135deg, var(--gold-dim), var(--gold)); border: none; border-radius: 8px; padding: 0.8rem 2rem; color: var(--bg-dark); font-family: inherit; font-weight: 700; cursor: pointer; }

        /* 登录注册弹窗 */
        .auth-input {
            background: var(--bg-dark); border: 1px solid var(--border); border-radius: 8px;
            padding: 0.7rem; color: var(--text); font-family: inherit; font-size: 1rem;
            width: 100%; box-sizing: border-box; margin-bottom: 0.8rem; min-height: 44px;
        }
        .auth-input:focus { outline: none; border-color: var(--gold); }
        .auth-error { color: var(--red-bright); font-size: 0.85rem; margin-bottom: 0.8rem; min-height: 1.2rem; }
        .auth-switch { color: var(--gold-dim); font-size: 0.85rem; cursor: pointer; text-decoration: underline; margin-top: 0.5rem; display: block; }
        .auth-btn { width: 100%; box-sizing: border-box; }
        .user-badge { font-size: 0.8rem; color: var(--gold-bright); cursor: pointer; }

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
            background: rgba(10,10,15,0.95); z-index: 300;
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
            box-shadow: 0 0 40px rgba(201,168,76,0.3);
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
            text-shadow: 0 0 20px rgba(201,168,76,0.5);
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
            border: 1px solid rgba(201,168,76,0.15); border-radius: 50%;
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
            .divine-taiji { width: 90px; height: 90px; }
            .divine-taiji::before, .divine-taiji::after { width: 45px; height: 45px; }
            .divine-taiji-inner { font-size: 1.5rem; }
            .divine-steps { font-size: 0.95rem; margin-top: 1.5rem; }
            .divine-progress { width: 180px; }
            .divine-ring { width: 160px; height: 160px; margin-top: -80px; margin-left: -80px; }
            .divine-ring:nth-child(2) { width: 220px; height: 220px; margin-top: -110px; margin-left: -110px; }
        }
    </style>
</head>
<body>
    <div class="header">
        <div class="logo">
            <div class="logo-taiji"></div>
            <div>
                <div class="logo-text">玄机阁</div>
                <div class="header-sub">八字排盘 · AI解读</div>
            </div>
        </div>
        <div style="display:flex;align-items:center;gap:0.8rem;">
            <span class="user-badge" id="userBadge" onclick="showAuthModal()" style="display:none;"></span>
            <div class="quota-badge" id="quotaBadge" onclick="showAuthModal('login')">登录领5次解读</div>
        </div>
    </div>

    <div class="container">
        <div class="input-card">
            <div class="card-title">输入生辰信息</div>
            <div class="form-grid">
                <div class="form-group" style="grid-column: 1 / -1;"><label>姓名/备注（选填）</label><input type="text" id="name" placeholder="如：张三"></div>
                <div class="form-group"><label>公历年份</label><input type="tel" id="year" placeholder="如：1990" inputmode="numeric" pattern="[0-9]*"></div>
                <div class="form-group"><label>月份</label><input type="tel" id="month" placeholder="1-12" inputmode="numeric" pattern="[0-9]*"></div>
                <div class="form-group"><label>日期</label><input type="tel" id="day" placeholder="1-31" inputmode="numeric" pattern="[0-9]*"></div>
                <div class="form-group"><label>时(24小时制)</label><input type="tel" id="hour" placeholder="0-23" inputmode="numeric" pattern="[0-9]*"></div>
                <div class="form-group"><label>分钟</label><input type="tel" id="minute" placeholder="0-59" inputmode="numeric" pattern="[0-9]*"></div>
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
                <div class="ai-tabs" id="aiTabs" style="display:none;">
                    <div class="ai-tab active" onclick="switchTab('性格', this)">性格</div>
                    <div class="ai-tab" onclick="switchTab('财运', this)">财运</div>
                    <div class="ai-tab" onclick="switchTab('婚姻', this)">婚姻</div>
                    <div class="ai-tab" onclick="switchTab('健康', this)">健康</div>
                    <div class="ai-tab" onclick="switchTab('大运', this)">大运</div>
                    <div class="ai-tab" onclick="switchTab('总评', this)">总评</div>
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
    <div class="modal-overlay" id="authModal">
        <div class="modal">
            <div class="modal-title" id="authTitle">登录</div>
            <input type="text" class="auth-input" id="authUsername" placeholder="用户名（2-20字符）" maxlength="20">
            <input type="password" class="auth-input" id="authPassword" placeholder="密码（至少4位）" style="-webkit-text-security:disc;">
            <div class="auth-error" id="authError"></div>
            <button class="modal-btn auth-btn" id="authSubmitBtn" onclick="handleAuth()">登录</button>
            <span class="auth-switch" id="authSwitch" onclick="switchAuthMode()">没有账号？去注册</span>
            <span class="auth-switch" onclick="document.getElementById('authModal').classList.remove('active')" style="margin-top:0.8rem;">取消</span>
        </div>
    </div>

    <script>
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

        // 命理典籍库（用于展示引据来源）
        const CLASSIC_BOOKS = ['滴天髓','子平真诠','穷通宝鉴','三命通会','渊海子平','神峰通考','命理约言','李虚中命书','黄金策','黄帝内经'];

        function escapeHtml(s) {
            return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
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

        function showAuthModal(mode) {
            authMode = mode || 'login';
            document.getElementById('authError').textContent = '';
            document.getElementById('authUsername').value = '';
            document.getElementById('authPassword').value = '';
            updateAuthUI();
            document.getElementById('authModal').classList.add('active');
        }

        function switchAuthMode() {
            authMode = authMode === 'login' ? 'register' : 'login';
            document.getElementById('authError').textContent = '';
            updateAuthUI();
        }

        function updateAuthUI() {
            document.getElementById('authTitle').textContent = authMode === 'login' ? '登录' : '注册';
            document.getElementById('authSubmitBtn').textContent = authMode === 'login' ? '登录' : '注册';
            document.getElementById('authSwitch').textContent = authMode === 'login' ? '没有账号？去注册' : '已有账号？去登录';
        }

        async function handleAuth() {
            const username = document.getElementById('authUsername').value.trim();
            const password = document.getElementById('authPassword').value;
            const errEl = document.getElementById('authError');
            errEl.textContent = '';
            if (!username || !password) {
                errEl.textContent = '请填写用户名和密码';
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
                    body: JSON.stringify({username, password})
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
                document.getElementById('authModal').classList.remove('active');
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
                const tabs = ['性格','财运','婚姻','健康','大运','总评'];
                tabs.forEach(t => { document.getElementById('ai_' + t).textContent = ''; });
                document.getElementById('aiTabs').style.display = 'none';
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

            const tabs = ['性格','财运','婚姻','健康','大运','总评'];
            tabs.forEach(t => { document.getElementById('ai_' + t).innerHTML = ''; });
            document.getElementById('r_ai_meta').style.display = 'none';
            document.getElementById('r_source_tag').style.display = 'none';
            document.getElementById('aiSeal').style.display = 'none';
            document.getElementById('aiTabs').style.display = 'flex';
            document.getElementById('ai_性格').innerHTML = '<span class="ai-waiting">道长焚香净手，展卷研读命书...</span><span class="ai-cursor"></span>';

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
                        document.getElementById('ai_性格').textContent = '登录后即可请道长开示（新用户5次免费）。';
                        showAuthModal('login');
                    }
                    updateAIGetBtn();
                    if (btn) { btn.style.display = 'block'; }
                    return;
                }

                if (aiRes.status === 403) {
                    const errData = await aiRes.json();
                    if (errData.need_recharge) {
                        document.getElementById('rechargeModal').classList.add('active');
                        document.getElementById('ai_性格').textContent = '免费次数已用完。';
                    }
                    updateAIGetBtn();
                    if (btn) { btn.style.display = 'block'; }
                    return;
                }

                // SSE流式读取
                const reader = aiRes.body.getReader();
                const decoder = new TextDecoder();
                let fullText = '';
                let buffer = '';
                let currentTab = '性格';
                let tabContents = {}; tabs.forEach(t => tabContents[t] = '');

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
                            if (chunk.text) {
                                fullText += chunk.text;
                                renderTabs(fullText, tabContents, tabs);
                                let activeTab = currentTab;
                                for (const t of tabs) {
                                    if (tabContents[t].length > 0) activeTab = t;
                                }
                                if (activeTab !== currentTab) {
                                    currentTab = activeTab;
                                    document.querySelectorAll('.ai-tab').forEach(t => t.classList.remove('active'));
                                    document.querySelectorAll('.ai-tab-content').forEach(c => c.classList.remove('active'));
                                    document.querySelector('.ai-tab:nth-child(' + (tabs.indexOf(currentTab)+1) + ')').classList.add('active');
                                    document.getElementById('tab_' + currentTab).classList.add('active');
                                }
                                document.getElementById('aiTabs').style.display = 'flex';
                                document.getElementById('ai_' + currentTab).innerHTML = formatInterpretation(tabContents[currentTab]) + '<span class="ai-cursor"></span>';
                            }
                            if (chunk.done) {
                                renderTabs(fullText, tabContents, tabs);
                                tabs.forEach(t => {
                                    document.getElementById('ai_' + t).innerHTML = formatInterpretation(tabContents[t]);
                                });
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
                                document.getElementById('ai_性格').textContent = '解读失败: ' + chunk.error;
                                document.getElementById('aiTabs').style.display = 'flex';
                                if (btn) { btn.style.display = 'block'; }
                            }
                        } catch(e) {}
                    }
                }
                updateQuota();
                loadHistory();  // 刷新历史列表的"已解读"标记
            } catch(e) {
                alert('请求失败: ' + e.message);
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

        function switchTab(name, el) {
            document.querySelectorAll('.ai-tab').forEach(t => t.classList.remove('active'));
            document.querySelectorAll('.ai-tab-content').forEach(c => c.classList.remove('active'));
            if (el) el.classList.add('active');
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
