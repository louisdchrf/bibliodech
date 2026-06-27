import json
import logging
from datetime import date, datetime
from app.models import Book

log = logging.getLogger(__name__)


def _safe_json(raw: str | None, default, book_id: int, field: str):
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        log.warning("book_to_dict: JSON invalide book_id=%s field=%s: %s", book_id, field, e)
        return default


def utc_iso(dt: datetime | None) -> str | None:
    """Sérialise un datetime UTC naïf en ISO 8601 avec suffixe Z."""
    if dt is None:
        return None
    s = dt.isoformat()
    if not s.endswith('Z') and '+' not in s:
        s += 'Z'
    return s


def book_to_dict(book: Book) -> dict:
    # Localisation effective : room_id direct prioritaire, sinon via shelf legacy
    room = book.room
    if room is None and book.location and book.location.room:
        room = book.location.room

    site = room.site if room else None

    shelf = book.location  # Shelf object (location_id → shelves)
    loc = {
        "room_id": room.id,
        "room_name": room.name,
        "site_id": site.id if site else None,
        "site_name": site.name if site else None,
        "shelf_id": shelf.id if shelf else None,
        "shelf_name": shelf.name if shelf else None,
        "label": f"{site.name} › {room.name}" if site else room.name,
    } if room else None

    # Prêt actif (pas de return_date)
    try:
        active_loan = next((l for l in book.loans if l.return_date is None), None)
    except Exception as e:
        log.warning("book_to_dict: erreur loans book_id=%s: %s", book.id, e)
        active_loan = None

    if active_loan:
        if active_loan.user:
            borrower_name = active_loan.user.username
        elif active_loan.borrower:
            borrower_name = active_loan.borrower.name
        else:
            borrower_name = None
        loan_info = {
            "id": active_loan.id,
            "borrower_name": borrower_name,
            "borrower_is_user": active_loan.user_id is not None,
            "loan_date": utc_iso(active_loan.loan_date),
            "due_date": utc_iso(active_loan.due_date),
            "overdue": (
                active_loan.due_date is not None
                and active_loan.due_date.date() < date.today()
            ),
        }
    else:
        loan_info = None

    return {
        "id": book.id,
        "isbn": book.isbn,
        "title": book.title,
        "subtitle": book.subtitle,
        "authors": _safe_json(book.authors, [], book.id, "authors"),
        "publisher": book.publisher,
        "publish_date": book.publish_date,
        "cover_url": book.cover_url,
        "description": book.description,
        "page_count": book.page_count,
        "language": book.language,
        "source": book.source,
        "work_key": book.work_key,
        "room_id": room.id if room else None,
        "location": loc,
        "added_at": utc_iso(book.added_at),
        "enrichment_status": book.enrichment_status,
        "active_loan": loan_info,
        "source_data": _safe_json(book.source_data, None, book.id, "source_data"),
        "series_id": book.series_id,
        "series_name": book.series.name if book.series else None,
        "series_position": book.series_position,
        "genre": book.genre,
    }
