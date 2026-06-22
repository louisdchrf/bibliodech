import io
import sys
import httpx
from PIL import Image

COVERS_DIR = "/app/data/covers"
MAX_WIDTH = 600
QUALITY = 82


async def fetch_and_save(isbn: str, url: str) -> str | None:
    """Télécharge, redimensionne et sauvegarde la couverture. Retourne l'URL locale ou None."""
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(url, follow_redirects=True)
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
        print(f"[covers] failed for {isbn}: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        return None
