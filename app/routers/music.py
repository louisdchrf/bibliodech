import asyncio
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
    site = d.room.site if d.room else None
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
        "site_id": site.id if site else None,
        "site_name": site.name if site else None,
        "location_id": d.location_id,
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
    location_id: int | None = None


async def _apply_info_to_disc(disc: Disc, info: dict, db) -> None:
    """Applique les métadonnées enrichies sur un disque et sauvegarde la pochette."""
    from app.covers import fetch_and_save
    disc.title = info.get("title") or disc.barcode or str(disc.id)
    disc.artist = info.get("artist")
    disc.label = info.get("label")
    disc.catalog_number = info.get("catalog_number")
    disc.year = str(info["year"]) if info.get("year") else None
    disc.format = info.get("format")
    disc.track_count = info.get("track_count")
    disc.language = info.get("language")
    disc.country = info.get("country")
    disc.mbid = info.get("mbid")
    disc.enrichment_status = "ok"
    disc.source_data = json.dumps(info)
    remote_cover = info.get("cover_url")
    if remote_cover:
        key = f"disc_{disc.barcode or disc.id}"
        local = await fetch_and_save(key, remote_cover)
        disc.cover_url = local or remote_cover
    else:
        disc.cover_url = None
    db.commit()


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
        await _apply_info_to_disc(disc, info, db)
    finally:
        db.close()


async def _reenrich_missing_discs(db, task_id: str = "reenrich-discs") -> dict:
    """Re-enrichit les disques not_found ou pending."""
    from app import scheduler as sched

    discs = db.query(Disc).filter(
        Disc.enrichment_status.in_(["not_found", "pending"])
    ).filter(Disc.barcode.isnot(None)).all()

    total = len(discs)
    updated = 0
    discogs_key = cfg.get(db, "discogs_api_key") or ""

    if task_id in sched._running:
        sched._running[task_id]["progress"] = {"current": 0, "total": total}

    for i, disc in enumerate(discs):
        info = await lookup_barcode(disc.barcode, discogs_key)
        if info:
            await _apply_info_to_disc(disc, info, db)
            updated += 1
        else:
            disc.enrichment_status = "not_found"
            db.commit()
        if task_id in sched._running:
            sched._running[task_id]["progress"] = {"current": i + 1, "total": total}
        await asyncio.sleep(1.2)

    return {"updated": updated, "total": total}


async def _covers_logic(db, task_id: str, only_missing: bool) -> dict:
    """Cherche/re-télécharge les pochettes de disques.
    only_missing=True : uniquement les disques sans cover.
    only_missing=False : tous les disques enrichis.
    """
    from app.covers import fetch_and_save
    from app.lookup_music import _cover_from_mb_search, _resolve_cover
    from app import scheduler as sched

    query = db.query(Disc).filter(Disc.enrichment_status == "ok")
    if only_missing:
        query = query.filter((Disc.cover_url.is_(None)) | (Disc.cover_url == ""))
    discs = query.all()

    total = len(discs)
    updated = 0
    discogs_key = cfg.get(db, "discogs_api_key") or ""

    if task_id in sched._running:
        sched._running[task_id]["progress"] = {"current": 0, "total": total}

    for i, disc in enumerate(discs):
        url = await _resolve_cover(
            {"mbid": disc.mbid, "artist": disc.artist, "title": disc.title},
            disc.barcode or "",
            discogs_key,
        )
        if url:
            key = f"disc_{disc.barcode or disc.id}"
            local = await fetch_and_save(key, url)
            if local:
                disc.cover_url = local
                db.commit()
                updated += 1
        if task_id in sched._running:
            sched._running[task_id]["progress"] = {"current": i + 1, "total": total}
        await asyncio.sleep(0.3)

    return {"updated": updated, "total": total}


async def _fetch_disc_covers_logic(db, task_id: str = "fetch-disc-covers") -> dict:
    return await _covers_logic(db, task_id, only_missing=True)


async def _refresh_disc_covers_logic(db, task_id: str = "refresh-disc-covers") -> dict:
    return await _covers_logic(db, task_id, only_missing=False)


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
    if not is_music_barcode(barcode):
        raise HTTPException(status_code=400, detail="Ce code-barres semble être un ISBN, utilisez /api/scan")

    existing = db.query(Disc).filter(Disc.barcode == barcode).first()
    if existing:
        return {"status": "exists", "disc": disc_to_dict(existing)}

    valid_room_id = body.room_id if (body.room_id and db.query(Room).filter(Room.id == body.room_id).first()) else None

    disc = Disc(
        barcode=barcode,
        title=barcode,
        room_id=valid_room_id,
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


@router.post("/api/music/{disc_id}/reenrich")
async def reenrich_disc(
    disc_id: int,
    background_tasks: BackgroundTasks,
    request: Request,
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    require_contributor(user)
    disc = db.query(Disc).filter(Disc.id == disc_id).first()
    if not disc:
        raise HTTPException(status_code=404, detail="Disque introuvable")
    if not disc.barcode:
        raise HTTPException(status_code=400, detail="Pas de code-barres pour relancer l'enrichissement")
    disc.enrichment_status = "pending"
    db.commit()
    background_tasks.add_task(_enrich_disc, disc.id, disc.barcode)
    return {"status": "pending"}


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
