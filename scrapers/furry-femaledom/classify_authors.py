#!/usr/bin/env python3
"""Split furry megathread by-user exports into authors vs commentors."""

from __future__ import annotations

import json
import re
import shutil
from collections import defaultdict
from pathlib import Path

ARCHIVE = Path(__file__).resolve().parents[2] / "archives" / "furry-femaledom"

COMMENT_RE = re.compile(
    r"^\s*("
    r"thanks?(\s+you)?|thx|ty|"
    r"great\s+(story|work|post|img|pic)s?|"
    r"nice\s+(story|work|one|post|pics?)?|"
    r"awesome|amazing|hot|wow|cool|"
    r"more\s+please|please\s+(post\s+)?more|keep\s+(it\s+)?(up|going)|"
    r"love\s+(it|this|your)|well\s+done|good\s+(job|work)|"
    r"lol|lmao|hehe|haha|"
    r"nice!+|yes!+|"
    r"can\s+you\s+post|do\s+you\s+have|anyone\s+have|"
    r"bump"
    r")\b",
    re.I | re.DOTALL,
)


def word_count(text: str) -> int:
    return len(re.findall(r"\b\w+\b", text))


def is_comment_like(text: str, words: int) -> bool:
    t = text.strip()
    if words < 20:
        return True
    if words < 60 and COMMENT_RE.match(t):
        return True
    if words < 45 and COMMENT_RE.search(t[:120]):
        return True
    if len(re.findall(r"https?://", text)) >= 2 and words < 100:
        return True
    return False


def is_narrative(text: str, words: int) -> bool:
    if is_comment_like(text, words):
        return False
    if words >= 250:
        return True
    if words < 75:
        return False
    if '"' in text or "\u201c" in text:
        if text.count('"') + text.count("\u201c") >= 2:
            return True
    if re.search(r"\b(chapter|part\s+\d|continued|to\s+be\s+continued|tbc)\b", text, re.I):
        return True
    if len(re.findall(r"\b(he|she|they)\s+\w+ed\b", text, re.I)) >= 2 and words >= 90:
        return True
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if words >= 120 and len(lines) >= 4:
        return True
    return False


def classify_user(posts: list[dict]) -> tuple[bool, str, dict]:
    posts = sorted(posts, key=lambda p: (p["post_number"], p["post_id"]))
    narr = sum(1 for p in posts if is_narrative(p["text"], word_count(p["text"])))
    total_w = sum(word_count(p["text"]) for p in posts)
    max_w = max(word_count(p["text"]) for p in posts) if posts else 0

    if narr == 0:
        return False, "no_narrative_posts", {"narr": 0, "posts": len(posts), "total_w": total_w, "max_w": max_w}

    reasons: list[str] = []
    if narr >= 2:
        reasons.append(f"{narr}_narrative_posts")
    if max_w >= 180:
        reasons.append(f"max_words_{max_w}")
    if total_w >= 400:
        reasons.append(f"total_words_{total_w}")
    if narr >= 1 and max_w >= 350:
        reasons.append("single_long_chapter")

    is_author = narr >= 2 or max_w >= 180 or (narr >= 1 and total_w >= 400) or (narr >= 1 and max_w >= 350)
    if not is_author:
        return False, "only_replies_or_short", {"narr": narr, "posts": len(posts), "total_w": total_w, "max_w": max_w}
    return True, ";".join(reasons), {"narr": narr, "posts": len(posts), "total_w": total_w, "max_w": max_w}


def main() -> None:
    raw_path = ARCHIVE / "raw" / "posts.jsonl"
    by_user_dir = ARCHIVE / "by-user"
    out_authors = ARCHIVE / "authors"
    out_commentors = ARCHIVE / "commentors"

    by_uid: dict[int, list[dict]] = defaultdict(list)
    with raw_path.open(encoding="utf-8") as f:
        for line in f:
            if line.strip():
                p = json.loads(line)
                by_uid[p["userid"]].append(p)

    uid_to_file = {
        int(m.group(1)): fp
        for fp in by_user_dir.glob("*.md")
        if (m := re.search(r"__uid(\d+)\.md$", fp.name))
    }

    for d in (out_authors, out_commentors):
        d.mkdir(exist_ok=True)
        for f in d.glob("*.md"):
            f.unlink()

    manifest: list[dict] = []
    authors = commentors = 0

    for uid in sorted(by_uid, key=lambda u: by_uid[u][0]["username"].lower()):
        posts = by_uid[uid]
        username = posts[0]["username"]
        src = uid_to_file.get(uid)
        if not src:
            continue
        is_author, reason, meta = classify_user(posts)
        dest = out_authors if is_author else out_commentors
        shutil.copy2(src, dest / src.name)
        role = "author" if is_author else "commentor"
        if is_author:
            authors += 1
        else:
            commentors += 1
        manifest.append(
            {"userid": uid, "username": username, "file": src.name, "role": role, "reason": reason, **meta}
        )

    (ARCHIVE / "classification.json").write_text(
        json.dumps({"authors": authors, "commentors": commentors, "users": manifest}, indent=2),
        encoding="utf-8",
    )
    print(f"authors={authors} commentors={commentors} → {ARCHIVE}")


if __name__ == "__main__":
    main()