import os
from datetime import datetime

from fastapi import Request, HTTPException, status
from fastapi.responses import Response
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from passlib.context import CryptContext
from sqlalchemy.orm import Session

from app.models import User

SECRET_KEY = os.environ.get("SECRET_KEY", "change-me-in-production-please")
COOKIE_NAME = "bibliodech_session"
COOKIE_MAX_AGE = 7 * 24 * 3600

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
serializer = URLSafeTimedSerializer(SECRET_KEY)


# ── Password helpers ─────────────────────────────────────────────────────────

def hash_password(pw: str) -> str:
    return pwd_context.hash(pw)


def verify_password(pw: str, hashed: str) -> bool:
    return pwd_context.verify(pw, hashed)


# ── Session cookie ───────────────────────────────────────────────────────────

def create_session(response: Response, user_id: int) -> None:
    token = serializer.dumps(user_id)
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
    )


def clear_session(response: Response) -> None:
    response.delete_cookie(key=COOKIE_NAME)


# ── Dependencies ─────────────────────────────────────────────────────────────

def get_current_user(request: Request, db: Session) -> User:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Non authentifié")
    try:
        user_id = serializer.loads(token, max_age=COOKIE_MAX_AGE)
    except (BadSignature, SignatureExpired):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session invalide")
    user = db.query(User).filter(User.id == user_id, User.is_active == True).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Utilisateur introuvable")
    return user


def require_admin(user: User) -> User:
    if user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Accès réservé aux administrateurs")
    return user


def require_contributor(user: User) -> User:
    if user.role not in ("admin", "contributeur"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Accès insuffisant")
    return user


# ── Bootstrap admin ──────────────────────────────────────────────────────────

def bootstrap_admin(db: Session) -> None:
    admin_username = os.environ.get("ADMIN_USERNAME", "admin")
    admin_password = os.environ.get("ADMIN_PASSWORD", "admin123")

    existing = db.query(User).filter(User.role == "admin").first()
    if existing:
        return

    admin = User(
        username=admin_username,
        password_hash=hash_password(admin_password),
        role="admin",
        is_active=True,
        created_at=datetime.utcnow(),
    )
    db.add(admin)
    db.commit()
    print(f"[bibliodech] Admin user '{admin_username}' created.", flush=True)
