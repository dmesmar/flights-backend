# Frontend Briefing — Flights Search App

## Stack del Backend
- **Framework:** FastAPI (Python)
- **URL base local:** `http://localhost:8000`
- **CORS:** habilitado para todos los orígenes (`*`)
- **Arranque:** `uvicorn backend:app --reload --port 8000`

---

## Endpoints

### `GET /health`
Comprueba que el servidor está vivo.
```json
{ "status": "ok" }
```

---

### `POST /api/search`
Busca vuelos. **Puede tardar varios minutos** si el rango de fechas es amplio (hace peticiones reales a Google Flights con sleeps entre llamadas).

#### Request body
```json
{
  "fecha_ini": "02-06-2026",
  "fecha_fin": "13-06-2026",
  "airport_from": ["RTM"],
  "airport_to": ["VLC"],
  "max_stops": 1
}
```

| Campo | Tipo | Obligatorio | Notas |
|---|---|---|---|
| `fecha_ini` | string | ✅ | Formato `DD-MM-YYYY` |
| `fecha_fin` | string | ✅ | Formato `DD-MM-YYYY` |
| `airport_from` | string[] | ✅ | Códigos IATA. Puede ser más de uno |
| `airport_to` | string[] | ✅ | Códigos IATA. Puede ser más de uno |
| `max_stops` | int | ❌ | Default `1`. Rango `0–3`. `0` = solo directos |

#### Response 200
```json
{
  "rutas": ["RTM → VLC"],
  "total_vuelos": 34,
  "vuelos": [
    {
      "fecha": "02-06-2026",
      "origen": "RTM",
      "destino": "VLC",
      "ruta": "RTM → VLC",
      "ranking": 1,
      "aerolinea": "Vueling",
      "salida": "07:30 on Jun 2",
      "llegada": "09:45 on Jun 2",
      "adelanto_llegada": "",
      "duracion": "2 hr 15 min",
      "escalas": 0,
      "precio": "€89",
      "total_vuelos": 12,
      "mas_barato": true
    }
  ]
}
```

| Campo | Tipo | Notas |
|---|---|---|
| `rutas` | string[] | Lista de rutas en formato `"XXX → YYY"` |
| `total_vuelos` | int | Nº total de entradas en `vuelos` |
| `fecha` | string | `DD-MM-YYYY` |
| `ranking` | int | `1` = más barato del día para esa ruta |
| `mas_barato` | bool | `true` solo para `ranking === 1` |
| `precio` | string | Viene con símbolo, ej: `"€89"` |
| `escalas` | int | Número de escalas |
| `adelanto_llegada` | string | Ej: `"+1"` si llega al día siguiente. Puede estar vacío |
| `total_vuelos` | int | Cuántos vuelos totales encontró Google ese día para esa ruta |

#### Errores
| Código | Motivo |
|---|---|
| `422` | Formato de fecha inválido |
| `500` | Error interno (fallo al obtener datos de Google Flights) |

---

## Consideraciones importantes para la UX

1. **La búsqueda es lenta** — Por cada día del rango hace hasta 4 reintentos con sleeps. Para 12 días puede tardar 30–90 segundos. Mostrar un loader/spinner y no bloquear la UI.
2. **Múltiples orígenes/destinos** — `airport_from` y `airport_to` son arrays, el formulario debería permitir añadir varios aeropuertos.
3. **Máximo 3 vuelos por día y ruta** — El backend solo devuelve los 3 más baratos por día.
4. **El precio es string con símbolo** — Para ordenar/filtrar hay que parsear: `parseFloat(precio.replace("€", ""))`.
5. **Documentación Swagger** disponible en `http://localhost:8000/docs` para probar los endpoints directamente.
