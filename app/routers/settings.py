from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.auth import get_current_user, require_admin
from app.database import get_db
import app.settings as cfg

router = APIRouter()


@router.get("/api/settings")
def read_settings(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    require_admin(user)
    return cfg.get_all(db)


@router.put("/api/settings/{key}")
def write_setting(key: str, body, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    require_admin(user)
    if key not in cfg.DEFAULTS:
        raise HTTPException(status_code=400, detail="Clé inconnue")
    value = body if not isinstance(body, dict) else body.get("value")
    cfg.set_(db, key, value)
    from app.applog import log
    log(db, f"Paramètre modifié : {key}", category="settings", detail={"key": key, "by": user.username})
    return cfg.get_all(db)


@router.post("/api/settings/email/test")
def test_smtp(body: dict, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    require_admin(user)
    to = body.get("to", "").strip()
    if not to:
        raise HTTPException(status_code=400, detail="Adresse de test requise")
    from app.email import send_mail, _wrap
    subject = "Test SMTP – Bibliodech"
    html = _wrap("Test de configuration", "<p>Si vous recevez cet email, la configuration SMTP est correcte ✓</p>")
    try:
        send_mail(db, to, subject, html)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"ok": True}
