"""Servidor MCP HTTP (Streamable + SSE) para que Claude (Desktop, Web, Mobile)
controle N8N de Mine Store de forma remota.

Diferencias con `n8n_oficina.py`:
- Usa transport HTTP (Streamable + SSE) en lugar de stdio → accesible vía URL
- Autenticación con Bearer token
- Tools extendidas: ahora puede LEER el estado de N8N (workflows, ejecuciones,
  errores) además de disparar workflows
- Pensado para deploy en VPS (EasyPanel) como servicio Docker

Variables de entorno requeridas (vienen del .env del servicio en EasyPanel):
    N8N_BASE         : URL base de N8N (ej. https://n8n.yoanyandres.one)
    N8N_API_KEY      : API key de N8N
    MCP_BEARER_TOKEN : token random que Claude debe enviar en Authorization

Puerto: 8080 (configurable por env PORT)
Endpoints:
    GET  /healthz                  → health check (público)
    POST /mcp                      → endpoint principal MCP (Streamable HTTP)
    GET  /sse                      → endpoint SSE legacy
"""
from __future__ import annotations
import asyncio
import json
import os
from contextlib import asynccontextmanager
from typing import Any

import httpx
from mcp.server import Server
from mcp.server.sse import SseServerTransport
from mcp.types import Tool, TextContent
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route

# =========================================================================
# CONFIG (todo viene de variables de entorno)
# =========================================================================
N8N_BASE = os.environ.get("N8N_BASE", "https://n8n.yoanyandres.one").rstrip("/")
N8N_API_KEY = os.environ.get("N8N_API_KEY", "")
MCP_BEARER_TOKEN = os.environ.get("MCP_BEARER_TOKEN", "")
PORT = int(os.environ.get("PORT", "8080"))

if not N8N_API_KEY:
    print("WARN: N8N_API_KEY no configurada — los tools de inspeccion no funcionaran")
if not MCP_BEARER_TOKEN:
    print("WARN: MCP_BEARER_TOKEN vacio — el servidor aceptara cualquier llamada (NO USAR EN PROD)")

# =========================================================================
# HTTP CLIENT helper
# =========================================================================
_HTTP_TIMEOUT = httpx.Timeout(connect=10, read=300, write=30, pool=30)


async def n8n_api_get(path: str) -> dict:
    """GET a la API REST de N8N."""
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT, verify=True) as c:
        r = await c.get(
            f"{N8N_BASE}/api/v1{path}",
            headers={"X-N8N-API-KEY": N8N_API_KEY},
        )
        r.raise_for_status()
        return r.json()


async def n8n_api_post(path: str, body: dict | None = None) -> dict:
    """POST a la API REST de N8N."""
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT, verify=True) as c:
        r = await c.post(
            f"{N8N_BASE}/api/v1{path}",
            headers={"X-N8N-API-KEY": N8N_API_KEY, "Content-Type": "application/json"},
            json=body or {},
        )
        return {"status": r.status_code, "body": r.text[:2000]}


async def n8n_webhook(path: str, body: dict | None = None, method: str = "POST") -> str:
    """Llama a un webhook de N8N (POST por defecto, GET si method='GET')."""
    url = f"{N8N_BASE}/webhook/{path.lstrip('/')}"
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT, verify=True) as c:
        if method.upper() == "GET":
            r = await c.get(url)
        else:
            r = await c.post(url, json=body or {})
        return f"HTTP {r.status_code}\n{r.text[:4000]}"


# =========================================================================
# MCP SERVER y TOOLS
# =========================================================================
server: Server = Server("n8n-oficina-mcp")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        # === Disparadores de workflows ===
        Tool(
            name="estado_sistema",
            description="Consulta el estado del sistema de conciliacion (metricas de hoy, salud).",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="procesar_dia",
            description="Dispara el workflow MASTER que ejecuta Scripts 1+2+3 (recargas, bancos, conciliacion). Tarda 1-3 min. Genera paso1, paso2, paso3 en OUTPUTS del Drive.",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="cerrar_dia",
            description="Dispara Workflow D (genera paso4 final). Cruza paso3 con chat de Telegram del dia anterior. Opcionalmente acepta chat_data del parser.",
            inputSchema={
                "type": "object",
                "properties": {
                    "chat_data": {
                        "type": "array",
                        "description": "Opcional: array de transacciones (monto, banco, fecha, terminacion, tipo)",
                        "items": {"type": "object"},
                    }
                },
            },
        ),
        Tool(
            name="ejecutar_script",
            description="Dispara Script 1 (Recargas), 2 (Bancos) o 3 (Conciliacion) individualmente.",
            inputSchema={
                "type": "object",
                "properties": {"script": {"type": "integer", "enum": [1, 2, 3]}},
                "required": ["script"],
            },
        ),
        Tool(
            name="procesar_chat_ia",
            description="Llama al workflow Procesar Chat que usa Gemini para extraer transacciones de una conversacion textual.",
            inputSchema={
                "type": "object",
                "properties": {
                    "cliente": {"type": "string"},
                    "fecha": {"type": "string", "description": "YYYY-MM-DD"},
                    "conversacion": {"type": "string"},
                },
                "required": ["cliente", "fecha", "conversacion"],
            },
        ),
        Tool(
            name="conciliar_telegram",
            description="Dispara el workflow Conciliacion_Telegram con chat_data (lista de transacciones extraidas).",
            inputSchema={
                "type": "object",
                "properties": {
                    "chat_data": {"type": "array", "items": {"type": "object"}},
                },
                "required": ["chat_data"],
            },
        ),
        # === Inspeccion de N8N (lectura) ===
        Tool(
            name="listar_workflows",
            description="Lista todos los workflows de N8N (id, nombre, activo).",
            inputSchema={
                "type": "object",
                "properties": {
                    "filtro_nombre": {"type": "string", "description": "Opcional: substring a buscar en el nombre"},
                    "solo_activos": {"type": "boolean", "default": False},
                },
            },
        ),
        Tool(
            name="obtener_workflow",
            description="Devuelve la definicion completa de un workflow (nodos, conexiones, settings).",
            inputSchema={
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
            },
        ),
        Tool(
            name="listar_ejecuciones",
            description="Lista las ultimas ejecuciones (status: success, error, running). Por defecto 10.",
            inputSchema={
                "type": "object",
                "properties": {
                    "workflow_id": {"type": "string", "description": "Opcional: filtrar por workflow"},
                    "limit": {"type": "integer", "default": 10},
                    "status": {"type": "string", "enum": ["success", "error", "running", "waiting"]},
                },
            },
        ),
        Tool(
            name="obtener_ejecucion",
            description="Devuelve el detalle de una ejecucion (con runData de cada nodo). Util para debug.",
            inputSchema={
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "include_data": {"type": "boolean", "default": True},
                },
                "required": ["id"],
            },
        ),
        Tool(
            name="errores_recientes",
            description="Lista las ejecuciones con error de las ultimas 24 horas, con el mensaje de error y el nodo donde fallo.",
            inputSchema={
                "type": "object",
                "properties": {"limit": {"type": "integer", "default": 20}},
            },
        ),
        # === Control de workflows ===
        Tool(
            name="activar_workflow",
            description="Activa un workflow en N8N (lo enciende para que reciba triggers).",
            inputSchema={
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
            },
        ),
        Tool(
            name="desactivar_workflow",
            description="Desactiva un workflow en N8N.",
            inputSchema={
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict | None = None) -> list[TextContent]:
    arguments = arguments or {}
    try:
        # ====== Disparadores ======
        if name == "estado_sistema":
            return [TextContent(type="text", text=await n8n_webhook("estado-bot", method="GET"))]

        if name == "procesar_dia":
            r = await n8n_webhook("exec-master-procesar")
            return [TextContent(type="text", text=f"MASTER disparado.\n{r}\n\nVerifica OUTPUTS en 2-5 min.")]

        if name == "cerrar_dia":
            body = {"chat_data": arguments["chat_data"]} if arguments.get("chat_data") else {}
            r = await n8n_webhook("exec-script4", body)
            return [TextContent(type="text", text=f"Workflow D disparado.\n{r}")]

        if name == "ejecutar_script":
            s = arguments.get("script")
            if s not in (1, 2, 3):
                return [TextContent(type="text", text="ERROR: script debe ser 1, 2 o 3")]
            r = await n8n_webhook(f"exec-script{s}")
            return [TextContent(type="text", text=f"Script {s} disparado.\n{r}")]

        if name == "procesar_chat_ia":
            body = {
                "clients": [
                    {
                        "cliente": arguments.get("cliente"),
                        "fecha": arguments.get("fecha"),
                        "conversacion": arguments.get("conversacion"),
                    }
                ]
            }
            r = await n8n_webhook("procesar-chat", body)
            return [TextContent(type="text", text=r)]

        if name == "conciliar_telegram":
            body = {"chat_data": arguments["chat_data"]}
            r = await n8n_webhook("exec-conciliacion-telegram", body)
            return [TextContent(type="text", text=r)]

        # ====== Inspeccion ======
        if name == "listar_workflows":
            data = await n8n_api_get("/workflows?limit=200")
            wfs = data.get("data", [])
            filtro = (arguments.get("filtro_nombre") or "").lower()
            solo_activos = arguments.get("solo_activos", False)
            out = []
            for w in wfs:
                if solo_activos and not w.get("active"):
                    continue
                if filtro and filtro not in (w.get("name", "")).lower():
                    continue
                out.append(f"  {w['id']:25} active={str(w.get('active','?')):<5} {w.get('name','?')}")
            return [TextContent(type="text", text=f"Total: {len(out)}\n" + "\n".join(out))]

        if name == "obtener_workflow":
            wid = arguments.get("id")
            data = await n8n_api_get(f"/workflows/{wid}")
            # Resumir: nombre, active, lista de nodos
            nodos = data.get("nodes", [])
            resumen = {
                "id": data.get("id"),
                "name": data.get("name"),
                "active": data.get("active"),
                "updatedAt": data.get("updatedAt"),
                "nodes_count": len(nodos),
                "node_types": list({n.get("type", "?") for n in nodos}),
                "nodes": [{"name": n.get("name"), "type": n.get("type")} for n in nodos],
            }
            return [TextContent(type="text", text=json.dumps(resumen, indent=2, ensure_ascii=False))]

        if name == "listar_ejecuciones":
            wid = arguments.get("workflow_id")
            limit = arguments.get("limit", 10)
            status = arguments.get("status")
            qs = f"?limit={limit}"
            if wid:
                qs += f"&workflowId={wid}"
            if status:
                qs += f"&status={status}"
            data = await n8n_api_get(f"/executions{qs}")
            execs = data.get("data", [])
            out = []
            for e in execs:
                out.append(
                    f"  id={e.get('id')} status={e.get('status','?'):<10} "
                    f"started={e.get('startedAt','?')} workflowId={e.get('workflowId','?')}"
                )
            return [TextContent(type="text", text=f"Total: {len(out)}\n" + "\n".join(out))]

        if name == "obtener_ejecucion":
            eid = arguments.get("id")
            include = arguments.get("include_data", True)
            data = await n8n_api_get(f"/executions/{eid}?includeData={str(include).lower()}")
            # Resumir: estado + errores por nodo si los hay
            ed = data.get("data")
            if isinstance(ed, str):
                try:
                    ed = json.loads(ed)
                except Exception:
                    ed = {}
            ed = ed or {}
            run_data = ed.get("resultData", {}).get("runData", {})
            nodos = []
            for nname, runs in run_data.items():
                for r in runs:
                    err = r.get("error")
                    nodos.append(
                        {
                            "node": nname,
                            "error": err.get("message", "?") if err else None,
                            "executionStatus": r.get("executionStatus"),
                        }
                    )
            out = {
                "id": data.get("id"),
                "status": data.get("status"),
                "startedAt": data.get("startedAt"),
                "stoppedAt": data.get("stoppedAt"),
                "workflowId": data.get("workflowId"),
                "nodes": nodos[:50],
            }
            return [TextContent(type="text", text=json.dumps(out, indent=2, ensure_ascii=False, default=str))]

        if name == "errores_recientes":
            limit = arguments.get("limit", 20)
            data = await n8n_api_get(f"/executions?status=error&limit={limit}")
            execs = data.get("data", [])
            out = []
            for e in execs:
                out.append(
                    f"  id={e.get('id')} workflowId={e.get('workflowId')} "
                    f"started={e.get('startedAt')}"
                )
            return [TextContent(type="text", text=f"Errores: {len(out)}\n" + "\n".join(out))]

        # ====== Control ======
        if name == "activar_workflow":
            wid = arguments.get("id")
            r = await n8n_api_post(f"/workflows/{wid}/activate")
            return [TextContent(type="text", text=f"Activar {wid}: {r}")]

        if name == "desactivar_workflow":
            wid = arguments.get("id")
            r = await n8n_api_post(f"/workflows/{wid}/deactivate")
            return [TextContent(type="text", text=f"Desactivar {wid}: {r}")]

        return [TextContent(type="text", text=f"Tool desconocida: {name}")]

    except httpx.HTTPStatusError as e:
        return [TextContent(type="text", text=f"HTTP error: {e.response.status_code} {e.response.text[:500]}")]
    except Exception as e:
        return [TextContent(type="text", text=f"ERROR: {type(e).__name__}: {e}")]


# =========================================================================
# STARLETTE APP con autenticacion Bearer
# =========================================================================
sse_transport = SseServerTransport("/messages/")


class BearerAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # Endpoint publico de health
        if request.url.path in ("/healthz", "/"):
            return await call_next(request)

        if MCP_BEARER_TOKEN:
            auth = request.headers.get("authorization", "")
            if not auth.startswith("Bearer "):
                return JSONResponse({"error": "missing_bearer"}, status_code=401)
            token = auth[len("Bearer ") :].strip()
            if token != MCP_BEARER_TOKEN:
                return JSONResponse({"error": "invalid_token"}, status_code=401)

        return await call_next(request)


async def handle_sse(request: Request):
    async with sse_transport.connect_sse(
        request.scope, request.receive, request._send
    ) as streams:
        await server.run(streams[0], streams[1], server.create_initialization_options())


async def healthz(request: Request):
    return JSONResponse(
        {
            "ok": True,
            "service": "mcp-oficina",
            "n8n_base": N8N_BASE,
            "has_api_key": bool(N8N_API_KEY),
            "has_bearer": bool(MCP_BEARER_TOKEN),
        }
    )


def make_app() -> Starlette:
    return Starlette(
        debug=False,
        routes=[
            Route("/healthz", endpoint=healthz, methods=["GET"]),
            Route("/", endpoint=healthz, methods=["GET"]),
            Route("/sse", endpoint=handle_sse, methods=["GET"]),
            Mount("/messages/", app=sse_transport.handle_post_message),
        ],
        middleware=[Middleware(BearerAuthMiddleware)],
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(make_app(), host="0.0.0.0", port=PORT, log_level="info")
