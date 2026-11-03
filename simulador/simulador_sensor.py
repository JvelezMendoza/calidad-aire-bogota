#!/usr/bin/env python3
"""Simulador de sensores IoT propios de calidad del aire.

Imita sensores de bajo costo (tipo PMS5003) instalados en zonas sin estación oficial
(colegio, parque, zona industrial) y envía sus lecturas a la función RecibirSensor.

Solo usa la librería estándar: corre en Azure Cloud Shell sin instalar nada.

Uso:
    export FUNC_URL="https://<tu-function-app>.azurewebsites.net/api"
    export SENSOR_KEY="<function key de RecibirSensor>"
    python3 simulador_sensor.py --backfill 24      # genera historial de las últimas 24 h
    python3 simulador_sensor.py --cada 300         # envía una lectura cada 5 min (Ctrl+C para parar)
"""
import argparse
import json
import math
import os
import random
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

SENSORES = [
    # sensor_id, nombre del sitio, lat, lon, factor de contaminación relativo
    ("propio-colegio-kennedy", "Colegio distrital - Kennedy", 4.6280, -74.1530, 1.25),
    ("propio-parque-simonbolivar", "Parque Simón Bolívar", 4.6580, -74.0940, 0.70),
    ("propio-industrial-puentearanda", "Zona industrial - Puente Aranda", 4.6170, -74.1110, 1.60),
]


def valor_simulado(fecha: datetime, factor: float) -> dict:
    """PM2.5 con patrón diario (picos de tráfico 7am y 6pm hora Bogotá) + ruido."""
    hora_local = (fecha - timedelta(hours=5)).hour + fecha.minute / 60
    pico_manana = math.exp(-((hora_local - 7) ** 2) / 4)
    pico_tarde = math.exp(-((hora_local - 18) ** 2) / 5)
    base = 12 + 18 * pico_manana + 14 * pico_tarde
    pm25 = max(1.0, base * factor + random.gauss(0, 3))
    pm10 = pm25 * random.uniform(1.6, 2.1)
    return {"pm25": round(pm25, 1), "pm10": round(pm10, 1)}


def construir_lecturas(fecha: datetime) -> list[dict]:
    lecturas = []
    for sensor_id, sitio, lat, lon, factor in SENSORES:
        for parametro, valor in valor_simulado(fecha, factor).items():
            lecturas.append({
                "sensor_id": sensor_id,
                "estacion": sitio,
                "ciudad": "Bogota",
                "parametro": parametro,
                "valor": valor,
                "unidad": "µg/m³",
                "lat": lat,
                "lon": lon,
                "fecha": fecha.strftime("%Y-%m-%dT%H:%M:%SZ"),
            })
    return lecturas


def enviar(url: str, key: str, lecturas: list[dict]) -> dict:
    req = urllib.request.Request(
        f"{url.rstrip('/')}/sensor?code={key}",
        data=json.dumps(lecturas).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", default=os.environ.get("FUNC_URL"), help="URL base de la API (…/api)")
    p.add_argument("--key", default=os.environ.get("SENSOR_KEY"), help="function key de RecibirSensor")
    p.add_argument("--backfill", type=int, default=0, help="horas de historial a generar (una lectura cada 15 min)")
    p.add_argument("--cada", type=int, default=0, help="segundos entre envíos en modo continuo")
    args = p.parse_args()
    if not args.url or not args.key:
        p.error("defina --url y --key (o las variables FUNC_URL y SENSOR_KEY)")

    ahora = datetime.now(timezone.utc).replace(second=0, microsecond=0)

    if args.backfill:
        lote = []
        t = ahora - timedelta(hours=args.backfill)
        while t <= ahora:
            lote.extend(construir_lecturas(t))
            t += timedelta(minutes=15)
        for i in range(0, len(lote), 500):
            print("backfill:", enviar(args.url, args.key, lote[i:i + 500]))

    if args.cada:
        while True:
            fecha = datetime.now(timezone.utc).replace(microsecond=0)
            try:
                print(fecha.isoformat(), enviar(args.url, args.key, construir_lecturas(fecha)))
            except Exception as exc:  # el sensor no debe morir si la red o la API fallan
                print(fecha.isoformat(), "error enviando, se reintenta en el próximo ciclo:", exc, file=sys.stderr)
            time.sleep(args.cada)
    elif not args.backfill:
        print(enviar(args.url, args.key, construir_lecturas(ahora)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
