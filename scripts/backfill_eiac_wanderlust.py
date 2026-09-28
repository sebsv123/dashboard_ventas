"""Entrada CLI; no se ejecuta al importar."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tooling.eiac_wanderlust_backfill import main

if __name__ == "__main__":
    main()
