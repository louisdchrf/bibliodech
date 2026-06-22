"""
Scheduler de tâches automatiques (APScheduler).
La config est stockée en DB sous la clé "task_schedules" (JSON).
"""
import asyncio
import json
import logging
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

log = logging.getLogger(__name__)

_scheduler = AsyncIOScheduler(timezone="UTC")
_SCHEDULES_KEY = "task_schedules"

# ── Activité en cours ─────────────────────────────────────────────────────────
_running: dict[str, dict] = {}  # task_id → {label, started_at}


def get_running() -> list[dict]:
    return [{"id": tid, **info} for tid, info in _running.items()]


def get_next_runs() -> dict[str, str | None]:
    """Retourne le prochain lancement (ISO) pour chaque tâche planifiée."""
    result = {}
    for task_id in SCHEDULABLE_TASKS:
        job = _scheduler.get_job(f"task_{task_id}")
        nrt = getattr(job, "next_run_time", None) if job else None
        result[task_id] = nrt.isoformat() if nrt else None
    return result

# Tâches planifiables : id → (label, coroutine_factory)
# Chaque coroutine_factory reçoit (db) et exécute la logique directement
SCHEDULABLE_TASKS = {
    "reenrich":      "Compléter les livres manquants",
    "fetch-covers":  "Rechercher les couvertures manquantes",
    "clean-authors": "Normaliser les auteurs",
}

_DEFAULT_CONFIG = {
    task_id: {"enabled": False, "interval_minutes": 60, "last_run": None, "last_result": None}
    for task_id in SCHEDULABLE_TASKS
}


def get_schedules(db) -> dict:
    from app.models import Setting
    row = db.query(Setting).filter(Setting.key == _SCHEDULES_KEY).first()
    if not row:
        return dict(_DEFAULT_CONFIG)
    stored = json.loads(row.value)
    # Fusionner avec les défauts pour les nouvelles tâches
    result = dict(_DEFAULT_CONFIG)
    result.update(stored)
    return result


def save_schedules(db, schedules: dict):
    from app.models import Setting
    row = db.query(Setting).filter(Setting.key == _SCHEDULES_KEY).first()
    if row:
        row.value = json.dumps(schedules, ensure_ascii=False)
    else:
        db.add(Setting(key=_SCHEDULES_KEY, value=json.dumps(schedules, ensure_ascii=False)))
    db.commit()


async def _run_task(task_id: str):
    """Exécute une tâche planifiée et met à jour last_run / last_result."""
    from app.database import SessionLocal
    from app.applog import log_task, log_error
    db = SessionLocal()
    _running[task_id] = {
        "label": SCHEDULABLE_TASKS.get(task_id, task_id),
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    started = datetime.now(timezone.utc)
    try:
        log.info(f"[scheduler] Lancement de la tâche '{task_id}'")
        log_task(db, task_id, "started")
        result = await _execute_task(task_id, db)
        elapsed = round((datetime.now(timezone.utc) - started).total_seconds())
        schedules = get_schedules(db)
        if task_id in schedules:
            schedules[task_id]["last_run"] = datetime.now(timezone.utc).isoformat()
            schedules[task_id]["last_result"] = result
            save_schedules(db, schedules)
        log.info(f"[scheduler] Tâche '{task_id}' terminée : {result}")
        log_task(db, task_id, "done", {"result": result, "elapsed_s": elapsed})
    except Exception as e:
        log.error(f"[scheduler] Erreur tâche '{task_id}': {e}")
        log_error(db, f"Tâche '{task_id}' échouée : {e}", category="task", detail={"task_id": task_id})
    finally:
        _running.pop(task_id, None)
        db.close()


async def _execute_task(task_id: str, db) -> str:
    if task_id == "reenrich":
        from app.routers.books import _reenrich_missing
        result = await _reenrich_missing(db, force=False)
        return f"{result.get('queued', 0)} livres enrichis"

    if task_id == "fetch-covers":
        from app.routers.books import _fetch_covers_logic
        result = await _fetch_covers_logic(db)
        return f"{result['updated']} couvertures récupérées"

    if task_id == "clean-authors":
        from app.routers.books import _clean_authors_logic
        result = _clean_authors_logic(db)
        return f"{result['updated']} auteurs normalisés"

    return "tâche inconnue"


def _rebuild_jobs(db):
    """Recrée tous les jobs selon la config DB."""
    schedules = get_schedules(db)
    for task_id in SCHEDULABLE_TASKS:
        job_id = f"task_{task_id}"
        if _scheduler.get_job(job_id):
            _scheduler.remove_job(job_id)

        cfg = schedules.get(task_id, {})
        if cfg.get("enabled") and cfg.get("interval_minutes", 0) > 0:
            _scheduler.add_job(
                _run_task,
                trigger=IntervalTrigger(minutes=cfg["interval_minutes"]),
                id=job_id,
                args=[task_id],
                replace_existing=True,
                misfire_grace_time=120,
            )
            log.info(f"[scheduler] Job '{task_id}' planifié toutes les {cfg['interval_minutes']} min")


def start(db):
    _rebuild_jobs(db)
    _scheduler.start()
    log.info("[scheduler] Démarré")


def stop():
    if _scheduler.running:
        _scheduler.shutdown(wait=False)


def reload(db):
    """Appelé après modification de la config pour reconstruire les jobs."""
    _rebuild_jobs(db)
