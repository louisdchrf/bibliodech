"""
Tomes manquants dans les séries.
"""
import asyncio
import json
import re
from datetime import datetime

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.auth import get_current_user, require_contributor
from app.database import get_db
from app.models import Book, Series, SeriesMissingVolume
from app.series_search import _ddg_query, _clean_html

router = APIRouter()


# ── Patterns pour extraire des numéros de tomes depuis du texte web ─────────
_VOLUME_PATTERNS = [
    r'\btome\s+(\d+(?:\.\d+)?)',
    r'\bvol(?:ume)?\.?\s*(\d+(?:\.\d+)?)',
    r'\bt\.?\s*(\d+(?:\.\d+)?)\b',
    r'#\s*(\d+(?:\.\d+)?)',
]


def _extract_volume_numbers(text: str) -> set[float]:
    nums: set[float] = set()
    for pat in _VOLUME_PATTERNS:
        for m in re.finditer(pat, text, re.IGNORECASE):
            try:
                nums.add(float(m.group(1)))
            except ValueError:
                pass
    return nums


def _series_missing_data(series: Series, db: Session) -> dict:
    """Calcule les données de tomes manquants pour une série."""
    books = (
        db.query(Book)
        .filter(Book.series_id == series.id)
        .order_by(Book.series_position.nullslast(), Book.title)
        .all()
    )

    owned_positions = sorted({b.series_position for b in books if b.series_position is not None})

    # Gaps dans la séquence connue
    gaps: list[float] = []
    if len(owned_positions) >= 2:
        for i in range(int(owned_positions[0]), int(owned_positions[-1])):
            pos = float(i)
            if pos not in owned_positions:
                gaps.append(pos)

    # Candidats : tous les livres dont le titre contient le nom de la série
    # (même s'ils sont assignés à une autre série)
    series_words = series.name.lower().split()
    candidates = []
    if len(series_words) >= 2:
        all_books = db.query(Book).filter(Book.id.notin_([b.id for b in books])).all()
        for b in all_books:
            title_lower = (b.title or "").lower()
            if all(w in title_lower for w in series_words[:2]):
                candidates.append({
                    "id": b.id,
                    "title": b.title,
                    "series_position": b.series_position,
                    "current_series_id": b.series_id,
                    "current_series_name": b.series.name if b.series else None,
                    "cover_url": b.cover_url,
                    "authors": json.loads(b.authors) if b.authors else [],
                })

    # Tomes connus manquants (stockés en DB via recherche web)
    stored_missing = (
        db.query(SeriesMissingVolume)
        .filter(SeriesMissingVolume.series_id == series.id)
        .order_by(SeriesMissingVolume.position.nullslast())
        .all()
    )

    # Filtrer les manquants stockés qui ont été trouvés depuis
    missing_to_buy = [
        {
            "id": m.id,
            "position": m.position,
            "title": m.title,
            "detected_at": m.detected_at.isoformat() if m.detected_at else None,
        }
        for m in stored_missing
        if not any(b.series_position == m.position for b in books)
    ]

    return {
        "series": {
            "id": series.id,
            "name": series.name,
            "book_count": len(books),
        },
        "owned": [
            {
                "id": b.id,
                "title": b.title,
                "position": b.series_position,
                "cover_url": b.cover_url,
            }
            for b in books
        ],
        "gaps": gaps,
        "candidates": candidates,
        "missing_to_buy": missing_to_buy,
        "has_issues": bool(gaps or candidates or missing_to_buy),
    }


@router.get("/api/missing")
def get_all_missing(request: Request, db: Session = Depends(get_db)):
    """Retourne toutes les séries avec des tomes manquants ou des candidats."""
    get_current_user(request, db)
    series_list = db.query(Series).order_by(Series.name).all()
    result = []
    for s in series_list:
        data = _series_missing_data(s, db)
        if data["has_issues"]:
            result.append(data)
    return result


@router.get("/api/missing/{series_id}")
def get_series_missing(series_id: int, request: Request, db: Session = Depends(get_db)):
    get_current_user(request, db)
    series = db.query(Series).filter(Series.id == series_id).first()
    if not series:
        raise HTTPException(status_code=404)
    return _series_missing_data(series, db)


@router.post("/api/missing/{series_id}/search-web")
async def search_missing_web(series_id: int, request: Request, db: Session = Depends(get_db)):
    """Cherche sur DDG le nombre total de tomes de la série et stocke les manquants."""
    user = get_current_user(request, db)
    require_contributor(user)
    series = db.query(Series).filter(Series.id == series_id).first()
    if not series:
        raise HTTPException(status_code=404)

    async with httpx.AsyncClient(timeout=15) as client:
        text1 = await _ddg_query(f'"{series.name}" liste tomes bd livre série', client)
        await asyncio.sleep(1.0)
        text2 = await _ddg_query(f'{series.name} intégrale nombre tomes', client)

    full_text = text1 + " " + text2
    found_positions = _extract_volume_numbers(full_text)

    owned = db.query(Book).filter(Book.series_id == series_id).all()
    owned_positions = {b.series_position for b in owned if b.series_position is not None}

    # Stocker uniquement les positions trouvées en ligne qui ne sont pas possédées
    db.query(SeriesMissingVolume).filter(SeriesMissingVolume.series_id == series_id).delete()
    added = []
    for pos in sorted(found_positions):
        if pos not in owned_positions and 1 <= pos <= 500:
            mv = SeriesMissingVolume(
                series_id=series_id,
                position=pos,
                detected_at=datetime.utcnow(),
            )
            db.add(mv)
            added.append(pos)
    db.commit()

    return {
        "found_positions": sorted(found_positions),
        "owned_positions": sorted(owned_positions),
        "missing_added": added,
    }


@router.post("/api/missing/{missing_id}/resolve")
def resolve_missing(missing_id: int, request: Request, db: Session = Depends(get_db)):
    """Marque un tome manquant comme résolu (acheté/trouvé)."""
    user = get_current_user(request, db)
    require_contributor(user)
    mv = db.query(SeriesMissingVolume).filter(SeriesMissingVolume.id == missing_id).first()
    if not mv:
        raise HTTPException(status_code=404)
    db.delete(mv)
    db.commit()
    return {"ok": True}


@router.post("/api/books/{book_id}/assign-series")
def assign_series(book_id: int, body: dict, request: Request, db: Session = Depends(get_db)):
    """Assigne un livre à une série (et optionnellement une position)."""
    user = get_current_user(request, db)
    require_contributor(user)
    book = db.query(Book).filter(Book.id == book_id).first()
    if not book:
        raise HTTPException(status_code=404)
    book.series_id = body.get("series_id")
    if "position" in body:
        book.series_position = body["position"]
    db.commit()
    return {"ok": True}
