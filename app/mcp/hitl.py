"""Aprobación humana en el bucle (HITL).

La regla que gobierna este módulo: **el agente no puede aprobar su propia
mutación**.

`set_incident_status` no cambia el estado de un incidente a la primera. Lo que
hace es registrar una *acción pendiente* y devolver un `action_id`. Un humano
—la persona que opera el servidor— aprueba esa acción desde su terminal. Al
aprobarse se emite un `confirm_token`, que es la prueba criptográfica de que
esa aprobación ocurrió. El agente vuelve a llamar con el token y sólo entonces
se ejecuta la mutación.

Por qué el token es un HMAC y no un `action_id`:

* Si bastara el `action_id`, el agente podría inventar uno, o adivinar el de
  otra acción, y se autoaprobaría.
* El HMAC se calcula sobre la acción completa (id, tool, parámetros, caducidad)
  con una clave que sólo el servidor posee. Sin la clave, no hay token; con la
  clave, el token ata el token a *esta* acción y a *estos* parámetros.
* La caducidad (TTL) evita que una aprobación de hace una semana siga
  valiendo hoy. Un token robado de un log queda inservible a los pocos minutos.

Por qué la acción vive en la base y no en memoria: el servidor MCP y la CLI
de operador son **dos procesos distintos**. Si el pendiente viviera en memoria,
el token no podría cruzar entre ellos y el flujo de aprobación sólo existiría
dentro de las pruebas.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.config import Settings, get_settings
from app.timeutil import to_canonical

PENDING = "PENDING"
APPROVED = "APPROVED"
EXECUTED = "EXECUTED"
REJECTED = "REJECTED"
EXPIRED = "EXPIRED"

VALID_STATUSES = (PENDING, APPROVED, EXECUTED, REJECTED, EXPIRED)

# Caducidad por defecto de una aprobación: 15 minutos. Suficiente para que una
# persona mire la pantalla, consulte y responda; demasiado corto para que un
# token robado de un log sirva de algo.
DEFAULT_TTL_SECONDS = 900

# Variable de entorno con la clave del HMAC. Si está, manda sobre el fichero.
SECRET_ENV_VAR = "LOGLENS_HITL_SECRET"


class HitlError(Exception):
    """Fallo de aprobación previsto: la tool lo convierte en `ToolError`."""


@dataclass(frozen=True)
class Action:
    """Una acción registrada, con su estado de aprobación."""

    id: str
    tool: str
    params: dict[str, object]
    status: str
    confirm_token: str | None
    created_at: str
    expires_at: str
    approved_at: str | None = None
    approved_by: str | None = None
    executed_at: str | None = None
    result: str | None = None


def _now() -> datetime:
    return datetime.now(UTC)


def _stamp(moment: datetime) -> str:
    return to_canonical(moment)


def secret_path(settings: Settings | None = None) -> Path:
    """Ruta del fichero donde se guarda la clave si no hay variable de entorno."""
    settings = settings or get_settings()
    db_path = settings.paths.db_path
    return db_path.with_name(db_path.name + ".hitl_secret")


def load_secret(settings: Settings | None = None) -> bytes:
    """Clave del HMAC.

    Orden de preferencia:

    1. `LOGLENS_HITL_SECRET` — explícita, la opción para producción: sobrevive
       al reinicio y se puede rotar sin tocar disco.
    2. Un fichero junto a la base, generado la primera vez con permisos 0600.
       Existe para que la instalación funcione sin configuración y para que el
       servidor MCP y la CLI (procesos distintos) compartan la misma clave.

    La clave nunca se registra ni se devuelve en una respuesta de la tool.
    """
    env = os.environ.get(SECRET_ENV_VAR)
    if env:
        return env.encode("utf-8")

    settings = settings or get_settings()
    path = secret_path(settings)
    if path.exists():
        return path.read_bytes()

    path.parent.mkdir(parents=True, exist_ok=True)
    value = secrets.token_hex(32).encode("ascii")
    descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(value)
    return value


def _payload(action_id: str, tool: str, params: dict[str, object], expires_at: str) -> bytes:
    """Bytes que se firman. Canónicos para que el token sea reproducible."""
    canonical_params = json.dumps(params, sort_keys=True, separators=(",", ":"))
    return "|".join([action_id, tool, canonical_params, expires_at]).encode("utf-8")


def compute_token(
    secret: bytes,
    action_id: str,
    tool: str,
    params: dict[str, object],
    expires_at: str,
) -> str:
    """Firma HMAC-SHA256 de una acción."""
    return hmac.new(
        secret, _payload(action_id, tool, params, expires_at), hashlib.sha256
    ).hexdigest()


def _row_to_action(row: sqlite3.Row) -> Action:
    try:
        params = json.loads(row["params"])
    except (TypeError, ValueError):
        params = {}
    if not isinstance(params, dict):
        params = {}
    return Action(
        id=str(row["id"]),
        tool=str(row["tool"]),
        params=params,
        status=str(row["status"]),
        confirm_token=row["confirm_token"],
        created_at=str(row["created_at"]),
        expires_at=str(row["expires_at"]),
        approved_at=row["approved_at"],
        approved_by=row["approved_by"],
        executed_at=row["executed_at"],
        result=row["result"],
    )


def create_action(
    conn: sqlite3.Connection,
    *,
    tool: str,
    params: dict[str, object],
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> Action:
    """Registra una acción pendiente de aprobación.

    No muta nada: sólo deja constancia de lo que se quiere hacer y de quién lo
    pidió, para que la aprobación sea auditable.
    """
    now = _now()
    expires_at = _stamp(now + timedelta(seconds=ttl_seconds))
    action_id = secrets.token_urlsafe(12)
    conn.execute(
        """
        INSERT INTO agent_actions
            (id, tool, params, status, confirm_token, created_at, expires_at)
        VALUES (?, ?, ?, ?, NULL, ?, ?)
        """,
        (
            action_id,
            tool,
            json.dumps(params, sort_keys=True, separators=(",", ":")),
            PENDING,
            _stamp(now),
            expires_at,
        ),
    )
    return Action(
        id=action_id,
        tool=tool,
        params=dict(params),
        status=PENDING,
        confirm_token=None,
        created_at=_stamp(now),
        expires_at=expires_at,
    )


def get_action(conn: sqlite3.Connection, action_id: str) -> Action | None:
    row = conn.execute("SELECT * FROM agent_actions WHERE id = ?", (action_id,)).fetchone()
    return _row_to_action(row) if row is not None else None


def list_actions(
    conn: sqlite3.Connection,
    *,
    status: str | None = None,
    limit: int = 50,
) -> list[Action]:
    """Acciones registradas, de la más reciente a la más antigua."""
    if status is not None and status not in VALID_STATUSES:
        raise ValueError(f"status inválido: {status!r}, esperado uno de {VALID_STATUSES}")
    sql = "SELECT * FROM agent_actions"
    params: list[object] = []
    if status is not None:
        sql += " WHERE status = ?"
        params.append(status)
    sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
    params.append(limit)
    return [_row_to_action(row) for row in conn.execute(sql, params)]


def is_expired(action: Action, *, now: datetime | None = None) -> bool:
    """`True` si la acción ya no puede aprobarse."""
    moment = now or _now()
    return moment > datetime.fromisoformat(action.expires_at.replace("Z", "+00:00"))


def approve_action(
    conn: sqlite3.Connection,
    action_id: str,
    *,
    approved_by: str = "operator",
    secret: bytes | None = None,
) -> Action:
    """Aprueba una acción y emite su `confirm_token`.

    Es la mitad humana del flujo: la ejecuta el operador, no el agente. Por eso
    esta función **no** está expuesta como tool MCP.
    """
    action = get_action(conn, action_id)
    if action is None:
        raise HitlError(f"no existe la acción {action_id!r}")
    if action.status == EXECUTED:
        raise HitlError(f"la acción {action_id!r} ya se ejecutó")
    if action.status == REJECTED:
        raise HitlError(f"la acción {action_id!r} fue rechazada")
    if action.status == EXPIRED or is_expired(action):
        conn.execute("UPDATE agent_actions SET status = ? WHERE id = ?", (EXPIRED, action_id))
        raise HitlError(f"la acción {action_id!r} caducó y ya no se puede aprobar")

    key = secret if secret is not None else load_secret()
    token = compute_token(key, action.id, action.tool, action.params, action.expires_at)
    now = _stamp(_now())
    conn.execute(
        """
        UPDATE agent_actions
           SET status = ?, confirm_token = ?, approved_at = ?, approved_by = ?
         WHERE id = ?
        """,
        (APPROVED, token, now, approved_by, action_id),
    )
    return Action(
        id=action.id,
        tool=action.tool,
        params=action.params,
        status=APPROVED,
        confirm_token=token,
        created_at=action.created_at,
        expires_at=action.expires_at,
        approved_at=now,
        approved_by=approved_by,
    )


def reject_action(
    conn: sqlite3.Connection, action_id: str, *, rejected_by: str = "operator"
) -> Action:
    """Rechaza una acción pendiente."""
    action = get_action(conn, action_id)
    if action is None:
        raise HitlError(f"no existe la acción {action_id!r}")
    if action.status in (EXECUTED, REJECTED):
        raise HitlError(f"la acción {action_id!r} está en {action.status} y no se puede rechazar")
    now = _stamp(_now())
    conn.execute(
        "UPDATE agent_actions SET status = ?, approved_at = ?, approved_by = ? WHERE id = ?",
        (REJECTED, now, rejected_by, action_id),
    )
    return Action(
        id=action.id,
        tool=action.tool,
        params=action.params,
        status=REJECTED,
        confirm_token=None,
        created_at=action.created_at,
        expires_at=action.expires_at,
        approved_at=now,
        approved_by=rejected_by,
    )


def mark_executed(conn: sqlite3.Connection, action_id: str, *, result: str = "") -> None:
    """Cierra la acción como ejecutada, con el resultado."""
    conn.execute(
        "UPDATE agent_actions SET status = ?, executed_at = ?, result = ? WHERE id = ?",
        (EXECUTED, _stamp(_now()), result, action_id),
    )


def verify(
    action: Action,
    *,
    tool: str,
    params: dict[str, object],
    token: str,
    secret: bytes | None = None,
    now: datetime | None = None,
) -> bool:
    """Comprueba que un token autoriza exactamente esta acción.

    Verifica las cuatro cosas por separado: que la acción está aprobada, que no
    ha caducado, que el token firma *esta* tool con *estos* parámetros, y que la
    comparación es en tiempo constante.
    """
    if action.status != APPROVED:
        return False
    if is_expired(action, now=now):
        return False
    if not action.confirm_token:
        return False
    key = secret if secret is not None else load_secret()
    expected = compute_token(key, action.id, tool, params, action.expires_at)
    return hmac.compare_digest(expected, token)


__all__ = [
    "APPROVED",
    "DEFAULT_TTL_SECONDS",
    "EXECUTED",
    "EXPIRED",
    "PENDING",
    "REJECTED",
    "SECRET_ENV_VAR",
    "VALID_STATUSES",
    "Action",
    "HitlError",
    "approve_action",
    "compute_token",
    "create_action",
    "get_action",
    "is_expired",
    "list_actions",
    "load_secret",
    "mark_executed",
    "reject_action",
    "secret_path",
    "verify",
]
