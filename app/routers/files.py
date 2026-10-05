from fastapi import APIRouter, Depends, HTTPException

from ..auth import CurrentUser, get_current_user
from ..config import get_settings
from ..utils.r2 import StorageNotConfiguredError, head_info_async, presign_get
from ..utils.storage import can_access_key, is_inline_mime

router = APIRouter()


@router.get("/v1/files/{key:path}/url")
async def get_file_url(key: str, user: CurrentUser = Depends(get_current_user)) -> dict:
    """Свежая presigned-ссылка на файл ai-platform.

    Доступ: владелец ключа (ai/{user_id}/...) и роли commercial_director/admin.
    Ссылки на чужие и некорректные ключи не выдаются (403, без различий —
    чтобы по ответу нельзя было проверять существование чужих объектов).
    """
    if not can_access_key(key, user.user_id, user.role):
        raise HTTPException(status_code=403, detail="Нет доступа к файлу")

    try:
        info = await head_info_async(key)
        if info is None:
            raise HTTPException(status_code=404, detail="Файл не найден")
        name = info["name"] or key.rsplit("/", 1)[-1]
        url = presign_get(
            key,
            download_name=name,
            inline=is_inline_mime(info["mime"]),
            content_type=info["mime"],
        )
    except StorageNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail="Хранилище файлов не настроено") from exc

    return {"url": url, "expires_in": get_settings().r2_presign_expires}
