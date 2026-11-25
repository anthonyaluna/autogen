# api/routes/gallery.py
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict

from ...database import DatabaseManager
from ...datamodel import Gallery, Response
from ...gallery.builder import create_default_gallery
from ..deps import get_current_user, get_db

router = APIRouter()


class GalleryRequest(BaseModel):
    config: dict

    model_config = ConfigDict(extra="ignore")


@router.put("/{gallery_id}")
async def update_gallery_entry(
    gallery_id: int,
    gallery_data: GalleryRequest,
    user_id: str = Depends(get_current_user),
    db: DatabaseManager = Depends(get_db),
) -> Response:
    result = db.update_owned(Gallery(id=gallery_id, config=gallery_data.config), user_id)
    if not result.status:
        raise HTTPException(status_code=404, detail="Gallery entry not found")
    return result


@router.post("/")
async def create_gallery_entry(
    gallery_data: GalleryRequest,
    user_id: str = Depends(get_current_user),
    db: DatabaseManager = Depends(get_db),
) -> Response:
    response = db.create_owned(Gallery(config=gallery_data.config), user_id)
    if not response.status:
        raise HTTPException(status_code=400, detail=response.message)
    return response


@router.get("/")
async def list_gallery_entries(
    user_id: str = Depends(get_current_user), db: DatabaseManager = Depends(get_db)
) -> Response:
    try:
        result = db.get(Gallery, filters={"user_id": user_id})
        if not result.data or len(result.data) == 0:
            # create a default gallery entry
            gallery_config = create_default_gallery()
            default_gallery = Gallery(user_id=user_id, config=gallery_config.model_dump())
            db.create_owned(default_gallery, user_id)
            result = db.get(Gallery, filters={"user_id": user_id})
        return result
    except Exception as e:
        return Response(status=False, data=[], message=f"Error retrieving gallery entries: {str(e)}")


@router.get("/{gallery_id}")
async def get_gallery_entry(
    gallery_id: int,
    user_id: str = Depends(get_current_user),
    db: DatabaseManager = Depends(get_db),
) -> Response:
    result = db.get(Gallery, filters={"id": gallery_id, "user_id": user_id})
    if not result.status or not result.data:
        raise HTTPException(status_code=404, detail="Gallery entry not found")

    return Response(status=result.status, data=result.data[0], message=result.message)


@router.delete("/{gallery_id}")
async def delete_gallery_entry(
    gallery_id: int,
    user_id: str = Depends(get_current_user),
    db: DatabaseManager = Depends(get_db),
) -> Response:
    # Check ownership first
    result = db.get(Gallery, filters={"id": gallery_id, "user_id": user_id})

    if not result.status or not result.data:
        raise HTTPException(status_code=404, detail="Gallery entry not found")
    response = db.delete(Gallery, filters={"id": gallery_id, "user_id": user_id})
    # Delete if authorized
    return response
