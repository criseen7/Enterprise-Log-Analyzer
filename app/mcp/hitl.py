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
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.config import Settings, get_settings
from app.db.connection import transaction
from app.securefs import ensure_private_dir, read_private
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

# Tope de la lista de acciones. `limit` llega de la CLI y un `-1` no puede
# convertirse en "vuelca la tabla entera": la aprobación es una consola humana,
# no un export.
MAX_ACTIONS = 200

# Variable de entorno con la clave del HMAC. Si está, manda sobre el fichero.
SECRET_ENV_VAR = "LOGLENS_HITL_SECRET"


class HitlError(ValueError):
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


@contextmanager
def _immediate(conn: sqlite3.Connection) -> Iterator[None]:
    """Toma el lock de escritura si la conexión no está ya en una transacción.

    Las transiciones HITL se leen y se escriben bajo `BEGIN IMMEDIATE` para que
    dos procesos (CLI de aprobación y servidor MCP) no puedan intercalar una
    lectura vieja con una escritura nueva. Si el llamante ya abrió la
    transacción, se reutiliza: abrir otra con `BEGIN` fallaría.
    """
    if conn.in_transaction:
        yield
        return
    with transaction(conn):
        yield


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
    try:
        return _read_secret(path)
    except FileNotFoundError:
        # Otro proceso lo borró entre `exists()` y `read`: se crea.
        pass
    except ValueError as exc:
        raise HitlError(str(exc)) from exc

    ensure_private_dir(path.parent)
    return _create_secret(path)


def _read_secret(path: Path) -> bytes:
    """Lee el secreto sin seguir symlinks y lo deja en 0600."""
    try:
        return read_private(path)
    except ValueError as exc:
        raise HitlError(str(exc)) from exc


def _create_secret(path: Path) -> bytes:
    """Crea el secreto sin pisar uno que otro proceso haya creado a la vez.

    Se escribe entero en un temporal y se publica con `os.link`, que falla si el
    destino ya existe. Así dos procesos que arrancan al mismo tiempo no pueden
    sobrescribirse la clave: uno gana la carrera y el otro lee la del ganador.
    """
    value = secrets.token_hex(32).encode("ascii")
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(6)}")
    descriptor = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(tmp, path)
        except FileExistsError:
            return _read_secret(path)
        return _read_secret(path)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


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


def _canonical_params(params: dict[str, object]) -> str:
    """JSON canónico para comparar parámetros sin depender del orden de claves."""
    return json.dumps(params, sort_keys=True, separators=(",", ":"))


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
            _canonical_params(params),
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
    """Acciones registradas, de la más reciente a la más antigua.

    `limit` se acota a `MAX_ACTIONS`: un valor negativo no puede desactivar el
    `LIMIT` y volcar la tabla completa.
    """
    if status is not None and status not in VALID_STATUSES:
        raise ValueError(f"status inválido: {status!r}, esperado uno de {VALID_STATUSES}")
    limit = max(1, min(int(limit), MAX_ACTIONS))
    sql = "SELECT * FROM agent_actions"
    params: list[object] = []
    if status is not None:
        sql += " WHERE status = ?"
        params.append(status)
    sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
    params.append(limit)
    return [_row_to_action(row) for row in conn.execute(sql, params)]


def expire_stale(conn: sqlite3.Connection, *, now: datetime | None = None) -> int:
    """Marca como `EXPIRED` las aprobaciones y pendientes caducadas.

    Sin este barrido, una acción caducada se queda en `PENDING`/`APPROVED` para
    siempre y el dedupe la sigue viendo como viva hasta que alguien la toca.
    Devuelve cuántas filas cambiaron.
    """
    stamp = _stamp(now or _now())
    with _immediate(conn):
        cursor = conn.execute(
            """
            UPDATE agent_actions
               SET status = ?
             WHERE status IN (?, ?) AND expires_at < ?
            """,
            (EXPIRED, PENDING, APPROVED, stamp),
        )
        return cursor.rowcount


def find_pending(
    conn: sqlite3.Connection,
    *,
    tool: str,
    params: dict[str, object],
) -> Action | None:
    """Acción reutilizable para esta tool y estos parámetros, si existe.

    Evita que un agente en bucle llene `agent_actions` con la misma petición:
    mientras la anterior siga viva se reutiliza en vez de crear otra. También
    se reutiliza una `APPROVED` pendiente de ejecutar: crear una `PENDING`
    nueva tiraría la aprobación humana que ya existe.
    """
    expire_stale(conn)
    row = conn.execute(
        """
        SELECT * FROM agent_actions
         WHERE status IN (?, ?) AND tool = ? AND params = ?
         ORDER BY CASE status WHEN ? THEN 0 ELSE 1 END,
                  created_at DESC, id DESC
         LIMIT 1
        """,
        (PENDING, APPROVED, tool, _canonical_params(params), APPROVED),
    ).fetchone()
    if row is None:
        return None
    return _row_to_action(row)


def find_approved(
    conn: sqlite3.Connection,
    *,
    tool: str,
    params: dict[str, object],
    token: str,
    secret: bytes | None = None,
) -> Action | None:
    """Acción aprobada cuyo token autoriza esta tool con estos parámetros.

    Filtra en SQL por `tool` y `params` antes de verificar el HMAC: el token no
    se puede indexar (es un HMAC), pero el conjunto de candidatos sí. Sin este
    filtro habría que recorrer todas las aprobadas con un `LIMIT` arbitrario, y
    un token válido podía quedar fuera de la página.
    """
    key = secret if secret is not None else load_secret()
    expire_stale(conn)
    rows = conn.execute(
        """
        SELECT * FROM agent_actions
         WHERE status = ? AND tool = ? AND params = ?
         ORDER BY created_at DESC, id DESC
        """,
        (APPROVED, tool, _canonical_params(params)),
    )
    for row in rows:
        action = _row_to_action(row)
        if verify(action, tool=tool, params=params, token=token, secret=key):
            return action
    return None


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

    La transición es compare-and-swap: sólo `PENDING -> APPROVED`. Si otro
    proceso ya la ejecutó, el `UPDATE` no toca ninguna fila y la aprobación
    falla en vez de rearmar una mutación que ya ocurrió.
    """
    key = secret if secret is not None else load_secret()
    expire_stale(conn)
    with _immediate(conn):
        action = get_action(conn, action_id)
        if action is None:
            raise HitlError(f"no existe la acción {action_id!r}")
        if action.status == EXECUTED:
            raise HitlError(f"la acción {action_id!r} ya se ejecutó")
        if action.status == REJECTED:
            raise HitlError(f"la acción {action_id!r} fue rechazada")
        if action.status == EXPIRED:
            raise HitlError(f"la acción {action_id!r} caducó y ya no se puede aprobar")
        if is_expired(action):
            # Carrera con el reloj: `expire_stale` ya barrió, pero entre esa
            # lectura y esta el TTL pudo vencer. No se escribe dentro de la
            # transacción porque el `raise` la revertiría; el siguiente barrido
            # la marca.
            raise HitlError(f"la acción {action_id!r} caducó y ya no se puede aprobar")
        if action.status == APPROVED:
            # Aprobar dos veces es un no-op idempotente: se devuelve el mismo
            # token sin reescribir `approved_at`/`approved_by`. Reescribirlos
            # borraría la auditoría de quién aprobó realmente.
            if not action.confirm_token:
                raise HitlError(f"la acción {action_id!r} está aprobada pero no tiene token")
            return action

        token = compute_token(key, action.id, action.tool, action.params, action.expires_at)
        now = _stamp(_now())
        cursor = conn.execute(
            """
            UPDATE agent_actions
               SET status = ?, confirm_token = ?, approved_at = ?, approved_by = ?
             WHERE id = ? AND status = ?
            """,
            (APPROVED, token, now, approved_by, action_id, PENDING),
        )
        if cursor.rowcount != 1:
            current = get_action(conn, action_id)
            state = current.status if current is not None else "desaparecida"
            raise HitlError(
                f"conflicto al aprobar {action_id!r}: la acción cambió de estado "
                f"({state}). No se aplicó ningún cambio."
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
    """Rechaza una acción pendiente o aprobada, sin tocar una ejecutada."""
    expire_stale(conn)
    with _immediate(conn):
        action = get_action(conn, action_id)
        if action is None:
            raise HitlError(f"no existe la acción {action_id!r}")
        if action.status in (EXECUTED, REJECTED, EXPIRED):
            raise HitlError(
                f"la acción {action_id!r} está en {action.status} y no se puede rechazar"
            )
        if is_expired(action):
            # Ver la nota de `approve_action`: escribir aquí no serviría porque
            # el `raise` revierte la transacción.
            raise HitlError(f"la acción {action_id!r} caducó y ya no se puede rechazar")
        now = _stamp(_now())
        cursor = conn.execute(
            """
            UPDATE agent_actions
               SET status = ?, approved_at = ?, approved_by = ?
             WHERE id = ? AND status IN (?, ?)
            """,
            (REJECTED, now, rejected_by, action_id, PENDING, APPROVED),
        )
        if cursor.rowcount != 1:
            current = get_action(conn, action_id)
            state = current.status if current is not None else "desaparecida"
            raise HitlError(
                f"conflicto al rechazar {action_id!r}: la acción cambió de estado "
                f"({state}). No se aplicó ningún cambio."
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
    """Cierra la acción como ejecutada, sólo si estaba `APPROVED`."""
    with _immediate(conn):
        cursor = conn.execute(
            """
            UPDATE agent_actions
               SET status = ?, executed_at = ?, result = ?
             WHERE id = ? AND status = ?
            """,
            (EXECUTED, _stamp(_now()), result, action_id, APPROVED),
        )
        if cursor.rowcount != 1:
            current = get_action(conn, action_id)
            state = current.status if current is not None else "desaparecida"
            raise HitlError(
                f"la acción {action_id!r} no está aprobada (estado: {state}); "
                "no se puede marcar como ejecutada"
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
    try:
        return hmac.compare_digest(expected.encode("utf-8"), token.encode("utf-8"))
    except UnicodeEncodeError:
        # Un token no-ASCII no puede ser el hex que emitimos: se rechaza sin
        # lanzar `TypeError` (compare_digest sólo acepta ASCII en `str`).
        return False


__all__ = [
    "APPROVED",
    "DEFAULT_TTL_SECONDS",
    "EXECUTED",
    "EXPIRED",
    "MAX_ACTIONS",
    "PENDING",
    "REJECTED",
    "SECRET_ENV_VAR",
    "VALID_STATUSES",
    "Action",
    "HitlError",
    "approve_action",
    "compute_token",
    "create_action",
    "expire_stale",
    "find_approved",
    "find_pending",
    "get_action",
    "is_expired",
    "list_actions",
    "load_secret",
    "mark_executed",
    "reject_action",
    "secret_path",
    "verify",
]
