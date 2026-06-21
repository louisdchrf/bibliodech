"""
Détection de série et recherche de volumes via DuckDuckGo + scraping Babelio.
Pas d'API key, pas de dépendance supplémentaire.
"""
import asyncio
import re
from collections import Counter

import httpx

_DDG_URL = "https://html.duckduckgo.com/html/"
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.5",
}

_SERIES_PATTERNS = [
    r'\btome\s+\d+\s+de\s+(?:la\s+série\s+)?([A-ZÀ-ÿ][^.!?,\n]{2,50})',
    r'\bla\s+série\s+[«""]?\s*([A-ZÀ-ÿ][^.!?,«""\n]{2,50})',
    r'\bsérie\s*:?\s+[«""]?\s*([A-ZÀ-ÿ][^.!?,«""\n]{2,50})',
    r'\(([A-ZÀ-ÿ][^()]{2,50}),\s*(?:tome|vol\.?|#|t\.)\s*\d',
    r'([A-ZÀ-ÿ][^–-]{3,50})\s*[-–]\s*(?:tome|vol\.?|t\.)\s*\d',
    r'\bcollection\s+[«""]?\s*([A-ZÀ-ÿ][^.!?,«""\n]{2,50})',
    r'([A-ZÀ-ÿ][^,\n]{3,50}),\s*(?:tome|vol\.?|t\.)\s*\d',
]

_NOISE_WORDS = {
    "le", "la", "les", "un", "une", "des", "de", "du", "en", "et", "ou",
    "par", "pour", "sur", "avec", "sans", "isbn", "livres", "livre",
    "amazon", "fnac", "decitre", "cultura", "babelio", "goodreads",
}

_VOL_PATTERNS = [
    r'\btome\s+(\d+(?:\.\d+)?)',
    r'\bvol(?:ume)?\.?\s*(\d+(?:\.\d+)?)',
    r'\bt\.?\s*(\d+(?:\.\d+)?)\b',
    r'#\s*(\d+(?:\.\d+)?)',
]


def _clean_html(text: str) -> str:
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'&amp;', '&', text)
    text = re.sub(r'&quot;', '"', text)
    text = re.sub(r'&[a-z]{2,6};', ' ', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def _extract_series_from_text(text: str, book_title: str) -> str | None:
    candidates: list[str] = []
    for pat in _SERIES_PATTERNS:
        for m in re.finditer(pat, text, re.IGNORECASE | re.MULTILINE):
            raw = m.group(1).strip().rstrip('.,;:– ')
            raw = re.sub(r'\s+', ' ', raw)
            if len(raw) < 3 or len(raw) > 60:
                continue
            if raw.lower() == book_title.lower():
                continue
            if raw.lower() in _NOISE_WORDS:
                continue
            candidates.append(raw)
    if not candidates:
        return None
    counter = Counter(c.lower() for c in candidates)
    best_lower = counter.most_common(1)[0][0]
    return next((c for c in candidates if c.lower() == best_lower), candidates[0])


async def _ddg_query(query: str, client: httpx.AsyncClient) -> str:
    """Lance une requête DDG, retourne le texte brut des titres+snippets."""
    try:
        r = await client.get(
            _DDG_URL,
            params={"q": query, "kl": "fr-fr", "kp": "-1"},
            headers=_HEADERS,
            timeout=12,
            follow_redirects=True,
        )
        if r.status_code != 200:
            return ""
    except Exception:
        return ""
    html = r.text
    raw_titles   = re.findall(r'class="result__a"[^>]*>(.*?)</a>', html, re.S)
    raw_snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', html, re.S)
    return " ".join(_clean_html(t) for t in raw_titles + raw_snippets)


def _extract_volume_numbers(text: str) -> set[float]:
    nums: set[float] = set()
    for pat in _VOL_PATTERNS:
        for m in re.finditer(pat, text, re.IGNORECASE):
            try:
                nums.add(float(m.group(1)))
            except ValueError:
                pass
    return nums


def _extract_total_from_text(text: str) -> int | None:
    """Cherche des formulations du type 'série en X tomes', 'X albums', etc."""
    patterns = [
        r's[eé]rie\s+(?:en\s+)?(\d+)\s+(?:tomes?|volumes?|albums?)',
        r'(\d+)\s+(?:tomes?|volumes?|albums?)\s+(?:au\s+total|en\s+tout|parus?)',
        r'intégrale\s+(?:en\s+)?(\d+)\s+(?:tomes?|volumes?)',
        r'(?:comporte|contient|comprend)\s+(\d+)\s+(?:tomes?|volumes?)',
        r'(\d+)\s+(?:tomes?|volumes?)\s+(?:dans\s+la\s+s[eé]rie|de\s+la\s+s[eé]rie)',
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            try:
                n = int(m.group(1))
                if 2 <= n <= 200:
                    return n
            except ValueError:
                pass
    return None


_WP_HEADERS = {"User-Agent": "Bibliodech/1.0 (ldecherf1@gmail.com)"}
_WP_API = "https://fr.wikipedia.org/w/api.php"


async def _wikipedia_volume_list(series_name: str, client: httpx.AsyncClient) -> list[dict] | None:
    """
    Cherche la série sur Wikipedia FR, extrait le total d'albums depuis l'infobox
    et les titres depuis la liste des albums.
    Retourne [{position, title}] ou None si rien trouvé.
    """
    # 1. Chercher le titre de l'article
    try:
        r = await client.get(_WP_API, params={
            "action": "query", "list": "search",
            "srsearch": f"{series_name} bande dessinée",
            "srlimit": 3, "format": "json", "utf8": 1,
        }, headers=_WP_HEADERS, timeout=10)
        hits = r.json().get("query", {}).get("search", [])
    except Exception:
        return None

    if not hits:
        return None

    # Prendre le premier hit dont le titre ressemble au nom de la série
    article_title = None
    for hit in hits:
        t = hit["title"].lower()
        if any(w in t for w in series_name.lower().split()[:2]):
            article_title = hit["title"]
            break
    if not article_title:
        article_title = hits[0]["title"]

    # 2. Récupérer le wikitext
    try:
        r2 = await client.get(_WP_API, params={
            "action": "query", "titles": article_title,
            "prop": "revisions", "rvprop": "content", "rvslots": "main",
            "format": "json", "utf8": 1,
        }, headers=_WP_HEADERS, timeout=10)
        pages = r2.json().get("query", {}).get("pages", {})
        content = next(iter(pages.values())) \
            .get("revisions", [{}])[0] \
            .get("slots", {}).get("main", {}).get("*", "")
    except Exception:
        return None

    if not content:
        return None

    # 3. Total depuis l'infobox : | albums = 17
    total: int | None = None
    m = re.search(r"\|\s*nombre\s+d[’'\"]albums\s*=\s*(\d+)", content) \
        or re.search(r'\|\s*(?:nb_)?albums\s*=\s*(\d+)', content)
    if m:
        total = int(m.group(1))

    if not total:
        total = _extract_total_from_text(content)

    # 4. Titres des tomes depuis la liste des albums dans le wikitext
    # Patterns : # ''Titre'' (année) ou | titre = Titre
    title_map: dict[int, str] = {}

    # Pattern liste numérotée wiki : # ''Titre''
    for i, m in enumerate(re.finditer(r'^\s*#\s*(?:\'\'\'?)?([^\'#\n\[]{3,60})(?:\'\'\'?)?', content, re.MULTILINE), start=1):
        raw = m.group(1).strip().rstrip("'").strip()
        if raw and not raw.startswith('|') and len(raw) > 2:
            title_map[i] = raw

    # Pattern tableau : | titre = X ou | Titre || ...
    for m in re.finditer(r'\|\s*(?:titre\d*\s*=\s*)([^\|\n\]]{3,60})', content, re.IGNORECASE):
        raw = m.group(1).strip()
        if raw and len(raw) > 2:
            pos = len(title_map) + 1
            if pos not in title_map:
                title_map[pos] = raw

    if not total and not title_map:
        return None

    # Si on a un total mais peu de titres, compléter avec positions sans titre
    if total:
        return [
            {"position": float(i), "title": title_map.get(i)}
            for i in range(1, total + 1)
        ]

    return [
        {"position": float(pos), "title": title}
        for pos, title in sorted(title_map.items())
    ]


async def search_complete_volume_list(
    series_name: str,
    client: httpx.AsyncClient,
) -> list[dict]:
    """
    Cherche la liste complète des volumes d'une série.
    Stratégie : Wikipedia FR d'abord, puis DDG en fallback.
    Retourne [{position, title}].
    """
    # Étape 1 : Wikipedia
    wp_result = await _wikipedia_volume_list(series_name, client)
    if wp_result:
        return wp_result

    # Étape 2 : DDG fallback
    await asyncio.sleep(0.5)
    text1 = await _ddg_query(f'"{series_name}" liste tomes bd série complet', client)
    await asyncio.sleep(1.0)
    text2 = await _ddg_query(f'"{series_name}" série nombre tomes total intégrale', client)
    full_text = text1 + " " + text2

    total = _extract_total_from_text(full_text)
    found_nums = _extract_volume_numbers(full_text)

    if total and found_nums:
        max_found = max(found_nums)
        all_nums = set(float(i) for i in range(1, total + 1)) if total >= max_found * 0.7 else found_nums
    elif total:
        all_nums = set(float(i) for i in range(1, total + 1))
    else:
        all_nums = found_nums

    return [{"position": p, "title": None} for p in sorted(all_nums) if 1 <= p <= 200]




# ── Détection de série ────────────────────────────────────────────────────────

async def search_series_ddg(
    title: str,
    authors: list[str],
    client: httpx.AsyncClient,
) -> str | None:
    author = authors[0] if authors else ""

    text = await _ddg_query(f'"{title}" "{author}" série tome', client)
    result = _extract_series_from_text(text, title) if text.strip() else None
    if result:
        return result

    await asyncio.sleep(0.8)
    text = await _ddg_query(f'{title} {author} série bd livre', client)
    return _extract_series_from_text(text, title) if text.strip() else None
