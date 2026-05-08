from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


PLUGIN_PATH = Path(__file__).resolve().parents[2] / "custom-plugins" / "remote-hosts" / "__init__.py"


@pytest.fixture()
def remote_hosts_module():
    spec = importlib.util.spec_from_file_location("test_remote_hosts_plugin", PLUGIN_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_config(hermes_home: Path, data: dict) -> None:
    hermes_home.mkdir(parents=True, exist_ok=True)
    (hermes_home / "config.yaml").write_text(yaml.safe_dump(data))


def test_config_parses_shorthand_full_dict_and_omits_disabled(
    tmp_path,
    monkeypatch,
    remote_hosts_module,
):
    key = tmp_path / "id_ed25519"
    key.write_text("secret")
    _write_config(
        tmp_path,
        {
            "remote_hosts": {
                "hosts": {
                    "lab": "donovan@homelab",
                    "desktop": {
                        "host": "desktop.example.com",
                        "user": "donovan",
                        "port": 2202,
                        "identity_file": str(key),
                        "workdir": "~/work",
                        "connect_timeout": 7,
                        "command_timeout": 30,
                        "strict_host_key_checking": False,
                        "allow_write": True,
                        "allowed_roots": ["~/work"],
                        "workdir_only": True,
                    },
                    "off": {
                        "enabled": False,
                        "host": "off.example.com",
                        "user": "nobody",
                    },
                }
            }
        },
    )
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    hosts = remote_hosts_module._load_hosts()

    assert set(hosts) == {"lab", "desktop"}
    assert hosts["lab"].user == "donovan"
    assert hosts["lab"].host == "homelab"
    assert hosts["desktop"].port == 2202
    assert hosts["desktop"].identity_file == str(key.resolve())
    assert hosts["desktop"].strict_host_key_checking is False


def test_config_rejects_bad_alias_missing_identity_and_option_like_user(
    tmp_path,
    monkeypatch,
    remote_hosts_module,
):
    _write_config(
        tmp_path,
        {
            "remote_hosts": {
                "hosts": {
                    "bad/alias": "donovan@host",
                }
            }
        },
    )
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    with pytest.raises(ValueError, match="invalid host alias"):
        remote_hosts_module._load_hosts()

    _write_config(
        tmp_path,
        {
            "remote_hosts": {
                "hosts": {
                    "desktop": {
                        "host": "desktop.example.com",
                        "user": "donovan",
                        "identity_file": str(tmp_path / "missing"),
                    }
                }
            }
        },
    )
    with pytest.raises(ValueError, match="identity_file does not exist"):
        remote_hosts_module._load_hosts()

    _write_config(
        tmp_path,
        {"remote_hosts": {"hosts": {"dict": {"host": "host", "user": "-lroot"}}}},
    )
    with pytest.raises(ValueError, match="may not start"):
        remote_hosts_module._load_hosts()

    _write_config(
        tmp_path,
        {"remote_hosts": {"hosts": {"short": "-lroot@host"}}},
    )
    with pytest.raises(ValueError, match="may not start"):
        remote_hosts_module._load_hosts()


def test_remote_hosts_list_hides_identity_file_and_raw_options(
    tmp_path,
    monkeypatch,
    remote_hosts_module,
):
    key = tmp_path / "id_ed25519"
    key.write_text("secret")
    _write_config(
        tmp_path,
        {
            "remote_hosts": {
                "hosts": {
                    "desktop": {
                        "host": "desktop.example.com",
                        "user": "donovan",
                        "identity_file": str(key),
                    }
                }
            }
        },
    )
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    payload = json.loads(remote_hosts_module.remote_hosts_list({}))

    assert payload["success"] is True
    listed = payload["hosts"][0]
    assert listed["alias"] == "desktop"
    assert "identity_file" not in listed
    assert "ssh_options" not in listed


def test_remote_terminal_uses_argv_shell_false_and_json_payload(
    tmp_path,
    monkeypatch,
    remote_hosts_module,
):
    key = tmp_path / "id_ed25519"
    key.write_text("secret")
    _write_config(
        tmp_path,
        {
            "remote_hosts": {
                "hosts": {
                    "desktop": {
                        "host": "desktop.example.com",
                        "user": "donovan",
                        "port": 2202,
                        "identity_file": str(key),
                        "workdir": "~/repo",
                        "connect_timeout": 3,
                        "command_timeout": 20,
                    }
                }
            }
        },
    )
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        req = json.loads(kwargs["input"])
        assert req == {
            "command": "printf '$HOME && a b'",
            "workdir": "~/repo",
            "max_chars": remote_hosts_module.MAX_OUTPUT_CHARS,
        }
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=json.dumps({"stdout": "ok", "stderr": "", "exit_code": 0}),
            stderr="",
        )

    monkeypatch.setattr(remote_hosts_module.subprocess, "run", fake_run)

    payload = json.loads(
        remote_hosts_module.remote_terminal(
            {"host": "desktop", "command": "printf '$HOME && a b'", "timeout": 999}
        )
    )

    assert payload["success"] is True
    assert payload["stdout"] == "ok"
    argv = seen["argv"]
    assert isinstance(argv, list)
    assert seen["kwargs"]["shell"] is False
    assert seen["kwargs"]["timeout"] == 28
    assert argv[:2] == ["ssh", "-o"]
    assert "BatchMode=yes" in argv
    assert "RequestTTY=no" in argv
    assert "ConnectTimeout=3" in argv
    assert "StrictHostKeyChecking=yes" in argv
    assert "-i" in argv
    dest_i = argv.index("donovan@desktop.example.com")
    assert argv[dest_i - 1] == "--"
    assert argv[dest_i + 1].startswith("python3 -c ")
    assert "-c" not in argv[dest_i + 2:]
    assert "printf '$HOME && a b'" not in argv


def test_remote_terminal_blocks_when_command_guard_denies(
    tmp_path,
    monkeypatch,
    remote_hosts_module,
):
    _write_config(
        tmp_path,
        {"remote_hosts": {"hosts": {"lab": "donovan@homelab"}}},
    )
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(
        remote_hosts_module,
        "_check_command_allowed",
        lambda command, host: {"approved": False, "message": "BLOCKED: nope"},
    )

    def fail_run(*_args, **_kwargs):
        raise AssertionError("ssh should not run when the command guard denies")

    monkeypatch.setattr(remote_hosts_module.subprocess, "run", fail_run)

    payload = json.loads(
        remote_hosts_module.remote_terminal({"host": "lab", "command": "rm -rf /"})
    )

    assert payload["success"] is False
    assert payload["error"] == "BLOCKED: nope"


def test_command_guard_import_failure_blocks(monkeypatch, remote_hosts_module):
    host = remote_hosts_module.HostConfig(alias="lab", host="homelab", user="donovan")
    real_import = __import__

    def fake_import(name, *args, **kwargs):
        if name == "tools.terminal_tool":
            raise ImportError("terminal guard unavailable")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", fake_import)

    result = remote_hosts_module._check_command_allowed("touch /tmp/ok", host)

    assert result["approved"] is False
    assert "guard unavailable" in result["message"]


def test_remote_terminal_respects_workdir_only(
    tmp_path,
    monkeypatch,
    remote_hosts_module,
):
    _write_config(
        tmp_path,
        {
            "remote_hosts": {
                "hosts": {
                    "lab": {
                        "host": "homelab",
                        "user": "donovan",
                        "workdir": "~/repo",
                        "workdir_only": True,
                    }
                }
            }
        },
    )
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(
        remote_hosts_module,
        "_check_command_allowed",
        lambda command, host: {"approved": True, "message": None},
    )

    payload = json.loads(
        remote_hosts_module.remote_terminal(
            {"host": "lab", "command": "pwd", "workdir": "/tmp"}
        )
    )

    assert payload["success"] is False
    assert "workdir override" in payload["error"]


def test_remote_terminal_timeout_and_output_truncation(
    tmp_path,
    monkeypatch,
    remote_hosts_module,
):
    _write_config(
        tmp_path,
        {"remote_hosts": {"hosts": {"lab": "donovan@homelab"}}},
    )
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    def fake_run_timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("ssh", timeout=1, output="partial", stderr="late")

    monkeypatch.setattr(remote_hosts_module.subprocess, "run", fake_run_timeout)
    payload = json.loads(
        remote_hosts_module.remote_terminal({"host": "lab", "command": "sleep 99"})
    )
    assert payload["success"] is True
    assert payload["timed_out"] is True
    assert payload["stdout"] == "partial"

    def fake_run_large(argv, **_kwargs):
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=json.dumps(
                {
                    "stdout": "x" * (remote_hosts_module.MAX_OUTPUT_CHARS + 1),
                    "stderr": "",
                    "exit_code": 0,
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(remote_hosts_module.subprocess, "run", fake_run_large)
    payload = json.loads(
        remote_hosts_module.remote_terminal({"host": "lab", "command": "yes"})
    )
    assert payload["truncated"] is True
    assert len(payload["stdout"]) == remote_hosts_module.MAX_OUTPUT_CHARS


def test_remote_helpers_cap_output_without_tempfile_or_readlines(
    tmp_path,
    remote_hosts_module,
):
    terminal_proc = subprocess.run(
        [sys.executable, "-c", remote_hosts_module.TERMINAL_HELPER],
        input=json.dumps(
            {
                "command": f"{sys.executable} -c \"print('x' * 2000)\"",
                "workdir": None,
                "max_chars": 100,
            }
        ),
        capture_output=True,
        text=True,
        check=True,
    )
    terminal_payload = json.loads(terminal_proc.stdout)
    assert terminal_payload["truncated"] is True
    assert len(terminal_payload["stdout"] + terminal_payload["stderr"]) == 100

    long_file = tmp_path / "long.txt"
    long_file.write_text("a" * 2000)
    read_proc = subprocess.run(
        [sys.executable, "-c", remote_hosts_module.READ_HELPER],
        input=json.dumps(
            {
                "path": str(long_file),
                "offset": 1,
                "limit": 1,
                "max_chars": 100,
                "allowed_roots": [str(tmp_path)],
                "workdir_only": False,
                "workdir": str(tmp_path),
            }
        ),
        capture_output=True,
        text=True,
        check=True,
    )
    read_payload = json.loads(read_proc.stdout)
    assert read_payload["truncated"] is True
    assert read_payload["total_lines"] is None
    assert len(read_payload["content"]) == 100


def test_file_helpers_workdir_only_ignores_allowed_roots(tmp_path, remote_hosts_module):
    workdir = tmp_path / "work"
    outside = tmp_path / "outside"
    workdir.mkdir()
    outside.mkdir()
    outside_file = outside / "file.txt"
    outside_file.write_text("secret")
    payload = {
        "path": str(outside_file),
        "offset": 1,
        "limit": 10,
        "max_chars": 1000,
        "allowed_roots": [str(outside)],
        "workdir_only": True,
        "workdir": str(workdir),
    }

    read_proc = subprocess.run(
        [sys.executable, "-c", remote_hosts_module.READ_HELPER],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=True,
    )
    write_proc = subprocess.run(
        [sys.executable, "-c", remote_hosts_module.WRITE_HELPER],
        input=json.dumps({**payload, "content": "nope"}),
        capture_output=True,
        text=True,
        check=True,
    )

    assert json.loads(read_proc.stdout)["error"] == "path is outside allowed roots"
    assert json.loads(write_proc.stdout)["error"] == "path is outside allowed roots"
    assert outside_file.read_text() == "secret"


def test_remote_read_and_write_helpers_use_json_stdin(
    tmp_path,
    monkeypatch,
    remote_hosts_module,
):
    _write_config(
        tmp_path,
        {
            "remote_hosts": {
                "hosts": {
                    "desktop": {
                        "host": "desktop.example.com",
                        "user": "donovan",
                        "allow_write": True,
                        "allowed_roots": ["~/repo"],
                    }
                }
            }
        },
    )
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, json.loads(kwargs["input"]), kwargs))
        assert kwargs["shell"] is False
        if len(calls) == 1:
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout=json.dumps(
                    {
                        "path": "/home/donovan/repo/file.txt",
                        "offset": 2,
                        "limit": 3,
                        "total_lines": 10,
                        "content": "b\nc\nd\n",
                        "truncated": False,
                    }
                ),
                stderr="",
            )
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=json.dumps({"path": "/home/donovan/repo/file.txt", "bytes": 5}),
            stderr="",
        )

    monkeypatch.setattr(remote_hosts_module.subprocess, "run", fake_run)

    read_payload = json.loads(
        remote_hosts_module.remote_read_file(
            {"host": "desktop", "path": "~/repo/file.txt", "offset": 2, "limit": 3}
        )
    )
    write_payload = json.loads(
        remote_hosts_module.remote_write_file(
            {"host": "desktop", "path": "~/repo/file.txt", "content": "hello"}
        )
    )

    assert read_payload["success"] is True
    assert read_payload["content"] == "b\nc\nd\n"
    assert write_payload["success"] is True
    assert calls[0][1]["path"] == "~/repo/file.txt"
    assert calls[0][1]["offset"] == 2
    assert calls[1][1]["content"] == "hello"
    assert "hello" not in calls[1][0]


def test_remote_write_disabled_by_default(tmp_path, monkeypatch, remote_hosts_module):
    _write_config(tmp_path, {"remote_hosts": {"hosts": {"lab": "donovan@homelab"}}})
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    payload = json.loads(
        remote_hosts_module.remote_write_file(
            {"host": "lab", "path": "/tmp/file", "content": "no"}
        )
    )

    assert payload["success"] is False
    assert "disabled" in payload["error"]
