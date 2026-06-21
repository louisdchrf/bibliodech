"""
Détection de série via recherche DuckDuckGo HTML.
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

# Patterns pour extraire un nom de série du texte brut
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


def _clean_html(text: str) -> str:
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'&amp;', '&', text)
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
    best_lower, _ = counter.most_common(1)[0]
    for c in candidates:
        if c.lower() == best_lower:
            return c
    return candidates[0]


async def _ddg_query(query: str, client: httpx.AsyncClient) -> str:
    """Lance une requête DDG et retourne le texte brut des titres+snippets."""
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


async def search_series_ddg(
    title: str,
    authors: list[str],
    client: httpx.AsyncClient,
) -> str | None:
    author = authors[0] if authors else ""

    # Stratégie 1 : requête stricte avec guillemets
    text = await _ddg_query(f'"{title}" "{author}" série tome', client)
    result = _extract_series_from_text(text, title) if text.strip() else None
    if result:
        return result

    # Stratégie 2 : sans guillemets (titres courts ou auteurs peu connus)
    await asyncio.sleep(0.8)
    text = await _ddg_query(f'{title} {author} série bd livre', client)
    return _extract_series_from_text(text, title) if text.strip() else None
