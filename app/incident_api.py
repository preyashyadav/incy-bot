from fastapi import APIRouter
from app.incident_runner import run_incident_from_fixtures

router = APIRouter()

@router.post("/incident/start")
async def incident_start(alert: dict):
    return run_incident_from_fixtures("payments_failing", alert)
