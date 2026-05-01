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
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from urllib.parse import urlencode
from fast_flights import create_filter, get_flights_from_filter, FlightData, Passengers
from fast_flights.schema import Result

# ============================================================================
# LOG LEVEL  (0=silent, 1=basic, 2=normal, 3=debug)
# Set LOG_LEVEL env var to override. Default: 2
# ============================================================================
log_level: int = int(os.environ.get("LOG_LEVEL", "2"))
_log_buffer: deque = deque(maxlen=500)
_search_progress: dict = {}  # search_id -> progress dict
_search_results: dict = {}   # search_id -> SearchResponse (actualizado en vivo por threads)
_resolve_jobs: dict = {}     # resolve_id -> {status, precio, intento}
_progress_lock = threading.Lock()


def _progress_tick(search_id: str, label: str) -> None:
    """Incrementa el contador de progreso de forma thread-safe."""
    with _progress_lock:
        prog = _search_progress[search_id]
        prog["completed"] += 1
        done = prog["completed"]
        total = prog["total"]
        prog["percent"] = round(done / total * 100, 1) if total else 0.0
        prog["message"] = f"{label} — {done}/{total} días"


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
    search_id: Optional[str] = Field(default=None, description="ID único para seguimiento de progreso")


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
    url: Optional[str] = None


class SearchResponse(BaseModel):
    search_id: str = ""
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


class ResolveStartResponse(BaseModel):
    resolve_id: str


class ResolveStatusResponse(BaseModel):
    resolve_id: str
    status: str          # "pending" | "found" | "exhausted" | "cancelled"
    precio: Optional[str] = None
    intento: int = 0


class CancelRequest(BaseModel):
    resolve_ids: List[str]


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


def _safe_price(price_str: str) -> float:
    """Parsea el precio de forma segura. Devuelve inf para precios ocultos o inválidos."""
    try:
        val = float(re.sub(r'[^\d.]', '', price_str))
        return val if val > 0 else float('inf')
    except (ValueError, TypeError):
        return float('inf')


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
            result = get_flights_from_filter(filter_obj, mode="common")
            if result and isinstance(result, Result) and result.flights:
                vuelos_vistos: set = set()
                vuelos_unicos = []
                vuelos_precio_oculto = []
                for flight in sorted(result.flights, key=lambda x: _safe_price(x.price)):
                    if not all([flight.name, flight.departure, flight.arrival, flight.duration]):
                        continue
                    clave = (flight.name, flight.departure, flight.arrival, flight.price)
                    if clave in vuelos_vistos:
                        continue
                    vuelos_vistos.add(clave)
                    if _safe_price(flight.price) == float('inf'):
                        # Precio oculto (ej. Wizz Air): guardar aparte para añadir al final
                        vuelos_precio_oculto.append(flight)
                    else:
                        vuelos_unicos.append(flight)
                    if len(vuelos_unicos) >= max_results:
                        break
                # Rellenar huecos restantes con vuelos de precio oculto
                for flight in vuelos_precio_oculto:
                    if len(vuelos_unicos) >= max_results:
                        break
                    vuelos_unicos.append(flight)
                if vuelos_unicos:
                    break
        except Exception as e:
            if "No flights found" in str(e):
                break  # sin vuelos reales ese día → no reintentar
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
    search_id: str = "",
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
        _search_progress[search_id] = {
            "percent": 0.0,
            "completed": 0,
            "total": total_units,
            "message": "Iniciando búsqueda…",
        }

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

                    _progress_tick(search_id, ruta_key)

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

    _search_progress[search_id]["percent"] = 100.0
    _search_progress[search_id]["message"] = "Búsqueda completada"
    return resultados_por_ruta


def _build_flight_url(
    from_airport: str,
    to_airport: str,
    fecha_str: str,
    stops: int,
    price_str: str,
    salida: Optional[str] = None,
    currency: str = "EUR",
) -> str:
    """Genera URL de Google Flights: ruta, fecha, escalas exactas, moneda, precio máximo y hora de salida en tfs."""
    price_match = re.search(r'(\d+)', price_str.replace(',', ''))
    raw_price = int(price_match.group(1)) if price_match else 0
    max_price = raw_price if raw_price > 0 else None  # precio oculto → sin filtro de precio

    # Parsear la hora de salida (formato "HH:MM ...")
    dep_hour: Optional[int] = None
    if salida and len(salida) >= 5:
        try:
            dep_hour = int(salida[:2])
        except ValueError:
            pass

    from fast_flights.filter import create_filter as _cf
    fd = FlightData(date=fecha_str, from_airport=from_airport, to_airport=to_airport, dep_hour=dep_hour)
    filter_obj = _cf(
        flight_data=[fd],
        trip="one-way",
        passengers=Passengers(adults=1, children=0, infants_in_seat=0, infants_on_lap=0),
        seat="economy",
        max_stops=stops,
    )
    filter_obj.max_price = max_price
    return filter_obj.as_url(currency=currency)


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
                flight_url = _build_flight_url(
                    from_airport=origen_ruta,
                    to_airport=destino_ruta,
                    fecha_str=res["fecha"],
                    stops=flight.stops,
                    price_str=flight.price,
                    salida=salida,
                )
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
                        precio=flight.price if _safe_price(flight.price) != float('inf') else "–",
                        total_vuelos=res["total_vuelos"],
                        mas_barato=(ranking == 1 and _safe_price(flight.price) != float('inf')),
                        url=flight_url,
                    )
                )

    return SearchResponse(
        rutas=list(resultados_por_ruta.keys()),
        total_vuelos=len(vuelos),
        vuelos=vuelos,
    )


# ============================================================================
# RESOLUCIÓN DE PRECIOS OCULTOS EN BACKGROUND
# ============================================================================

def _resolver_precio_background(price_req: PriceRequest, resolve_id: str) -> None:
    """Hilo daemon que reintenta hasta 100 veces obtener el precio de un vuelo con precio
    oculto. Actualiza _resolve_jobs[resolve_id] en cada intento para que el front pueda
    consultar el estado via GET /api/resolve-price/{resolve_id}."""
    tag = resolve_id[:8]
    log(f"[PRECIO] [{tag}] Iniciando: {price_req.aerolinea} {price_req.fecha} {price_req.salida}", min_level=2)
    cancel_event: threading.Event = _resolve_jobs[resolve_id]["cancel"]
    for intento in range(1, 16):
        if cancel_event.is_set():
            log(f"[PRECIO] [{tag}] Cancelado en intento {intento}", min_level=2)
            return
        time.sleep(1.0)
        if cancel_event.is_set():
            log(f"[PRECIO] [{tag}] Cancelado tras espera intento {intento}", min_level=2)
            return
        _resolve_jobs[resolve_id]["intento"] = intento
        log(f"[PRECIO] [{tag}] Intento {intento}/15 — {price_req.aerolinea} {price_req.fecha} {price_req.salida}", min_level=2)
        try:
            resp = get_price(price_req)
            if resp.precio and _safe_price(resp.precio) != float('inf'):
                _resolve_jobs[resolve_id]["status"] = "found"
                _resolve_jobs[resolve_id]["precio"] = resp.precio
                log(f"[PRECIO] [{tag}] ✓ Resuelto → {resp.precio} (intento {intento})", min_level=2)
                return
            else:
                log(f"[PRECIO] [{tag}] Sin precio aún (intento {intento}) — resp: {resp.precio!r}", min_level=2)
        except Exception as e:
            log(f"[PRECIO] [{tag}] Error en intento {intento}: {e}", min_level=2)
    _resolve_jobs[resolve_id]["status"] = "exhausted"
    log(f"[PRECIO] [{tag}] ✗ Sin precio tras 100 intentos: {price_req.aerolinea} {price_req.fecha} {price_req.salida}", min_level=2)


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
            result = get_flights_from_filter(filter_obj, mode="common")
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


@app.post("/api/resolve-price/cancel")
def cancel_resolve_prices(req: CancelRequest) -> dict:
    """
    Cancela trabajos de resolución de precio activos.
    Llamado por el front vía sendBeacon al cerrar/recargar la página.
    Siempre devuelve 200 con el número de trabajos cancelados.
    """
    cancelled = 0
    for rid in req.resolve_ids:
        job = _resolve_jobs.get(rid)
        if job and job["status"] == "pending":
            job["cancel"].set()
            job["status"] = "cancelled"
            cancelled += 1
            log(f"[PRECIO] [{rid[:8]}] Cancelado por el cliente", min_level=2)
    return {"cancelled": cancelled}


@app.post("/api/resolve-price", response_model=ResolveStartResponse)
def start_resolve_price(req: PriceRequest) -> ResolveStartResponse:
    """
    Inicia la resolución en background del precio de un vuelo con precio oculto.
    Devuelve un resolve_id para consultar el estado con GET /api/resolve-price/{resolve_id}.
    """
    resolve_id = str(uuid.uuid4())
    _resolve_jobs[resolve_id] = {"status": "pending", "precio": None, "intento": 0, "cancel": threading.Event()}
    threading.Thread(
        target=_resolver_precio_background,
        args=(req, resolve_id),
        daemon=True,
    ).start()
    log(f"[PRECIO] Resolución iniciada [{resolve_id[:8]}]: {req.aerolinea} {req.fecha} {req.salida}", min_level=2)
    return ResolveStartResponse(resolve_id=resolve_id)


@app.get("/api/resolve-price/{resolve_id}", response_model=ResolveStatusResponse)
def get_resolve_status(resolve_id: str) -> ResolveStatusResponse:
    """
    Consulta el estado de una resolución de precio iniciada con POST /api/resolve-price.
    - status="pending"   → sigue buscando (sigue haciendo polling)
    - status="found"     → precio encontrado en el campo `precio`
    - status="exhausted" → 100 intentos agotados, no se encontró precio
    """
    if resolve_id not in _resolve_jobs:
        raise HTTPException(status_code=404, detail="resolve_id no encontrado")
    job = _resolve_jobs[resolve_id]
    return ResolveStatusResponse(
        resolve_id=resolve_id,
        status=job["status"],
        precio=job["precio"],
        intento=job["intento"],
    )


@app.get("/api/results/{search_id}", response_model=SearchResponse)
def get_results(search_id: str) -> SearchResponse:
    """Devuelve los resultados actualizados de una búsqueda previa (con precios resueltos en background)."""
    if search_id not in _search_results:
        raise HTTPException(status_code=404, detail="Búsqueda no encontrada")
    return _search_results[search_id]


@app.get("/api/progress")
def get_progress(search_id: Optional[str] = None) -> dict:
    if search_id:
        return _search_progress.get(search_id, {"percent": 0.0, "message": "No encontrado", "completed": 0, "total": 0})
    return {"percent": 0.0, "message": "Proporciona search_id como parámetro", "completed": 0, "total": 0}


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

    search_id = req.search_id or str(uuid.uuid4())
    try:
        resultados = crear_filtro_main(
            fecha_ini=req.fecha_ini,
            fecha_fin=req.fecha_fin,
            airport_from=req.airport_from,
            airport_to=req.airport_to,
            max_stops=req.max_stops,
            max_results=req.max_results,
            search_id=search_id,
        )
        result = serializar_resultados(resultados)
        response = SearchResponse(
            search_id=search_id,
            rutas=result.rutas,
            total_vuelos=result.total_vuelos,
            vuelos=result.vuelos,
        )
        # Guardar referencia viva para que /api/results devuelva datos actualizados
        _search_results[search_id] = response
        return response
    except Exception as e:
        log(f"[ERROR] {e}", min_level=1)
        raise HTTPException(status_code=500, detail=str(e))
