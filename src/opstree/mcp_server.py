"""op3 MCP server — dependency-free stdio JSON-RPC.

Adopts wellmanifest/poa (typed tools, closed inputSchema, fail-closed
dispatch) and wellmanifest/logs (append-only hash-chained JSONL event
stream under $XDG_STATE_HOME/op3/mcp-events.jsonl, overridable with
OP3_MCP_EVENT_LOG).
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable

_PROTOCOL_VERSION = "2024-11-05"
_NOTIFICATIONS = frozenset({"notifications/initialized", "notifications/cancelled"})

SERVER_NAME = "op3"
try:
    from importlib.metadata import version as _pkg_version

    SERVER_VERSION = _pkg_version("op3")
except Exception:
    SERVER_VERSION = "0.0.0"

_ZERO_HASH = "0" * 64


def _event_log_path() -> Path:
    override = os.environ.get("OP3_MCP_EVENT_LOG")
    if override:
        return Path(override)
    state_home = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return state_home / "op3" / "mcp-events.jsonl"


def _emit_event(tool: str, status: str, duration_ms: int, detail: str = "") -> None:
    """Append one hash-chained event; logging failure never breaks a tool call."""
    try:
        path = _event_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        prev = _ZERO_HASH
        if path.exists() and path.stat().st_size:
            with path.open("rb") as fh:
                fh.seek(0, os.SEEK_END)
                tail = fh.read(65536).decode("utf-8", errors="replace")
            last = tail.rstrip().rsplit("\n", 1)[-1]
            prev = json.loads(last).get("event_hash", _ZERO_HASH)
        event = {
            "schema": "op3.mcp/event/v1",
            "event_id": f"event:{uuid.uuid4().hex[:24]}",
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "actor": "agent:mcp",
            "tool": tool,
            "status": status,
            "duration_ms": duration_ms,
            "detail": detail[:200],
            "prev_hash": prev,
        }
        body = json.dumps(event, sort_keys=True)
        event["event_hash"] = hashlib.sha256(body.encode()).hexdigest()
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event) + "\n")
    except Exception:
        pass


_LAYERS = ("physical.display", "physical.compute", "os.kernel", "os.config", "runtime.container")
_FORMATS = ("less", "migration_yaml", "snapshot_yaml")


def _tool_scan(args: dict[str, Any]) -> str:
    from opstree.layers.tree import LayerTree
    from opstree.layers.builtin import OsLayer, PhysicalLayer, RuntimeLayer
    from opstree.probes.builtin.os_linux import OsConfigProbe, OsKernelProbe
    from opstree.probes.builtin.physical_rpi import RpiPhysicalDisplayProbe
    from opstree.probes.builtin.runtime_container import RuntimeContainerProbe
    from opstree.probes.context import LocalContext
    from opstree.probes.registry import ProbeRegistry
    from opstree.scanner.linear import LinearScanner

    layer_filter = set(args.get("layers") or _LAYERS)
    unknown = layer_filter - set(_LAYERS)
    if unknown:
        raise ValueError(f"Unknown layers: {sorted(unknown)}")

    tree = LayerTree()
    if "physical.display" in layer_filter:
        tree.register(PhysicalLayer.display)
    if "physical.compute" in layer_filter:
        tree.register(PhysicalLayer.compute)
    if "os.kernel" in layer_filter:
        tree.register(OsLayer.kernel)
    if "os.config" in layer_filter:
        tree.register(OsLayer.config)
    if "runtime.container" in layer_filter:
        tree.register(RuntimeLayer.container)

    registry = ProbeRegistry()
    registry.register(RpiPhysicalDisplayProbe())
    registry.register(OsKernelProbe())
    registry.register(OsConfigProbe())
    registry.register(RuntimeContainerProbe())

    scanner = LinearScanner(tree)
    scanner.probe_registry = registry
    snapshot = scanner.scan(args.get("target", "localhost"), LocalContext().execute)

    if args.get("format", "yaml") == "json":
        return json.dumps(snapshot.model_dump(mode="json"), indent=2)
    return snapshot.to_yaml()


def _parse_intended(path: Path):
    from opstree.formats.less import LessAdapter
    from opstree.formats.migration_yaml import MigrationYamlAdapter
    from opstree.snapshot.model import Snapshot

    if path.suffix == ".less":
        return LessAdapter().parse(path.read_text())
    if path.name == "migration.yaml" or path.name.endswith(".migration.yaml"):
        return MigrationYamlAdapter().parse(path.read_text())
    return Snapshot.load(path)


def _tool_drift(args: dict[str, Any]) -> str:
    from opstree.drift.detector import DriftDetector
    from opstree.snapshot.model import Snapshot

    intended = _parse_intended(Path(args["intended"]))
    actual = Snapshot.load(Path(args["actual"]))
    report = DriftDetector().detect(intended, actual)
    return json.dumps(
        {
            "intended_source": report.intended_source,
            "actual_target": report.actual_target,
            "has_drift": report.has_drift,
            "summary": report.summary,
            "changes": [
                change.model_dump(mode="json") if hasattr(change, "model_dump") else change
                for change in report.changes
            ],
        },
        indent=2,
        default=str,
    )


def _tool_convert(args: dict[str, Any]) -> str:
    from datetime import datetime, timezone

    from opstree._version import __version__
    from opstree.formats.less import LessAdapter
    from opstree.formats.migration_yaml import MigrationYamlAdapter
    from opstree.formats.snapshot_yaml import SnapshotYamlAdapter
    from opstree.snapshot.model import Snapshot

    input_path = Path(args["input_file"])
    if not input_path.is_file():
        raise FileNotFoundError(str(input_path))
    target = args["format"]

    parsed = _parse_intended(input_path)
    if not isinstance(parsed, Snapshot):
        if target == "snapshot_yaml":
            parsed = Snapshot(
                target="unknown",
                scanned_at=datetime.now(timezone.utc),
                scanner_version=__version__,
                layers=parsed.layers,
                anomalies=[],
            )
        elif target != "snapshot_yaml":
            pass

    adapter = {"less": LessAdapter, "migration_yaml": MigrationYamlAdapter, "snapshot_yaml": SnapshotYamlAdapter}[target]()
    output_text = adapter.render(parsed)

    output_file = args.get("output_file")
    if output_file:
        Path(output_file).write_text(output_text)
        return json.dumps({"output_file": output_file, "format": target, "bytes": len(output_text)})
    return output_text


def _tool_formats(_args: dict[str, Any]) -> str:
    return json.dumps(
        {
            "input_formats": ["less (.less)", "migration_yaml (migration.yaml)", "snapshot_yaml"],
            "output_formats": list(_FORMATS),
            "scan_layers": list(_LAYERS),
            "scan_contexts": ["local"],
        },
        indent=2,
    )


TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "op3_scan": {
        "name": "op3_scan",
        "description": "Scan the local machine and return a layered operations snapshot (yaml or json).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "default": "localhost"},
                "layers": {
                    "type": "array",
                    "items": {"type": "string", "enum": list(_LAYERS)},
                    "description": "Layer filter; defaults to all layers",
                },
                "format": {"type": "string", "enum": ["yaml", "json"], "default": "yaml"},
            },
        },
    },
    "op3_drift": {
        "name": "op3_drift",
        "description": "Detect drift between an intended-state file (.less/migration.yaml/snapshot) and an actual snapshot file.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "intended": {"type": "string", "description": "Path to intended-state file"},
                "actual": {"type": "string", "description": "Path to actual snapshot file"},
            },
            "required": ["intended", "actual"],
        },
    },
    "op3_convert": {
        "name": "op3_convert",
        "description": "Convert a config file between less, migration_yaml and snapshot_yaml; returns text or writes output_file.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "input_file": {"type": "string"},
                "format": {"type": "string", "enum": list(_FORMATS)},
                "output_file": {"type": "string", "description": "Optional output path"},
            },
            "required": ["input_file", "format"],
        },
    },
    "op3_formats": {
        "name": "op3_formats",
        "description": "List supported input/output formats and scan layers.",
        "inputSchema": {"type": "object", "properties": {}},
    },
}

TOOL_HANDLERS: dict[str, Callable[[dict[str, Any]], str]] = {
    "op3_scan": _tool_scan,
    "op3_drift": _tool_drift,
    "op3_convert": _tool_convert,
    "op3_formats": _tool_formats,
}


def _handle_initialize(request_id: Any, params: dict[str, Any] | None) -> dict[str, Any]:
    client_version = (params or {}).get("protocolVersion", _PROTOCOL_VERSION)
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {
            "protocolVersion": client_version,
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "capabilities": {"tools": {}},
        },
    }


def _handle_tools_list(request_id: Any) -> dict[str, Any]:
    tools = [
        {
            "name": schema["name"],
            "description": schema["description"],
            "inputSchema": schema["inputSchema"],
        }
        for schema in TOOL_SCHEMAS.values()
    ]
    return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": tools}}


def _handle_tools_call(request_id: Any, params: dict[str, Any]) -> dict[str, Any]:
    tool_name = params.get("name")
    arguments = params.get("arguments", {}) or {}

    if tool_name not in TOOL_HANDLERS:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32601, "message": f"Tool '{tool_name}' not found"},
        }

    start = time.monotonic()
    try:
        result = TOOL_HANDLERS[tool_name](arguments)
        _emit_event(tool_name, "ok", int((time.monotonic() - start) * 1000))
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"content": [{"type": "text", "text": result}]},
        }
    except Exception as exc:
        _emit_event(
            tool_name, "error", int((time.monotonic() - start) * 1000), str(exc)
        )
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32603, "message": f"Tool execution failed: {exc}"},
        }


def handle_request(request: dict[str, Any]) -> dict[str, Any]:
    method = request.get("method", "")
    params = request.get("params", {}) or {}
    request_id = request.get("id")

    if method in _NOTIFICATIONS:
        return {}
    if method == "initialize":
        return _handle_initialize(request_id, params)
    if method == "tools/list":
        return _handle_tools_list(request_id)
    if method == "tools/call":
        return _handle_tools_call(request_id, params)

    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": -32601, "message": f"Method '{method}' not found"},
    }


def run_server() -> None:
    print("op3 MCP Server started", file=sys.stderr)
    print(f"Available tools: {', '.join(sorted(TOOL_SCHEMAS))}", file=sys.stderr)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
            response = handle_request(request)
            if response:
                print(json.dumps(response), flush=True)
        except json.JSONDecodeError as exc:
            print(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "error": {"code": -32700, "message": f"Parse error: {exc}"},
                    }
                ),
                flush=True,
            )
        except Exception as exc:
            print(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "error": {"code": -32603, "message": f"Internal error: {exc}"},
                    }
                ),
                flush=True,
            )


def main() -> None:
    run_server()


if __name__ == "__main__":
    main()
