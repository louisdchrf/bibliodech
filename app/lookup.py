import html
import re
import sys
import json
import time
import httpx
import xml.etree.ElementTree as ET

TIMEOUT = 3.0

_TAG_RE = re.compile(r'<[^>]+>')

def _clean_description(text: str | None) -> str | None:
    if not text:
        return None
    text = _TAG_RE.sub(' ', text)          # strip balises HTML
    text = html.unescape(text)             # &amp; → & etc.
    text = re.sub(r'\s+', ' ', text).strip()
    return text or None

# Mise en veille de Google Books après un 429 (évite d'attendre un timeout inutile)
_google_blocked_until: float = 0.0
_GOOGLE_BACKOFF = 300  # 5 minutes

# Chaque pattern capture le numéro en groupe 1 ET le préfixe (nom de série candidat) en groupe 2
_POS_PATTERNS_WITH_PREFIX = [
    # "Lucky Luke T.3" / "XIII T3 - Le titre"
    re.compile(r'^(.+?)\s*[-–,\s]?\bT\.?\s*(\d+(?:\.\d+)?)\b', re.IGNORECASE),
    # "Blake et Mortimer, tome 5" / "Astérix tome 3"
    re.compile(r'^(.+?)\s*[,\s]+\b(?:tome|vol\.?|volume)\s*(\d+(?:\.\d+)?)\b', re.IGNORECASE),
    # "Série - Tome 2 - Titre"
    re.compile(r'^(.+?)\s*[-–]\s*(?:tome|t\.?|vol\.?)\s*(\d+(?:\.\d+)?)\b', re.IGNORECASE),
]
# Fallback sans préfixe (pas de nom de série extractible)
_POS_FALLBACK = [
    re.compile(r'\((\d+(?:\.\d+)?)\)\s*$'),
    re.compile(r',\s*(\d+(?:\.\d+)?)\s*$'),
]

_NS = {
    "srw": "http://www.loc.gov/zing/srw/",
    "dc":  "http://purl.org/dc/elements/1.1/",
    "oai_dc": "http://www.openarchives.org/OAI/2.0/oai_dc/",
}


def _extract_series_and_position(title: str, subtitle: str | None) -> tuple[str | None, float | None]:
    """
    Retourne (series_name, position) extraits du titre.
    Les patterns avec préfixe permettent d'obtenir le nom de série en même temps que le numéro.
    Ex: "Lucky Luke T.3 - Le titre" → ("Lucky Luke", 3.0)
    """
    text = title  # on cherche d'abord dans le titre seul pour le nom de série

    for pat in _POS_PATTERNS_WITH_PREFIX:
        m = pat.match(text)
        if m:
            prefix = m.group(1).strip().rstrip(',-: ').strip()
            try:
                pos = float(m.group(2))
            except ValueError:
                continue
            series = prefix if len(prefix) >= 2 else None
            return series, pos

    # Fallback : numéro seul, pas de nom de série extractible
    full = f"{title} {subtitle or ''}"
    for pat in _POS_FALLBACK:
        m = pat.search(full)
        if m:
            try:
                return None, float(m.group(1))
            except ValueError:
                pass

    return None, None


def _extract_series_position(title: str, subtitle: str | None) -> float | None:
    _, pos = _extract_series_and_position(title, subtitle)
    return pos


def _extract_series_from_title(title: str, subtitle: str | None) -> str | None:
    series, _ = _extract_series_and_position(title, subtitle)
    return series


def _normalize_isbn(isbn: str) -> str:
    return re.sub(r"[\s\-]", "", isbn)


def _isbn10_check(digits: str) -> str:
    s = sum((10 - i) * int(d) for i, d in enumerate(digits[:9]))
    r = (11 - s % 11) % 11
    return "X" if r == 10 else str(r)


def _isbn13_check(digits: str) -> str:
    s = sum(int(d) * (1 if i % 2 == 0 else 3) for i, d in enumerate(digits[:12]))
    return str((10 - s % 10) % 10)


def classify_isbn(isbn: str) -> dict:
    """
    Retourne des informations sur le format du code et les variantes à tester.
    - is_isbn: True si c'est un ISBN-13 standard (978/979) ou ISBN-10 valide
    - variants: liste d'ISBN à tenter en lookup (original + conversions)
    - warning: message si le code n'est pas un ISBN standard
    """
    raw = _normalize_isbn(isbn)
    variants = [raw]
    warning = None
    is_isbn = False

    if len(raw) == 13:
        if raw.startswith(("978", "979")):
            is_isbn = True
            # Dériver ISBN-10 depuis ISBN-13 (seulement pour préfixe 978)
            if raw.startswith("978"):
                body = raw[3:12]
                isbn10 = body + _isbn10_check(body)
                variants.append(isbn10)
        else:
            warning = f"Ce code ({raw}) n'est pas un ISBN standard (préfixe {raw[:3]}, attendu 978/979). La recherche peut être limitée."
            # Tenter quand même une variante avec préfixe 978
            body = raw[3:12]
            alt = "978" + body + _isbn13_check("978" + body)
            if alt != raw:
                variants.append(alt)

    elif len(raw) == 10:
        is_isbn = True
        # Convertir ISBN-10 → ISBN-13
        body = "978" + raw[:9]
        isbn13 = body + _isbn13_check(body)
        variants.append(isbn13)

    return {"is_isbn": is_isbn, "variants": variants, "warning": warning}


# ── Open Library search (fallback large) ────────────────────────────────────

async def _lookup_openlibrary_search(client: httpx.AsyncClient, isbn: str) -> dict | None:
    """Fallback via l'endpoint /search.json d'Open Library, plus large que l'API /api/books."""
    try:
        resp = await client.get(f"https://openlibrary.org/search.json?isbn={isbn}&limit=1")
        resp.raise_for_status()
        data = resp.json()
        docs = data.get("docs", [])
        if not docs:
            return None
        doc = docs[0]
        raw_title = doc.get("title", "")
        if not raw_title:
            return None

        # OL embed souvent "Série - Tome N - Titre réel" ou "Série, tome N : Titre"
        # Extraire série + position, puis isoler le vrai titre
        series_name, series_position = _extract_series_and_position(raw_title, None)
        title = raw_title
        if series_name and series_position is not None:
            # Retirer le préfixe "Série - Tome N - " ou "Série, tome N : "
            cleaned = re.sub(
                r'^.+?(?:[-–—]|,)\s*[Tt]ome\s*[\d.]+\s*(?:[-–—:]|$)\s*',
                '', raw_title
            ).strip()
            if cleaned and cleaned != raw_title:
                title = cleaned
        # OL stocke aussi la série dans le champ "series"
        ol_series = (doc.get("series") or [None])[0]
        if ol_series and not series_name:
            series_name = ol_series

        authors = doc.get("author_name") or []
        cover_id = doc.get("cover_i")
        cover_url = f"https://covers.openlibrary.org/b/id/{cover_id}-L.jpg" if cover_id else None
        olid = (doc.get("key") or "").replace("/works/", "")
        return {
            "title": title,
            "subtitle": None,
            "authors": authors,
            "publisher": (doc.get("publisher") or [None])[0],
            "publish_date": str(doc.get("first_publish_year", "")) or None,
            "cover_url": cover_url,
            "description": None,
            "page_count": doc.get("number_of_pages_median"),
            "language": (doc.get("language") or [None])[0],
            "source": "openlibrary_search",
            "work_key": olid or None,
            "series_name": series_name,
            "series_position": series_position,
        }
    except Exception as e:
        print(f"[lookup] OL search error: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        return None


# ── Google Books ──────────────────────────────────────────────────────────────

async def _lookup_google(client: httpx.AsyncClient, isbn: str, api_key: str = "") -> dict | None:
    global _google_blocked_until
    if time.time() < _google_blocked_until:
        return None  # en veille après un 429

    url = f"https://www.googleapis.com/books/v1/volumes?q=isbn:{isbn}"
    if api_key:
        url += f"&key={api_key}"
    try:
        resp = await client.get(url)
        if resp.status_code == 429:
            _google_blocked_until = time.time() + _GOOGLE_BACKOFF
            print(f"[lookup] Google Books 429 — pause {_GOOGLE_BACKOFF}s", file=sys.stderr, flush=True)
            return None
        resp.raise_for_status()
        data = json.loads(resp.content.decode("utf-8"))
        items = data.get("items", [])
        if not items:
            return None
        info = items[0].get("volumeInfo", {})
        title = info.get("title", "")
        subtitle = info.get("subtitle")
        image_links = info.get("imageLinks", {})
        cover_url = (
            image_links.get("extraLarge")
            or image_links.get("large")
            or image_links.get("medium")
            or image_links.get("thumbnail")
        )
        if cover_url:
            cover_url = cover_url.replace("http://", "https://")
        # Google Books seriesInfo (pas toujours présent)
        series_name = None
        series_position = _extract_series_position(title, subtitle)
        series_info = info.get("seriesInfo") or {}
        if series_info.get("shortSeriesBookTitle"):
            series_name = series_info["shortSeriesBookTitle"]
        if series_info.get("bookDisplayNumber"):
            try:
                series_position = float(series_info["bookDisplayNumber"])
            except (ValueError, TypeError):
                pass

        return {
            "title": title,
            "subtitle": subtitle,
            "authors": info.get("authors", []),
            "publisher": info.get("publisher"),
            "publish_date": info.get("publishedDate"),
            "cover_url": cover_url,
            "description": info.get("description"),
            "page_count": info.get("pageCount"),
            "language": info.get("language"),
            "source": "googlebooks",
            "work_key": None,
            "series_name": series_name,
            "series_position": series_position,
        }
    except Exception as e:
        print(f"[lookup] Google Books error: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        return None


# ── Open Library ──────────────────────────────────────────────────────────────

async def _lookup_openlibrary(client: httpx.AsyncClient, isbn: str) -> dict | None:
    url = f"https://openlibrary.org/api/books?bibkeys=ISBN:{isbn}&format=json&jscmd=data"
    try:
        resp = await client.get(url)
        resp.raise_for_status()
        data = json.loads(resp.content.decode("utf-8"))
        key = f"ISBN:{isbn}"
        if key not in data:
            return None
        book_data = data[key]
        title = book_data.get("title", "")
        subtitle = book_data.get("subtitle")
        authors = [a["name"] for a in book_data.get("authors", [])]
        publishers = book_data.get("publishers", [])
        publisher = publishers[0]["name"] if publishers else None
        langs = book_data.get("languages", [])
        language = langs[0].get("key", "").replace("/languages/", "") if langs else None
        covers = book_data.get("cover")
        cover_url = None
        if covers:
            cover_url = covers.get("large") or covers.get("medium") or covers.get("small")
        description = None
        desc = book_data.get("description")
        if isinstance(desc, dict):
            description = desc.get("value")
        elif isinstance(desc, str):
            description = desc
        works = book_data.get("works", [])
        work_key = works[0].get("key", "").replace("/works/", "") if works else None
        series_name = None
        series_position = _extract_series_position(title, subtitle)

        if work_key:
            try:
                wr = await client.get(f"https://openlibrary.org/works/{work_key}.json")
                wr.raise_for_status()
                wd = json.loads(wr.content.decode("utf-8"))
                sf = wd.get("series")
                if sf and isinstance(sf, list) and sf:
                    series_name = sf[0]
                if not description:
                    d = wd.get("description")
                    if isinstance(d, dict):
                        description = d.get("value")
                    elif isinstance(d, str):
                        description = d
            except Exception as e:
                print(f"[lookup] OL work fetch error: {type(e).__name__}: {e}", file=sys.stderr, flush=True)

        return {
            "title": title,
            "subtitle": subtitle,
            "authors": authors,
            "publisher": publisher,
            "publish_date": book_data.get("publish_date"),
            "cover_url": cover_url,
            "description": description,
            "page_count": book_data.get("number_of_pages"),
            "language": language,
            "source": "openlibrary",
            "work_key": work_key,
            "series_name": series_name,
            "series_position": series_position,
        }
    except Exception as e:
        print(f"[lookup] Open Library error: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        return None


# ── BNF (Bibliothèque nationale de France) ───────────────────────────────────

def _bnf_text(record: ET.Element, tag: str) -> str | None:
    el = record.find(f".//{{{_NS['dc']}}}{tag}")
    return el.text.strip() if el is not None and el.text else None


def _bnf_texts(record: ET.Element, tag: str) -> list[str]:
    return [
        el.text.strip()
        for el in record.findall(f".//{{{_NS['dc']}}}{tag}")
        if el.text
    ]


# Phrases de rôle BNF après le prénom (UNIMARC)
_BNF_ROLE_PHRASES = re.compile(
    r'(?:Auteur du texte|Autrice du texte|Illustrateur|Illustratrice'
    r'|Traducteur|Traductrice|Éditeur scientifique|Editeur scientifique'
    r'|Directeur de publication|Directrice de publication'
    r'|Préfacier|Photographe|Compositeur|Interprète|Adaptateur|Compilateur)',
    re.IGNORECASE,
)
# Entrée fantôme = que du rôle, rien d'autre
_BNF_ROLE_ONLY = re.compile(
    r'^(?:Auteur du texte|Autrice du texte)(?:\s*/\s*(?:Auteur du texte|Autrice du texte))*$',
    re.IGNORECASE,
)

def _bnf_clean_creators(raw: str) -> list[str]:
    """Parse une chaîne BNF dc:creator et retourne une liste de noms normalisés."""
    # Supprimer les dates entre parenthèses
    s = re.sub(r'\s*\(\d{4}[^)]*\)', '', raw).strip()

    # Entrée fantôme : que des rôles
    if _BNF_ROLE_ONLY.match(s):
        return []

    # Format mention de responsabilité BD : "[dessin de] X ; [scénario de] Y"
    # Aussi le cas sans crochets : "X ; Y"
    if ';' in s:
        results = []
        for part in s.split(';'):
            part = part.strip()
            # Supprimer "[rôle]" en tête
            part = re.sub(r'^\[.*?\]\s*', '', part).strip()
            if part:
                results.extend(_bnf_clean_creators(part))
        return results

    # Format UNIMARC : "Prénom. <Rôle> Nom"
    m = re.match(r'^(.+?)\s*\.\s*' + _BNF_ROLE_PHRASES.pattern + r'\s+(.+)$', s, re.IGNORECASE)
    if m:
        prenom, nom = m.group(1).strip(), m.group(2).strip()
        return [f"{prenom} {nom}"] if prenom and nom else [prenom or nom]

    # Institution avec suffixe rôle : "Musée du Louvre (Paris). Auteur du texte"
    m2 = re.match(r'^(.+?)\s*\.\s*' + _BNF_ROLE_PHRASES.pattern + r'\s*$', s, re.IGNORECASE)
    if m2:
        return [m2.group(1).strip()]

    # Format ISBD multi-auteurs : "Nom1, Prénom1, Nom2, Prénom2, ..."
    # Détecter un nombre pair de tokens séparés par des virgules
    parts = [p.strip() for p in s.split(',')]
    if len(parts) >= 2 and len(parts) % 2 == 0 and all(parts):
        # Vérifier heuristiquement que c'est bien des paires Nom/Prénom
        # (les tokens pairs ressemblent à des noms, les impairs à des prénoms)
        paired = [f"{parts[i+1]} {parts[i]}" for i in range(0, len(parts), 2)]
        return paired

    # Format ISBD simple : "Nom, Prénom"
    if len(parts) == 2 and parts[1]:
        return [f"{parts[1]} {parts[0]}"]

    return [s]


def _bnf_clean_creator(raw: str) -> str:
    """Compat : retourne le premier auteur nettoyé (usage legacy)."""
    results = _bnf_clean_creators(raw)
    return results[0] if results else ''


async def _lookup_bnf(client: httpx.AsyncClient, isbn: str) -> dict | None:
    url = (
        "https://catalogue.bnf.fr/api/SRU"
        f'?version=1.2&operation=searchRetrieve'
        f'&query=bib.isbn%20adj%20%22{isbn}%22'
        f'&recordSchema=dublincore&maximumRecords=1'
    )
    try:
        try:
            resp = await client.get(url)
        except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.ConnectError):
            # Retry une fois sur erreur réseau transitoire
            resp = await client.get(url)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
        records = root.findall(f".//{{{_NS['srw']}}}record")
        if not records:
            return None
        rec = records[0]
        title_raw = _bnf_text(rec, "title")
        if not title_raw:
            return None
        # BNF sometimes includes subtitle after " / " or " : "
        title, subtitle = title_raw, None
        for sep in [" / ", " : "]:
            if sep in title_raw:
                parts = title_raw.split(sep, 1)
                title, subtitle = parts[0].strip(), parts[1].strip()
                break

        creators = _bnf_texts(rec, "creator")
        authors = []
        for c in creators:
            if c:
                for a in _bnf_clean_creators(c):
                    if a and a not in authors:
                        authors.append(a)

        # Sous-titre : ignorer la mention de responsabilité BNF ("[dessin de] X ; [scénario de] Y")
        if subtitle and (';' in subtitle or re.search(r'\[.+\]', subtitle)):
            subtitle = None

        publisher_raw = _bnf_text(rec, "publisher")
        # BNF publisher : "Éditeur (Ville)" → garder juste "Éditeur"
        if publisher_raw:
            publisher_raw = re.sub(r'\s*\([^)]+\)\s*$', '', publisher_raw).strip()

        date_raw = _bnf_text(rec, "date")
        language = _bnf_text(rec, "language")
        description = _bnf_text(rec, "description")

        # BNF cover via Open Library covers API (fallback)
        cover_url = f"https://covers.openlibrary.org/b/isbn/{isbn}-L.jpg"

        # BNF : série dans dc:relation (ex: "Collection Folio, 1234") ou titre lui-même
        series_name = None
        series_position = _extract_series_position(title, subtitle)
        for rel in _bnf_texts(rec, "relation"):
            # Filtrer les relations qui ressemblent à une collection/série
            rel_clean = rel.strip()
            if rel_clean and not rel_clean.startswith("http") and len(rel_clean) < 120:
                # Garder uniquement si c'est pas un ISBN/URL/notice liée
                if not re.match(r'^[0-9\-X ]+$', rel_clean):
                    series_name = rel_clean
                    break

        return {
            "title": title,
            "subtitle": subtitle,
            "authors": authors,
            "publisher": publisher_raw,
            "publish_date": date_raw,
            "cover_url": cover_url,
            "description": description,
            "page_count": None,
            "language": language,
            "source": "bnf",
            "work_key": None,
            "series_name": series_name,
            "series_position": series_position,
        }
    except Exception as e:
        print(f"[lookup] BNF error: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        return None


# ── SUDOC (Système Universitaire de Documentation) ───────────────────────────

def _unimarc_subfield(record: ET.Element, tag: str, code: str) -> str | None:
    el = record.find(f".//datafield[@tag='{tag}']/subfield[@code='{code}']")
    return el.text.strip() if el is not None and el.text else None

def _unimarc_subfields(record: ET.Element, tag: str, code: str) -> list[str]:
    return [
        el.text.strip()
        for el in record.findall(f".//datafield[@tag='{tag}']/subfield[@code='{code}']")
        if el.text
    ]


async def _lookup_sudoc(client: httpx.AsyncClient, isbn: str) -> dict | None:
    try:
        # Étape 1 : ISBN → PPN
        ppn_resp = await client.get(f"https://www.sudoc.fr/services/isbn2ppn/{isbn}")
        ppn_resp.raise_for_status()
        ppn_root = ET.fromstring(ppn_resp.content)
        ppn_el = ppn_root.find(".//ppn")
        if ppn_el is None or not ppn_el.text:
            return None
        ppn = ppn_el.text.strip()

        # Étape 2 : PPN → notice UNIMARC
        # Essayer d'abord l'URL directe .xml, puis le service /ppn/
        rec_resp = None
        for ppn_url in (
            f"https://www.sudoc.fr/{ppn}.xml",
            f"https://www.sudoc.fr/services/ppn/{ppn}",
        ):
            try:
                r = await client.get(ppn_url, headers={"Accept": "text/xml, application/xml"})
                if r.status_code == 200:
                    rec_resp = r
                    break
            except Exception:
                continue
        if rec_resp is None:
            print(f"[lookup] SUDOC: PPN {ppn} introuvable (toutes URLs)", file=sys.stderr, flush=True)
            return None
        root = ET.fromstring(rec_resp.content)
        record = root.find(".//record")
        if record is None:
            return None

        # Titre (200 $a) + sous-titre (200 $e) + numéro de tome (200 $h)
        title = _unimarc_subfield(record, "200", "a") or ""
        subtitle = _unimarc_subfield(record, "200", "e")
        vol_in_title = _unimarc_subfield(record, "200", "h")  # "3" ou "tome 3"
        if not title:
            return None

        # Série (225 $a) + numéro dans la série (225 $v) — le champ clé du SUDOC
        series_name = _unimarc_subfield(record, "225", "a")
        series_vol = _unimarc_subfield(record, "225", "v") or vol_in_title
        series_position = None
        if series_vol:
            m = re.search(r'(\d+(?:\.\d+)?)', series_vol)
            if m:
                try:
                    series_position = float(m.group(1))
                except ValueError:
                    pass

        # Auteurs : 700 $a (nom) + 700 $b (prénom), puis 701 pour co-auteurs
        authors = []
        for tag in ("700", "701", "702"):
            for df in record.findall(f".//datafield[@tag='{tag}']"):
                nom = (df.findtext("subfield[@code='a']") or "").strip()
                prenom = (df.findtext("subfield[@code='b']") or "").strip()
                if nom:
                    authors.append(f"{prenom} {nom}".strip() if prenom else nom)

        # Éditeur (210 $c) + date (210 $d)
        publisher = _unimarc_subfield(record, "210", "c")
        publish_date = _unimarc_subfield(record, "210", "d")

        # Langue (101 $a)
        language = _unimarc_subfield(record, "101", "a")

        # Nombre de pages (215 $a) — souvent "123 p."
        pages_raw = _unimarc_subfield(record, "215", "a")
        page_count = None
        if pages_raw:
            m = re.search(r'(\d+)', pages_raw)
            if m:
                try:
                    page_count = int(m.group(1))
                except ValueError:
                    pass

        # Résumé (330 $a)
        description = _unimarc_subfield(record, "330", "a")

        print(f"[lookup] SUDOC: {isbn} → {title!r} série={series_name!r} pos={series_position}", file=sys.stderr, flush=True)

        return {
            "title": title,
            "subtitle": subtitle,
            "authors": authors,
            "publisher": publisher,
            "publish_date": publish_date,
            "cover_url": f"https://covers.openlibrary.org/b/isbn/{isbn}-L.jpg",
            "description": description,
            "page_count": page_count,
            "language": language,
            "source": "sudoc",
            "work_key": None,
            "series_name": series_name,
            "series_position": series_position or _extract_series_position(title, subtitle),
        }
    except Exception as e:
        print(f"[lookup] SUDOC error: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        return None


# ── Decitre (scraping JSON-LD) ───────────────────────────────────────────────

_DECITRE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "fr-FR,fr;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
_JSONLD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)


async def _lookup_decitre(client: httpx.AsyncClient, isbn: str) -> dict | None:
    try:
        resp = await client.get(
            f"https://www.decitre.fr/livres/{isbn}.html",
            headers=_DECITRE_HEADERS,
            follow_redirects=True,
        )
        # 404 = ISBN inconnu, 301 sans corps = redirect vers recherche → pas trouvé
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            return None

        # Chercher tous les blocs JSON-LD et prendre celui de type Book
        book_data = None
        for match in _JSONLD_RE.finditer(resp.text):
            try:
                obj = json.loads(match.group(1))
                # Peut être une liste ou un objet unique
                items = obj if isinstance(obj, list) else [obj]
                for item in items:
                    if item.get("@type") in ("Book", "Product") and item.get("name"):
                        book_data = item
                        break
            except (json.JSONDecodeError, AttributeError):
                continue
            if book_data:
                break

        if not book_data:
            return None

        title = book_data.get("name", "").strip()
        if not title:
            return None

        # Auteurs — peut être str, dict ou liste
        authors = []
        raw_authors = book_data.get("author") or book_data.get("creator") or []
        if isinstance(raw_authors, str):
            authors = [raw_authors.strip()]
        elif isinstance(raw_authors, dict):
            authors = [raw_authors.get("name", "").strip()]
        elif isinstance(raw_authors, list):
            for a in raw_authors:
                name = a.get("name", "").strip() if isinstance(a, dict) else str(a).strip()
                if name:
                    authors.append(name)

        publisher = None
        pub = book_data.get("publisher")
        if isinstance(pub, dict):
            publisher = pub.get("name")
        elif isinstance(pub, str):
            publisher = pub

        cover_url = book_data.get("image") or None
        if isinstance(cover_url, list):
            cover_url = cover_url[0] if cover_url else None

        description = book_data.get("description") or None

        page_count = None
        try:
            page_count = int(book_data.get("numberOfPages") or 0) or None
        except (ValueError, TypeError):
            pass

        publish_date = book_data.get("datePublished") or book_data.get("copyrightYear") or None
        if publish_date:
            publish_date = str(publish_date)[:10]  # garder juste YYYY ou YYYY-MM-DD

        language = None
        lang = book_data.get("inLanguage")
        if isinstance(lang, str):
            language = lang
        elif isinstance(lang, dict):
            language = lang.get("name") or lang.get("alternateName")

        # Série : isPartOf dans le JSON-LD (source la plus fiable pour le fonds FR)
        series_name = None
        series_position = None
        is_part_of = book_data.get("isPartOf")
        if isinstance(is_part_of, list) and is_part_of:
            is_part_of = is_part_of[0]
        if isinstance(is_part_of, dict):
            series_name = is_part_of.get("name") or is_part_of.get("title")
            raw_pos = is_part_of.get("position") or is_part_of.get("volumeNumber")
            if raw_pos is not None:
                try:
                    series_position = float(raw_pos)
                except (ValueError, TypeError):
                    pass
        # Position peut aussi être directement sur le livre
        if series_position is None:
            raw_pos = book_data.get("position") or book_data.get("volumeNumber")
            if raw_pos is not None:
                try:
                    series_position = float(raw_pos)
                except (ValueError, TypeError):
                    pass
        # Fallback : heuristique titre
        if series_position is None:
            series_position = _extract_series_position(title, None)
        if series_name is None:
            series_name = _extract_series_from_title(title, None)

        print(f"[lookup] Decitre: {isbn} → {title!r} série={series_name!r} pos={series_position}", file=sys.stderr, flush=True)
        return {
            "title": title,
            "subtitle": None,
            "authors": authors,
            "publisher": publisher,
            "publish_date": publish_date,
            "cover_url": cover_url,
            "description": description,
            "page_count": page_count,
            "language": language,
            "source": "decitre",
            "work_key": None,
            "series_name": series_name,
            "series_position": series_position,
        }
    except Exception as e:
        print(f"[lookup] Decitre error: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        return None


# ── ISBNdb ───────────────────────────────────────────────────────────────────

async def _lookup_isbndb(client: httpx.AsyncClient, isbn: str, api_key: str) -> dict | None:
    try:
        resp = await client.get(
            f"https://api2.isbndb.com/book/{isbn}",
            headers={"Authorization": api_key},
        )
        if resp.status_code == 401:
            print("[lookup] ISBNdb: clé API invalide ou expirée", file=sys.stderr, flush=True)
            return None
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        data = json.loads(resp.content.decode("utf-8")).get("book", {})
        if not data:
            return None

        title = data.get("title") or data.get("title_long") or ""
        if not title:
            return None

        authors = data.get("authors") or []
        if isinstance(authors, str):
            authors = [authors]

        # ISBNdb retourne souvent "title_long" qui contient série + tome
        title_long = data.get("title_long") or title

        cover_url = data.get("image") or None
        page_count = None
        try:
            page_count = int(data.get("pages") or 0) or None
        except (ValueError, TypeError):
            pass

        print(f"[lookup] ISBNdb: {isbn} → {title!r}", file=sys.stderr, flush=True)
        return {
            "title": title,
            "subtitle": title_long if title_long != title else None,
            "authors": authors,
            "publisher": data.get("publisher") or None,
            "publish_date": data.get("date_published") or None,
            "cover_url": cover_url,
            "description": data.get("synopsis") or None,
            "page_count": page_count,
            "language": data.get("language") or None,
            "source": "isbndb",
            "work_key": None,
            "series_name": None,
            "series_position": _extract_series_position(title, title_long if title_long != title else None),
        }
    except Exception as e:
        print(f"[lookup] ISBNdb error: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        return None


# ── Aggregation ───────────────────────────────────────────────────────────────

def _merge(base: dict, extra: dict) -> dict:
    """Complète les champs manquants de base avec extra."""
    for key in ("title", "subtitle", "authors", "publisher", "publish_date",
                "cover_url", "description", "page_count", "language",
                "work_key", "series_name", "series_position"):
        if not base.get(key) and extra.get(key):
            base[key] = extra[key]
    return base


_SOURCE_FNS = {
    "googlebooks": _lookup_google,
    "openlibrary": _lookup_openlibrary,
    "bnf": _lookup_bnf,
    "sudoc": _lookup_sudoc,
    "decitre": _lookup_decitre,
    # isbndb géré séparément (nécessite api_key en paramètre)
}


async def lookup_isbn(isbn: str, db=None) -> dict | None:
    isbn = _normalize_isbn(isbn)
    info  = classify_isbn(isbn)
    variants = info["variants"]
    warning  = info["warning"]
    if warning:
        print(f"[lookup] warning: {warning}", file=sys.stderr, flush=True)
    print(f"[lookup] start: {isbn} variants={variants}", file=sys.stderr, flush=True)

    # Charger les préférences sources
    if db is not None:
        import app.settings as cfg
        sources_cfg = cfg.get(db, "lookup_sources")
        isbndb_key = cfg.get(db, "isbndb_api_key") or ""
        googlebooks_key = cfg.get(db, "googlebooks_api_key") or ""
    else:
        sources_cfg = None
        isbndb_key = ""
        googlebooks_key = ""

    if sources_cfg is None:
        sources_cfg = [
            {"id": "sudoc",       "enabled": True,  "timeout": 5},
            {"id": "bnf",         "enabled": True,  "timeout": 5},
            {"id": "decitre",     "enabled": True,  "timeout": 10},
            {"id": "googlebooks", "enabled": True,  "timeout": 5},
        ]

    active_cfgs = {s["id"]: s for s in sources_cfg if s.get("enabled", True)}

    import asyncio

    # Un seul client httpx avec timeout généreux — asyncio.wait_for gère le timeout par source
    MAX_TIMEOUT = max((s.get("timeout", 5) for s in active_cfgs.values()), default=10)

    async def _call_with_timeout(coro, timeout_s: float):
        try:
            return await asyncio.wait_for(coro, timeout=float(timeout_s))
        except asyncio.TimeoutError:
            return None

    # Chercher sur toutes les variantes ISBN (original + conversions)
    all_results: list[dict | None] = []
    async with httpx.AsyncClient(timeout=MAX_TIMEOUT + 2) as client:
        for variant in variants:
            tasks = {}
            for sid, scfg in active_cfgs.items():
                t = scfg.get("timeout", 5)
                if sid == "isbndb":
                    if isbndb_key:
                        tasks[sid] = _call_with_timeout(_lookup_isbndb(client, variant, isbndb_key), t)
                elif sid == "googlebooks":
                    tasks[sid] = _call_with_timeout(_lookup_google(client, variant, googlebooks_key), t)
                elif sid in _SOURCE_FNS:
                    tasks[sid] = _call_with_timeout(_SOURCE_FNS[sid](client, variant), t)
            results_list = await asyncio.gather(*tasks.values(), return_exceptions=True)
            variant_results: dict[str, dict | None] = {}
            for sid, res in zip(tasks.keys(), results_list):
                variant_results[sid] = res if isinstance(res, dict) else None
            all_results.append(variant_results)
            if any(v for v in variant_results.values()):
                break  # trouvé sur cette variante, pas besoin d'essayer les suivantes

        # Fusionner les résultats de toutes les variantes testées
        results: dict[str, dict | None] = {}
        for vr in all_results:
            for sid, val in vr.items():
                if val and not results.get(sid):
                    results[sid] = val

        # Open Library search (configurable, utilisé aussi en fallback si rien trouvé)
        ol_search_cfg = active_cfgs.get("openlibrary_search")
        if ol_search_cfg and (not any(results.values()) or True):
            t = ol_search_cfg.get("timeout", 8)
            for variant in variants:
                sol = await _call_with_timeout(_lookup_openlibrary_search(client, variant), t)
                if sol:
                    results["openlibrary_search"] = sol
                    break

    found = {k: v for k, v in results.items() if v}
    print(f"[lookup] sources found: {list(found.keys())}", file=sys.stderr, flush=True)

    if not found:
        print(f"[lookup] not found in any source: {isbn}", file=sys.stderr, flush=True)
        return None

    # Priorité dans l'ordre de sources_cfg (premier = priorité haute pour les métadonnées)
    ordered = [results.get(s["id"]) for s in sources_cfg if results.get(s.get("id", ""))]
    result = ordered[0]
    for other in ordered[1:]:
        result = _merge(result, other)

    # Priorité série : ordre configuré dans les paramètres
    for src in ordered:
        if src and src.get("series_name") and not result.get("series_name"):
            result["series_name"] = src["series_name"]
            print(f"[lookup] series from {src.get('source')}: {src['series_name']!r}", file=sys.stderr, flush=True)
        if src and src.get("series_position") and not result.get("series_position"):
            result["series_position"] = src["series_position"]
    if not result.get("series_name"):
        guessed = _extract_series_from_title(result.get("title", ""), result.get("subtitle"))
        if guessed:
            result["series_name"] = guessed
            print(f"[lookup] series guessed from title: {guessed!r}", file=sys.stderr, flush=True)

    result["description"] = _clean_description(result.get("description"))
    if warning:
        result["_ean_warning"] = warning

    # Stocker les résultats bruts par source (champs utiles seulement)
    _KEEP = ("title", "subtitle", "authors", "publisher", "publish_date",
             "language", "page_count", "series_name", "series_position", "source")
    result["_per_source"] = {
        sid: {k: v for k, v in src.items() if k in _KEEP}
        for sid, src in results.items() if src
    }

    print(f"[lookup] found: {isbn} → {result.get('title', '?')!r} series={result.get('series_name')!r} (source: {result.get('source')})", file=sys.stderr, flush=True)
    return result


async def debug_isbn(isbn: str) -> dict:
    isbn = _normalize_isbn(isbn)
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        import asyncio
        gb, ol, bnf, sudoc = await asyncio.gather(
            _lookup_google(client, isbn),
            _lookup_openlibrary(client, isbn),
            _lookup_bnf(client, isbn),
            _lookup_sudoc(client, isbn),
        )
    return {"isbn": isbn, "google_books": gb, "open_library": ol, "bnf": bnf, "sudoc": sudoc}
