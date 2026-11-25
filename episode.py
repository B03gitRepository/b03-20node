"""Public entry for sampling a B03 work order or evaluating a sealed run."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from episode_contract import evaluate, main, sample

if __name__ == "__main__":
    main()
