"""
账号与会话管理 — 多用户隔离的鉴权基础。

密码使用标准库 scrypt 加盐哈希存储；会话为数据库中的随机令牌，
由 HttpOnly Cookie 承载，支持注销与过期。
"""
from __future__ import annotations

import datetime
import hashlib
import logging
import re
import secrets
from typing import Optional

import pymysql

from config import SESSION_TTL_DAYS

logger = logging.getLogger(__name__)

USERNAME_RE = re.compile(r"^[A-Za-z0-9_\-\u4e00-\u9fff]{2,32}$")
PASSWORD_MIN_LEN = 6
PASSWORD_MAX_LEN = 128

_SCRYPT_N = 16384
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32


def hash_password(password: str) -> str:
    """生成 `scrypt$N$r$p$salt$hash` 格式的密码哈希。"""
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(
        password.encode("utf-8"), salt=salt,
        n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_SCRYPT_DKLEN,
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """校验密码是否与哈希匹配（恒定时间比较）。"""
    try:
        scheme, n, r, p, salt_hex, hash_hex = (stored or "").split("$")
        if scheme != "scrypt":
            return False
        expected = bytes.fromhex(hash_hex)
        dk = hashlib.scrypt(
            password.encode("utf-8"), salt=bytes.fromhex(salt_hex),
            n=int(n), r=int(r), p=int(p), dklen=len(expected),
        )
        return secrets.compare_digest(dk, expected)
    except (ValueError, TypeError):
        return False


# 用户不存在时仍走一次相同耗时的哈希，避免按响应时间枚举用户名
_DUMMY_PASSWORD_HASH = hash_password("__nehchat_dummy__")


def validate_username(username: str) -> str:
    u = (username or "").strip()
    if not USERNAME_RE.match(u):
        raise ValueError("用户名需为 2-32 位字母、数字、下划线、连字符或中文")
    return u


def validate_password(password: str) -> str:
    if not password or len(password) < PASSWORD_MIN_LEN:
        raise ValueError(f"密码至少需要 {PASSWORD_MIN_LEN} 位")
    if len(password) > PASSWORD_MAX_LEN:
        raise ValueError(f"密码不能超过 {PASSWORD_MAX_LEN} 位")
    return password


class AccountManager:
    """用户与会话的数据库访问；复用 SlotManager 的连接池。"""

    def __init__(self, pool):
        self.pool = pool
        self._init_tables()

    def _init_tables(self):
        conn = self.pool.connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS users (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        username VARCHAR(32) NOT NULL UNIQUE,
                        password_hash VARCHAR(256) NOT NULL,
                        is_admin TINYINT(1) NOT NULL DEFAULT 0,
                        created_at VARCHAR(32) DEFAULT ''
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """)
                cursor.execute("SHOW COLUMNS FROM `users` LIKE 'is_admin'")
                if not cursor.fetchone():
                    cursor.execute(
                        "ALTER TABLE `users` ADD COLUMN `is_admin` TINYINT(1) NOT NULL DEFAULT 0 AFTER `password_hash`"
                    )
                    logger.info("已添加 users.is_admin 列")
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS sessions (
                        token CHAR(64) PRIMARY KEY,
                        user_id INT NOT NULL,
                        created_at VARCHAR(32) DEFAULT '',
                        expires_at VARCHAR(32) DEFAULT '',
                        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                        INDEX idx_sessions_user (user_id)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """)
                self._ensure_admin(cursor)
            conn.commit()
            logger.info("用户与会话表初始化完成")
        except pymysql.Error as e:
            conn.rollback()
            logger.error(f"用户与会话表初始化失败: {e}")
            raise
        finally:
            conn.close()

    @staticmethod
    def _now() -> datetime.datetime:
        return datetime.datetime.now()

    @staticmethod
    def _ensure_admin(cursor) -> None:
        """若尚无管理员，将最早创建的账号提升为管理员。"""
        cursor.execute("SELECT COUNT(*) AS c FROM users WHERE is_admin = 1")
        if (cursor.fetchone() or {}).get("c", 0) > 0:
            return
        cursor.execute("SELECT id FROM users ORDER BY id ASC LIMIT 1")
        row = cursor.fetchone()
        if not row:
            return
        cursor.execute("UPDATE users SET is_admin = 1 WHERE id = %s", (row["id"],))
        logger.info(f"已将最早账号 #{row['id']} 设为管理员")

    @staticmethod
    def public_user(row: dict, *, include_meta: bool = False) -> dict:
        data = {
            "id": int(row["id"]),
            "username": row["username"],
            "is_admin": bool(row.get("is_admin")),
        }
        if include_meta:
            data["created_at"] = row.get("created_at") or ""
            data["slot_count"] = int(row.get("slot_count") or 0)
        return data

    def has_users(self) -> bool:
        conn = self.pool.connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute("SELECT COUNT(*) AS c FROM users")
                return (cursor.fetchone() or {}).get("c", 0) > 0
        finally:
            conn.close()

    def create_user(self, username: str, password: str, is_admin: bool = False) -> dict:
        """创建账号；用户名重复时抛出 ValueError("duplicate")。"""
        u = validate_username(username)
        p = validate_password(password)
        admin_flag = 1 if is_admin else 0
        conn = self.pool.connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO users (username, password_hash, is_admin, created_at) VALUES (%s, %s, %s, %s)",
                    (u, hash_password(p), admin_flag, self._now().isoformat()),
                )
                user_id = cursor.lastrowid
            conn.commit()
            return {"id": user_id, "username": u, "is_admin": bool(is_admin)}
        except pymysql.IntegrityError:
            conn.rollback()
            raise ValueError("duplicate")
        except pymysql.Error as e:
            conn.rollback()
            logger.error(f"创建用户失败: {e}")
            raise RuntimeError(f"创建账号失败: {e}")
        finally:
            conn.close()

    def _get_user(self, where: str, value) -> Optional[dict]:
        conn = self.pool.connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    f"SELECT id, username, password_hash, is_admin, created_at FROM users WHERE {where} = %s",
                    (value,),
                )
                return cursor.fetchone()
        finally:
            conn.close()

    def get_user_by_username(self, username: str) -> Optional[dict]:
        return self._get_user("username", (username or "").strip())

    def get_user_by_id(self, user_id: int) -> Optional[dict]:
        return self._get_user("id", user_id)

    def authenticate(self, username: str, password: str) -> Optional[dict]:
        """校验用户名密码，成功返回 {id, username}，失败返回 None。"""
        row = self.get_user_by_username(username)
        stored = (row.get("password_hash") if row else "") or _DUMMY_PASSWORD_HASH
        ok = verify_password(password, stored)
        if not row or not ok:
            return None
        return self.public_user(row)

    def create_session(self, user_id: int) -> str:
        token = secrets.token_urlsafe(32)
        now = self._now()
        expires = now + datetime.timedelta(days=SESSION_TTL_DAYS)
        conn = self.pool.connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO sessions (token, user_id, created_at, expires_at) VALUES (%s, %s, %s, %s)",
                    (token, user_id, now.isoformat(), expires.isoformat()),
                )
            conn.commit()
            return token
        except pymysql.Error as e:
            conn.rollback()
            logger.error(f"创建会话失败: {e}")
            raise RuntimeError(f"创建会话失败: {e}")
        finally:
            conn.close()

    def resolve_session(self, token: str) -> Optional[dict]:
        """根据令牌返回当前用户；过期会话会被清理。"""
        if not token:
            return None
        conn = self.pool.connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT s.user_id, s.expires_at, u.username, u.is_admin "
                    "FROM sessions s JOIN users u ON u.id = s.user_id "
                    "WHERE s.token = %s",
                    (token,),
                )
                row = cursor.fetchone()
                if not row:
                    return None
                try:
                    expires = datetime.datetime.fromisoformat(row.get("expires_at") or "")
                except ValueError:
                    expires = self._now()
                if expires <= self._now():
                    cursor.execute("DELETE FROM sessions WHERE token = %s", (token,))
                    conn.commit()
                    return None
                return {
                    "id": row["user_id"],
                    "username": row["username"],
                    "is_admin": bool(row.get("is_admin")),
                }
        finally:
            conn.close()

    def delete_session(self, token: str) -> None:
        if not token:
            return
        conn = self.pool.connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute("DELETE FROM sessions WHERE token = %s", (token,))
            conn.commit()
        except pymysql.Error as e:
            conn.rollback()
            logger.warning(f"删除会话失败: {e}")
        finally:
            conn.close()

    def list_users(self) -> list[dict]:
        """列出全部账号（不含密码），附带各账号存档数量。"""
        conn = self.pool.connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT u.id, u.username, u.is_admin, u.created_at, "
                    "(SELECT COUNT(*) FROM slots s WHERE s.user_id = u.id) AS slot_count "
                    "FROM users u ORDER BY u.is_admin DESC, u.id ASC"
                )
                return [self.public_user(row, include_meta=True) for row in (cursor.fetchall() or [])]
        finally:
            conn.close()

    def count_admins(self) -> int:
        conn = self.pool.connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute("SELECT COUNT(*) AS c FROM users WHERE is_admin = 1")
                return int((cursor.fetchone() or {}).get("c", 0) or 0)
        finally:
            conn.close()

    def set_password(self, user_id: int, password: str) -> bool:
        p = validate_password(password)
        conn = self.pool.connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    "UPDATE users SET password_hash = %s WHERE id = %s",
                    (hash_password(p), user_id),
                )
                updated = cursor.rowcount > 0
            conn.commit()
            return updated
        except pymysql.Error as e:
            conn.rollback()
            logger.error(f"更新密码失败: {e}")
            raise RuntimeError(f"更新密码失败: {e}")
        finally:
            conn.close()

    def set_admin(self, user_id: int, is_admin: bool) -> bool:
        """切换管理员身份；撤销时若已是最后一位管理员则抛出 ValueError("last_admin")。"""
        conn = self.pool.connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT id, is_admin FROM users WHERE id = %s",
                    (user_id,),
                )
                row = cursor.fetchone()
                if not row:
                    return False
                currently_admin = bool(row.get("is_admin"))
                if currently_admin and not is_admin:
                    cursor.execute("SELECT COUNT(*) AS c FROM users WHERE is_admin = 1")
                    if int((cursor.fetchone() or {}).get("c", 0) or 0) <= 1:
                        raise ValueError("last_admin")
                cursor.execute(
                    "UPDATE users SET is_admin = %s WHERE id = %s",
                    (1 if is_admin else 0, user_id),
                )
            conn.commit()
            return True
        except ValueError:
            conn.rollback()
            raise
        except pymysql.Error as e:
            conn.rollback()
            logger.error(f"更新管理员身份失败: {e}")
            raise RuntimeError(f"更新管理员身份失败: {e}")
        finally:
            conn.close()

    def delete_user_sessions(self, user_id: int) -> None:
        conn = self.pool.connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute("DELETE FROM sessions WHERE user_id = %s", (user_id,))
            conn.commit()
        except pymysql.Error as e:
            conn.rollback()
            logger.warning(f"清理用户会话失败: {e}")
        finally:
            conn.close()

    def delete_user(self, user_id: int) -> bool:
        """删除账号及其存档、供应商与会话。"""
        conn = self.pool.connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute("DELETE FROM uploads WHERE user_id = %s", (user_id,))
                cursor.execute("DELETE FROM messages WHERE user_id = %s", (user_id,))
                cursor.execute("DELETE FROM slots WHERE user_id = %s", (user_id,))
                cursor.execute("DELETE FROM providers WHERE user_id = %s", (user_id,))
                cursor.execute("DELETE FROM users WHERE id = %s", (user_id,))
                deleted = cursor.rowcount > 0
            conn.commit()
            return deleted
        except pymysql.Error as e:
            conn.rollback()
            logger.error(f"删除用户失败: {e}")
            raise RuntimeError(f"删除用户失败: {e}")
        finally:
            conn.close()

    def claim_legacy_data(self, user_id: int) -> None:
        """把旧版全局数据（user_id = 0）归入指定账号。"""
        conn = self.pool.connection()
        try:
            with conn.cursor() as cursor:
                for table in ("slots", "messages", "providers"):
                    cursor.execute(
                        f"UPDATE `{table}` SET user_id = %s WHERE user_id = 0",
                        (user_id,),
                    )
            conn.commit()
            logger.info(f"已将旧版全局数据归入账号 #{user_id}")
        except pymysql.Error as e:
            conn.rollback()
            logger.error(f"迁移旧数据失败: {e}")
            raise
        finally:
            conn.close()
