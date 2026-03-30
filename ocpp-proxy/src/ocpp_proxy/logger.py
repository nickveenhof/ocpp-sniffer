import datetime
import sqlite3
from typing import Any


class EventLogger:
    """
    Track charger sessions and revenue, persist in SQLite.
    """

    def __init__(self, db_path: str = "usage_log.db"):
        self.db_path = db_path
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                timestamp TEXT,
                backend_id TEXT,
                duration_s REAL,
                energy_kwh REAL,
                revenue REAL,
                id_tag TEXT
            )
        """
        )
        conn.commit()
        conn.close()

    def log_session(
        self,
        backend_id: str,
        duration_s: float,
        energy_kwh: float,
        revenue: float,
        id_tag: str = "",
    ) -> None:
        """Persist a session record into SQLite."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO sessions (timestamp, backend_id, duration_s, energy_kwh, revenue, id_tag) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                datetime.datetime.now(datetime.UTC).isoformat(),
                backend_id,
                duration_s,
                energy_kwh,
                revenue,
                id_tag,
            ),
        )
        conn.commit()
        conn.close()

    def get_sessions(self) -> list[dict[str, Any]]:
        """Fetch all logged sessions as list of dicts."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT timestamp, backend_id, duration_s, energy_kwh, revenue, id_tag "
            "FROM sessions ORDER BY timestamp"
        )
        rows = cursor.fetchall()
        conn.close()

        sessions = []
        for ts, backend, dur, energy, rev, id_tag in rows:
            sessions.append(
                {
                    "timestamp": ts,
                    "backend_id": backend,
                    "duration_s": dur,
                    "energy_kwh": energy,
                    "revenue": rev,
                    "id_tag": id_tag or "",
                }
            )
        return sessions

    def export_db(self) -> str:
        """Return path to the SQLite database file."""
        return self.db_path
