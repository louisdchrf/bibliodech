import io
import ipaddress
import logging
import socket
from urllib.parse import urlparse
import httpx
from PIL import Image

log = logging.getLogger(__name__)

COVERS_DIR = "/app/data/covers"
MAX_WIDTH = 600
QUALITY = 82


def _is_safe_url(url: str) -> bool:
    """Rejette les URLs pointant vers des adresses internes/privées."""
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False
        host = parsed.hostname or ""
        try:
            ip = ipaddress.ip_address(host)
            return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved)
        except ValueError:
            # C'est un nom de domaine — on résout pour vérifier
            resolved = socket.gethostbyname(host)
            ip = ipaddress.ip_address(resolved)
            return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved)
    except Exception:
        return False


async def fetch_and_save(isbn: str, url: str) -> str | None:
    """Télécharge, redimensionne et sauvegarde la couverture. Retourne l'URL locale ou None."""
    if not _is_safe_url(url):
        log.warning("covers: URL rejetée (adresse privée/invalide): %s", url)
        return None
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(url, follow_redirects=False)
            resp.raise_for_status()
            content_type = resp.headers.get("content-type", "")
            if "html" in content_type or len(resp.content) < 1000:
                return None

        img = Image.open(io.BytesIO(resp.content)).convert("RGB")
        if img.width > MAX_WIDTH:
            ratio = MAX_WIDTH / img.width
            img = img.resize((MAX_WIDTH, int(img.height * ratio)), Image.LANCZOS)

        path = f"{COVERS_DIR}/{isbn}.jpg"
        img.save(path, "JPEG", quality=QUALITY, optimize=True)
        return f"/covers/{isbn}.jpg"

    except Exception as e:
        log.warning("covers: failed for %s: %s: %s", isbn, type(e).__name__, e)
        return None
