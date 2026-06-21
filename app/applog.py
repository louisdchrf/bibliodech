import json
from sqlalchemy.orm import Session
from app.models import AppLog


def log(db: Session, message: str, level: str = "info", category: str = "system", detail: dict | None = None):
    entry = AppLog(
        level=level,
        category=category,
        message=message,
        detail=json.dumps(detail, ensure_ascii=False) if detail else None,
    )
    db.add(entry)
    db.commit()


def log_task(db: Session, task_id: str, status: str, detail: dict | None = None):
    """Log un événement de tâche (started / done / error)."""
    level = "error" if status == "error" else "info"
    log(db, f"[{task_id}] {status}", level=level, category="task", detail=detail)


def log_error(db: Session, message: str, category: str = "system", detail: dict | None = None):
    log(db, message, level="error", category=category, detail=detail)
