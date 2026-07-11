"""Top-level plugin entry point — re-exports register() from hermes_sandbox."""
try:
    from .hermes_sandbox import register  # plugin loader: __package__ = hermes_plugins.sandbox
except ImportError:
    from hermes_sandbox import register  # standalone: pytest, dev import  # noqa: F401
