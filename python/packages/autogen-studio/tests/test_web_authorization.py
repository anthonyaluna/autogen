import asyncio
from typing import Generator

import pytest
from fastapi import HTTPException

from autogenstudio.database import DatabaseManager
from autogenstudio.datamodel import (
    EnvironmentVariable,
    Gallery,
    Run,
    RunStatus,
    Session,
    Settings,
    SettingsConfig,
    Team,
)
from autogenstudio.web.routes.gallery import (
    GalleryRequest,
    delete_gallery_entry,
    get_gallery_entry,
    update_gallery_entry,
)
from autogenstudio.web.routes.runs import CreateRunRequest, create_run, get_run, get_run_messages
from autogenstudio.web.routes.sessions import (
    SessionRequest,
    create_session,
    delete_session,
    get_session,
    list_session_runs,
    update_session,
)
from autogenstudio.web.routes.settingsroute import SettingsRequest, get_settings, update_settings
from autogenstudio.web.routes.teams import TeamRequest, delete_team, get_team, update_team

OWNER = "owner@example.com"
ATTACKER = "attacker@example.com"


@pytest.fixture
def authorization_db(tmp_path) -> Generator[DatabaseManager, None, None]:
    db = DatabaseManager(f"sqlite:///{tmp_path / 'authorization.db'}", base_dir=tmp_path)
    db.initialize_database(auto_upgrade=False, force_init_alembic=False)
    yield db
    asyncio.run(db.close())


def _save(db: DatabaseManager, model):
    response = db.upsert(model, return_json=False)
    assert response.status is True
    return response.data


def _owned_resources(db: DatabaseManager):
    team = _save(db, Team(user_id=OWNER, component={"label": "Owner team"}))
    session = _save(db, Session(user_id=OWNER, team_id=team.id, name="Owner session"))
    run = _save(
        db,
        Run(
            user_id=OWNER,
            session_id=session.id,
            status=RunStatus.CREATED,
            task={},
            team_result={},
            messages=[],
        ),
    )
    gallery = _save(db, Gallery(user_id=OWNER, config={"name": "Owner gallery"}))
    return team, session, run, gallery


def test_write_models_ignore_owner_and_ids() -> None:
    session = SessionRequest.model_validate({"id": 42, "user_id": OWNER, "team_id": 7, "name": "Session"})
    run = CreateRunRequest.model_validate({"id": 42, "user_id": OWNER, "session_id": 7})
    team = TeamRequest.model_validate({"id": 42, "user_id": OWNER, "component": {"label": "Team"}})
    gallery = GalleryRequest.model_validate({"id": 42, "user_id": OWNER, "config": {"name": "Gallery"}})
    settings = SettingsRequest.model_validate(
        {"id": 42, "user_id": OWNER, "config": SettingsConfig().model_dump()}
    )

    for request_model in (session, run, team, gallery, settings):
        assert "id" not in request_model.model_dump()
        assert "user_id" not in request_model.model_dump()


@pytest.mark.asyncio
async def test_other_users_cannot_read_owned_resources(authorization_db: DatabaseManager) -> None:
    team, session, run, gallery = _owned_resources(authorization_db)

    with pytest.raises(HTTPException) as session_error:
        await get_session(session.id, user_id=ATTACKER, db=authorization_db)
    assert session_error.value.status_code == 404

    with pytest.raises(HTTPException) as history_error:
        await list_session_runs(session.id, user_id=ATTACKER, db=authorization_db)
    assert history_error.value.status_code == 404

    with pytest.raises(HTTPException) as run_error:
        await get_run(run.id, user_id=ATTACKER, db=authorization_db)
    assert run_error.value.status_code == 404

    with pytest.raises(HTTPException) as messages_error:
        await get_run_messages(run.id, user_id=ATTACKER, db=authorization_db)
    assert messages_error.value.status_code == 404

    with pytest.raises(HTTPException) as team_error:
        await get_team(team.id, user_id=ATTACKER, db=authorization_db)
    assert team_error.value.status_code == 404

    with pytest.raises(HTTPException) as gallery_error:
        await get_gallery_entry(gallery.id, user_id=ATTACKER, db=authorization_db)
    assert gallery_error.value.status_code == 404


@pytest.mark.asyncio
async def test_other_users_cannot_mutate_owned_resources(authorization_db: DatabaseManager) -> None:
    team, session, run, gallery = _owned_resources(authorization_db)
    owner_settings = _save(
        authorization_db,
        Settings(
            user_id=OWNER,
            config=SettingsConfig(
                environment=[EnvironmentVariable(name="OWNER_TOKEN", value="owner-secret", type="secret")]
            ).model_dump(mode="json"),
        ),
    )

    ownership_transfer = authorization_db.upsert(
        Team(id=team.id, user_id=ATTACKER, component={"label": "Transferred team"})
    )
    assert ownership_transfer.status is False

    with pytest.raises(HTTPException) as session_error:
        await update_session(
            session.id,
            SessionRequest(team_id=None, name="Stolen session"),
            user_id=ATTACKER,
            db=authorization_db,
        )
    assert session_error.value.status_code == 404

    with pytest.raises(HTTPException) as team_error:
        await update_team(
            team.id,
            TeamRequest(component={"label": "Stolen team"}),
            user_id=ATTACKER,
            db=authorization_db,
        )
    assert team_error.value.status_code == 404

    with pytest.raises(HTTPException) as gallery_error:
        await update_gallery_entry(
            gallery.id,
            GalleryRequest(config={"name": "Stolen gallery"}),
            user_id=ATTACKER,
            db=authorization_db,
        )
    assert gallery_error.value.status_code == 404

    with pytest.raises(HTTPException) as run_error:
        await create_run(CreateRunRequest(session_id=session.id), user_id=ATTACKER, db=authorization_db)
    assert run_error.value.status_code == 404

    attacker_settings = await update_settings(
        SettingsRequest(config=SettingsConfig(environment=[])),
        user_id=ATTACKER,
        db=authorization_db,
    )
    assert attacker_settings["data"]["user_id"] == ATTACKER

    await delete_session(session.id, user_id=ATTACKER, db=authorization_db)
    await delete_team(team.id, user_id=ATTACKER, db=authorization_db)
    with pytest.raises(HTTPException):
        await delete_gallery_entry(gallery.id, user_id=ATTACKER, db=authorization_db)

    assert authorization_db.get(Session, {"id": session.id, "user_id": OWNER}).data
    assert authorization_db.get(Team, {"id": team.id, "user_id": OWNER}).data
    assert authorization_db.get(Gallery, {"id": gallery.id, "user_id": OWNER}).data
    assert authorization_db.get(Run, {"id": run.id, "user_id": OWNER}).data
    stored_owner_settings = authorization_db.get(Settings, {"id": owner_settings.id}, return_json=False).data[0]
    stored_owner_config = (
        stored_owner_settings.config
        if isinstance(stored_owner_settings.config, SettingsConfig)
        else SettingsConfig(**stored_owner_settings.config)
    )
    assert stored_owner_config.environment[0].value == "owner-secret"


@pytest.mark.asyncio
async def test_owned_workflows_and_secret_redaction(authorization_db: DatabaseManager) -> None:
    owner_team = _save(authorization_db, Team(user_id=OWNER, component={"label": "Owner team"}))
    attacker_team = _save(authorization_db, Team(user_id=ATTACKER, component={"label": "Attacker team"}))

    relation_error = await create_session(
        SessionRequest(team_id=owner_team.id, name="Cross-owner session"),
        user_id=ATTACKER,
        db=authorization_db,
    )
    assert relation_error.status is False

    created_session = await create_session(
        SessionRequest(team_id=attacker_team.id, name="Attacker session"),
        user_id=ATTACKER,
        db=authorization_db,
    )
    assert created_session.status is True
    assert created_session.data["user_id"] == ATTACKER

    updated_team = await update_team(
        attacker_team.id,
        TeamRequest(component={"label": "Updated attacker team"}),
        user_id=ATTACKER,
        db=authorization_db,
    )
    assert updated_team["data"]["component"]["label"] == "Updated attacker team"

    updated_session = await update_session(
        created_session.data["id"],
        SessionRequest(team_id=attacker_team.id, name="Updated attacker session"),
        user_id=ATTACKER,
        db=authorization_db,
    )
    assert updated_session["data"]["name"] == "Updated attacker session"

    attacker_gallery = _save(authorization_db, Gallery(user_id=ATTACKER, config={"name": "Gallery"}))
    updated_gallery = await update_gallery_entry(
        attacker_gallery.id,
        GalleryRequest(config={"name": "Updated gallery"}),
        user_id=ATTACKER,
        db=authorization_db,
    )
    assert updated_gallery.data["config"]["name"] == "Updated gallery"

    created_run = await create_run(
        CreateRunRequest(session_id=created_session.data["id"]),
        user_id=ATTACKER,
        db=authorization_db,
    )
    assert created_run["status"] is True

    secret_settings = SettingsConfig(
        environment=[EnvironmentVariable(name="SERVICE_TOKEN", value="persisted-secret", type="secret")]
    )
    _save(authorization_db, Settings(user_id=ATTACKER, config=secret_settings.model_dump(mode="json")))

    settings_response = await get_settings(user_id=ATTACKER, db=authorization_db)
    assert settings_response["data"]["config"]["environment"][0]["value"] == "********"

    settings_response["data"]["config"]["ui"]["show_llm_call_events"] = True
    updated = await update_settings(
        SettingsRequest(config=settings_response["data"]["config"]),
        user_id=ATTACKER,
        db=authorization_db,
    )
    assert updated["data"]["config"]["environment"][0]["value"] == "********"

    stored = authorization_db.get(Settings, {"user_id": ATTACKER}, return_json=False).data[0]
    stored_config = stored.config if isinstance(stored.config, SettingsConfig) else SettingsConfig(**stored.config)
    assert stored_config.environment[0].value == "persisted-secret"
    assert stored_config.ui.show_llm_call_events is True


@pytest.mark.parametrize(
    "changes",
    [
        {"name": "RENAMED"},
        {"type": "string"},
    ],
)
@pytest.mark.asyncio
async def test_environment_secret_edit_requires_reentry_when_identity_changes(authorization_db, changes) -> None:
    config = SettingsConfig(environment=[EnvironmentVariable(name="SERVICE", value="fixture-secret", type="secret")])
    _save(authorization_db, Settings(user_id=ATTACKER, config=config.model_dump(mode="json")))
    data = (await get_settings(user_id=ATTACKER, db=authorization_db))["data"]["config"]
    data["environment"][0].update(changes)

    with pytest.raises(HTTPException) as error:
        await update_settings(SettingsRequest(config=data), user_id=ATTACKER, db=authorization_db)
    assert error.value.status_code == 422

    data["environment"][0]["value"] = "replacement"
    updated = await update_settings(SettingsRequest(config=data), user_id=ATTACKER, db=authorization_db)
    expected = "replacement" if changes.get("type") == "string" else "********"
    assert updated["data"]["config"]["environment"][0]["value"] == expected


@pytest.mark.asyncio
async def test_reordered_environment_secrets_keep_their_names(authorization_db) -> None:
    config = SettingsConfig(
        environment=[
            EnvironmentVariable(name="FIRST", value="first-example", type="secret"),
            EnvironmentVariable(name="SECOND", value="second-example", type="secret"),
        ]
    )
    _save(authorization_db, Settings(user_id=ATTACKER, config=config.model_dump(mode="json")))
    data = (await get_settings(user_id=ATTACKER, db=authorization_db))["data"]["config"]
    data["environment"].reverse()
    await update_settings(SettingsRequest(config=data), user_id=ATTACKER, db=authorization_db)
    stored = authorization_db.get(Settings, {"user_id": ATTACKER}, return_json=False).data[0]
    saved = SettingsConfig.model_validate(stored.config)
    assert [(entry.name, entry.value) for entry in saved.environment] == [
        ("SECOND", "second-example"),
        ("FIRST", "first-example"),
    ]


@pytest.mark.asyncio
async def test_model_change_requires_credential_reentry(authorization_db) -> None:
    data = (await get_settings(user_id=ATTACKER, db=authorization_db))["data"]["config"]
    component = data["default_model_client"]
    component["config"]["api_key"] = "first-example"
    saved = await update_settings(SettingsRequest(config=data), user_id=ATTACKER, db=authorization_db)
    data = saved["data"]["config"]
    assert data["default_model_client"]["config"]["api_key"] == "********"

    component = data["default_model_client"]
    component["provider"] = "example.OtherClient"
    with pytest.raises(HTTPException) as error:
        await update_settings(SettingsRequest(config=data), user_id=ATTACKER, db=authorization_db)
    assert error.value.status_code == 422

    component["config"]["api_key"] = "replacement-example"
    saved = await update_settings(SettingsRequest(config=data), user_id=ATTACKER, db=authorization_db)
    assert saved["data"]["config"]["default_model_client"]["config"]["api_key"] == "********"
