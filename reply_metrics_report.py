"""Summarize live daily JSONL or future replay results, without business IO."""
import argparse
import json
from pathlib import Path

from application.reply_metrics import aggregate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="+", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    def rows():
        for path in args.inputs:
            with path.open(encoding="utf-8-sig") as stream:
                for line in stream:
                    if line.strip():
                        yield json.loads(line)

    report = json.dumps(aggregate(rows()), ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(report, encoding="utf-8")
    print(report)


if __name__ == "__main__":
    main()
