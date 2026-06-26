import json
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth import get_current_user, require_contributor
from app.database import get_db, SessionLocal
from app.lookup_music import lookup_barcode, is_music_barcode
from app.models import Disc, Room
from app import settings as cfg

router = APIRouter()


def disc_to_dict(d: Disc) -> dict:
    return {
        "id": d.id,
        "barcode": d.barcode,
        "title": d.title,
        "artist": d.artist,
        "label": d.label,
        "catalog_number": d.catalog_number,
        "year": d.year,
        "format": d.format,
        "genre": d.genre,
        "track_count": d.track_count,
        "language": d.language,
        "country": d.country,
        "cover_url": d.cover_url,
        "mbid": d.mbid,
        "room_id": d.room_id,
        "room_name": d.room.name if d.room else None,
        "enrichment_status": d.enrichment_status,
        "added_at": d.added_at.isoformat() if d.added_at else None,
    }


class DiscScanRequest(BaseModel):
    barcode: str
    room_id: int | None = None


class DiscUpdateRequest(BaseModel):
    title: str | None = None
    artist: str | None = None
    label: str | None = None
    year: str | None = None
    format: str | None = None
    genre: str | None = None
    room_id: int | None = None


async def _enrich_disc(disc_id: int, barcode: str) -> None:
    db = SessionLocal()
    try:
        disc = db.query(Disc).filter(Disc.id == disc_id).first()
        if not disc:
            return
        discogs_key = cfg.get(db, "discogs_api_key") or ""
        info = await lookup_barcode(barcode, discogs_key)
        if not info:
            disc.enrichment_status = "not_found"
            db.commit()
            return
        disc.title = info.get("title") or barcode
        disc.artist = info.get("artist")
        disc.label = info.get("label")
        disc.catalog_number = info.get("catalog_number")
        disc.year = str(info["year"]) if info.get("year") else None
        disc.format = info.get("format")
        disc.track_count = info.get("track_count")
        disc.language = info.get("language")
        disc.country = info.get("country")
        # Télécharger et mettre en cache la pochette localement
        remote_cover = info.get("cover_url")
        if remote_cover:
            from app.covers import fetch_and_save
            local = await fetch_and_save(f"disc_{barcode}", remote_cover)
            disc.cover_url = local or remote_cover
        else:
            disc.cover_url = None
        disc.mbid = info.get("mbid")
        disc.enrichment_status = "ok"
        disc.source_data = json.dumps(info)
        db.commit()
    finally:
        db.close()


@router.post("/api/scan/music")
async def scan_music(
    body: DiscScanRequest,
    background_tasks: BackgroundTasks,
    request: Request,
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    require_contributor(user)

    barcode = body.barcode.strip()
    if not barcode:
        raise HTTPException(status_code=400, detail="Code-barres manquant")

    existing = db.query(Disc).filter(Disc.barcode == barcode).first()
    if existing:
        return {"status": "exists", "disc": disc_to_dict(existing)}

    disc = Disc(
        barcode=barcode,
        title=barcode,
        room_id=body.room_id,
        added_at=datetime.now(timezone.utc),
        added_by=user.id,
        enrichment_status="pending",
    )
    db.add(disc)
    db.commit()
    db.refresh(disc)

    background_tasks.add_task(_enrich_disc, disc.id, barcode)
    return {"status": "pending", "disc": disc_to_dict(disc)}


@router.get("/api/music")
def list_discs(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    discs = db.query(Disc).order_by(Disc.artist, Disc.title).all()
    return [disc_to_dict(d) for d in discs]


@router.get("/api/music/{disc_id}")
def get_disc(disc_id: int, request: Request, db: Session = Depends(get_db)):
    get_current_user(request, db)
    disc = db.query(Disc).filter(Disc.id == disc_id).first()
    if not disc:
        raise HTTPException(status_code=404, detail="Disque introuvable")
    return disc_to_dict(disc)


@router.put("/api/music/{disc_id}")
def update_disc(
    disc_id: int,
    body: DiscUpdateRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    require_contributor(user)
    disc = db.query(Disc).filter(Disc.id == disc_id).first()
    if not disc:
        raise HTTPException(status_code=404, detail="Disque introuvable")
    for field, val in body.model_dump(exclude_unset=True).items():
        setattr(disc, field, val)
    db.commit()
    db.refresh(disc)
    return disc_to_dict(disc)


@router.delete("/api/music/{disc_id}")
def delete_disc(disc_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    require_contributor(user)
    disc = db.query(Disc).filter(Disc.id == disc_id).first()
    if not disc:
        raise HTTPException(status_code=404, detail="Disque introuvable")
    db.delete(disc)
    db.commit()
    return {"ok": True}
