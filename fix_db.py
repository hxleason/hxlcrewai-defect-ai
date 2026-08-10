import sqlite3
conn = sqlite3.connect("app/db/database.db")
c = conn.cursor()
for col in ["started_at TIMESTAMP", "completed_at TIMESTAMP", "last_heartbeat TIMESTAMP"]:
    try:
        c.execute(f"ALTER TABLE tasks ADD COLUMN {col}")
    except sqlite3.OperationalError as e:
        if "duplicate column" in str(e): pass
conn.commit()
conn.close()
print("OK")