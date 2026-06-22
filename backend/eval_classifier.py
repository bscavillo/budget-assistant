"""Spotcheck / re-categorise tool for the transaction categorizer.

Dev tool that runs the live cleanup + classification pipeline from
``ollama_service`` over the real transactions in ``budget.db`` and prints a
report that makes miscategorisations easy to eyeball. By default it is
read-only; ``--apply`` persists the result.

Usage (from the ``backend`` directory)::

    PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe eval_classifier.py
    PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe eval_classifier.py --compare
    PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe eval_classifier.py --limit 60
    PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe eval_classifier.py --apply

``--compare`` diffs the new category against the one currently stored, so a
re-run's changes are reviewable first. ``--limit N`` classifies only the N most
recent expenses for a fast iteration loop. ``--apply`` clears every expense's AI
category and writes the freshly computed ones back — the one-time re-categorise
after a classifier change (normal imports classify themselves, so this is only
needed to refresh existing history).
"""

import argparse
import sqlite3
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

import ollama_service as svc

DB_PATH = svc.database.DB_PATH


def _load_expenses(limit):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        sql = ("SELECT id, description, category, std_category, amount "
               "FROM transactions WHERE type = 'expense' "
               "ORDER BY date DESC, id DESC")
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [dict(r) for r in conn.execute(sql)]


def _group_reps(rows):
    """Collapse rows to one representative per dedup key, as the service does."""
    groups = {}
    for tx in rows:
        clean = svc._clean_transaction(tx)
        key = svc._dedup_key(clean) or f"\0{tx['id']}"
        groups.setdefault(key, (clean, []))[1].append(tx)
    return list(groups.values())


def _classify_reps(reps):
    """Return ``[(clean, members, category, merchant), ...]`` for every rep."""
    out = []
    with ThreadPoolExecutor(max_workers=svc._MAX_CONCURRENCY) as pool:
        futures = {pool.submit(svc._classify_one, clean): (clean, members)
                   for clean, members in reps}
        for fut in futures:
            clean, members = futures[fut]
            result = fut.result()
            category, merchant = result if result else ("<FAILED>", None)
            out.append((clean, members, category, merchant))
    return out


def _report(results):
    by_cat = defaultdict(list)
    total = 0
    for clean, members, category, merchant in results:
        by_cat[category].append((merchant, clean, len(members)))
        total += len(members)

    print(f"\n=== {len(results)} unique payees, {total} transactions ===\n")
    for category in sorted(by_cat, key=lambda c: -sum(n for _, _, n in by_cat[c])):
        entries = by_cat[category]
        rows = sum(n for _, _, n in entries)
        flag = "  <-- review" if category in ("Other", "<FAILED>") else ""
        print(f"## {category}  ({len(entries)} payees, {rows} tx){flag}")
        for merchant, clean, n in sorted(entries, key=lambda e: -e[2]):
            loc = f" [{clean['city']}/{clean['country']}]".replace("/]", "]")
            loc = loc if clean["city"] or clean["country"] else ""
            print(f"   {n:>4}x  {merchant!r:<32}{loc}  <= {clean['text'][:70]!r}")
        print()


def _compare(results):
    """Show only payees whose new category differs from the stored one."""
    print("\n=== changes vs currently stored std_category ===\n")
    changes = 0
    for clean, members, category, _merchant in results:
        old = members[0].get("std_category")
        if old != category:
            changes += 1
            n = len(members)
            print(f"   {n:>4}x  {old!r:>14} -> {category!r:<14}  "
                  f"{clean['text'][:60]!r}")
    print(f"\n{changes} of {len(results)} payees change category.\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0,
                    help="classify only the N most recent expenses")
    ap.add_argument("--compare", action="store_true",
                    help="diff new categories against the stored ones")
    ap.add_argument("--apply", action="store_true",
                    help="persist the computed categories (re-categorise for real)")
    args = ap.parse_args()

    rows = _load_expenses(args.limit)
    reps = _group_reps(rows)
    print(f"Classifying {len(reps)} unique payees from {len(rows)} expenses "
          f"using model {svc.MODEL!r} ...")
    results = _classify_reps(reps)
    _report(results)
    if args.compare:
        _compare(results)
    if args.apply:
        _apply(results)


def _apply(results):
    """Clear existing AI categories and persist the freshly computed ones."""
    cleared = svc.database.clear_classifications()
    updates = [(member["id"], category, merchant)
               for _clean, members, category, merchant in results
               for member in members
               if category != "<FAILED>"]
    svc.database.set_classifications(updates)
    print(f"\nApplied: cleared {cleared} expenses, wrote {len(updates)} "
          f"classifications.\n")


if __name__ == "__main__":
    main()
