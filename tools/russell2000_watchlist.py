"""Russell 2000 watchlist - same as `py tools\\index_watchlist.py russell2000 ...` (kept for old instructions).

    py tools\\russell2000_watchlist.py --top 300 --min-price 10
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from index_watchlist import main  # noqa: E402

if __name__ == "__main__":
    main(["russell2000"] + sys.argv[1:])
