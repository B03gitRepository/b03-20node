"""Run a bounded online update or verify its replay in a fresh process."""
from pathlib import Path
import sys

if __name__ == "__main__":
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
    from packet_training import main
    main()
