-- ─────────────────────────────────────────────────────────────────────────────
-- SIGS-RS PostgreSQL + TimescaleDB Database Initialization
--
-- SRS REQ-PA-3: SQL schemas compatible with PostgreSQL/TimescaleDB.
-- SRS REQ-SE-4: Immutable audit log.
-- SRS REQ-PA-1: Geolocation metadata & device IDs stripped before persistence.
-- ─────────────────────────────────────────────────────────────────────────────

-- 1. Enable TimescaleDB extension
CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE;

-- 2. Create required schemas
CREATE SCHEMA IF NOT EXISTS telemetry;
CREATE SCHEMA IF NOT EXISTS audit;
CREATE SCHEMA IF NOT EXISTS config;

-- ─────────────────────────────────────────────────────────────────────────────
-- 3. Telemetry Schema (Sanitized Data Only — NO PII)
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS telemetry.sanitized_readings (
    id              BIGSERIAL,
    ts              TIMESTAMPTZ      NOT NULL,
    ingested_at     TIMESTAMPTZ      NOT NULL DEFAULT NOW(),
    sensor_id       VARCHAR(64)      NOT NULL,
    resource_type   VARCHAR(32)      NOT NULL,
    value           DOUBLE PRECISION NOT NULL,
    unit            VARCHAR(32)      NOT NULL,
    metadata        JSONB,
    water_ext       JSONB,
    energy_ext      JSONB,
    air_quality_ext JSONB,
    PRIMARY KEY (id, ts)
);

-- Convert to TimescaleDB hypertable partitioned by timestamp (ts)
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb') THEN
        PERFORM create_hypertable(
            'telemetry.sanitized_readings',
            'ts',
            if_not_exists => TRUE,
            chunk_time_interval => INTERVAL '1 day'
        );
    END IF;
EXCEPTION
    WHEN others THEN
        RAISE NOTICE 'Could not create hypertable: %', SQLERRM;
END $$;

-- Indexes for time-series analytics
CREATE INDEX IF NOT EXISTS idx_sanitized_sensor_ts
    ON telemetry.sanitized_readings (sensor_id, ts DESC);

CREATE INDEX IF NOT EXISTS idx_sanitized_resource_ts
    ON telemetry.sanitized_readings (resource_type, ts DESC);

-- ─────────────────────────────────────────────────────────────────────────────
-- 4. Audit Schema (Immutable Security Log)
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS audit.security_log (
    id          BIGSERIAL        PRIMARY KEY,
    ts          TIMESTAMPTZ      NOT NULL DEFAULT NOW(),
    event_type  VARCHAR(64)      NOT NULL,
    severity    VARCHAR(16)      NOT NULL DEFAULT 'INFO',
    sensor_id   VARCHAR(64),
    user_id     VARCHAR(128),
    detail      JSONB            NOT NULL DEFAULT '{}',
    ip_address  VARCHAR(45)
);

CREATE INDEX IF NOT EXISTS idx_audit_ts
    ON audit.security_log (ts DESC);

CREATE INDEX IF NOT EXISTS idx_audit_event_type
    ON audit.security_log (event_type);

CREATE INDEX IF NOT EXISTS idx_audit_sensor_id
    ON audit.security_log (sensor_id);

-- Enforce immutability on audit log (SRS REQ-SE-4)
CREATE OR REPLACE FUNCTION audit.prevent_tampering()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'audit.security_log is immutable: UPDATE and DELETE operations are prohibited (SRS REQ-SE-4)';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_audit_immutable ON audit.security_log;
CREATE TRIGGER trg_audit_immutable
    BEFORE UPDATE OR DELETE ON audit.security_log
    FOR EACH ROW
    EXECUTE FUNCTION audit.prevent_tampering();

-- ─────────────────────────────────────────────────────────────────────────────
-- 5. Configuration Schema
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS config.middleware_settings (
    key         VARCHAR(128) PRIMARY KEY,
    value       JSONB        NOT NULL,
    updated_by  VARCHAR(128) NOT NULL,
    updated_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- Seed baseline configuration
INSERT INTO config.middleware_settings (key, value, updated_by)
VALUES
    ('inspector.window_seconds', '10'::jsonb, 'system'),
    ('inspector.max_packets_per_window', '50'::jsonb, 'system'),
    ('inspector.impossible_values', '{"energy_min": 0.0, "water_min": 0.0, "aqi_min": 0.0, "aqi_max": 500.0}'::jsonb, 'system'),
    ('anonymizer.pii_fields', '["latitude", "longitude", "lat", "lon", "mac_address", "device_id", "geo"]'::jsonb, 'system')
ON CONFLICT (key) DO NOTHING;
