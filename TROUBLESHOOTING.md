# MCP Troubleshooting — lecciones aprendidas

> Problemas que aparecieron durante el deploy inicial y cómo se resolvieron.
> Si el sistema se rompe o querés modificar algo, revisá acá primero.

---

## 1. Auto-deploy de EasyPanel NO se dispara con `git push`

**Síntoma**: hacés `git push` con cambios al código, pero EasyPanel no rebuilda. El servicio sigue corriendo la versión vieja. En "Historial de implementaciones" no aparece deploy nuevo.

**Causa**: el webhook GitHub → EasyPanel falla en silencio (o no está conectado correctamente).

**Solución**:
- ❌ **"Implementar"** del botón verde: solo reinicia el contenedor con la imagen vieja. NO rebuilda.
- ✅ **"Forzar reconstrucción"** (🔧 llave inglesa en la fila de íconos): SÍ rebuilda desde cero usando el último commit del repo.

→ Cada vez que hagas push, dale **Forzar reconstrucción** en EasyPanel.

**Tiempo del rebuild**:
- Primer build (sin cache): ~3-5 min
- Builds siguientes (cache poblado): ~30-60s

---

## 2. Claude.ai Web da error "Missing permissions" al agregar conector

**Síntoma**: el formulario "Agregar conector personalizado" se llena con URL del MCP, pero al darle "Agregar" muestra:
- "Missing permissions"
- "Error al agregar el conector"

**Causa A — CORS faltante** (lo más común): el servidor MCP no envía headers CORS y Claude.ai (que corre en el navegador) no puede hacer requests cross-origin.

**Solución A**: el `mcp_server.py` debe tener `CORSMiddleware` de Starlette permitiendo orígenes `https://claude.ai` y `*.anthropic.com`. Verificá con:

```bash
curl -i "https://<TU_MCP>/healthz" -H "Origin: https://claude.ai" | grep -i "access-control"
# Debe responder: Access-Control-Allow-Origin: https://claude.ai
```

**Causa B — Cache del navegador o intento previo fallido**: una vez que probás con un MCP que falla, Claude.ai puede guardar ese resultado en cache y no reintentar limpiamente.

**Solución B**:
- Probar en **ventana incógnito** (Ctrl+Shift+N)
- O cambiar el **nombre del conector** (no usar el mismo que un intento anterior)
- O cerrar/abrir el modal completamente

**Causa C — Probar URL del root vs URL del endpoint SSE**:

Algunos clientes esperan la URL raíz del servidor (`https://...`) y descubren endpoints vía metadata. Otros esperan la URL del endpoint SSE específico (`https://.../sse`).

**Solución C**: probar ambas URLs:
- `https://n8n-mcp-n8n.4043xj.easypanel.host` (root — recomendado, deja que Claude descubra)
- `https://n8n-mcp-n8n.4043xj.easypanel.host/sse` (explícito)

**Causa D — Streamable HTTP vs SSE**: Claude.ai en versiones recientes prefiere `Streamable HTTP` (endpoint único `POST /mcp` con `Accept: text/event-stream`). SSE clásico puede no ser suficiente.

**Solución D**: agregar endpoint Streamable HTTP al server. Ver sección 13 al final.

---

## 3. Claude Web NO acepta Bearer Token (solo OAuth)

**Hecho**: la UI de Claude.ai para custom connectors **NO acepta Bearer tokens**. Solo soporta OAuth 2.1 + DCR + PKCE.

**Implicación**: el MCP debe implementar:
- `GET /.well-known/oauth-authorization-server` (metadata)
- `POST /register` (Dynamic Client Registration RFC 7591)
- `GET/POST /authorize` (UI de aprobación)
- `POST /token` (intercambio de code por access_token con PKCE)

**Claude Desktop SÍ acepta Bearer**: por eso el código mantiene compatibilidad dual.

---

## 4. Endpoints OAuth devuelven 401 cuando deberían ser públicos

**Síntoma**: `GET /.well-known/oauth-authorization-server` devuelve `{"error":"missing_bearer"}`.

**Causa**: el middleware de auth está bloqueando esos endpoints por defecto.

**Solución**: en `mcp_server.py`, el set `_PUBLIC_PATHS` debe incluir:
```python
_PUBLIC_PATHS = {
    "/", "/healthz",
    "/.well-known/oauth-authorization-server",
    "/.well-known/oauth-protected-resource",
    "/register", "/authorize", "/token",
}
```

---

## 5. El dominio EasyPanel apunta al puerto interno equivocado

**Síntoma**: `/healthz` devuelve HTML "Not Found" en lugar del JSON esperado.

**Causa**: EasyPanel mapeó el dominio al **puerto 80** del contenedor (default), pero el MCP corre en **8080**.

**Solución**: EasyPanel → servicio → Dominios → editar → cambiar **Puerto** a `8080`.

---

## 6. OAuth metadata devuelve URLs HTTP en lugar de HTTPS

**Síntoma**: el `issuer` en `/.well-known/oauth-authorization-server` aparece como `http://...` (no `https://...`).

**Causa**: detrás del reverse proxy, uvicorn no detecta que la conexión externa es HTTPS.

**Solución**:
- En `make_app()` / uvicorn: agregar `proxy_headers=True, forwarded_allow_ips="*"`
- En env vars de EasyPanel: definir `MCP_PUBLIC_URL=https://<tu-dominio>` para forzar la URL pública

---

## 7. Cómo modificar el código del MCP

```bash
# 1. Editar localmente
cd c:/Users/Novalion/Desktop/OFICINA/1.Conciliacion/SISTEMA_ACTIVO/04_mcp_local
# ... editar mcp_server.py ...

# 2. Verificar sintaxis
py -c "import ast; ast.parse(open('mcp_server.py', encoding='utf-8').read()); print('OK')"

# 3. Push
git add mcp_server.py
git commit -m "<mensaje>"
git push

# 4. En EasyPanel: click "Forzar reconstrucción" (🔧 llave inglesa)
# El auto-deploy no se dispara fiable. Mejor manual.

# 5. Verificar después del rebuild (~2-3 min)
curl https://n8n-mcp-n8n.4043xj.easypanel.host/healthz
```

---

## 8. Cómo agregar una tool nueva al MCP

En `mcp_server.py`:

```python
# En @server.list_tools(), agregar:
Tool(name="mi_tool_nueva",
     description="Lo que hace",
     inputSchema={"type": "object", "properties": {
         "param1": {"type": "string"}
     }, "required": ["param1"]}),

# En @server.call_tool(), agregar:
if name == "mi_tool_nueva":
    p = arguments.get("param1")
    # ... hacer la llamada que sea ...
    return [TextContent(type="text", text="resultado")]
```

Después: commit + push + **Forzar reconstrucción** en EasyPanel.

Claude Desktop la ve la próxima vez que reinicia. Claude Web la ve automáticamente.

---

## 9. Cómo rotar la password OAuth

1. Generar una nueva: `python -c "import secrets; print(secrets.token_urlsafe(32))"`
2. EasyPanel → servicio `mcp_n8n` → **Entorno** → editar `MCP_AUTH_PASSWORD` → Guardar (auto-restart)
3. Actualizar en `SISTEMA_ACTIVO/.env` → variable `MCP_AUTH_PASSWORD`
4. Los conectores Claude existentes **siguen funcionando** (sus access_tokens son válidos hasta 30 días)
5. Cuando un cliente nuevo se conecte, va a pedir la nueva password

---

## 10. Cómo replicar este MCP para otra organización

1. Crear repo nuevo en GitHub (público temporal o conectar GitHub App)
2. Copiar archivos de `04_mcp_local/` (`mcp_server.py`, `Dockerfile`, `requirements.txt`, `.gitignore`)
3. Crear servicio "App" en EasyPanel (o el VPS que sea)
4. Configurar Source = Git + repo nuevo
5. Configurar Build = Dockerfile
6. Variables de entorno:
   - `N8N_BASE`, `N8N_API_KEY` → del N8N destino
   - `MCP_BEARER_TOKEN` → generar random
   - `MCP_AUTH_PASSWORD` → generar random fuerte
   - `MCP_PUBLIC_URL` → URL pública del subdominio
   - `PORT=8080`
7. Dominio: asignar + puerto interno 8080
8. Implementar (o Forzar reconstrucción)
9. Verificar `curl <url>/healthz`
10. Agregar conector en cada cliente Claude

Tiempo total: ~30 min si todo va bien.

---

## 11. ¿Cuándo pasar el repo a privado?

**Recomendación**: cuando todo funcione end-to-end y esté validado en Claude Web/Desktop.

```bash
gh repo edit techmystoredigital/mcp-oficina --visibility private
```

⚠️ **Cuidado**: si el repo es privado, EasyPanel necesita autenticación GitHub para clonar. Hay que conectar la **GitHub App de EasyPanel** antes de hacerlo privado, o el próximo `Forzar reconstrucción` va a fallar.

### Conectar GitHub App de EasyPanel

EasyPanel → Settings/Configuración (engranaje) → GitHub → Install App → seleccionar `techmystoredigital` → Authorize.

---

## 12.1. Claude Desktop NO acepta `url` + `headers` directo

**Síntoma**: error popup "No se pudieron cargar algunos servidores MCP. Las siguientes entradas en claude_desktop_config.json no son configuraciones válidas".

**Causa**: Claude Desktop **no soporta** el formato remoto `url + headers` directamente. Solo soporta MCP locales (stdio) o vía un proxy local.

**Solución**: usar `mcp-remote` (paquete npm) como proxy local. Requiere Node.js + npx instalados.

Sintaxis correcta (con workaround de bug de espacios en Windows):

```json
{
  "mcpServers": {
    "n8n-oficina": {
      "command": "npx",
      "args": [
        "-y",
        "mcp-remote",
        "https://n8n-mcp-n8n.4043xj.easypanel.host/sse",
        "--header",
        "Authorization:${AUTH_HEADER}"
      ],
      "env": {
        "AUTH_HEADER": "Bearer <TOKEN>"
      }
    }
  }
}
```

> **CRÍTICO en Windows**: NO poner `Authorization: Bearer XXX` con espacio. Usar env var (`${AUTH_HEADER}`) sin espacios en el arg.

## 12.2. JSON inválido en claude_desktop_config.json

**Síntoma**: "Unexpected token 'X', '...' is not valid JSON".

**Causa**: pegaste algo que no es JSON puro (output de terminal, comentarios, etc.).

**Solución**: borrar TODO el archivo (Ctrl+A → Delete) y pegar **solo** el JSON. Verificar abriéndolo en el navegador (drag-and-drop): si muestra árbol colapsable está OK, si muestra texto plano hay error de sintaxis.

## 12.3. Claude Desktop pide permitir Node.js la primera vez

**Síntoma**: al reiniciar Claude Desktop con el nuevo config, aparece un diálogo pidiendo permitir ejecutar `node.exe` / `npx.cmd`.

**Causa**: `mcp-remote` arranca un proceso Node.js local que actúa como proxy hacia el MCP remoto. Claude Desktop te pide aprobar la ejecución por seguridad.

**Solución**: aceptar el prompt. Se acepta UNA vez y queda recordado.

> Si rechazás, el MCP no va a funcionar. Hay que ir a Claude Desktop → Settings → MCP/Tools → permitir el comando.

## 12.4. Requisitos en cada PC donde se replique Claude Desktop

Para que `mcp-remote` funcione, en cada PC se necesita:

- **Node.js** instalado (cualquier versión >= 18). Descargar de https://nodejs.org → instalar (Next, Next, Finish)
- **npx** disponible (viene incluido con Node.js)
- El archivo `claude_desktop_config.json` con el bloque `mcpServers.n8n-oficina` correcto

Tiempo de setup en una PC nueva: ~5 min (instalar Node si no está + pegar JSON + reiniciar Claude).

La primera vez que se ejecuta `npx -y mcp-remote ...`, descarga ~5 MB del paquete. Las siguientes corridas son instantáneas (cache local).

## 13. Tabla de errores comunes y respuesta esperada

| Test | Esperado | Si falla |
|---|---|---|
| `curl /healthz` | 200 JSON `{ok:true, has_oauth:true}` | Servicio caído → reiniciar |
| `curl /healthz -H "Origin: https://claude.ai"` (ver header) | `Access-Control-Allow-Origin: https://claude.ai` | Falta CORS → ver problema #2 |
| `curl /.well-known/oauth-authorization-server` | 200 JSON con `issuer`, `authorization_endpoint`, etc. | Falta endpoint → revisar `_PUBLIC_PATHS` |
| `curl /sse` (sin auth) | 401 `{error: "missing_bearer"}` | Si responde 200 → middleware roto |
| `curl /sse -H "Authorization: Bearer <TOKEN>"` | 200 streaming SSE | Bearer inválido → revisar env var |

---

## FIN

Si algo se rompe que no está acá, mirar:
- Logs en EasyPanel → servicio `mcp_n8n` → Registros
- Historial de implementaciones (¿el último commit está deployado?)
- `git log --oneline -5` (¿qué cambió?)
- `curl /healthz` (¿el servicio responde?)
