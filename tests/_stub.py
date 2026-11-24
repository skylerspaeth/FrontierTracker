"""Shared test setup: make the project importable and stub curl_cffi if it isn't installed."""
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
if "curl_cffi" not in sys.modules:
    try:
        import curl_cffi  # noqa: F401
    except ImportError:
        stub = types.ModuleType("curl_cffi")
        stub.requests = types.SimpleNamespace(Session=object)
        sys.modules["curl_cffi"] = stub
