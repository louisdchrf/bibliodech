import asyncio
import json
import re
from itertools import combinations
from typing import Optional

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.auth import get_current_user, require_admin, require_contributor
from app.database import get_db, SessionLocal
from app.models import Book, Series
from app.schemas import LinkBooksRequest
from app.series_logic import get_or_create_series
from app.lookup import _extract_series_and_position


def _lcp_words(t1: str, t2: str) -> list[str]:
    """Longest common prefix in words (case-insensitive, original case kept)."""
    w1 = t1.split()
    w2 = t2.split()
    result = []
    for a, b in zip(w1, w2):
        if a.lower().rstrip(",:;") == b.lower().rstrip(",:;"):
            result.append(a)
        else:
            break
    return result


def _cluster_books_by_prefix(books: list[dict], min_words: int = 2) -> list[dict]:
    """Group books by shared title prefix (≥ min_words words in common)."""
    n = len(books)
    parent = list(range(n))
    prefix_map: dict[int, list[str]] = {}

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i, j in combinations(range(n), 2):
        lcp = _lcp_words(books[i]["title"], books[j]["title"])
        if len(lcp) >= min_words:
            ri, rj = find(i), find(j)
            if ri != rj:
                parent[rj] = ri
            r = find(i)
            existing = prefix_map.get(r, lcp)
            shared = _lcp_words(" ".join(existing), " ".join(lcp))
            prefix_map[r] = shared if shared else existing

    groups: dict[int, list[int]] = {}
    for i in range(n):
        r = find(i)
        if r in prefix_map:
            groups.setdefault(r, []).append(i)

    result = []
    for root, indices in groups.items():
        if len(indices) >= 2:
            series_name = " ".join(prefix_map[root]).strip().rstrip(",:;- ")
            result.append({
                "series_name": series_name,
                "source": "title_cluster",
                "books": [books[i] for i in indices],
            })
    return result


router = APIRouter()


def _series_to_dict(series: Series, book_count: int = 0) -> dict:
    return {
        "id": series.id,
        "name": series.name,
        "source": series.source,
        "created_at": series.created_at.isoformat() + "Z" if series.created_at else None,
        "book_count": book_count,
    }


def _book_mini(book: Book) -> dict:
    return {
        "id": book.id,
        "title": book.title,
        "authors": json.loads(book.authors) if book.authors else [],
        "cover_url": book.cover_url,
        "series_position": book.series_position,
        "shelf": book.shelf,
    }


@router.get("/api/series")
def list_series(request: Request, db: Session = Depends(get_db)):
    get_current_user(request, db)
    series_list = db.query(Series).order_by(Series.name).all()
    result = []
    for s in series_list:
        count = db.query(Book).filter(Book.series_id == s.id).count()
        result.append(_series_to_dict(s, book_count=count))
    return result


@router.post("/api/series", status_code=201)
def create_series(body: dict, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    require_contributor(user)
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Le nom est requis")
    existing = db.query(Series).filter(Series.name == name).first()
    if existing:
        raise HTTPException(status_code=409, detail="Cette série existe déjà")
    s = Series(name=name, source=body.get("source", "manual"))
    db.add(s)
    db.commit()
    db.refresh(s)
    return _series_to_dict(s)


async def _detect_all_series(db: Session) -> dict:
    """Logique de détection de séries par heuristiques titre, appelable depuis l'endpoint et le scheduler."""
    books_no_series = (
        db.query(Book)
        .filter(Book.series_id.is_(None), Book.enrichment_status == "ok")
        .all()
    )
    found_instant = 0
    queued_lookup = []
    for book in books_no_series:
        title = book.title or ""
        subtitle = book.subtitle
        series_name, series_position = _extract_series_and_position(title, subtitle)
        if series_name:
            series = get_or_create_series(db, series_name, source="manual")
            book.series_id = series.id
            if series_position is not None and book.series_position is None:
                book.series_position = series_position
            found_instant += 1
        else:
            queued_lookup.append(book.id)
    db.commit()

    # Passe 2 : re-lookup API pour les livres restants sans correspondance titre
    if queued_lookup:
        from app.routers.scan import _enrich_book
        import asyncio
        pairs = []
        for book_id in queued_lookup:
            book = db.query(Book).filter(Book.id == book_id).first()
            if book and book.isbn:
                book.enrichment_status = "pending"
                pairs.append((book_id, book.isbn))
        db.commit()
        sem = asyncio.Semaphore(3)
        async def _bounded(bid, isbn):
            async with sem:
                await _enrich_book(bid, isbn)
        if pairs:
            await asyncio.gather(*[_bounded(bid, isbn) for bid, isbn in pairs])

    return {"found_instant": found_instant, "queued_lookup": len(queued_lookup)}


@router.post("/api/series/detect-all")
async def detect_series_all(
    request: Request,
    db: Session = Depends(get_db),
):
    """Lance la détection de série sur tous les livres sans série (heuristiques titre + re-lookup API)."""
    user = get_current_user(request, db)
    require_contributor(user)
    return await _detect_all_series(db)


@router.get("/api/series/suggestions")
def series_suggestions(request: Request, db: Session = Depends(get_db)):
    get_current_user(request, db)
    # Group books without a series by author
    books_no_series = db.query(Book).filter(Book.series_id == None).all()  # noqa: E711

    author_groups: dict[str, list[dict]] = {}
    for book in books_no_series:
        try:
            authors = json.loads(book.authors) if book.authors else []
        except Exception:
            authors = []
        for author in authors:
            if author not in author_groups:
                author_groups[author] = []
            author_groups[author].append(_book_mini(book))

    # Only return groups with 2+ books
    result = []
    seen_book_ids: set[int] = set()
    for author, books in author_groups.items():
        unique_books = [b for b in books if b["id"] not in seen_book_ids]
        if len(unique_books) >= 2:
            for b in unique_books:
                seen_book_ids.add(b["id"])
            result.append({"author": author, "books": unique_books})

    return result


async def _analyze_series_logic(db: Session) -> list:
    """Logique d'analyse de séries, utilisable depuis l'endpoint et le scheduler."""
    from app.series_search import search_series_ddg

    books_no_series = (
        db.query(Book)
        .filter(Book.series_id.is_(None), Book.enrichment_status == "ok")
        .all()
    )

    proposals: dict[str, dict] = {}  # series_name.lower() → {source, books}
    claimed_ids: set[int] = set()

    # Index auteur → {series_names} pour la cross-référence
    # Inclut tous les auteurs connus (stockés + source_data)
    author_to_series: dict[str, set[str]] = {}

    def _all_authors(book: Book) -> set[str]:
        """Collecte tous les auteurs d'un livre depuis toutes les sources."""
        auths: set[str] = set()
        try:
            for a in (json.loads(book.authors) if book.authors else []):
                auths.add(a.strip().lower())
        except Exception:
            pass
        try:
            for src_data in (json.loads(book.source_data) if book.source_data else {}).values():
                for a in (src_data.get("authors") or []):
                    if a:
                        auths.add(a.strip().lower().rstrip(','))
        except Exception:
            pass
        return auths

    def _book_dict(book: Book, work_title=None, position=None) -> dict:
        return {
            "id": book.id,
            "title": book.title,
            "work_title": work_title,
            "position": position,
            "authors": json.loads(book.authors) if book.authors else [],
            "cover_url": book.cover_url,
        }

    def _add_proposal(series_name: str, source: str, bdict: dict):
        key = series_name.lower()
        if key not in proposals:
            proposals[key] = {"series_name": series_name, "source": source, "books": []}
        if not any(b["id"] == bdict["id"] for b in proposals[key]["books"]):
            proposals[key]["books"].append(bdict)

    def _register_authors(book: Book, series_name: str):
        """Associe tous les auteurs connus d'un livre à une série trouvée."""
        for author in _all_authors(book):
            author_to_series.setdefault(author, set()).add(series_name.lower())

    # ── Passe 0 : miner source_data ─────────────────────────────────────────
    for book in books_no_series:
        try:
            sd = json.loads(book.source_data) if book.source_data else {}
        except Exception:
            continue
        for src_data in sd.values():
            series_name = src_data.get("series_name")
            if series_name:
                _add_proposal(series_name, "source_data", _book_dict(book))
                claimed_ids.add(book.id)
                _register_authors(book, series_name)
                break  # une seule série par livre suffit

    # ── Passe 1 : OL work title ──────────────────────────────────────────────
    books_with_key = [b for b in books_no_series if b.work_key and b.id not in claimed_ids]
    if books_with_key:
        async with httpx.AsyncClient(timeout=8) as client:
            responses = await asyncio.gather(
                *[client.get(f"https://openlibrary.org/works/{b.work_key}.json")
                  for b in books_with_key],
                return_exceptions=True,
            )
        for book, resp in zip(books_with_key, responses):
            if isinstance(resp, Exception) or resp.status_code != 200:
                continue
            work_title = resp.json().get("title", "")
            series_name, position = _extract_series_and_position(work_title, None)
            if series_name:
                _add_proposal(series_name, "openlibrary_work",
                              _book_dict(book, work_title=work_title, position=position))
                claimed_ids.add(book.id)
                _register_authors(book, series_name)

    # ── Passe 2 : DuckDuckGo par livre ──────────────────────────────────────
    to_search = [b for b in books_no_series if b.id not in claimed_ids]
    async with httpx.AsyncClient(timeout=15) as client:
        for i, book in enumerate(to_search):
            if i > 0:
                await asyncio.sleep(1.2)
            authors = json.loads(book.authors) if book.authors else []
            series_name = await search_series_ddg(book.title, authors, client)
            if series_name:
                _add_proposal(series_name, "web_search", _book_dict(book))
                claimed_ids.add(book.id)
                _register_authors(book, series_name)

    # ── Cross-référence co-auteurs ───────────────────────────────────────────
    # Si un auteur connu d'un livre non réclamé est lié à une série déjà trouvée,
    # proposer ce livre pour cette série.
    still_unclaimed = [b for b in books_no_series if b.id not in claimed_ids]
    for book in still_unclaimed:
        matched_series: set[str] = set()
        for author in _all_authors(book):
            matched_series |= author_to_series.get(author, set())
        for series_key in matched_series:
            if series_key in proposals:
                _add_proposal(proposals[series_key]["series_name"], "co_author", _book_dict(book))
                claimed_ids.add(book.id)

    # ── Passe 3 : clustering par préfixe de titre (même auteur) ─────────────
    unclaimed = [b for b in books_no_series if b.id not in claimed_ids]
    author_groups: dict[str, list[dict]] = {}
    for book in unclaimed:
        authors = json.loads(book.authors) if book.authors else []
        first_author = authors[0] if authors else "__unknown__"
        author_groups.setdefault(first_author, []).append(_book_dict(book))

    for abooks in author_groups.values():
        if len(abooks) < 2:
            continue
        for cluster in _cluster_books_by_prefix(abooks, min_words=2):
            for bdict in cluster["books"]:
                _add_proposal(cluster["series_name"], "title_cluster", bdict)

    result = [v for v in proposals.values() if len(v["books"]) >= 1]
    result.sort(key=lambda x: (-len(x["books"]), x["series_name"].lower()))
    return result


@router.post("/api/series/analyze")
async def analyze_series(request: Request, db: Session = Depends(get_db)):
    get_current_user(request, db)
    return await _analyze_series_logic(db)


@router.get("/api/series/duplicates")
def series_duplicates(request: Request, db: Session = Depends(get_db)):
    """Retourne les paires de séries dont les noms sont très proches (doublons potentiels)."""
    get_current_user(request, db)
    series_list = db.query(Series).order_by(Series.name).all()

    def _normalize(name: str) -> str:
        """Nom normalisé pour comparaison : minuscules, sans ponctuation, sans accents courants."""
        n = name.lower()
        n = re.sub(r"[,.\-''\s]+", " ", n).strip()
        return n

    pairs = []
    seen = set()
    for i, s1 in enumerate(series_list):
        for s2 in series_list[i + 1:]:
            key = (min(s1.id, s2.id), max(s1.id, s2.id))
            if key in seen:
                continue
            n1, n2 = _normalize(s1.name), _normalize(s2.name)
            # Doublon si noms normalisés identiques OU l'un contient l'autre
            if n1 == n2 or n1 in n2 or n2 in n1:
                seen.add(key)
                c1 = db.query(Book).filter(Book.series_id == s1.id).count()
                c2 = db.query(Book).filter(Book.series_id == s2.id).count()
                pairs.append({
                    "a": {**_series_to_dict(s1, c1)},
                    "b": {**_series_to_dict(s2, c2)},
                })
    return pairs


@router.post("/api/series/merge")
def merge_series(body: dict, request: Request, db: Session = Depends(get_db)):
    """Fusionne series_id_from dans series_id_into, supprime la série source."""
    user = get_current_user(request, db)
    require_contributor(user)
    from_id = body.get("from_id")
    into_id = body.get("into_id")
    if not from_id or not into_id or from_id == into_id:
        raise HTTPException(status_code=400, detail="from_id et into_id requis et distincts")
    src = db.query(Series).filter(Series.id == from_id).first()
    dst = db.query(Series).filter(Series.id == into_id).first()
    if not src or not dst:
        raise HTTPException(status_code=404, detail="Série introuvable")
    db.query(Book).filter(Book.series_id == from_id).update({"series_id": into_id})
    db.delete(src)
    db.commit()
    count = db.query(Book).filter(Book.series_id == into_id).count()
    return _series_to_dict(dst, count)


@router.get("/api/series/lookup")
async def lookup_series(q: str, request: Request, db: Session = Depends(get_db)):
    """Cherche une série par nom dans Open Library et retourne les volumes + correspondances en bibliothèque."""
    get_current_user(request, db)
    if not q or len(q.strip()) < 2:
        raise HTTPException(status_code=400, detail="Requête trop courte")

    import difflib
    from collections import defaultdict

    async with httpx.AsyncClient(timeout=12) as client:
        resp = await client.get(
            "https://openlibrary.org/search.json",
            params={"q": q, "fields": "title,author_name,isbn,series,cover_i,number_of_pages_median,first_publish_year", "limit": 100},
        )
        resp.raise_for_status()
        docs = resp.json().get("docs", [])

    by_series: dict[str, list[dict]] = defaultdict(list)
    for doc in docs:
        for s in (doc.get("series") or []):
            if difflib.SequenceMatcher(None, q.lower(), s.lower()).ratio() > 0.5:
                by_series[s].append(doc)

    if not by_series:
        return {"series_name": None, "query": q}

    best_name = max(by_series, key=lambda s: len(by_series[s]))
    volumes_raw = by_series[best_name]

    seen_titles: set[str] = set()
    volumes = []
    for doc in volumes_raw:
        title = doc.get("title", "")
        if title.lower() in seen_titles:
            continue
        seen_titles.add(title.lower())
        _, pos = _extract_series_and_position(title, None)
        isbns = doc.get("isbn") or []
        cover_id = doc.get("cover_i")
        volumes.append({
            "title": title,
            "authors": doc.get("author_name") or [],
            "position": pos,
            "isbns": isbns[:5],
            "cover_url": f"https://covers.openlibrary.org/b/id/{cover_id}-S.jpg" if cover_id else None,
        })
    volumes.sort(key=lambda v: (v["position"] is None, v["position"] or 9999))

    all_isbns = {isbn for v in volumes for isbn in v["isbns"]}
    matched_by_isbn = {}
    if all_isbns:
        for b in db.query(Book).filter(Book.isbn.in_(all_isbns)).all():
            matched_by_isbn[b.isbn] = {"id": b.id, "title": b.title, "series_id": b.series_id}

    for v in volumes:
        v["library_book"] = next((matched_by_isbn[i] for i in v["isbns"] if i in matched_by_isbn), None)

    return {
        "query": q,
        "series_name": best_name,
        "total_volumes": len(volumes),
        "in_library": sum(1 for v in volumes if v["library_book"]),
        "volumes": volumes,
        "all_series": [{"name": n, "count": len(v)} for n, v in sorted(by_series.items(), key=lambda x: -len(x[1]))[:5]],
    }


@router.post("/api/series/apply-lookup")
def apply_series_lookup(body: dict, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    require_contributor(user)
    series_name = (body.get("series_name") or "").strip()
    book_ids = body.get("book_ids") or []
    positions = body.get("positions") or {}
    if not series_name:
        raise HTTPException(status_code=400, detail="Nom de série requis")
    series = get_or_create_series(db, series_name, source="openlibrary_search")
    linked = 0
    for bid in book_ids:
        book = db.query(Book).filter(Book.id == bid).first()
        if book:
            book.series_id = series.id
            pos = positions.get(str(bid))
            if pos is not None:
                book.series_position = pos
            linked += 1
    db.commit()
    count = db.query(Book).filter(Book.series_id == series.id).count()
    return {**_series_to_dict(series, book_count=count), "linked": linked}


@router.get("/api/series/{series_id}")
def get_series(series_id: int, request: Request, db: Session = Depends(get_db)):
    get_current_user(request, db)
    series = db.query(Series).filter(Series.id == series_id).first()
    if not series:
        raise HTTPException(status_code=404, detail="Série introuvable")
    books = (
        db.query(Book)
        .filter(Book.series_id == series_id)
        .order_by(Book.series_position.nullslast(), Book.title)
        .all()
    )
    data = _series_to_dict(series, book_count=len(books))
    data["books"] = [_book_mini(b) for b in books]
    return data


@router.put("/api/series/{series_id}")
def update_series(
    series_id: int,
    body: dict,
    request: Request,
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    require_contributor(user)

    series = db.query(Series).filter(Series.id == series_id).first()
    if not series:
        raise HTTPException(status_code=404, detail="Série introuvable")
    if "name" in body:
        series.name = body["name"]
    db.commit()
    db.refresh(series)
    count = db.query(Book).filter(Book.series_id == series_id).count()
    return _series_to_dict(series, book_count=count)


@router.delete("/api/series/{series_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_series(series_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    require_admin(user)

    series = db.query(Series).filter(Series.id == series_id).first()
    if not series:
        raise HTTPException(status_code=404, detail="Série introuvable")
    # Detach books
    db.query(Book).filter(Book.series_id == series_id).update(
        {"series_id": None, "series_position": None}
    )
    db.delete(series)
    db.commit()


@router.post("/api/series/link-books")
def link_books(
    body: LinkBooksRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    require_contributor(user)

    series = get_or_create_series(db, body.series_name, source="manual")
    for book_id in body.book_ids:
        book = db.query(Book).filter(Book.id == book_id).first()
        if book:
            book.series_id = series.id
    db.commit()
    count = db.query(Book).filter(Book.series_id == series.id).count()
    return _series_to_dict(series, book_count=count)
