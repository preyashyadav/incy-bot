# app/approvals_api.py
from fastapi import APIRouter
from app.approvals_store import enqueue, take_next

router = APIRouter()

@router.post("/approvals")
async def create_approval(payload: dict):
    item = enqueue(
        alert=payload.get("alert", {}),
        channel_id=payload.get("channel_id"),
        thread_ts=payload.get("thread_ts"),
    )
    return {"ok": True, "approval_id": item["id"]}

@router.get("/approvals/next")
async def get_next_approval():
    item = take_next()
    if not item:
        return {"ok": True, "has_item": False}
    return {"ok": True, "has_item": True, "item": item}
