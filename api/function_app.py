"""Comparador de Calidad de Aire — microservicios serverless (Azure Functions, Python v2).

1. IngestaOpenAQ   (timer, cada hora) -> datos abiertos de OpenAQ + referencia en vivo de Open-Meteo
2. RecibirSensor   (HTTP POST)        -> recibe lecturas de sensores propios (IoT, por ahora simulados)
3. ObtenerLecturas (HTTP GET)         -> expone las lecturas (serie o perfil horario) en JSON para el dashboard
+  CargaHistorica  (HTTP POST, admin) -> utilidad de una sola vez: histórico oficial RMCAB/SIATA desde OpenAQ

Fuentes guardadas en la columna `fuente`:
  openaq  -> mediciones de estaciones publicadas en OpenAQ (incluye el histórico oficial RMCAB/SIATA)
  modelo  -> Open-Meteo Air Quality (modelo CAMS de Copernicus), referencia en vivo por ciudad
  propio  -> sensores IoT propios
"""
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone

import azure.functions as func
import requests

import db

app = func.FunctionApp()

OPENAQ_BASE = "https://api.openaq.org/v3"
OPEN_METEO_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"
CIUDADES = {
    "Bogota": (4.7110, -74.0721),
    "Medellin": (6.2442, -75.5812),
    "Cali": (3.4516, -76.5320),
}
RADIO_METROS = 25000  # máximo permitido por OpenAQ
MAX_ESTACIONES_POR_CIUDAD = int(os.environ.get("MAX_ESTACIONES_POR_CIUDAD", "15"))
DIAS_ESTACION_ACTIVA = 14  # en Colombia casi no hay estaciones reportando en vivo a OpenAQ
PARAMETROS_VALIDOS = {"pm25", "pm10", "pm1", "o3", "no2", "so2", "co", "temperature", "relativehumidity"}
# Variables de Open-Meteo -> nombre de parámetro OpenAQ
OPEN_METEO_VARIABLES = {
    "pm2_5": "pm25", "pm10": "pm10", "ozone": "o3",
    "nitrogen_dioxide": "no2", "sulphur_dioxide": "so2", "carbon_monoxide": "co",
}


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------
def _json(body, status=200):
    return func.HttpResponse(
        json.dumps(body, ensure_ascii=False, default=str),
        status_code=status,
        headers={"Content-Type": "application/json; charset=utf-8"},
    )


def _ahora_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


def _parse_fecha(valor: str | None) -> datetime:
    """ISO 8601 -> datetime UTC naive (como se guarda en SQL)."""
    if not valor:
        return _ahora_utc()
    dt = datetime.fromisoformat(valor.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(tzinfo=None, microsecond=0)


def _openaq_session() -> requests.Session | None:
    api_key = os.environ.get("OPENAQ_API_KEY", "").strip()
    if not api_key:
        return None
    session = requests.Session()
    session.headers.update({"X-API-Key": api_key, "Accept": "application/json"})
    return session


def _openaq_get(session: requests.Session, path: str, params: dict | None = None) -> dict:
    """GET a OpenAQ con un reintento ante 429/5xx."""
    for intento in range(2):
        resp = session.get(f"{OPENAQ_BASE}{path}", params=params, timeout=30)
        if resp.status_code == 429 or resp.status_code >= 500:
            espera = int(resp.headers.get("x-ratelimit-reset", "5")) if intento == 0 else 0
            logging.warning("OpenAQ %s respondió %s; reintento en %ss", path, resp.status_code, espera)
            time.sleep(min(espera, 30))
            continue
        resp.raise_for_status()
        return resp.json()
    resp.raise_for_status()
    return resp.json()


def _estaciones(session: requests.Session, lat: float, lon: float) -> list[dict]:
    data = _openaq_get(session, "/locations", {"coordinates": f"{lat},{lon}", "radius": RADIO_METROS, "limit": 100})
    return data.get("results", [])


def _guardar(lecturas: list[dict]) -> int:
    conn = db.get_connection()
    try:
        db.ensure_schema(conn)
        return db.insert_lecturas(conn, lecturas)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Fuentes de datos
# ---------------------------------------------------------------------------
def ingestar_openaq_ciudad(session: requests.Session, ciudad: str, lat: float, lon: float) -> list[dict]:
    """Última medición de cada sensor de las estaciones de OpenAQ que siguen reportando."""
    limite = _ahora_utc() - timedelta(days=DIAS_ESTACION_ACTIVA)
    activas = [
        loc for loc in _estaciones(session, lat, lon)
        if (loc.get("datetimeLast") or {}).get("utc") and _parse_fecha(loc["datetimeLast"]["utc"]) >= limite
    ][:MAX_ESTACIONES_POR_CIUDAD]
    logging.info("OpenAQ %s: %s estaciones activas", ciudad, len(activas))

    lecturas = []
    for loc in activas:
        sensores = {s["id"]: s.get("parameter", {}) for s in loc.get("sensors", [])}
        try:
            latest = _openaq_get(session, f"/locations/{loc['id']}/latest")
        except requests.RequestException as exc:
            # Una estación caída no debe tumbar la ingesta del resto
            logging.warning("Estación %s (%s) falló: %s", loc.get("name"), loc["id"], exc)
            continue
        for m in latest.get("results", []):
            param = sensores.get(m.get("sensorsId"), {})
            nombre_param = (param.get("name") or "").lower()
            if nombre_param not in PARAMETROS_VALIDOS or m.get("value") is None or m["value"] < 0:
                continue
            coords = m.get("coordinates") or loc.get("coordinates") or {}
            lecturas.append({
                "fuente": "openaq",
                "ciudad": ciudad,
                "parametro": nombre_param,
                "sensor_id": m["sensorsId"],
                "estacion": loc.get("name"),
                "unidad": param.get("units"),
                "lat": coords.get("latitude"),
                "lon": coords.get("longitude"),
                "fecha": _parse_fecha(m["datetime"]["utc"]),
                "valor": float(m["value"]),
            })
        time.sleep(1.1)  # respetar el límite de 60 peticiones/minuto de OpenAQ
    return lecturas


def ingestar_open_meteo_ciudad(ciudad: str, lat: float, lon: float) -> list[dict]:
    """Referencia en vivo: modelo CAMS (Open-Meteo) para el centro de la ciudad, últimas 48 h."""
    resp = requests.get(OPEN_METEO_URL, params={
        "latitude": lat, "longitude": lon, "hourly": ",".join(OPEN_METEO_VARIABLES),
        "past_hours": 48, "forecast_hours": 1, "timezone": "UTC",
    }, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    horas = data["hourly"]["time"]
    unidades = data.get("hourly_units", {})
    ahora = _ahora_utc()
    lecturas = []
    for variable, parametro in OPEN_METEO_VARIABLES.items():
        for fecha_txt, valor in zip(horas, data["hourly"].get(variable, [])):
            fecha = _parse_fecha(fecha_txt)
            if valor is None or fecha > ahora:
                continue
            lecturas.append({
                "fuente": "modelo",
                "ciudad": ciudad,
                "parametro": parametro,
                "sensor_id": f"open-meteo-{ciudad.lower()}",
                "estacion": "Open-Meteo (modelo CAMS)",
                "unidad": unidades.get(variable, "µg/m³").replace("μ", "µ"),
                "lat": lat,
                "lon": lon,
                "fecha": fecha,
                "valor": float(valor),
            })
    return lecturas


# ---------------------------------------------------------------------------
# 1. IngestaOpenAQ — cada hora en el minuto 5
# ---------------------------------------------------------------------------
@app.function_name("IngestaOpenAQ")
@app.timer_trigger(schedule="0 5 * * * *", arg_name="timer", run_on_startup=False, use_monitor=True)
def ingesta_openaq(timer: func.TimerRequest) -> None:
    session = _openaq_session()
    if session is None:
        logging.error("OPENAQ_API_KEY no está configurada; se omite OpenAQ (Open-Meteo sí se ingesta).")

    resumen = {}
    todas = []
    for ciudad, (lat, lon) in CIUDADES.items():
        # Cada fuente y cada ciudad en su propio try: si una falla, las demás siguen (resiliencia)
        if session is not None:
            try:
                l = ingestar_openaq_ciudad(session, ciudad, lat, lon)
                todas.extend(l)
                resumen[f"{ciudad}/openaq"] = len(l)
            except Exception as exc:
                logging.exception("Falló OpenAQ para %s: %s", ciudad, exc)
                resumen[f"{ciudad}/openaq"] = f"error: {exc}"
        try:
            l = ingestar_open_meteo_ciudad(ciudad, lat, lon)
            todas.extend(l)
            resumen[f"{ciudad}/modelo"] = len(l)
        except Exception as exc:
            logging.exception("Falló Open-Meteo para %s: %s", ciudad, exc)
            resumen[f"{ciudad}/modelo"] = f"error: {exc}"

    if not todas:
        logging.warning("Ingesta sin lecturas. Resumen: %s", resumen)
        return
    try:
        nuevas = _guardar(todas)
        logging.info("Ingesta OK: %s lecturas obtenidas, %s nuevas. Resumen: %s", len(todas), nuevas, resumen)
    except Exception as exc:
        logging.exception("No se pudo guardar en SQL: %s", exc)


# ---------------------------------------------------------------------------
# Utilidad: CargaHistorica — POST /api/historico?ciudad=Bogota&dias=30&parametros=pm25,pm10
# Trae las mediciones horarias de los últimos `dias` que reportó cada estación oficial antes de
# dejar de publicar en OpenAQ (RMCAB hasta 2022, SIATA hasta 2024). Línea base para el análisis.
# ---------------------------------------------------------------------------
@app.function_name("CargaHistorica")
@app.route(route="historico", methods=["POST"], auth_level=func.AuthLevel.ADMIN)
def carga_historica(req: func.HttpRequest) -> func.HttpResponse:
    ciudad = req.params.get("ciudad", "Bogota")
    if ciudad not in CIUDADES:
        return _json({"error": f"ciudad inválida. Válidas: {list(CIUDADES)}"}, 400)
    try:
        dias = max(1, min(int(req.params.get("dias", "30")), 90))
    except ValueError:
        return _json({"error": "dias debe ser un entero"}, 400)
    parametros = {p.strip().lower() for p in req.params.get("parametros", "pm25,pm10").split(",")} & PARAMETROS_VALIDOS
    proveedor = req.params.get("proveedor", "Bogota" if ciudad == "Bogota" else "Medellin")
    session = _openaq_session()
    if session is None:
        return _json({"error": "OPENAQ_API_KEY no configurada"}, 503)

    lat, lon = CIUDADES[ciudad]
    estaciones = [loc for loc in _estaciones(session, lat, lon)
                  if (loc.get("provider") or {}).get("name") == proveedor and (loc.get("datetimeLast") or {}).get("utc")]
    lecturas, errores = [], []
    for loc in estaciones:
        hasta = _parse_fecha(loc["datetimeLast"]["utc"])
        desde = hasta - timedelta(days=dias)
        coords = loc.get("coordinates") or {}
        for s in loc.get("sensors", []):
            param = s.get("parameter", {})
            nombre = (param.get("name") or "").lower()
            if nombre not in parametros:
                continue
            pagina = 1
            while True:
                try:
                    data = _openaq_get(session, f"/sensors/{s['id']}/hours", {
                        "datetime_from": desde.isoformat() + "Z", "datetime_to": hasta.isoformat() + "Z",
                        "limit": 1000, "page": pagina,
                    })
                except requests.RequestException as exc:
                    errores.append(f"{loc.get('name')}/{nombre}: {exc}")
                    break
                resultados = data.get("results", [])
                for m in resultados:
                    if m.get("value") is None or m["value"] < 0:
                        continue
                    lecturas.append({
                        "fuente": "openaq",
                        "ciudad": ciudad,
                        "parametro": nombre,
                        "sensor_id": s["id"],
                        "estacion": loc.get("name"),
                        "unidad": param.get("units"),
                        "lat": coords.get("latitude"),
                        "lon": coords.get("longitude"),
                        "fecha": _parse_fecha(m["period"]["datetimeFrom"]["utc"]),
                        "valor": float(m["value"]),
                    })
                time.sleep(1.1)
                if len(resultados) < 1000:
                    break
                pagina += 1

    try:
        nuevas = _guardar(lecturas)
    except Exception as exc:
        logging.exception("Error guardando histórico: %s", exc)
        return _json({"error": "no se pudo guardar", "detalle": str(exc)}, 503)
    return _json({"ciudad": ciudad, "proveedor": proveedor, "estaciones": len(estaciones),
                  "obtenidas": len(lecturas), "guardadas": nuevas, "errores": errores[:20]})


# ---------------------------------------------------------------------------
# 2. RecibirSensor — POST /api/sensor  (requiere function key)
# ---------------------------------------------------------------------------
def _validar_lectura(d: dict) -> dict:
    if not isinstance(d, dict):
        raise ValueError("cada lectura debe ser un objeto JSON")
    faltan = [k for k in ("sensor_id", "ciudad", "parametro", "valor") if d.get(k) in (None, "")]
    if faltan:
        raise ValueError(f"faltan campos: {', '.join(faltan)}")
    parametro = str(d["parametro"]).lower()
    if parametro not in PARAMETROS_VALIDOS:
        raise ValueError(f"parametro inválido '{parametro}'. Válidos: {sorted(PARAMETROS_VALIDOS)}")
    valor = float(d["valor"])
    if not 0 <= valor <= 5000:
        raise ValueError("valor fuera de rango (0-5000)")
    lat = float(d["lat"]) if d.get("lat") is not None else None
    lon = float(d["lon"]) if d.get("lon") is not None else None
    if lat is not None and not -90 <= lat <= 90 or lon is not None and not -180 <= lon <= 180:
        raise ValueError("coordenadas inválidas")
    return {
        "fuente": "propio",
        "ciudad": str(d["ciudad"])[:50],
        "parametro": parametro,
        "sensor_id": str(d["sensor_id"])[:64],
        "estacion": str(d["estacion"])[:200] if d.get("estacion") else None,
        "unidad": str(d.get("unidad") or "µg/m³")[:20],
        "lat": lat,
        "lon": lon,
        "fecha": _parse_fecha(d.get("fecha")),
        "valor": valor,
    }


@app.function_name("RecibirSensor")
@app.route(route="sensor", methods=["POST"], auth_level=func.AuthLevel.FUNCTION)
def recibir_sensor(req: func.HttpRequest) -> func.HttpResponse:
    try:
        body = req.get_json()
    except ValueError:
        return _json({"error": "el cuerpo debe ser JSON válido"}, 400)

    items = body if isinstance(body, list) else [body]
    if not items or len(items) > 500:
        return _json({"error": "envíe entre 1 y 500 lecturas"}, 400)

    try:
        lecturas = [_validar_lectura(i) for i in items]
    except (ValueError, TypeError) as exc:
        return _json({"error": str(exc)}, 400)

    try:
        nuevas = _guardar(lecturas)
    except Exception as exc:
        logging.exception("Error guardando lecturas del sensor: %s", exc)
        return _json({"error": "no se pudo guardar, intente de nuevo"}, 503)

    return _json({"recibidas": len(lecturas), "guardadas": nuevas}, 201)


# ---------------------------------------------------------------------------
# 3. ObtenerLecturas — GET /api/lecturas?ciudad=Bogota&parametro=pm25&horas=48[&fuente=openaq]
#                      GET /api/lecturas?ciudad=Bogota&parametro=pm25&modo=perfil
#    modo=perfil -> promedio por hora del día (hora Bogotá) y fuente sobre TODO el histórico
# ---------------------------------------------------------------------------
_SQL_SERIE = """
    SELECT TOP (8000) fuente, ciudad, parametro, sensor_id, estacion, unidad, lat, lon, fecha, valor
    FROM dbo.lecturas
    WHERE ciudad = ? AND parametro = ? AND fecha >= DATEADD(HOUR, -?, SYSUTCDATETIME())
"""

_SQL_PERFIL = """
    SELECT fuente,
           DATEPART(HOUR, DATEADD(HOUR, -5, fecha)) AS hora_local,
           ROUND(AVG(valor), 1) AS promedio,
           COUNT(*) AS n,
           MIN(fecha) AS desde,
           MAX(fecha) AS hasta
    FROM dbo.lecturas
    WHERE ciudad = ? AND parametro = ?
    GROUP BY fuente, DATEPART(HOUR, DATEADD(HOUR, -5, fecha))
    ORDER BY fuente, hora_local
"""


@app.function_name("ObtenerLecturas")
@app.route(route="lecturas", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS)
def obtener_lecturas(req: func.HttpRequest) -> func.HttpResponse:
    ciudad = req.params.get("ciudad", "Bogota")
    parametro = req.params.get("parametro", "pm25").lower()
    fuente = req.params.get("fuente")
    modo = req.params.get("modo", "serie")
    try:
        horas = max(1, min(int(req.params.get("horas", "48")), 720))
    except ValueError:
        return _json({"error": "horas debe ser un entero"}, 400)

    if modo == "perfil":
        sql, params = _SQL_PERFIL, [ciudad, parametro]
    else:
        sql, params = _SQL_SERIE, [ciudad, parametro, horas]
        if fuente:
            sql += " AND fuente = ?"
            params.append(fuente)
        sql += " ORDER BY fecha ASC"

    try:
        conn = db.get_connection()
        try:
            db.ensure_schema(conn)
            cur = conn.cursor()
            cur.execute(sql, params)
            cols = [c[0] for c in cur.description]
            filas = []
            for row in cur.fetchall():
                r = dict(zip(cols, row))
                for k in ("fecha", "desde", "hasta"):
                    if isinstance(r.get(k), datetime):
                        r[k] = r[k].isoformat() + "Z"
                filas.append(r)
        finally:
            conn.close()
    except Exception as exc:
        logging.exception("Error consultando lecturas: %s", exc)
        return _json({"error": "base de datos no disponible, intente en un minuto"}, 503)

    if modo == "perfil":
        return _json({"filtros": {"ciudad": ciudad, "parametro": parametro, "modo": "perfil"}, "perfil": filas})
    return _json({
        "filtros": {"ciudad": ciudad, "parametro": parametro, "horas": horas, "fuente": fuente},
        "total": len(filas),
        "lecturas": filas,
    })
