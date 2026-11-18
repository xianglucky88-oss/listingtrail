import argparse
import json
import sys

from . import pipeline
from . import store as _store


def main(argv=None):
    parser = argparse.ArgumentParser(description="listingtrail offline pipeline")
    parser.add_argument(
        "--pages",
        metavar="PATH",
        help="run an offline page-sequence fixture instead of the default input.json",
    )
    parser.add_argument("--db", metavar="PATH", help="sqlite database path")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="delete existing rows before running (offline runs / tests)",
    )
    parser.add_argument(
        "--detailed",
        action="store_true",
        help="include per-record status / error classification in the output",
    )
    args = parser.parse_args(argv)

    con = _store.connect(args.db)
    try:
        if args.reset:
            _store.reset(con)
        if args.pages:
            from .loader import load

            result = pipeline.run_records(load(args.pages), con=con)
        elif args.detailed:
            result = pipeline.run_detailed(con)
        else:
            result = pipeline.run(con)
    finally:
        con.close()

    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
