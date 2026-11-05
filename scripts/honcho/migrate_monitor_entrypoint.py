#!/usr/bin/env python3
"""Move an existing monitor wrapper to an installed Hermes Ops CLI command."""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import stat
import subprocess
import tempfile


def migrate(hermes_home: Path, hermes_cli: Path, expected_sha256: str) -> Path:
    home = hermes_home.resolve(strict=True)
    cli = hermes_cli.absolute()
    if not cli.is_file() or not os.access(cli, os.X_OK):
        raise ValueError("the owning installation's Hermes CLI must be executable")
    target = home / "scripts" / "honcho_monitor.py"
    if target.is_symlink():
        raise ValueError("refusing to replace a symlinked monitor entry point")
    original = target.read_bytes()
    digest = hashlib.sha256(original).hexdigest()
    if digest != expected_sha256:
        raise ValueError("monitor entry point changed; inspect it before migrating")
    subprocess.run(
        [str(cli), "honcho-ops", "monitor", "--help"],
        env=dict(os.environ, HERMES_HOME=str(home)),
        check=True, capture_output=True, text=True, timeout=60,
    )
    wrapper = (
        "#!/usr/bin/env python3\n"
        '"""Honcho monitor cron entry point owned by Hermes Ops."""\n'
        "import os\nimport sys\n\n"
        f"os.execv({str(cli)!r}, [{str(cli)!r}, 'honcho-ops', 'monitor', *sys.argv[1:]])\n"
    ).encode()
    backup = target.with_name(f"{target.name}.before-honcho-ops-{digest[:12]}")
    if backup.exists():
        if backup.read_bytes() != original:
            raise ValueError("the existing recovery copy differs from the original")
    else:
        with backup.open("xb") as stream:
            stream.write(original)
        backup.chmod(stat.S_IMODE(target.stat().st_mode))
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".honcho-ops-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(wrapper)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(stat.S_IMODE(target.stat().st_mode))
        if target.is_symlink() or target.read_bytes() != original:
            raise ValueError("monitor entry point changed during migration")
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return backup


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes-home", type=Path, required=True)
    parser.add_argument("--hermes-cli", type=Path, required=True)
    parser.add_argument("--expected-sha256", required=True)
    args = parser.parse_args()
    backup = migrate(args.hermes_home, args.hermes_cli, args.expected_sha256)
    print(f"Monitor entry point migrated; original preserved at {backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
