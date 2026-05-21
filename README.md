# mac-monitor

Recolector ligero de métricas de macOS (RAM, CPU, disco, batería, top apps por RAM) que escribe a SQLite y regenera un dashboard HTML estático para abrir con Safari.

- **Cero dependencias externas.** Solo Python 3 (incluido en macOS) y herramientas nativas (`vm_stat`, `ps`, `sysctl`, `df`, `pmset`, `top`).
- **Control manual.** Se enciende y se apaga con un comando — vos decidís cuándo medir.
- **Histórico completo.** El dashboard muestra todo el rango de muestras recolectadas, con downsampling automático si hay muchas.
- **Sin servidor, sin JS frameworks.** El dashboard es HTML + SVG inline, se abre con `file://`.

## Captura rápida

```
~/Documents/personal/mac-monitor $ ./bin/mac-monitor on
mac-monitor: ENCENDIDO.
  Intervalo:    30 s
  Primer tick:  inmediato (RunAtLoad)
  Runtime:      /Users/<vos>/Library/Application Support/mac-monitor
  Dashboard:    /Users/<vos>/Library/Application Support/mac-monitor/dashboard.html

~/Documents/personal/mac-monitor $ ./bin/mac-monitor open
# abre Safari con el dashboard
```

## Requisitos

- macOS (probado en versiones recientes con Apple Silicon; debería funcionar en Intel).
- Python 3 disponible en `/usr/bin/python3` (viene con las Command Line Tools de Xcode).
- Una shell `bash` o `zsh`.

No se instala nada con `pip`, `brew` ni similares.

## Instalación

```bash
git clone git@github.com:<tu-usuario>/mac-monitor.git
cd mac-monitor
chmod +x bin/mac-monitor bin/tick.py
./bin/mac-monitor now      # primera muestra manual
./bin/mac-monitor open     # abre el dashboard en Safari
```

Para que recolecte automáticamente cada 30 segundos:

```bash
./bin/mac-monitor on
```

## Comandos

| Comando | Qué hace |
|---|---|
| `./bin/mac-monitor on` | Activa la recolección automática vía `launchd`. |
| `./bin/mac-monitor off` | Apaga la recolección. **Conserva el histórico.** |
| `./bin/mac-monitor status` | Muestra si está activo y cuándo fue la última actualización. |
| `./bin/mac-monitor now` | Toma una muestra ahora mismo (manual, no requiere estar `on`). |
| `./bin/mac-monitor open` | Abre `dashboard.html` en Safari. |
| `./bin/mac-monitor logs` | Imprime los últimos logs de stdout/stderr. |

## Dónde se guardan los datos

Los datos **no** viven dentro del repo. Por restricciones de TCC de macOS (ver más abajo), el runtime vive en:

```
~/Library/Application Support/mac-monitor/
├── metrics.db        # histórico SQLite (todas las muestras)
├── dashboard.html    # UI regenerada en cada tick
├── tick.py           # copia sincronizada desde bin/tick.py
├── stdout.log
└── stderr.log
```

Y el agente de `launchd`:

```
~/Library/LaunchAgents/local.mac-monitor.plist
```

`metrics.db` **es persistente**: se conserva al hacer `off`, al reiniciar el Mac, al cerrar sesión. Solo se borra si vos lo borrás manualmente.

## Cómo funciona

### Estructura híbrida (código vs runtime)

```
mac-monitor/                           # código fuente, versionable
├── bin/
│   ├── mac-monitor       # script bash de control (on/off/status/now/open/logs)
│   └── tick.py           # FUENTE del recolector + render
├── launchd/
│   └── local.mac-monitor.plist.tpl
└── README.md
```

`launchd` ejecuta procesos sin el contexto de la sesión interactiva, y macOS **TCC** (Transparency, Consent & Control) bloquea la lectura de archivos en `~/Documents/`, `~/Desktop/`, etc. desde esos procesos. Por eso `bin/tick.py` se copia a `~/Library/Application Support/mac-monitor/` antes de ejecutarse vía `launchd` — esa ubicación no está bajo TCC.

Si editás `bin/tick.py`, basta con `./bin/mac-monitor on` (o `now`) para resincronizar la copia.

### Por qué `launchd` y no `cron`

`launchd` es el sistema nativo de macOS para tareas programadas, sobrevive a reinicios y maneja logs, intervalos y procesos huérfanos correctamente. El proyecto genera dinámicamente el `.plist` con paths absolutos al hacer `on`.

### Render estático

El dashboard se regenera completo en cada tick — Python lee la DB, hace downsampling si hay más de 800 muestras, y escribe `dashboard.html` con SVG inline. Sin servidor, sin red, sin CDNs.

## Personalización

### Cambiar el intervalo de muestreo

Editá `launchd/local.mac-monitor.plist.tpl`:

```xml
<key>StartInterval</key>
<integer>30</integer>     <!-- segundos entre muestras -->
```

Luego corré `./bin/mac-monitor on` para regenerar el plist y recargar.

> **Nota:** intervalos muy cortos (< 30 s) van a generar muchas muestras por día. A 30 s son ~2880 muestras/día; a 5 min son ~288/día. La DB crece poco (cada muestra ocupa ~200 bytes), pero el dashboard se recalcula en cada tick.

### Cambiar el máximo de puntos del gráfico

En `bin/tick.py`, función `downsample()` — por defecto `max_points=800`. Bajalo si querés gráficos más livianos, subilo si querés más resolución.

### Agregar/quitar apps en el clasificador

`bin/tick.py` tiene una función `classify(comm)` que mapea nombres de procesos a categorías legibles (Cursor, Safari, VSCode, etc.). Agregá las apps que uses.

### Cambiar el label de launchd

Por defecto el label es `local.mac-monitor`. Si querés algo más específico (ej. `com.<tu-usuario>.mac-monitor`), cambialo en dos lugares: `bin/mac-monitor` (variable `LABEL`) y `launchd/local.mac-monitor.plist.tpl` (campo `<key>Label</key>`).

## Desinstalación completa

```bash
# 1. Apagar el agente
./bin/mac-monitor off

# 2. Borrar el plist
rm ~/Library/LaunchAgents/local.mac-monitor.plist

# 3. Borrar el runtime (DB, dashboard, logs)
rm -rf ~/Library/Application\ Support/mac-monitor

# 4. Borrar el repo
rm -rf <ruta-al-clon>
```

## Limitaciones conocidas

- `top -l 2` introduce un delay de ~1 segundo en cada tick (es lo que tarda en tomar dos muestras de CPU). Si te molesta, podés cambiar a `-l 1` aceptando que la primera medición de CPU será desde boot.
- El clasificador de apps (`classify()`) es manual y específico. Procesos no reconocidos caen en "otros (sistema)".
- El dashboard no se auto-refresca en el navegador. Recargá la pestaña (`⌘R`) para ver datos nuevos.

## Contribuciones

Issues y PRs bienvenidos. El proyecto está intencionalmente acotado — su valor es ser pequeño, leíble y sin dependencias.

## Licencia

[MIT](./LICENSE) © 2026 erickmdgz
