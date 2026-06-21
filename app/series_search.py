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


async def _ddg_find_url(query: str, client: httpx.AsyncClient, domain: str) -> str | None:
    """Cherche via DDG et retourne la première URL du domaine cible trouvée dans les résultats."""
    try:
        r = await client.get(
            _DDG_URL,
            params={"q": query, "kl": "fr-fr", "kp": "-1"},
            headers=_HEADERS,
            timeout=12,
            follow_redirects=True,
        )
        if r.status_code != 200:
            return None
    except Exception:
        return None

    # DDG encode les URLs en data-href ou href dans les liens de résultats
    urls = re.findall(r'href="(https?://[^"]*' + re.escape(domain) + r'[^"]*)"', r.text)
    for url in urls:
        if domain in url:
            return url
    return None


# ── Babelio ───────────────────────────────────────────────────────────────────

async def _fetch_babelio_volumes(url: str, client: httpx.AsyncClient) -> list[dict]:
    """
    Récupère la liste des volumes depuis une page série Babelio.
    Retourne [{position, title}].
    """
    try:
        r = await client.get(url, headers=_HEADERS, timeout=15, follow_redirects=True)
        if r.status_code != 200:
            return []
    except Exception:
        return []

    html = r.text
    volumes = []

    # Les livres d'une série Babelio sont dans des blocs avec titre et numéro de tome
    # Pattern typique : <a ...>Titre - Tome N - Sous-titre</a> ou "(Série, #N)"
    book_blocks = re.findall(
        r'<a[^>]+href="[^"]*babelio\.com/livres/[^"]*"[^>]*>(.*?)</a>',
        html, re.S
    )
    for block in book_blocks:
        text = _clean_html(block)
        if not text or len(text) < 3:
            continue

        position = None
        # Chercher le numéro de tome dans le titre
        for pat in _VOL_PATTERNS:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                try:
                    position = float(m.group(1))
                    break
                except ValueError:
                    pass

        if position is not None:
            # Nettoyer le titre : enlever les patterns "Tome N -" du début
            title = re.sub(r'^.*?[-–]\s*(?:tome|vol\.?|t\.)\s*\d+\s*[-–]\s*', '', text, flags=re.IGNORECASE).strip()
            if not title:
                title = text
            volumes.append({"position": position, "title": title})

    # Dédupliquer par position, garder le premier
    seen: set[float] = set()
    result = []
    for v in volumes:
        if v["position"] not in seen:
            seen.add(v["position"])
            result.append(v)

    return sorted(result, key=lambda x: x["position"])


async def search_complete_volume_list(
    series_name: str,
    client: httpx.AsyncClient,
) -> list[dict]:
    """
    Cherche la liste complète des volumes d'une série.
    Stratégie :
      1. DDG site:babelio.com → URL série → scrape liste complète
      2. Fallback : extraction de numéros depuis snippets DDG généraux
    Retourne [{position, title}].
    """
    # ── Étape 1 : trouver la page Babelio via DDG ─────────────────────────
    babelio_url = await _ddg_find_url(
        f'site:babelio.com "{series_name}" série',
        client,
        "babelio.com/serie",
    )

    if babelio_url:
        await asyncio.sleep(0.8)
        volumes = await _fetch_babelio_volumes(babelio_url, client)
        if volumes:
            return volumes

    # ── Étape 2 : fallback DDG général ────────────────────────────────────
    await asyncio.sleep(0.8)
    text1 = await _ddg_query(f'"{series_name}" liste tomes bd livre série', client)
    await asyncio.sleep(0.8)
    text2 = await _ddg_query(f'{series_name} intégrale nombre tomes', client)
    full_text = text1 + " " + text2

    nums: set[float] = set()
    for pat in _VOL_PATTERNS:
        for m in re.finditer(pat, full_text, re.IGNORECASE):
            try:
                nums.add(float(m.group(1)))
            except ValueError:
                pass

    return [{"position": p, "title": None} for p in sorted(nums) if 1 <= p <= 500]


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
