# mac-monitor

## Descripción

Recolector ligero de métricas de macOS (RAM, CPU, disco, batería, top apps) que escribe a SQLite y regenera un dashboard HTML estático visualizable con Safari. Se activa/desactiva manualmente con un comando para controlar cuándo se mide.

## Stack y versiones

- Lenguaje: Python 3.9 (stdlib pura, sin dependencias externas)
- Almacenamiento: SQLite (stdlib)
- Programación: `launchd` (nativo de macOS, gestionado por el comando `mac-monitor`)
- Render: HTML + SVG inline generado desde Python (sin JS frameworks, sin CDNs)

## Comandos

```bash
# Encender recolección (cada 30 s vía launchd)
./bin/mac-monitor on

# Apagar
./bin/mac-monitor off

# Ver si está activo
./bin/mac-monitor status

# Recolectar una muestra ahora (manual, no requiere estar "on")
./bin/mac-monitor now

# Abrir dashboard.html en Safari
./bin/mac-monitor open
```

## Estructura interna (híbrida por TCC)

```
~/Documents/personal/mac-monitor/        # código fuente, editable, versionable
├── bin/
│   ├── mac-monitor       # script bash de control (on/off/status/now/open/logs)
│   └── tick.py           # FUENTE del recolector + render
├── launchd/
│   └── com.etegi.mac-monitor.plist.tpl
└── CLAUDE.md

~/Library/Application Support/mac-monitor/  # runtime (datos + ejecución)
├── tick.py               # copia sincronizada desde fuente al hacer 'on' o 'now'
├── metrics.db            # SQLite con histórico
├── dashboard.html        # generado por tick.py
└── stdout.log / stderr.log

~/Library/LaunchAgents/
└── com.etegi.mac-monitor.plist  # generado por 'mac-monitor on'
```

## Notas para Claude

- **Cero dependencias externas**: solo Python stdlib + comandos nativos macOS (`vm_stat`, `ps`, `sysctl`, `df`, `pmset`, `top`). No instalar psutil ni librerías de gráficos.
- **Por qué la estructura híbrida**: `launchd` lanza procesos sin el contexto de la sesión interactiva. macOS TCC bloquea lectura de archivos en `~/Documents/` desde estos procesos (`Operation not permitted`). Por eso `tick.py` se copia a `~/Library/Application Support/mac-monitor/` antes de ejecutarse vía launchd. Si editás `bin/tick.py`, basta con `mac-monitor on` (o `now`) para resincronizar.
- **launchd, no cron**: el plist va a `~/Library/LaunchAgents/com.etegi.mac-monitor.plist`. Se genera dinámicamente con paths absolutos.
- **Control manual**: el diseño exige que el usuario decida cuándo medir. `RunAtLoad: true` solo se activa al hacer `mac-monitor on`, no al iniciar sesión del usuario.
- **Persistencia**: `mac-monitor off` solo descarga el agente (`launchctl bootout`), no borra plist ni DB. Para reset total, borrar manualmente `~/Library/Application Support/mac-monitor/metrics.db` y `~/Library/LaunchAgents/com.etegi.mac-monitor.plist`.
- **Render estático sin JS**: el dashboard es HTML+SVG generado desde Python. Permite abrir el archivo con `file://` sin servidor.
