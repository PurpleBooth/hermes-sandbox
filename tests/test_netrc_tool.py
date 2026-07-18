"""Tests for host-side SANDBOX_NETRC slash/tool helpers."""

from __future__ import annotations

import json

import pytest

from hermes_sandbox.config import NetrcSpec, SandboxConfig
from hermes_sandbox.netrc_tool import (
    handle_netrc_command,
    render_netrc,
    resolve_entries,
    show_netrc_status,
    write_netrc_via_ssh,
)


def test_resolve_entries_skips_missing(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_x")
    monkeypatch.delenv("CODEBERG_TOKEN", raising=False)
    specs = (
        NetrcSpec(machine="github.com", password_env="GITHUB_TOKEN"),
        NetrcSpec(machine="codeberg.org", password_env="CODEBERG_TOKEN"),
    )
    entries = resolve_entries(specs)
    assert entries == [("github.com", "x-access-token", "ghp_x")]


def test_render_netrc_body(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_x")
    body = render_netrc((NetrcSpec(machine="github.com", password_env="GITHUB_TOKEN"),))
    assert body is not None
    assert "password ghp_x" in body
    assert "login x-access-token" in body


def test_render_netrc_none_when_empty(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert render_netrc((NetrcSpec(machine="github.com", password_env="GITHUB_TOKEN"),)) is None


def test_show_netrc_status_redacted(monkeypatch):
    monkeypatch.setenv("SANDBOX_PROVIDER", "dry-run")
    monkeypatch.setenv("SANDBOX_NETRC", "github.com:GITHUB_TOKEN")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
    cfg = SandboxConfig.from_env(__import__("os").environ)
    status = show_netrc_status(cfg)
    blob = json.dumps(status)
    assert "ghp_secret" not in blob
    assert status["resolvable"] is True
    assert status["machines"][0]["password_set"] is True
    assert status["machines"][0]["login"] == "x-access-token"


def test_handle_show(monkeypatch):
    monkeypatch.setenv("SANDBOX_PROVIDER", "dry-run")
    monkeypatch.setenv("SANDBOX_NETRC", "github.com:GITHUB_TOKEN")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
    out = handle_netrc_command(["show"])
    data = json.loads(out)
    assert data["configured"] is True
    assert "ghp_secret" not in out


def test_handle_render_redacts_password(monkeypatch):
    monkeypatch.setenv("SANDBOX_PROVIDER", "dry-run")
    monkeypatch.setenv("SANDBOX_NETRC", "github.com:GITHUB_TOKEN")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
    out = handle_netrc_command(["render"])
    assert "password ***" in out
    assert "ghp_secret" not in out
    assert "machine github.com" in out


def test_handle_render_empty(monkeypatch):
    monkeypatch.setenv("SANDBOX_PROVIDER", "dry-run")
    monkeypatch.delenv("SANDBOX_NETRC", raising=False)
    out = handle_netrc_command(["render"])
    assert "No resolvable netrc entries" in out


def test_handle_write_without_manager(monkeypatch):
    monkeypatch.setenv("SANDBOX_PROVIDER", "dry-run")
    monkeypatch.setenv("SANDBOX_NETRC", "github.com:GITHUB_TOKEN")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
    out = handle_netrc_command(["write"], manager=None)
    assert "Not configured" in out


def test_handle_write_calls_ssh(monkeypatch):
    monkeypatch.setenv("SANDBOX_PROVIDER", "dry-run")
    monkeypatch.setenv("SANDBOX_NETRC", "github.com:GITHUB_TOKEN")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")

    called: dict[str, str] = {}

    def fake_write(manager, content):  # noqa: ANN001
        called["content"] = content

    monkeypatch.setattr("hermes_sandbox.netrc_tool.write_netrc_via_ssh", fake_write)
    out = handle_netrc_command(["write"], manager=object())
    assert out.startswith("[sandbox] Wrote")
    assert "password ghp_secret" in called["content"]


def test_handle_unknown():
    out = handle_netrc_command(["wat"])
    assert "Unknown netrc subcommand" in out


def test_handle_help():
    out = handle_netrc_command(["help"])
    assert "sandbox netrc" in out
    assert "SANDBOX_NETRC" in out


def test_slash_netrc_show(monkeypatch):
    """`/sandbox netrc show` is reachable through the main slash handler."""
    import hermes_sandbox

    monkeypatch.setenv("SANDBOX_PROVIDER", "dry-run")
    monkeypatch.setenv("SANDBOX_NETRC", "github.com:GITHUB_TOKEN")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
    out = hermes_sandbox._handle_slash("netrc show")
    assert out is not None
    data = json.loads(out)
    assert data["configured"] is True


def test_tool_handler_show(monkeypatch):
    import hermes_sandbox

    monkeypatch.setenv("SANDBOX_PROVIDER", "dry-run")
    monkeypatch.setenv("SANDBOX_NETRC", "github.com:GITHUB_TOKEN")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
    out = json.loads(hermes_sandbox._sandbox_netrc_handler({"action": "show"}))
    assert out["status"] == "ok"
    assert out["resolvable"] is True
    assert "ghp_secret" not in json.dumps(out)


def test_tool_handler_render(monkeypatch):
    import hermes_sandbox

    monkeypatch.setenv("SANDBOX_PROVIDER", "dry-run")
    monkeypatch.setenv("SANDBOX_NETRC", "github.com:GITHUB_TOKEN")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
    out = json.loads(hermes_sandbox._sandbox_netrc_handler({"action": "render"}))
    assert out["status"] == "ok"
    assert "password ***" in out["netrc"]
    assert "ghp_secret" not in out["netrc"]


def test_tool_handler_bad_action():
    import hermes_sandbox

    out = json.loads(hermes_sandbox._sandbox_netrc_handler({"action": "explode"}))
    assert "error" in out
