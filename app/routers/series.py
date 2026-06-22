import asyncio
import json
import re
import unicodedata
from collections import defaultdict
from datetime import datetime

import httpx
from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.auth import get_current_user, require_admin
from app.database import get_db
from app.models import Book, Series, SeriesProposal

router = APIRouter()

_ARTICLES = {"le", "la", "les", "l", "un", "une", "des", "du", "the", "a", "an"}


# ── Normalisation ─────────────────────────────────────────────────────────────

def _norm(s: str) -> str:
    s = unicodedata.normalize("NFD", s.lower())
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = re.sub(r"[''\"«»&]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _norm_pub(p: str | None) -> str:
    if not p:
        return ""
    return _norm(p.split("(")[0])


def _norm_authors(a: str | None) -> frozenset:
    try:
        return frozenset(_norm(x) for x in json.loads(a or "[]"))
    except Exception:
        return frozenset()


def _first_word(title: str) -> str | None:
    """Premier mot significatif du titre (ignore les articles)."""
    t = _norm(title)
    for word in t.split():
        w = re.sub(r"[^a-z0-9]", "", word)
        if w and w not in _ARTICLES and len(w) >= 3:
            return w
    return None


# ── Parsing du titre ─────────────────────────────────────────────────────────

# Chaque pattern : (regex, group_série, group_position)
_TITLE_PATTERNS = [
    # "Série. N, sous-titre"  — format bibliographique BnF/SUDOC
    # ex: "Spirou et Fantasio. 10, 1972-1975"  |  "La femme léopard. 2, Le maître…"
    (re.compile(r'^(.+?)\.\s*(\d{1,3})\s*,\s*.+$'), 1, 2),

    # "Série. Tome N"  |  "Série. Tome N, sous-titre"
    (re.compile(r'^(.+?)\.\s*[Tt]omes?\s+(\d{1,3})', re.I), 1, 2),

    # "Série Tome N"  (sans point)
    (re.compile(r'^(.+?)\s+[Tt]omes?\s+(\d{1,3})(?:\s|$|,|-)', re.I), 1, 2),

    # "Série - Tome N"  |  "Série – Tome N"
    (re.compile(r'^(.+?)\s*[-–]\s*[Tt]omes?\s+(\d{1,3})', re.I), 1, 2),

    # "Série Volume N"  |  "Série Vol. N"
    (re.compile(r'^(.+?)\s+(?:[Vv]olumes?|[Vv]ol\.)\s*(\d{1,3})', re.I), 1, 2),

    # "Série - T. N"  |  "Série T. N"
    (re.compile(r'^(.+?)\s*[-–]?\s*[Tt]\.\s*(\d{1,3})(?:\s|$)', re.I), 1, 2),

    # "Série n°N"  |  "Série no N"
    (re.compile(r'^(.+?)\s+n[o°]\s*(\d{1,3})', re.I), 1, 2),
]

_TITLE_NOISE = re.compile(
    r'\s*[\({\[].+?[\)}\]]$|'          # parenthèses/crochets en fin
    r'\s*:\s*.+$|'                       # sous-titre après ":"
    r'\s*[-–]\s*.+$',                    # sous-titre après tiret
)


def _parse_title(title: str) -> tuple[str, int | None] | None:
    """Extrait (nom_de_série, position) depuis un titre, ou None si non reconnu."""
    for pattern, gi, gp in _TITLE_PATTERNS:
        m = pattern.match(title.strip())
        if m:
            raw_name = m.group(gi).strip().rstrip('.,;:- ')
            # Nettoyer le nom restant (sous-titres, parenthèses…)
            name = _TITLE_NOISE.sub('', raw_name).strip().rstrip('.,;:- ')
            if len(name) >= 2:
                return name, int(m.group(gp))
    return None


# ── Requête Google Books pour le nom de série ─────────────────────────────────

async def _google_series_name(isbn: str, api_key: str = "") -> str | None:
    if not isbn:
        return None
    url = f"https://www.googleapis.com/books/v1/volumes?q=isbn:{isbn}"
    if api_key:
        url += f"&key={api_key}"
    try:
        async with httpx.AsyncClient(timeout=6) as client:
            r = await client.get(url)
            if r.status_code != 200:
                return None
            items = r.json().get("items", [])
            if not items:
                return None
            si = items[0].get("volumeInfo", {}).get("seriesInfo", {})
            return si.get("shortSeriesBookTitle") or si.get("bookSeries", [{}])[0].get("title")
    except Exception:
        return None


# ── OCR couverture ────────────────────────────────────────────────────────────

_COVERS_DIR = "/app/data/covers"

# Formules courantes qui précèdent le nom de série sur les couvertures BD
_SERIE_PREFIXES = re.compile(
    r"(?:les?\s+aventures?\s+(?:de|du|des?)\s+|"
    r"une?\s+aventure\s+(?:de|du|des?)\s+|"
    r"les?\s+histoires?\s+(?:de|du|des?)\s+|"
    r"collection\s+)",
    re.I,
)


def _ocr_image_path(cover_url: str) -> str | None:
    """Convertit une cover_url (/covers/xxx.jpg) en chemin local."""
    if cover_url.startswith("/covers/"):
        return f"{_COVERS_DIR}/{cover_url[len('/covers/'):]}"
    return None


def _preprocess_for_ocr(img):
    """Améliore l'image pour Tesseract : niveaux de gris, upscale, contraste."""
    from PIL import ImageEnhance, ImageFilter
    img = img.convert("L")
    # Doubler la résolution pour aider Tesseract sur les petits textes
    img = img.resize((img.width * 2, img.height * 2), resample=1)  # LANCZOS=1
    img = ImageEnhance.Contrast(img).enhance(2.0)
    img = ImageEnhance.Sharpness(img).enhance(2.0)
    return img


def _ocr_text(img, lang: str = "fra+eng") -> str:
    import pytesseract
    try:
        return pytesseract.image_to_string(img, lang=lang, config="--psm 3 --oem 1")
    except Exception:
        return ""


def _ocr_series_from_cover(cover_url: str, known_series: list[str]) -> str | None:
    """
    Lance l'OCR sur la couverture et tente d'extraire un nom de série.
    Stratégie :
      1. Matcher le texte OCR contre les séries connues (exact, mots-clés)
      2. Extraire le nom après les formules "Les aventures de …"
      3. Retourner la première ligne significative en majuscules
    """
    try:
        import pytesseract  # noqa — vérifie la dispo
        from PIL import Image
    except ImportError:
        return None

    path = _ocr_image_path(cover_url)
    if not path:
        return None

    try:
        img = Image.open(path).convert("RGB")
    except Exception:
        return None

    w, h = img.size
    # Tiers supérieur (séries souvent en haut), puis image complète
    top = img.crop((0, 0, w, h // 3))
    top_lines = [l.strip() for l in _ocr_text(_preprocess_for_ocr(top)).splitlines() if len(l.strip()) >= 2]
    full_lines = [l.strip() for l in _ocr_text(_preprocess_for_ocr(img)).splitlines() if len(l.strip()) >= 2]

    # Texte unifié tiers supérieur (jointure pour capturer "LES AVENTURES DE\nTINTIN")
    top_joined = " ".join(top_lines)
    full_joined = " ".join(full_lines)

    known_norm = {_norm(s): s for s in known_series}

    # ── Passe 1 : formules "Les aventures de X" dans le texte unifié ─────────
    # Capturer ce qui suit la formule jusqu'à la fin de ligne / ponctuation
    formula_match = re.search(
        r"(?:les?\s+aventures?\s+(?:de|du|des?)|une?\s+aventure\s+(?:de|du|des?))\s+([A-ZÀ-Ÿa-zà-ÿ][A-ZÀ-Ÿa-zà-ÿ &'\-]{1,40})",
        top_joined, re.I
    )
    if not formula_match:
        formula_match = re.search(
            r"(?:les?\s+aventures?\s+(?:de|du|des?)|une?\s+aventure\s+(?:de|du|des?))\s+([A-ZÀ-Ÿa-zà-ÿ][A-ZÀ-Ÿa-zà-ÿ &'\-]{1,40})",
            full_joined, re.I
        )
    if formula_match:
        candidate = formula_match.group(1).strip().rstrip(".,;:!?- ")
        # Vérifier si ça matche une série connue
        cn = _norm(candidate)
        if cn in known_norm:
            return known_norm[cn]
        for kn, ks in known_norm.items():
            if len(cn) >= 3 and (cn in kn or kn in cn):
                return ks
        # Pas de série connue → retourner le nom extrait directement
        if len(candidate) >= 3:
            return candidate.title() if candidate.isupper() else candidate

    # ── Passe 2 : matching direct contre les séries connues ──────────────────
    full_norm = _norm(full_joined)
    # Exact
    for kn, ks in sorted(known_norm.items(), key=lambda x: -len(x[0])):
        if kn and kn in full_norm:
            return ks
    # Mots-clés significatifs
    for kn, ks in known_norm.items():
        words = [w for w in kn.split() if w not in _ARTICLES and len(w) >= 4]
        if words and all(w in full_norm for w in words):
            return ks

    # ── Passe 3 : heuristique — première ligne en majuscules dans le top ─────
    for line in top_lines:
        clean = _SERIE_PREFIXES.sub("", line).strip().rstrip(".,;:!?- ")
        # Ignorer les lignes trop courtes ou trop longues
        if len(clean) < 3 or len(clean) > 60:
            continue
        # Ignorer les lignes qui ressemblent à des auteurs (Prénom Nom)
        if re.match(r'^[A-Z][a-z]+ [A-Z][a-z]+$', clean):
            continue
        if clean.isupper() or (clean[0].isupper() and sum(1 for c in clean if c.isupper()) >= 2):
            n = _norm(clean)
            for kn, ks in known_norm.items():
                if len(n) >= 4 and (n in kn or kn in n):
                    return ks
            # Pas de correspondance → ne pas retourner du texte aléatoire sans série connue
            # (trop de bruit)

    return None

    return None


async def _ocr_detect(db: Session, task_id: str = "ocr-series") -> dict:
    """
    Signal OCR : pour chaque livre orphelin avec couverture locale,
    tente de lire le nom de série sur l'image.
    """
    from app import scheduler as sched

    books = (
        db.query(Book)
        .filter(
            Book.enrichment_status == "ok",
            Book.series_id.is_(None),
            Book.cover_url.isnot(None),
            Book.cover_url.like("/covers/%"),
        )
        .all()
    )

    total = len(books)
    if task_id in sched._running:
        sched._running[task_id]["progress"] = {"current": 0, "total": total}

    known_series = [s.name for s in db.query(Series).all()]
    known_norm = {_norm(s): s for s in known_series}

    assigned = 0
    proposed = 0
    ocr_groups: dict[str, list[Book]] = defaultdict(list)

    # raw_names : norm_key → nom brut le plus fréquent (pour le nom canonique)
    raw_names: dict[str, list[str]] = defaultdict(list)

    loop = asyncio.get_event_loop()
    for i, b in enumerate(books):
        # Exécuter l'OCR (bloquant) dans le thread pool pour ne pas bloquer l'event loop
        name = await loop.run_in_executor(
            None, _ocr_series_from_cover, b.cover_url, known_series
        )
        if name:
            ocr_groups[_norm(name)].append(b)
            raw_names[_norm(name)].append(name)
        if task_id in sched._running:
            sched._running[task_id]["progress"]["current"] = i + 1

    # ── Fusionner les groupes dont les clés se contiennent mutuellement ──────
    # ex: "tintin" et "les aventures de tintin" → même groupe
    keys = list(ocr_groups.keys())
    merged: dict[str, str] = {}  # clé secondaire → clé principale
    for i, k1 in enumerate(keys):
        for k2 in keys[i + 1:]:
            if k2 in merged or k1 in merged:
                continue
            words1 = {w for w in k1.split() if w not in _ARTICLES and len(w) >= 3}
            words2 = {w for w in k2.split() if w not in _ARTICLES and len(w) >= 3}
            # Fusionner si l'un contient l'autre, ou si ≥50% de mots en commun
            if (k1 in k2 or k2 in k1) or (
                words1 and words2 and len(words1 & words2) / max(len(words1), len(words2)) >= 0.5
            ):
                # Garder la clé la plus courte (nom le plus concis) comme principale
                main, sec = (k1, k2) if len(k1) <= len(k2) else (k2, k1)
                merged[sec] = main
                ocr_groups[main].extend(ocr_groups.pop(sec, []))
                raw_names[main].extend(raw_names.pop(sec, []))

    for norm_name, group in ocr_groups.items():
        # Chercher série existante
        series = None
        if norm_name in known_norm:
            series = db.query(Series).filter(Series.name == known_norm[norm_name]).first()
        if not series:
            series = next(
                (s for s in db.query(Series).all() if _norm(s.name) == norm_name),
                None,
            )

        if series:
            # Relier directement à la série connue
            for b in group:
                b.series_id = series.id
                assigned += 1
            db.commit()
        elif len(group) >= 2:
            # Nouvelle série potentielle → proposition
            ids = [b.id for b in group]
            existing = [
                frozenset(json.loads(p.book_ids))
                for p in db.query(SeriesProposal).filter(SeriesProposal.status == "pending").all()
            ]
            if frozenset(ids) not in existing:
                from collections import Counter
                canon_raw = Counter(raw_names.get(norm_name, [])).most_common(1)
                canon = canon_raw[0][0] if canon_raw else norm_name
                p = SeriesProposal(
                    book_ids=json.dumps(ids),
                    proposed_name=canon,
                    signal="ocr",
                    status="pending",
                )
                db.add(p)
                proposed += 1
                db.commit()

    return {"auto_assigned": assigned, "proposals": proposed}


# ── Algorithme de détection ───────────────────────────────────────────────────

async def _detect(db: Session, task_id: str = "detect-series") -> dict:
    from app import scheduler as sched
    import app.settings as cfg
    gb_key = cfg.get(db, "googlebooks_api_key") or ""

    books = (
        db.query(Book)
        .filter(Book.enrichment_status == "ok", Book.series_id.is_(None))
        .all()
    )

    total = len(books)
    if task_id in sched._running:
        sched._running[task_id]["progress"] = {"current": 0, "total": total}

    auto_assigned = 0
    proposals_created = 0
    already_proposed_book_sets: list[frozenset] = [
        frozenset(json.loads(p.book_ids))
        for p in db.query(SeriesProposal).filter(SeriesProposal.status == "pending").all()
    ]

    def _already_proposed(book_ids: list[int]) -> bool:
        s = frozenset(book_ids)
        return any(s == existing for existing in already_proposed_book_sets)

    # ── Signal 0 : parsing direct du titre ──────────────────────────────────
    # Groupe les orphelins dont le titre contient explicitement un nom de série.
    # Haute confiance → auto-assignation dès 1 livre si la série existe déjà,
    # création de série si ≥1 livre avec position, regroupement si ≥2 livres.

    title_groups: dict[str, list[tuple[Book, int | None]]] = defaultdict(list)
    parsed_books: set[int] = set()

    for i, b in enumerate(books):
        result = _parse_title(b.title)
        if result:
            series_name, position = result
            title_groups[_norm(series_name)].append((b, position))
            parsed_books.add(b.id)
        if task_id in sched._running:
            sched._running[task_id]["progress"]["current"] = i + 1
        if i % 10 == 0:
            await asyncio.sleep(0)

    for norm_name, entries in title_groups.items():
        # Trouver le nom cannonique (le plus fréquent dans le groupe)
        name_counts: dict[str, int] = defaultdict(int)
        for b, _ in entries:
            r = _parse_title(b.title)
            if r:
                name_counts[r[0]] += 1
        canon_name = max(name_counts, key=name_counts.__getitem__)

        # Chercher une série existante (par nom normalisé)
        existing = db.query(Series).all()
        series = next((s for s in existing if _norm(s.name) == norm_name), None)

        if not series:
            # Chercher aussi les séries dont le nom normalisé est contenu
            series = next((s for s in existing if norm_name in _norm(s.name) or _norm(s.name) in norm_name), None)

        if not series:
            series = Series(name=canon_name, source="detected")
            db.add(series)
            db.flush()

        for b, position in entries:
            b.series_id = series.id
            if position is not None and b.series_position is None:
                b.series_position = position
            auto_assigned += 1

    db.commit()

    # Recalculer les orphelins après Signal 0
    books = (
        db.query(Book)
        .filter(Book.enrichment_status == "ok", Book.series_id.is_(None))
        .all()
    )

    # Remettre le compteur à zéro pour les signaux 1+2
    if task_id in sched._running:
        sched._running[task_id]["progress"] = {"current": 0, "total": len(books)}

    # Index : livres déjà en série
    series_books = db.query(Book).filter(Book.series_id.isnot(None)).all()

    # (auteur_set, pub) → set(series_id)
    author_pub_series: dict[tuple, set] = defaultdict(set)
    for b in series_books:
        pub = _norm_pub(b.publisher)
        for a in _norm_authors(b.authors):
            author_pub_series[(a, pub)].add(b.series_id)

    # (first_word, pub) → set(series_id)
    prefix_pub_series: dict[tuple, set] = defaultdict(set)
    for b in series_books:
        fw = _first_word(b.title)
        pub = _norm_pub(b.publisher)
        if fw:
            prefix_pub_series[(fw, pub)].add(b.series_id)

    # Grouper les livres orphelins par (first_word, pub) et (authors, pub)
    prefix_groups: dict[tuple, list] = defaultdict(list)
    author_groups: dict[tuple, list] = defaultdict(list)

    for i, b in enumerate(books):
        fw = _first_word(b.title)
        pub = _norm_pub(b.publisher)
        if fw:
            prefix_groups[(fw, pub)].append(b)
        au = _norm_authors(b.authors)
        if au:
            author_groups[(au, pub)].append(b)
        if task_id in sched._running:
            sched._running[task_id]["progress"]["current"] = i + 1
        if i % 10 == 0:
            await asyncio.sleep(0)

    # ── Signal 1 : préfixe + éditeur ────────────────────────────────────────
    for (fw, pub), group in prefix_groups.items():
        if len(group) < 1:
            continue
        known_sids = prefix_pub_series.get((fw, pub), set())

        if len(known_sids) == 1:
            # Haute confiance : relier directement
            sid = next(iter(known_sids))
            for b in group:
                b.series_id = sid
                auto_assigned += 1
            db.commit()

        elif len(known_sids) == 0 and len(group) >= 2:
            # Nouveau groupe potentiel → proposition
            ids = [b.id for b in group]
            if _already_proposed(ids):
                continue
            # Chercher le nom via Google Books sur le premier livre avec ISBN
            name = None
            for b in group:
                if b.isbn:
                    name = await _google_series_name(b.isbn, gb_key)
                    if name:
                        break
            p = SeriesProposal(
                book_ids=json.dumps(ids),
                proposed_name=name,
                signal="prefix",
                status="pending",
            )
            db.add(p)
            proposals_created += 1
            already_proposed_book_sets.append(frozenset(ids))

    db.commit()

    # ── Signal 2 : auteur + éditeur ─────────────────────────────────────────
    # Livres encore sans série après le signal 1
    books_still_orphan = (
        db.query(Book)
        .filter(Book.enrichment_status == "ok", Book.series_id.is_(None))
        .all()
    )

    author_groups2: dict[tuple, list] = defaultdict(list)
    for b in books_still_orphan:
        pub = _norm_pub(b.publisher)
        au = _norm_authors(b.authors)
        if au:
            author_groups2[(au, pub)].append(b)

    for (au, pub), group in author_groups2.items():
        if len(group) < 1:
            continue
        # Chercher les séries connues pour cet auteur+éditeur (après signal 1)
        known_sids: set = set()
        for a in au:
            known_sids |= author_pub_series.get((a, pub), set())

        if len(known_sids) == 1:
            sid = next(iter(known_sids))
            for b in group:
                b.series_id = sid
                auto_assigned += 1
            db.commit()

        elif len(known_sids) > 1:
            # Ambigu → proposition à valider
            ids = [b.id for b in group]
            if _already_proposed(ids):
                continue
            name = None
            for b in group:
                if b.isbn:
                    name = await _google_series_name(b.isbn, gb_key)
                    if name:
                        break
            p = SeriesProposal(
                book_ids=json.dumps(ids),
                proposed_name=name,
                signal="author",
                status="pending",
            )
            db.add(p)
            proposals_created += 1
            already_proposed_book_sets.append(frozenset(ids))

        elif len(known_sids) == 0 and len(group) >= 2:
            # Nouveau groupe sans série connue → proposition
            ids = [b.id for b in group]
            if _already_proposed(ids):
                continue
            name = None
            for b in group:
                if b.isbn:
                    name = await _google_series_name(b.isbn, gb_key)
                    if name:
                        break
            p = SeriesProposal(
                book_ids=json.dumps(ids),
                proposed_name=name,
                signal="author",
                status="pending",
            )
            db.add(p)
            proposals_created += 1
            already_proposed_book_sets.append(frozenset(ids))

    db.commit()

    return {"auto_assigned": auto_assigned, "proposals": proposals_created}


# ── API ───────────────────────────────────────────────────────────────────────

@router.post("/api/series/detect")
async def detect_series(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    require_admin(user)
    result = await _detect(db)
    return result


@router.post("/api/series/ocr-covers")
async def ocr_covers(request: Request, db: Session = Depends(get_db)):
    """OCR sur une liste de cover_url → retourne le nom de série détecté (ou null)."""
    user = get_current_user(request, db)
    require_admin(user)
    body = await request.json()
    cover_urls: list[str] = body.get("cover_urls", [])
    known_series = [s.name for s in db.query(Series).all()]
    loop = asyncio.get_event_loop()
    names: list[str] = []
    for url in cover_urls[:5]:  # max 5 couvertures par appel
        name = await loop.run_in_executor(None, _ocr_series_from_cover, url, known_series)
        if name:
            names.append(name)
    if not names:
        return {"name": None}
    from collections import Counter
    canon_raw = Counter(names).most_common(1)[0][0]
    return {"name": canon_raw}


@router.get("/api/series/proposals")
def list_proposals(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    require_admin(user)
    proposals = (
        db.query(SeriesProposal)
        .filter(SeriesProposal.status == "pending")
        .order_by(SeriesProposal.detected_at.desc())
        .all()
    )
    out = []
    for p in proposals:
        ids = json.loads(p.book_ids)
        books = db.query(Book).filter(Book.id.in_(ids)).all()
        out.append({
            "id": p.id,
            "signal": p.signal,
            "proposed_name": p.proposed_name,
            "existing_series_id": p.existing_series_id,
            "existing_series_name": p.existing_series.name if p.existing_series else None,
            "detected_at": p.detected_at.isoformat() if p.detected_at else None,
            "books": [
                {
                    "id": b.id,
                    "title": b.title,
                    "authors": json.loads(b.authors or "[]"),
                    "cover_url": b.cover_url,
                    "series_id": b.series_id,
                }
                for b in books
            ],
        })
    return out


@router.post("/api/series/proposals/{proposal_id}/accept")
def accept_proposal(
    proposal_id: int,
    body: dict,
    request: Request,
    db: Session = Depends(get_db),
):
    """
    body: { name: str, existing_series_id: int | null }
    Si existing_series_id fourni → relier à cette série.
    Sinon → créer une nouvelle série avec `name`.
    """
    user = get_current_user(request, db)
    require_admin(user)

    from fastapi import HTTPException
    p = db.query(SeriesProposal).filter(SeriesProposal.id == proposal_id).first()
    if not p:
        raise HTTPException(404, "Proposition introuvable")

    existing_sid = body.get("existing_series_id")
    name = (body.get("name") or "").strip()

    if existing_sid:
        series = db.query(Series).filter(Series.id == existing_sid).first()
        if not series:
            raise HTTPException(400, "Série introuvable")
    elif name:
        series = db.query(Series).filter(Series.name == name).first()
        if not series:
            series = Series(name=name, source="detected")
            db.add(series)
            db.flush()
    else:
        raise HTTPException(400, "Nom de série requis")

    ids = json.loads(p.book_ids)
    books = db.query(Book).filter(Book.id.in_(ids)).all()
    for b in books:
        b.series_id = series.id

    p.status = "accepted"
    db.commit()
    return {"ok": True, "series_id": series.id, "series_name": series.name, "linked": len(books)}


@router.post("/api/series/proposals/{proposal_id}/reject")
def reject_proposal(proposal_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    require_admin(user)
    from fastapi import HTTPException
    p = db.query(SeriesProposal).filter(SeriesProposal.id == proposal_id).first()
    if not p:
        raise HTTPException(404, "Proposition introuvable")
    p.status = "rejected"
    db.commit()
    return {"ok": True}


@router.get("/api/series")
def list_series(request: Request, db: Session = Depends(get_db)):
    get_current_user(request, db)
    series = db.query(Series).order_by(Series.name).all()
    out = []
    for s in series:
        books = sorted(s.books, key=lambda b: (b.series_position is None, b.series_position or 0))
        cover = next((b.cover_url for b in books if b.cover_url), None)
        out.append({"id": s.id, "name": s.name, "book_count": len(books), "cover_url": cover})
    return out


@router.get("/api/series/{series_id}/books")
def get_series_books(series_id: int, request: Request, db: Session = Depends(get_db)):
    get_current_user(request, db)
    from fastapi import HTTPException
    series = db.query(Series).filter(Series.id == series_id).first()
    if not series:
        raise HTTPException(404)
    books = sorted(series.books, key=lambda b: (b.series_position is None, b.series_position or 0))
    return {
        "id": series.id,
        "name": series.name,
        "books": [
            {
                "id": b.id,
                "title": b.title,
                "authors": json.loads(b.authors or "[]"),
                "cover_url": b.cover_url,
                "series_position": b.series_position,
                "publisher": b.publisher,
            }
            for b in books
        ],
    }


@router.post("/api/series/purge")
def purge_series(request: Request, db: Session = Depends(get_db)):
    """Supprime toutes les séries, proposals et réinitialise les livres."""
    user = get_current_user(request, db)
    require_admin(user)
    from sqlalchemy import text
    db.execute(text("UPDATE books SET series_id = NULL, series_position = NULL"))
    db.execute(text("DELETE FROM series_proposals"))
    db.execute(text("DELETE FROM series_missing_volumes"))
    db.execute(text("DELETE FROM series"))
    db.commit()
    return {"ok": True}
