"""
对话附件接口 — 上传图片或文本，并在登录后读取。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, File, UploadFile
from fastapi.responses import FileResponse

from attachments import AttachmentError, resolve_download, save_upload
from auth import current_user
from config import UPLOAD_MAX_IMAGE_BYTES
from helpers import error

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/uploads", tags=["uploads"])

_STATUS = {
    "empty": 400,
    "unsupported": 400,
    "image_too_large": 400,
    "text_too_large": 400,
    "invalid": 400,
}


@router.post("")
async def upload_file(file: UploadFile = File(...), user: dict = Depends(current_user)):
    """保存一个附件，返回 id。发送消息时再绑定到用户消息。"""
    data = await file.read(UPLOAD_MAX_IMAGE_BYTES + 1)
    try:
        return save_upload(user["id"], file.filename or "", data)
    except AttachmentError as exc:
        error(exc.code, str(exc), _STATUS.get(exc.code, 400))


@router.get("/{upload_id}")
def download_file(upload_id: str, user: dict = Depends(current_user)):
    """读取当前用户自己的附件。"""
    try:
        path, mime, name = resolve_download(user["id"], upload_id)
    except AttachmentError:
        error("not_found", "附件不存在", 404)
    return FileResponse(
        path,
        media_type=mime,
        filename=name,
        content_disposition_type="inline",
        headers={"Cache-Control": "private, max-age=3600"},
    )
