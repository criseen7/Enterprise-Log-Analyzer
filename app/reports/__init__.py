"""Exportación de reportes (Fase 5)."""

from __future__ import annotations

import csv
import io
from typing import Any

import sqlite3


def export_incidents_csv(conn: sqlite3.Connection, *, status: str | None = None) -> bytes:
    query = """
        SELECT id, fingerprint, category, severity, sample_message,
               first_seen, last_seen, event_count, status, notified_at
        FROM incidents
    """
    params: list[Any] = []
    if status:
        query += " WHERE status = ?"
        params.append(status)
    query += " ORDER BY severity DESC, last_seen DESC"
    rows = conn.execute(query, params).fetchall()
    buf = io.StringIO(newline="")
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(
        [
            "id",
            "fingerprint",
            "category",
            "severity",
            "sample_message",
            "first_seen",
            "last_seen",
            "event_count",
            "status",
            "notified_at",
        ]
    )
    for r in rows:
        d = dict(r)
        writer.writerow(
            [
                d["id"],
                d["fingerprint"],
                d["category"],
                d["severity"],
                d["sample_message"],
                d["first_seen"],
                d["last_seen"],
                d["event_count"],
                d["status"],
                d["notified_at"],
            ]
        )
    # Excel-friendly: utf-8-sig
    return buf.getvalue().encode("utf-8-sig")


def export_incidents_html(conn: sqlite3.Connection, *, status: str | None = None) -> str:
    query = """
        SELECT id, fingerprint, category, severity, sample_message,
               first_seen, last_seen, event_count, status, notified_at
        FROM incidents
    """
    params: list[Any] = []
    if status:
        query += " WHERE status = ?"
        params.append(status)
    query += " ORDER BY severity DESC, last_seen DESC"
    rows = conn.execute(query, params).fetchall()

    def esc(v: Any) -> str:
        if v is None:
            return ""
        s = str(v)
        return (
            s.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
            .replace("'", "&#39;")
        )

    parts = [
        "<!DOCTYPE html>",
        "<html><head><meta charset=\"utf-8\"><title>Incidents</title>",
        "<style>table{border-collapse:collapse}td,th{border:1px solid #ccc;padding:4px}</style>",
        "</head><body>",
        "<h1>Incidents</h1>",
        "<table><thead><tr>",
        "<th>id</th><th>fingerprint</th><th>category</th><th>severity</th><th>sample_message</th>",
        "<th>first_seen</th><th>last_seen</th><th>event_count</th><th>status</th><th>notified_at</th>",
        "</tr></thead><tbody>",
    ]
    for r in rows:
        d = dict(r)
        parts.append(
            "<tr>"
            f"<td>{esc(d['id'])}</td>"
            f"<td>{esc(d['fingerprint'])}</td>"
            f"<td>{esc(d['category'])}</td>"
            f"<td>{esc(d['severity'])}</td>"
            f"<td>{esc(d['sample_message'])}</td>"
            f"<td>{esc(d['first_seen'])}</td>"
            f"<td>{esc(d['last_seen'])}</td>"
            f"<td>{esc(d['event_count'])}</td>"
            f"<td>{esc(d['status'])}</td>"
            f"<td>{esc(d['notified_at'])}</td>"
            "</tr>"
        )
    parts.append("</tbody></table></body></html>")
    return "\n".join(parts)


__all__ = ["export_incidents_csv", "export_incidents_html"]
