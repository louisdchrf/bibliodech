import json
from sqlalchemy.orm import Session
from app.models import AuditLog


def log(db: Session, book_id: int, action: str, user_id: int | None = None, detail: dict | None = None):
    entry = AuditLog(
        book_id=book_id,
        user_id=user_id,
        action=action,
        detail=json.dumps(detail, ensure_ascii=False) if detail else None,
    )
    db.add(entry)
    # Pas de commit ici — l'appelant commite avec le reste
