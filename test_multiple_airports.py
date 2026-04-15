#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from fast_flights import create_filter, get_flights_from_filter, FlightData, Passengers
from datetime import datetime, timedelta
import re

def convert_to_24h(time_str):
    """Convierte formato 12h (AM/PM) a 24h"""
    if not time_str:
        return time_str
    
    # Buscar patrón: HH:MM (AM/PM)
    match = re.search(r'(\d{1,2}):(\d{2})\s*(AM|PM)', time_str)
    if not match:
        return time_str
    
    hour = int(match.group(1))
    minute = match.group(2)
    period = match.group(3)
    
    # Convertir a 24h
    if period == 'PM' and hour != 12:
        hour += 12
    elif period == 'AM' and hour == 12:
        hour = 0
    
    # Reemplazar en la cadena original
    new_time = f"{hour:02d}:{minute}"
    return time_str.replace(match.group(0), new_time)


def crear_filtro_main(fecha_ini, fecha_fin, airport_from, airport_to):
    """
    Crea filtros para todos los días entre dos fechas y múltiples rutas.
    """
    
    # Convertir strings a listas si es necesario
    if isinstance(airport_from, str):
        airport_from = [airport_from]
    if isinstance(airport_to, str):
        airport_to = [airport_to]
    
    # Convertir strings a datetime
    fecha_inicio = datetime.strptime(fecha_ini, "%d-%m-%Y")
    fecha_fin_dt = datetime.strptime(fecha_fin, "%d-%m-%Y")
    
    resultados_por_ruta = {}
    
    print(f"\n[INFO] Buscando vuelos desde {fecha_ini} hasta {fecha_fin}")
    print(f"       Rutas: {len(airport_from)} origen(es) × {len(airport_to)} destino(s) = {len(airport_from) * len(airport_to)} combinación(es)\n")
    
    # Iterar por cada combinación de aeropuertos
    for from_airport in airport_from:
        for to_airport in airport_to:
            ruta_key = f"{from_airport} → {to_airport}"
            resultados_por_ruta[ruta_key] = []
            
            print(f"{'='*60}")
            print(f"RUTA: {ruta_key}")
            print(f"{'='*60}")
            
            fecha_actual = fecha_inicio
            day_count = 0
            
            # Iterar por cada día
            while fecha_actual <= fecha_fin_dt:
                fecha_str = fecha_actual.strftime("%Y-%m-%d")
                day_count += 1
                
                try:
                    print(f"[{day_count}] {fecha_str}...", end=" ")
                    
                    # Crear el filtro para ese día
                    filter_obj = create_filter(
                        flight_data=[
                            FlightData(
                                date=fecha_str,
                                from_airport=from_airport,
                                to_airport=to_airport,
                            )
                        ],
                        trip="one-way",
                        passengers=Passengers(adults=1, children=0, infants_in_seat=0, infants_on_lap=0),
                        seat="economy",
                        max_stops=1,
                    )
                    
                    # Obtener resultados
                    result = get_flights_from_filter(filter_obj, mode="common")
                    
                    if result and result.flights:
                        # Eliminar duplicados
                        vuelos_unicos = []
                        vuelos_vistos = set()
                        
                        for flight in sorted(result.flights, key=lambda x: float(x.price.replace('€', ''))):
                            # Validar que el vuelo tenga todos los datos
                            if not flight.name or not flight.departure or not flight.arrival or not flight.duration:
                                continue
                            
                            clave = (flight.name, flight.departure, flight.arrival, flight.price)
                            
                            if clave not in vuelos_vistos:
                                vuelos_vistos.add(clave)
                                vuelos_unicos.append(flight)
                                
                            if len(vuelos_unicos) >= 3:
                                break
                        
                        if vuelos_unicos:
                            cheapest = vuelos_unicos[0]
                            print(f"OK - {cheapest.price} ({len(result.flights)} vuelos)")
                            
                            resultados_por_ruta[ruta_key].append({
                                "fecha": fecha_str,
                                "resultado": result,
                                "vuelos_baratos": vuelos_unicos,
                                "precio_minimo": cheapest.price,
                                "total_vuelos": len(result.flights)
                            })
                        else:
                            print("Sin datos completos")
                    else:
                        print("Sin vuelos")
                        
                except Exception as e:
                    print(f"ERROR: {str(e)[:50]}")
                
                fecha_actual += timedelta(days=1)
            
            print()
    
    return resultados_por_ruta


# ============================================================================
# EJEMPLO: MÚLTIPLES AEROPUERTOS
# ============================================================================

print("\n" + "="*120)
print("EJEMPLO 1: BÚSQUEDA CON MÚLTIPLES ORÍGENES Y UN DESTINO")
print("="*120)

resultados = crear_filtro_main(
    fecha_ini="23-05-2026",
    fecha_fin="24-05-2026",
    airport_from=["MAD", "BCN"],  # Múltiples orígenes
    airport_to="VLC"               # Un destino (se convierte a lista)
)

# Mostrar resultados
total_rutas = len(resultados)
total_dias = sum(len(res_list) for res_list in resultados.values())

print(f"\n{'='*120}")
print(f"RESUMEN: {total_rutas} ruta(s) con {total_dias} día(s) disponibles")
print(f"{'='*120}\n")

for ruta, resultados_por_dia in resultados.items():
    print(f"\n{'='*120}")
    print(f"RUTA: {ruta}")
    print(f"{'='*120}\n")
    
    if not resultados_por_dia:
        print("  Sin vuelos disponibles en este período\n")
        continue
    
    for res in resultados_por_dia:
        fecha_formateada = datetime.strptime(res['fecha'], "%Y-%m-%d").strftime("%d-%m-%Y")
        print(f"📅 {fecha_formateada} ({res['total_vuelos']} vuelos totales)")
        print("-" * 120)
        
        for i, flight in enumerate(res['vuelos_baratos'], 1):
            departure_24h = convert_to_24h(flight.departure)
            arrival_24h = convert_to_24h(flight.arrival)
            
            print(f"  {i}. {flight.name:20s} | "
                  f"Dep: {departure_24h:25s} | "
                  f"Arr: {arrival_24h:25s} {flight.arrival_time_ahead:3s} | "
                  f"Duration: {flight.duration:12s} | "
                  f"Stops: {flight.stops} | "
                  f"{flight.price:>6s}")
        
        print()
