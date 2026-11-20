"""Start one packet simulation or verify the evidence from a previously completed run."""
from pathlib import Path
import sys

if __name__ == "__main__":
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
    from cli import main
    main()
