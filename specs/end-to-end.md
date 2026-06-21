# End-to-End Specification

This specification verifies the full build → install → import lifecycle of the
vm-sandbox plugin. It builds the wheel from the source tree, installs only that
wheel into a throwaway virtualenv, and proves the published artifact is
self-contained: the package imports and registers its cloud providers without
Hermes Agent installed (Hermes is imported lazily inside hook functions).

Each script runs in a temporary workspace, so the build output and throwaway
venv never touch the repository. The source tree is reached via
`$SPECDOWN_START_DIR`.

## Building the package

The wheel and sdist build cleanly from the source tree.

```shell,script(name="build", expected_exit_code=0)
uv build --project "$SPECDOWN_START_DIR" --out-dir "$SPECDOWN_WORKSPACE_DIR/dist"
```

The build produces a wheel and an sdist.

```shell,script(name="list-dist", expected_exit_code=0)
ls "$SPECDOWN_WORKSPACE_DIR/dist"/*.whl "$SPECDOWN_WORKSPACE_DIR/dist"/*.tar.gz
```

## The wheel is installable in isolation

Create a fresh virtualenv with no dev dependencies and install only the wheel.
`uv venv` provisions an interpreter, so no system Python is required.

```shell,script(name="install", expected_exit_code=0)
uv venv "$SPECDOWN_WORKSPACE_DIR/venv"
uv pip install --python "$SPECDOWN_WORKSPACE_DIR/venv/bin/python" \
    "$SPECDOWN_WORKSPACE_DIR"/dist/*.whl
```

## The installed package registers its cloud providers

Importing `vm_sandbox` registers the Hetzner and DigitalOcean providers. The
package imports without Hermes Agent on `PYTHONPATH`.

```shell,script(name="providers", expected_exit_code=0)
"$SPECDOWN_WORKSPACE_DIR/venv/bin/python" -c \
    "from vm_sandbox.provider import available_providers; print('\n'.join(available_providers()))"
```

```text,verify(script_name="providers", stream=stdout)
digitalocean
fake
hetzner
```

## The plugin manifest ships inside the wheel

Hermes' PluginManager discovers the plugin via `plugin.yaml`, so the manifest
must be present in the installed package — not just in the source checkout.

```shell,script(name="manifest", expected_exit_code=0)
"$SPECDOWN_WORKSPACE_DIR/venv/bin/python" -c \
    "import vm_sandbox, pathlib; print(pathlib.Path(vm_sandbox.__file__).with_name('plugin.yaml').exists())"
```

```text,verify(script_name="manifest", stream=stdout)
True
```

## The shipped version matches the source

The version reported by the installed package metadata matches the version
declared in `pyproject.toml`, so a released tag and the shipped wheel can never
drift.

```shell,script(name="version-match", expected_exit_code=0)
SHIPPED=$("$SPECDOWN_WORKSPACE_DIR/venv/bin/python" -c \
    "from importlib.metadata import version; print(version('hermes-vm-sandbox'))")
DECLARED=$(grep -m1 '^version' "$SPECDOWN_START_DIR/pyproject.toml" \
    | sed -E 's/.*"([^"]+)".*/\1/')
echo "shipped=$SHIPPED declared=$DECLARED" >&2
test "$SHIPPED" = "$DECLARED"
echo "match"
```

```text,verify(script_name="version-match", stream=stdout)
match
```
