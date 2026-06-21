"""
Scheduler de tâches automatiques (APScheduler).
La config est stockée en DB sous la clé "task_schedules" (JSON).
"""
import asyncio
import json
import logging
from datetime import datetime, timezone

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

log = logging.getLogger(__name__)

_scheduler = AsyncIOScheduler(timezone="UTC")
_SCHEDULES_KEY = "task_schedules"

# Tâches planifiables : id → (label, coroutine_factory)
# Chaque coroutine_factory reçoit (db) et exécute la logique directement
SCHEDULABLE_TASKS = {
    "reenrich":       "Compléter les livres manquants",
    "search-missing": "Chercher les tomes manquants",
    "detect-series":  "Détecter les séries",
    "analyze-series": "Analyser les séries (Open Library + Web)",
    "match-library":  "Trouver les tomes manquants dans la bibliothèque",
    "clean-authors":  "Normaliser les auteurs",
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
    db = SessionLocal()
    try:
        log.info(f"[scheduler] Lancement de la tâche '{task_id}'")
        result = await _execute_task(task_id, db)
        schedules = get_schedules(db)
        if task_id in schedules:
            schedules[task_id]["last_run"] = datetime.now(timezone.utc).isoformat()
            schedules[task_id]["last_result"] = result
            save_schedules(db, schedules)
        log.info(f"[scheduler] Tâche '{task_id}' terminée : {result}")
    except Exception as e:
        log.error(f"[scheduler] Erreur tâche '{task_id}': {e}")
    finally:
        db.close()


async def _execute_task(task_id: str, db) -> str:
    if task_id == "reenrich":
        from app.routers.books import _reenrich_missing
        result = await _reenrich_missing(db, force=False)
        return f"{result.get('queued', 0)} livres enrichis"

    if task_id == "search-missing":
        from app.routers.missing import search_all_missing_web_logic
        result = await search_all_missing_web_logic(db)
        return f"{result['series_checked']} séries · {result['missing_added']} manquants"

    if task_id == "detect-series":
        from app.routers.series import _detect_all_series
        result = await _detect_all_series(db)
        return f"{result.get('found_instant', 0)} séries détectées"

    if task_id == "analyze-series":
        # Appel HTTP interne pour réutiliser la logique complexe de l'endpoint
        try:
            async with httpx.AsyncClient(base_url="http://localhost:8000", timeout=300) as client:
                r = await client.post("/api/series/analyze", cookies={"internal_scheduler": "1"})
                data = r.json()
                return f"{len(data)} proposition{'s' if len(data) != 1 else ''}"
        except Exception as e:
            return f"erreur: {e}"

    if task_id == "match-library":
        from app.routers.missing import _find_library_matches, auto_assign_matches as _aa
        from collections import defaultdict
        matches = _find_library_matches(db)
        by_slot: dict = defaultdict(list)
        for m in matches:
            by_slot[(m["series_id"], m["suggested_position"])].append(m)
        assigned = 0
        for (series_id, pos), candidates in by_slot.items():
            if len(candidates) == 1:
                from app.models import Book
                book = db.query(Book).filter(Book.id == candidates[0]["book_id"]).first()
                if book:
                    book.series_id = series_id
                    book.series_position = pos
                    assigned += 1
        db.commit()
        return f"{assigned} livre{'s' if assigned != 1 else ''} assigné{'s' if assigned != 1 else ''}"

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
