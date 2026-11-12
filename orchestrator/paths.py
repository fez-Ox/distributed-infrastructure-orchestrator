from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ANSIBLE_DIR = REPO_ROOT / "ansible"


def inventory_path() -> Path:
    """Prefer the operator's real inventory; fall back to the committed example.

    `ansible/inventory.ini` is gitignored because it holds real addresses.
    The example keeps a fresh clone testable and gives new users a starting point.
    """
    real = ANSIBLE_DIR / "inventory.ini"
    if real.exists():
        return real
    return ANSIBLE_DIR / "inventory.example.ini"


ANSIBLE_INVENTORY = inventory_path()
