"""Acceso a Azure SQL Database compartido por las tres funciones."""
import logging
import os
import time

import pyodbc

# Errores transitorios de Azure SQL (p. ej. la base serverless se está reanudando tras auto-pausa)
_TRANSIENT_ERRORS = ("40613", "40197", "40501", "49918", "49919", "49920", "4060", "10928", "10929", "HYT00", "08S01", "08001")

_SCHEMA_STATEMENTS = [
    """
    IF OBJECT_ID('dbo.lecturas', 'U') IS NULL
    CREATE TABLE dbo.lecturas (
        id         BIGINT IDENTITY(1,1) PRIMARY KEY,
        fuente     VARCHAR(20)   NOT NULL,
        ciudad     VARCHAR(50)   NOT NULL,
        parametro  VARCHAR(20)   NOT NULL,
        sensor_id  VARCHAR(64)   NOT NULL,
        estacion   NVARCHAR(200) NULL,
        unidad     VARCHAR(20)   NULL,
        lat        FLOAT         NULL,
        lon        FLOAT         NULL,
        fecha      DATETIME2(0)  NOT NULL,
        valor      FLOAT         NOT NULL,
        creado_en  DATETIME2(0)  NOT NULL DEFAULT SYSUTCDATETIME()
    )
    """,
    """
    IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ux_lecturas_medicion')
    CREATE UNIQUE INDEX ux_lecturas_medicion ON dbo.lecturas (fuente, sensor_id, parametro, fecha)
    """,
    """
    IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_lecturas_consulta')
    CREATE INDEX ix_lecturas_consulta ON dbo.lecturas (ciudad, parametro, fecha DESC)
        INCLUDE (fuente, valor, sensor_id, estacion, lat, lon)
    """,
]

_schema_ready = False

_CREATE_STAGING = """
CREATE TABLE #staging (
    fuente VARCHAR(20), ciudad VARCHAR(50), parametro VARCHAR(20), sensor_id VARCHAR(64),
    estacion NVARCHAR(200), unidad NVARCHAR(20), lat FLOAT, lon FLOAT, fecha DATETIME2(0), valor FLOAT
)
"""

# Mueve el lote a la tabla final descartando duplicados (dentro del lote y contra lo ya guardado)
_MERGE_STAGING = """
INSERT INTO dbo.lecturas (fuente, ciudad, parametro, sensor_id, estacion, unidad, lat, lon, fecha, valor)
SELECT fuente, ciudad, parametro, sensor_id, estacion, unidad, lat, lon, fecha, valor
FROM (
    SELECT s.*, ROW_NUMBER() OVER (PARTITION BY fuente, sensor_id, parametro, fecha ORDER BY (SELECT 0)) AS rn
    FROM #staging s
) s
WHERE rn = 1 AND NOT EXISTS (
    SELECT 1 FROM dbo.lecturas l WITH (UPDLOCK, HOLDLOCK)
    WHERE l.fuente = s.fuente AND l.sensor_id = s.sensor_id AND l.parametro = s.parametro AND l.fecha = s.fecha
)
"""


def get_connection(retries: int = 5) -> pyodbc.Connection:
    """Abre una conexión con reintentos (la base serverless puede tardar ~1 min en reanudar)."""
    conn_str = os.environ["SQL_CONNECTION_STRING"]
    delay = 5
    for intento in range(1, retries + 1):
        try:
            return pyodbc.connect(conn_str, timeout=30)
        except pyodbc.Error as exc:
            transitorio = any(code in str(exc) for code in _TRANSIENT_ERRORS)
            if not transitorio or intento == retries:
                raise
            logging.warning("SQL no disponible (intento %s/%s): %s. Reintentando en %ss", intento, retries, exc, delay)
            time.sleep(delay)
            delay = min(delay * 2, 30)


def ensure_schema(conn: pyodbc.Connection) -> None:
    global _schema_ready
    if _schema_ready:
        return
    cur = conn.cursor()
    for stmt in _SCHEMA_STATEMENTS:
        cur.execute(stmt)
    conn.commit()
    _schema_ready = True


def insert_lecturas(conn: pyodbc.Connection, lecturas: list[dict]) -> int:
    """Inserta lecturas en bloque ignorando duplicados. Devuelve cuántas filas nuevas se guardaron."""
    if not lecturas:
        return 0
    cur = conn.cursor()
    cur.execute("IF OBJECT_ID('tempdb..#staging') IS NOT NULL DROP TABLE #staging")
    cur.execute(_CREATE_STAGING)
    cur.fast_executemany = True
    cur.executemany(
        "INSERT INTO #staging VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(
            l["fuente"], l["ciudad"], l["parametro"], str(l["sensor_id"]), l.get("estacion"), l.get("unidad"),
            l.get("lat"), l.get("lon"), l["fecha"], l["valor"],
        ) for l in lecturas],
    )
    cur.fast_executemany = False
    cur.execute(_MERGE_STAGING)
    nuevas = cur.rowcount
    cur.execute("DROP TABLE #staging")
    conn.commit()
    return nuevas
