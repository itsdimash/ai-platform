from dataclasses import dataclass

from fastapi import Header, HTTPException
from jose import JWTError, jwt

from .config import get_settings


@dataclass
class CurrentUser:
    user_id: int
    role: str


async def get_current_user(authorization: str = Header(...)) -> CurrentUser:
    """Проверяет тот же JWT, что выдаёт ERP-бэкенд — общий секрет, не нужно
    логиниться дважды. Ожидаемые claims: sub (user_id), role."""

    settings = get_settings()

    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Отсутствует Bearer-токен")

    token = authorization.removeprefix("Bearer ")
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except JWTError as e:
        raise HTTPException(status_code=401, detail="Невалидный токен") from e

    try:
        return CurrentUser(user_id=int(payload["sub"]), role=payload["role"])
    except (KeyError, ValueError) as e:
        raise HTTPException(status_code=401, detail="Токен без обязательных claims") from e
