-- Esquema replicado del modelo de datos de OpenAQ (estación, contaminante, valor, ubicación, fecha).
-- La Function App lo crea automáticamente si no existe; este archivo es la referencia/documentación.

IF OBJECT_ID('dbo.lecturas', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.lecturas (
        id         BIGINT IDENTITY(1,1) PRIMARY KEY,
        fuente     VARCHAR(20)   NOT NULL,          -- 'openaq' (estaciones) | 'modelo' (Open-Meteo) | 'propio' (sensor IoT)
        ciudad     VARCHAR(50)   NOT NULL,          -- Bogota, Medellin, Cali
        parametro  VARCHAR(20)   NOT NULL,          -- pm25, pm10, o3, no2, co, so2...
        sensor_id  VARCHAR(64)   NOT NULL,          -- id del sensor en OpenAQ o id del sensor propio
        estacion   NVARCHAR(200) NULL,              -- nombre de la estación / sitio
        unidad     VARCHAR(20)   NULL,              -- µg/m³, ppm...
        lat        FLOAT         NULL,
        lon        FLOAT         NULL,
        fecha      DATETIME2(0)  NOT NULL,          -- UTC
        valor      FLOAT         NOT NULL,
        creado_en  DATETIME2(0)  NOT NULL DEFAULT SYSUTCDATETIME()
    );

    -- Evita duplicar una misma medición cuando la ingesta horaria la vuelve a traer
    CREATE UNIQUE INDEX ux_lecturas_medicion
        ON dbo.lecturas (fuente, sensor_id, parametro, fecha);

    -- Consultas del dashboard: por ciudad/parámetro/rango de fechas
    CREATE INDEX ix_lecturas_consulta
        ON dbo.lecturas (ciudad, parametro, fecha DESC) INCLUDE (fuente, valor, sensor_id, estacion, lat, lon);
END;
