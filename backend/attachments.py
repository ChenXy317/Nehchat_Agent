"""
对话附件 — 图片与文本文件的保存、读取和清理。

图片在调用模型时作为 image_url（data URL）传入；文本并入用户消息。
文件只放在当前用户目录下，接口不接受路径。
"""
from __future__ import annotations

import base64
import logging
import re
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pymysql

from config import (
    UPLOADS_DIR,
    UPLOAD_MAX_CONTEXT_IMAGES,
    UPLOAD_MAX_IMAGE_BYTES,
    UPLOAD_MAX_TEXT_BYTES,
    UPLOAD_MAX_TEXT_CHARS,
)
from session_manager import _get_connection
from state import get_slot_mgr

logger = logging.getLogger(__name__)

_ID_RE = re.compile(r"^[a-f0-9]{32}$")

IMAGE_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}
TEXT_TYPES = {
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".csv": "text/csv",
    ".json": "application/json",
    ".log": "text/plain",
}

_ERRORS = {
    "empty": "文件是空的",
    "unsupported": "只支持图片（jpg、png、webp、gif）和文本（txt、md、csv、json、log）",
    "image_too_large": "单张图片不能超过 8MB",
    "text_too_large": "单个文本文件不能超过 512KB",
    "invalid": "文件内容与扩展名不符",
}


class AttachmentError(ValueError):
    def __init__(self, code: str):
        super().__init__(_ERRORS.get(code, "附件无效"))
        self.code = code


def _user_dir(user_id: int) -> Path:
    return UPLOADS_DIR / str(int(user_id))


def _check_id(upload_id: str) -> str:
    upload_id = (upload_id or "").strip().lower()
    if not _ID_RE.fullmatch(upload_id):
        raise AttachmentError("invalid")
    return upload_id


def _sniff_image(ext: str, data: bytes) -> bool:
    if ext in {".jpg", ".jpeg"}:
        return data.startswith(b"\xff\xd8\xff")
    if ext == ".png":
        return data.startswith(b"\x89PNG\r\n\x1a\n")
    if ext == ".gif":
        return data.startswith((b"GIF87a", b"GIF89a"))
    if ext == ".webp":
        return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    return False


def _display_name(filename: str) -> str:
    name = Path(filename or "").name
    name = re.sub(r"[\x00-\x1f]", "", name).strip()
    return (name or "未命名")[:200]


def public_attachment(row: dict) -> dict:
    upload_id = row["id"]
    return {
        "id": upload_id,
        "name": row.get("original_name") or row.get("name") or "附件",
        "mime": row.get("mime") or "",
        "kind": row.get("kind") or "",
        "size": int(row.get("size") or 0),
        "url": f"/api/uploads/{upload_id}",
    }


def _find_file(user_id: int, upload_id: str) -> Path | None:
    upload_id = _check_id(upload_id)
    directory = _user_dir(user_id)
    if not directory.is_dir():
        return None
    matches = [p for p in directory.glob(upload_id + ".*") if p.is_file() and p.suffix]
    return matches[0] if matches else None


def delete_files(user_id: int, upload_ids: list[str]) -> None:
    """删除磁盘上的附件。数据库记录由调用方另行清理。"""
    for upload_id in upload_ids:
        try:
            path = _find_file(user_id, upload_id)
        except AttachmentError:
            continue
        if path is None:
            continue
        try:
            path.unlink()
        except OSError as e:
            logger.warning(f"删除附件文件失败: {e}")


def cleanup_user_dir(user_id: int) -> None:
    directory = _user_dir(user_id)
    if not directory.is_dir():
        return
    import shutil
    try:
        shutil.rmtree(directory)
    except OSError as e:
        logger.warning(f"清理用户 #{user_id} 附件目录失败: {e}")


def _connect():
    return _get_connection(get_slot_mgr().pool)


def cleanup_stale(hours: int = 24) -> None:
    """删掉超过时限仍未挂到消息上的临时附件。"""
    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
    conn = _connect()
    rows = []
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT id, user_id FROM uploads WHERE message_id IS NULL AND created_at < %s",
                (cutoff,),
            )
            rows = list(cursor.fetchall() or [])
            if rows:
                ids = [r["id"] for r in rows]
                ph = ", ".join(["%s"] * len(ids))
                cursor.execute(f"DELETE FROM uploads WHERE id IN ({ph})", ids)
        conn.commit()
    except pymysql.Error as e:
        conn.rollback()
        logger.warning(f"清理临时附件失败: {e}")
        return
    finally:
        conn.close()
    for row in rows:
        delete_files(row["user_id"], [row["id"]])


def save_upload(user_id: int, filename: str, data: bytes) -> dict:
    """校验并保存一个尚未绑定消息的附件。"""
    if not data:
        raise AttachmentError("empty")
    ext = Path(filename or "").suffix.lower()
    if ext in IMAGE_TYPES:
        kind = "image"
        if len(data) > UPLOAD_MAX_IMAGE_BYTES:
            raise AttachmentError("image_too_large")
        if not _sniff_image(ext, data):
            raise AttachmentError("invalid")
        mime = IMAGE_TYPES[ext]
    elif ext in TEXT_TYPES:
        kind = "text"
        if len(data) > UPLOAD_MAX_TEXT_BYTES:
            raise AttachmentError("text_too_large")
        if b"\x00" in data:
            raise AttachmentError("invalid")
        mime = TEXT_TYPES[ext]
    else:
        raise AttachmentError("unsupported")

    upload_id = uuid.uuid4().hex
    directory = _user_dir(user_id)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{upload_id}{ext}"
    path.write_bytes(data)
    row = {
        "id": upload_id,
        "original_name": _display_name(filename),
        "mime": mime,
        "kind": kind,
        "size": len(data),
    }
    conn = _connect()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "INSERT INTO uploads (id, user_id, original_name, mime, kind, size, ext, message_id, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, NULL, %s)",
                (
                    upload_id, int(user_id), row["original_name"], mime, kind,
                    len(data), ext, datetime.now().isoformat(),
                ),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        try:
            path.unlink()
        except OSError:
            pass
        raise
    finally:
        conn.close()
    try:
        cleanup_stale()
    except Exception:
        logger.debug("清理临时附件时出错", exc_info=True)
    return public_attachment(row)


def fetch_pending(user_id: int, upload_ids: list[str]) -> list[dict] | None:
    """按请求顺序取未绑定的附件。任一不存在或已使用则返回 None。"""
    if not upload_ids:
        return []
    checked = []
    for upload_id in upload_ids:
        try:
            checked.append(_check_id(upload_id))
        except AttachmentError:
            return None
    conn = _connect()
    try:
        with conn.cursor() as cursor:
            ph = ", ".join(["%s"] * len(checked))
            cursor.execute(
                f"SELECT id, original_name, mime, kind, size FROM uploads "
                f"WHERE user_id = %s AND message_id IS NULL AND id IN ({ph})",
                (int(user_id), *checked),
            )
            rows = {r["id"]: r for r in cursor.fetchall() or []}
    finally:
        conn.close()
    if len(rows) != len(checked):
        return None
    return [public_attachment(rows[i]) for i in checked]


def resolve_download(user_id: int, upload_id: str) -> tuple[Path, str, str]:
    """确认附件属于该用户后返回路径、MIME 和原始文件名。"""
    upload_id = _check_id(upload_id)
    conn = _connect()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT original_name, mime FROM uploads WHERE id = %s AND user_id = %s",
                (upload_id, int(user_id)),
            )
            row = cursor.fetchone()
    finally:
        conn.close()
    if not row:
        raise AttachmentError("invalid")
    path = _find_file(user_id, upload_id)
    if path is None:
        raise AttachmentError("invalid")
    return path, row.get("mime") or "application/octet-stream", row.get("original_name") or "附件"


def _read_text(user_id: int, upload_id: str, limit: int) -> str:
    path = _find_file(user_id, upload_id)
    if path is None:
        return ""
    data = path.read_bytes()[:UPLOAD_MAX_TEXT_BYTES]
    text = data.decode("utf-8", errors="replace")
    if len(text) > limit:
        return text[:limit] + "\n…（文件已截断）"
    return text


def _image_data_url(user_id: int, upload_id: str, mime: str) -> str | None:
    path = _find_file(user_id, upload_id)
    if path is None or not path.is_file():
        return None
    raw = path.read_bytes()
    if not raw:
        return None
    encoded = base64.b64encode(raw).decode("ascii")
    return f"data:{mime or 'image/jpeg'};base64,{encoded}"


def append_model_text(content, extra: str):
    """把补充文本接到字符串或多模态 content 上。"""
    extra = (extra or "").strip()
    if not extra:
        return content
    if isinstance(content, str):
        return f"{content}\n\n{extra}" if content else extra
    if isinstance(content, list):
        parts = [dict(part) if isinstance(part, dict) else part for part in content]
        for part in parts:
            if isinstance(part, dict) and part.get("type") == "text":
                part["text"] = f"{part.get('text') or ''}\n\n{extra}".strip()
                return parts
        parts.insert(0, {"type": "text", "text": extra})
        return parts
    return extra


def parse_attachments(raw) -> list[dict]:
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    return []


def select_image_ids(history: list, limit: int | None = None) -> set[str]:
    """只把最近若干张图片再次发给模型，避免上下文被旧图撑满。"""
    cap = UPLOAD_MAX_CONTEXT_IMAGES if limit is None else limit
    chosen: list[str] = []
    for message in reversed(history or []):
        if message.get("role") != "user":
            continue
        for att in parse_attachments(message.get("attachments")):
            if att.get("kind") == "image" and att.get("id"):
                chosen.append(str(att["id"]))
                if len(chosen) >= cap:
                    return set(chosen)
    return set(chosen)


def build_user_content(user_id: int, text: str, attachments, image_ids: set[str], text_left: list[int]):
    """把一条用户消息变成模型可接受的 content（纯文本或图文数组）。"""
    notes: list[str] = []
    if (text or "").strip():
        notes.append(text.strip())
    images = []
    for att in parse_attachments(attachments):
        name = att.get("name") or "附件"
        kind = att.get("kind")
        upload_id = str(att.get("id") or "")
        if kind == "text":
            room = text_left[0] if text_left else 0
            if room <= 0:
                notes.append(f"[文件: {name}，内容过长未再次发送]")
                continue
            body = _read_text(user_id, upload_id, room) if upload_id else ""
            if not body:
                notes.append(f"[文件: {name}，内容不可用]")
                continue
            notes.append(f"[文件: {name}]\n{body}")
            if text_left:
                text_left[0] = max(0, text_left[0] - len(body))
        elif kind == "image":
            if upload_id not in image_ids:
                notes.append(f"[图片: {name}]")
                continue
            url = _image_data_url(user_id, upload_id, att.get("mime") or "image/jpeg")
            if not url:
                notes.append(f"[图片: {name}，文件不可用]")
                continue
            images.append({"type": "image_url", "image_url": {"url": url}})
    merged = "\n\n".join(notes).strip()
    if not images:
        return merged
    return [{"type": "text", "text": merged or "请查看图片。"}, *images]
