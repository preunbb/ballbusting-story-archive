#!/usr/bin/env python3
"""Extract story-only posts for selected authors into archives/furry-femaledom/stories-only/."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ARCHIVE = Path(__file__).resolve().parents[2] / "archives" / "furry-femaledom"
RAW = ARCHIVE / "raw" / "posts.jsonl"
OUT = ARCHIVE / "stories-only"

POST_DELIM = "\n" + ("=" * 72) + "\n"

# username -> output filename stem
DEFAULT_AUTHORS = {
    "poiu": 7654,
    "OneBadMamaJama": 21607,
    "SagaDC": 12478,
}

COMMENT_OPENERS = re.compile(
    r"^\s*("
    r"thanks?(\s+you)?|thx|"
    r"great\s+story|nice\s+story|awesome\s+story|love\s+(it|this)|"
    r"lol|lmao|wow|cool|hot|nice!+|"
    r"originally posted by"
    r")",
    re.I,
)

META_ONLY = re.compile(
    r"^\s*("
    r"sweet\s*[-—]|"
    r"not sure if (this|that)|"
    r"i'?ve been (busy|working|sitting)|"
    r"quick\s+update|"
    r"as an aside|"
    r"just (finished|posted|uploaded)|"
    r"plugging away|"
    r"having given it"
    r")",
    re.I,
)


def word_count(text: str) -> int:
    return len(re.findall(r"\b\w+\b", text))


def strip_leading_quote_thread(text: str) -> str:
    """Drop vBulletin 'Originally Posted by…' quote blocks at the top."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "Originally Posted by":
        return text
    i = 1
    # skip username line; quote until blank line + non-quote content
    if i < len(lines):
        i += 1
    while i < len(lines):
        if lines[i].strip() == "":
            rest = "\n".join(lines[i + 1 :]).strip()
            return rest if rest else text
        i += 1
    return text


def is_narrative_body(text: str, words: int) -> bool:
    if words < 90:
        return False
    if '"' in text or "\u201c" in text:
        if text.count('"') + text.count("\u201c") >= 2:
            return True
    if re.search(r"\b(chapter|part\s+\d|continued|tbc)\b", text, re.I):
        return True
    if re.search(r"\b(she|he|they)\s+\w+ed\b", text, re.I):
        if len(re.findall(r"\b(she|he|they)\s+\w+ed\b", text, re.I)) >= 2:
            return True
    sents = len(re.findall(r"[.!?](\s|$)", text))
    if words >= 120 and sents >= 3:
        return True
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return words >= 200 and len(lines) >= 5


def is_story_post(text: str) -> bool:
    raw_words = word_count(text)
    if raw_words < 100:
        return False

    body = strip_leading_quote_thread(text.strip())
    words = word_count(body)
    if words < 100:
        return False

    if COMMENT_OPENERS.match(body) and words < 220:
        return False

    urls = len(re.findall(r"https?://", body))
    if urls >= 1:
        has_fiction = bool(
            re.search(r"\b(she|he|they)\s+\w+ed\b", body, re.I)
            and (body.count('"') + body.count("\u201c") >= 2 or re.search(r"\b(said|asked|replied|whispered)\b", body, re.I))
        )
        if not has_fiction and words < 600:
            return False
    if urls >= 3 and words < 800:
        return False

    # Long forum/meta posts (FA updates, thanks for comments)
    if words < 600 and META_ONLY.match(body) and not (words >= 300 and is_narrative_body(body, words)):
        return False

    # Story chapters — primary signal
    if words >= 220 and is_narrative_body(body, words):
        return True

    if words >= 500:
        return is_narrative_body(body, words) or bool(re.search(r"\b(said|asked|replied)\b", body, re.I))

    return False


def safe_filename(username: str, userid: int) -> str:
    base = re.sub(r"[^\w.-]+", "_", username).strip("_") or f"user_{userid}"
    return f"{base}__uid{userid}.md"


def main(argv: list[str]) -> int:
    targets = dict(DEFAULT_AUTHORS)
    if len(argv) > 1:
        # optional extra usernames from CLI
        pass

    posts_by_uid: dict[int, list[dict]] = {uid: [] for uid in targets.values()}
    uname_for_uid = {v: k for k, v in targets.items()}

    with RAW.open(encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            p = json.loads(line)
            if p["userid"] in posts_by_uid:
                posts_by_uid[p["userid"]].append(p)

    OUT.mkdir(parents=True, exist_ok=True)
    index: dict[str, dict] = {}

    for username, uid in targets.items():
        posts = sorted(posts_by_uid[uid], key=lambda p: (p["post_number"], p["post_id"]))
        story_posts = [p for p in posts if is_story_post(p["text"])]
        out_path = OUT / safe_filename(username, uid)
        chunks: list[str] = []
        for i, p in enumerate(story_posts):
            if i > 0:
                chunks.append(POST_DELIM)
            chunks.append(f"Post #{p['post_number']} · {p['date']} · post_id {p['post_id']}")
            chunks.append("")
            chunks.append(p["text"].strip())
        out_path.write_text("\n".join(chunks) + ("\n" if chunks else ""), encoding="utf-8")
        index[username] = {
            "file": out_path.name,
            "total_posts": len(posts),
            "story_posts": len(story_posts),
            "story_post_numbers": [p["post_number"] for p in story_posts],
        }
        print(f"{username}: {len(story_posts)}/{len(posts)} story posts → {out_path.name}")

    (OUT / "index.json").write_text(json.dumps(index, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))