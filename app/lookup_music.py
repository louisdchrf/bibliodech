import asyncio
import json
import httpx

_MB_USER_AGENT = "Bibliodech/1.0 (contact@bibliodech.local)"
_MB_BASE = "https://musicbrainz.org/ws/2"
_CAA_BASE = "https://coverartarchive.org/release"

_last_mb_call = 0.0


async def _mb_throttle():
    """Respecte la limite MusicBrainz : 1 req/s."""
    global _last_mb_call
    import time
    now = time.monotonic()
    wait = 1.1 - (now - _last_mb_call)
    if wait > 0:
        await asyncio.sleep(wait)
    _last_mb_call = time.monotonic()


async def _cover_from_mbid(client: httpx.AsyncClient, mbid: str) -> str | None:
    try:
        r = await client.get(f"{_CAA_BASE}/{mbid}", follow_redirects=True, timeout=5)
        if r.status_code != 200:
            return None
        imgs = r.json().get("images", [])
        front = next((i for i in imgs if i.get("front")), imgs[0] if imgs else None)
        if not front:
            return None
        return front.get("thumbnails", {}).get("large") or front.get("image")
    except Exception:
        return None


async def _lookup_musicbrainz(barcode: str) -> dict | None:
    await _mb_throttle()
    try:
        async with httpx.AsyncClient(
            timeout=10,
            headers={"User-Agent": _MB_USER_AGENT},
        ) as client:
            r = await client.get(
                f"{_MB_BASE}/release",
                params={
                    "query": f"barcode:{barcode}",
                    "fmt": "json",
                    "limit": 1,
                    "inc": "artist-credits+labels+release-groups",
                },
            )
            if r.status_code != 200:
                return None
            releases = r.json().get("releases", [])
            if not releases or releases[0].get("score", 0) < 70:
                return None

            rel = releases[0]
            artist = ""
            for ac in rel.get("artist-credit", []):
                if isinstance(ac, dict) and "artist" in ac:
                    artist = ac["artist"]["name"]
                    break

            label = ""
            catalog_number = ""
            for li in rel.get("label-info", []):
                if li.get("label"):
                    label = li["label"].get("name", "")
                catalog_number = li.get("catalog-number", "")
                if label:
                    break

            mbid = rel.get("id", "")
            cover_url = await _cover_from_mbid(client, mbid) if mbid else None

            media = rel.get("media", [{}])[0]
            fmt = media.get("format") or rel.get("release-group", {}).get("primary-type") or "CD"

            return {
                "title": rel.get("title", ""),
                "artist": artist,
                "label": label,
                "catalog_number": catalog_number,
                "year": (rel.get("date") or "")[:4] or None,
                "format": fmt,
                "track_count": media.get("track-count"),
                "language": rel.get("text-representation", {}).get("language"),
                "country": rel.get("country"),
                "cover_url": cover_url,
                "mbid": mbid,
                "source": "musicbrainz",
            }
    except Exception:
        return None


async def _lookup_discogs(barcode: str, api_key: str = "") -> dict | None:
    headers = {"User-Agent": _MB_USER_AGENT}
    if api_key:
        headers["Authorization"] = f"Discogs key={api_key}"
    try:
        async with httpx.AsyncClient(timeout=10, headers=headers) as client:
            r = await client.get(
                "https://api.discogs.com/database/search",
                params={"barcode": barcode, "type": "release"},
            )
            if r.status_code != 200:
                return None
            results = r.json().get("results", [])
            if not results:
                return None
            res = results[0]
            title_parts = res.get("title", "").split(" - ", 1)
            artist = title_parts[0] if len(title_parts) == 2 else ""
            title = title_parts[1] if len(title_parts) == 2 else title_parts[0]
            fmt_list = res.get("format", [])
            fmt = fmt_list[0] if fmt_list else "CD"
            return {
                "title": title,
                "artist": artist,
                "label": (res.get("label") or [""])[0],
                "catalog_number": (res.get("catno") or ""),
                "year": res.get("year") or None,
                "format": fmt,
                "track_count": None,
                "language": None,
                "country": res.get("country"),
                "cover_url": res.get("cover_image") or None,
                "mbid": None,
                "source": "discogs",
            }
    except Exception:
        return None


async def _cover_from_mb_search(artist: str, title: str) -> str | None:
    """Cherche un MBID par titre+artiste et tente la Cover Art Archive."""
    if not artist or not title:
        return None
    await _mb_throttle()
    try:
        async with httpx.AsyncClient(
            timeout=10,
            headers={"User-Agent": _MB_USER_AGENT},
        ) as client:
            r = await client.get(
                f"{_MB_BASE}/release",
                params={
                    "query": f'artist:"{artist}" AND release:"{title}"',
                    "fmt": "json",
                    "limit": 5,
                },
            )
            if r.status_code != 200:
                return None
            for rel in r.json().get("releases", []):
                if rel.get("score", 0) < 80:
                    continue
                mbid = rel.get("id")
                if not mbid:
                    continue
                url = await _cover_from_mbid(client, mbid)
                if url:
                    return url
    except Exception:
        return None
    return None


async def lookup_barcode(barcode: str, discogs_key: str = "") -> dict | None:
    """Cherche un code-barres musical sur MusicBrainz puis Discogs en fallback."""
    result = await _lookup_musicbrainz(barcode)
    if result:
        if not result.get("cover_url"):
            discogs = await _lookup_discogs(barcode, discogs_key)
            if discogs and discogs.get("cover_url"):
                result["cover_url"] = discogs["cover_url"]
        if not result.get("cover_url"):
            result["cover_url"] = await _cover_from_mb_search(result.get("artist"), result.get("title"))
        return result
    result = await _lookup_discogs(barcode, discogs_key)
    if result and not result.get("cover_url"):
        result["cover_url"] = await _cover_from_mb_search(result.get("artist"), result.get("title"))
    return result


def is_music_barcode(barcode: str) -> bool:
    """Retourne True si le code-barres n'est pas un ISBN livre (978/979)."""
    return bool(barcode) and not barcode.startswith("978") and not barcode.startswith("979")
