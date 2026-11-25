# api/routes/sessions.py
from typing import Dict

from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from pydantic import BaseModel, ConfigDict

from ...datamodel import Message, Response, Run, Session, Team
from ..deps import get_current_user, get_db

router = APIRouter()


class SessionRequest(BaseModel):
    team_id: int | None = None
    name: str | None = None

    model_config = ConfigDict(extra="ignore")


def _require_owned_team(team_id: int | None, user_id: str, db) -> None:
    if team_id is None:
        return
    team = db.get(Team, filters={"id": team_id, "user_id": user_id}, return_json=False)
    if not team.status or not team.data:
        raise HTTPException(status_code=404, detail="Team not found")


@router.get("/")
async def list_sessions(user_id: str = Depends(get_current_user), db=Depends(get_db)) -> Dict:
    """List all sessions for a user"""
    response = db.get(Session, filters={"user_id": user_id})
    return {"status": True, "data": response.data}


@router.get("/{session_id}")
async def get_session(session_id: int, user_id: str = Depends(get_current_user), db=Depends(get_db)) -> Dict:
    """Get a specific session"""
    response = db.get(Session, filters={"id": session_id, "user_id": user_id})
    if not response.status or not response.data:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"status": True, "data": response.data[0]}


@router.post("/")
async def create_session(
    session: SessionRequest, user_id: str = Depends(get_current_user), db=Depends(get_db)
) -> Response:
    """Create a new session"""
    try:
        _require_owned_team(session.team_id, user_id, db)
        response = db.create_owned(Session(team_id=session.team_id, name=session.name), user_id)
        if not response.status:
            return Response(status=False, message=f"Failed to create session: {response.message}")
        return Response(status=True, data=response.data, message="Session created successfully")
    except Exception as e:
        logger.error(f"Error creating session: {str(e)}")
        return Response(status=False, message=f"Failed to create session: {str(e)}")


@router.put("/{session_id}")
async def update_session(
    session_id: int,
    session: SessionRequest,
    user_id: str = Depends(get_current_user),
    db=Depends(get_db),
) -> Dict:
    """Update an existing session"""
    _require_owned_team(session.team_id, user_id, db)
    response = db.update_owned(Session(id=session_id, team_id=session.team_id, name=session.name), user_id)
    if not response.status:
        raise HTTPException(status_code=404, detail="Session not found")

    return {"status": True, "data": response.data, "message": "Session updated successfully"}


@router.delete("/{session_id}")
async def delete_session(session_id: int, user_id: str = Depends(get_current_user), db=Depends(get_db)) -> Dict:
    """Delete a session"""
    db.delete(filters={"id": session_id, "user_id": user_id}, model_class=Session)
    return {"status": True, "message": "Session deleted successfully"}


@router.get("/{session_id}/runs")
async def list_session_runs(
    session_id: int, user_id: str = Depends(get_current_user), db=Depends(get_db)
) -> Dict:
    """Get complete session history organized by runs"""

    try:
        # 1. Verify session exists and belongs to user
        session = db.get(Session, filters={"id": session_id, "user_id": user_id}, return_json=False)
        if not session.status:
            raise HTTPException(status_code=500, detail="Database error while fetching session")
        if not session.data:
            raise HTTPException(status_code=404, detail="Session not found or access denied")

        # 2. Get ordered runs for session
        runs = db.get(
            Run, filters={"session_id": session_id, "user_id": user_id}, order="asc", return_json=False
        )
        if not runs.status:
            raise HTTPException(status_code=500, detail="Database error while fetching runs")

        # 3. Build response with messages per run
        run_data = []
        if runs.data:  # It's ok to have no runs
            for run in runs.data:
                try:
                    # Get messages for this specific run
                    messages = db.get(Message, filters={"run_id": run.id}, order="asc", return_json=False)
                    if not messages.status:
                        logger.error(f"Failed to fetch messages for run {run.id}")
                        # Continue processing other runs even if one fails
                        messages.data = []

                    run_data.append(
                        {
                            "id": str(run.id),
                            "created_at": run.created_at,
                            "status": run.status,
                            "task": run.task,
                            "team_result": run.team_result,
                            "messages": messages.data or [],
                        }
                    )
                except Exception as e:
                    logger.error(f"Error processing run {run.id}: {str(e)}")
                    # Include run with error state instead of failing entirely
                    run_data.append(
                        {
                            "id": str(run.id),
                            "created_at": run.created_at,
                            "status": "ERROR",
                            "task": run.task,
                            "team_result": None,
                            "messages": [],
                            "error": f"Failed to process run: {str(e)}",
                        }
                    )

        return {"status": True, "data": {"runs": run_data}}

    except HTTPException:
        raise  # Re-raise HTTP exceptions
    except Exception as e:
        logger.error(f"Unexpected error in list_messages: {str(e)}")
        raise HTTPException(status_code=500, detail="Internal server error while fetching session data") from e
