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
from typing import Any

from .models import ApiError, VMInstance

# Signature of the injectable HTTP request function used by REST providers.
# (method, path, token, body, timeout) -> parsed JSON dict.
RequestFn = Callable[..., dict[str, Any]]

#: cloud-init template: creates a non-root ``hermes`` user with passwordless
#: sudo and the injected SSH key, and pre-installs the toolchain a coding /
#: research / creative AI agent is likely to invoke.  Every provider uses this
#: so the sandbox never connects as (or runs commands as) ``root`` and boots
#: ready to work on first SSH.
_CLOUD_INIT_USER_DATA = """\
#cloud-config
package_update: true

users:
  - name: hermes
    sudo: ALL=(ALL) NOPASSWD:ALL
    shell: /bin/bash
    ssh_authorized_keys:
      - {public_key}

packages:
  # ── build essentials ──
  - build-essential
  - pkg-config
  - cmake
  - libssl-dev
  - libffi-dev
  - libsqlite3-dev
  - libbz2-dev
  - liblzma-dev
  - zlib1g-dev
  # ── network / transfer / archives ──
  - curl
  - wget
  - git
  - rsync
  - openssh-client
  - ca-certificates
  - gnupg
  - unzip
  - zip
  - socat
  - netcat-openbsd
  # ── shell / text / search ──
  - jq
  - ripgrep
  - fd-find
  - fzf
  - bat
  - eza
  - tree
  - tmux
  - direnv
  - zoxide
  - less
  # ── languages ──
  - python3-pip
  - python3-venv
  - python3-dev
  - golang-go
  - nodejs
  - npm
  # ── containers (rootless podman) ──
  - podman
  - buildah
  - skopeo
  - uidmap
  - slirp4netns
  - fuse-overlayfs
  - catatonit
  # ── media / creative ──
  - ffmpeg
  - imagemagick
  - poppler-utils
  - tesseract-ocr
  - pandoc
  # ── audio / TTS support ──
  - espeak-ng
  - mpg123
  - sox
  - libsox-fmt-all
  - alsa-utils
  # ── dev quality / data ──
  - shellcheck
  - shfmt
  - sqlite3
  - gh
  - htop
  - graphviz

runcmd:
  # Canonical binary-name aliases (Ubuntu renames these to avoid conflicts;
  # an agent will call them by their upstream names).
  - ln -sf "$(which fdfind)" /usr/local/bin/fd
  - ln -sf "$(which batcat)" /usr/local/bin/bat
  # Rootless-podman subuid/subgid ranges for the hermes user.
  - usermod --add-subuids 100000-165535 --add-subgids 100000-165535 hermes
  # uv — fast Python package/venv manager.
  - curl -LsSf https://astral.sh/uv/install.sh | sh
  # rustup + stable toolchain (current Rust; apt's is stale).
  - curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
  # yq (mikefarah Go binary — NOT the apt Python yq wrapper).
  - |
    YQ_VER=$(curl -sL https://api.github.com/repos/mikefarah/yq/releases/latest | grep tag_name | cut -d '"' -f4)
    wget -qO /usr/local/bin/yq "https://github.com/mikefarah/yq/releases/download/${{YQ_VER}}/yq_linux_amd64"
    chmod +x /usr/local/bin/yq
  # edge-tts — open-source Microsoft Edge TTS (no API key required).
  - python3 -m pip install --break-system-packages edge-tts
"""


def build_cloud_init_user_data(public_key: str) -> str:
    """Return cloud-init user-data that creates the ``hermes`` user.

    All providers use this helper so non-root provisioning stays consistent
    across QEMU (NoCloud seed ISO) and cloud providers (``user_data`` API
    field).  The caller is responsible for base64-encoding when the API
    requires it (e.g. Hetzner).

    The template pre-installs a curated toolchain (build essentials, common
    languages, media/TTS tools, modern shell utilities) so the sandbox boots
    ready to work.  The ``${{...}}`` escaping preserves cloud-init's literal
    shell variable after Python's ``str.format`` runs.
    """
    return _CLOUD_INIT_USER_DATA.format(public_key=public_key)


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
    ) -> VMInstance:
        """Create (or reuse) a VM and return its description.

        Implementations should block until the VM has a public IP, but may
        return before SSH is fully reachable — the lifecycle manager polls
        readiness separately.
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
    """
    deadline = clock() + timeout
    while True:
        if predicate():
            return
        if clock() >= deadline:
            raise TimeoutError(f"poll_until: condition not met within {timeout}s")
        sleep(interval)


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
