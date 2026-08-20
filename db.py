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

DB_PATH = os.environ.get('DB_PATH', os.path.join(os.path.dirname(__file__), 'xuanjige.db'))


def get_db():
    """获取数据库连接"""
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
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
    ''')
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
    """保存排盘历史"""
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
    else:
        db.execute(
            '''INSERT INTO history (fingerprint, name, gender, solar_date, paipan_json, has_ai)
               VALUES (?, ?, ?, ?, ?, ?)''',
            (fingerprint, name, gender, solar_date, paipan_json, has_ai)
        )
    db.commit()
    db.close()


def get_history(fingerprint):
    """获取用户排盘历史"""
    db = get_db()
    rows = db.execute(
        '''SELECT id, name, gender, solar_date, has_ai, created_at
           FROM history WHERE fingerprint = ?
           ORDER BY created_at DESC''',
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
    total_users = db.execute('SELECT COUNT(*) as c FROM users').fetchone()['c']
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
    db.close()
    return {
        'total_users': total_users,
        'total_requests': total_requests,
        'cache_hits': cache_hits,
        'cache_rate': f"{cache_hits}/{total_requests}" if total_requests > 0 else "0/0",
        'total_tokens': total_tokens,
        'total_cost_usd': round(total_cost, 4),
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
    """密码哈希"""
    return hashlib.sha256(password.encode()).hexdigest()


def register(username, password):
    """注册新账号，返回 (token, error)"""
    db = get_db()
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
    db.commit()
    db.close()
    return token, None


def login(username, password):
    """登录，返回 (token, error)"""
    db = get_db()
    account = db.execute(
        'SELECT * FROM accounts WHERE username = ? AND password_hash = ?',
        (username, hash_password(password))
    ).fetchone()
    if not account:
        db.close()
        return None, '用户名或密码错误'
    token = secrets.token_hex(24)
    fingerprint = f"acct:{account['id']}"
    db.execute(
        'INSERT INTO sessions (token, account_id, fingerprint, expires_at) VALUES (?, ?, ?, ?)',
        (token, account['id'], fingerprint, (datetime.now() + timedelta(days=365)).isoformat())
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


# 初始化
init_db()
