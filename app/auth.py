import logging
import os
import time
from collections import defaultdict
from datetime import datetime

from fastapi import Request, HTTPException, status
from fastapi.responses import Response
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from passlib.context import CryptContext
from sqlalchemy.orm import Session

from app.models import User

log = logging.getLogger(__name__)

COOKIE_NAME = "bibliodech_session"
COOKIE_MAX_AGE = 7 * 24 * 3600
_COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "false").lower() in ("1", "true", "yes")

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# SECRET_KEY : priorité à l'env var, sinon on charge depuis la DB (générée au démarrage).
# Le serializer est initialisé après init_db() via init_serializer().
_SECRET_KEY: str = ""
serializer: URLSafeTimedSerializer | None = None


def init_serializer(db_url: str) -> None:
    """Appelé après init_db() pour charger la SECRET_KEY persistée."""
    global _SECRET_KEY, serializer
    env_key = os.environ.get("SECRET_KEY", "")
    if env_key:
        _SECRET_KEY = env_key
        log.info("[auth] SECRET_KEY chargée depuis l'environnement.")
    else:
        # Lire depuis la table settings
        from sqlalchemy import create_engine, text
        import json
        engine = create_engine(db_url, connect_args={"check_same_thread": False})
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT value FROM settings WHERE key='secret_key'")
            ).scalar()
            if row:
                _SECRET_KEY = json.loads(row)
                log.info("[auth] SECRET_KEY chargée depuis la base de données.")
            else:
                import secrets
                _SECRET_KEY = secrets.token_hex(32)
                log.warning("[auth] SECRET_KEY introuvable en base — clé éphémère générée.")
    serializer = URLSafeTimedSerializer(_SECRET_KEY)

# ── Brute-force protection ────────────────────────────────────────────────────
_BRUTE_WINDOW = 60        # secondes
_BRUTE_MAX    = 10        # tentatives max dans la fenêtre
_BRUTE_BLOCK  = 60        # durée de blocage en secondes
_login_attempts: dict[str, list[float]] = defaultdict(list)
_login_blocked:  dict[str, float]       = {}


def _client_ip(request: Request) -> str:
    # On ne fait confiance à X-Forwarded-For que si la requête vient d'un proxy local (Caddy).
    # Sinon un attaquant peut le forger pour bypasser le brute-force.
    real_ip = request.client.host if request.client else "unknown"
    _trusted = {"127.0.0.1", "::1", "172.16.0.0/12"}
    try:
        import ipaddress
        is_trusted = any(
            ipaddress.ip_address(real_ip) in ipaddress.ip_network(n, strict=False)
            for n in _trusted
        )
    except ValueError:
        is_trusted = False
    if is_trusted:
        xff = request.headers.get("X-Forwarded-For")
        if xff:
            return xff.split(",")[0].strip()
    return real_ip


def check_brute_force(request: Request) -> None:
    ip = _client_ip(request)
    now = time.time()
    blocked_until = _login_blocked.get(ip, 0)
    if now < blocked_until:
        remaining = int(blocked_until - now)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Trop de tentatives. Réessayez dans {remaining}s.",
        )
    # Purger les tentatives hors fenêtre
    _login_attempts[ip] = [t for t in _login_attempts[ip] if now - t < _BRUTE_WINDOW]


def record_failed_login(request: Request) -> None:
    ip = _client_ip(request)
    now = time.time()
    _login_attempts[ip].append(now)
    if len(_login_attempts[ip]) >= _BRUTE_MAX:
        _login_blocked[ip] = now + _BRUTE_BLOCK
        _login_attempts[ip] = []


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
        secure=_COOKIE_SECURE,
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
    admin_password = os.environ.get("ADMIN_PASSWORD", "")
    using_default_password = not admin_password
    if using_default_password:
        import secrets
        admin_password = secrets.token_urlsafe(16)

    existing = db.query(User).filter(User.role == "admin").first()
    if existing:
        return

    admin = User(
        username=admin_username,
        password_hash=hash_password(admin_password),
        role="admin",
        is_active=True,
        must_change_password=False,
        created_at=datetime.utcnow(),
    )
    db.add(admin)
    db.commit()
    if using_default_password:
        log.warning(
            "[auth] Admin créé avec mot de passe temporaire aléatoire : %s  "
            "(à changer à la première connexion via ADMIN_PASSWORD ou l'interface)",
            admin_password,
        )
    else:
        log.info("Admin user '%s' created.", admin_username)
