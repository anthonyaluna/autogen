# api/routes/settings.py
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, SecretStr

from ...datamodel import Settings, SettingsConfig
from ..deps import get_current_user, get_db

router = APIRouter()

REDACTED_SECRET = "********"
SENSITIVE_KEYS = {"api_key", "client_secret", "password", "secret", "token", "access_token"}


class SettingsRequest(BaseModel):
    config: SettingsConfig

    model_config = ConfigDict(extra="ignore")


def _to_plain_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return {key: _to_plain_value(item) for key, item in value.model_dump().items()}
    if isinstance(value, dict):
        return {key: _to_plain_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_to_plain_value(item) for item in value]
    return value


def _redact_secrets(value: Any) -> Any:
    if isinstance(value, SecretStr):
        return REDACTED_SECRET
    if isinstance(value, BaseModel):
        return _redact_secrets(value.model_dump())
    if isinstance(value, list):
        return [_redact_secrets(item) for item in value]
    if isinstance(value, dict):
        redacted = {key: _redact_secrets(item) for key, item in value.items()}
        if value.get("type") == "secret" and "value" in value:
            redacted["value"] = REDACTED_SECRET
        for key in value:
            if key.lower() in SENSITIVE_KEYS and value[key] is not None:
                redacted[key] = REDACTED_SECRET
        return redacted
    return value


def _restore_redacted_secrets(value: Any, stored: Any) -> Any:
    value = _to_plain_value(value)
    stored = _to_plain_value(stored)
    if value == REDACTED_SECRET:
        return stored
    if isinstance(value, dict):
        stored_dict = stored if isinstance(stored, dict) else {}
        return {key: _restore_redacted_secrets(item, stored_dict.get(key)) for key, item in value.items()}
    if isinstance(value, list):
        stored_list = stored if isinstance(stored, list) else []
        stored_by_name = {
            item.get("name"): item for item in stored_list if isinstance(item, dict) and item.get("name") is not None
        }
        restored = []
        for index, item in enumerate(value):
            stored_item = stored_list[index] if index < len(stored_list) else None
            if isinstance(item, dict) and item.get("name") in stored_by_name:
                stored_item = stored_by_name[item["name"]]
            restored.append(_restore_redacted_secrets(item, stored_item))
        return restored
    return value


def _settings_response(settings: Settings) -> Dict[str, Any]:
    data = settings.model_dump()
    data["config"] = _redact_secrets(settings.config)
    return data


@router.get("/")
async def get_settings(user_id: str = Depends(get_current_user), db=Depends(get_db)) -> Dict:
    try:
        response = db.get(Settings, filters={"user_id": user_id}, return_json=False)
        if not response.status or not response.data:
            # create a default settings
            config = SettingsConfig()
            default_settings = Settings(user_id=user_id, config=_to_plain_value(config))
            created = db.create_owned(default_settings, user_id, return_json=False)
            if not created.status:
                raise HTTPException(status_code=500, detail="Failed to create settings")
            settings = created.data
        else:
            settings = response.data[0]
        return {"status": True, "data": _settings_response(settings)}
    except Exception as e:
        if isinstance(e, HTTPException):
            raise
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.put("/")
async def update_settings(
    settings: SettingsRequest, user_id: str = Depends(get_current_user), db=Depends(get_db)
) -> Dict:
    existing = db.get(Settings, filters={"user_id": user_id}, return_json=False)
    if not existing.status:
        raise HTTPException(status_code=500, detail="Failed to load settings")

    if existing.data:
        current = existing.data[0]
        config = _restore_redacted_secrets(settings.config, current.config)
        response = db.update_owned(
            Settings(id=current.id, config=_to_plain_value(SettingsConfig(**config))),
            user_id,
            return_json=False,
        )
    else:
        response = db.create_owned(Settings(config=_to_plain_value(settings.config)), user_id, return_json=False)

    if not response.status:
        raise HTTPException(status_code=400, detail=response.message)
    return {"status": True, "data": _settings_response(response.data)}
