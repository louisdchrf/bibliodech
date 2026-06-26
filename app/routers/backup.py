import os
import shutil
from datetime import datetime, timezone
import zoneinfo
from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.auth import get_current_user, require_admin
from app.database import get_db, DATABASE_URL
import app.settings as cfg

router = APIRouter()

DB_PATH = DATABASE_URL.replace("sqlite:///", "")
BACKUP_DIR = "/app/data/backups"
DEFAULT_MAX_BACKUPS = 10


def _ensure_dir():
    os.makedirs(BACKUP_DIR, exist_ok=True)


def _get_max_backups(db) -> int:
    val = cfg.get(db, "backup_max_count")
    try:
        return max(1, int(val)) if val else DEFAULT_MAX_BACKUPS
    except (ValueError, TypeError):
        return DEFAULT_MAX_BACKUPS


def _list_backups(db=None) -> list[dict]:
    _ensure_dir()
    tz_name = cfg.get(db, "timezone") if db else None
    try:
        tz = zoneinfo.ZoneInfo(tz_name or "Europe/Paris")
    except Exception:
        tz = zoneinfo.ZoneInfo("Europe/Paris")
    files = []
    for name in sorted(os.listdir(BACKUP_DIR), reverse=True):
        if not name.endswith(".db"):
            continue
        path = os.path.join(BACKUP_DIR, name)
        stat = os.stat(path)
        local_dt = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).astimezone(tz)
        files.append({
            "filename": name,
            "size": stat.st_size,
            "created_at": local_dt.isoformat(),
        })
    return files


def _local_now(db=None) -> datetime:
    tz_name = cfg.get(db, "timezone") if db else None
    try:
        tz = zoneinfo.ZoneInfo(tz_name or "Europe/Paris")
    except Exception:
        tz = zoneinfo.ZoneInfo("Europe/Paris")
    return datetime.now(tz)


def _create_backup(db=None) -> dict:
    _ensure_dir()
    ts = _local_now(db).strftime("%Y%m%d_%H%M%S")
    dest = os.path.join(BACKUP_DIR, f"backup_{ts}.db")
    shutil.copy2(DB_PATH, dest)
    # Rotation
    max_backups = _get_max_backups(db) if db else DEFAULT_MAX_BACKUPS
    all_backups = sorted([f for f in os.listdir(BACKUP_DIR) if f.endswith(".db")])
    for old in all_backups[:-max_backups]:
        os.remove(os.path.join(BACKUP_DIR, old))
    stat = os.stat(dest)
    return {
        "filename": os.path.basename(dest),
        "size": stat.st_size,
        "created_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
    }


@router.get("/api/backups")
def list_backups(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    require_admin(user)
    return {
        "backups": _list_backups(db),
        "max_count": _get_max_backups(db),
    }


@router.post("/api/backups", status_code=201)
def create_backup(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    require_admin(user)
    return _create_backup(db)


@router.post("/api/backups/settings")
def save_backup_settings(body: dict, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    require_admin(user)
    max_count = body.get("max_count")
    try:
        max_count = max(1, int(max_count))
    except (ValueError, TypeError):
        raise HTTPException(status_code=422, detail="Valeur invalide")
    cfg.set_(db, "backup_max_count", str(max_count))
    return {"max_count": max_count}


@router.get("/api/backups/{filename}/download")
def download_backup(filename: str, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    require_admin(user)
    if ".." in filename or "/" in filename:
        raise HTTPException(status_code=400, detail="Nom de fichier invalide")
    path = os.path.join(BACKUP_DIR, filename)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Fichier introuvable")
    return FileResponse(path, media_type="application/octet-stream", filename=filename)


@router.post("/api/backups/{filename}/restore")
def restore_backup(filename: str, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    require_admin(user)
    if ".." in filename or "/" in filename:
        raise HTTPException(status_code=400, detail="Nom de fichier invalide")
    path = os.path.join(BACKUP_DIR, filename)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Fichier introuvable")
    _create_backup(db)
    shutil.copy2(path, DB_PATH)
    return {"ok": True, "restored": filename}


@router.delete("/api/backups/{filename}", status_code=204)
def delete_backup(filename: str, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    require_admin(user)
    if ".." in filename or "/" in filename:
        raise HTTPException(status_code=400, detail="Nom de fichier invalide")
    path = os.path.join(BACKUP_DIR, filename)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Fichier introuvable")
    os.remove(path)


@router.post("/api/backups/upload", status_code=201)
async def upload_backup(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    require_admin(user)
    form = await request.form()
    file: UploadFile = form.get("file")
    if not file or not file.filename.endswith(".db"):
        raise HTTPException(status_code=422, detail="Fichier .db requis")
    _ensure_dir()
    _create_backup(db)
    data = await file.read()
    if not data.startswith(b"SQLite format 3\x00"):
        raise HTTPException(status_code=422, detail="Fichier SQLite invalide")
    with open(DB_PATH, "wb") as f:
        f.write(data)
    return {"ok": True, "restored": file.filename}
