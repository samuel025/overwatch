"""Break the demo system in a known way, or fix it.

  python inject_fault.py list               # all faults with category and how they surface
  python inject_fault.py show <fault>       # ground truth for one fault
  python inject_fault.py <fault>            # break it (records a deploy/config change when the fault ships one)
  python inject_fault.py reset              # undo everything
"""

from __future__ import annotations

import sys

from faults import FAULTS, reset


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd = argv[0]
    if cmd == "list":
        print(f"{'fault':<20} {'category':<15} {'surfaces via':<12} {'past ticket':<12} title")
        for f in FAULTS.values():
            print(f"{f.name:<20} {f.category:<15} {f.trigger:<12} {f.past_ticket or '-':<12} {f.title}")
        return 0
    if cmd == "show":
        f = FAULTS[argv[1]]
        print(f"{f.name}: {f.title}\n  truth: {f.truth}\n  surfaces via: {f.trigger}"
              + (f"\n  human report: {f.report}" if f.report else "")
              + (f"\n  why it's interesting: {f.note}" if f.note else ""))
        return 0
    if cmd == "reset":
        reset()
        print("reset: done")
        return 0
    if cmd not in FAULTS:
        print(f"unknown fault '{cmd}'. Try: python inject_fault.py list")
        return 2
    f = FAULTS[cmd]
    f.apply()
    print(f"{f.name}: injected ({f.title}). Signal should be clear in ~{f.settle_seconds}s.")
    if f.report:
        print(f'Surfaces via a human report: overwatch investigate "{f.report}"')
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
