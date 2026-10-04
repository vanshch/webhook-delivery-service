"""Public project page and a bounded set of non-sensitive evidence files."""

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

PROJECT_ROOT = Path(__file__).resolve().parents[2]
WEB_DIR = PROJECT_ROOT / "app" / "web"
ASSET_DIR = WEB_DIR / "assets"
ASSETS = {
    "styles.css": "text/css",
    "app.mjs": "text/javascript",
    "scenarios.mjs": "text/javascript",
    "mark.svg": "image/svg+xml",
}
EVIDENCE = {
    "soak": "oracle-postfix-20260906T213502Z.json",
    "benchmark": "k6-ingest-20260912-metadata.json",
}
PAGE_HEADERS = {
    "Cache-Control": "no-cache",
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; "
        "img-src 'self'; connect-src 'self'; font-src 'self'; "
        "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}

router = APIRouter(include_in_schema=False)


@router.get("/", response_class=FileResponse)
@router.head("/", response_class=FileResponse)
def project_page():
    return FileResponse(WEB_DIR / "index.html", media_type="text/html", headers=PAGE_HEADERS)


@router.get("/assets/web/{name}", response_class=FileResponse)
@router.head("/assets/web/{name}", response_class=FileResponse)
def project_asset(name: str):
    media_type = ASSETS.get(name)
    if media_type is None:
        raise HTTPException(status_code=404, detail="Asset not found")
    return FileResponse(
        ASSET_DIR / name, media_type=media_type,
        headers={"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"},
    )


@router.get("/evidence/{name}", response_class=FileResponse)
def project_evidence(name: str):
    filename = EVIDENCE.get(name)
    if filename is None:
        raise HTTPException(status_code=404, detail="Evidence not found")
    return FileResponse(
        PROJECT_ROOT / "docs" / "evidence" / filename,
        media_type="application/json",
        headers={"X-Content-Type-Options": "nosniff"},
    )
