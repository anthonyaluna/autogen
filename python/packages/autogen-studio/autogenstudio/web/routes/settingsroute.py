# api/routes/settings.py
from collections import Counter
from typing import Any, Dict

from autogen_core import ComponentModel
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, SecretStr, ValidationError

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


def _contains_mask(value: Any) -> bool:
    if isinstance(value, dict):
        return any(_contains_mask(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_mask(item) for item in value)
    return value == REDACTED_SECRET


def _settings_config_for_write(config: SettingsConfig, stored: Any) -> Dict[str, Any]:
    incoming = _to_plain_value(config)
    previous = _to_plain_value(stored) or {}
    variables = incoming["environment"]
    previous_variables = [
        item
        for item in (previous.get("environment") or [])
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    ]
    stored_counts = Counter(item.get("name") for item in previous_variables)
    submitted_counts = Counter(item["name"] for item in variables)
    stored_by_name = {item["name"]: item for item in previous_variables}

    for variable in variables:
        if variable["value"] != REDACTED_SECRET:
            continue
        name = variable["name"]
        original = stored_by_name.get(name, {})
        if variable["type"] != "secret":
            if original.get("type") != "secret":
                continue  # A public value can literally consist of eight asterisks.
            raise HTTPException(status_code=422, detail="Re-enter the value when changing a secret variable's type.")
        if (
            stored_counts[name] != 1
            or submitted_counts[name] != 1
            or original.get("type") != "secret"
            or original.get("value") is None
        ):
            raise HTTPException(status_code=422, detail="Re-enter the value for the changed environment variable.")
        variable["value"] = original["value"]

    model = incoming.get("default_model_client")
    previous_model = previous.get("default_model_client")
    if _contains_mask(model):
        # Provider configurations are opaque. Even a nested edit can select a different credential destination.
        detail = "Re-enter masked credentials when editing the model client."
        try:
            original_component = ComponentModel.model_validate(previous_model).model_dump()
        except ValidationError:
            raise HTTPException(status_code=422, detail=detail) from None
        if model != _redact_secrets(original_component):
            raise HTTPException(status_code=422, detail=detail)
        incoming["default_model_client"] = original_component
    return incoming


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
        config = _settings_config_for_write(settings.config, current.config)
        response = db.update_owned(
            Settings(id=current.id, config=_to_plain_value(SettingsConfig(**config))),
            user_id,
            return_json=False,
        )
    else:
        config = _settings_config_for_write(settings.config, None)
        response = db.create_owned(Settings(config=config), user_id, return_json=False)

    if not response.status:
        raise HTTPException(status_code=400, detail=response.message)
    return {"status": True, "data": _settings_response(response.data)}
