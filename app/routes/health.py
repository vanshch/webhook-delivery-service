"""Health check route for liveness probes."""

from fastapi import APIRouter

router = APIRouter(tags=["health"])

@router.get("/health")
def health_check():
    """Return service health status."""
    return {"status": "ok"}
