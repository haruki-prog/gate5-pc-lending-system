PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS employees (
 id INTEGER PRIMARY KEY, name VARCHAR(50) NOT NULL,
 department VARCHAR(50) NOT NULL,
 employment_status VARCHAR(20) NOT NULL CHECK(employment_status IN ('ACTIVE','LEAVE','RETIRED'))
);
CREATE TABLE IF NOT EXISTS devices (
 id INTEGER PRIMARY KEY, asset_no VARCHAR(20) NOT NULL UNIQUE,
 model_name VARCHAR(50) NOT NULL,
 device_type VARCHAR(20) NOT NULL CHECK(device_type IN ('LAPTOP','TABLET','MONITOR')),
 status VARCHAR(20) NOT NULL DEFAULT 'AVAILABLE' CHECK(status IN ('AVAILABLE','LENT','REPAIR','DISPOSED')),
 purchased_at DATE NOT NULL
);
CREATE TABLE IF NOT EXISTS lendings (
 id INTEGER PRIMARY KEY, device_id INTEGER NOT NULL REFERENCES devices(id),
 user_id INTEGER NOT NULL REFERENCES employees(id), lent_at DATETIME NOT NULL,
 due_date DATE NOT NULL, returned_at DATETIME,
 purpose VARCHAR(100) NOT NULL CHECK(length(purpose) BETWEEN 1 AND 100),
 CHECK(returned_at IS NULL OR returned_at >= lent_at)
);
CREATE UNIQUE INDEX IF NOT EXISTS one_open_lending_per_device
 ON lendings(device_id) WHERE returned_at IS NULL;
CREATE INDEX IF NOT EXISTS lending_by_user ON lendings(user_id,returned_at);
CREATE TABLE IF NOT EXISTS operation_requests (
 request_key VARCHAR(43) PRIMARY KEY CHECK(length(request_key)=43),
 user_id INTEGER NOT NULL REFERENCES employees(id),
 operation VARCHAR(10) NOT NULL CHECK(operation IN ('LEND','RETURN')),
 payload_json TEXT NOT NULL,
 status VARCHAR(10) NOT NULL DEFAULT 'READY' CHECK(status IN ('READY','SUCCEEDED')),
 created_at DATETIME NOT NULL, expires_at DATETIME NOT NULL,
 result_lending_id INTEGER REFERENCES lendings(id), result_json TEXT, completed_at DATETIME,
 CHECK((status='READY' AND result_lending_id IS NULL AND result_json IS NULL AND completed_at IS NULL)
 OR (status='SUCCEEDED' AND result_lending_id IS NOT NULL AND result_json IS NOT NULL AND completed_at IS NOT NULL))
);
CREATE TABLE IF NOT EXISTS device_holds (
 device_id INTEGER PRIMARY KEY REFERENCES devices(id), reason VARCHAR(200) NOT NULL,
 held_at DATETIME NOT NULL, released_at DATETIME
);
CREATE TABLE IF NOT EXISTS maintenance_audit (
 id INTEGER PRIMARY KEY, device_id INTEGER NOT NULL REFERENCES devices(id),
 operator TEXT NOT NULL, reason TEXT NOT NULL, before_json TEXT NOT NULL,
 after_json TEXT NOT NULL, changed_at DATETIME NOT NULL
);
CREATE TABLE IF NOT EXISTS app_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
