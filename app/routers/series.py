import asyncio
import json
import re
import unicodedata
import xml.etree.ElementTree as ET
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
    if not a:
        return frozenset()
    try:
        return frozenset(_norm(x) for x in json.loads(a))
    except json.JSONDecodeError:
        return frozenset()


def _first_word(title: str) -> str | None:
    """Deux premiers mots significatifs du titre (ignore les articles)."""
    t = _norm(title)
    words = []
    for word in t.split():
        w = re.sub(r"[^a-z0-9]", "", word)
        if w and w not in _ARTICLES and len(w) >= 3:
            words.append(w)
            if len(words) == 2:
                break
    return " ".join(words) if words else None


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
    # Upscale pour aider Tesseract — moins aggressif si l'image est déjà grande
    scale = 1.5 if img.width >= 500 else 2
    img = img.resize((int(img.width * scale), int(img.height * scale)), resample=1)  # LANCZOS=1
    img = ImageEnhance.Contrast(img).enhance(2.0)
    img = ImageEnhance.Sharpness(img).enhance(2.0)
    return img


def _ocr_text(img, lang: str = "fra+eng") -> str:
    import pytesseract
    try:
        return pytesseract.image_to_string(img, lang=lang, config="--psm 3 --oem 1")
    except Exception:
        return ""


_KNOWN_PUBLISHERS = {
    "dupuis", "dargaud", "casterman", "lombard", "glenat", "delcourt",
    "soleil", "bamboo", "lucky", "comics", "marvel", "dc", "editions",
    "rue", "de", "la", "moisson", "futuropolis", "humanoides", "associes",
}


def _ocr_series_from_cover(cover_url: str, known_series: list[str], book_authors: list[str] | None = None) -> str | None:
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

    def _clean_line(s: str) -> str:
        """Nettoie les caractères parasites OCR : underscores, tirets isolés, etc."""
        return re.sub(r'[_|\\]', '', s).strip().strip('.,;:!?- ')

    w, h = img.size
    # Scanner plusieurs bandes verticales pour maximiser les chances de trouver le titre
    crops = {
        "top_quarter": img.crop((0, 0, w, h // 4)),
        "top_third":   img.crop((0, 0, w, h // 3)),
        "top_half":    img.crop((0, 0, w, h // 2)),
        "full":        img,
    }
    crop_lines: dict[str, list[str]] = {}
    for name, crop in crops.items():
        crop_lines[name] = [_clean_line(l) for l in _ocr_text(_preprocess_for_ocr(crop)).splitlines()
                            if len(_clean_line(l)) >= 2]

    top_lines = crop_lines["top_quarter"]
    full_lines = crop_lines["full"]

    # Texte unifié (jointure pour capturer "LES AVENTURES DE\nTINTIN")
    all_lines = []
    seen = set()
    for lines in crop_lines.values():
        for l in lines:
            if l not in seen:
                all_lines.append(l)
                seen.add(l)
    top_joined = " ".join(top_lines)
    full_joined = " ".join(all_lines)

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

    # ── Passe 2b : fuzzy matching (distance d'édition) contre les séries connues ─
    # Pour les erreurs OCR type "Rabh Alan" → "Ralph Azham"
    def _levenshtein(a: str, b: str) -> int:
        if len(a) < len(b):
            return _levenshtein(b, a)
        if not b:
            return len(a)
        prev = list(range(len(b) + 1))
        for i, ca in enumerate(a):
            curr = [i + 1]
            for j, cb in enumerate(b):
                curr.append(min(prev[j + 1] + 1, curr[j] + 1, prev[j] + (ca != cb)))
            prev = curr
        return prev[-1]

    for line in all_lines:
        ln = _norm(line)
        if len(ln) < 4:
            continue
        for kn, ks in known_norm.items():
            if len(kn) < 4:
                continue
            max_len = max(len(ln), len(kn))
            dist = _levenshtein(ln, kn)
            # Similarité ≥ 70% (distance ≤ 30% des caractères)
            if dist <= max_len * 0.30:
                return ks

    # Construire le set des mots d'auteurs à exclure
    author_norms: set[str] = set()
    if book_authors:
        for a in book_authors:
            author_norms.add(_norm(a))
            # Ajouter aussi chaque mot du nom (nom, prénom séparément)
            for word in _norm(a).split():
                if len(word) >= 3:
                    author_norms.add(word)

    def _is_excluded(s: str) -> bool:
        """Retourne True si la ligne ressemble à un auteur ou éditeur connu."""
        n = _norm(s)
        # Éditeur connu
        if n in _KNOWN_PUBLISHERS:
            return True
        # Correspond exactement à un auteur connu
        if n in author_norms:
            return True
        # Ligne dont tous les mots principaux sont des mots d'auteur
        words = [w for w in n.split() if len(w) >= 3]
        if words and all(w in author_norms for w in words):
            return True
        # Exclure uniquement si ça ressemble à un des auteurs du livre (fuzzy)
        if author_norms:
            raw_words = s.split()
            if 2 <= len(raw_words) <= 3:
                # Chaque mot est-il proche d'un mot d'auteur connu ?
                words_norm = [_norm(w) for w in raw_words if w]
                if all(any(abs(len(wn) - len(an)) <= 1 and wn[:2] == an[:2] for an in author_norms) for wn in words_norm):
                    return True
        return False

    # ── Passe 3 : heuristique — lignes en majuscules dans toutes les bandes ────
    best_candidate: str | None = None
    for line in all_lines:
        clean = _SERIE_PREFIXES.sub("", line).strip()
        if len(clean) < 3 or len(clean) > 60:
            continue
        if _is_excluded(clean):
            continue
        if clean.isupper() or (clean[0].isupper() and sum(1 for c in clean if c.isupper()) >= 2):
            n = _norm(clean)
            # D'abord chercher dans les séries connues
            for kn, ks in known_norm.items():
                if len(n) >= 4 and (n in kn or kn in n):
                    return ks
            # Conserver comme candidat (1 ou 2 mots, majuscules ou initiales)
            if len(clean) >= 4 and best_candidate is None:
                words_list = clean.split()
                # 1 mot tout en majuscules → très probable série (LOUCA, TINTIN…)
                if len(words_list) == 1 and clean.isupper():
                    best_candidate = clean.title()
                # 2 mots avec initiales majuscules, pas un auteur connu → possible série
                elif len(words_list) == 2 and all(w[0].isupper() for w in words_list if w):
                    best_candidate = clean

    return best_candidate


async def _sudoc_detect(db: Session, task_id: str = "sudoc-series") -> dict:
    """
    Interroge SUDOC + lookup_isbn pour chaque livre orphelin avec ISBN.
    Auto-assigne si la série existe déjà, sinon crée une proposition.
    """
    from app.routers.scan import _lookup_series_sudoc
    from app.lookup import lookup_isbn
    from app import scheduler as sched

    books = (
        db.query(Book)
        .filter(Book.enrichment_status == "ok", Book.series_id.is_(None), Book.isbn.isnot(None))
        .all()
    )
    total = len(books)
    if task_id in sched._running:
        sched._running[task_id]["progress"] = {"current": 0, "total": total}

    assigned = 0
    proposed = 0

    # Charger toutes les séries une seule fois (dict norm_name → Series)
    series_by_norm: dict[str, Series] = {_norm(s.name): s for s in db.query(Series).all()}

    for i, b in enumerate(books):
        series_name = None
        series_vol = None

        result = await _lookup_series_sudoc(b.isbn)
        if result:
            series_name, series_vol = result
        else:
            info = await lookup_isbn(b.isbn)
            if info and info.get("series_name"):
                series_name = info["series_name"]
                series_vol = info.get("series_position")

        if series_name:
            series = series_by_norm.get(_norm(series_name))
            if not series:
                series = Series(name=series_name, source="sudoc")
                db.add(series)
                db.flush()
                series_by_norm[_norm(series_name)] = series
            b.series_id = series.id
            if series_vol is not None and b.series_position is None:
                b.series_position = int(series_vol)
            assigned += 1
            db.commit()

        if task_id in sched._running:
            sched._running[task_id]["progress"]["current"] = i + 1
        await asyncio.sleep(0)

    return {"auto_assigned": assigned, "proposals": proposed}


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
        authors = json.loads(b.authors or "[]")
        name = await loop.run_in_executor(
            None, _ocr_series_from_cover, b.cover_url, known_series, authors
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

    # Charger toutes les séries une seule fois pour éviter N+1 queries
    all_series_by_norm: dict[str, Series] = {_norm(s.name): s for s in db.query(Series).all()}

    for norm_name, entries in title_groups.items():
        # Trouver le nom cannonique (le plus fréquent dans le groupe)
        name_counts: dict[str, int] = defaultdict(int)
        for b, _ in entries:
            r = _parse_title(b.title)
            if r:
                name_counts[r[0]] += 1
        canon_name = max(name_counts, key=name_counts.__getitem__)

        # Chercher une série existante (par nom normalisé)
        series = all_series_by_norm.get(norm_name)

        if not series:
            # Chercher aussi les séries dont le nom normalisé est contenu
            series = next(
                (s for n, s in all_series_by_norm.items() if norm_name in n or n in norm_name),
                None,
            )

        if not series:
            series = Series(name=canon_name, source="detected")
            db.add(series)
            db.flush()
            all_series_by_norm[_norm(canon_name)] = series

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


@router.post("/api/series/sudoc-lookup")
async def sudoc_lookup(request: Request, db: Session = Depends(get_db)):
    """
    Interroge plusieurs sources pour trouver la série d'un groupe de livres.
    Ordre de priorité : SUDOC → Open Library (champ series) → lookup_isbn.
    """
    user = get_current_user(request, db)
    require_admin(user)
    body = await request.json()
    book_ids: list[int] = body.get("book_ids", [])
    from app.routers.scan import _lookup_series_sudoc
    from app.lookup import lookup_isbn
    books = db.query(Book).filter(Book.id.in_(book_ids)).all()
    for b in books:
        if not b.isbn:
            continue
        # Source 1 : SUDOC (meilleure pour les BDs françaises)
        result = await _lookup_series_sudoc(b.isbn)
        if result:
            name, vol = result
            return {"name": name, "volume": vol, "isbn": b.isbn, "source": "SUDOC"}
        # Source 2 : lookup_isbn (Open Library, Google Books…) → champ series_name
        info = await lookup_isbn(b.isbn)
        if info and info.get("series_name"):
            return {"name": info["series_name"], "volume": info.get("series_position"), "isbn": b.isbn, "source": info.get("source", "lookup")}
    return {"name": None}


@router.post("/api/series/ocr-cover-single")
async def ocr_cover_single(request: Request, db: Session = Depends(get_db)):
    """OCR sur une seule couverture → retourne le nom détecté (ou null)."""
    user = get_current_user(request, db)
    require_admin(user)
    body = await request.json()
    cover_url: str = body.get("cover_url", "")
    book_authors: list[str] = body.get("authors", [])
    known_series = [s.name for s in db.query(Series).all()]
    loop = asyncio.get_event_loop()
    name = await loop.run_in_executor(None, _ocr_series_from_cover, cover_url, known_series, book_authors)
    return {"name": name}


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
                    "isbn": b.isbn,
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


@router.patch("/api/series/proposals/{proposal_id}")
def patch_proposal(proposal_id: int, body: dict, request: Request, db: Session = Depends(get_db)):
    """Met à jour le nom proposé et/ou le signal d'une proposition."""
    user = get_current_user(request, db)
    require_admin(user)
    from fastapi import HTTPException
    p = db.query(SeriesProposal).filter(SeriesProposal.id == proposal_id).first()
    if not p:
        raise HTTPException(404, "Proposition introuvable")
    if "proposed_name" in body:
        p.proposed_name = body["proposed_name"]
    if "signal" in body:
        p.signal = body["signal"]
    if "add_signal" in body:
        existing = [s for s in (p.signal or "").split(",") if s]
        new_sig = body["add_signal"]
        if new_sig not in existing:
            existing.append(new_sig)
        p.signal = ",".join(existing)
    db.commit()
    return {"ok": True}


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


def _clean_series_names_logic(db, task_id: str = "clean-series") -> str:
    """Normalise les noms de séries et fusionne les doublons."""
    from app import scheduler as sched
    from app.models import SeriesProposal

    def _normalize(name: str) -> str:
        name = re.sub(r"\s+", " ", name).strip()
        # Supprimer la ponctuation en fin de nom (., !, ?, :, …)
        name = re.sub(r"[.!?:…]+$", "", name).strip()
        # Capitaliser uniquement la première lettre
        if name and name[0].islower():
            name = name[0].upper() + name[1:]
        return name

    all_series = db.query(Series).order_by(Series.id).all()
    total = len(all_series)
    if task_id in sched._running:
        sched._running[task_id]["progress"] = {"current": 0, "total": total}

    renamed = 0
    merged = 0

    def _merge_into(keeper: Series, duplicate: Series):
        """Déplace les livres de duplicate vers keeper puis supprime duplicate."""
        for b in list(duplicate.books):
            if b.series_position is not None:
                conflict = next(
                    (ob for ob in keeper.books if ob.series_position == b.series_position), None
                )
                if conflict:
                    b.series_position = None
            b.series_id = keeper.id
        db.query(SeriesProposal).filter(SeriesProposal.existing_series_id == duplicate.id).update(
            {"existing_series_id": keeper.id}
        )
        db.delete(duplicate)

    # Passe 1 : renommer (ponctuation, espaces, première lettre)
    for i, s in enumerate(all_series):
        if task_id in sched._running:
            sched._running[task_id]["progress"]["current"] = i + 1
        try:
            db.refresh(s)
        except Exception:
            continue
        cleaned = _normalize(s.name)
        if cleaned == s.name:
            continue
        existing = db.query(Series).filter(Series.name == cleaned, Series.id != s.id).first()
        if existing:
            # Garder celui qui a le plus de livres
            keeper, dup = (existing, s) if len(existing.books) >= len(s.books) else (s, existing)
            keeper.name = cleaned
            _merge_into(keeper, dup)
            merged += 1
        else:
            s.name = cleaned
            renamed += 1
    db.commit()

    # Passe 2 : fusionner les séries avec le même _norm() (casse, accents)
    all_series2 = db.query(Series).order_by(Series.id).all()
    norm_map: dict[str, Series] = {}
    for s in all_series2:
        key = _norm(s.name)
        if key in norm_map:
            keeper = norm_map[key]
            # Garder le nom du keeper (celui avec le plus de livres)
            if len(s.books) > len(keeper.books):
                keeper, s = s, keeper
                norm_map[key] = keeper
            _merge_into(keeper, s)
            merged += 1
        else:
            norm_map[key] = s
    db.commit()

    parts = []
    if renamed:
        parts.append(f"{renamed} renommée(s)")
    if merged:
        parts.append(f"{merged} fusionnée(s)")
    return ", ".join(parts) if parts else "Aucun changement"


@router.post("/api/series/clean-names")
def clean_series_names(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    require_admin(user)
    result = _clean_series_names_logic(db)
    return {"result": result}


@router.get("/api/series/missing")
def get_missing_volumes(request: Request, db: Session = Depends(get_db)):
    """Retourne les trous de tomes pour chaque série ayant des positions renseignées."""
    get_current_user(request, db)
    all_series = db.query(Series).order_by(Series.name).all()
    result = []
    for s in all_series:
        books_with_pos = sorted(
            [b for b in s.books if b.series_position is not None],
            key=lambda b: b.series_position,
        )
        if len(books_with_pos) < 2:
            continue
        positions = [int(b.series_position) for b in books_with_pos if b.series_position == int(b.series_position)]
        if not positions:
            continue
        min_pos, max_pos = min(positions), max(positions)
        owned = set(positions)
        gaps = [i for i in range(min_pos, max_pos + 1) if i not in owned]
        if not gaps:
            continue
        cover = next((b.cover_url for b in s.books if b.cover_url), None)
        result.append({
            "id": s.id,
            "name": s.name,
            "cover_url": cover,
            "owned": sorted(owned),
            "max_owned": max_pos,
            "gaps": gaps,
            "books": [
                {
                    "id": b.id,
                    "title": b.title,
                    "cover_url": b.cover_url,
                    "series_position": b.series_position,
                }
                for b in books_with_pos
            ],
        })
    return result


@router.get("/api/series/{series_id}/check-bnf")
async def check_bnf_volumes(series_id: int, request: Request, db: Session = Depends(get_db)):
    """Interroge la BnF pour trouver le nombre total de volumes connus pour une série."""
    get_current_user(request, db)
    from fastapi import HTTPException
    series = db.query(Series).filter(Series.id == series_id).first()
    if not series:
        raise HTTPException(404)

    result = await _bnf_check_one_series(series, db)
    if not result.get("volumes_found"):
        return {"series_id": series_id, "name": series.name, "volumes_found": [], "max_known": None, "source": None}

    return {
        "series_id": series_id,
        "name": series.name,
        "volumes_found": result["volumes_found"],
        "max_known": max(result["volumes_found"]),
        "titles": result.get("titles", {}),
        "isbn_by_volume": result["isbn_by_volume"],
        "in_library": result["in_library"],
        "source": "BnF",
    }


def _isbn10_from_13(isbn13: str) -> str | None:
    if not isbn13 or not isbn13.startswith("978") or len(isbn13) != 13:
        return None
    body = isbn13[3:12]
    s = sum((10 - i) * int(d) for i, d in enumerate(body))
    r = (11 - s % 11) % 11
    return body + ("X" if r == 10 else str(r))


def _isbn13_from_10(isbn10: str) -> str | None:
    if not isbn10 or len(isbn10) != 10:
        return None
    body = "978" + isbn10[:9]
    s = sum(int(d) * (1 if i % 2 == 0 else 3) for i, d in enumerate(body))
    return body + str((10 - s % 10) % 10)


async def _bnf_check_one_series(series: Series, db) -> dict:
    """
    Interroge la BnF en 2 étapes pour une série :
    1. ISBN d'un livre possédé → nom exact BnF (champ 225 $a)
    2. Recherche tous les volumes avec ce nom, extrait ISBNs, rattache les livres trouvés
    Retourne un dict avec volumes_found, isbn_by_volume, in_library, assigned.
    """
    ns_map = {"srw": "http://www.loc.gov/zing/srw/", "mxc": "info:lc/xmlns/marcxchange-v2"}
    volumes_found: set[int] = set()
    titles_found: dict[int, str] = {}
    isbn_by_volume: dict[int, str] = {}

    async with httpx.AsyncClient(timeout=10) as client:
        # Étape 1 : trouver le nom exact BnF via ISBN d'un livre possédé
        bnf_series_name: str | None = None
        for book in [b for b in series.books if b.isbn][:5]:
            isbn10 = _isbn10_from_13(book.isbn) or (book.isbn if len(book.isbn) == 10 else None)
            if not isbn10:
                continue
            resp = await client.get(
                "https://catalogue.bnf.fr/api/SRU",
                params={"version": "1.2", "operation": "searchRetrieve",
                        "query": f'bib.isbn any "{isbn10}"',
                        "maximumRecords": "3", "recordSchema": "unimarcxchange"},
            )
            if resp.status_code != 200:
                continue
            root = ET.fromstring(resp.text)
            for rec in root.findall(".//mxc:record", ns_map):
                name_225 = None
                name_461 = None
                for df in rec.findall("mxc:datafield", ns_map):
                    tag = df.get("tag")
                    subs = {sf.get("code"): sf.text for sf in df.findall("mxc:subfield", ns_map)}
                    if tag == "225" and subs.get("a") and subs.get("v"):
                        name_225 = subs["a"]
                    elif tag == "461" and subs.get("t") and subs.get("v"):
                        name_461 = subs["t"]
                bnf_series_name = name_225 or name_461
                if bnf_series_name:
                    break
            if bnf_series_name:
                break

        search_name = bnf_series_name or series.name
        norm_search = _norm(search_name)

        # Étape 2 : trouver tous les volumes
        resp2 = await client.get(
            "https://catalogue.bnf.fr/api/SRU",
            params={"version": "1.2", "operation": "searchRetrieve",
                    "query": f'bib.anywhere adj "{search_name}"',
                    "maximumRecords": "100", "recordSchema": "unimarcxchange"},
        )
        if resp2.status_code != 200:
            return {}
        root2 = ET.fromstring(resp2.text)
        for record in root2.findall(".//mxc:record", ns_map):
            vol_num = None; title_val = None; isbn_val = None; series_match = False
            for df in record.findall("mxc:datafield", ns_map):
                tag = df.get("tag", "")
                subs = {sf.get("code"): sf.text for sf in df.findall("mxc:subfield", ns_map)}
                if tag == "010" and subs.get("a"):
                    v = re.sub(r"[^\dX]", "", subs["a"].upper())
                    if len(v) in (10, 13):
                        isbn_val = v
                if tag == "225" and _norm(subs.get("a", "")) == norm_search:
                    series_match = True
                    try:
                        vol_num = int(re.sub(r"[^\d]", "", subs.get("v", "") or ""))
                    except ValueError:
                        pass
                if tag == "461" and _norm(subs.get("t", "")) == norm_search:
                    series_match = True
                    try:
                        vol_num = int(re.sub(r"[^\d]", "", subs.get("v", "") or ""))
                    except ValueError:
                        pass
                if tag == "200" and subs.get("a"):
                    title_val = subs["a"]
            if series_match and vol_num and vol_num > 0:
                volumes_found.add(vol_num)
                if title_val:
                    titles_found[vol_num] = title_val
                if isbn_val:
                    if len(isbn_val) == 10:
                        isbn_val = _isbn13_from_10(isbn_val) or isbn_val
                    isbn_by_volume[vol_num] = isbn_val

    # Rattacher les livres trouvés qui ne sont pas encore dans la série
    in_library: dict[int, int] = {}
    assigned = 0
    for vol, isbn in isbn_by_volume.items():
        book = db.query(Book).filter(Book.isbn == isbn).first()
        if book:
            in_library[vol] = book.id
            if book.series_id != series.id:
                book.series_id = series.id
                book.series_position = float(vol)
                assigned += 1
    if assigned:
        db.commit()

    return {
        "volumes_found": sorted(volumes_found),
        "titles": titles_found,
        "isbn_by_volume": isbn_by_volume,
        "in_library": in_library,
        "assigned": assigned,
    }


async def _bnf_series_logic(db, task_id: str = "bnf-series") -> dict:
    """
    Tâche batch : pour chaque série avec des trous, interroge la BnF
    et rattache automatiquement les livres trouvés.
    """
    from app import scheduler as sched

    # Séries ayant au moins 2 livres avec position et au moins un trou
    all_series = db.query(Series).all()
    series_with_gaps: list[Series] = []
    for s in all_series:
        positions = sorted(set(
            int(b.series_position) for b in s.books
            if b.series_position is not None and b.series_position == int(b.series_position)
        ))
        if len(positions) >= 2:
            min_p, max_p = min(positions), max(positions)
            if any(i not in set(positions) for i in range(min_p, max_p + 1)):
                series_with_gaps.append(s)

    total = len(series_with_gaps)
    total_assigned = 0
    total_found = 0

    if task_id in sched._running:
        sched._running[task_id]["progress"] = {"current": 0, "total": total}

    for i, series in enumerate(series_with_gaps):
        try:
            result = await _bnf_check_one_series(series, db)
            total_found += len(result.get("volumes_found", []))
            total_assigned += result.get("assigned", 0)
        except Exception:
            pass
        if task_id in sched._running:
            sched._running[task_id]["progress"]["current"] = i + 1

    return {"series_checked": total, "volumes_found": total_found, "assigned": total_assigned}


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
