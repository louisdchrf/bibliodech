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


async def search_complete_volume_list(
    series_name: str,
    client: httpx.AsyncClient,
) -> list[dict]:
    """
    Cherche la liste complète des volumes d'une série via DDG.
    Stratégie en 3 requêtes :
      1. Liste des tomes + extraction des numéros individuels
      2. Nombre total de tomes
      3. Titres des tomes (enrichissement)
    Retourne [{position, title}].
    """
    text1 = await _ddg_query(f'"{series_name}" liste tomes bd série complet', client)
    await asyncio.sleep(1.0)
    text2 = await _ddg_query(f'"{series_name}" série nombre tomes total intégrale', client)
    full_text = text1 + " " + text2

    total = _extract_total_from_text(full_text)
    found_nums = _extract_volume_numbers(full_text)

    if total and found_nums:
        max_found = max(found_nums)
        if total >= max_found * 0.7:
            all_nums = set(float(i) for i in range(1, total + 1))
        else:
            all_nums = found_nums
    elif total:
        all_nums = set(float(i) for i in range(1, total + 1))
    else:
        all_nums = found_nums

    if not all_nums:
        return []

    await asyncio.sleep(1.0)
    text3 = await _ddg_query(f'"{series_name}" tome 1 2 3 titre liste', client)
    title_map: dict[float, str] = {}
    for m in re.finditer(
        r'(?:tome|vol\.?)\s+(\d+)\s*[-–:]\s*([A-ZÀ-ÿ][^,.\n!?]{3,60})',
        text3, re.IGNORECASE
    ):
        pos = float(m.group(1))
        title = m.group(2).strip()
        if pos in all_nums and pos not in title_map:
            title_map[pos] = title

    return [
        {"position": p, "title": title_map.get(p)}
        for p in sorted(all_nums)
        if 1 <= p <= 200
    ]




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
