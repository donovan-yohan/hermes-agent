# Remote Hosts

Fork-local Hermes plugin for using one long-lived Hermes gateway as an explicit SSH control plane for configured remote host aliases.

This is intentionally **not** a replacement for the built-in `terminal()` backend. The normal local terminal stays stable; the model must opt into remote execution by calling `remote_terminal(host=...)`, `remote_read_file(host=...)`, or `remote_write_file(host=...)` against a human-configured alias.

## Enable

```bash
hermes plugins enable remote-hosts
```

Restart the Hermes gateway after changing plugin or `remote_hosts` config.

## Configure

Add host aliases to `config.yaml`:

```yaml
remote_hosts:
  hosts:
    desktop:
      host: desktop.example.com
      user: donovan
      port: 22
      identity_file: ~/.ssh/id_ed25519
      workdir: ~/Documents/Programs
      enabled: true
      connect_timeout: 10
      command_timeout: 120
      strict_host_key_checking: true
      allow_write: false
      allowed_roots:
        - ~/Documents/Programs
      workdir_only: false
```

Shorthand is also supported:

```yaml
remote_hosts:
  hosts:
    homelab: donovan@homelab
```

Disabled hosts are omitted:

```yaml
remote_hosts:
  hosts:
    old-box:
      enabled: false
      host: old-box.example.com
      user: donovan
```

## Tools

- `remote_hosts_list()` — lists enabled aliases without exposing `identity_file` or raw SSH options.
- `remote_terminal(host, command, workdir?, timeout?)` — runs a foreground command with remote `bash -lc`.
- `remote_read_file(host, path, offset?, limit?)` — reads text with line pagination using a remote `python3` helper.
- `remote_write_file(host, path, content)` — atomically overwrites a text file using a remote `python3` helper; disabled unless the host has `allow_write: true`.

## Security model

- The model can only target configured aliases. It cannot provide arbitrary SSH destinations.
- Local SSH is invoked as an argv list with `shell=False`.
- SSH uses `BatchMode=yes`, `RequestTTY=no`, `ConnectTimeout`, `ServerAliveInterval`, `ServerAliveCountMax`, and `StrictHostKeyChecking=yes` by default.
- `user`, `host`, alias, port, timeouts, and identity file paths are validated before use.
- `remote_terminal` runs Hermes' existing dangerous-command guard with SSH semantics before execution.
- `remote_read_file` and `remote_write_file` pass JSON over stdin/stdout; file contents are not shell-embedded.
- `allowed_roots` restricts remote file read/write paths when provided.
- `workdir_only: true` restricts file tools to the configured `workdir` and disables per-call terminal workdir overrides.

This plugin is a routing boundary, not a sandbox. Remote commands still have whatever permissions the SSH user has on the remote machine; avoid configuring high-privilege SSH accounts unless the operational risk is intentional.
