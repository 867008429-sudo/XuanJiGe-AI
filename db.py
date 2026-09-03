"""
玄机阁 - 数据库层
SQLite存储：用户识别、使用次数、AI解读缓存、token用量统计、账号登录
"""
import sqlite3
import hashlib
import os
import json
import secrets
from datetime import datetime, timedelta
from werkzeug.security import check_password_hash, generate_password_hash

DB_PATH = os.environ.get('DB_PATH', os.path.join(os.path.dirname(__file__), 'xuanjige.db'))

# chat 链路配置（agent-plan.md v4.1 §2.1/§4.7；香火计量 persona-plan.md §3）
# 语义：每个命盘（hid）N 炷免费香火；1 炷 = flash 档短回复的均值成本锚点。
# used 列从"条数"改记"炷数"，缺省人格（清虚 2 炷/问）下与旧"5 次追问"等价。
CHAT_FREE_INCENSE = int(os.environ.get('CHAT_FREE_INCENSE', '10'))
DB_BUSY_TIMEOUT_MS = int(os.environ.get('DB_BUSY_TIMEOUT_MS', '5000'))


def get_db():
    """获取数据库连接

    busy_timeout 是 spike 3 的结论性配置：gunicorn 2 worker 场景下，
    无该参数的并发写会确定性报 database is locked；WAL + 5000ms 等待实测通过。
    """
    db = sqlite3.connect(DB_PATH, timeout=DB_BUSY_TIMEOUT_MS / 1000.0)
    db.row_factory = sqlite3.Row
    db.execute(f'PRAGMA busy_timeout = {DB_BUSY_TIMEOUT_MS}')
    return db


def init_db():
    """初始化数据库表"""
    db = get_db()
    db.executescript('''
        -- 用户表（基于IP+UA指纹识别，无需登录）
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fingerprint TEXT UNIQUE NOT NULL,
            ip TEXT,
            user_agent TEXT,
            free_trials_used INTEGER DEFAULT 0,
            credits INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now')),
            last_active TEXT DEFAULT (datetime('now'))
        );

        -- AI解读缓存表（相同生辰信息不重复调用API）
        CREATE TABLE IF NOT EXISTS ai_cache (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cache_key TEXT UNIQUE NOT NULL,
            paipan_json TEXT NOT NULL,
            interpretation TEXT NOT NULL,
            prompt_tokens INTEGER DEFAULT 0,
            completion_tokens INTEGER DEFAULT 0,
            total_tokens INTEGER DEFAULT 0,
            model TEXT,
            cost_usd REAL DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        );

        -- 使用记录表
        CREATE TABLE IF NOT EXISTS usage_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fingerprint TEXT NOT NULL,
            endpoint TEXT NOT NULL,
            cache_hit BOOLEAN DEFAULT 0,
            tokens_used INTEGER DEFAULT 0,
            cost_usd REAL DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        );

        -- 历史记录表（用户排过的盘）
        CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fingerprint TEXT NOT NULL,
            name TEXT DEFAULT '',
            gender TEXT NOT NULL,
            solar_date TEXT NOT NULL,
            paipan_json TEXT NOT NULL,
            has_ai BOOLEAN DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        );

        -- 账号表（注册登录用）
        CREATE TABLE IF NOT EXISTS accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            free_trials_used INTEGER DEFAULT 0,
            credits INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        );

        -- 登录会话表
        CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY NOT NULL,
            account_id INTEGER NOT NULL,
            fingerprint TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now')),
            expires_at TEXT,
            FOREIGN KEY (account_id) REFERENCES accounts(id)
        );

        -- 注册尝试表：用于防止同IP/同设备批量注册薅免费次数
        CREATE TABLE IF NOT EXISTS registration_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ip TEXT NOT NULL,
            client_id TEXT DEFAULT '',
            username TEXT DEFAULT '',
            success INTEGER DEFAULT 0,
            reason TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_registration_attempts_ip_time
            ON registration_attempts(ip, created_at);
        CREATE INDEX IF NOT EXISTS idx_registration_attempts_client_time
            ON registration_attempts(client_id, created_at);

        -- 体验码表：用于邀请制注册和防止单个体验码无限复用
        CREATE TABLE IF NOT EXISTS invite_codes (
            code TEXT PRIMARY KEY NOT NULL,
            max_uses INTEGER DEFAULT 1,
            used_count INTEGER DEFAULT 0,
            disabled INTEGER DEFAULT 0,
            note TEXT DEFAULT '',
            used_by_account_id INTEGER,
            created_at TEXT DEFAULT (datetime('now')),
            used_at TEXT,
            FOREIGN KEY (used_by_account_id) REFERENCES accounts(id)
        );

        CREATE INDEX IF NOT EXISTS idx_invite_codes_state
            ON invite_codes(disabled, used_count, max_uses);

        -- 对话追问配额表（每盘 N 炷香火，used 记炷数，persona-plan.md §3）
        CREATE TABLE IF NOT EXISTS chat_followups (
            hid INTEGER NOT NULL,
            fingerprint TEXT NOT NULL,
            used INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (hid, fingerprint)
        );

        -- 对话幂等表：同 (thread, request_id) 命中直接回放，不进图不扣额度（agent-plan §5）
        -- price/persona 记录该次实扣与所请教道长（断线重放的账目依据，persona-plan §3.1）
        CREATE TABLE IF NOT EXISTS chat_requests (
            thread_id TEXT NOT NULL,
            request_id TEXT NOT NULL,
            reply_text TEXT NOT NULL,
            quota_left INTEGER DEFAULT 0,
            price INTEGER NOT NULL DEFAULT 2,
            persona TEXT NOT NULL DEFAULT 'qingxu',
            created_at TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (thread_id, request_id)
        );

        CREATE INDEX IF NOT EXISTS idx_chat_requests_thread
            ON chat_requests(thread_id, created_at);
    ''')
    # persona-plan §3.2：条数→炷数存量迁移（幂等）。
    # 标记 = chat_requests.price 列：新库建表自带该列直接跳过。
    # 原子性：必须显式 BEGIN IMMEDIATE——SQLite 本身 ALTER 是事务性的，
    # 但 Python sqlite3 隐式模式下 DDL 会自动提交，若不加显式事务，
    # 崩溃在"加列已提交、换算未提交"之间会留下错账且标记已存在、永不重试。
    # 并发竞态（gunicorn 2 worker 同时 init_db）：败者的 ALTER 撞
    # duplicate column 抛 OperationalError，回滚后复查标记，存在即跳过。
    _cols = {row[1] for row in db.execute('PRAGMA table_info(chat_requests)')}
    if 'price' not in _cols:
        try:
            db.isolation_level = None  # 手动事务模式
            db.execute('BEGIN IMMEDIATE')  # 抢写锁：与并发迁移者串行化
            db.execute(
                'ALTER TABLE chat_requests ADD COLUMN price INTEGER NOT NULL DEFAULT 2'
            )
            db.execute(
                "ALTER TABLE chat_requests ADD COLUMN persona TEXT NOT NULL DEFAULT 'qingxu'"
            )
            # 换算自洽：旧 1 条追问 ≈ 缺省道长 2 炷；used=3 条 → 6 炷，
            # 剩 4 炷 = 还能问 2 次，与迁移前"剩 2 次"的可用量等价
            db.execute('UPDATE chat_followups SET used = used * 2')
            db.execute('UPDATE chat_requests SET quota_left = quota_left * 2')
            db.execute('COMMIT')
        except sqlite3.OperationalError:
            try:
                db.execute('ROLLBACK')
            except sqlite3.OperationalError:
                pass  # 事务未开启（BEGIN 即失败）或已结束
            _cols2 = {row[1] for row in db.execute('PRAGMA table_info(chat_requests)')}
            if 'price' not in _cols2:
                raise
        finally:
            db.isolation_level = ''  # 恢复隐式模式（get_db 语义不变）
    # WAL 是 spike 3 结论：多 worker 并发写下读写不互斥（journal_mode 是库级持久属性）
    db.execute('PRAGMA journal_mode = WAL')
    db.commit()
    db.close()


def get_fingerprint(ip, user_agent):
    """生成用户指纹（IP + User-Agent的SHA256）"""
    raw = f"{ip}|{user_agent}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def get_or_create_user(fingerprint, ip, user_agent):
    """获取或创建用户记录"""
    db = get_db()
    user = db.execute(
        'SELECT * FROM users WHERE fingerprint = ?', (fingerprint,)
    ).fetchone()

    if user is None:
        db.execute(
            'INSERT INTO users (fingerprint, ip, user_agent) VALUES (?, ?, ?)',
            (fingerprint, ip, user_agent)
        )
        db.commit()
        user = db.execute(
            'SELECT * FROM users WHERE fingerprint = ?', (fingerprint,)
        ).fetchone()

    # 更新最后活跃时间
    db.execute(
        'UPDATE users SET last_active = datetime("now") WHERE fingerprint = ?',
        (fingerprint,)
    )
    db.commit()
    db.close()
    return user


def check_quota(fingerprint):
    """
    检查用户配额
    返回: (can_use, free_remaining, is_free)
    """
    db = get_db()
    user = db.execute(
        'SELECT * FROM users WHERE fingerprint = ?', (fingerprint,)
    ).fetchone()
    db.close()

    if user is None:
        return True, 5, True  # 新用户，5次免费

    free_used = user['free_trials_used']
    free_remaining = max(0, 5 - free_used)
    credits = user['credits'] if user['credits'] else 0

    if free_remaining > 0:
        return True, free_remaining, True
    elif credits > 0:
        return True, 0, False
    else:
        return False, 0, False


def consume_quota(fingerprint, is_free):
    """消耗一次配额"""
    db = get_db()
    if is_free:
        db.execute(
            'UPDATE users SET free_trials_used = free_trials_used + 1 WHERE fingerprint = ?',
            (fingerprint,)
        )
    else:
        db.execute(
            'UPDATE users SET credits = credits - 1 WHERE fingerprint = ?',
            (fingerprint,)
        )
    db.commit()
    db.close()


def get_cache(cache_key):
    """获取缓存的AI解读"""
    db = get_db()
    cache = db.execute(
        'SELECT * FROM ai_cache WHERE cache_key = ?', (cache_key,)
    ).fetchone()
    db.close()
    return cache


def save_cache(cache_key, paipan_json, interpretation,
               prompt_tokens, completion_tokens, total_tokens,
               model, cost_usd):
    """保存AI解读到缓存"""
    db = get_db()
    db.execute(
        '''INSERT OR REPLACE INTO ai_cache
           (cache_key, paipan_json, interpretation, prompt_tokens,
            completion_tokens, total_tokens, model, cost_usd)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
        (cache_key, paipan_json, interpretation,
         prompt_tokens, completion_tokens, total_tokens,
         model, cost_usd)
    )
    db.commit()
    db.close()


def log_usage(fingerprint, endpoint, cache_hit, tokens_used, cost_usd):
    """记录使用日志"""
    db = get_db()
    db.execute(
        '''INSERT INTO usage_logs (fingerprint, endpoint, cache_hit, tokens_used, cost_usd)
           VALUES (?, ?, ?, ?, ?)''',
        (fingerprint, endpoint, cache_hit, tokens_used, cost_usd)
    )
    db.commit()
    db.close()


def save_history(fingerprint, name, gender, solar_date, paipan_json, has_ai=False):
    """保存排盘历史（同人同生辰只保留一条），返回该记录的 hid。

    chat 追问链路以 hid 定位命盘，排盘落库后立即回传 id（api_paipan
    → history_id），前端据此建立会话，无需再查列表。
    """
    db = get_db()
    # 同一个人同样的生辰只保留一条（更新即可）
    existing = db.execute(
        'SELECT id FROM history WHERE fingerprint = ? AND solar_date = ? AND gender = ?',
        (fingerprint, solar_date, gender)
    ).fetchone()
    if existing:
        db.execute(
            '''UPDATE history SET name = ?, paipan_json = ?, has_ai = ?, created_at = datetime('now')
               WHERE id = ?''',
            (name, paipan_json, has_ai, existing['id'])
        )
        hid = existing['id']
    else:
        cur = db.execute(
            '''INSERT INTO history (fingerprint, name, gender, solar_date, paipan_json, has_ai)
               VALUES (?, ?, ?, ?, ?, ?)''',
            (fingerprint, name, gender, solar_date, paipan_json, has_ai)
        )
        hid = cur.lastrowid
    db.commit()
    db.close()
    return hid


def get_history(fingerprint):
    """获取用户排盘历史（最新优先）。

    created_at 只有秒级精度，同一秒内的多条记录会并列；
    用 id DESC 作次级排序保证"后插入的排前面"是确定性的，
    否则同一秒连排两个盘时历史列表顺序会错乱。
    """
    db = get_db()
    rows = db.execute(
        '''SELECT id, name, gender, solar_date, has_ai, created_at
           FROM history WHERE fingerprint = ?
           ORDER BY created_at DESC, id DESC''',
        (fingerprint,)
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]


def mark_history_has_ai(fingerprint, gender, solar_date):
    """AI解读完成后，把对应历史记录标记为已解读"""
    db = get_db()
    db.execute(
        '''UPDATE history SET has_ai = 1
           WHERE fingerprint = ? AND solar_date = ? AND gender = ?''',
        (fingerprint, solar_date, gender)
    )
    db.commit()
    db.close()


def migrate_history(from_fp, to_fp):
    """
    把匿名设备（client_id指纹）的历史记录迁移到账号名下
    用于注册/登录时：访客先排过盘，登录后历史记录跟着账号走
    """
    if not from_fp or not to_fp or from_fp == to_fp:
        return
    db = get_db()
    rows = db.execute(
        'SELECT id, solar_date, gender, has_ai FROM history WHERE fingerprint = ?',
        (from_fp,)
    ).fetchall()
    for row in rows:
        dup = db.execute(
            '''SELECT id, has_ai FROM history
               WHERE fingerprint = ? AND solar_date = ? AND gender = ?''',
            (to_fp, row['solar_date'], row['gender'])
        ).fetchone()
        if dup:
            # 账号下已有同一命盘：保留账号的，但别丢"已解读"标记
            if row['has_ai'] and not dup['has_ai']:
                db.execute('UPDATE history SET has_ai = 1 WHERE id = ?', (dup['id'],))
            db.execute('DELETE FROM history WHERE id = ?', (row['id'],))
        else:
            db.execute(
                'UPDATE history SET fingerprint = ? WHERE id = ?',
                (to_fp, row['id'])
            )
    db.commit()
    db.close()


def get_history_detail(history_id, fingerprint):
    """获取单条历史详情"""
    db = get_db()
    row = db.execute(
        'SELECT * FROM history WHERE id = ? AND fingerprint = ?',
        (history_id, fingerprint)
    ).fetchone()
    db.close()
    return dict(row) if row else None


def delete_history(history_id, fingerprint):
    """删除单条历史"""
    db = get_db()
    db.execute(
        'DELETE FROM history WHERE id = ? AND fingerprint = ?',
        (history_id, fingerprint)
    )
    db.commit()
    db.close()


def get_stats():
    """获取系统统计（用于管理后台）"""
    db = get_db()
    since_24h = "-1 day"
    total_users = db.execute('SELECT COUNT(*) as c FROM users').fetchone()['c']
    total_accounts = db.execute('SELECT COUNT(*) as c FROM accounts').fetchone()['c']
    total_history = db.execute('SELECT COUNT(*) as c FROM history').fetchone()['c']
    interpreted_history = db.execute(
        'SELECT COUNT(*) as c FROM history WHERE has_ai = 1'
    ).fetchone()['c']
    total_cache_items = db.execute('SELECT COUNT(*) as c FROM ai_cache').fetchone()['c']
    total_requests = db.execute('SELECT COUNT(*) as c FROM usage_logs').fetchone()['c']
    cache_hits = db.execute(
        'SELECT COUNT(*) as c FROM usage_logs WHERE cache_hit = 1'
    ).fetchone()['c']
    total_tokens = db.execute(
        'SELECT COALESCE(SUM(tokens_used), 0) as s FROM usage_logs'
    ).fetchone()['s']
    total_cost = db.execute(
        'SELECT COALESCE(SUM(cost_usd), 0) as s FROM usage_logs'
    ).fetchone()['s']
    ai_24h = db.execute(
        '''SELECT COUNT(*) AS requests,
                  COALESCE(SUM(tokens_used), 0) AS tokens,
                  COALESCE(SUM(cost_usd), 0) AS cost,
                  SUM(CASE WHEN cache_hit = 1 THEN 1 ELSE 0 END) AS cache_hits
           FROM usage_logs
           WHERE endpoint = '/api/interpret'
             AND created_at >= datetime('now', ?)''',
        (since_24h,)
    ).fetchone()
    accounts_24h = db.execute(
        "SELECT COUNT(*) AS c FROM accounts WHERE created_at >= datetime('now', ?)",
        (since_24h,)
    ).fetchone()['c']
    registrations_24h = db.execute(
        '''SELECT COUNT(*) AS attempts,
                  SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) AS successes,
                  SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) AS failures
           FROM registration_attempts
           WHERE created_at >= datetime('now', ?)''',
        (since_24h,)
    ).fetchone()
    recent_failure_reasons = db.execute(
        '''SELECT reason, COUNT(*) AS count
           FROM registration_attempts
           WHERE success = 0
             AND created_at >= datetime('now', ?)
           GROUP BY reason
           ORDER BY count DESC
           LIMIT 5''',
        (since_24h,)
    ).fetchall()
    db.close()
    ai_24h_requests = ai_24h['requests'] or 0
    ai_24h_cache_hits = ai_24h['cache_hits'] or 0
    invite_stats = get_invite_code_stats()
    return {
        'total_users': total_users,
        'total_accounts': total_accounts,
        'total_history': total_history,
        'interpreted_history': interpreted_history,
        'total_cache_items': total_cache_items,
        'total_requests': total_requests,
        'cache_hits': cache_hits,
        'cache_rate': f"{cache_hits}/{total_requests}" if total_requests > 0 else "0/0",
        'cache_rate_percent': round(cache_hits / total_requests * 100, 1) if total_requests else 0,
        'total_tokens': total_tokens,
        'total_cost_usd': round(total_cost, 4),
        'invite_codes': invite_stats,
        'last_24h': {
            'accounts_created': accounts_24h,
            'ai_requests': ai_24h_requests,
            'ai_tokens': ai_24h['tokens'] or 0,
            'ai_cost_usd': round(ai_24h['cost'] or 0, 4),
            'cache_hits': ai_24h_cache_hits,
            'cache_rate_percent': round(ai_24h_cache_hits / ai_24h_requests * 100, 1) if ai_24h_requests else 0,
            'registration_attempts': registrations_24h['attempts'] or 0,
            'registration_successes': registrations_24h['successes'] or 0,
            'registration_failures': registrations_24h['failures'] or 0,
            'top_registration_failure_reasons': [
                {'reason': row['reason'] or 'unknown', 'count': row['count']}
                for row in recent_failure_reasons
            ],
        },
    }


def record_registration_attempt(ip, client_id, username, success, reason=''):
    """记录注册尝试，供限流和风控使用。"""
    db = get_db()
    db.execute(
        '''INSERT INTO registration_attempts
           (ip, client_id, username, success, reason)
           VALUES (?, ?, ?, ?, ?)''',
        (
            ip or 'unknown',
            client_id or '',
            (username or '')[:32],
            1 if success else 0,
            (reason or '')[:80],
        )
    )
    db.commit()
    db.close()


def check_registration_gate(
    ip,
    client_id,
    username,
    window_minutes=60,
    max_attempts=8,
    daily_max_per_ip=2,
    daily_max_per_client=1,
):
    """检查注册频率限制，返回 (allowed, error_message)。"""
    ip = ip or 'unknown'
    client_id = client_id or ''
    db = get_db()

    if max_attempts and max_attempts > 0:
        recent_since = f'-{int(window_minutes)} minutes'
        recent_attempts = db.execute(
            '''SELECT COUNT(*) AS c
               FROM registration_attempts
               WHERE created_at >= datetime('now', ?)
                 AND (ip = ? OR (? != '' AND client_id = ?))''',
            (recent_since, ip, client_id, client_id)
        ).fetchone()['c']
        if recent_attempts >= max_attempts:
            db.close()
            return False, '注册尝试过于频繁，请稍后再试'

    if daily_max_per_ip and daily_max_per_ip > 0:
        ip_success = db.execute(
            '''SELECT COUNT(*) AS c
               FROM registration_attempts
               WHERE success = 1
                 AND ip = ?
                 AND created_at >= datetime('now', '-1 day')''',
            (ip,)
        ).fetchone()['c']
        if ip_success >= daily_max_per_ip:
            db.close()
            return False, '当前网络今日注册名额已用完，请明天再试'

    if client_id and daily_max_per_client and daily_max_per_client > 0:
        client_success = db.execute(
            '''SELECT COUNT(*) AS c
               FROM registration_attempts
               WHERE success = 1
                 AND client_id = ?
                 AND created_at >= datetime('now', '-1 day')''',
            (client_id,)
        ).fetchone()['c']
        if client_success >= daily_max_per_client:
            db.close()
            return False, '当前设备今日已注册过账号，请明天再试'

    db.close()
    return True, None


# ==================== 账号系统 ====================

def hash_password(password):
    """生成带盐密码哈希。"""
    return generate_password_hash(password)


def verify_password(password, stored_hash):
    """校验新式 Werkzeug 哈希，并兼容早期 SHA256 账号。"""
    if not stored_hash:
        return False
    if stored_hash.startswith(('scrypt:', 'pbkdf2:')):
        return check_password_hash(stored_hash, password)
    legacy = hashlib.sha256(password.encode()).hexdigest()
    return secrets.compare_digest(stored_hash, legacy)


def seed_invite_codes(codes, max_uses=1, note=''):
    """批量写入体验码；重复 code 会被忽略。"""
    cleaned = []
    for code in codes:
        text = (code or '').strip()
        if text and text not in cleaned:
            cleaned.append(text)
    if not cleaned:
        return 0
    max_uses = max(1, int(max_uses or 1))
    db = get_db()
    before = db.total_changes
    db.executemany(
        '''INSERT OR IGNORE INTO invite_codes (code, max_uses, note)
           VALUES (?, ?, ?)''',
        [(code, max_uses, (note or '')[:120]) for code in cleaned]
    )
    inserted = db.total_changes - before
    db.commit()
    db.close()
    return inserted


def has_invite_codes():
    """是否配置过数据库体验码；即使用完也仍要求体验码。"""
    db = get_db()
    count = db.execute('SELECT COUNT(*) AS c FROM invite_codes').fetchone()['c']
    db.close()
    return count > 0


def has_available_invite_codes():
    """是否存在仍可使用的数据库体验码。"""
    db = get_db()
    count = db.execute(
        '''SELECT COUNT(*) AS c
           FROM invite_codes
           WHERE disabled = 0 AND used_count < max_uses'''
    ).fetchone()['c']
    db.close()
    return count > 0


def is_invite_code_available(code):
    """检查单个数据库体验码是否可用。"""
    code = (code or '').strip()
    if not code:
        return False
    db = get_db()
    row = db.execute(
        '''SELECT code
           FROM invite_codes
           WHERE code = ?
             AND disabled = 0
             AND used_count < max_uses''',
        (code,)
    ).fetchone()
    db.close()
    return row is not None


def disable_invite_codes(codes):
    """禁用一批体验码，返回实际命中的数量。"""
    cleaned = []
    for code in codes:
        text = (code or '').strip()
        if text and text not in cleaned:
            cleaned.append(text)
    if not cleaned:
        return 0
    db = get_db()
    before = db.total_changes
    db.executemany(
        'UPDATE invite_codes SET disabled = 1 WHERE code = ?',
        [(code,) for code in cleaned]
    )
    changed = db.total_changes - before
    db.commit()
    db.close()
    return changed


def get_invite_code_stats():
    """体验码运营统计。"""
    db = get_db()
    row = db.execute(
        '''SELECT COUNT(*) AS total,
                  SUM(CASE WHEN disabled = 0 AND used_count < max_uses THEN 1 ELSE 0 END) AS available,
                  SUM(CASE WHEN used_count >= max_uses THEN 1 ELSE 0 END) AS used_up,
                  SUM(CASE WHEN disabled = 1 THEN 1 ELSE 0 END) AS disabled
           FROM invite_codes'''
    ).fetchone()
    db.close()
    return {
        'total': row['total'] or 0,
        'available': row['available'] or 0,
        'used_up': row['used_up'] or 0,
        'disabled': row['disabled'] or 0,
    }


def register(username, password, invite_code=None):
    """注册新账号，返回 (token, error)"""
    db = get_db()
    invite_code = (invite_code or '').strip()
    # 检查用户名是否已存在
    existing = db.execute('SELECT id FROM accounts WHERE username = ?', (username,)).fetchone()
    if existing:
        db.close()
        return None, '用户名已存在'
    # 用户名长度限制
    if len(username) < 2 or len(username) > 20:
        db.close()
        return None, '用户名需2-20个字符'
    if len(password) < 4:
        db.close()
        return None, '密码至少4个字符'
    if invite_code:
        invite = db.execute(
            '''SELECT code
               FROM invite_codes
               WHERE code = ?
                 AND disabled = 0
                 AND used_count < max_uses''',
            (invite_code,)
        ).fetchone()
        if not invite:
            db.close()
            return None, '体验码已被使用或不存在'
    # 创建账号
    pw_hash = hash_password(password)
    cursor = db.execute(
        'INSERT INTO accounts (username, password_hash) VALUES (?, ?)',
        (username, pw_hash)
    )
    account_id = cursor.lastrowid
    # 生成会话token
    token = secrets.token_hex(24)
    fingerprint = f"acct:{account_id}"
    db.execute(
        'INSERT INTO sessions (token, account_id, fingerprint, expires_at) VALUES (?, ?, ?, ?)',
        (token, account_id, fingerprint, (datetime.now() + timedelta(days=365)).isoformat())
    )
    if invite_code:
        db.execute(
            '''UPDATE invite_codes
               SET used_count = used_count + 1,
                   used_at = datetime('now'),
                   used_by_account_id = ?
               WHERE code = ?''',
            (account_id, invite_code)
        )
    db.commit()
    db.close()
    return token, None


def login(username, password):
    """登录，返回 (token, error)"""
    db = get_db()
    account = db.execute(
        'SELECT * FROM accounts WHERE username = ?',
        (username,)
    ).fetchone()
    if not account or not verify_password(password, account['password_hash']):
        db.close()
        return None, '用户名或密码错误'
    token = secrets.token_hex(24)
    fingerprint = f"acct:{account['id']}"
    db.execute(
        'INSERT INTO sessions (token, account_id, fingerprint, expires_at) VALUES (?, ?, ?, ?)',
        (token, account['id'], fingerprint, (datetime.now() + timedelta(days=365)).isoformat())
    )
    if not account['password_hash'].startswith(('scrypt:', 'pbkdf2:')):
        db.execute(
            'UPDATE accounts SET password_hash = ? WHERE id = ?',
            (hash_password(password), account['id'])
        )
    db.commit()
    db.close()
    return token, None


def get_account_by_token(token):
    """通过token获取账号信息"""
    if not token:
        return None
    db = get_db()
    session = db.execute(
        '''SELECT s.*, a.username, a.free_trials_used, a.credits
           FROM sessions s
           JOIN accounts a ON s.account_id = a.id
           WHERE s.token = ?''',
        (token,)
    ).fetchone()
    db.close()
    if not session:
        return None
    # 检查是否过期
    if session['expires_at']:
        try:
            expires = datetime.fromisoformat(session['expires_at'])
            if datetime.now() > expires:
                return None
        except Exception:
            pass
    return dict(session)


def logout(token):
    """注销"""
    if not token:
        return
    db = get_db()
    db.execute('DELETE FROM sessions WHERE token = ?', (token,))
    db.commit()
    db.close()


def get_account_fingerprint(token):
    """通过token获取账号指纹（acct:xxx 格式）"""
    account = get_account_by_token(token)
    if account:
        return account['fingerprint']
    return None


def check_quota_account(token):
    """检查账号配额，返回 (can_use, free_remaining, is_free)"""
    account = get_account_by_token(token)
    if not account:
        return None
    free_used = account['free_trials_used']
    free_remaining = max(0, 5 - free_used)
    credits = account['credits'] if account['credits'] else 0
    if free_remaining > 0:
        return True, free_remaining, True
    elif credits > 0:
        return True, 0, False
    else:
        return False, 0, False


def consume_quota_account(token, is_free):
    """消耗账号配额"""
    account = get_account_by_token(token)
    if not account:
        return
    db = get_db()
    if is_free:
        db.execute(
            'UPDATE accounts SET free_trials_used = free_trials_used + 1 WHERE id = ?',
            (account['account_id'],)
        )
    else:
        db.execute(
            'UPDATE accounts SET credits = credits - 1 WHERE id = ?',
            (account['account_id'],)
        )
    db.commit()
    db.close()


# ==================== chat 链路（agent-plan v4.1 §2.1/§5） ====================

def build_chat_thread_id(fingerprint, hid):
    """会话粒度：账号指纹 + 命盘 id。服务端拼接，客户端不可注入（§4.5）。"""
    return f'{fingerprint}:{hid}'


def consume_chat_quota(hid, fingerprint, price, limit=None):
    """gate 时原子扣减 price 炷香火（persona-plan §3.1）。

    UPDATE 的 WHERE used + price <= limit 条件使"检查+扣减"成为一条原子语句，
    多进程并发下不可能双花（spike 3 的防双花结论对任意 price 成立）。
    price 来自人格注册表白名单（≥1）；非法值属编程错误，抛错不静默。
    返回 (是否成功, 扣减后已用炷数)。
    """
    limit = CHAT_FREE_INCENSE if limit is None else limit
    price = int(price)
    if price < 1:
        raise ValueError(f'香火定价必须 ≥ 1，收到 {price!r}')
    db = get_db()
    try:
        db.execute(
            'INSERT OR IGNORE INTO chat_followups (hid, fingerprint, used) VALUES (?, ?, 0)',
            (hid, fingerprint)
        )
        cur = db.execute(
            '''UPDATE chat_followups SET used = used + ?
               WHERE hid = ? AND fingerprint = ? AND used + ? <= ?''',
            (price, hid, fingerprint, price, limit)
        )
        ok = cur.rowcount == 1
        # 同事务内读取（提交前），返回值即本次扣减结果，不受并发后续写影响
        used = db.execute(
            'SELECT used FROM chat_followups WHERE hid = ? AND fingerprint = ?',
            (hid, fingerprint)
        ).fetchone()['used']
        db.commit()
        return ok, used
    finally:
        db.close()


def refund_chat_quota(hid, fingerprint, price):
    """失败退款：按实扣退 price 炷（唯一调用点在 /api/chat 异常分支）。

    used >= price 守卫：重复退款或超额退款返回 False，
    保证 used 不会因退款而被透支成负数。
    """
    price = int(price)
    if price < 1:
        raise ValueError(f'退款香火必须 ≥ 1，收到 {price!r}')
    db = get_db()
    try:
        cur = db.execute(
            '''UPDATE chat_followups SET used = used - ?
               WHERE hid = ? AND fingerprint = ? AND used >= ?''',
            (price, hid, fingerprint, price)
        )
        db.commit()
        return cur.rowcount == 1
    finally:
        db.close()


def get_chat_quota_left(hid, fingerprint, limit=None):
    """剩余香火（炷数）。"""
    limit = CHAT_FREE_INCENSE if limit is None else limit
    db = get_db()
    try:
        row = db.execute(
            'SELECT used FROM chat_followups WHERE hid = ? AND fingerprint = ?',
            (hid, fingerprint)
        ).fetchone()
        used = row['used'] if row else 0
        return max(0, limit - used)
    finally:
        db.close()


def save_chat_reply(thread_id, request_id, reply_text, quota_left, price, persona):
    """落幂等表：首次写入后不再覆盖（INSERT OR IGNORE）。

    语义约定：同一 (thread_id, request_id) 的回复只落一次库；
    之后重发一律走 get_chat_reply 回放，绝不改写历史回复。
    price/persona 记录本次实扣与所请教道长（回放事件的账目依据）。
    """
    db = get_db()
    try:
        db.execute(
            '''INSERT OR IGNORE INTO chat_requests
               (thread_id, request_id, reply_text, quota_left, price, persona)
               VALUES (?, ?, ?, ?, ?, ?)''',
            (thread_id, request_id, reply_text, quota_left, price, persona)
        )
        db.commit()
    finally:
        db.close()


def get_chat_reply(thread_id, request_id):
    """查幂等命中：返回 dict（含 price/persona）或 None（None = 未见过该 request_id）。"""
    db = get_db()
    try:
        row = db.execute(
            '''SELECT reply_text, quota_left, price, persona, created_at
               FROM chat_requests WHERE thread_id = ? AND request_id = ?''',
            (thread_id, request_id)
        ).fetchone()
        return dict(row) if row else None
    finally:
        db.close()


def get_last_chat_persona(thread_id):
    """该 thread 最近一次落账的道长（/api/chat/personas 的 current 标注依据）。

    表是复合主键 (thread_id, request_id)、无自增 id，但 rowid 表的隐式
    rowid 随插入单调递增，"最近一条"用它排序即可。
    无任何对话记录返回 None，路由层回落缺省人格。
    幂等表只记成功回答的请求，失败退款不落账——所以"最近落账"即
    "最近一次真实请教的道长"，与用户心智一致。
    """
    db = get_db()
    try:
        row = db.execute(
            '''SELECT persona FROM chat_requests
               WHERE thread_id = ? ORDER BY rowid DESC LIMIT 1''',
            (thread_id,)
        ).fetchone()
        return row['persona'] if row else None
    finally:
        db.close()


def delete_chat_cascade(fingerprint, hid):
    """删盘级联：清掉该命盘的追问额度行，并按精确 thread_id 删除对话幂等记录。

    当前会话模型 thread_id == fingerprint:hid（一盘一会话），精确匹配即完整级联；
    checkpoint 线程由调用方（/api/history DELETE）另行删除。
    生辰数据是隐私敏感信息，删了盘却留着完整对话是合规问题（§5 P0）。
    返回删除的总行数。
    """
    thread_id = build_chat_thread_id(fingerprint, hid)
    db = get_db()
    try:
        c1 = db.execute(
            'DELETE FROM chat_followups WHERE hid = ? AND fingerprint = ?',
            (hid, fingerprint)
        ).rowcount
        c2 = db.execute(
            'DELETE FROM chat_requests WHERE thread_id = ?',
            (thread_id,)
        ).rowcount
        db.commit()
        return c1 + c2
    finally:
        db.close()


# 初始化
init_db()
