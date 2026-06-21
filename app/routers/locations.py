from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import func

from app.auth import get_current_user, require_admin, require_contributor
from app.database import get_db
from app.models import Site, Room, Shelf, Book

router = APIRouter()


# ── Helpers ──────────────────────────────────────────────────────────────────

def _shelf_book_counts(db: Session) -> dict:
    return {
        r[0]: r[1]
        for r in db.query(Book.location_id, func.count(Book.id))
            .filter(Book.location_id.isnot(None))
            .group_by(Book.location_id)
            .all()
    }


def _shelf_dict(shelf: Shelf, book_count: int = 0) -> dict:
    return {
        "id": shelf.id,
        "name": shelf.name,
        "room_id": shelf.room_id,
        "book_count": book_count,
    }


def _room_dict(room: Room) -> dict:
    return {
        "id": room.id,
        "name": room.name,
        "site_id": room.site_id,
    }


def _site_dict(site: Site) -> dict:
    return {
        "id": site.id,
        "name": site.name,
    }


# ── Arbre complet ─────────────────────────────────────────────────────────────

@router.get("/api/locations/tree")
def location_tree(request: Request, db: Session = Depends(get_db)):
    get_current_user(request, db)
    counts = _shelf_book_counts(db)

    # Sites avec leurs pièces et étagères
    sites = (
        db.query(Site)
        .options(joinedload(Site.rooms).joinedload(Room.shelves))
        .order_by(Site.name)
        .all()
    )
    result = []
    for site in sites:
        rooms_out = []
        for room in sorted(site.rooms, key=lambda r: r.name):
            shelves_out = [
                {**_shelf_dict(sh, counts.get(sh.id, 0))}
                for sh in sorted(room.shelves, key=lambda s: s.name)
            ]
            rooms_out.append({**_room_dict(room), "shelves": shelves_out})
        result.append({**_site_dict(site), "rooms": rooms_out})

    # Pièces sans site (site_id IS NULL)
    orphan_rooms = (
        db.query(Room)
        .options(joinedload(Room.shelves))
        .filter(Room.site_id.is_(None))
        .order_by(Room.name)
        .all()
    )
    if orphan_rooms:
        rooms_out = []
        for room in orphan_rooms:
            shelves_out = [
                {**_shelf_dict(sh, counts.get(sh.id, 0))}
                for sh in sorted(room.shelves, key=lambda s: s.name)
            ]
            rooms_out.append({**_room_dict(room), "shelves": shelves_out})
        result.append({"id": None, "name": "(sans adresse)", "rooms": rooms_out})

    # Étagères sans pièce (room_id IS NULL)
    orphan_shelves = (
        db.query(Shelf)
        .filter(Shelf.room_id.is_(None))
        .order_by(Shelf.name)
        .all()
    )
    if orphan_shelves:
        shelves_out = [
            {**_shelf_dict(sh, counts.get(sh.id, 0))}
            for sh in orphan_shelves
        ]
        result.append({
            "id": None,
            "name": "(sans pièce)",
            "rooms": [{"id": None, "name": "", "shelves": shelves_out}],
        })

    return result


# ── Liste plate des étagères (rétrocompat bibliothèque) ──────────────────────

@router.get("/api/locations")
def list_locations(request: Request, db: Session = Depends(get_db)):
    get_current_user(request, db)
    counts = _shelf_book_counts(db)
    shelves = (
        db.query(Shelf)
        .options(joinedload(Shelf.room).joinedload(Room.site))
        .all()
    )
    return [
        {
            "id": sh.id,
            "name": sh.name,
            "label": sh.label,
            "room_id": sh.room_id,
            "room_name": sh.room.name if sh.room else None,
            "site_name": sh.room.site.name if sh.room and sh.room.site else None,
            "book_count": counts.get(sh.id, 0),
        }
        for sh in shelves
    ]


# ── Sites ─────────────────────────────────────────────────────────────────────

@router.get("/api/sites")
def list_sites(request: Request, db: Session = Depends(get_db)):
    get_current_user(request, db)
    return [_site_dict(s) for s in db.query(Site).order_by(Site.name).all()]


@router.post("/api/sites", status_code=status.HTTP_201_CREATED)
def create_site(body: dict, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    require_contributor(user)
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Le nom est requis")
    site = Site(name=name)
    db.add(site)
    db.commit()
    db.refresh(site)
    return _site_dict(site)


@router.put("/api/sites/{site_id}")
def update_site(site_id: int, body: dict, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    require_contributor(user)
    site = db.query(Site).filter(Site.id == site_id).first()
    if not site:
        raise HTTPException(status_code=404, detail="Localisation introuvable")
    site.name = (body.get("name") or "").strip() or site.name
    db.commit()
    return _site_dict(site)


@router.delete("/api/sites/{site_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_site(site_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    require_admin(user)
    site = db.query(Site).filter(Site.id == site_id).first()
    if not site:
        raise HTTPException(status_code=404, detail="Localisation introuvable")
    # Les books dans les shelves de ce site → délier
    for room in site.rooms:
        for shelf in room.shelves:
            db.query(Book).filter(Book.location_id == shelf.id).update({"location_id": None})
    db.delete(site)
    db.commit()


# ── Pièces ────────────────────────────────────────────────────────────────────

@router.get("/api/rooms")
def list_rooms(request: Request, db: Session = Depends(get_db)):
    get_current_user(request, db)
    return [_room_dict(r) for r in db.query(Room).order_by(Room.name).all()]


@router.post("/api/rooms", status_code=status.HTTP_201_CREATED)
def create_room(body: dict, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    require_contributor(user)
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Le nom est requis")
    site_id = body.get("site_id") or None
    if site_id and not db.query(Site).filter(Site.id == site_id).first():
        raise HTTPException(status_code=400, detail="Localisation inconnue")
    room = Room(name=name, site_id=site_id)
    db.add(room)
    db.commit()
    db.refresh(room)
    return _room_dict(room)


@router.put("/api/rooms/{room_id}")
def update_room(room_id: int, body: dict, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    require_contributor(user)
    room = db.query(Room).filter(Room.id == room_id).first()
    if not room:
        raise HTTPException(status_code=404, detail="Pièce introuvable")
    room.name = (body.get("name") or "").strip() or room.name
    if "site_id" in body:
        room.site_id = body["site_id"] or None
    db.commit()
    return _room_dict(room)


@router.delete("/api/rooms/{room_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_room(room_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    require_admin(user)
    room = db.query(Room).filter(Room.id == room_id).first()
    if not room:
        raise HTTPException(status_code=404, detail="Pièce introuvable")
    for shelf in room.shelves:
        db.query(Book).filter(Book.location_id == shelf.id).update({"location_id": None})
    db.delete(room)
    db.commit()


# ── Étagères ──────────────────────────────────────────────────────────────────

@router.get("/api/shelves")
def list_shelves(request: Request, room_id: int = None, db: Session = Depends(get_db)):
    get_current_user(request, db)
    q = db.query(Shelf)
    if room_id:
        q = q.filter(Shelf.room_id == room_id)
    counts = _shelf_book_counts(db)
    return [_shelf_dict(sh, counts.get(sh.id, 0)) for sh in q.order_by(Shelf.name).all()]


@router.post("/api/shelves", status_code=status.HTTP_201_CREATED)
def create_shelf(body: dict, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    require_contributor(user)
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Le nom est requis")
    room_id = body.get("room_id") or None
    if room_id and not db.query(Room).filter(Room.id == room_id).first():
        raise HTTPException(status_code=400, detail="Pièce inconnue")
    shelf = Shelf(name=name, room_id=room_id)
    db.add(shelf)
    db.commit()
    db.refresh(shelf)
    return _shelf_dict(shelf)


@router.put("/api/shelves/{shelf_id}")
def update_shelf(shelf_id: int, body: dict, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    require_contributor(user)
    shelf = db.query(Shelf).filter(Shelf.id == shelf_id).first()
    if not shelf:
        raise HTTPException(status_code=404, detail="Étagère introuvable")
    shelf.name = (body.get("name") or "").strip() or shelf.name
    if "room_id" in body:
        shelf.room_id = body["room_id"] or None
    db.commit()
    return _shelf_dict(shelf)


@router.delete("/api/shelves/{shelf_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_shelf(shelf_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    require_admin(user)
    shelf = db.query(Shelf).filter(Shelf.id == shelf_id).first()
    if not shelf:
        raise HTTPException(status_code=404, detail="Étagère introuvable")
    db.query(Book).filter(Book.location_id == shelf_id).update({"location_id": None})
    db.delete(shelf)
    db.commit()
