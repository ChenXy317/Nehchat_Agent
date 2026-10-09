"""
Slot Manager — 基于 MySQL 的对话存档存储实现。

提供存档的增删改查、消息追加与删除等数据库操作。
多用户隔离：slots / messages / providers 全部按 user_id 归属，
主键与唯一键均为 (user_id, ...) 复合形式；旧库在启动时自动迁移。
"""
import datetime
import json
import logging
from contextlib import contextmanager
from typing import List, Dict, Optional

import pymysql
from dbutils.pooled_db import PooledDB

from config import SLOT_COUNT
from config import MYSQL_HOST, MYSQL_PORT, MYSQL_USER, MYSQL_PASSWORD
from config import MYSQL_DATABASE, MYSQL_CHARSET

logger = logging.getLogger(__name__)


def _get_connection(pool):
    """安全地从连接池获取连接，失败时统一处理。"""
    try:
        return pool.connection()
    except pymysql.Error as e:
        logger.error(f"无法从连接池获取数据库连接: {e}")
        raise RuntimeError(f"数据库连接池获取连接失败: {e}")
    except Exception as e:
        logger.error(f"获取数据库连接时发生未知错误: {e}")
        raise RuntimeError(f"数据库连接异常: {e}")


def _safe_rollback(conn):
    """安全回滚，避免在异常处理中二次抛出异常。"""
    if conn:
        try:
            conn.rollback()
        except Exception:
            pass


def _column_exists(cursor, table: str, column: str) -> bool:
    cursor.execute(f"SHOW COLUMNS FROM `{table}` LIKE %s", (column,))
    return cursor.fetchone() is not None


def _primary_key_columns(cursor, table: str) -> List[str]:
    cursor.execute(
        "SELECT COLUMN_NAME FROM information_schema.STATISTICS "
        "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s AND INDEX_NAME = 'PRIMARY' "
        "ORDER BY SEQ_IN_INDEX",
        (MYSQL_DATABASE, table),
    )
    return [r["COLUMN_NAME"] for r in cursor.fetchall() or []]


def _foreign_key_to(cursor, table: str, ref_table: str) -> Optional[str]:
    cursor.execute(
        "SELECT CONSTRAINT_NAME FROM information_schema.KEY_COLUMN_USAGE "
        "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s AND REFERENCED_TABLE_NAME = %s",
        (MYSQL_DATABASE, table, ref_table),
    )
    row = cursor.fetchone()
    return row["CONSTRAINT_NAME"] if row else None


def _index_columns(cursor, table: str, index_name: str) -> List[str]:
    cursor.execute(
        "SELECT COLUMN_NAME FROM information_schema.STATISTICS "
        "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s AND INDEX_NAME = %s "
        "ORDER BY SEQ_IN_INDEX",
        (MYSQL_DATABASE, table, index_name),
    )
    return [r["COLUMN_NAME"] for r in cursor.fetchall() or []]


class SlotManager:
    def __init__(self):
        self._ensure_database()
        try:
            self.pool = PooledDB(
                creator=pymysql,
                maxconnections=10,
                host=MYSQL_HOST,
                port=MYSQL_PORT,
                user=MYSQL_USER,
                password=MYSQL_PASSWORD,
                database=MYSQL_DATABASE,
                charset=MYSQL_CHARSET,
                cursorclass=pymysql.cursors.DictCursor,
            )
            self._init_tables()
        except pymysql.Error as e:
            logger.critical(f"数据库连接池初始化失败: {e}")
            raise RuntimeError(f"无法连接到 MySQL 数据库: {e}")

    def close(self):
        """关闭数据库连接池（服务关闭时调用）。"""
        try:
            if getattr(self, "pool", None) is not None:
                self.pool.close()
        except Exception as e:
            logger.warning(f"关闭数据库连接池失败: {e}")

    def acquire_slot_lock(self, user_id: int, index: int):
        conn = pymysql.connect(
            host=MYSQL_HOST,
            port=MYSQL_PORT,
            user=MYSQL_USER,
            password=MYSQL_PASSWORD,
            database=MYSQL_DATABASE,
            charset=MYSQL_CHARSET,
        )
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT GET_LOCK(%s, 0)",
                    (f"ai_chat_slot_{user_id}_{index}",),
                )
                acquired = cursor.fetchone()[0]
            if not acquired:
                conn.close()
                return None
            return conn
        except Exception:
            conn.close()
            raise

    def release_slot_lock(self, conn, user_id: int, index: int):
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT RELEASE_LOCK(%s)",
                    (f"ai_chat_slot_{user_id}_{index}",),
                )
        finally:
            conn.close()

    # ── 数据库初始化 ──

    def _ensure_database(self):
        """确保数据库存在，不存在则自动创建。"""
        try:
            conn = pymysql.connect(
                host=MYSQL_HOST,
                port=MYSQL_PORT,
                user=MYSQL_USER,
                password=MYSQL_PASSWORD,
                charset=MYSQL_CHARSET,
            )
        except pymysql.Error as e:
            raise RuntimeError(
                f"无法连接到 MySQL 服务器 ({MYSQL_HOST}:{MYSQL_PORT})，请确认 MySQL 服务已启动: {e}"
            )
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    f"CREATE DATABASE IF NOT EXISTS `{MYSQL_DATABASE}` "
                    f"DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
                )
            conn.commit()
        except pymysql.Error as e:
            raise RuntimeError(
                f"无法创建数据库 `{MYSQL_DATABASE}`，请确认用户 '{MYSQL_USER}' 有 CREATE 权限: {e}"
            )
        finally:
            conn.close()

    def _init_tables(self):
        """自动建表（若不存在），并将旧库迁移为多用户结构。"""
        conn = _get_connection(self.pool)
        try:
            with conn.cursor() as cursor:
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS slots (
                        id INT NOT NULL,
                        user_id INT NOT NULL DEFAULT 0,
                        model VARCHAR(192) NOT NULL DEFAULT '',
                        system_prompt TEXT,
                        api_key VARCHAR(256) DEFAULT '',
                        title VARCHAR(128) DEFAULT '',
                        params TEXT,
                        dual_config TEXT,
                        created_at VARCHAR(32) DEFAULT '',
                        updated_at VARCHAR(32) DEFAULT '',
                        PRIMARY KEY (user_id, id)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """)
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS messages (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        slot_id INT NOT NULL,
                        user_id INT NOT NULL DEFAULT 0,
                        role VARCHAR(16) NOT NULL,
                        source VARCHAR(16) NOT NULL DEFAULT '',
                        content MEDIUMTEXT,
                        created_at VARCHAR(32) DEFAULT '',
                        FOREIGN KEY (user_id, slot_id) REFERENCES slots(user_id, id)
                            ON DELETE CASCADE ON UPDATE CASCADE,
                        INDEX idx_slot_user (user_id, slot_id)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """)

                for col, col_def in [
                    ("params", "TEXT AFTER title"),
                    ("dual_config", "TEXT AFTER params"),
                ]:
                    cursor.execute(f"SHOW COLUMNS FROM `slots` LIKE '{col}'")
                    if not cursor.fetchone():
                        cursor.execute(f"ALTER TABLE `slots` ADD COLUMN `{col}` {col_def}")
                        logger.info(f"已添加 {col} 列到 slots 表")
                cursor.execute(f"SHOW COLUMNS FROM `messages` LIKE 'source'")
                if not cursor.fetchone():
                    cursor.execute(
                        "ALTER TABLE `messages` ADD COLUMN `source` VARCHAR(16) NOT NULL DEFAULT '' AFTER role"
                    )
                    logger.info("已添加 source 列到 messages 表")
                cursor.execute("SHOW COLUMNS FROM `slots` LIKE 'model'")
                model_col = cursor.fetchone()
                if model_col and "192" not in str(model_col.get("Type", "")).lower():
                    cursor.execute(
                        "ALTER TABLE `slots` MODIFY COLUMN `model` VARCHAR(192) NOT NULL DEFAULT ''"
                    )
                    logger.info("已扩展 slots.model 为 VARCHAR(192)")
                cursor.execute("SHOW COLUMNS FROM `messages` LIKE 'attachments'")
                if not cursor.fetchone():
                    cursor.execute(
                        "ALTER TABLE `messages` ADD COLUMN `attachments` TEXT NULL AFTER `content`"
                    )
                    logger.info("已添加 attachments 列到 messages 表")
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS uploads (
                        id VARCHAR(32) NOT NULL PRIMARY KEY,
                        user_id INT NOT NULL,
                        original_name VARCHAR(255) NOT NULL DEFAULT '',
                        mime VARCHAR(128) NOT NULL DEFAULT '',
                        kind VARCHAR(16) NOT NULL DEFAULT '',
                        size INT NOT NULL DEFAULT 0,
                        ext VARCHAR(16) NOT NULL DEFAULT '',
                        message_id INT NULL,
                        created_at VARCHAR(32) DEFAULT '',
                        INDEX idx_uploads_user (user_id),
                        INDEX idx_uploads_message (message_id)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """)
                cursor.execute("SHOW COLUMNS FROM `messages` LIKE 'content'")
                content_col = cursor.fetchone()
                content_type = str((content_col or {}).get("Type", "")).lower()
                if content_col and "mediumtext" not in content_type and "longtext" not in content_type:
                    cursor.execute(
                        "ALTER TABLE `messages` MODIFY COLUMN `content` MEDIUMTEXT"
                    )
                    logger.info("已扩展 messages.content 为 MEDIUMTEXT")
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS providers (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        user_id INT NOT NULL DEFAULT 0,
                        slug VARCHAR(64) NOT NULL,
                        display_name VARCHAR(64) NOT NULL DEFAULT '',
                        base_url VARCHAR(512) NOT NULL DEFAULT '',
                        api_key VARCHAR(256) DEFAULT '',
                        use_env_key TINYINT(1) NOT NULL DEFAULT 0,
                        api_key_env VARCHAR(64) DEFAULT '',
                        sort_order INT NOT NULL DEFAULT 0,
                        created_at VARCHAR(32) DEFAULT '',
                        updated_at VARCHAR(32) DEFAULT '',
                        UNIQUE KEY uniq_user_slug (user_id, slug)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """)
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS catalog_models (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        provider_id INT NOT NULL,
                        model_id VARCHAR(128) NOT NULL,
                        display_name VARCHAR(64) DEFAULT '',
                        UNIQUE KEY uniq_provider_model (provider_id, model_id),
                        FOREIGN KEY (provider_id) REFERENCES providers(id) ON DELETE CASCADE,
                        INDEX idx_catalog_provider (provider_id)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """)

                # 所有表就绪后再做多用户迁移（新库为无害的幂等检查）
                self._migrate_multiuser(cursor)
            conn.commit()
            logger.info("MySQL 数据库表初始化完成")
        except pymysql.Error as e:
            logger.error(f"数据库表初始化失败: {e}")
            _safe_rollback(conn)
            raise
        finally:
            conn.close()

    def _migrate_multiuser(self, cursor):
        """旧库升级：补齐 user_id 列并把主键/唯一键改为按用户复合。"""
        legacy_slots = not _column_exists(cursor, "slots", "user_id")
        if legacy_slots:
            cursor.execute(
                "ALTER TABLE `slots` ADD COLUMN `user_id` INT NOT NULL DEFAULT 0 AFTER `id`"
            )
            logger.info("多用户迁移：已添加 slots.user_id")

        if _primary_key_columns(cursor, "slots") == ["id"]:
            fk = _foreign_key_to(cursor, "messages", "slots")
            if fk:
                cursor.execute(f"ALTER TABLE `messages` DROP FOREIGN KEY `{fk}`")
            cursor.execute(
                "ALTER TABLE `slots` DROP PRIMARY KEY, ADD PRIMARY KEY (`user_id`, `id`)"
            )
            logger.info("多用户迁移：slots 主键已改为 (user_id, id)")

        if not _column_exists(cursor, "messages", "user_id"):
            cursor.execute(
                "ALTER TABLE `messages` ADD COLUMN `user_id` INT NOT NULL DEFAULT 0 AFTER `slot_id`"
            )
            logger.info("多用户迁移：已添加 messages.user_id")

        if not _foreign_key_to(cursor, "messages", "slots"):
            cursor.execute(
                "ALTER TABLE `messages` ADD CONSTRAINT `fk_messages_slot_user` "
                "FOREIGN KEY (`user_id`, `slot_id`) REFERENCES `slots`(`user_id`, `id`) "
                "ON DELETE CASCADE ON UPDATE CASCADE"
            )
            logger.info("多用户迁移：messages 外键已改为复合外键")

        if not _column_exists(cursor, "providers", "user_id"):
            cursor.execute(
                "ALTER TABLE `providers` ADD COLUMN `user_id` INT NOT NULL DEFAULT 0 AFTER `id`"
            )
            logger.info("多用户迁移：已添加 providers.user_id")

        if _index_columns(cursor, "providers", "slug") == ["slug"]:
            cursor.execute("ALTER TABLE `providers` DROP INDEX `slug`")
            cursor.execute(
                "ALTER TABLE `providers` ADD UNIQUE KEY `uniq_user_slug` (`user_id`, `slug`)"
            )
            logger.info("多用户迁移：providers 唯一键已改为 (user_id, slug)")

    # ── 通用事务模板 ──

    @contextmanager
    def _transaction(self):
        """获取连接 → 执行 → 提交 → 关闭，异常时回滚并重抛。"""
        conn = _get_connection(self.pool)
        try:
            yield conn
            conn.commit()
        except pymysql.Error:
            _safe_rollback(conn)
            raise
        finally:
            conn.close()

    @staticmethod
    def _now() -> str:
        return datetime.datetime.now().isoformat()

    @staticmethod
    def _json_or_default(raw, default):
        if isinstance(raw, str):
            try:
                return json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                return default
        return raw if raw is not None else default

    # ── 核心 CRUD ──

    def create_slot(self, user_id: int, index: int, model: str, system_prompt: str,
                     api_key: str = "", params: Optional[dict] = None,
                     title: str = "", dual_config: Optional[dict] = None) -> bool:
        if index < 0 or index >= SLOT_COUNT:
            return False
        try:
            with self._transaction() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "SELECT id FROM slots WHERE user_id = %s AND id = %s",
                        (user_id, index),
                    )
                    if cursor.fetchone():
                        return False
                    now = self._now()
                    title = title.strip()
                    params_json = json.dumps(params) if params else "{}"
                    dual_json = json.dumps(dual_config) if dual_config else "{}"
                    cursor.execute(
                        "INSERT INTO slots (id, user_id, model, system_prompt, api_key, title, params, dual_config, created_at, updated_at) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                        (index, user_id, model, system_prompt, api_key, title, params_json, dual_json, now, now),
                    )
            return True
        except pymysql.Error as e:
            logger.error(f"create_slot(user={user_id}, {index}) 失败: {e}")
            return False

    def get_slot(self, user_id: int, index: int) -> Optional[Dict]:
        try:
            with self._transaction() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "SELECT * FROM slots WHERE user_id = %s AND id = %s",
                        (user_id, index),
                    )
                    slot = cursor.fetchone()
                    if not slot:
                        return None
                    slot["params"] = self._json_or_default(slot.get("params"), {})
                    slot["dual_config"] = self._json_or_default(slot.get("dual_config"), {})
                    cursor.execute(
                        "SELECT id, role, content, source, attachments FROM messages "
                        "WHERE user_id = %s AND slot_id = %s ORDER BY id ASC",
                        (user_id, index),
                    )
                    history = list(cursor.fetchall())
                    for row in history:
                        parsed = self._json_or_default(row.get("attachments"), [])
                        row["attachments"] = parsed if isinstance(parsed, list) else []
                    slot["history"] = history
                    return slot
        except pymysql.Error as e:
            logger.error(f"get_slot(user={user_id}, {index}) 失败: {e}")
            return None

    def delete_slot(self, user_id: int, index: int) -> bool:
        file_ids: List[str] = []
        try:
            with self._transaction() as conn:
                with conn.cursor() as cursor:
                    file_ids = self._take_attachment_ids(cursor, user_id, "slot_id = %s", (index,))
                    cursor.execute(
                        "DELETE FROM slots WHERE user_id = %s AND id = %s",
                        (user_id, index),
                    )
                    deleted = cursor.rowcount > 0
            if deleted:
                self._unlink_uploads(user_id, file_ids)
            return deleted
        except pymysql.Error as e:
            logger.error(f"delete_slot(user={user_id}, {index}) 失败: {e}")
            return False

    # 允许通过 update_slot_meta 更新的列（白名单，防止动态 SQL 注入）
    _META_ALLOWED_COLUMNS = {"title", "api_key"}

    def update_slot_meta(self, user_id: int, index: int, meta: dict) -> bool:
        try:
            with self._transaction() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "SELECT id FROM slots WHERE user_id = %s AND id = %s",
                        (user_id, index),
                    )
                    if not cursor.fetchone():
                        return False
                    set_parts = []
                    values = []
                    for key, val in meta.items():
                        if key not in self._META_ALLOWED_COLUMNS:
                            logger.warning(f"update_slot_meta({index}) 跳过非法列: {key}")
                            continue
                        set_parts.append(f"{key} = %s")
                        values.append(val)
                    if not set_parts:
                        return False
                    set_parts.append("updated_at = %s")
                    values.append(self._now())
                    values.append(user_id)
                    values.append(index)
                    sql = f"UPDATE slots SET {', '.join(set_parts)} WHERE user_id = %s AND id = %s"
                    cursor.execute(sql, values)
            return True
        except pymysql.Error as e:
            logger.error(f"update_slot_meta(user={user_id}, {index}) 失败: {e}")
            return False

    def save_slot_history(self, user_id: int, index: int, history: List) -> bool:
        try:
            with self._transaction() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "SELECT id FROM slots WHERE user_id = %s AND id = %s",
                        (user_id, index),
                    )
                    if not cursor.fetchone():
                        return False
                    cursor.execute(
                        "SELECT attachments FROM messages WHERE user_id = %s AND slot_id = %s",
                        (user_id, index),
                    )
                    old_ids = set(self._ids_from_attachment_rows(cursor.fetchall()))
                    new_ids = set(self._ids_from_attachment_rows(
                        [{"attachments": m.get("attachments")} for m in history]
                    ))
                    removed = [fid for fid in old_ids if fid not in new_ids]
                    reused = [fid for fid in old_ids if fid in new_ids]
                    if removed:
                        ph = ", ".join(["%s"] * len(removed))
                        cursor.execute(
                            f"DELETE FROM uploads WHERE user_id = %s AND id IN ({ph})",
                            (user_id, *removed),
                        )
                    if reused:
                        ph = ", ".join(["%s"] * len(reused))
                        cursor.execute(
                            f"UPDATE uploads SET message_id = NULL WHERE user_id = %s AND id IN ({ph})",
                            (user_id, *reused),
                        )
                    cursor.execute(
                        "DELETE FROM messages WHERE user_id = %s AND slot_id = %s",
                        (user_id, index),
                    )
                    if history:
                        for m in history:
                            self._insert_message(cursor, user_id, index, m)
                    cursor.execute(
                        "UPDATE slots SET updated_at = %s WHERE user_id = %s AND id = %s",
                        (self._now(), user_id, index),
                    )
            self._unlink_uploads(user_id, removed)
            return True
        except pymysql.Error as e:
            logger.error(f"save_slot_history(user={user_id}, {index}) 失败: {e}")
            return False

    # ── append-only 写入 ──

    @staticmethod
    def _dump_attachments(value) -> Optional[str]:
        if not value:
            return None
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            return json.dumps(value, ensure_ascii=False)
        return None

    @staticmethod
    def _ids_from_attachment_rows(rows) -> List[str]:
        ids: List[str] = []
        seen = set()
        for row in rows or []:
            raw = row.get("attachments") if isinstance(row, dict) else row
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw or "null")
                except json.JSONDecodeError:
                    raw = None
            if not isinstance(raw, list):
                continue
            for att in raw:
                if isinstance(att, dict) and att.get("id"):
                    fid = str(att["id"])
                    if fid not in seen:
                        seen.add(fid)
                        ids.append(fid)
        return ids

    def _insert_message(self, cursor, user_id: int, slot_id: int, message: dict) -> int:
        attachments = message.get("attachments") or []
        if not isinstance(attachments, list):
            attachments = []
        cursor.execute(
            "INSERT INTO messages (slot_id, user_id, role, content, source, attachments) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (
                slot_id,
                user_id,
                message.get("role", ""),
                message.get("content", ""),
                message.get("source", ""),
                self._dump_attachments(attachments),
            ),
        )
        message_id = cursor.lastrowid
        upload_ids = [str(a.get("id")) for a in attachments if isinstance(a, dict) and a.get("id")]
        if upload_ids:
            unique_ids = list(dict.fromkeys(upload_ids))
            ph = ", ".join(["%s"] * len(unique_ids))
            cursor.execute(
                f"UPDATE uploads SET message_id = %s "
                f"WHERE user_id = %s AND message_id IS NULL AND id IN ({ph})",
                (message_id, user_id, *unique_ids),
            )
            if cursor.rowcount != len(unique_ids):
                raise pymysql.err.InternalError("附件不存在或已使用")
        return message_id

    def _take_attachment_ids(self, cursor, user_id: int, where_sql: str, params: tuple) -> List[str]:
        cursor.execute(
            f"SELECT attachments FROM messages WHERE user_id = %s AND {where_sql}",
            (user_id, *params),
        )
        ids = self._ids_from_attachment_rows(cursor.fetchall())
        if ids:
            ph = ", ".join(["%s"] * len(ids))
            cursor.execute(
                f"DELETE FROM uploads WHERE user_id = %s AND id IN ({ph})",
                (user_id, *ids),
            )
        return ids

    @staticmethod
    def _unlink_uploads(user_id: int, upload_ids: List[str]) -> None:
        if not upload_ids:
            return
        from attachments import delete_files
        delete_files(user_id, upload_ids)

    def append_messages(self, user_id: int, slot_id: int, messages: List[Dict]) -> List[int]:
        try:
            with self._transaction() as conn:
                with conn.cursor() as cursor:
                    # 逐条插入并记录每条真实 ID
                    ids = []
                    for m in messages:
                        ids.append(self._insert_message(cursor, user_id, slot_id, m))
                    cursor.execute(
                        "UPDATE slots SET updated_at = %s WHERE user_id = %s AND id = %s",
                        (self._now(), user_id, slot_id),
                    )
            return ids
        except pymysql.Error as e:
            logger.error(f"append_messages(user={user_id}, {slot_id}) 失败: {e}")
            return []

    def delete_messages_from(self, user_id: int, slot_id: int, from_message_id: int) -> bool:
        file_ids: List[str] = []
        try:
            with self._transaction() as conn:
                with conn.cursor() as cursor:
                    file_ids = self._take_attachment_ids(
                        cursor, user_id, "slot_id = %s AND id >= %s", (slot_id, from_message_id),
                    )
                    cursor.execute(
                        "DELETE FROM messages WHERE user_id = %s AND slot_id = %s AND id >= %s",
                        (user_id, slot_id, from_message_id),
                    )
                    cursor.execute(
                        "UPDATE slots SET updated_at = %s WHERE user_id = %s AND id = %s",
                        (self._now(), user_id, slot_id),
                    )
            self._unlink_uploads(user_id, file_ids)
            return True
        except pymysql.Error as e:
            logger.error(f"delete_messages_from(user={user_id}, {slot_id}, {from_message_id}) 失败: {e}")
            return False

    def delete_messages_after(self, user_id: int, slot_id: int, after_message_id: int) -> bool:
        """删除某条消息之后的全部消息，保留该条本身。"""
        file_ids: List[str] = []
        try:
            with self._transaction() as conn:
                with conn.cursor() as cursor:
                    file_ids = self._take_attachment_ids(
                        cursor, user_id, "slot_id = %s AND id > %s", (slot_id, after_message_id),
                    )
                    cursor.execute(
                        "DELETE FROM messages WHERE user_id = %s AND slot_id = %s AND id > %s",
                        (user_id, slot_id, after_message_id),
                    )
                    cursor.execute(
                        "UPDATE slots SET updated_at = %s WHERE user_id = %s AND id = %s",
                        (self._now(), user_id, slot_id),
                    )
            self._unlink_uploads(user_id, file_ids)
            return True
        except pymysql.Error as e:
            logger.error(f"delete_messages_after(user={user_id}, {slot_id}, {after_message_id}) 失败: {e}")
            return False

    def list_messages_after(self, user_id: int, slot_id: int, after_message_id: int) -> List[Dict]:
        """读取某条消息之后的全部消息（用于重新生成失败时写回）。"""
        try:
            with self._transaction() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "SELECT id, role, content, source, attachments, created_at FROM messages "
                        "WHERE user_id = %s AND slot_id = %s AND id > %s ORDER BY id ASC",
                        (user_id, slot_id, after_message_id),
                    )
                    return list(cursor.fetchall() or [])
        except pymysql.Error as e:
            logger.error(f"list_messages_after(user={user_id}, {slot_id}, {after_message_id}) 失败: {e}")
            return []

    def restore_messages(self, user_id: int, slot_id: int, rows: List[Dict]) -> bool:
        """按原 ID 写回消息快照。"""
        if not rows:
            return True
        try:
            with self._transaction() as conn:
                with conn.cursor() as cursor:
                    cursor.executemany(
                        "INSERT INTO messages (id, slot_id, user_id, role, content, source, attachments, created_at) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                        [
                            (
                                r.get("id"),
                                slot_id,
                                user_id,
                                r.get("role", ""),
                                r.get("content", ""),
                                r.get("source", ""),
                                r.get("attachments") if isinstance(r.get("attachments"), str) else self._dump_attachments(r.get("attachments")),
                                r.get("created_at") or "",
                            )
                            for r in rows
                        ],
                    )
            return True
        except pymysql.Error as e:
            logger.error(f"restore_messages(user={user_id}, {slot_id}) 失败: {e}")
            return False

    def delete_messages_by_ids(self, user_id: int, slot_id: int, message_ids: List[int]) -> bool:
        """按消息 ID 精确删除（用于删除中间一段消息，不改变其余消息 ID）。"""
        if not message_ids:
            return True
        file_ids: List[str] = []
        try:
            with self._transaction() as conn:
                with conn.cursor() as cursor:
                    placeholders = ", ".join(["%s"] * len(message_ids))
                    file_ids = self._take_attachment_ids(
                        cursor, user_id, f"slot_id = %s AND id IN ({placeholders})", (slot_id, *message_ids),
                    )
                    cursor.execute(
                        f"DELETE FROM messages WHERE user_id = %s AND slot_id = %s AND id IN ({placeholders})",
                        (user_id, slot_id, *message_ids),
                    )
                    cursor.execute(
                        "UPDATE slots SET updated_at = %s WHERE user_id = %s AND id = %s",
                        (self._now(), user_id, slot_id),
                    )
            self._unlink_uploads(user_id, file_ids)
            return True
        except pymysql.Error as e:
            logger.error(f"delete_messages_by_ids(user={user_id}, {slot_id}) 失败: {e}")
            return False

    def clear_all_messages(self, user_id: int, slot_id: int) -> bool:
        file_ids: List[str] = []
        try:
            with self._transaction() as conn:
                with conn.cursor() as cursor:
                    file_ids = self._take_attachment_ids(cursor, user_id, "slot_id = %s", (slot_id,))
                    cursor.execute(
                        "DELETE FROM messages WHERE user_id = %s AND slot_id = %s",
                        (user_id, slot_id),
                    )
                    cursor.execute(
                        "UPDATE slots SET updated_at = %s WHERE user_id = %s AND id = %s",
                        (self._now(), user_id, slot_id),
                    )
            self._unlink_uploads(user_id, file_ids)
            return True
        except pymysql.Error as e:
            logger.error(f"clear_all_messages(user={user_id}, {slot_id}) 失败: {e}")
            return False

    def touch_slot(self, user_id: int, index: int) -> bool:
        """仅刷新存档的 updated_at（继续回复等只改内容不新增消息的场景）。"""
        try:
            with self._transaction() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "UPDATE slots SET updated_at = %s WHERE user_id = %s AND id = %s",
                        (self._now(), user_id, index),
                    )
            return True
        except pymysql.Error as e:
            logger.error(f"touch_slot(user={user_id}, {index}) 失败: {e}")
            return False

    def update_message_content(self, user_id: int, slot_id: int, message_id: int, content: str) -> bool:
        try:
            with self._transaction() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "UPDATE messages SET content = %s WHERE id = %s AND slot_id = %s AND user_id = %s",
                        (content, message_id, slot_id, user_id),
                    )
                    updated = cursor.rowcount > 0
            return updated
        except pymysql.Error as e:
            logger.error(f"update_message_content(user={user_id}, {message_id}) 失败: {e}")
            return False

    def list_slots(self, user_id: int) -> List[Optional[Dict]]:
        try:
            with self._transaction() as conn:
                with conn.cursor() as cursor:
                    cursor.execute("""
                        SELECT s.*,
                               (SELECT COALESCE(SUM(role = 'user'), 0)
                                FROM messages m
                                WHERE m.user_id = s.user_id AND m.slot_id = s.id) AS round_count
                        FROM slots s
                        WHERE s.user_id = %s
                        ORDER BY s.id ASC
                    """, (user_id,))
                    rows = cursor.fetchall()
                    row_map = {r["id"]: r for r in rows}
                    result = []
                    for i in range(SLOT_COUNT):
                        slot = row_map.get(i)
                        if slot is None:
                            result.append(None)
                        else:
                            params_val = self._json_or_default(slot.get("params"), {})
                            dual_val = self._json_or_default(slot.get("dual_config"), {})
                            result.append({
                                "index": i,
                                "model": slot.get("model", "未知"),
                                "system_prompt": slot.get("system_prompt", ""),
                                "created_at": slot.get("created_at", ""),
                                "updated_at": slot.get("updated_at", ""),
                                "message_count": slot.get("round_count", 0),
                                "title": slot.get("title", ""),
                                "params": params_val,
                                "dual_config": dual_val,
                                "dual_enabled": dual_val.get("enabled", False),
                            })
                    return result
        except pymysql.Error as e:
            logger.error(f"list_slots(user={user_id}) 失败: {e}")
            return [None] * SLOT_COUNT

    def update_dual_config(self, user_id: int, index: int, dual_config: dict) -> bool:
        """更新 dual_config（如切换响应模式）。"""
        try:
            with self._transaction() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "SELECT id FROM slots WHERE user_id = %s AND id = %s",
                        (user_id, index),
                    )
                    if not cursor.fetchone():
                        return False
                    cursor.execute(
                        "UPDATE slots SET dual_config = %s, updated_at = %s WHERE user_id = %s AND id = %s",
                        (json.dumps(dual_config), self._now(), user_id, index),
                    )
            return True
        except pymysql.Error as e:
            logger.error(f"update_dual_config(user={user_id}, {index}) 失败: {e}")
            return False

    def update_slot(self, user_id: int, index: int, model: str | None = None,
                    system_prompt: str | None = None, api_key: str | None = None,
                    title: str | None = None, params: dict | None = None,
                    dual_config: dict | None = None) -> bool:
        """原子更新存档配置（仅更新非 None 字段），用于模型更换。

        - model/system_prompt/api_key/title/params 对应 slots 表直属列
        - dual_config 为完整 JSON 字典（调用方需自行合并好再传入）
        """
        try:
            with self._transaction() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "SELECT id FROM slots WHERE user_id = %s AND id = %s",
                        (user_id, index),
                    )
                    if not cursor.fetchone():
                        return False
                    set_parts = []
                    values = []
                    if model is not None:
                        set_parts.append("model = %s")
                        values.append(model)
                    if system_prompt is not None:
                        set_parts.append("system_prompt = %s")
                        values.append(system_prompt)
                    if api_key is not None:
                        set_parts.append("api_key = %s")
                        values.append(api_key)
                    if title is not None:
                        set_parts.append("title = %s")
                        values.append(title)
                    if params is not None:
                        set_parts.append("params = %s")
                        values.append(json.dumps(params))
                    if dual_config is not None:
                        set_parts.append("dual_config = %s")
                        values.append(json.dumps(dual_config))
                    if not set_parts:
                        return False
                    set_parts.append("updated_at = %s")
                    values.append(self._now())
                    values.append(user_id)
                    values.append(index)
                    sql = f"UPDATE slots SET {', '.join(set_parts)} WHERE user_id = %s AND id = %s"
                    cursor.execute(sql, values)
            return True
        except pymysql.Error as e:
            logger.error(f"update_slot(user={user_id}, {index}) 失败: {e}")
            return False

    # ── 供应商 / 模型目录 ──

    @staticmethod
    def model_key(slug: str, model_id: str) -> str:
        return f"{slug}:{model_id}"

    def _list_models_for_provider(self, cursor, provider_id: int, slug: str) -> List[Dict]:
        cursor.execute(
            "SELECT id, model_id, display_name FROM catalog_models "
            "WHERE provider_id = %s ORDER BY id ASC",
            (provider_id,),
        )
        items = []
        for m in cursor.fetchall():
            mid = m.get("model_id") or ""
            items.append({
                "id": m["id"],
                "model_id": mid,
                "display_name": (m.get("display_name") or "").strip() or mid,
                "key": self.model_key(slug, mid),
            })
        return items

    def list_providers(self, user_id: int) -> List[Dict]:
        try:
            with self._transaction() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "SELECT * FROM providers WHERE user_id = %s ORDER BY sort_order ASC, id ASC",
                        (user_id,),
                    )
                    rows = list(cursor.fetchall() or [])
                    result = []
                    for row in rows:
                        models = self._list_models_for_provider(
                            cursor, row["id"], row.get("slug") or "",
                        )
                        result.append({
                            **row,
                            "models": models,
                        })
                    return result
        except pymysql.Error as e:
            logger.error(f"list_providers(user={user_id}) 失败: {e}")
            raise RuntimeError(f"读取供应商目录失败: {e}")

    def get_provider(self, user_id: int, provider_id: int) -> Optional[Dict]:
        try:
            with self._transaction() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "SELECT * FROM providers WHERE id = %s AND user_id = %s",
                        (provider_id, user_id),
                    )
                    row = cursor.fetchone()
                    if not row:
                        return None
                    row["models"] = self._list_models_for_provider(
                        cursor, row["id"], row.get("slug") or "",
                    )
                    return row
        except pymysql.Error as e:
            logger.error(f"get_provider(user={user_id}, {provider_id}) 失败: {e}")
            raise RuntimeError(f"读取供应商失败: {e}")

    def get_catalog_model(self, user_id: int, model_row_id: int) -> Optional[Dict]:
        try:
            with self._transaction() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "SELECT m.id, m.provider_id, m.model_id, m.display_name, "
                        "p.slug, p.display_name AS provider_name, p.base_url, "
                        "p.api_key, p.use_env_key, p.api_key_env "
                        "FROM catalog_models m "
                        "JOIN providers p ON p.id = m.provider_id "
                        "WHERE m.id = %s AND p.user_id = %s",
                        (model_row_id, user_id),
                    )
                    return cursor.fetchone()
        except pymysql.Error as e:
            logger.error(f"get_catalog_model(user={user_id}, {model_row_id}) 失败: {e}")
            raise RuntimeError(f"读取模型失败: {e}")

    def resolve_model_key(self, user_id: int, key: str) -> Optional[Dict]:
        if not key or ":" not in key:
            return None
        slug, model_id = key.split(":", 1)
        if not slug or not model_id:
            return None
        try:
            with self._transaction() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "SELECT m.id, m.provider_id, m.model_id, m.display_name, "
                        "p.slug, p.display_name AS provider_name, p.base_url, "
                        "p.api_key, p.use_env_key, p.api_key_env "
                        "FROM catalog_models m "
                        "JOIN providers p ON p.id = m.provider_id "
                        "WHERE p.slug = %s AND m.model_id = %s AND p.user_id = %s",
                        (slug, model_id, user_id),
                    )
                    return cursor.fetchone()
        except pymysql.Error as e:
            logger.error(f"resolve_model_key(user={user_id}, {key}) 失败: {e}")
            raise RuntimeError(f"解析模型失败: {e}")

    def list_catalog_models(self, user_id: int) -> List[Dict]:
        try:
            with self._transaction() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "SELECT m.id, m.model_id, m.display_name, "
                        "p.slug AS provider, p.display_name AS provider_name, "
                        "p.sort_order "
                        "FROM catalog_models m "
                        "JOIN providers p ON p.id = m.provider_id "
                        "WHERE p.user_id = %s "
                        "ORDER BY p.sort_order ASC, p.id ASC, m.id ASC",
                        (user_id,),
                    )
                    items = []
                    for row in cursor.fetchall() or []:
                        mid = row.get("model_id") or ""
                        items.append({
                            "key": self.model_key(row.get("provider") or "", mid),
                            "id": mid,
                            "display_name": (row.get("display_name") or "").strip() or mid,
                            "provider": row.get("provider") or "",
                            "provider_name": row.get("provider_name") or "",
                            "max_tokens": 8192,
                        })
                    return items
        except pymysql.Error as e:
            logger.error(f"list_catalog_models(user={user_id}) 失败: {e}")
            raise RuntimeError(f"读取模型列表失败: {e}")

    def _slot_rows(self, user_id: int, cursor) -> List[Dict]:
        cursor.execute(
            "SELECT id, model, dual_config FROM slots WHERE user_id = %s",
            (user_id,),
        )
        return list(cursor.fetchall() or [])

    def find_slots_referencing_model_key(self, user_id: int, key: str) -> List[int]:
        try:
            with self._transaction() as conn:
                with conn.cursor() as cursor:
                    hits = []
                    for row in self._slot_rows(user_id, cursor):
                        if row.get("model") == key:
                            hits.append(row["id"])
                            continue
                        dual = self._json_or_default(row.get("dual_config"), {})
                        m2 = (dual.get("model2") or {}).get("model")
                        if m2 == key:
                            hits.append(row["id"])
                    return hits
        except pymysql.Error as e:
            logger.error(f"find_slots_referencing_model_key(user={user_id}) 失败: {e}")
            raise RuntimeError(f"检查模型引用失败: {e}")

    def find_slots_referencing_provider_slug(self, user_id: int, slug: str) -> List[int]:
        prefix = f"{slug}:"
        try:
            with self._transaction() as conn:
                with conn.cursor() as cursor:
                    hits = []
                    for row in self._slot_rows(user_id, cursor):
                        model = row.get("model") or ""
                        if model.startswith(prefix):
                            hits.append(row["id"])
                            continue
                        dual = self._json_or_default(row.get("dual_config"), {})
                        m2 = (dual.get("model2") or {}).get("model") or ""
                        if m2.startswith(prefix):
                            hits.append(row["id"])
                    return hits
        except pymysql.Error as e:
            logger.error(f"find_slots_referencing_provider_slug(user={user_id}) 失败: {e}")
            raise RuntimeError(f"检查供应商引用失败: {e}")

    def rewrite_model_key_in_slots(self, user_id: int, old_key: str, new_key: str) -> None:
        if not old_key or old_key == new_key:
            return
        now = self._now()
        try:
            with self._transaction() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "UPDATE slots SET model = %s, updated_at = %s WHERE model = %s AND user_id = %s",
                        (new_key, now, old_key, user_id),
                    )
                    for row in self._slot_rows(user_id, cursor):
                        dual = self._json_or_default(row.get("dual_config"), {})
                        m2 = dual.get("model2") if isinstance(dual.get("model2"), dict) else None
                        if m2 and m2.get("model") == old_key:
                            m2["model"] = new_key
                            dual["model2"] = m2
                            cursor.execute(
                                "UPDATE slots SET dual_config = %s, updated_at = %s WHERE id = %s AND user_id = %s",
                                (json.dumps(dual), now, row["id"], user_id),
                            )
        except pymysql.Error as e:
            logger.error(f"rewrite_model_key_in_slots(user={user_id}) 失败: {e}")
            raise RuntimeError(f"同步存档模型引用失败: {e}")

    def create_provider(
        self,
        user_id: int,
        slug: str,
        display_name: str,
        base_url: str,
        api_key: str = "",
        use_env_key: bool = False,
        api_key_env: str = "",
        models: Optional[List[Dict]] = None,
    ) -> Dict:
        now = self._now()
        try:
            with self._transaction() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "SELECT COALESCE(MAX(sort_order), 0) AS m FROM providers WHERE user_id = %s",
                        (user_id,),
                    )
                    max_order = (cursor.fetchone() or {}).get("m") or 0
                    cursor.execute(
                        "INSERT INTO providers "
                        "(user_id, slug, display_name, base_url, api_key, use_env_key, api_key_env, "
                        "sort_order, created_at, updated_at) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                        (
                            user_id, slug, display_name, base_url, api_key or "",
                            1 if use_env_key else 0, api_key_env or "",
                            int(max_order) + 1, now, now,
                        ),
                    )
                    provider_id = cursor.lastrowid
                    for item in models or []:
                        mid = (item.get("model_id") or "").strip()
                        if not mid:
                            continue
                        dname = (item.get("display_name") or "").strip()
                        cursor.execute(
                            "INSERT INTO catalog_models (provider_id, model_id, display_name) "
                            "VALUES (%s, %s, %s)",
                            (provider_id, mid, dname),
                        )
            created = self.get_provider(user_id, provider_id)
            if not created:
                raise RuntimeError("创建供应商后读取失败")
            return created
        except pymysql.IntegrityError as e:
            logger.warning(f"create_provider 冲突: {e}")
            raise ValueError("duplicate") from e
        except pymysql.Error as e:
            logger.error(f"create_provider(user={user_id}) 失败: {e}")
            raise RuntimeError(f"创建供应商失败: {e}")

    def update_provider(
        self,
        user_id: int,
        provider_id: int,
        display_name: Optional[str] = None,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        use_env_key: Optional[bool] = None,
        api_key_env: Optional[str] = None,
    ) -> Optional[Dict]:
        try:
            with self._transaction() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "SELECT id FROM providers WHERE id = %s AND user_id = %s",
                        (provider_id, user_id),
                    )
                    if not cursor.fetchone():
                        return None
                    parts = []
                    values: list = []
                    if display_name is not None:
                        parts.append("display_name = %s")
                        values.append(display_name)
                    if base_url is not None:
                        parts.append("base_url = %s")
                        values.append(base_url)
                    if api_key is not None:
                        parts.append("api_key = %s")
                        values.append(api_key)
                    if use_env_key is not None:
                        parts.append("use_env_key = %s")
                        values.append(1 if use_env_key else 0)
                    if api_key_env is not None:
                        parts.append("api_key_env = %s")
                        values.append(api_key_env)
                    if not parts:
                        pass
                    else:
                        parts.append("updated_at = %s")
                        values.append(self._now())
                        values.append(provider_id)
                        values.append(user_id)
                        cursor.execute(
                            f"UPDATE providers SET {', '.join(parts)} WHERE id = %s AND user_id = %s",
                            values,
                        )
            return self.get_provider(user_id, provider_id)
        except pymysql.Error as e:
            logger.error(f"update_provider(user={user_id}, {provider_id}) 失败: {e}")
            raise RuntimeError(f"更新供应商失败: {e}")

    def delete_provider(self, user_id: int, provider_id: int) -> bool:
        try:
            with self._transaction() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "DELETE FROM providers WHERE id = %s AND user_id = %s",
                        (provider_id, user_id),
                    )
                    return cursor.rowcount > 0
        except pymysql.Error as e:
            logger.error(f"delete_provider(user={user_id}, {provider_id}) 失败: {e}")
            raise RuntimeError(f"删除供应商失败: {e}")

    def add_catalog_model(self, user_id: int, provider_id: int, model_id: str, display_name: str = "") -> Dict:
        try:
            with self._transaction() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "SELECT id FROM providers WHERE id = %s AND user_id = %s",
                        (provider_id, user_id),
                    )
                    if not cursor.fetchone():
                        raise ValueError("provider_not_found")
                    cursor.execute(
                        "INSERT INTO catalog_models (provider_id, model_id, display_name) "
                        "VALUES (%s, %s, %s)",
                        (provider_id, model_id, display_name),
                    )
                    new_id = cursor.lastrowid
            row = self.get_catalog_model(user_id, new_id)
            if not row:
                raise RuntimeError("创建模型后读取失败")
            return row
        except ValueError:
            raise
        except pymysql.IntegrityError as e:
            raise ValueError("duplicate_model") from e
        except pymysql.Error as e:
            logger.error(f"add_catalog_model(user={user_id}) 失败: {e}")
            raise RuntimeError(f"添加模型失败: {e}")

    def update_catalog_model(
        self,
        user_id: int,
        model_row_id: int,
        model_id: Optional[str] = None,
        display_name: Optional[str] = None,
    ) -> Optional[Dict]:
        existing = self.get_catalog_model(user_id, model_row_id)
        if existing is None:
            return None
        old_key = self.model_key(existing.get("slug") or "", existing.get("model_id") or "")
        try:
            with self._transaction() as conn:
                with conn.cursor() as cursor:
                    parts = []
                    values: list = []
                    if model_id is not None:
                        parts.append("model_id = %s")
                        values.append(model_id)
                    if display_name is not None:
                        parts.append("display_name = %s")
                        values.append(display_name)
                    if parts:
                        values.append(model_row_id)
                        cursor.execute(
                            f"UPDATE catalog_models SET {', '.join(parts)} WHERE id = %s",
                            values,
                        )
        except pymysql.IntegrityError as e:
            raise ValueError("duplicate_model") from e
        except pymysql.Error as e:
            logger.error(f"update_catalog_model(user={user_id}) 失败: {e}")
            raise RuntimeError(f"更新模型失败: {e}")
        updated = self.get_catalog_model(user_id, model_row_id)
        if updated and model_id is not None:
            new_key = self.model_key(updated.get("slug") or "", updated.get("model_id") or "")
            self.rewrite_model_key_in_slots(user_id, old_key, new_key)
        return updated

    def delete_catalog_model(self, user_id: int, model_row_id: int) -> bool:
        if not self.get_catalog_model(user_id, model_row_id):
            return False
        try:
            with self._transaction() as conn:
                with conn.cursor() as cursor:
                    cursor.execute("DELETE FROM catalog_models WHERE id = %s", (model_row_id,))
                    return cursor.rowcount > 0
        except pymysql.Error as e:
            logger.error(f"delete_catalog_model(user={user_id}) 失败: {e}")
            raise RuntimeError(f"删除模型失败: {e}")
