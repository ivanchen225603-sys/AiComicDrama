"""账户与会话凭证：与业务数据落在**同一个 drama.sqlite**，绝不另起 DB 文件。

边界（写进代码以免后来者好心办坏事）：
- `drama_accounts` / `drama_sessions` 属于身份凭证，不进 `export_doc/import_doc`
  （密码哈希、会话令牌跨库导出会泄露身份），也不进 `store.reset()` 的 DELETE 清单
  （它们是账户与账务凭证，不随演示数据重置）。见 `store.py` 同名注释。
- 所有 SELECT / DELETE 一律带 owner 条件：同一份库里可能有多个账户。
- 注册一律 `role='user'`。管理员能力**仍由 settings.credit_admins 白名单决定**
  （`DramaService._require_admin` 不改），本期不提供任何自助提权接口；保留 role 列
  只是为了后续扩展。这样离线 `owner="local"` 与既有管理员路径完全不受影响。
- 会话令牌只以 sha256 摘要入库，明文只在 Set-Cookie 里出现一次。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import sqlite3
import time
from typing import TYPE_CHECKING, Any

from .errors import AppError
from .models import ID_PATTERN

if TYPE_CHECKING:
    from .store import DramaStore

# 建表语句挂在 store._SCHEMA 上，由 DramaStore.install() 统一执行；这样新库与
# 老库（_migrate 逐列补列）走的是同一条安装路径，不会出现「业务表有、账户表没有」。
ACCOUNT_SCHEMA: tuple[str, ...] = (
    "CREATE TABLE IF NOT EXISTS drama_accounts("
    "username TEXT PRIMARY KEY,owner TEXT UNIQUE NOT NULL,password_hash TEXT NOT NULL,"
    "salt TEXT NOT NULL,role TEXT NOT NULL DEFAULT 'user',"
    "failed_count INTEGER NOT NULL DEFAULT 0,locked_until REAL NOT NULL DEFAULT 0,"
    "created_at REAL NOT NULL)",
    "CREATE TABLE IF NOT EXISTS drama_sessions("
    "token_hash TEXT PRIMARY KEY,owner TEXT NOT NULL,expires_at REAL NOT NULL,"
    "created_at REAL NOT NULL)",
    "CREATE INDEX IF NOT EXISTS drama_sessions_owner ON drama_sessions(owner)",
)

SESSION_COOKIE = "drama_session"
SESSION_TTL_SECONDS = 24 * 60 * 60
MAX_FAILED_ATTEMPTS = 5
LOCK_SECONDS = 15 * 60
PBKDF2_ROUNDS = 200_000
SALT_BYTES = 16
USERNAME_MIN_LENGTH = 3
USERNAME_MAX_LENGTH = 64
PASSWORD_MIN_LENGTH = 8
PASSWORD_MAX_LENGTH = 128
RESERVED_OWNERS: frozenset[str] = frozenset({"public", "system", "local"})
_USERNAME_RE = re.compile(r"[A-Za-z0-9_.-]+")


def hash_password(password: str, salt_b64: str | None = None) -> tuple[str, str]:
    """PBKDF2-HMAC-SHA256，返回 (密码摘要 base64, salt base64)。

    salt 由 secrets.token_bytes(16) 生成并入库；摘要与 salt 都以文本存，方便 SQLite
    与导出边界做「不许带走」的静态检查。校验使用 hmac.compare_digest 防时序侧信道。
    """
    salt = (
        base64.b64decode(salt_b64.encode("ascii")) if salt_b64 else secrets.token_bytes(SALT_BYTES)
    )
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS)
    return base64.b64encode(digest).decode("ascii"), base64.b64encode(salt).decode("ascii")


def verify_password(password: str, password_hash: str, salt: str) -> bool:
    """恒定时间比较；库里被手工改坏的数据一律当成「密码不对」，不抛 500。"""
    try:
        computed, _ = hash_password(password, salt)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(computed, password_hash)


def token_digest(token: str) -> str:
    """会话令牌的 sha256 摘要。**库里只存这个，绝不存明文。**"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def new_owner() -> str:
    """生成符合 ID_PATTERN 且不是保留字的 owner。"""
    for _ in range(16):
        owner = "u_" + secrets.token_hex(8)
        if re.fullmatch(ID_PATTERN, owner) and owner not in RESERVED_OWNERS:
            return owner
    raise RuntimeError("unable to allocate a non-reserved account owner")


def validate_username(username: str) -> str:
    value = str(username or "").strip()
    if not USERNAME_MIN_LENGTH <= len(value) <= USERNAME_MAX_LENGTH or not _USERNAME_RE.fullmatch(
        value
    ):
        raise AppError("account_invalid", "账号需为 3–64 位字母、数字或 _ . -", 400)
    return value


def validate_password(password: str) -> None:
    if not PASSWORD_MIN_LENGTH <= len(str(password or "")) <= PASSWORD_MAX_LENGTH:
        raise AppError("account_invalid", "密码需为 8–128 位字符", 400)


def _row_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


class AccountStore:
    """账户 / 会话的同步访问层，复用 DramaStore 的同一连接与同一把 RLock。"""

    def __init__(self, store: DramaStore) -> None:
        self.store = store

    def register(self, username: str, password: str) -> dict[str, Any]:
        name = validate_username(username)
        validate_password(password)
        password_hash, salt = hash_password(password)
        owner = new_owner()
        now = time.time()

        def op(db: sqlite3.Connection) -> None:
            if db.execute("SELECT 1 FROM drama_accounts WHERE username=?", (name,)).fetchone():
                raise AppError("account_username_taken", "该账号已被注册，请换一个", 409)
            db.execute(
                "INSERT INTO drama_accounts(username,owner,password_hash,salt,role,"
                "failed_count,locked_until,created_at) VALUES(?,?,?,?,?,0,0,?)",
                (name, owner, password_hash, salt, "user", now),
            )

        try:
            self.store.run(op)
        except sqlite3.IntegrityError as exc:
            # 唯一索引 (username / owner) 是最后一道并发闸门，不能把 sqlite 原文抛给用户。
            raise AppError("account_username_taken", "该账号已被注册，请换一个", 409) from exc
        return {"username": name, "owner": owner, "role": "user"}

    def _verify_login(
        self, db: sqlite3.Connection, name: str, password: str, now: float
    ) -> dict[str, Any]:
        """在**一个事务里**更新失败计数，把拒绝结果作为值返回而不是抛异常。

        `DramaStore.run()` 遇到异常会 ROLLBACK；若在事务里抛「密码错误」，那 5 次
        失败计数会全部回滚，限次永远不生效。所以这里返回 {"error": AppError}，由
        调用方在事务提交之后才 raise。
        """
        row = db.execute("SELECT * FROM drama_accounts WHERE username=?", (name,)).fetchone()
        if row is None:
            return {"error": AppError("account_credentials_invalid", "账号或密码不正确", 401)}
        locked_until = float(row["locked_until"] or 0)
        failed = int(row["failed_count"] or 0)
        if locked_until > now:
            return {"error": AppError("account_locked", "尝试次数过多，请 15 分钟后再试", 429)}
        if locked_until and locked_until <= now:
            # 锁定期已过：恢复成一个干净的计数窗口，否则「恢复后错一次立刻又锁 15 分钟」。
            failed = 0
            db.execute(
                "UPDATE drama_accounts SET failed_count=0, locked_until=0 WHERE username=?",
                (name,),
            )
        if not verify_password(password, str(row["password_hash"]), str(row["salt"])):
            new_failed = failed + 1
            new_locked = now + LOCK_SECONDS if new_failed >= MAX_FAILED_ATTEMPTS else 0.0
            db.execute(
                "UPDATE drama_accounts SET failed_count=?, locked_until=? WHERE username=?",
                (new_failed, new_locked, name),
            )
            return {"error": AppError("account_credentials_invalid", "账号或密码不正确", 401)}
        # 成功清零。
        db.execute(
            "UPDATE drama_accounts SET failed_count=0, locked_until=0 WHERE username=?", (name,)
        )
        return {"error": None, "row": _row_dict(row)}

    def login(
        self, username: str, password: str, ttl: float | None = None
    ) -> tuple[str, dict[str, Any]]:
        name = validate_username(username)
        now = time.time()
        checked = self.store.run(lambda db: self._verify_login(db, name, password, now))
        error = checked.get("error")
        if error is not None:
            raise error
        row = checked["row"]
        owner = str(row["owner"])
        token = new_session_token()
        expires_at = now + (SESSION_TTL_SECONDS if ttl is None else float(ttl))

        def op(db: sqlite3.Connection) -> None:
            # 顺手清掉过期会话：否则这张表只增不减，而它又不能随演示数据 reset。
            # 清理与插入在同一个事务里，避免「刚清完又插入失败」留下空窗。
            db.execute("DELETE FROM drama_sessions WHERE expires_at<=?", (now,))
            db.execute(
                "INSERT INTO drama_sessions(token_hash,owner,expires_at,created_at) "
                "VALUES(?,?,?,?)",
                (token_digest(token), owner, expires_at, now),
            )

        self.store.run(op)
        return token, {"username": name, "owner": owner, "role": str(row["role"])}

    def owner_for_session(self, token: str) -> str | None:
        """有效且未过期的会话返回账户 owner；否则返回 None（回退到既有 local/bearer）。"""
        if not token:
            return None
        digest = token_digest(token)
        now = time.time()

        def op(db: sqlite3.Connection) -> str | None:
            row = db.execute(
                "SELECT owner,expires_at FROM drama_sessions WHERE token_hash=?", (digest,)
            ).fetchone()
            if row is None or float(row["expires_at"]) <= now:
                return None
            return str(row["owner"])

        try:
            return self.store.read(op)
        except sqlite3.OperationalError:
            # install() 之前收到带 cookie 的请求（例如健康检查）：表还没建好时
            # 不能让整站 500，按「无有效会话」回退即可。
            return None

    def logout(self, token: str) -> None:
        if not token:
            return
        digest = token_digest(token)
        self.store.run(
            lambda db: db.execute("DELETE FROM drama_sessions WHERE token_hash=?", (digest,))
        )

    def change_password(self, token: str, old_password: str, new_password: str) -> dict[str, Any]:
        validate_password(new_password)
        digest = token_digest(token)
        now = time.time()

        def op(db: sqlite3.Connection) -> dict[str, Any]:
            session = db.execute(
                "SELECT owner,expires_at FROM drama_sessions WHERE token_hash=?", (digest,)
            ).fetchone()
            if session is None or float(session["expires_at"]) <= now:
                return {"error": AppError("account_session_required", "请先登录", 401)}
            owner = str(session["owner"])
            account = db.execute(
                "SELECT password_hash,salt FROM drama_accounts WHERE owner=?", (owner,)
            ).fetchone()
            if account is None or not verify_password(
                old_password, str(account["password_hash"]), str(account["salt"])
            ):
                return {"error": AppError("account_credentials_invalid", "原密码不正确", 401)}
            password_hash, salt = hash_password(new_password)
            db.execute(
                "UPDATE drama_accounts SET password_hash=?,salt=?,failed_count=0,locked_until=0 "
                "WHERE owner=?",
                (password_hash, salt, owner),
            )
            # 只保留当前会话，该 owner 的其它会话一律失效。
            db.execute(
                "DELETE FROM drama_sessions WHERE owner=? AND token_hash<>?", (owner, digest)
            )
            return {"error": None, "owner": owner}

        result = self.store.run(op)
        error = result.get("error")
        if error is not None:
            raise error
        return {"owner": str(result["owner"]), "changed": True}

    def me(self, token: str, admins: frozenset[str]) -> dict[str, Any]:
        owner = self.owner_for_session(token)
        if owner is None:
            raise AppError("account_session_required", "请先登录", 401)
        row = self.store.read(
            lambda db: db.execute(
                "SELECT owner,role FROM drama_accounts WHERE owner=?", (owner,)
            ).fetchone()
        )
        role = str(row["role"]) if row is not None else "user"
        # 管理员能力只由白名单决定，与 role 列无关：不提供自助提权接口。
        return {"owner": owner, "role": role, "admin": owner in admins}
