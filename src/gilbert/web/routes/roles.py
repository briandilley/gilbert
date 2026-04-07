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
                tools.append({
                    "name": tool_def.name,
                    "provider": svc.tool_provider_name,
                    "default_role": tool_def.required_role,
                    "effective_role": override or tool_def.required_role,
                    "has_override": override is not None,
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


# --- AI Context Profiles ---


def _get_ai_service(gilbert: Gilbert) -> Any:
    svc = gilbert.service_manager.get_by_capability("ai_chat")
    if svc is None:
        raise HTTPException(status_code=503, detail="AI service not available")
    return svc


@router.get("/profiles")
async def ai_profiles_page(
    request: Request,
    user: UserContext = Depends(require_role("admin")),
) -> Any:
    gilbert: Gilbert = request.app.state.gilbert
    ai_svc = _get_ai_service(gilbert)

    profiles = ai_svc.list_profiles()
    assignments = ai_svc.list_assignments()

    # Gather all declared ai_calls from services
    sm = gilbert.service_manager
    declared_calls: list[dict[str, str]] = []
    for svc_name in sm.started_services:
        svc = sm._registered.get(svc_name)
        if svc is not None:
            info = svc.service_info()
            for call_name in sorted(info.ai_calls):
                declared_calls.append({
                    "call_name": call_name,
                    "service": info.name,
                    "profile": assignments.get(call_name, "default"),
                })
    # Add built-in calls not from services
    for call_name in sorted(assignments):
        if not any(d["call_name"] == call_name for d in declared_calls):
            declared_calls.append({
                "call_name": call_name,
                "service": "(built-in)",
                "profile": assignments[call_name],
            })

    profile_names = [p.name for p in profiles]

    # Gather all tool names for checkbox UI
    all_tool_names: list[str] = []
    for svc_name in sm.started_services:
        svc = sm._registered.get(svc_name)
        if svc is not None and isinstance(svc, ToolProvider):
            for tool_def in svc.get_tools():
                all_tool_names.append(tool_def.name)
    all_tool_names.sort()

    return templates.TemplateResponse(request, "ai_profiles.html", {
        "profiles": [
            {
                "name": p.name,
                "description": p.description,
                "tool_mode": p.tool_mode,
                "tools": p.tools,
                "tool_roles": p.tool_roles,
            }
            for p in profiles
        ],
        "declared_calls": declared_calls,
        "profile_names": profile_names,
        "all_tool_names": all_tool_names,
        "user": user,
    })


def _is_ajax(request: Request) -> bool:
    return request.headers.get("x-requested-with") == "fetch"


@router.post("/profiles/save")
async def save_ai_profile(
    request: Request,
    user: UserContext = Depends(require_role("admin")),
) -> Any:
    from gilbert.core.services.ai import AIContextProfile

    gilbert: Gilbert = request.app.state.gilbert
    ai_svc = _get_ai_service(gilbert)

    form = await request.form()
    name = str(form.get("name", "")).strip()
    description = str(form.get("description", "")).strip()
    tool_mode = str(form.get("tool_mode", "all"))
    tools_list = form.getlist("tools")

    profile = AIContextProfile(
        name=name,
        description=description,
        tool_mode=tool_mode,
        tools=tools_list,
    )
    await ai_svc.set_profile(profile)
    if _is_ajax(request):
        return {"status": "ok", "profile": name}
    return RedirectResponse(url="/roles/profiles", status_code=303)


@router.post("/profiles/{profile_name}/delete")
async def delete_ai_profile(
    request: Request,
    profile_name: str,
    user: UserContext = Depends(require_role("admin")),
) -> Any:
    gilbert: Gilbert = request.app.state.gilbert
    ai_svc = _get_ai_service(gilbert)

    try:
        await ai_svc.delete_profile(profile_name)
    except (KeyError, ValueError):
        pass
    if _is_ajax(request):
        return {"status": "ok"}
    return RedirectResponse(url="/roles/profiles", status_code=303)


@router.post("/profiles/assign")
async def assign_ai_profile(
    request: Request,
    call_name: str = Form(...),
    profile: str = Form(...),
    user: UserContext = Depends(require_role("admin")),
) -> Any:
    gilbert: Gilbert = request.app.state.gilbert
    ai_svc = _get_ai_service(gilbert)

    try:
        await ai_svc.set_assignment(call_name, profile)
    except ValueError:
        pass
    if _is_ajax(request):
        return {"status": "ok", "call_name": call_name, "profile": profile}
    return RedirectResponse(url="/roles/profiles", status_code=303)


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
