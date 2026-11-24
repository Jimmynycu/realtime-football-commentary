"""Build the evaluation set for prompt tuning from the 7B's accepted rewrites. Why: those drafts are real match lines and
each already has a verified rewrite, so candidates can be compared on drafts that really occur.

The match is replayed through booth.run with a cache-only Writer, so no model is needed. Rewrites equal to their draft
(the model failed, the draft stayed) and prep-sheet notes (said verbatim, never re-voiced) are dropped.
Split is by md5 of the draft, so it is stable when the cache grows and a draft can never sit in both files.

    python tuning/build_dataset.py            build train.jsonl / val.jsonl (both are evaluation drafts)
    python tuning/build_dataset.py --check    PASS/FAIL lines on the files on disk
"""
import hashlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import booth  # noqa: E402
import colour  # noqa: E402
import engine  # noqa: E402
import game  # noqa: E402

DATA = HERE / "data"
TRAIN, VAL = DATA / "train.jsonl", DATA / "val.jsonl"


class Recording(colour.Writer):
    """Cache only (live=False); remembers every call so the triples can be rebuilt without a model."""

    def __init__(self, m):
        super().__init__(m, live=False)
        self.calls = []

    def write(self, draft, situation, variant=0):
        out = super().write(draft, situation, variant)
        self.calls.append((situation, draft, out))
        return out


def notes() -> set:
    return {n["text"] for n in json.loads((engine.ROOT / "prep_notes.json").read_text(encoding="utf-8"))["notes"]}


def example(situation, draft, voiced) -> dict:
    return {"messages": [{"role": "system", "content": colour.SYSTEM},
                         {"role": "user", "content": f"Situation: {situation}\nDraft: {draft}"},
                         {"role": "assistant", "content": voiced}]}


def is_val(draft: str) -> bool:
    return int(hashlib.md5(draft.encode()).hexdigest(), 16) % 100 < 15


def build() -> None:
    m, frames = game.load()
    w = Recording(m)
    booth.run(m, frames, booth.load_params(), w)
    skip, seen, train, val = notes(), set(), [], []
    for situation, draft, voiced in w.calls:
        if voiced == draft or draft in skip or draft in seen:
            continue
        seen.add(draft)
        (val if is_val(draft) else train).append(example(situation, draft, voiced))
    DATA.mkdir(exist_ok=True)
    for path, rows in ((TRAIN, train), (VAL, val)):
        path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    print(f"write calls {len(w.calls)}, unique drafts kept {len(seen)}: train {len(train)}, val {len(val)}")


def load(path: Path) -> list:
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()] if path.exists() else []


def check() -> int:
    tr, va, skip = load(TRAIN), load(VAL), notes()
    drafts = lambda rows: {r["messages"][1]["content"].split("\nDraft: ", 1)[1] for r in rows}  # noqa: E731
    rows = tr + va
    tests = [("val drafts not in train", not drafts(tr) & drafts(va)),
             ("no voiced == draft", all(r["messages"][2]["content"] != r["messages"][1]["content"].split("\nDraft: ", 1)[1] for r in rows)),
             ("no verbatim note", not (drafts(rows) & skip)),
             ("exactly three messages", all([x["role"] for x in r["messages"]] == ["system", "user", "assistant"] for r in rows)),
             ("files not empty", bool(tr) and bool(va))]
    for name, ok in tests:
        print(("PASS " if ok else "FAIL ") + name)
    return 0 if all(ok for _, ok in tests) else 1


if __name__ == "__main__":
    sys.exit(check()) if "--check" in sys.argv else build()
