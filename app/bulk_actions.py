"""Bulk operations on duplicate groups.

Run via:
    docker compose --profile tools run --rm bulk_actions [SUBCOMMAND] [OPTIONS]

Subcommands:
    preview    Show what would be deleted given the filters. Read-only.
    auto       Mark and execute every unreviewed group matching the filters.
    auto-exact Back-compat alias for `auto --match exact`.

Filter options (apply to both preview and auto):
    --media {video,audio}              restrict to one media type
    --match {exact,perceptual,chromaprint}
                                       restrict to a match type (repeatable)
    --threshold N                      only groups with similarity >= N

Examples:
    # Preview everything that would be deleted
    docker compose --profile tools run --rm bulk_actions preview

    # Preview only video perceptual matches at >=0.95 similarity
    docker compose --profile tools run --rm bulk_actions preview \\
        --media video --match perceptual --threshold 0.95

    # Delete all exact dupes (interactive confirm)
    docker compose --profile tools run --rm bulk_actions auto --match exact

    # Delete every >=0.97 similarity group (any media, any match type)
    docker compose --profile tools run --rm bulk_actions auto --threshold 0.97

    # Same but non-interactive
    docker compose --profile tools run --rm bulk_actions auto \\
        --threshold 0.97 --yes

    # Dry run -- shows plan without changing anything
    docker compose --profile tools run --rm bulk_actions auto \\
        --threshold 0.95 --dry-run
"""
from __future__ import annotations

import argparse
import logging
import sys

from . import actions as actions_mod
from .db import ensure_schema

log = logging.getLogger("bulk_actions")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def _humanize_bytes(b: int) -> str:
    f = float(b)
    for u in ["B", "KB", "MB", "GB", "TB"]:
        if f < 1024 or u == "TB":
            return f"{f:.1f} {u}" if u != "B" else f"{int(f)} B"
        f /= 1024
    return str(b)


def _summary_print(summary: dict) -> tuple[int, int, int]:
    """Print a per-(media,match) preview table. Returns (groups, files, bytes)."""
    if not summary:
        return 0, 0, 0
    total_g = total_f = total_b = 0
    print(f"{'Media':<8} {'Match':<12} {'Groups':>8} {'Files':>10} {'Reclaimable':>14}")
    print("-" * 56)
    for (mt, match), row in sorted(summary.items()):
        g = int(row["groups"] or 0)
        f = int(row["files_to_delete"] or 0)
        b = int(row["bytes_to_free"] or 0)
        total_g += g; total_f += f; total_b += b
        print(f"{mt:<8} {match:<12} {g:>8} {f:>10} {_humanize_bytes(b):>14}")
    print("-" * 56)
    print(f"{'TOTAL':<8} {'':<12} {total_g:>8} {total_f:>10} {_humanize_bytes(total_b):>14}")
    return total_g, total_f, total_b


def cmd_preview(args: argparse.Namespace) -> int:
    summary = actions_mod.preview_groups(
        match_types=args.match_types,
        media_type=args.media,
        min_similarity=args.threshold,
    )
    if not summary:
        print("No unreviewed groups match those filters.")
        return 0
    _summary_print(summary)
    return 0


def cmd_auto(args: argparse.Namespace) -> int:
    summary = actions_mod.preview_groups(
        match_types=args.match_types,
        media_type=args.media,
        min_similarity=args.threshold,
    )
    if not summary:
        print("Nothing to do: no unreviewed groups match those filters.")
        return 0

    total_g, total_f, total_b = _summary_print(summary)
    print()

    scope_bits = []
    if args.match_types:
        scope_bits.append(f"match_types={','.join(args.match_types)}")
    if args.media:
        scope_bits.append(f"media={args.media}")
    if args.threshold is not None:
        scope_bits.append(f"similarity≥{args.threshold:.2f}")
    scope = ", ".join(scope_bits) or "all unreviewed groups"

    print(f"About to process {total_g} groups ({scope}).")
    print(f"  → Files to move to .mediadedup-trash: {total_f}")
    print(f"  → Reclaimable space:                  {_humanize_bytes(total_b)}")
    print()

    if args.dry_run:
        print("--dry-run set; no changes made.")
        return 0

    if not args.yes:
        try:
            answer = input("Proceed? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.")
            return 1
        if answer not in ("y", "yes"):
            print("Aborted.")
            return 1

    n_marked = actions_mod.auto_mark_groups(
        match_types=args.match_types,
        media_type=args.media,
        min_similarity=args.threshold,
    )
    log.info("Marked %d groups for execution", n_marked)

    result = actions_mod.execute_groups(
        match_types=args.match_types,
        media_type=args.media,
        min_similarity=args.threshold,
    )
    print()
    print("=" * 56)
    print("Done.")
    print(f"  Groups processed:   {result.groups_processed}")
    print(f"  Files moved:        {result.files_deleted}")
    print(f"  Files failed:       {result.files_failed}")
    print(f"  Space reclaimed:    {_humanize_bytes(result.bytes_freed)}")
    if result.errors:
        print(f"  First few errors:")
        for err in result.errors[:5]:
            print(f"    - {err}")
    print()
    print("Files were MOVED, not deleted. Inspect <mount>/.mediadedup-trash/<date>/")
    print("and `rm -rf` it manually when satisfied.")
    return 0 if result.files_failed == 0 else 2


# Back-compat: cmd_auto_exact still exists, just delegates to cmd_auto.
def cmd_auto_exact(args: argparse.Namespace) -> int:
    args.match_types = ["exact"]
    args.threshold = None
    return cmd_auto(args)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="bulk_actions",
        description="Bulk operations on duplicate groups.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    # Common option helpers --------------------------------------------------
    def add_common_filters(p):
        p.add_argument("--media", choices=("video", "audio"),
                       help="restrict to one media type")
        p.add_argument("--match", dest="match_types", action="append",
                       choices=("exact", "perceptual", "chromaprint"),
                       help="restrict to a match type (repeat for multiple)")
        p.add_argument("--threshold", type=float, metavar="0.95",
                       help="only groups with similarity >= this value (0..1)")

    # preview ----------------------------------------------------------------
    p_prev = sub.add_parser("preview", help="show what auto-delete would do")
    add_common_filters(p_prev)
    p_prev.set_defaults(func=cmd_preview)

    # auto -- the new general command ---------------------------------------
    p_auto = sub.add_parser("auto",
                            help="mark + execute every unreviewed group "
                                 "matching the filters")
    add_common_filters(p_auto)
    p_auto.add_argument("--yes", "-y", action="store_true",
                        help="skip the interactive confirmation prompt")
    p_auto.add_argument("--dry-run", action="store_true",
                        help="print the plan and exit without making changes")
    p_auto.set_defaults(func=cmd_auto)

    # auto-exact -- back-compat alias ---------------------------------------
    p_exact = sub.add_parser("auto-exact",
                             help="alias for `auto --match exact` "
                                  "(kept for back-compat)")
    p_exact.add_argument("--media", choices=("video", "audio"),
                         help="restrict to one media type")
    p_exact.add_argument("--yes", "-y", action="store_true",
                         help="skip the interactive confirmation prompt")
    p_exact.add_argument("--dry-run", action="store_true",
                         help="print the plan and exit without making changes")
    p_exact.set_defaults(func=cmd_auto_exact)

    args = parser.parse_args(argv)
    ensure_schema()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
