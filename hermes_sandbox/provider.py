"""Cloud provider abstraction for the sandbox plugin.

Each provider implements the same :class:`CloudProvider` interface so the
lifecycle manager and session hook stay cloud-agnostic.  A provider is
registered via :func:`register_provider` and instantiated by name through
:func:`get_provider`.

The interface is deliberately minimal: provision a VM (injecting an SSH
public key), describe it, tear it down, and list VMs carrying a given
label so the lifecycle manager can find a previously-created sandbox.

Domain value objects (:class:`~hermes_sandbox.models.VMInstance`,
:class:`~hermes_sandbox.models.ApiError`) live in :mod:`hermes_sandbox.models`.
"""

from __future__ import annotations

import contextlib
import json
import time
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from collections.abc import Callable
from importlib import resources
from typing import Any

import yaml

from .models import ApiError, VMInstance

# Signature of the injectable HTTP request function used by REST providers.
# (method, path, token, body, timeout) -> parsed JSON dict.
RequestFn = Callable[..., dict[str, Any]]


def _base_packages() -> list[str]:
    """Return the curated package list installed on every sandbox VM."""
    return [
        # -- build essentials --
        "build-essential",
        "pkg-config",
        "cmake",
        "libssl-dev",
        "libffi-dev",
        "libsqlite3-dev",
        "libbz2-dev",
        "liblzma-dev",
        "zlib1g-dev",
        # -- network / transfer / archives --
        "curl",
        "wget",
        "git",
        "rsync",
        "openssh-client",
        "ca-certificates",
        "gnupg",
        "unzip",
        "zip",
        "socat",
        "netcat-openbsd",
        # -- shell / text / search --
        "jq",
        "ripgrep",
        "fd-find",
        "fzf",
        "bat",
        "eza",
        "tree",
        "tmux",
        "direnv",
        "zoxide",
        "less",
        # -- languages --
        "python3-pip",
        "python3-venv",
        "python3-dev",
        "golang-go",
        "nodejs",
        "npm",
        # -- containers (rootless podman) --
        "podman",
        "buildah",
        "skopeo",
        "uidmap",
        "slirp4netns",
        "fuse-overlayfs",
        "catatonit",
        # -- media / creative --
        "ffmpeg",
        "imagemagick",
        "poppler-utils",
        "tesseract-ocr",
        "pandoc",
        # -- audio / TTS support --
        "espeak-ng",
        "mpg123",
        "sox",
        "libsox-fmt-all",
        "alsa-utils",
        # -- security: automatic updates --
        "unattended-upgrades",
        # -- dev quality / data --
        "shellcheck",
        "shfmt",
        "sqlite3",
        "gh",
        "htop",
        "graphviz",
    ]


_YQ_INSTALL_CMD = (
    'YQ_VER=$(curl -sL https://api.github.com/repos/mikefarah/yq/releases/latest | grep tag_name | cut -d \'"\' -f4)\n'
    'wget -qO /usr/local/bin/yq "https://github.com/mikefarah/yq/releases/download/${YQ_VER}/yq_linux_amd64"\n'
    "chmod +x /usr/local/bin/yq"
)


def _base_runcmd() -> list[str]:
    """Return the base runcmd list executed on every sandbox VM first boot."""
    return [
        "systemctl restart ssh",
        # Enable unattended-upgrades (automatic security updates).
        "dpkg-reconfigure -f noninteractive unattended-upgrades",
        "systemctl enable --now apt-daily.timer apt-daily-upgrade.timer",
        'ln -sf "$(which fdfind)" /usr/local/bin/fd',
        'ln -sf "$(which batcat)" /usr/local/bin/bat',
        "usermod --add-subuids 100000-165535 --add-subgids 100000-165535 hermes",
        "curl -LsSf https://astral.sh/uv/install.sh | sh",
        "curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y",
        _YQ_INSTALL_CMD,
        "python3 -m pip install --break-system-packages edge-tts",
    ]


def _agents_md_content() -> str:
    """Return the AGENTS.md text shipped as a package resource.

    The file is seeded into every sandbox VM's home directory so the agent
    boots with context about its environment.
    """
    return resources.files(__name__).joinpath("AGENTS.md").read_text("utf-8")


def _base_cloud_init_config(public_key: str) -> dict[str, Any]:
    """Return the base cloud-init config as a Python dict.

    Creates a non-root ``hermes`` user with passwordless sudo and the injected
    SSH key, hardens SSH to key-only access (no root login, no password auth),
    and pre-installs a curated toolchain so the sandbox boots ready to work.

    SSH hardening is applied via ``ssh_pwauth: false`` plus a drop-in sshd
    config in ``write_files``, followed by an early ``systemctl restart ssh``
    in ``runcmd`` so the hardening is active before any other first-boot
    activity.
    """
    return {
        "package_update": True,
        "ssh_pwauth": False,
        "users": [
            {
                "name": "hermes",
                "sudo": "ALL=(ALL) NOPASSWD:ALL",
                "shell": "/bin/bash",
                "ssh_authorized_keys": [public_key],
            }
        ],
        "write_files": [
            {
                "path": "/etc/ssh/sshd_config.d/99-sandbox-hardening.conf",
                "content": (
                    "PermitRootLogin no\n"
                    "PasswordAuthentication no\n"
                    "PubkeyAuthentication yes\n"
                ),
                "permissions": "0644",
                "owner": "root:root",
            },
            {
                "path": "/home/hermes/AGENTS.md",
                "content": _agents_md_content(),
                "permissions": "0644",
                "owner": "hermes:hermes",
            },
        ],
        "packages": _base_packages(),
        "runcmd": _base_runcmd(),
    }


class _CloudInitDumper(yaml.SafeDumper):  # type: ignore[misc]
    """SafeDumper that forces literal-block style for multi-line runcmd strings."""


def _str_representer(dumper: _CloudInitDumper, data: str) -> yaml.Node:
    """Use literal block scalar (``|``) for strings containing newlines."""
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


_CloudInitDumper.add_representer(str, _str_representer)


def _dump_cloud_config(config: dict[str, Any]) -> str:
    """Serialize a cloud-init config dict to ``#cloud-config`` YAML text."""
    body = yaml.dump(
        config,
        Dumper=_CloudInitDumper,
        default_flow_style=False,
        sort_keys=False,
        width=1000,
    )
    return f"#cloud-config\n{body}"


def build_cloud_init_user_data(
    public_key: str,
    forward_env: dict[str, str] | None = None,
    apt_repos: tuple[str, ...] = (),
    extra_packages: tuple[str, ...] = (),
    setup_commands: tuple[str, ...] = (),
) -> str:
    """Return cloud-init user-data that creates the ``hermes`` user.

    All providers use this helper so non-root provisioning stays consistent
    across QEMU (NoCloud seed ISO) and cloud providers (``user_data`` API
    field).  The caller is responsible for base64-encoding when the API
    requires it (e.g. Hetzner).

    The config pre-installs a curated toolchain (build essentials, common
    languages, media/TTS tools, modern shell utilities) so the sandbox boots
    ready to work.

    SSH is hardened to key-only access: password authentication is disabled,
    root login over SSH is blocked, and sshd is restarted early in ``runcmd``
    so the hardening is active before any other first-boot activity.

    When *forward_env* is non-empty, a ``runcmd`` entry is prepended to the
    template's existing ``runcmd`` list that appends each forwarded var to
    ``/etc/environment``.  Appending (rather than ``write_files`` which
    clobbers) preserves existing system entries like ``PATH`` and ``LANG``.
    ``/etc/environment`` is read by PAM (``pam_env``) during every
    SSH session, so the forwarded values are injected at the process level
    — no shell sourcing, login shell, or snapshot dependency required.

    *apt_repos* lines are injected as ``apt_sources`` entries (before the
    ``packages`` list) so the VM can install packages from third-party
    repositories.  *extra_packages* are appended to the ``packages`` list.
    *setup_commands* are appended to the ``runcmd`` list after the existing
    entries.  All three are additive — the existing template content is
    unchanged.
    """
    config = _base_cloud_init_config(public_key)

    # Rebuild the dict so apt_sources is ordered before packages (cloud-init
    # processes keys independently, but keeping a natural order is cleaner and
    # the tests assert on source ordering).
    apt_sources = (
        [{"source": repo} for repo in apt_repos] if apt_repos else None
    )

    ordered: dict[str, Any] = {}
    ordered["package_update"] = config["package_update"]
    ordered["ssh_pwauth"] = config["ssh_pwauth"]
    ordered["users"] = config["users"]
    ordered["write_files"] = config["write_files"]
    if apt_sources:
        ordered["apt_sources"] = apt_sources
    if extra_packages:
        ordered["packages"] = [*config["packages"], *extra_packages]
    else:
        ordered["packages"] = config["packages"]
    if setup_commands:
        ordered["runcmd"] = [*config["runcmd"], *setup_commands]
    else:
        ordered["runcmd"] = config["runcmd"]

    if forward_env:
        append_cmds = "".join(
            f"printf '%s=%s\\n' {name!r} {value!r} >> /etc/environment\n"
            for name, value in sorted(forward_env.items())
        )
        ordered["runcmd"] = [append_cmds, *ordered["runcmd"]]

    return _dump_cloud_config(ordered)


class CloudProvider(ABC):
    """Interface every cloud backend must implement."""

    #: Registry key / display name.  Subclasses set this.
    name: str = ""

    #: Environment variables checked (in order) when resolving the API token.
    #: The first non-empty value wins.  ``SANDBOX_TOKEN`` is a generic
    #: fallback so users can set one token for any provider.
    token_env_keys: tuple[str, ...] = ("SANDBOX_TOKEN",)

    #: Provider-specific defaults applied by ``SandboxConfig.from_env`` when the
    #: corresponding ``SANDBOX_*`` env var is unset.  Subclasses override
    #: these so adding a provider needs no edit to ``SandboxConfig``.
    default_region: str = "nbg1"
    default_image: str = "ubuntu-24.04"
    default_instance_type: str = "cx23"
    default_ssh_user: str = "hermes"

    #: When True, the lifecycle manager skips the SSH readiness probe for VMs
    #: from this provider.  Real clouds need the probe (a VM may be "running"
    #: per the API but not yet accepting SSH), but in-memory / dry-run
    #: providers return a fake IP that can never be reached -- probing it
    #: blocks for the full timeout on every call, which exhausts the CI
    #: runner time budget.  Dry-run and other simulated providers set this.
    skip_ssh_probe: bool = False

    @classmethod
    def resolve_token(cls, env: dict[str, str]) -> str:
        """Return the first non-empty token from :attr:`token_env_keys`."""
        for key in cls.token_env_keys:
            val = env.get(key, "")
            if val:
                return val
        return ""

    @abstractmethod
    def provision(
        self,
        name: str,
        instance_type: str,
        region: str,
        image: str,
        ssh_public_key: str,
        labels: dict[str, str] | None = None,
        forward_env: dict[str, str] | None = None,
        apt_repos: tuple[str, ...] = (),
        extra_packages: tuple[str, ...] = (),
        setup_commands: tuple[str, ...] = (),
    ) -> VMInstance:
        """Create (or reuse) a VM and return its description.

        Implementations should block until the VM has a public IP, but may
        return before SSH is fully reachable — the lifecycle manager polls
        readiness separately.

        When *forward_env* is non-empty, the values are injected into the VM
        at provision time so the login shell environment includes them.

        *apt_repos* are injected as cloud-init ``apt_sources`` entries,
        *extra_packages* are appended to the ``packages`` list, and
        *setup_commands* are appended to the ``runcmd`` list.  All three
        are additive to the base template.
        """

    @abstractmethod
    def get(self, vm_id: str) -> VMInstance:
        """Return the current description of a VM by provider id."""

    @abstractmethod
    def teardown(self, vm_id: str) -> None:
        """Destroy the VM.  Idempotent: a missing VM is a success."""

    @abstractmethod
    def list_by_label(self, label_key: str, label_value: str) -> list[VMInstance]:
        """Return VMs carrying ``label_key=label_value``."""


# --------------------------------------------------------------------------- #
# Functional helpers
# --------------------------------------------------------------------------- #


def poll_until(
    predicate: Callable[[], bool],
    *,
    interval: float,
    timeout: float,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Call ``predicate`` until it returns True, sleeping ``interval`` between tries.

    Raises ``TimeoutError`` if ``predicate`` is still falsy after ``timeout``
    seconds.  ``sleep`` and ``clock`` are injectable so the poll loop can be
    exercised in tests without real waiting.  The predicate is always called at
    least once before sleeping.

    The sleep duration is reduced by the time the predicate itself took, so
    slow API calls inside the predicate don't consume the entire timeout
    budget.  If the predicate took longer than ``interval``, no sleep occurs.
    """
    deadline = clock() + timeout
    while True:
        pre = clock()
        if predicate():
            return
        post = clock()
        if post >= deadline:
            raise TimeoutError(f"poll_until: condition not met within {timeout}s")
        predicate_duration = post - pre
        remaining_sleep = max(0.0, interval - predicate_duration)
        sleep(min(remaining_sleep, deadline - post))


# --------------------------------------------------------------------------- #
# Provider registry
# --------------------------------------------------------------------------- #

_REGISTRY: dict[str, type[CloudProvider]] = {}


def register_provider(cls: type[CloudProvider]) -> type[CloudProvider]:
    """Class decorator: register a :class:`CloudProvider` subclass by ``name``."""
    if not getattr(cls, "name", ""):
        raise ValueError(f"{cls.__name__} must set a non-empty `name`")
    _REGISTRY[cls.name] = cls
    return cls


def get_provider(name: str, **kwargs: Any) -> CloudProvider:
    """Instantiate a registered provider by name."""
    return get_provider_class(name)(**kwargs)


def get_provider_class(name: str) -> type[CloudProvider]:
    """Return the registered provider class by name (without instantiating)."""
    try:
        return _REGISTRY[name]
    except KeyError as exc:
        raise KeyError(f"Unknown cloud provider {name!r}. Registered: {sorted(_REGISTRY)}") from exc


def available_providers() -> list[str]:
    """Return the sorted names of every registered cloud provider."""
    return sorted(_REGISTRY)


# --------------------------------------------------------------------------- #
# REST plumbing shared by cloud providers
# --------------------------------------------------------------------------- #


def http_json_request(
    api_base: str,
    method: str,
    path: str,
    token: str,
    body: dict[str, Any] | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    """Perform an authenticated bearer-token JSON request over stdlib urllib.

    Shared by every REST provider — the only thing that varies is ``api_base``.
    Raises :class:`ApiError` (a ``RuntimeError``) on a non-2xx response.
    """
    url = f"{api_base}/{path.lstrip('/')}"
    data = None
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = ""
        with contextlib.suppress(Exception):
            detail = exc.read().decode("utf-8", errors="replace")
        raise ApiError(method, path, status_code=exc.code, detail=detail) from exc


class RestCloudProvider(CloudProvider):
    """Base for providers built on a bearer-token REST API over urllib.

    Holds the token/poll/sleep wiring and idempotent teardown once, so concrete
    providers only declare their API shape (endpoints, JSON → VMInstance mapping,
    status normalisation).
    """

    #: Base URL of the cloud REST API, e.g. ``https://api.hetzner.cloud/v1``.
    api_base: str = ""

    def __init__(
        self,
        token: str,
        poll_interval: float = 3.0,
        poll_timeout: int = 180,
        request_fn: RequestFn | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not token:
            raise ValueError(f"{type(self).__name__} requires a non-empty token")
        self.token = token
        self.poll_interval = poll_interval
        self.poll_timeout = poll_timeout
        # request_fn is injectable for tests; default hits the real API.
        self._request: RequestFn = request_fn or self._default_request
        self._sleep: Callable[[float], None] = sleep

    def _default_request(
        self,
        method: str,
        path: str,
        token: str,
        body: dict[str, Any] | None = None,
        timeout: int = 30,
    ) -> dict[str, Any]:
        return http_json_request(self.api_base, method, path, token, body, timeout)

    @abstractmethod
    def _delete_path(self, vm_id: str) -> str:
        """REST path for destroying a VM. Subclasses must implement."""

    def teardown(self, vm_id: str) -> None:
        """Idempotent teardown: a 404 (already gone) is a success."""
        try:
            self._request("DELETE", self._delete_path(vm_id), self.token)
        except ApiError as exc:
            if exc.status_code == 404:
                return
            raise
