"""
Backend API for flights search.
Run with: uvicorn backend:app --reload --port 8000
"""
import re
import math
import time
import os
import logging
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from fast_flights import create_filter, get_flights_from_filter, FlightData, Passengers
from fast_flights.schema import Result

# ============================================================================
# LOG LEVEL  (0=silent, 1=basic, 2=normal, 3=debug)
# Set LOG_LEVEL env var to override. Default: 2
# ============================================================================
log_level: int = int(os.environ.get("LOG_LEVEL", "2"))
_log_buffer: deque = deque(maxlen=500)
search_progress: dict = {"percent": 0.0, "message": "", "completed": 0, "total": 0}
_progress_lock = threading.Lock()


def _progress_tick(label: str) -> None:
    """Incrementa el contador de progreso de forma thread-safe."""
    with _progress_lock:
        search_progress["completed"] += 1
        done = search_progress["completed"]
        total = search_progress["total"]
        search_progress["percent"] = round(done / total * 100, 1) if total else 0.0
        search_progress["message"] = f"{label} — {done}/{total} días"


_LEVEL_NAMES = {1: "INFO", 2: "INFO", 3: "DEBUG"}


def log(msg: str, min_level: int = 2) -> None:
    if log_level >= min_level:
        print(msg)
        _log_buffer.append({
            "ts":    datetime.now().strftime("%H:%M:%S"),
            "level": _LEVEL_NAMES.get(min_level, "INFO"),
            "msg":   str(msg),
        })


# ============================================================================
# LOG BUFFER (uvicorn handler)
# ============================================================================

_EXCLUDED_PATHS = ("/api/logs", "/api/log-level", "/api/progress")

class _BufferHandler(logging.Handler):
    def emit(self, record):
        try:
            msg = record.getMessage()
            if any(p in msg for p in _EXCLUDED_PATHS):
                return
            _log_buffer.append({
                "ts":    logging.Formatter().formatTime(record, "%H:%M:%S"),
                "level": record.levelname,
                "msg":   msg,
            })
        except Exception:
            pass

_buf_handler = _BufferHandler()
logging.getLogger().addHandler(_buf_handler)
logging.getLogger("uvicorn").addHandler(_buf_handler)
logging.getLogger("uvicorn.access").addHandler(_buf_handler)

# ============================================================================
# APP
# ============================================================================
app = FastAPI(title="Flights Search API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["POST", "GET"],
    allow_headers=["*"],
)


# ============================================================================
# SCHEMAS
# ============================================================================
class SearchRequest(BaseModel):
    fecha_ini: str = Field(..., examples=["02-06-2026"], description="Fecha inicio DD-MM-YYYY")
    fecha_fin: str = Field(..., examples=["13-06-2026"], description="Fecha fin DD-MM-YYYY")
    airport_from: List[str] = Field(..., examples=[["RTM"]])
    airport_to: List[str] = Field(..., examples=[["VLC"]])
    max_stops: int = Field(default=1, ge=0, le=3)
    max_results: int = Field(default=3, ge=1, description="Máx. vuelos baratos por día")


class FlightResult(BaseModel):
    fecha: str
    origen: str
    destino: str
    ruta: str
    ranking: int
    aerolinea: str
    salida: str
    llegada: str
    adelanto_llegada: str
    duracion: str
    escalas: int
    precio: str
    total_vuelos: int
    mas_barato: bool


class SearchResponse(BaseModel):
    rutas: List[str]
    total_vuelos: int
    vuelos: List[FlightResult]


class PriceRequest(BaseModel):
    fecha: str = Field(..., examples=["06-06-2026"], description="DD-MM-YYYY")
    origen: str = Field(..., examples=["VLC"])
    destino: str = Field(..., examples=["ZRH"])
    salida: str = Field(..., examples=["06:00"], description="HH:MM en formato 24h")
    aerolinea: str = Field(..., examples=["SWISS"])
    escalas: int = Field(..., ge=0)


class PriceResponse(BaseModel):
    precio: Optional[str]


# ============================================================================
# CORE LOGIC (extracted from test.py)
# ============================================================================
def convert_to_24h(time_str: str) -> str:
    """Convierte formato 12h (AM/PM) a 24h."""
    match = re.match(r'(\d{1,2}):(\d{2})\s(AM|PM)\s(on\s.+)', time_str)
    if not match:
        return time_str

    hour, minute, period, date_part = match.groups()
    hour = int(hour)

    if period == "PM":
        if hour != 12:
            hour += 12
    else:
        if hour == 12:
            hour = 0

    return f"{hour:02d}:{minute} {date_part}"


def _buscar_dia(from_airport: str, to_airport: str, fecha_str: str, max_stops: int, max_results: int = 3):
    """Fetch hasta max_results vuelos baratos para un día. Seguro para usar en threads."""
    vuelos_unicos: list = []
    result = None
    for intento in range(1, 4):
        try:
            filter_obj = create_filter(
                flight_data=[FlightData(date=fecha_str, from_airport=from_airport, to_airport=to_airport)],
                trip="one-way",
                passengers=Passengers(adults=1, children=0, infants_in_seat=0, infants_on_lap=0),
                seat="economy",
                max_stops=max_stops,
            )
            result = get_flights_from_filter(filter_obj, mode="force-fallback")
            if result and isinstance(result, Result) and result.flights:
                vuelos_vistos: set = set()
                for flight in sorted(result.flights, key=lambda x: float(x.price.replace("€", ""))):
                    if not all([flight.name, flight.departure, flight.arrival, flight.duration]):
                        continue
                    clave = (flight.name, flight.departure, flight.arrival, flight.price)
                    if clave not in vuelos_vistos:
                        vuelos_vistos.add(clave)
                        vuelos_unicos.append(flight)
                    if len(vuelos_unicos) >= max_results:
                        break
                if vuelos_unicos:
                    break
        except Exception as e:
            if "No flights found" in str(e):
                break
            if intento < 3:
                time.sleep(0.5)
    total_v = len(result.flights) if result and isinstance(result, Result) else 0
    return fecha_str, vuelos_unicos, total_v


def crear_filtro_main(
    fecha_ini: str,
    fecha_fin: str,
    airport_from: List[str],
    airport_to: List[str],
    max_stops: int = 1,
    max_results: int = 3,
) -> dict:
    """Busca vuelos para cada combinación origen/destino en el rango de fechas."""
    if isinstance(airport_from, str):
        airport_from = [airport_from]
    if isinstance(airport_to, str):
        airport_to = [airport_to]

    fecha_inicio = datetime.strptime(fecha_ini, "%d-%m-%Y")
    fecha_fin_dt = datetime.strptime(fecha_fin, "%d-%m-%Y")
    total_days = (fecha_fin_dt - fecha_inicio).days + 1

    # Rutas válidas (excluir mismo origen/destino)
    rutas_validas = [
        (src, dst)
        for src in airport_from
        for dst in airport_to
        if src != dst
    ]
    total_units = len(rutas_validas) * total_days

    with _progress_lock:
        search_progress["percent"] = 0.0
        search_progress["completed"] = 0
        search_progress["total"] = total_units
        search_progress["message"] = "Iniciando búsqueda…"

    resultados_por_ruta: dict = {}

    # Generar lista de fechas una sola vez
    fechas: List[str] = []
    f = fecha_inicio
    while f <= fecha_fin_dt:
        fechas.append(f.strftime("%Y-%m-%d"))
        f += timedelta(days=1)

    log(f"\n[INFO] Buscando vuelos desde {fecha_ini} hasta {fecha_fin}", min_level=2)
    log(f"Rutas válidas: {len(rutas_validas)} | Días por ruta: {total_days} | Total unidades: {total_units}",
        min_level=2,
    )
    log(f"       Máximo escalas: {max_stops}\n", min_level=2)

    
    sqrt_days = int(math.isqrt(total_days))
    workers = next((i for i in range(sqrt_days, 0, -1) if total_days % i == 0), 1)
    workers = max(3, min(workers, 8))
    log(f"[INFO] Usando {workers} workers por ruta", min_level=2)

    for ruta_num, (from_airport, to_airport) in enumerate(rutas_validas, 1):
            ruta_key = f"{from_airport} → {to_airport}"
            resultados_por_ruta[ruta_key] = []

            log(f"{'='*60}", min_level=2)
            log(f"RUTA: {ruta_key}  ({ruta_num}/{len(rutas_validas)})", min_level=2)
            log(f"{'='*60}", min_level=2)

            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {
                    executor.submit(_buscar_dia, from_airport, to_airport, fecha_str, max_stops, max_results): fecha_str
                    for fecha_str in fechas
                }
                for future in as_completed(futures):
                    fecha_str, vuelos_unicos, total_v = future.result()

                    _progress_tick(ruta_key)

                    if vuelos_unicos:
                        log(f"OK [{fecha_str}] - {vuelos_unicos[0].price} ({total_v} vuelos)", min_level=3)
                        resultados_por_ruta[ruta_key].append({
                            "fecha": fecha_str,
                            "vuelos_baratos": vuelos_unicos,
                            "precio_minimo": vuelos_unicos[0].price,
                            "total_vuelos": total_v,
                        })
                    else:
                        log(f"SIN VUELOS para {fecha_str}", min_level=3)

            # Ordenar por fecha (as_completed no preserva orden)
            resultados_por_ruta[ruta_key].sort(key=lambda x: x["fecha"])
            log("", min_level=2)

    search_progress["percent"] = 100.0
    search_progress["message"] = "Búsqueda completada"
    return resultados_por_ruta


def serializar_resultados(resultados_por_ruta: dict, origen: str = "", destino: str = "") -> SearchResponse:
    """Convierte los objetos de vuelo en dicts serializables."""
    vuelos: List[FlightResult] = []

    for ruta, resultados_por_dia in resultados_por_ruta.items():
        origen_ruta, destino_ruta = ruta.split(" → ")
        for res in resultados_por_dia:
            fecha = datetime.strptime(res["fecha"], "%Y-%m-%d").strftime("%d-%m-%Y")
            for ranking, flight in enumerate(res["vuelos_baratos"], 1):
                salida = convert_to_24h(flight.departure).replace(", ", " ")
                llegada = convert_to_24h(flight.arrival).replace(", ", " ")
                vuelos.append(
                    FlightResult(
                        fecha=fecha,
                        origen=origen_ruta,
                        destino=destino_ruta,
                        ruta=ruta,
                        ranking=ranking,
                        aerolinea=flight.name.strip(),
                        salida=salida,
                        llegada=llegada,
                        adelanto_llegada=getattr(flight, "arrival_time_ahead", ""),
                        duracion=flight.duration,
                        escalas=flight.stops,
                        precio=flight.price,
                        total_vuelos=res["total_vuelos"],
                        mas_barato=(ranking == 1),
                    )
                )

    return SearchResponse(
        rutas=list(resultados_por_ruta.keys()),
        total_vuelos=len(vuelos),
        vuelos=vuelos,
    )


# ============================================================================
# ENDPOINTS
# ============================================================================

@app.post("/api/price", response_model=PriceResponse)
def get_price(req: PriceRequest) -> PriceResponse:
    """
    Devuelve el precio actualizado de un vuelo concreto.
    Busca por fecha, ruta, hora de salida (HH:MM), aerolínea y escalas.
    """
    try:
        fecha_api = datetime.strptime(req.fecha, "%d-%m-%Y").strftime("%Y-%m-%d")
    except ValueError as e:
        raise HTTPException(status_code=422, detail=f"Formato de fecha inválido: {e}")

    for intento in range(1, 4):
        try:
            filter_obj = create_filter(
                flight_data=[FlightData(date=fecha_api, from_airport=req.origen, to_airport=req.destino)],
                trip="one-way",
                passengers=Passengers(adults=1, children=0, infants_in_seat=0, infants_on_lap=0),
                seat="economy",
                max_stops=req.escalas,
            )
            result = get_flights_from_filter(filter_obj, mode="force-fallback")
            if result and isinstance(result, Result) and result.flights:
                for flight in result.flights:
                    salida_24h = convert_to_24h(flight.departure)
                    # Extraer solo HH:MM del resultado (ej. "06:00 on Jun 6" → "06:00")
                    salida_hhmm = salida_24h[:5]
                    if (
                        salida_hhmm == req.salida[:5]
                        and req.aerolinea.lower() in flight.name.lower()
                        and flight.stops <= req.escalas
                    ):
                        return PriceResponse(precio=flight.price)
                # Encontrado resultado pero no coincide el vuelo exacto
                return PriceResponse(precio=None)
        except Exception as e:
            if "No flights found" in str(e):
                return PriceResponse(precio=None)
            if intento < 3:
                time.sleep(0.5)

    return PriceResponse(precio=None)


@app.get("/api/ping")
def ping() -> dict:
    return {"status": "ok"}


@app.get("/api/progress")
def get_progress() -> dict:
    return search_progress


@app.get("/api/logs")
def get_logs() -> dict:
    return {"logs": list(_log_buffer), "log_level": log_level}


@app.post("/api/log-level")
def set_log_level(body: dict) -> dict:
    global log_level
    log_level = int(body.get("level", log_level))
    return {"log_level": log_level}


@app.post("/api/search", response_model=SearchResponse)
def search_flights(req: SearchRequest) -> SearchResponse:
    """
    Busca vuelos en el rango de fechas para las rutas indicadas.

    - **fecha_ini / fecha_fin**: formato DD-MM-YYYY
    - **airport_from / airport_to**: lista de códigos IATA (ej. ["RTM", "AMS"])
    - **max_stops**: 0 = directo, 1 = máx. 1 escala, etc.
    """
    try:
        # Validar fechas
        datetime.strptime(req.fecha_ini, "%d-%m-%Y")
        datetime.strptime(req.fecha_fin, "%d-%m-%Y")
    except ValueError as e:
        raise HTTPException(status_code=422, detail=f"Formato de fecha inválido: {e}")

    try:
        resultados = crear_filtro_main(
            fecha_ini=req.fecha_ini,
            fecha_fin=req.fecha_fin,
            airport_from=req.airport_from,
            airport_to=req.airport_to,
            max_stops=req.max_stops,
            max_results=req.max_results,
        )
        return serializar_resultados(resultados)
    except Exception as e:
        log(f"[ERROR] {e}", min_level=1)
        raise HTTPException(status_code=500, detail=str(e))
