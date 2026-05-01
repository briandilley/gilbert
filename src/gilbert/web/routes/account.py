"""Account self-service routes — auto-captured memory management.

These routes are scoped to the *calling* user (``user.user_id`` from
``require_authenticated``); they never accept a ``user_id`` parameter.
That means an admin can't manage another user's auto-memories through
this surface — that's a deliberate constraint so the privacy story is
"only the user themselves can see / clear / opt out". Admin-side
controls go through the Settings page (``opted_out_user_ids`` config)
and the storage entity browser.
"""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from gilbert.core.app import Gilbert
from gilbert.interfaces.auth import UserContext
from gilbert.web.auth import require_authenticated

router = APIRouter(prefix="/account", tags=["account"])


def _get_user_memory_service(request: Request) -> Any:
    gilbert: Gilbert = request.app.state.gilbert
    svc = gilbert.service_manager.get_by_capability("user_memory_synthesis")
    if svc is None:
        raise HTTPException(
            status_code=503,
            detail="User memory service is not enabled",
        )
    return svc


@router.get("/memories")
async def list_memories(
    request: Request,
    user: UserContext = Depends(require_authenticated),  # noqa: B008
) -> list[dict[str, Any]]:
    """List the calling user's stored memories (both auto and user-saved)."""
    svc = _get_user_memory_service(request)
    memories = await svc.list_user_memories(user.user_id)
    # Return a stable shape; drop internal fields the SPA doesn't need.
    return [
        {
            "memory_id": m.get("_id") or m.get("memory_id"),
            "summary": m.get("summary", ""),
            "content": m.get("content", ""),
            "source": m.get("source", "user"),
            "created_at": m.get("created_at", ""),
            "updated_at": m.get("updated_at", ""),
            "access_count": m.get("access_count", 0),
        }
        for m in memories
    ]


@router.delete("/memories/{memory_id}")
async def delete_memory(
    memory_id: str,
    request: Request,
    user: UserContext = Depends(require_authenticated),  # noqa: B008
) -> Response:
    svc = _get_user_memory_service(request)
    deleted = await svc.delete_user_memory(user.user_id, memory_id)
    if not deleted:
        raise HTTPException(
            status_code=404,
            detail="Memory not found or not yours",
        )
    return Response(status_code=204)


@router.post("/memories/clear")
async def clear_memories(
    request: Request,
    user: UserContext = Depends(require_authenticated),  # noqa: B008
) -> dict[str, int]:
    svc = _get_user_memory_service(request)
    count = await svc.clear_user_memories(user.user_id)
    return {"deleted": count}


@router.get("/memory-opt-out")
async def get_opt_out(
    request: Request,
    user: UserContext = Depends(require_authenticated),  # noqa: B008
) -> dict[str, bool]:
    svc = _get_user_memory_service(request)
    return {"opted_out": await svc.get_self_opt_out(user.user_id)}


@router.post("/memory-opt-out")
async def set_opt_out(
    request: Request,
    user: UserContext = Depends(require_authenticated),  # noqa: B008
) -> Response:
    body = await request.json()
    if "opted_out" not in body:
        raise HTTPException(
            status_code=400, detail="Missing 'opted_out' boolean"
        )
    svc = _get_user_memory_service(request)
    await svc.set_self_opt_out(user.user_id, bool(body["opted_out"]))
    return Response(status_code=204)
