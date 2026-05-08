from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MAX_OUTPUT_CHARS = 100_000
MAX_READ_CHARS = 100_000
MIN_TIMEOUT = 1
MAX_COMMAND_TIMEOUT = 600
DEFAULT_CONNECT_TIMEOUT = 10
DEFAULT_COMMAND_TIMEOUT = 120

ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$")
USER_RE = re.compile(r"^[A-Za-z0-9._-]+$")


@dataclass(frozen=True)
class HostConfig:
    alias: str
    host: str
    user: str
    port: int = 22
    identity_file: str | None = None
    workdir: str | None = None
    connect_timeout: int = DEFAULT_CONNECT_TIMEOUT
    command_timeout: int = DEFAULT_COMMAND_TIMEOUT
    strict_host_key_checking: bool = True
    allow_write: bool = False
    allowed_roots: list[str] = field(default_factory=list)
    workdir_only: bool = False


def _ok(**payload: Any) -> str:
    return json.dumps({"success": True, **payload})


def _err(message: str, **payload: Any) -> str:
    return json.dumps({"success": False, "error": message, **payload})


def _clamp_int(value: Any, default: int, minimum: int, maximum: int, name: str) -> int:
    if value in (None, ""):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be an integer")
    if parsed < minimum or parsed > maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return parsed


def _clamped_timeout(value: Any, default: int, maximum: int) -> int:
    try:
        parsed = int(value if value not in (None, "") else default)
    except (TypeError, ValueError):
        return default
    return max(MIN_TIMEOUT, min(parsed, maximum))


def _validate_alias(alias: Any) -> str:
    if not isinstance(alias, str) or not ALIAS_RE.match(alias):
        raise ValueError(
            f"invalid host alias {alias!r}; aliases must match {ALIAS_RE.pattern}"
        )
    return alias


def _validate_host(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("host must be a non-empty string")
    host = value.strip()
    if any(ch.isspace() for ch in host) or "\x00" in host or host.startswith("-") or "@" in host:
        raise ValueError("host contains invalid characters")
    return host


def _validate_user(value: Any) -> str:
    if not isinstance(value, str) or not USER_RE.match(value) or value.startswith("-"):
        raise ValueError("user must match [A-Za-z0-9._-]+ and may not start with '-'")
    return value


def _expand_identity_file(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ValueError("identity_file must be a string")
    path = Path(os.path.expandvars(os.path.expanduser(value))).resolve()
    if not path.is_file():
        raise ValueError(f"identity_file does not exist: {value}")
    return str(path)


def _parse_bool(value: Any, default: bool, name: str) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    raise ValueError(f"{name} must be a boolean")


def _parse_roots(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise ValueError("allowed_roots must be a list of non-empty strings")
    return value


def _parse_host(alias: str, raw: Any) -> HostConfig | None:
    alias = _validate_alias(alias)
    if isinstance(raw, str):
        if "@" not in raw or raw.count("@") != 1:
            raise ValueError(f"host {alias}: shorthand must be user@host")
        user, host = raw.split("@", 1)
        return HostConfig(alias=alias, user=_validate_user(user), host=_validate_host(host))

    if not isinstance(raw, dict):
        raise ValueError(f"host {alias}: config must be a dict or user@host string")
    if raw.get("enabled") is False:
        return None

    host = _validate_host(raw.get("host"))
    user = _validate_user(raw.get("user"))
    port = _clamp_int(raw.get("port", 22), 22, 1, 65535, "port")
    connect_timeout = _clamp_int(
        raw.get("connect_timeout", DEFAULT_CONNECT_TIMEOUT),
        DEFAULT_CONNECT_TIMEOUT,
        MIN_TIMEOUT,
        60,
        "connect_timeout",
    )
    command_timeout = _clamp_int(
        raw.get("command_timeout", DEFAULT_COMMAND_TIMEOUT),
        DEFAULT_COMMAND_TIMEOUT,
        MIN_TIMEOUT,
        MAX_COMMAND_TIMEOUT,
        "command_timeout",
    )
    workdir = raw.get("workdir")
    if workdir is not None and not isinstance(workdir, str):
        raise ValueError("workdir must be a string")
    workdir_only = _parse_bool(raw.get("workdir_only"), False, "workdir_only")
    if workdir_only and not workdir:
        raise ValueError("workdir_only requires workdir")

    return HostConfig(
        alias=alias,
        host=host,
        user=user,
        port=port,
        identity_file=_expand_identity_file(raw.get("identity_file")),
        workdir=workdir or None,
        connect_timeout=connect_timeout,
        command_timeout=command_timeout,
        strict_host_key_checking=_parse_bool(
            raw.get("strict_host_key_checking"),
            True,
            "strict_host_key_checking",
        ),
        allow_write=_parse_bool(raw.get("allow_write"), False, "allow_write"),
        allowed_roots=_parse_roots(raw.get("allowed_roots")),
        workdir_only=workdir_only,
    )


def _load_hosts() -> dict[str, HostConfig]:
    from hermes_cli.config import cfg_get, load_config

    config = load_config()
    raw_hosts = cfg_get(config, "remote_hosts", "hosts", default={})
    if not isinstance(raw_hosts, dict):
        raise ValueError("remote_hosts.hosts must be a mapping")
    hosts: dict[str, HostConfig] = {}
    for alias, raw in raw_hosts.items():
        parsed = _parse_host(alias, raw)
        if parsed is not None:
            hosts[parsed.alias] = parsed
    return hosts


def _host_from_args(args: dict[str, Any]) -> HostConfig:
    alias = _validate_alias(args.get("host"))
    hosts = _load_hosts()
    try:
        return hosts[alias]
    except KeyError:
        raise ValueError(f"unknown or disabled remote host alias: {alias}") from None


def _ssh_argv(host: HostConfig, remote_command: str) -> list[str]:
    argv = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "RequestTTY=no",
        "-o",
        f"ConnectTimeout={host.connect_timeout}",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=2",
        "-o",
        f"StrictHostKeyChecking={'yes' if host.strict_host_key_checking else 'no'}",
        "-p",
        str(host.port),
    ]
    if host.identity_file:
        argv.extend(["-i", host.identity_file])
    argv.extend(["--", f"{host.user}@{host.host}", remote_command])
    return argv


def _remote_python_command(helper: str) -> str:
    return "python3 -c " + shlex.quote(helper)


def _run_remote_python(host: HostConfig, helper: str, payload: dict[str, Any], timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(
        _ssh_argv(host, _remote_python_command(helper)),
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=timeout,
        shell=False,
    )


TERMINAL_HELPER = r"""
import json, os, subprocess, sys, threading
req = json.load(sys.stdin)
workdir = req.get("workdir")
if workdir:
    os.chdir(os.path.expanduser(workdir))
max_chars = int(req.get("max_chars", 100000))
proc = subprocess.Popen(
    ["bash", "-lc", req.get("command", "")],
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
    bufsize=1,
)
buffers = {"stdout": [], "stderr": []}
stored = {"stdout": 0, "stderr": 0}
truncated = False
lock = threading.Lock()

def drain(stream, key):
    global truncated
    while True:
        chunk = stream.read(4096)
        if not chunk:
            break
        with lock:
            total_stored = stored["stdout"] + stored["stderr"]
            remaining = max(0, max_chars - total_stored)
            if remaining:
                piece = chunk[:remaining]
                buffers[key].append(piece)
                stored[key] += len(piece)
            if len(chunk) > remaining:
                truncated = True

threads = [
    threading.Thread(target=drain, args=(proc.stdout, "stdout")),
    threading.Thread(target=drain, args=(proc.stderr, "stderr")),
]
for thread in threads:
    thread.start()
returncode = proc.wait()
for thread in threads:
    thread.join()
stdout = "".join(buffers["stdout"])
stderr = "".join(buffers["stderr"])
sys.stdout.write(json.dumps({
    "stdout": stdout,
    "stderr": stderr,
    "exit_code": returncode,
    "truncated": truncated,
}))
"""


READ_HELPER = r"""
import json, os, pathlib, sys
req = json.load(sys.stdin)
path = pathlib.Path(os.path.expanduser(req["path"])).resolve()
if req.get("workdir_only"):
    allowed = [pathlib.Path(os.path.expanduser(req["workdir"])).resolve()]
else:
    allowed = [pathlib.Path(os.path.expanduser(p)).resolve() for p in req.get("allowed_roots", [])]
if allowed and not any(path == root or root in path.parents for root in allowed):
    print(json.dumps({"error": "path is outside allowed roots"}))
    sys.exit(0)
offset = max(1, int(req.get("offset", 1)))
limit = max(1, int(req.get("limit", 200)))
max_chars = max(1, int(req.get("max_chars", 100000)))
end_line = offset + limit - 1
parts = []
chars = 0
truncated = False
saw_any = False
last_char = ""
current_line = 1
stop = False
with path.open("r", encoding="utf-8", errors="replace") as f:
    while True:
        chunk = f.read(8192)
        if not chunk:
            break
        for ch in chunk:
            saw_any = True
            last_char = ch
            if current_line > end_line:
                truncated = True
                stop = True
                break
            if offset <= current_line <= end_line:
                if chars >= max_chars:
                    truncated = True
                    stop = True
                    break
                parts.append(ch)
                chars += 1
            if ch == "\n":
                current_line += 1
        if stop:
            break
text = "".join(parts)
total_lines = None if truncated else (0 if not saw_any else current_line - (1 if last_char == "\n" else 0))
print(json.dumps({
    "path": str(path),
    "offset": offset,
    "limit": limit,
    "total_lines": total_lines,
    "content": text,
    "truncated": truncated,
}))
"""


WRITE_HELPER = r"""
import json, os, pathlib, tempfile, sys
req = json.load(sys.stdin)
path = pathlib.Path(os.path.expanduser(req["path"])).resolve()
if req.get("workdir_only"):
    allowed = [pathlib.Path(os.path.expanduser(req["workdir"])).resolve()]
else:
    allowed = [pathlib.Path(os.path.expanduser(p)).resolve() for p in req.get("allowed_roots", [])]
if allowed and not any(path == root or root in path.parents for root in allowed):
    print(json.dumps({"error": "path is outside allowed roots"}))
    sys.exit(0)
path.parent.mkdir(parents=True, exist_ok=True)
content = req.get("content", "")
fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
try:
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp_name, path)
finally:
    if os.path.exists(tmp_name):
        os.unlink(tmp_name)
print(json.dumps({"path": str(path), "bytes": len(content.encode("utf-8"))}))
"""


def remote_hosts_list(args: dict[str, Any], **_: Any) -> str:
    try:
        hosts = _load_hosts()
    except Exception as exc:
        return _err(str(exc))
    return _ok(
        hosts=[
            {
                "alias": host.alias,
                "host": host.host,
                "user": host.user,
                "port": host.port,
                "workdir": host.workdir,
                "connect_timeout": host.connect_timeout,
                "command_timeout": host.command_timeout,
                "allow_write": host.allow_write,
                "allowed_roots": host.allowed_roots,
                "workdir_only": host.workdir_only,
            }
            for host in hosts.values()
        ]
    )


def _check_command_allowed(command: str, host: HostConfig) -> dict[str, Any]:
    """Run Hermes' existing dangerous-command guard before SSH execution."""
    try:
        from tools.terminal_tool import _check_all_guards
    except ImportError as exc:
        return {
            "approved": False,
            "message": f"BLOCKED: Remote command guard unavailable for {host.alias}: {exc}",
        }

    try:
        return _check_all_guards(command, "ssh")
    except Exception as exc:
        return {
            "approved": False,
            "message": f"BLOCKED: Remote command guard failed for {host.alias}: {exc}",
        }


def remote_terminal(args: dict[str, Any], **_: Any) -> str:
    try:
        host = _host_from_args(args)
        command = args.get("command")
        if not isinstance(command, str) or not command:
            raise ValueError("command must be a non-empty string")
        guard = _check_command_allowed(command, host)
        if not guard.get("approved"):
            return _err(guard.get("message") or "remote command blocked by safety guard")
        workdir = args.get("workdir", host.workdir)
        if workdir is not None and not isinstance(workdir, str):
            raise ValueError("workdir must be a string")
        if host.workdir_only and workdir != host.workdir:
            raise ValueError("workdir override is disabled for this host")
        timeout = _clamped_timeout(
            args.get("timeout", host.command_timeout),
            host.command_timeout,
            host.command_timeout,
        )
        proc = _run_remote_python(
            host,
            TERMINAL_HELPER,
            {
                "command": command,
                "workdir": workdir,
                "max_chars": MAX_OUTPUT_CHARS,
            },
            timeout + host.connect_timeout + 5,
        )
        if proc.returncode != 0:
            stdout = proc.stdout or ""
            stderr = proc.stderr or ""
            exit_code = proc.returncode
            helper_truncated = False
        else:
            payload = json.loads(proc.stdout or "{}")
            stdout = payload.get("stdout", "")
            stderr = payload.get("stderr", "")
            exit_code = payload.get("exit_code", 0)
            helper_truncated = bool(payload.get("truncated", False))
        combined = stdout + stderr
        truncated = helper_truncated or len(combined) > MAX_OUTPUT_CHARS
        if truncated:
            remaining = MAX_OUTPUT_CHARS
            stdout = stdout[:remaining]
            remaining -= len(stdout)
            stderr = stderr[: max(0, remaining)]
        return _ok(
            host=host.alias,
            command=command,
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
            timed_out=False,
            truncated=truncated,
        )
    except subprocess.TimeoutExpired as exc:
        return _ok(
            host=args.get("host"),
            command=args.get("command"),
            stdout=exc.stdout or "",
            stderr=exc.stderr or "",
            exit_code=None,
            timed_out=True,
            truncated=False,
        )
    except Exception as exc:
        return _err(str(exc))


def remote_read_file(args: dict[str, Any], **_: Any) -> str:
    try:
        host = _host_from_args(args)
        path = args.get("path")
        if not isinstance(path, str) or not path:
            raise ValueError("path must be a non-empty string")
        offset = _clamp_int(args.get("offset", 1), 1, 1, 1_000_000_000, "offset")
        limit = _clamp_int(args.get("limit", 200), 200, 1, 10_000, "limit")
        proc = _run_remote_python(
            host,
            READ_HELPER,
            {
                "path": path,
                "offset": offset,
                "limit": limit,
                "max_chars": MAX_READ_CHARS,
                "allowed_roots": host.allowed_roots,
                "workdir_only": host.workdir_only,
                "workdir": host.workdir or ".",
            },
            host.connect_timeout + host.command_timeout,
        )
        if proc.returncode != 0:
            return _err("remote read helper failed", stderr=proc.stderr, exit_code=proc.returncode)
        payload = json.loads(proc.stdout or "{}")
        if payload.get("error"):
            return _err(payload["error"])
        return _ok(host=host.alias, **payload)
    except subprocess.TimeoutExpired:
        return _err("remote read timed out", timed_out=True)
    except Exception as exc:
        return _err(str(exc))


def remote_write_file(args: dict[str, Any], **_: Any) -> str:
    try:
        host = _host_from_args(args)
        if not host.allow_write:
            raise ValueError("remote_write_file is disabled for this host")
        path = args.get("path")
        content = args.get("content")
        if not isinstance(path, str) or not path:
            raise ValueError("path must be a non-empty string")
        if not isinstance(content, str):
            raise ValueError("content must be a string")
        proc = _run_remote_python(
            host,
            WRITE_HELPER,
            {
                "path": path,
                "content": content,
                "allowed_roots": host.allowed_roots,
                "workdir_only": host.workdir_only,
                "workdir": host.workdir or ".",
            },
            host.connect_timeout + host.command_timeout,
        )
        if proc.returncode != 0:
            return _err("remote write helper failed", stderr=proc.stderr, exit_code=proc.returncode)
        payload = json.loads(proc.stdout or "{}")
        if payload.get("error"):
            return _err(payload["error"])
        return _ok(host=host.alias, **payload)
    except subprocess.TimeoutExpired:
        return _err("remote write timed out", timed_out=True)
    except Exception as exc:
        return _err(str(exc))


def register(ctx) -> None:
    ctx.register_tool(
        name="remote_hosts_list",
        toolset="remote-hosts",
        schema={
            "name": "remote_hosts_list",
            "description": "List configured, enabled remote host aliases without exposing identity files or raw SSH options.",
            "parameters": {"type": "object", "properties": {}},
        },
        handler=remote_hosts_list,
    )
    ctx.register_tool(
        name="remote_terminal",
        toolset="remote-hosts",
        schema={
            "name": "remote_terminal",
            "description": "Run a foreground shell command on a configured remote host alias over SSH.",
            "parameters": {
                "type": "object",
                "properties": {
                    "host": {"type": "string", "description": "Configured remote host alias."},
                    "command": {"type": "string"},
                    "workdir": {"type": "string"},
                    "timeout": {"type": "integer"},
                },
                "required": ["host", "command"],
            },
        },
        handler=remote_terminal,
    )
    ctx.register_tool(
        name="remote_read_file",
        toolset="remote-hosts",
        schema={
            "name": "remote_read_file",
            "description": "Read a text file from a configured remote host alias with line pagination.",
            "parameters": {
                "type": "object",
                "properties": {
                    "host": {"type": "string", "description": "Configured remote host alias."},
                    "path": {"type": "string"},
                    "offset": {"type": "integer", "description": "1-indexed starting line."},
                    "limit": {"type": "integer", "description": "Maximum number of lines."},
                },
                "required": ["host", "path"],
            },
        },
        handler=remote_read_file,
    )
    ctx.register_tool(
        name="remote_write_file",
        toolset="remote-hosts",
        schema={
            "name": "remote_write_file",
            "description": "Atomically overwrite a text file on a configured remote host alias. Requires allow_write=true for the host.",
            "parameters": {
                "type": "object",
                "properties": {
                    "host": {"type": "string", "description": "Configured remote host alias."},
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["host", "path", "content"],
            },
        },
        handler=remote_write_file,
    )
