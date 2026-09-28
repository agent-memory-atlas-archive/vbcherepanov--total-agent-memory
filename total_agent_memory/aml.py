import sys
from pathlib import Path


def main():
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
    from aml_adapter.cli import main as run
    sys.exit(run())
