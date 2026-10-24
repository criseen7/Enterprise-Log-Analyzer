"""CLI del servidor MCP: `loglens mcp|pending|approve|reject|tools`

Subcomandos:

* `mcp` (alias `serve`) — arranca el servidor. Por defecto en stdio (lo que usan
  Claude Desktop, Cursor y cualquier cliente MCP de escritorio); con `--http` en
  streamable-http para clientes remotos.
* `pending` — lista las acciones que esperan aprobación.
* `approve` — aprueba una acción y **devuelve su `confirm_token`**. Es el
  acto humano del flujo HITL: el token que sale de aquí es lo que autoriza la
  mutación.
* `reject` — rechaza una acción.
* `tools` — lista las tools registradas.

El subcomando `approve` es la otra mitad del bucle de aprobación: el servidor
MCP y esta CLI son procesos distintos, y la acción pendiente vive en la base
precisamente para que el token pueda cruzar entre ellos.
"""

from __future__ import annotations

import argparse
import sys

from app.mcp import hitl
from app.mcp._context import write_connection
from app.mcp.server import SERVER_NAME, SERVER_VERSION, build_server


def _cmd_serve(args: argparse.Namespace) -> int:
    from app.mcp.server import run

    if args.http:
        run("streamable-http", host=args.host, port=args.port)
    else:
        run("stdio")
    return 0


def _cmd_pending(args: argparse.Namespace) -> int:
    with write_connection() as conn:
        actions = hitl.list_actions(conn, status=hitl.PENDING, limit=args.limit)
    if not actions:
        print("No hay acciones pendientes de aprobación.")
        return 0
    for action in actions:
        params = ", ".join(f"{k}={v!r}" for k, v in sorted(action.params.items()))
        print(f"{action.id}  {action.tool}({params})")
        print(f"    creado: {action.created_at}   caduca: {action.expires_at}")
    print()
    print("Aprobar con: python -m app.mcp approve <action_id>")
    return 0


def _cmd_approve(args: argparse.Namespace) -> int:
    with write_connection() as conn:
        try:
            action = hitl.approve_action(conn, args.action_id, approved_by=args.approved_by)
        except hitl.HitlError as exc:
            print(f"No se pudo aprobar: {exc}", file=sys.stderr)
            return 1
    print(action.confirm_token)
    return 0


def _cmd_reject(args: argparse.Namespace) -> int:
    with write_connection() as conn:
        try:
            hitl.reject_action(conn, args.action_id, rejected_by=args.approved_by)
        except hitl.HitlError as exc:
            print(f"No se pudo rechazar: {exc}", file=sys.stderr)
            return 1
    print(f"Acción {args.action_id} rechazada.")
    return 0


def _cmd_tools(_: argparse.Namespace) -> int:
    server = build_server()
    tools = server._tool_manager.list_tools()  # noqa: SLF001
    print(f"{SERVER_NAME} {SERVER_VERSION} — {len(tools)} tools")
    for tool in tools:
        print(f"  {tool.name:22} {tool.description.splitlines()[0] if tool.description else ''}")
    return 0


def _cmd_investigate(args: argparse.Namespace) -> int:
    # Import perezoso: la CLI de aprobación debe funcionar aunque la capa
    # agentic no esté instalada (el extra `agent` es opcional).
    from app.agent.cli import run as investigate

    return investigate(
        " ".join(args.symptom),
        model=args.model,
        no_llm=args.no_llm,
        allow_mutations=args.allow_mutations,
        traces=args.traces,
        as_json=args.as_json,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Servidor MCP de LogLens y consola de aprobación.",
    )
    sub = parser.add_subparsers(dest="command")

    # `loglens mcp` es el nombre documentado; `serve` se mantiene como alias
    # porque es el verbo que aparece en la ayuda histórica y en los tutoriales.
    serve = sub.add_parser(
        "mcp",
        aliases=["serve"],
        help="Arranca el servidor MCP.",
    )
    serve.add_argument(
        "--http",
        action="store_true",
        help="Usa streamable-http en vez de stdio.",
    )
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.set_defaults(func=_cmd_serve)

    pending = sub.add_parser("pending", help="Lista acciones por aprobar.")
    pending.add_argument("--limit", type=int, default=20)
    pending.set_defaults(func=_cmd_pending)

    approve = sub.add_parser("approve", help="Aprueba una acción y emite su token.")
    approve.add_argument("action_id")
    approve.add_argument("--approved-by", default="operator")
    approve.set_defaults(func=_cmd_approve)

    reject = sub.add_parser("reject", help="Rechaza una acción.")
    reject.add_argument("action_id")
    reject.add_argument("--approved-by", default="operator")
    reject.set_defaults(func=_cmd_reject)

    tools = sub.add_parser("tools", help="Lista las tools registradas.")
    tools.set_defaults(func=_cmd_tools)

    investigate = sub.add_parser(
        "investigate",
        help="Investiga un síntoma con la capa agentic (motor + LLM opcional).",
    )
    investigate.add_argument("symptom", nargs="+")
    investigate.add_argument("--model", default="claude-sonnet-4-5")
    investigate.add_argument(
        "--no-llm",
        action="store_true",
        help="Fuerza el modo determinista aunque haya ANTHROPIC_API_KEY.",
    )
    investigate.add_argument(
        "--allow-mutations",
        action="store_true",
        help="Permite proponer un cambio de estado (queda pendiente de aprobación).",
    )
    investigate.add_argument("--traces", default=None)
    investigate.add_argument("--json", action="store_true", dest="as_json")
    investigate.set_defaults(func=_cmd_investigate)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "func", None) is None:
        # Sin subcomando: servidor por stdio, que es el modo en que lo lanzan
        # los clientes MCP de escritorio.
        from app.mcp.server import run

        run("stdio")
        return 0
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
