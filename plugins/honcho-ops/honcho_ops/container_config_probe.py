"""Run inside a deployed image: reconstruct route fields with isolated inputs.

Never imports settings against ambient credentials or file sources. No HTTP or
provider client is constructed. Output includes no arbitrary configuration.
"""

import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
from urllib.parse import urlsplit, urlunsplit


def probe():
    source = Path("/app/src/config.py")
    if any(Path(p).exists() for p in ("/app/config.toml", "/app/.env")):
        return {"status": "unknown", "reason": "file_sources_present"}
    prefixes = (
        "EMBEDDING_",
        "DERIVER_MODEL_CONFIG",
        "DREAM_",
        "SUMMARY_MODEL_CONFIG",
        "DIALECTIC_LEVELS",
    )
    fixture = {
        k: v
        for k, v in os.environ.items()
        if k.startswith(prefixes)
        and not any(s in k.upper() for s in ("API_KEY", "TOKEN", "SECRET", "PASSWORD"))
    }

    def without_credentials(value):
        if isinstance(value, dict):
            return {
                k: without_credentials(v)
                for k, v in value.items()
                if not any(
                    t in k.lower()
                    for t in ("api_key", "token", "secret", "password", "authorization")
                )
            }
        if isinstance(value, list):
            return [without_credentials(v) for v in value]
        return value

    for k, v in list(fixture.items()):
        if v.startswith(("{", "[")):
            try:
                fixture[k] = json.dumps(without_credentials(json.loads(v)))
            except ValueError:
                pass
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    # The child has an empty credential environment and a private, empty cwd.
    # The original selected environment is used as parser fixture input only.
    os.environ.clear()
    os.environ.update(fixture)
    os.environ["HONCHO_CONFIG_TOML_DISABLED"] = "1"

    def no_network(*args, **kwargs):
        raise RuntimeError("configuration_probe_network_forbidden")

    socket.socket.connect = no_network
    socket.create_connection = no_network
    sys.path.insert(0, "/app")

    def select(value):
        if isinstance(value, dict):
            return {
                k: select(v)
                for k, v in value.items()
                if k.lower()
                in {
                    "model",
                    "transport",
                    "base_url",
                    "overrides",
                    "model_config",
                    "levels",
                    "minimal",
                    "low",
                    "medium",
                    "high",
                    "max",
                    "deduction_model_config",
                    "induction_model_config",
                    "fallback",
                    "vector_dimensions",
                    "dimensions_mode",
                }
            }
        if isinstance(value, list):
            return [select(v) for v in value]
        return value

    with tempfile.TemporaryDirectory(prefix="honcho-route-fixture-") as d:
        os.chdir(d)
        from src.config import settings

        data = settings.model_dump(mode="json")
        routes = {
            k: select(data[k])
            for k in ("EMBEDDING", "DERIVER", "DREAM", "SUMMARY", "DIALECTIC")
        }

    def scrub(value):
        if isinstance(value, dict):
            for k, v in value.items():
                if k.lower() == "base_url" and isinstance(v, str):
                    u = urlsplit(v)
                    value[k] = urlunsplit(
                        (
                            u.scheme,
                            (u.hostname or "") + (":" + str(u.port) if u.port else ""),
                            u.path,
                            "",
                            "",
                        )
                    )
                else:
                    scrub(v)
        elif isinstance(value, list):
            for v in value:
                scrub(v)

    scrub(routes)
    return {
        "status": "reconstructed",
        "source_sha256": digest,
        "pydantic_settings": importlib.metadata.version("pydantic-settings"),
        "routes": routes,
    }


if __name__ == "__main__":
    try:
        print(json.dumps(probe(), sort_keys=True))
    except Exception as exc:
        print(json.dumps({"status": "unknown", "reason": type(exc).__name__}))
        raise SystemExit(1)
