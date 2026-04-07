"""Roles & access control routes — manage role hierarchy, tool permissions, collection ACLs."""

from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from starlette.responses import RedirectResponse

from gilbert.core.app import Gilbert
from gilbert.interfaces.auth import UserContext
from gilbert.interfaces.tools import ToolProvider
from gilbert.web import templates
from gilbert.web.auth import require_role

router = APIRouter(prefix="/roles")


def _get_acl(gilbert: Gilbert) -> Any:
    svc = gilbert.service_manager.get_by_capability("access_control")
    if svc is None:
        raise HTTPException(status_code=503, detail="Access control service not available")
    return svc


# --- Roles list ---


@router.get("")
async def roles_list(
    request: Request,
    user: UserContext = Depends(require_role("admin")),
) -> Any:
    gilbert: Gilbert = request.app.state.gilbert
    acl = _get_acl(gilbert)

    roles = await acl.list_roles()
    for r in roles:
        r.pop("_id", None)

    return templates.TemplateResponse(request, "roles.html", {
        "roles": roles,
        "user": user,
    })


# --- Create role ---


@router.post("/create")
async def create_role(
    request: Request,
    name: str = Form(...),
    level: int = Form(...),
    description: str = Form(""),
    user: UserContext = Depends(require_role("admin")),
) -> Any:
    gilbert: Gilbert = request.app.state.gilbert
    acl = _get_acl(gilbert)

    try:
        await acl.create_role(name=name, level=level, description=description)
    except ValueError:
        pass  # silently ignore duplicate
    return RedirectResponse(url="/roles", status_code=303)


# --- Update role ---


@router.post("/{role_name}/update")
async def update_role(
    request: Request,
    role_name: str,
    level: int = Form(...),
    description: str = Form(""),
    user: UserContext = Depends(require_role("admin")),
) -> Any:
    gilbert: Gilbert = request.app.state.gilbert
    acl = _get_acl(gilbert)

    try:
        await acl.update_role(name=role_name, level=level, description=description)
    except (KeyError, ValueError):
        pass
    return RedirectResponse(url="/roles", status_code=303)


# --- Delete role ---


@router.post("/{role_name}/delete")
async def delete_role(
    request: Request,
    role_name: str,
    user: UserContext = Depends(require_role("admin")),
) -> Any:
    gilbert: Gilbert = request.app.state.gilbert
    acl = _get_acl(gilbert)

    try:
        await acl.delete_role(role_name)
    except (KeyError, ValueError):
        pass
    return RedirectResponse(url="/roles", status_code=303)


# --- Tool permissions page ---


@router.get("/tools")
async def tool_permissions(
    request: Request,
    user: UserContext = Depends(require_role("admin")),
) -> Any:
    gilbert: Gilbert = request.app.state.gilbert
    acl = _get_acl(gilbert)

    # Gather all tools from all providers
    sm = gilbert.service_manager
    tools: list[dict[str, Any]] = []
    for name in sm.started_services:
        svc = sm._registered.get(name)
        if svc is not None and isinstance(svc, ToolProvider):
            for tool_def in svc.get_tools():
                override = acl._tool_overrides.get(tool_def.name)
                chat_override = acl._chat_overrides.get(tool_def.name)
                tools.append({
                    "name": tool_def.name,
                    "provider": svc.tool_provider_name,
                    "default_role": tool_def.required_role,
                    "effective_role": override or tool_def.required_role,
                    "has_override": override is not None,
                    "default_chat_enabled": tool_def.chat_enabled,
                    "effective_chat_enabled": chat_override if chat_override is not None else tool_def.chat_enabled,
                    "has_chat_override": chat_override is not None,
                })
    tools.sort(key=lambda t: (t["provider"], t["name"]))

    roles = await acl.list_roles()
    role_names = [r["name"] for r in roles]

    return templates.TemplateResponse(request, "tool_permissions.html", {
        "tools": tools,
        "role_names": role_names,
        "user": user,
    })


# --- Set tool permission override ---


@router.post("/tools/{tool_name}/set")
async def set_tool_permission(
    request: Request,
    tool_name: str,
    required_role: str = Form(...),
    user: UserContext = Depends(require_role("admin")),
) -> Any:
    gilbert: Gilbert = request.app.state.gilbert
    acl = _get_acl(gilbert)

    try:
        await acl.set_tool_override(tool_name, required_role)
    except ValueError:
        pass
    return RedirectResponse(url="/roles/tools", status_code=303)


# --- Clear tool permission override ---


@router.post("/tools/{tool_name}/clear")
async def clear_tool_permission(
    request: Request,
    tool_name: str,
    user: UserContext = Depends(require_role("admin")),
) -> Any:
    gilbert: Gilbert = request.app.state.gilbert
    acl = _get_acl(gilbert)

    await acl.clear_tool_override(tool_name)
    return RedirectResponse(url="/roles/tools", status_code=303)


# --- Set tool chat visibility override ---


@router.post("/tools/{tool_name}/chat")
async def set_tool_chat(
    request: Request,
    tool_name: str,
    chat_enabled: bool = Form(...),
    user: UserContext = Depends(require_role("admin")),
) -> Any:
    gilbert: Gilbert = request.app.state.gilbert
    acl = _get_acl(gilbert)

    await acl.set_chat_override(tool_name, chat_enabled)
    return RedirectResponse(url="/roles/tools", status_code=303)


# --- Clear tool chat visibility override ---


@router.post("/tools/{tool_name}/chat/clear")
async def clear_tool_chat(
    request: Request,
    tool_name: str,
    user: UserContext = Depends(require_role("admin")),
) -> Any:
    gilbert: Gilbert = request.app.state.gilbert
    acl = _get_acl(gilbert)

    await acl.clear_chat_override(tool_name)
    return RedirectResponse(url="/roles/tools", status_code=303)


# --- Users & role assignment page ---


@router.get("/users")
async def user_roles(
    request: Request,
    user: UserContext = Depends(require_role("admin")),
) -> Any:
    gilbert: Gilbert = request.app.state.gilbert
    acl = _get_acl(gilbert)

    user_svc = gilbert.service_manager.get_by_capability("users")
    if user_svc is None:
        raise HTTPException(status_code=503, detail="User service not available")

    users = await user_svc.list_users()
    for u in users:
        u.pop("password_hash", None)

    roles = await acl.list_roles()
    role_names = [r["name"] for r in roles]

    return templates.TemplateResponse(request, "user_roles.html", {
        "users": users,
        "role_names": role_names,
        "user": user,
    })


# --- Assign roles to a user ---


@router.post("/users/{user_id}/roles")
async def set_user_roles(
    request: Request,
    user_id: str,
    user: UserContext = Depends(require_role("admin")),
) -> Any:
    gilbert: Gilbert = request.app.state.gilbert

    user_svc = gilbert.service_manager.get_by_capability("users")
    if user_svc is None:
        raise HTTPException(status_code=503, detail="User service not available")

    form = await request.form()
    selected_roles = form.getlist("roles")

    await user_svc.backend.update_user(user_id, {"roles": sorted(selected_roles)})
    return RedirectResponse(url="/roles/users", status_code=303)


# --- Collection ACLs page ---


@router.get("/collections")
async def collection_acls(
    request: Request,
    user: UserContext = Depends(require_role("admin")),
) -> Any:
    gilbert: Gilbert = request.app.state.gilbert
    acl = _get_acl(gilbert)

    # Get all collections from storage
    storage_svc = gilbert.service_manager.get_by_capability("entity_storage")
    collections: list[str] = []
    if storage_svc is not None:
        collections = await storage_svc.backend.list_collections()

    # Build ACL info for each collection
    acl_entries: list[dict[str, Any]] = []
    for col in sorted(collections):
        entry = acl._collection_acl.get(col)
        acl_entries.append({
            "collection": col,
            "read_role": entry["read_role"] if entry else "user",
            "write_role": entry["write_role"] if entry else "admin",
            "has_custom": entry is not None,
        })

    roles = await acl.list_roles()
    role_names = [r["name"] for r in roles]

    return templates.TemplateResponse(request, "collection_acls.html", {
        "collections": acl_entries,
        "role_names": role_names,
        "user": user,
    })


@router.post("/collections/{collection}/set")
async def set_collection_acl(
    request: Request,
    collection: str,
    read_role: str = Form(...),
    write_role: str = Form(...),
    user: UserContext = Depends(require_role("admin")),
) -> Any:
    gilbert: Gilbert = request.app.state.gilbert
    acl = _get_acl(gilbert)

    try:
        await acl.set_collection_acl(collection, read_role=read_role, write_role=write_role)
    except ValueError:
        pass
    return RedirectResponse(url="/roles/collections", status_code=303)


@router.post("/collections/{collection}/clear")
async def clear_collection_acl(
    request: Request,
    collection: str,
    user: UserContext = Depends(require_role("admin")),
) -> Any:
    gilbert: Gilbert = request.app.state.gilbert
    acl = _get_acl(gilbert)

    await acl.clear_collection_acl(collection)
    return RedirectResponse(url="/roles/collections", status_code=303)
