# Deploy del MCP en VPS (EasyPanel)

Guía paso a paso para que **Claude (Desktop, Web, Móvil)** pueda controlar el sistema de conciliación llamando al MCP que corre en el VPS.

---

## Resumen de la arquitectura

```
Claude (Desktop/Web/Móvil)
        │  HTTPS + Bearer Token
        ▼
mcp.yoanyandres.one        ← subdominio nuevo
        │  Reverse proxy EasyPanel (SSL Let's Encrypt)
        ▼
Servicio "mcp" en EasyPanel (Docker)
        │  HTTP interno
        ▼
n8n.yoanyandres.one (API REST + webhooks)
        │
        ▼
Workflows + Google Drive + Sheets + Gemini
```

**No toca nada de lo que ya está corriendo.** El MCP es un servicio nuevo, aislado, en su propio contenedor Docker.

---

## Archivos preparados (en SISTEMA_ACTIVO/04_mcp_local/)

| Archivo | Función |
|---|---|
| `mcp_server.py` | Servidor MCP con SSE + 13 tools (disparar workflows, ver estado, inspeccionar ejecuciones, manejar errores) |
| `Dockerfile` | Imagen Docker (Python 3.12 slim, ~150 MB) |
| `requirements.txt` | Deps Python (mcp, httpx, starlette, uvicorn) |
| `.env.example` | Plantilla de variables — los valores reales se pegan en EasyPanel |

---

## Tools que el MCP expone a Claude

| Tool | Qué hace |
|---|---|
| `estado_sistema` | Consulta `/webhook/estado-bot` (métricas del día) |
| `procesar_dia` | Dispara MASTER (Scripts 1+2+3) |
| `cerrar_dia` | Dispara Workflow D (paso4) |
| `ejecutar_script` | Dispara Script 1, 2 o 3 individualmente |
| `procesar_chat_ia` | Llama a `Conciliacion_PROCESAR_CHAT` con Gemini |
| `conciliar_telegram` | Dispara `Conciliacion_Telegram` con chat_data |
| `listar_workflows` | Lista todos los workflows (con filtros) |
| `obtener_workflow` | Devuelve la definición de un workflow |
| `listar_ejecuciones` | Lista ejecuciones (con filtro por status/workflow) |
| `obtener_ejecucion` | Detalle de una ejecución (errores por nodo) |
| `errores_recientes` | Ejecuciones con error en las últimas N |
| `activar_workflow` / `desactivar_workflow` | Control on/off |

---

## PASO 1 — Subir el código a GitHub

Como hacemos con N8N (que ya usa `techmystoredigital/n8n-image-mystore`), creamos un repo similar para el MCP.

### 1.1 Crear el repo

1. Andá a https://github.com/techmystoredigital (o tu organización)
2. **New repository**
3. Nombre: `mcp-oficina` (o el que prefieras)
4. **Privado** (recomendado, porque tendrá referencias internas)
5. Initialize: vacío (sin README)
6. Crear

### 1.2 Subir los archivos

Desde tu PC, dentro de `SISTEMA_ACTIVO/04_mcp_local/`:

```bash
cd "c:/Users/Novalion/Desktop/OFICINA/1.Conciliacion/SISTEMA_ACTIVO/04_mcp_local"
git init
git remote add origin https://github.com/techmystoredigital/mcp-oficina.git
git add Dockerfile requirements.txt mcp_server.py DEPLOY.md .env.example
git commit -m "Initial commit: MCP server para N8N"
git branch -M main
git push -u origin main
```

(Si te pide credenciales, usá tu token de GitHub.)

**No subas `.env`** con valores reales. Solo `.env.example`.

---

## PASO 2 — Agregar DNS para `mcp.yoanyandres.one`

### Donde tengas el DNS de `yoanyandres.one` (Cloudflare, etc.)

Crear un registro **A** o **CNAME** apuntando al mismo IP que `n8n.yoanyandres.one`.

**Opción A (más simple) — CNAME**:
- Tipo: **CNAME**
- Nombre: `mcp`
- Valor: `n8n.yoanyandres.one`
- TTL: Auto (o 300)

**Opción B — A**:
- Tipo: **A**
- Nombre: `mcp`
- Valor: (la misma IP que tiene `n8n.yoanyandres.one`)
- TTL: Auto

Esperá 1-5 min a que propague. Podés verificar con: `nslookup mcp.yoanyandres.one`.

---

## PASO 3 — Crear el servicio en EasyPanel

### 3.1 Entrar al proyecto `n8n`

En EasyPanel, click en el proyecto **n8n** (al lado del servicio n8n actual).

### 3.2 Crear el servicio

1. Click en el botón **+** (al lado del nombre del proyecto)
2. Elegir **App** (no Postgres, no Redis)
3. Nombre: `mcp` (queda como `n8n_mcp` internamente)
4. **Create**

### 3.3 Configurar fuente

En el nuevo servicio `mcp`:
- Source → **Git**
- **Owner**: `techmystoredigital`
- **Repository**: `mcp-oficina`
- **Reference**: `main`
- **Path**: `/` (raíz)
- **Build**: **Dockerfile** (path: `Dockerfile`)
- **Auto deploy**: ✅ (que se redepoye al hacer push)

### 3.4 Variables de entorno

Pegar exactamente esto (reemplazar los valores entre `<>`):

```
N8N_BASE=https://n8n.yoanyandres.one
N8N_API_KEY=<copiar la N8N_API_KEY de SISTEMA_ACTIVO/.env>
MCP_BEARER_TOKEN=<token generado abajo>
PORT=8080
```

**Token Bearer**: generar uno único para tu instalación (mismo token va en EasyPanel y en la config de Claude):

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Copialo y pegalo en la env var `MCP_BEARER_TOKEN`. **Nunca lo subas a Git.**

### 3.5 Recursos (Settings → Resources)

- **CPU**: 0.25 vCPU (suficiente, ajustable)
- **Memoria**: 256 MB (más que suficiente, ajustable)

### 3.6 Dominio (Settings → Domains)

- **Add domain**: `mcp.yoanyandres.one`
- **HTTPS**: ✅
- **Port**: `8080`
- **Path**: `/`
- **Internal protocol**: `http`

Guardar. EasyPanel pide el certificado SSL automáticamente.

### 3.7 Deploy

Click en **Deploy**. Esperar 1-3 min mientras construye la imagen.

---

## PASO 4 — Verificar que funciona

### 4.1 Health check (sin auth)

```bash
curl https://mcp.yoanyandres.one/healthz
```

Debe responder:
```json
{"ok": true, "service": "mcp-oficina", "n8n_base": "https://n8n.yoanyandres.one", "has_api_key": true, "has_bearer": true}
```

### 4.2 Test sin token (debe fallar 401)

```bash
curl -i https://mcp.yoanyandres.one/sse
```

→ debe devolver `HTTP 401 {"error":"missing_bearer"}`. Esto confirma que la autenticación funciona.

### 4.3 Test con token

```bash
curl -i -H "Authorization: Bearer RcdVFIGroBICFM3bkKC0dUFg1AGoXGy-qJyRqak5LZodydjdAwb076ewgZ0FINoa" https://mcp.yoanyandres.one/sse
```

→ debe quedar abierto (SSE) o devolver eventos.

---

## PASO 5 — Conectar Claude Desktop

### Editar `claude_desktop_config.json`

Ubicación: `%APPDATA%\Claude\claude_desktop_config.json`

Agregar dentro de `"mcpServers"` (reemplazá `<TOKEN>` por el bearer real):

```json
{
  "mcpServers": {
    "n8n-oficina": {
      "url": "https://<TU-DOMINIO-MCP>/sse",
      "headers": {
        "Authorization": "Bearer <TOKEN>"
      }
    }
  }
}
```

Reiniciar Claude Desktop.

### En Claude Web / Móvil

Settings → MCP → Add server:
- URL: `https://mcp.yoanyandres.one/sse`
- Authentication: Bearer token (pegar el token)

---

## Mantenimiento

- **Logs**: EasyPanel → servicio `mcp` → Logs
- **Reiniciar**: EasyPanel → servicio `mcp` → Restart
- **Actualizar código**: git push al repo `mcp-oficina` → EasyPanel autodespliega (si Auto deploy ✅)
- **Cambiar recursos**: Settings → Resources
- **Cambiar token**: editar env var `MCP_BEARER_TOKEN` en EasyPanel → restart → actualizar en Claude

## Si algo falla

1. Mirar logs del servicio `mcp` en EasyPanel
2. `curl /healthz` para confirmar que está vivo
3. Si dice `has_bearer: false` → falta MCP_BEARER_TOKEN en env
4. Si dice `has_api_key: false` → falta N8N_API_KEY en env

## FIN
