"""
管理员路由 — 用户列表、创建账号、重置密码、调整权限与删除。
"""
from __future__ import annotations

import logging
import shutil

from fastapi import APIRouter, Depends

from auth import current_user
from config import BACKGROUNDS_DIR
from attachments import cleanup_user_dir
from helpers import error
from models import AdminCreateUserRequest, AdminUpdateUserRequest
from state import get_auth_mgr

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin", tags=["admin"])


def require_admin(user: dict = Depends(current_user)) -> dict:
    """要求当前登录用户为管理员。"""
    if not user.get("is_admin"):
        error("forbidden", "需要管理员权限", 403)
    return user


def _remove_user_backgrounds(user_id: int) -> None:
    path = BACKGROUNDS_DIR / str(int(user_id))
    try:
        if path.is_dir():
            shutil.rmtree(path)
    except Exception:
        logger.warning(f"清理用户 #{user_id} 背景目录失败", exc_info=True)


@router.get("/users")
def list_users(admin: dict = Depends(require_admin)):
    return get_auth_mgr().list_users()


@router.post("/users")
def create_user(req: AdminCreateUserRequest, admin: dict = Depends(require_admin)):
    mgr = get_auth_mgr()
    try:
        user = mgr.create_user(req.username, req.password, is_admin=bool(req.is_admin))
    except ValueError as e:
        if str(e) == "duplicate":
            error("duplicate_user", "用户名已存在", 409)
        error("invalid_account", str(e), 400)
    except RuntimeError as e:
        error("create_failed", str(e), 500)
    logger.info(f"管理员 {admin.get('username')} 创建账号: {user['username']}")
    return user


@router.patch("/users/{user_id}")
def update_user(user_id: int, req: AdminUpdateUserRequest, admin: dict = Depends(require_admin)):
    if req.password is None and req.is_admin is None:
        error("invalid_request", "没有需要更新的字段", 400)

    mgr = get_auth_mgr()
    target = mgr.get_user_by_id(user_id)
    if not target:
        error("user_not_found", "用户不存在", 404)

    if req.password is not None:
        try:
            if not mgr.set_password(user_id, req.password):
                error("user_not_found", "用户不存在", 404)
        except ValueError as e:
            error("invalid_account", str(e), 400)
        except RuntimeError as e:
            error("update_failed", str(e), 500)
        if user_id != admin["id"]:
            mgr.delete_user_sessions(user_id)

    if req.is_admin is not None:
        try:
            if not mgr.set_admin(user_id, bool(req.is_admin)):
                error("user_not_found", "用户不存在", 404)
        except ValueError as e:
            if str(e) == "last_admin":
                error("last_admin", "不能取消最后一位管理员", 400)
            error("invalid_account", str(e), 400)
        except RuntimeError as e:
            error("update_failed", str(e), 500)

    updated = mgr.get_user_by_id(user_id) or target
    logger.info(f"管理员 {admin.get('username')} 更新账号 #{user_id}")
    return mgr.public_user(updated)


@router.delete("/users/{user_id}")
def delete_user(user_id: int, admin: dict = Depends(require_admin)):
    if user_id == admin["id"]:
        error("cannot_delete_self", "不能删除当前登录的账号", 400)

    mgr = get_auth_mgr()
    target = mgr.get_user_by_id(user_id)
    if not target:
        error("user_not_found", "用户不存在", 404)
    if target.get("is_admin") and mgr.count_admins() <= 1:
        error("last_admin", "不能删除最后一位管理员", 400)

    try:
        deleted = mgr.delete_user(user_id)
    except RuntimeError as e:
        error("delete_failed", str(e), 500)
    if not deleted:
        error("user_not_found", "用户不存在", 404)

    _remove_user_backgrounds(user_id)
    cleanup_user_dir(user_id)
    logger.info(f"管理员 {admin.get('username')} 删除账号 #{user_id} ({target.get('username')})")
    return {"ok": True}
