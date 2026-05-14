"""Servidor MCP HTTP para que Claude (Desktop, Web, Mobile) controle N8N de Mine Store.

Soporta DOS modos de autenticación:
- **Bearer token fijo** (para Claude Desktop / Claude Code CLI): se envia en el header
  `Authorization: Bearer <MCP_BEARER_TOKEN>`. Simple, configurado por env var.
- **OAuth 2.1 + DCR + PKCE** (para Claude Web / Mobile): el cliente se registra dinámicamente,
  obtiene un access token mediante código + verificador PKCE. El usuario aprueba ingresando
  la `MCP_AUTH_PASSWORD` configurada por env var.

Variables de entorno requeridas:
    N8N_BASE            : URL base de N8N (ej. https://n8n.yoanyandres.one)
    N8N_API_KEY         : API key de N8N
    MCP_BEARER_TOKEN    : token fijo para Desktop/CLI (opcional pero recomendado)
    MCP_AUTH_PASSWORD   : password que aprueba autorizaciones OAuth (recomendado)
    MCP_PUBLIC_URL      : URL pública completa donde corre este servidor
                          (ej. https://n8n-mcp-n8n.4043xj.easypanel.host)
                          Default: se infiere del host del request

Puerto: 8080 (env PORT)
Endpoints:
    GET  /healthz                                      → health check (público)
    GET  /.well-known/oauth-authorization-server       → metadata OAuth (público, RFC 8414)
    POST /register                                     → DCR OAuth (público, RFC 7591)
    GET  /authorize                                    → form HTML para aprobar auth
    POST /authorize                                    → procesa form, emite code
    POST /token                                        → intercambia code por access_token (PKCE RFC 7636)
    GET  /sse                                          → endpoint MCP SSE (protegido)
    POST /messages/                                    → endpoint interno SSE
"""
from __future__ import annotations
import asyncio
import base64
import hashlib
import json
import os
import secrets
import time
from typing import Any
from urllib.parse import urlencode

import httpx
from mcp.server import Server
from mcp.server.sse import SseServerTransport
from mcp.types import Tool, TextContent
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.routing import Mount, Route

# =========================================================================
# CONFIG (variables de entorno)
# =========================================================================
N8N_BASE = os.environ.get("N8N_BASE", "https://n8n.yoanyandres.one").rstrip("/")
N8N_API_KEY = os.environ.get("N8N_API_KEY", "")
MCP_BEARER_TOKEN = os.environ.get("MCP_BEARER_TOKEN", "")
MCP_AUTH_PASSWORD = os.environ.get("MCP_AUTH_PASSWORD", "")
MCP_PUBLIC_URL = os.environ.get("MCP_PUBLIC_URL", "").rstrip("/")
PORT = int(os.environ.get("PORT", "8080"))

# Logs de arranque para debug
print(f"[boot] N8N_BASE: {N8N_BASE}")
print(f"[boot] N8N_API_KEY: {'SET' if N8N_API_KEY else 'EMPTY'}")
print(f"[boot] MCP_BEARER_TOKEN: {'SET' if MCP_BEARER_TOKEN else 'EMPTY (sin auth fijo, solo OAuth)'}")
print(f"[boot] MCP_AUTH_PASSWORD: {'SET' if MCP_AUTH_PASSWORD else 'EMPTY (OAuth desactivado)'}")
print(f"[boot] PORT: {PORT}")

# =========================================================================
# HTTP CLIENT helper (sigue igual)
# =========================================================================
_HTTP_TIMEOUT = httpx.Timeout(connect=10, read=300, write=30, pool=30)


async def n8n_api_get(path: str) -> dict:
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT, verify=True) as c:
        r = await c.get(f"{N8N_BASE}/api/v1{path}", headers={"X-N8N-API-KEY": N8N_API_KEY})
        r.raise_for_status()
        return r.json()


async def n8n_api_post(path: str, body: dict | None = None) -> dict:
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT, verify=True) as c:
        r = await c.post(
            f"{N8N_BASE}/api/v1{path}",
            headers={"X-N8N-API-KEY": N8N_API_KEY, "Content-Type": "application/json"},
            json=body or {},
        )
        return {"status": r.status_code, "body": r.text[:2000]}


async def n8n_webhook(path: str, body: dict | None = None, method: str = "POST") -> str:
    url = f"{N8N_BASE}/webhook/{path.lstrip('/')}"
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT, verify=True) as c:
        if method.upper() == "GET":
            r = await c.get(url)
        else:
            r = await c.post(url, json=body or {})
        return f"HTTP {r.status_code}\n{r.text[:4000]}"


# =========================================================================
# MCP SERVER y TOOLS (idéntico a versión Bearer)
# =========================================================================
server: Server = Server("n8n-oficina-mcp")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(name="estado_sistema",
             description="Consulta el estado del sistema de conciliacion (metricas, salud).",
             inputSchema={"type": "object", "properties": {}, "required": []}),
        Tool(name="procesar_dia",
             description="Dispara MASTER (Scripts 1+2+3). Tarda 1-3 min. Genera paso1/2/3 en OUTPUTS.",
             inputSchema={"type": "object", "properties": {}, "required": []}),
        Tool(name="cerrar_dia",
             description="Dispara Workflow D (genera paso4). Cruza paso3 con chat. Opcional: chat_data del parser.",
             inputSchema={"type": "object", "properties": {
                 "chat_data": {"type": "array", "items": {"type": "object"}}}}),
        Tool(name="ejecutar_script",
             description="Dispara Script 1, 2 o 3 individualmente.",
             inputSchema={"type": "object", "properties": {
                 "script": {"type": "integer", "enum": [1, 2, 3]}}, "required": ["script"]}),
        Tool(name="procesar_chat_ia",
             description="Llama al workflow Procesar Chat (Gemini extrae transacciones de texto).",
             inputSchema={"type": "object", "properties": {
                 "cliente": {"type": "string"},
                 "fecha": {"type": "string", "description": "YYYY-MM-DD"},
                 "conversacion": {"type": "string"}}, "required": ["cliente", "fecha", "conversacion"]}),
        Tool(name="conciliar_telegram",
             description="Dispara Conciliacion_Telegram con chat_data (lista de transacciones).",
             inputSchema={"type": "object", "properties": {
                 "chat_data": {"type": "array", "items": {"type": "object"}}}, "required": ["chat_data"]}),
        Tool(name="listar_workflows",
             description="Lista todos los workflows de N8N.",
             inputSchema={"type": "object", "properties": {
                 "filtro_nombre": {"type": "string"},
                 "solo_activos": {"type": "boolean", "default": False}}}),
        Tool(name="obtener_workflow",
             description="Devuelve la definicion de un workflow (nodos, conexiones).",
             inputSchema={"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}),
        Tool(name="listar_ejecuciones",
             description="Lista las ultimas ejecuciones (filtro por workflow / status).",
             inputSchema={"type": "object", "properties": {
                 "workflow_id": {"type": "string"},
                 "limit": {"type": "integer", "default": 10},
                 "status": {"type": "string", "enum": ["success", "error", "running", "waiting"]}}}),
        Tool(name="obtener_ejecucion",
             description="Detalle de una ejecucion (runData de cada nodo). Util para debug.",
             inputSchema={"type": "object", "properties": {
                 "id": {"type": "string"},
                 "include_data": {"type": "boolean", "default": True}}, "required": ["id"]}),
        Tool(name="errores_recientes",
             description="Ejecuciones con error de las ultimas N.",
             inputSchema={"type": "object", "properties": {"limit": {"type": "integer", "default": 20}}}),
        Tool(name="activar_workflow",
             description="Activa un workflow en N8N.",
             inputSchema={"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}),
        Tool(name="desactivar_workflow",
             description="Desactiva un workflow en N8N.",
             inputSchema={"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict | None = None) -> list[TextContent]:
    arguments = arguments or {}
    try:
        if name == "estado_sistema":
            return [TextContent(type="text", text=await n8n_webhook("estado-bot", method="GET"))]
        if name == "procesar_dia":
            r = await n8n_webhook("exec-master-procesar")
            return [TextContent(type="text", text=f"MASTER disparado.\n{r}\n\nVerifica OUTPUTS en 2-5 min.")]
        if name == "cerrar_dia":
            body = {"chat_data": arguments["chat_data"]} if arguments.get("chat_data") else {}
            return [TextContent(type="text", text=f"Workflow D disparado.\n{await n8n_webhook('exec-script4', body)}")]
        if name == "ejecutar_script":
            s = arguments.get("script")
            if s not in (1, 2, 3):
                return [TextContent(type="text", text="ERROR: script debe ser 1, 2 o 3")]
            return [TextContent(type="text", text=f"Script {s} disparado.\n{await n8n_webhook(f'exec-script{s}')}")]
        if name == "procesar_chat_ia":
            body = {"clients": [{"cliente": arguments.get("cliente"), "fecha": arguments.get("fecha"),
                                  "conversacion": arguments.get("conversacion")}]}
            return [TextContent(type="text", text=await n8n_webhook("procesar-chat", body))]
        if name == "conciliar_telegram":
            return [TextContent(type="text", text=await n8n_webhook("exec-conciliacion-telegram",
                                                                      {"chat_data": arguments["chat_data"]}))]
        if name == "listar_workflows":
            data = await n8n_api_get("/workflows?limit=200")
            wfs = data.get("data", [])
            filtro = (arguments.get("filtro_nombre") or "").lower()
            solo_act = arguments.get("solo_activos", False)
            out = []
            for w in wfs:
                if solo_act and not w.get("active"):
                    continue
                if filtro and filtro not in w.get("name", "").lower():
                    continue
                out.append(f"  {w['id']:25} active={str(w.get('active','?')):<5} {w.get('name','?')}")
            return [TextContent(type="text", text=f"Total: {len(out)}\n" + "\n".join(out))]
        if name == "obtener_workflow":
            data = await n8n_api_get(f"/workflows/{arguments.get('id')}")
            resumen = {"id": data.get("id"), "name": data.get("name"), "active": data.get("active"),
                       "updatedAt": data.get("updatedAt"), "nodes_count": len(data.get("nodes", [])),
                       "nodes": [{"name": n.get("name"), "type": n.get("type")} for n in data.get("nodes", [])]}
            return [TextContent(type="text", text=json.dumps(resumen, indent=2, ensure_ascii=False))]
        if name == "listar_ejecuciones":
            qs = f"?limit={arguments.get('limit', 10)}"
            if arguments.get("workflow_id"):
                qs += f"&workflowId={arguments['workflow_id']}"
            if arguments.get("status"):
                qs += f"&status={arguments['status']}"
            data = await n8n_api_get(f"/executions{qs}")
            execs = data.get("data", [])
            out = [f"  id={e.get('id')} status={e.get('status','?'):<10} started={e.get('startedAt','?')} workflowId={e.get('workflowId','?')}" for e in execs]
            return [TextContent(type="text", text=f"Total: {len(out)}\n" + "\n".join(out))]
        if name == "obtener_ejecucion":
            data = await n8n_api_get(f"/executions/{arguments.get('id')}?includeData={str(arguments.get('include_data', True)).lower()}")
            ed = data.get("data")
            if isinstance(ed, str):
                try: ed = json.loads(ed)
                except Exception: ed = {}
            ed = ed or {}
            run_data = ed.get("resultData", {}).get("runData", {})
            nodos = []
            for nname, runs in run_data.items():
                for r in runs:
                    err = r.get("error")
                    nodos.append({"node": nname, "error": err.get("message", "?") if err else None,
                                   "executionStatus": r.get("executionStatus")})
            return [TextContent(type="text", text=json.dumps({"id": data.get("id"), "status": data.get("status"),
                                                                "startedAt": data.get("startedAt"),
                                                                "workflowId": data.get("workflowId"),
                                                                "nodes": nodos[:50]}, indent=2, default=str))]
        if name == "errores_recientes":
            data = await n8n_api_get(f"/executions?status=error&limit={arguments.get('limit', 20)}")
            execs = data.get("data", [])
            out = [f"  id={e.get('id')} workflowId={e.get('workflowId')} started={e.get('startedAt')}" for e in execs]
            return [TextContent(type="text", text=f"Errores: {len(out)}\n" + "\n".join(out))]
        if name == "activar_workflow":
            wid = arguments.get("id")
            result = await n8n_api_post(f"/workflows/{wid}/activate")
            return [TextContent(type="text", text=f"Activar {wid}: {result}")]
        if name == "desactivar_workflow":
            wid = arguments.get("id")
            result = await n8n_api_post(f"/workflows/{wid}/deactivate")
            return [TextContent(type="text", text=f"Desactivar {wid}: {result}")]
        return [TextContent(type="text", text=f"Tool desconocida: {name}")]
    except httpx.HTTPStatusError as e:
        return [TextContent(type="text", text=f"HTTP error: {e.response.status_code} {e.response.text[:500]}")]
    except Exception as e:
        return [TextContent(type="text", text=f"ERROR: {type(e).__name__}: {e}")]


# =========================================================================
# OAuth 2.1 + DCR + PKCE
# Storage in-memory: si el contenedor reinicia, los clients se vuelven a registrar automaticamente
# =========================================================================
_oauth_clients: dict[str, dict] = {}        # client_id -> {redirect_uris, ...}
_oauth_auth_codes: dict[str, dict] = {}     # code -> {client_id, code_challenge, redirect_uri, expires_at}
_oauth_access_tokens: dict[str, dict] = {}  # access_token -> {client_id, expires_at}


def _get_base_url(request: Request) -> str:
    """URL pública del servidor. Prioriza MCP_PUBLIC_URL, sino infiere del request."""
    if MCP_PUBLIC_URL:
        return MCP_PUBLIC_URL
    # Detrás de reverse proxy
    proto = request.headers.get("x-forwarded-proto", "https")
    host = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
    return f"{proto}://{host}"


async def oauth_metadata(request: Request):
    """RFC 8414 - Authorization Server Metadata."""
    base = _get_base_url(request)
    return JSONResponse({
        "issuer": base,
        "authorization_endpoint": f"{base}/authorize",
        "token_endpoint": f"{base}/token",
        "registration_endpoint": f"{base}/register",
        "scopes_supported": ["mcp"],
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
    })


async def oauth_register(request: Request):
    """RFC 7591 - Dynamic Client Registration."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    client_id = secrets.token_urlsafe(16)
    _oauth_clients[client_id] = {
        "redirect_uris": body.get("redirect_uris", []),
        "client_name": body.get("client_name", "unknown"),
        "issued_at": int(time.time()),
    }
    return JSONResponse({
        "client_id": client_id,
        "client_id_issued_at": _oauth_clients[client_id]["issued_at"],
        "redirect_uris": body.get("redirect_uris", []),
        "client_name": body.get("client_name", "MCP Client"),
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code"],
        "response_types": ["code"],
    })


def _auth_form_html(client_id: str, redirect_uri: str, code_challenge: str,
                    code_challenge_method: str, state: str, scope: str, error: str = "") -> str:
    safe_client = client_id.replace("<", "&lt;")
    safe_redirect = redirect_uri.replace("<", "&lt;").replace('"', "&quot;")
    safe_state = (state or "").replace("<", "&lt;").replace('"', "&quot;")
    err_html = f'<p style="color:#ff6b6b;background:#3a1818;padding:10px;border-radius:6px;">{error}</p>' if error else ""
    return f"""<!doctype html>
<html lang="es"><head><meta charset="utf-8"/>
<title>Aprobar acceso MCP — Mine Store</title>
<style>
body {{ font-family: sans-serif; background: #1a1d24; color: #e6e8eb; max-width: 460px; margin: 50px auto; padding: 30px; border: 1px solid #374151; border-radius: 12px; }}
h1 {{ font-size: 22px; color: #4ade80; }}
.client {{ background: #242830; padding: 12px; border-radius: 8px; font-size: 13px; margin: 16px 0; }}
.client code {{ color: #93c5fd; }}
form {{ margin-top: 20px; }}
input[type=password] {{ width: 100%; padding: 12px; background: #0f1115; color: #fff; border: 1px solid #374151; border-radius: 6px; font-size: 15px; box-sizing: border-box; }}
button {{ margin-top: 16px; width: 100%; padding: 12px; background: #4ade80; color: #0a0a0a; border: 0; border-radius: 6px; font-weight: 700; font-size: 15px; cursor: pointer; }}
button:hover {{ background: #22c55e; }}
.help {{ font-size: 12px; color: #94a3b8; margin-top: 14px; }}
</style></head><body>
<h1>🔐 Aprobar acceso al MCP</h1>
<p>Un cliente quiere conectarse al servidor MCP de <strong>Mine Store Digital</strong>.</p>
<div class="client">Cliente: <code>{safe_client}</code></div>
{err_html}
<form method="POST" action="/authorize">
  <input type="hidden" name="client_id" value="{safe_client}"/>
  <input type="hidden" name="redirect_uri" value="{safe_redirect}"/>
  <input type="hidden" name="code_challenge" value="{code_challenge}"/>
  <input type="hidden" name="code_challenge_method" value="{code_challenge_method}"/>
  <input type="hidden" name="state" value="{safe_state}"/>
  <input type="hidden" name="scope" value="{scope}"/>
  <label>Password de aprobación:</label>
  <input type="password" name="password" autofocus required/>
  <button type="submit">Aprobar y conectar</button>
</form>
<div class="help">Si no iniciaste vos esta solicitud, cerrá esta ventana.</div>
</body></html>"""


async def oauth_authorize_get(request: Request):
    """Muestra el form para que el user ingrese la password."""
    if not MCP_AUTH_PASSWORD:
        return JSONResponse({"error": "oauth_disabled", "msg": "MCP_AUTH_PASSWORD no configurado"}, status_code=503)
    q = request.query_params
    client_id = q.get("client_id", "")
    redirect_uri = q.get("redirect_uri", "")
    code_challenge = q.get("code_challenge", "")
    method = q.get("code_challenge_method", "S256")
    state = q.get("state", "")
    scope = q.get("scope", "mcp")
    response_type = q.get("response_type", "code")

    if response_type != "code":
        return JSONResponse({"error": "unsupported_response_type"}, status_code=400)
    if not client_id or client_id not in _oauth_clients:
        return JSONResponse({"error": "invalid_client", "msg": f"client_id desconocido: {client_id}"}, status_code=400)
    if not code_challenge or method != "S256":
        return JSONResponse({"error": "invalid_request", "msg": "Se requiere PKCE S256"}, status_code=400)

    html = _auth_form_html(client_id, redirect_uri, code_challenge, method, state, scope)
    return HTMLResponse(html)


async def oauth_authorize_post(request: Request):
    """Procesa el form. Si password OK, emite code y redirect."""
    form = await request.form()
    password = form.get("password", "")
    client_id = form.get("client_id", "")
    redirect_uri = form.get("redirect_uri", "")
    code_challenge = form.get("code_challenge", "")
    method = form.get("code_challenge_method", "S256")
    state = form.get("state", "")

    if password != MCP_AUTH_PASSWORD:
        html = _auth_form_html(client_id, redirect_uri, code_challenge, method, state, "mcp",
                               error="Password incorrecta. Volvé a intentar.")
        return HTMLResponse(html, status_code=401)

    code = secrets.token_urlsafe(32)
    _oauth_auth_codes[code] = {
        "client_id": client_id,
        "code_challenge": code_challenge,
        "code_challenge_method": method,
        "redirect_uri": redirect_uri,
        "expires_at": time.time() + 300,  # 5 min
    }

    params = {"code": code}
    if state:
        params["state"] = state
    sep = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(f"{redirect_uri}{sep}{urlencode(params)}", status_code=302)


async def oauth_token(request: Request):
    """RFC 6749 - intercambia code por access_token con verificación PKCE."""
    try:
        form = await request.form()
    except Exception:
        return JSONResponse({"error": "invalid_request"}, status_code=400)

    grant_type = form.get("grant_type")
    code = form.get("code", "")
    code_verifier = form.get("code_verifier", "")
    redirect_uri = form.get("redirect_uri", "")
    client_id = form.get("client_id", "")

    if grant_type != "authorization_code":
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)

    auth = _oauth_auth_codes.get(code)
    if not auth or auth["expires_at"] < time.time():
        return JSONResponse({"error": "invalid_grant", "msg": "code invalido o expirado"}, status_code=400)
    if client_id and client_id != auth["client_id"]:
        return JSONResponse({"error": "invalid_grant", "msg": "client_id mismatch"}, status_code=400)
    if redirect_uri != auth["redirect_uri"]:
        return JSONResponse({"error": "invalid_grant", "msg": "redirect_uri mismatch"}, status_code=400)

    # Verificar PKCE
    if auth["code_challenge_method"] == "S256":
        challenge = base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode("ascii")).digest()).decode("ascii").rstrip("=")
        if challenge != auth["code_challenge"]:
            return JSONResponse({"error": "invalid_grant", "msg": "PKCE verification failed"}, status_code=400)
    else:
        return JSONResponse({"error": "invalid_grant", "msg": "Solo S256 soportado"}, status_code=400)

    # One-time use
    del _oauth_auth_codes[code]

    access_token = secrets.token_urlsafe(32)
    _oauth_access_tokens[access_token] = {
        "client_id": auth["client_id"],
        "expires_at": time.time() + 86400 * 30,  # 30 días
    }

    return JSONResponse({
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": 86400 * 30,
        "scope": "mcp",
    })


def _is_token_valid(token: str) -> bool:
    """Verifica si un token es válido: Bearer fijo o OAuth issued."""
    if MCP_BEARER_TOKEN and token == MCP_BEARER_TOKEN:
        return True
    t = _oauth_access_tokens.get(token)
    if t and t["expires_at"] > time.time():
        return True
    return False


# =========================================================================
# STARLETTE app
# =========================================================================
sse_transport = SseServerTransport("/messages/")

# Endpoints públicos que NO requieren auth
_PUBLIC_PATHS = {
    "/", "/healthz",
    "/.well-known/oauth-authorization-server",
    "/.well-known/oauth-protected-resource",
    "/register", "/authorize", "/token",
}


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path in _PUBLIC_PATHS:
            return await call_next(request)

        auth = request.headers.get("authorization", "")
        if not auth.lower().startswith("bearer "):
            # Avisar a clientes OAuth dónde encontrar la metadata
            base = _get_base_url(request)
            resp = JSONResponse({"error": "missing_bearer"}, status_code=401)
            resp.headers["WWW-Authenticate"] = (
                f'Bearer realm="mcp", '
                f'resource_metadata="{base}/.well-known/oauth-protected-resource"'
            )
            return resp

        token = auth[7:].strip()
        if not _is_token_valid(token):
            return JSONResponse({"error": "invalid_token"}, status_code=401)
        return await call_next(request)


async def handle_sse(request: Request):
    async with sse_transport.connect_sse(request.scope, request.receive, request._send) as streams:
        await server.run(streams[0], streams[1], server.create_initialization_options())


async def healthz(request: Request):
    return JSONResponse({
        "ok": True,
        "service": "mcp-oficina",
        "n8n_base": N8N_BASE,
        "has_api_key": bool(N8N_API_KEY),
        "has_bearer": bool(MCP_BEARER_TOKEN),
        "has_oauth": bool(MCP_AUTH_PASSWORD),
        "clients_registered": len(_oauth_clients),
        "active_tokens": sum(1 for t in _oauth_access_tokens.values() if t["expires_at"] > time.time()),
    })


async def oauth_protected_resource_metadata(request: Request):
    """RFC 9728 - Protected Resource Metadata. Le dice al cliente qué auth server usar."""
    base = _get_base_url(request)
    return JSONResponse({
        "resource": base,
        "authorization_servers": [base],
        "bearer_methods_supported": ["header"],
        "scopes_supported": ["mcp"],
    })


def make_app() -> Starlette:
    return Starlette(
        debug=False,
        routes=[
            Route("/", endpoint=healthz, methods=["GET"]),
            Route("/healthz", endpoint=healthz, methods=["GET"]),
            Route("/.well-known/oauth-authorization-server", endpoint=oauth_metadata, methods=["GET"]),
            Route("/.well-known/oauth-protected-resource", endpoint=oauth_protected_resource_metadata, methods=["GET"]),
            Route("/register", endpoint=oauth_register, methods=["POST"]),
            Route("/authorize", endpoint=oauth_authorize_get, methods=["GET"]),
            Route("/authorize", endpoint=oauth_authorize_post, methods=["POST"]),
            Route("/token", endpoint=oauth_token, methods=["POST"]),
            Route("/sse", endpoint=handle_sse, methods=["GET"]),
            Mount("/messages/", app=sse_transport.handle_post_message),
        ],
        middleware=[Middleware(AuthMiddleware)],
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(make_app(), host="0.0.0.0", port=PORT, log_level="info", proxy_headers=True, forwarded_allow_ips="*")
