import random
import string

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.auth import get_current_user, hash_password, require_admin
from app.database import get_db
from app.models import User
from app.schemas import UserCreate, UserOut, UserUpdate
import app.settings as cfg


def _try_send(db, to: str, subject: str, html: str):
    if not to:
        return
    try:
        from app.email import send_mail
        send_mail(db, to, subject, html)
    except Exception:
        pass  # ne jamais bloquer une action admin pour un mail raté


def _random_password(length: int = 12) -> str:
    chars = string.ascii_letters + string.digits
    return ''.join(random.SystemRandom().choice(chars) for _ in range(length))

router = APIRouter()


@router.get("/api/users/active")
def list_active_users(request: Request, db: Session = Depends(get_db)):
    """Liste légère id+username pour les selects (accessible à tous les authentifiés)."""
    get_current_user(request, db)
    users = db.query(User).filter(User.is_active == True).order_by(User.username).all()  # noqa: E712
    return [{"id": u.id, "username": u.username} for u in users]


@router.get("/api/users", response_model=list[UserOut])
def list_users(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    require_admin(user)
    return db.query(User).order_by(User.username).all()


@router.post("/api/users", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def create_user(body: UserCreate, request: Request, db: Session = Depends(get_db)):
    current = get_current_user(request, db)
    require_admin(current)

    if db.query(User).filter(User.username == body.username).first():
        raise HTTPException(status_code=409, detail="Nom d'utilisateur déjà pris")
    if body.role not in ("admin", "contributeur", "viewer"):
        raise HTTPException(status_code=422, detail="Rôle invalide")

    new_user = User(
        username=body.username,
        password_hash=hash_password(body.password),
        role=body.role,
        is_active=True,
        must_change_password=body.must_change_password,
        email=body.email,
    )
    db.add(new_user)
    db.commit()
    db.refresh(new_user)

    if body.email and cfg.get(db, "mail_new_account"):
        from app.email import mail_new_account
        site_url = cfg.get(db, "site_url") or ""
        subject, html = mail_new_account(body.username, body.password, site_url)
        _try_send(db, body.email, subject, html)

    return new_user


@router.put("/api/users/{user_id}", response_model=UserOut)
def update_user(
    user_id: int,
    body: UserUpdate,
    request: Request,
    db: Session = Depends(get_db),
):
    current = get_current_user(request, db)
    require_admin(current)

    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="Utilisateur introuvable")

    if body.username is not None:
        existing = db.query(User).filter(User.username == body.username, User.id != user_id).first()
        if existing:
            raise HTTPException(status_code=409, detail="Nom d'utilisateur déjà pris")
        target.username = body.username
    if body.password is not None:
        target.password_hash = hash_password(body.password)
    if body.role is not None:
        if body.role not in ("admin", "contributeur", "viewer"):
            raise HTTPException(status_code=422, detail="Rôle invalide")
        target.role = body.role
    if body.is_active is not None:
        target.is_active = body.is_active
    if body.must_change_password is not None:
        target.must_change_password = body.must_change_password

    db.commit()
    db.refresh(target)
    return target


@router.post("/api/users/{user_id}/reset-password")
def reset_user_password(user_id: int, request: Request, db: Session = Depends(get_db)):
    """Admin génère un mot de passe temporaire — l'utilisateur doit le changer à la connexion."""
    current = get_current_user(request, db)
    require_admin(current)
    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="Utilisateur introuvable")
    temp_pw = _random_password()
    target.password_hash = hash_password(temp_pw)
    target.must_change_password = True
    db.commit()

    if target.email and cfg.get(db, "mail_reset_password"):
        from app.email import mail_reset_password
        site_url = cfg.get(db, "site_url") or ""
        subject, html = mail_reset_password(target.username, temp_pw, site_url)
        _try_send(db, target.email, subject, html)

    return {"temp_password": temp_pw}


@router.post("/api/me/change-password")
def change_own_password(body: dict, request: Request, db: Session = Depends(get_db)):
    """L'utilisateur connecté change son propre mot de passe (vérifie l'ancien)."""
    from app.auth import verify_password
    from app.email import send_mail, mail_password_changed, get_smtp_config
    import app.settings as cfg

    user = get_current_user(request, db)

    old_pw = body.get("old_password", "")
    new_pw = body.get("new_password", "").strip()
    confirm_pw = body.get("confirm_password", "").strip()

    if not verify_password(old_pw, user.password_hash):
        raise HTTPException(status_code=403, detail="Mot de passe actuel incorrect")
    if len(new_pw) < 6:
        raise HTTPException(status_code=422, detail="Le nouveau mot de passe doit faire au moins 6 caractères")
    if new_pw != confirm_pw:
        raise HTTPException(status_code=422, detail="Les deux mots de passe ne correspondent pas")

    user.password_hash = hash_password(new_pw)
    user.must_change_password = False
    db.commit()

    # Notification par mail si l'utilisateur a une adresse et que le SMTP est configuré
    if user.email and get_smtp_config(db).get("host"):
        try:
            site_url = cfg.get(db, "site_url") or ""
            subject, html = mail_password_changed(user.username, site_url)
            send_mail(db, user.email, subject, html)
        except Exception:
            pass

    return {"ok": True}


@router.put("/api/me")
def update_own_profile(body: dict, request: Request, db: Session = Depends(get_db)):
    """L'utilisateur connecté met à jour son propre profil (username, email)."""
    user = get_current_user(request, db)
    if "username" in body:
        username = body["username"].strip()
        if not username:
            raise HTTPException(status_code=422, detail="Le nom d'utilisateur ne peut pas être vide")
        existing = db.query(User).filter(User.username == username, User.id != user.id).first()
        if existing:
            raise HTTPException(status_code=409, detail="Ce nom d'utilisateur est déjà pris")
        user.username = username
    if "email" in body:
        user.email = body["email"].strip() or None
    db.commit()
    return {"ok": True, "username": user.username}


@router.delete("/api/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_user(user_id: int, request: Request, db: Session = Depends(get_db)):
    current = get_current_user(request, db)
    require_admin(current)

    if current.id == user_id:
        raise HTTPException(status_code=400, detail="Vous ne pouvez pas supprimer votre propre compte")

    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="Utilisateur introuvable")
    db.delete(target)
    db.commit()
