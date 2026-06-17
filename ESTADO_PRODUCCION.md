# MCP en Producción — Estado Actual

> **Última actualización**: 2026-05-14 (cierre de sesión MCP completo)
> **Estado**: ✅ FUNCIONANDO en Claude Web (Project + OAuth) y Claude Desktop (Bearer). Mobile pendiente de probar.

---

## 🔑 Credenciales (también en `SISTEMA_ACTIVO/.env`)

```
URL pública:        https://n8n-mcp-n8n.4043xj.easypanel.host
Endpoint SSE:       https://n8n-mcp-n8n.4043xj.easypanel.host/sse
Health (público):   https://n8n-mcp-n8n.4043xj.easypanel.host/healthz

Bearer Token:       RcdVFIGroBICFM3bkKC0dUFg1AGoXGy-qJyRqak5LZodydjdAwb076ewgZ0FINoa
Password OAuth:     Y8xBK5g@}%GK588Vk]!ZL\sm3
```

## 🏗️ Infraestructura

| Componente | Valor |
|---|---|
| Repo GitHub | https://github.com/techmystoredigital/mcp-oficina |
| Servicio EasyPanel | proyecto `n8n` → servicio `mcp_n8n` |
| Dominio | `n8n-mcp-n8n.4043xj.easypanel.host` (auto, SSL Let's Encrypt) |
| Puerto interno | 8080 |
| Auto-deploy | ✅ activo (cada push redeploya) |
| Recursos | 256 MB RAM, 0.25 vCPU |

## 📝 Variables de entorno en EasyPanel

| Variable | Función |
|---|---|
| `N8N_BASE` | URL de N8N: `https://n8n.yoanyandres.one` |
| `N8N_API_KEY` | API key de N8N (misma que `.env`) |
| `MCP_BEARER_TOKEN` | Token fijo para Claude Desktop / CLI |
| `MCP_AUTH_PASSWORD` | Password para aprobar OAuth (Claude Web/Mobile) |
| `MCP_PUBLIC_URL` | URL pública: `https://n8n-mcp-n8n.4043xj.easypanel.host` |
| `PORT` | `8080` |
| `OAUTH_DB_PATH` *(opcional)* | Ruta de la DB SQLite. Default `/data/oauth.db`. Solo cambiarlo si querés otra ubicación. |

## 💾 Persistencia OAuth (SQLite en volumen Docker)

Los clientes OAuth registrados y los access tokens emitidos se guardan en **`/data/oauth.db`** (SQLite). Esto es crítico:

- ✅ **Tokens NO expiran** — aprobás OAuth UNA SOLA VEZ y queda para siempre
- ✅ **Sobreviven a restarts del contenedor** (git push + Forzar reconstrucción, cambio de env vars, reinicio de la VPS)
- ✅ **3 tablas**: `oauth_clients`, `oauth_auth_codes` (TTL 5 min, solo para flow OAuth), `oauth_access_tokens` (permanentes)
- ⚠️ **Requiere volumen Docker `/data`** montado en EasyPanel — si no está montado, los datos viven en `oauth.db` dentro del contenedor (NO persistente). Verificar en healthz: `"db_persistent": true`.

### Cómo invalidar todos los tokens (revocación manual)

Hay 2 formas:

1. **Cambiar `MCP_AUTH_PASSWORD`** — esto NO invalida tokens viejos (siguen funcionando hasta que vos los borres), pero las conexiones NUEVAS van a requerir el password nuevo. Útil para evitar que se registren clientes nuevos sin que vos lo apruebes.
2. **Borrar filas de la DB** — entrá al contenedor por EasyPanel → Terminal → ejecutar:
   ```bash
   sqlite3 /data/oauth.db "DELETE FROM oauth_access_tokens;"
   ```
   Esto invalida TODOS los tokens emitidos. Próxima conexión requiere reaprobar OAuth.

### Para invalidar UN solo token (ej. PC robada)
```bash
sqlite3 /data/oauth.db "SELECT access_token, client_id, datetime(created_at, 'unixepoch') FROM oauth_access_tokens;"
# Identificá la fila → eliminala
sqlite3 /data/oauth.db "DELETE FROM oauth_access_tokens WHERE access_token = 'XXX';"
```

## 🔐 Sistema de autenticación dual

**Bearer Token fijo** (Claude Desktop / Claude Code CLI):
- Se manda en header `Authorization: Bearer <MCP_BEARER_TOKEN>`
- Configurado en `claude_desktop_config.json` del usuario

**OAuth 2.1 + DCR + PKCE** (Claude Web / Claude Mobile):
- Claude se registra dinámicamente vía `POST /register`
- Pide aprobación en `GET /authorize` → muestra form HTML con campo password
- Usuario pega `MCP_AUTH_PASSWORD` para aprobar
- Recibe `access_token` válido por 30 días vía `POST /token` (con PKCE S256)

## 🛠️ Tools expuestas (13)

| Categoría | Tools |
|---|---|
| **Disparar** | `estado_sistema`, `procesar_dia`, `cerrar_dia`, `ejecutar_script`, `procesar_chat_ia`, `conciliar_telegram` |
| **Inspección** | `listar_workflows`, `obtener_workflow`, `listar_ejecuciones`, `obtener_ejecucion`, `errores_recientes` |
| **Control** | `activar_workflow`, `desactivar_workflow` |

## 🧪 Verificación rápida

```bash
# Health (público, sin auth)
curl https://n8n-mcp-n8n.4043xj.easypanel.host/healthz
# → {"ok":true,"has_api_key":true,"has_bearer":true,"has_oauth":true}

# OAuth metadata (público)
curl https://n8n-mcp-n8n.4043xj.easypanel.host/.well-known/oauth-authorization-server

# SSE con Bearer (debe abrir streaming)
curl -H "Authorization: Bearer <TOKEN>" https://n8n-mcp-n8n.4043xj.easypanel.host/sse

# SSE sin auth (debe responder 401)
curl https://n8n-mcp-n8n.4043xj.easypanel.host/sse
```

## ⚙️ Config para clientes Claude

### Claude Desktop (Bearer)

`%APPDATA%\Claude\claude_desktop_config.json`:
```json
{
  "mcpServers": {
    "n8n-oficina": {
      "url": "https://n8n-mcp-n8n.4043xj.easypanel.host/sse",
      "headers": {
        "Authorization": "Bearer RcdVFIGroBICFM3bkKC0dUFg1AGoXGy-qJyRqak5LZodydjdAwb076ewgZ0FINoa"
      }
    }
  }
}
```

### Claude Web / Mobile (OAuth)

1. URL: `https://claude.ai/customize/connectors?modal=add-custom-connector`
2. Nombre: `n8n Oficina`
3. URL del servidor: `https://n8n-mcp-n8n.4043xj.easypanel.host/sse`
4. Configuración avanzada: dejar vacía
5. Al "Agregar" → Claude abre pantalla de aprobación → password: `Y8xBK5g@}%GK588Vk]!ZL\sm3`

## 🔄 Mantenimiento

| Tarea | Cómo |
|---|---|
| Ver logs | EasyPanel → servicio `mcp_n8n` → Registros |
| Reiniciar | EasyPanel → botón ↻ |
| **Modificar código** | Editar localmente → git push → **Forzar reconstrucción (🔧)** en EasyPanel. ⚠️ El autodeploy NO se dispara confiable. |
| Rollback | `git revert <commit>` → push → Forzar reconstrucción |
| Cambiar password OAuth | EasyPanel → Entorno → editar `MCP_AUTH_PASSWORD` → Guardar (auto-restart). Ver detalle abajo. |
| Rotar Bearer | Generar nuevo (`python -c "import secrets; print(secrets.token_urlsafe(48))"`), actualizar en EasyPanel + .env + claude_desktop_config.json |
| Apagar el MCP | EasyPanel → Stop. NO afecta otros servicios |

### 🔑 Detalle: cambiar el password OAuth (`MCP_AUTH_PASSWORD`)

El password sale **únicamente** de la env var `MCP_AUTH_PASSWORD` en EasyPanel. No está en el código.

**Pasos:**
1. EasyPanel → proyecto `n8n` → servicio `mcp_n8n` → pestaña **Entorno** (la captura de las 6 vars con `N8N_BASE`, `N8N_API_KEY`, `MCP_BEARER_TOKEN`, etc.)
2. Editar el valor de `MCP_AUTH_PASSWORD` por el nuevo
3. Guardar → EasyPanel reinicia el contenedor automáticamente (~10 s)
4. **Actualizar en paralelo** en estos lugares para no perder el nuevo password:
   - `SISTEMA_ACTIVO/.env` línea `MCP_AUTH_PASSWORD=`
   - Esta misma documentación (`ESTADO_PRODUCCION.md` sección "🔑 Credenciales")
5. **Reconectar clientes OAuth ya conectados** (Claude Web / Mobile): los tokens existentes seguirán funcionando hasta expirar (30 días). Para conexiones nuevas o reconexión, se va a pedir el nuevo password.

> ⚠️ **No requiere tocar Claude Desktop** — Desktop usa Bearer, no OAuth.
> ⚠️ **No requiere `git push`** — el password no está en el código, solo en env vars de EasyPanel.

> **Lección importante**: el botón "Implementar" verde solo reinicia el contenedor con la imagen vieja. **Para que un nuevo commit se aplique, hay que dar "Forzar reconstrucción"** (🔧 llave inglesa). Ver `TROUBLESHOOTING.md`.

## 🚀 Deploy de la persistencia OAuth a producción (pendiente)

Esto debe hacerse UNA vez para que la persistencia entre en efecto. Después queda funcionando siempre.

### Pre-requisito: configurar volumen en EasyPanel

1. EasyPanel → proyecto `n8n` → servicio `mcp_n8n` → pestaña **Volúmenes**
2. Agregar nuevo volumen:
   - **Path en el contenedor**: `/data`
   - **Tipo**: Volumen Docker (default)
   - **Nombre**: `mcp-oficina-data` (o el default que sugiera)
3. Guardar (NO reiniciar todavía)

### Hacer el deploy

```bash
git add mcp_server.py Dockerfile
git commit -m "OAuth: persistir clients/tokens en SQLite (sin expiración), fix nombre 'My Store Digital'"
git push origin main
```

Después en EasyPanel → servicio `mcp_n8n` → **Forzar reconstrucción** (🔧). Esperar ~1-2 min.

### Verificar que quedó bien

```bash
curl https://n8n-mcp-n8n.4043xj.easypanel.host/healthz
```

Respuesta esperada — agregadas 2 keys nuevas:
```json
{
  "ok": true,
  "clients_registered": 0,    // empieza vacío después del deploy
  "active_tokens": 0,          // empieza vacío también
  "db_path": "/data/oauth.db",
  "db_persistent": true        // ← CLAVE: debe ser true
}
```

> ⚠️ Si `db_persistent: false` → el volumen NO está montado. Revisar paso 1-3 del pre-requisito.

### Reaprobar OAuth UNA vez

Como la DB arranca vacía, las conexiones OAuth existentes en Claude Web/Mobile van a perder el acceso. **Reaprobá UNA vez**:

1. En Claude Web → Project → conector `n8n Oficina MCP` → "Conectar"
2. Pega `MCP_AUTH_PASSWORD` → aprobar
3. **Listo para siempre.** Verificá con `Listame los workflows activos en N8N`.

A partir de acá, podés hacer cuantos git push y `Forzar reconstrucción` quieras — el token va a sobrevivir.

> Claude Desktop **NO se ve afectado** — usa Bearer, no OAuth.

## 📜 Historial de commits relevantes

```
(pendiente) — OAuth: persistir clients/tokens en SQLite (sin expiración) + fix nombre "My Store Digital"
59d138c — Trigger redeploy (sin cambios funcionales)
3903d9e — Agregar CORS middleware para que Claude.ai pueda conectar desde el navegador
5bda49a — Agregar OAuth 2.1 + DCR + PKCE para soportar Claude Web/Mobile
8a7d832 — Quitar bearer token de DEPLOY.md (usar placeholder)
237dd56 — Initial commit: MCP server con SSE transport
```

> El cambio de nombre ya está aplicado en `mcp_server.py` local (3 ocurrencias). Falta `git push` + Forzar reconstrucción en EasyPanel para que se vea reflejado en la pantalla de OAuth.

## 📚 Archivos de documentación del MCP

| Archivo | Para qué |
|---|---|
| `ESTADO_PRODUCCION.md` (este) | Snapshot del estado actual + credenciales |
| `DEPLOY.md` | Guía paso a paso para deploy desde cero |
| `TROUBLESHOOTING.md` | Problemas comunes + cómo resolverlos (autodeploy, CORS, OAuth) |
| `mcp_server.py` | Código fuente del MCP (700 líneas, todo en uno) |
| `Dockerfile` | Imagen Docker Python 3.12 slim |
| `requirements.txt` | Deps: mcp, httpx, starlette, uvicorn |
| `.env.example` | Plantilla de variables |

## 🔓 Visibilidad del repo

- **Estado**: público (temporal, durante setup inicial)
- **Razón**: para que EasyPanel pueda clonarlo sin GitHub App configurada
- **Cuando pasar a privado**: una vez confirmado que todo funciona, ejecutar `gh repo edit techmystoredigital/mcp-oficina --visibility private`
- **Riesgo**: ninguno. El repo NO contiene secretos (todo viene de env vars de EasyPanel)

## ✅ Validaciones hechas hasta ahora

- [x] Code MCP funciona (Uvicorn corriendo en puerto 8080)
- [x] Bearer Token funciona (HTTP 401 sin auth, HTTP 200 con auth)
- [x] OAuth metadata responde (`has_oauth: true` en healthz)
- [x] DCR funciona (`POST /register` devuelve client_id)
- [x] CORS funciona (admin habilitó conector custom en la org)
- [x] **Claude Desktop conectado con Bearer** (mcp-remote como proxy, 10/10 tools no destructivas probadas)
- [x] **Claude Web conectado y funcional** (Project creado, OAuth aprobado, `listar_workflows` devolvió 26 workflows reales el 2026-05-14)
- [ ] Claude Mobile (pendiente probar — debería heredar OAuth de Web automáticamente al entrar al Project con la misma cuenta)
- [ ] 3 tools destructivas restantes (`procesar_dia`, `ejecutar_script`, `conciliar_telegram`) — probadas con datos reales

## ⚠️ Tareas pendientes (orden sugerido)

1. **Probar Claude Mobile** (mismo Project, misma cuenta) — solo abrir y mandar prompt de prueba
2. **Replicar setup en PC de la encargada** — ver sección "Setup nuevas PCs" más abajo
3. **Desactivar workflows residuales `_TEST_*` / `_TEMP_*`** — 5 workflows que quedaron activos del setup inicial:
   - `EPj7wBYQXGra8jFf` (_tmp_full_tools)
   - `OfBdiJFT5GaYu2BG` (_TEST_xls_FINAL)
   - `WBeKoooqoVpoj74C` (_TEST_WRITE_MIN)
   - `ZKzqKh52AUKcdwt8` (_TEMP_DELETE_FILE)
   - `wbRjYJGMCFHaA1fO` (_TEMP_CLEAR_SHEET)
4. **Probar las 3 tools destructivas restantes** con datos reales
5. **Volver repo a privado** (`gh repo edit techmystoredigital/mcp-oficina --visibility private`) con GitHub App de EasyPanel configurada

## 🖥️ Setup en nuevas PCs (encargada, etc.)

Web y Desktop son **independientes**: aprobar OAuth en Web NO conecta Desktop, y viceversa.

### Opción A — Solo Claude Web (2 min, recomendada si no necesita Desktop)
1. Que la persona inicie sesión en claude.ai con su cuenta de la org
2. Admin invita su cuenta al Project que tiene el conector configurado
3. Primera vez que abre el Project → click "Conectar" en `n8n Oficina MCP` → pega `MCP_AUTH_PASSWORD` → aprueba
4. Listo. No instala nada local. El móvil de esa misma cuenta hereda la conexión.

### Opción B — Claude Desktop (10-15 min, solo si lo necesita SÍ o SÍ)
1. Instalar **Node.js LTS** (nodejs.org)
2. Crear/editar `%APPDATA%\Claude\claude_desktop_config.json` con este contenido:
   ```json
   {
     "mcpServers": {
       "n8n-oficina": {
         "command": "npx",
         "args": ["-y", "mcp-remote", "https://n8n-mcp-n8n.4043xj.easypanel.host/sse",
                  "--header", "Authorization:${AUTH_HEADER}"],
         "env": {
           "AUTH_HEADER": "Bearer RcdVFIGroBICFM3bkKC0dUFg1AGoXGy-qJyRqak5LZodydjdAwb076ewgZ0FINoa"
         }
       }
     }
   }
   ```
3. Cerrar Claude Desktop completo (incluido bandeja del sistema) y volver a abrir
4. Primera vez Windows preguntará "¿Permitir que Node.js acceda a la red?" → **Sí**
5. Verificar: en un chat nuevo, escribir `Listame los workflows activos en N8N`. Debe responder con datos reales.

> ⚠️ Si el JSON ya existe con otra config, NO sobreescribir — fusionar agregando solo el objeto `mcpServers`.

---

## FIN
