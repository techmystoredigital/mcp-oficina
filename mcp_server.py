"""Servidor MCP HTTP para que Claude (Desktop, Web, Mobile) controle N8N de My Store Digital.

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
import sqlite3
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx
from mcp.server import Server
from mcp.server.sse import SseServerTransport
from mcp.types import Tool, TextContent
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.routing import Mount, Route

# =========================================================================
# CONFIG (variables de entorno)
# =========================================================================
N8N_BASE = os.environ.get("N8N_BASE", "https://n8n.yoanyandres.one").rstrip("/")
N8N_API_KEY = os.environ.get("N8N_API_KEY", "")
# Operador (donde corre el webhook de visión) — para contar recibos leídos en vivo (progreso_whatsapp)
OP_N8N_BASE = os.environ.get("OP_N8N_BASE", "https://n8n.yoanyandres.one").rstrip("/")
OP_N8N_KEY = os.environ.get("OP_N8N_KEY", "")
VISION_WF_ID = os.environ.get("VISION_WF_ID", "EOawUwsoGf94R3OE")
CONCILIAR_WF_ID = os.environ.get("CONCILIAR_WF_ID", "M98V0iDysrLyhFfL")
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


async def op_api_get(path: str) -> dict:
    """GET a la N8N del operador (para progreso: contar ejecuciones del webhook de visión)."""
    if not OP_N8N_KEY:
        return {}
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT, verify=True) as c:
        r = await c.get(f"{OP_N8N_BASE}/api/v1{path}", headers={"X-N8N-API-KEY": OP_N8N_KEY})
        r.raise_for_status()
        return r.json()


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
        Tool(name="previsualizar_fechas",
             description="Lee las cajas Virtualsoft y el Yopago SIN procesar nada. Devuelve: fechas detectadas en las cajas (con cantidad y monto), fechas_sugeridas (nuevas, posteriores a la ultima del Yopago), fechas_ya_procesadas, ultima_fecha_yopago y dia_actual. Usar SIEMPRE antes de procesar para mostrar a Grecia que fechas se procesarian y que ella confirme.",
             inputSchema={"type": "object", "properties": {}, "required": []}),
        Tool(name="procesar_dia",
             description="Dispara MASTER (Scripts 1+2+3). Tarda 1-3 min. Genera paso1/2/3 en OUTPUTS. Parametro opcional fechas_a_procesar (lista YYYY-MM-DD): si se pasa, procesa SOLO esas fechas (control manual); si se omite, procesa automaticamente todas las posteriores a la ultima fecha del Yopago.",
             inputSchema={"type": "object", "properties": {
                 "fechas_a_procesar": {"type": "array", "items": {"type": "string"},
                     "description": "Lista de fechas YYYY-MM-DD a procesar. Opcional; omitir = modo automatico."}}, "required": []}),
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
        Tool(name="conciliar_whatsapp",
             description="2da automatizacion: concilia por WhatsApp. Lee el puntero del ultimo run de Scripts (_ultimo_run.json), baja el paso3, captura el chat de WhatsApp UNA vez (/api/state), lee los recibos con Vision (Gemini) y genera 'paso4_whatsapp' (Yopago final conciliado) en la carpeta del dia. Correr DESPUES de procesar_dia. Tarda 1-3 min. Solo llena con certeza (regla de oro); lo dudoso queda en blanco y se reporta.",
             inputSchema={"type": "object", "properties": {
                 "fechas": {"type": "array", "items": {"type": "string"},
                     "description": "Opcional: lista de fechas YYYY-MM-DD a conciliar. Omitir = todas las pendientes del paso3."}}, "required": []}),
        Tool(name="progreso_whatsapp",
             description="Consulta el AVANCE de la conciliacion WhatsApp en curso: estado (corriendo/termino/error), recibos leidos en los ultimos minutos y errores recientes. Usar DESPUES de conciliar_whatsapp, repetidamente cada ~30s, para reportar el progreso al usuario hasta que termine.",
             inputSchema={"type": "object", "properties": {}, "required": []}),
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
        if name == "previsualizar_fechas":
            return [TextContent(type="text", text=await n8n_webhook("previsualizar-fechas"))]
        if name == "procesar_dia":
            body = {}
            fechas = arguments.get("fechas_a_procesar")
            if fechas:
                body["fechas_a_procesar"] = fechas
            r = await n8n_webhook("exec-master-procesar", body)
            extra = f" (fechas elegidas: {', '.join(fechas)})" if fechas else " (modo automatico: posteriores a la ultima del Yopago)"
            return [TextContent(type="text", text=f"MASTER disparado{extra}.\n{r}\n\nVerifica OUTPUTS en 2-5 min.")]
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
        if name == "conciliar_whatsapp":
            body = {}
            if arguments.get("fechas"):
                body["fechas"] = arguments["fechas"]
            r = await n8n_webhook("conciliar-whatsapp", body)
            return [TextContent(type="text", text="Conciliacion WhatsApp INICIADA en segundo plano. Consulta 'progreso_whatsapp' cada ~30s para ver el avance (recibos leidos / estado / errores). El paso4 y Recibos_Leidos.xlsx quedan en OUTPUTS al terminar.\n" + r)]
        if name == "progreso_whatsapp":
            import datetime as _dt
            cdata = await n8n_api_get(f"/executions?workflowId={CONCILIAR_WF_ID}&limit=1")
            cex = cdata.get("data") or []
            estado, corriendo = "sin ejecuciones", False
            if cex:
                e = cex[0]; st = e.get("status", "?")
                corriendo = (st == "running") or (not e.get("stoppedAt"))
                estado = ("CORRIENDO desde " + str(e.get("startedAt", ""))[11:19] + "Z") if corriendo else (str(st).upper() + " (fin " + str(e.get("stoppedAt", ""))[11:19] + "Z)")
            recibos = None
            if OP_N8N_KEY:
                try:
                    vdata = await op_api_get(f"/executions?workflowId={VISION_WF_ID}&limit=250")
                    now = _dt.datetime.now(_dt.timezone.utc); cnt = 0
                    for e in (vdata.get("data") or []):
                        t = e.get("startedAt")
                        if t:
                            try:
                                d = _dt.datetime.fromisoformat(str(t).replace("Z", "+00:00"))
                                if (now - d).total_seconds() <= 40 * 60:
                                    cnt += 1
                            except Exception:
                                pass
                    recibos = cnt
                except Exception:
                    recibos = None
            edata = await n8n_api_get(f"/executions?workflowId={CONCILIAR_WF_ID}&status=error&limit=3")
            errs = len(edata.get("data") or [])
            txt = f"Conciliacion WhatsApp: {estado}. "
            if recibos is not None:
                txt += f"Recibos leidos (ult. 40 min): {recibos}. "
            txt += f"Errores recientes: {errs}."
            payload = {"estado": estado, "corriendo": corriendo, "recibos_leidos_40min": recibos, "errores_recientes": errs}
            return [TextContent(type="text", text=txt + "\n" + json.dumps(payload, ensure_ascii=False))]
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
# Storage: SQLite persistente. Se guarda en /data/oauth.db (volumen Docker montado en EasyPanel).
# Si /data no es escribible (ej. dev local sin volumen), fallback a ./oauth.db al lado del script.
# Los access tokens NO expiran (vida útil indefinida hasta revocación manual o cambio de password OAuth).
# Los auth codes mantienen TTL de 5 min (security best practice, no afecta UX).
# =========================================================================
OAUTH_DB_PATH = os.environ.get("OAUTH_DB_PATH", "/data/oauth.db")


def _resolve_db_path() -> str:
    """Devuelve la ruta efectiva de la DB. Si la preferida no es escribible, usa fallback local."""
    preferred = Path(OAUTH_DB_PATH)
    try:
        preferred.parent.mkdir(parents=True, exist_ok=True)
        # Probar escritura real
        test = preferred.parent / ".write_test"
        test.write_text("ok")
        test.unlink()
        return str(preferred)
    except (OSError, PermissionError):
        fallback = Path(__file__).resolve().parent / "oauth.db"
        return str(fallback)


_DB_PATH = _resolve_db_path()


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH, timeout=10.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def _db_init() -> None:
    """Crea las tablas si no existen. Idempotente."""
    with _db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS oauth_clients (
                client_id     TEXT PRIMARY KEY,
                client_name   TEXT,
                redirect_uris TEXT,
                issued_at     INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS oauth_auth_codes (
                code                  TEXT PRIMARY KEY,
                client_id             TEXT NOT NULL,
                code_challenge        TEXT NOT NULL,
                code_challenge_method TEXT NOT NULL,
                redirect_uri          TEXT NOT NULL,
                expires_at            REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS oauth_access_tokens (
                access_token TEXT PRIMARY KEY,
                client_id    TEXT NOT NULL,
                created_at   REAL NOT NULL
            );
            """
        )


# ---- Helpers de acceso a la DB ----
def _client_insert(client_id: str, client_name: str, redirect_uris: list, issued_at: int) -> None:
    with _db() as conn:
        conn.execute(
            "INSERT INTO oauth_clients (client_id, client_name, redirect_uris, issued_at) VALUES (?, ?, ?, ?)",
            (client_id, client_name, json.dumps(redirect_uris), issued_at),
        )


def _client_exists(client_id: str) -> bool:
    with _db() as conn:
        row = conn.execute("SELECT 1 FROM oauth_clients WHERE client_id = ?", (client_id,)).fetchone()
        return row is not None


def _code_insert(code: str, client_id: str, code_challenge: str, method: str, redirect_uri: str, expires_at: float) -> None:
    with _db() as conn:
        conn.execute(
            "INSERT INTO oauth_auth_codes (code, client_id, code_challenge, code_challenge_method, redirect_uri, expires_at) VALUES (?, ?, ?, ?, ?, ?)",
            (code, client_id, code_challenge, method, redirect_uri, expires_at),
        )


def _code_pop(code: str) -> dict | None:
    """Devuelve el code y lo elimina (one-time use). None si no existe."""
    with _db() as conn:
        row = conn.execute("SELECT * FROM oauth_auth_codes WHERE code = ?", (code,)).fetchone()
        if row is None:
            return None
        conn.execute("DELETE FROM oauth_auth_codes WHERE code = ?", (code,))
        return dict(row)


def _token_insert(access_token: str, client_id: str) -> None:
    with _db() as conn:
        conn.execute(
            "INSERT INTO oauth_access_tokens (access_token, client_id, created_at) VALUES (?, ?, ?)",
            (access_token, client_id, time.time()),
        )


def _token_exists(access_token: str) -> bool:
    with _db() as conn:
        row = conn.execute(
            "SELECT 1 FROM oauth_access_tokens WHERE access_token = ?", (access_token,)
        ).fetchone()
        return row is not None


def _count_clients() -> int:
    with _db() as conn:
        return conn.execute("SELECT COUNT(*) FROM oauth_clients").fetchone()[0]


def _count_tokens() -> int:
    with _db() as conn:
        return conn.execute("SELECT COUNT(*) FROM oauth_access_tokens").fetchone()[0]


_db_init()


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
    issued_at = int(time.time())
    _client_insert(
        client_id=client_id,
        client_name=body.get("client_name", "unknown"),
        redirect_uris=body.get("redirect_uris", []),
        issued_at=issued_at,
    )
    return JSONResponse({
        "client_id": client_id,
        "client_id_issued_at": issued_at,
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
<title>Aprobar acceso MCP — My Store Digital</title>
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
<p>Un cliente quiere conectarse al servidor MCP de <strong>My Store Digital</strong>.</p>
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
    if not client_id or not _client_exists(client_id):
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
    _code_insert(
        code=code,
        client_id=client_id,
        code_challenge=code_challenge,
        method=method,
        redirect_uri=redirect_uri,
        expires_at=time.time() + 300,  # 5 min, sigue siendo TTL corto por seguridad
    )

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

    # _code_pop hace SELECT + DELETE atómico (one-time use garantizado)
    auth = _code_pop(code)
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

    access_token = secrets.token_urlsafe(32)
    _token_insert(access_token=access_token, client_id=auth["client_id"])

    # Los tokens NO expiran. Se invalidan solo borrando la fila o cambiando MCP_AUTH_PASSWORD
    # (los tokens viejos siguen siendo válidos, pero conexiones nuevas requieren el password nuevo).
    # Devolvemos un expires_in alto para cumplir con clientes OAuth estrictos que esperan el campo.
    NEVER_EXPIRES = 86400 * 365 * 100  # 100 años — efectivamente permanente
    return JSONResponse({
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": NEVER_EXPIRES,
        "scope": "mcp",
    })


def _is_token_valid(token: str) -> bool:
    """Verifica si un token es válido: Bearer fijo o OAuth issued (vida permanente)."""
    if MCP_BEARER_TOKEN and token == MCP_BEARER_TOKEN:
        return True
    return _token_exists(token)


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
        "clients_registered": _count_clients(),
        "active_tokens": _count_tokens(),
        "db_path": _DB_PATH,
        "db_persistent": _DB_PATH.startswith("/data/"),
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
        middleware=[
            # CORS primero (responde a preflight OPTIONS y agrega headers a las respuestas)
            Middleware(
                CORSMiddleware,
                allow_origins=[
                    "https://claude.ai",
                    "https://*.claude.ai",
                    "https://api.anthropic.com",
                    "https://console.anthropic.com",
                    "https://platform.claude.com",
                ],
                allow_origin_regex=r"https://([a-z0-9-]+\.)*(claude\.ai|anthropic\.com|claude\.com)",
                allow_methods=["GET", "POST", "OPTIONS", "DELETE"],
                allow_headers=["*"],
                allow_credentials=True,
                expose_headers=["WWW-Authenticate", "Mcp-Session-Id"],
                max_age=86400,
            ),
            Middleware(AuthMiddleware),
        ],
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(make_app(), host="0.0.0.0", port=PORT, log_level="info", proxy_headers=True, forwarded_allow_ips="*")

