#!/usr/bin/env python3
"""Archive narrative threads from femaledom.com Ballbusting World (forum 81).

Exports concatenated OP-only post bodies from one-thread-one-story threads.
Skips megathreads and meta/sticky threads.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BASE = "http://femaledom.com/forum/"
FORUM_URL = "forumdisplay.php?81-Ballbusting-World"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Meta / admin / megathread — not one-story-per-thread exports.
BLOCKED_THREAD_IDS = {
    2933,  # Read before posting
    2175,  # Admin notices
    6388,  # FDmovies promo
    3662,  # attachments FAQ
    1932,  # fantasies/stories megathread (~483 pages)
    2253,  # Ballbusting Cartoons megathread (~693 pages)
}

# After page 1: if total pages exceed this, treat as megathread (do not paginate).
MEGA_THREAD_MIN_PAGES = 120

STORY_TITLE_RE = re.compile(
    r"\b(stor(y|ies)|chapter|ch\.?\s*\d|part\s+\d|tale|fiction|fantasy)\b",
    re.I,
)


@dataclass
class PostSlice:
    post_id: int
    post_number: int
    date: str
    html: str
    text: str
    word_count: int


@dataclass
class ThreadRecord:
    thread_id: int
    title: str
    url: str
    starter_userid: int
    starter_username: str
    page_count: int
    op_posts: list[PostSlice]
    op_word_count: int
    exported: bool
    skip_reason: str | None


class FemaledomSession:
    def __init__(self, delay: float = 1.75):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": UA})
        self.delay = delay
        self._securitytoken = "guest"

    def _sleep(self) -> None:
        time.sleep(self.delay)

    def get(self, path: str, *, params: dict | None = None) -> requests.Response:
        url = urljoin(BASE, path)
        self._sleep()
        r = self.session.get(url, params=params, timeout=90)
        r.encoding = r.apparent_encoding or "iso-8859-1"
        r.raise_for_status()
        return r

    def login(self, username: str, password: str) -> None:
        r = self.get("index.php")
        soup = BeautifulSoup(r.text, "html.parser")
        hidden_s = soup.find("input", {"name": "s"})
        session_s = hidden_s["value"] if hidden_s else ""
        st = soup.find("input", {"name": "securitytoken"})
        if st and st.get("value"):
            self._securitytoken = st["value"]

        md5_pass = hashlib.md5(password.encode("utf-8")).hexdigest()
        data = {
            "vb_login_username": username,
            "vb_login_password": "",
            "vb_login_md5password": md5_pass,
            "vb_login_md5password_utf": md5_pass,
            "cookieuser": "1",
            "s": session_s,
            "securitytoken": self._securitytoken,
            "do": "login",
        }
        self._sleep()
        login_url = urljoin(BASE, "login.php?do=login")
        r = self.session.post(
            login_url,
            data=data,
            timeout=90,
            headers={"Referer": urljoin(BASE, "index.php")},
        )
        r.encoding = r.apparent_encoding or "iso-8859-1"
        if r.status_code not in (200, 302):
            raise RuntimeError(f"Login HTTP {r.status_code}")
        if "Thank you for logging in" not in r.text:
            r2 = self.get("index.php")
            if "Thank you for logging in" not in r2.text and "Welcome back, <strong>" not in r2.text:
                raise RuntimeError("Login failed — check credentials or site changes.")

    def page_count_from_soup(self, soup: BeautifulSoup) -> int:
        label = soup.select_one(".pagination span a.popupctrl")
        if not label:
            return 1
        m = re.search(r"Page\s+1\s+of\s+(\d+)", label.get_text(" ", strip=True))
        return int(m.group(1)) if m else 1


def slugify(title: str, thread_id: int, max_len: int = 80) -> str:
    t = title.strip().lower()
    t = re.sub(r"[^\w\s-]", "", t, flags=re.UNICODE)
    t = re.sub(r"[-\s]+", "_", t).strip("_")
    if not t:
        t = "untitled"
    t = t[:max_len].rstrip("_")
    return f"{thread_id}_{t}"


def html_to_text(fragment: str) -> str:
    soup = BeautifulSoup(fragment, "html.parser")
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for tag in soup.find_all(["script", "style"]):
        tag.decompose()
    text = soup.get_text("\n")
    text = unescape(text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def word_count(text: str) -> int:
    return len(re.findall(r"\b\w+\b", text))


def parse_posts(soup: BeautifulSoup) -> list[tuple[int, int, str, str, int, str]]:
    """Returns list of (post_id, userid, username, date, post_number, message_html)."""
    out: list[tuple[int, int, str, str, int, str]] = []
    for li in soup.select("li[id^=post_]"):
        post_id_m = re.search(r"post_(\d+)", li.get("id", ""))
        if not post_id_m:
            continue
        post_id = int(post_id_m.group(1))

        user_a = li.select_one(".username_container a.username")
        if not user_a:
            continue
        username = user_a.get_text(strip=True)
        href = user_a.get("href", "")
        uid_m = re.search(r"member\.php\?(\d+)", href)
        userid = int(uid_m.group(1)) if uid_m else -1

        postdate = li.select_one(".postdate")
        date_s = postdate.get_text(" ", strip=True) if postdate else ""

        counter = li.select_one("a.postcounter")
        num = 1
        if counter:
            num_m = re.search(r"#(\d+)", counter.get_text())
            if num_m:
                num = int(num_m.group(1))

        msg = li.select_one(f"div#post_message_{post_id}")
        html = str(msg) if msg else ""
        inner = msg.decode_contents() if msg else ""
        out.append((post_id, userid, username, date_s, num, inner))
    return out


def thread_urls_from_forum(soup: BeautifulSoup) -> list[tuple[int, str, str]]:
    """thread_id, title, relative showthread path (no session)."""
    seen: set[int] = set()
    items: list[tuple[int, str, str]] = []
    for a in soup.select("a[id^=thread_title_]"):
        tid_m = re.search(r"thread_title_(\d+)", a.get("id", ""))
        if not tid_m:
            continue
        tid = int(tid_m.group(1))
        if tid in seen:
            continue
        seen.add(tid)
        href = a.get("href", "")
        href = href.split("&s=")[0] if "&s=" in href else href
        title = a.get_text(strip=True)
        items.append((tid, title, href))
    return items


def is_story_thread(
    title: str,
    page_count: int,
    op_posts: list[PostSlice],
    *,
    stories_only: bool,
) -> tuple[bool, str | None]:
    if page_count >= MEGA_THREAD_MIN_PAGES:
        return False, f"megathread_{page_count}_pages"
    total_words = sum(p.word_count for p in op_posts)
    if not stories_only:
        return True, None
    if len(op_posts) >= 2 and total_words >= 400:
        return True, None
    if len(op_posts) == 1 and total_words >= 1200:
        return True, None
    if STORY_TITLE_RE.search(title) and total_words >= 200:
        return True, None
    if total_words < 200:
        return False, "too_short"
    return False, "not_story_heuristic"


def fetch_thread(
    client: FemaledomSession, showthread_href: str
) -> tuple[BeautifulSoup, int, list[tuple[int, int, str, str, int, str]]]:
    """Load all pages; return first-page soup, page_count, all parsed posts."""
    path = showthread_href.split("?")[-1] if "?" in showthread_href else showthread_href
    if path.startswith("showthread.php?"):
        path = path[len("showthread.php?") :]
    elif path.startswith("showthread.php"):
        path = path.replace("showthread.php", "").lstrip("?")

    first = client.get(f"showthread.php?{path}")
    soup = BeautifulSoup(first.text, "html.parser")
    pages = client.page_count_from_soup(soup)
    all_posts = parse_posts(soup)

    if pages > MEGA_THREAD_MIN_PAGES:
        return soup, pages, all_posts

    for page in range(2, pages + 1):
        # vB URLs: .../pageN or ...&page=N — try path form
        if "/page" in path or re.search(r"page\d", path):
            base = re.sub(r"/page\d+", "", path)
            page_path = f"{base}/page{page}"
        else:
            page_path = f"{path}/page{page}"
        r = client.get(f"showthread.php?{page_path}")
        psoup = BeautifulSoup(r.text, "html.parser")
        all_posts.extend(parse_posts(psoup))

    all_posts.sort(key=lambda x: x[4])
    return soup, pages, all_posts


def write_story_md(
    out_dir: Path,
    rec: ThreadRecord,
) -> Path:
    fname = slugify(rec.title, rec.thread_id) + ".md"
    path = out_dir / fname
    lines = [
        "---",
        f"title: {json.dumps(rec.title)}",
        f"author: {json.dumps(rec.starter_username)}",
        f"thread_id: {rec.thread_id}",
        f"source_url: {json.dumps(rec.url)}",
        f"op_word_count: {rec.op_word_count}",
        f"op_post_count: {len(rec.op_posts)}",
        f"archived_at: {datetime.now(timezone.utc).isoformat()}",
        "---",
        "",
        f"# {rec.title}",
        "",
        f"*Archived from femaledom.com — author: **{rec.starter_username}***",
        "",
    ]
    for i, p in enumerate(rec.op_posts):
        if len(rec.op_posts) > 1:
            lines.append(f"## Post #{p.post_number}")
            if p.date:
                lines.append(f"*{p.date}*")
            lines.append("")
        lines.append(p.text)
        if i < len(rec.op_posts) - 1:
            lines.append("")
            lines.append("---")
            lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Archive femaledom Ballbusting World OP stories")
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(__file__).resolve().parents[2] / "archives" / "femaledom-stories",
    )
    parser.add_argument("--stories-only", action="store_true", default=True)
    parser.add_argument("--all-threads", action="store_true", help="Export all non-blocked threads")
    parser.add_argument("--delay", type=float, default=1.0)
    parser.add_argument("--max-threads", type=int, default=0, help="0 = no limit (dry-run sample)")
    args = parser.parse_args()

    user = os.environ.get("FEMALEDOM_USER")
    password = os.environ.get("FEMALEDOM_PASS")
    if not user or not password:
        print("Set FEMALEDOM_USER and FEMALEDOM_PASS", file=sys.stderr)
        return 1

    stories_only = not args.all_threads
    out_dir = args.out
    out_dir.mkdir(parents=True, exist_ok=True)

    client = FemaledomSession(delay=args.delay)
    print("Logging in…", flush=True)
    client.login(user, password)
    print("Listing threads…", flush=True)

    thread_list: list[tuple[int, str, str]] = []
    page = 1
    while True:
        suffix = "" if page == 1 else f"/page{page}"
        r = client.get(f"{FORUM_URL}{suffix}")
        soup = BeautifulSoup(r.text, "html.parser")
        batch = thread_urls_from_forum(soup)
        if not batch and page > 1:
            break
        thread_list.extend(batch)
        pages = client.page_count_from_soup(soup)
        if page >= pages:
            break
        page += 1

    # Deduplicate by id
    by_id: dict[int, tuple[int, str, str]] = {}
    for tid, title, href in thread_list:
        by_id[tid] = (tid, title, href)
    threads = sorted(by_id.values(), key=lambda x: x[0])

    manifest: list[dict] = []
    exported = 0
    skipped = 0

    limit = args.max_threads if args.max_threads > 0 else len(threads)

    for tid, title, href in threads:
        if tid in BLOCKED_THREAD_IDS:
            rec = ThreadRecord(
                thread_id=tid,
                title=title,
                url=urljoin(BASE, href),
                starter_userid=0,
                starter_username="",
                page_count=0,
                op_posts=[],
                op_word_count=0,
                exported=False,
                skip_reason="blocked_list",
            )
            manifest.append(asdict(rec))
            skipped += 1
            continue

        if exported + skipped >= limit and args.max_threads:
            break

        try:
            print(f"Fetching {tid}: {title[:60]}…", flush=True)
            _, page_count, posts = fetch_thread(client, href)
            if page_count >= MEGA_THREAD_MIN_PAGES:
                rec = ThreadRecord(
                    thread_id=tid,
                    title=title,
                    url=urljoin(BASE, href),
                    starter_userid=0,
                    starter_username="",
                    page_count=page_count,
                    op_posts=[],
                    op_word_count=0,
                    exported=False,
                    skip_reason=f"megathread_{page_count}_pages",
                )
                manifest.append(asdict(rec))
                print(f"Skip {tid} — megathread ({page_count} pages): {title[:55]}", flush=True)
                skipped += 1
                continue
        except Exception as e:
            manifest.append(
                {
                    "thread_id": tid,
                    "title": title,
                    "error": str(e),
                    "exported": False,
                }
            )
            print(f"ERROR {tid}: {e}", file=sys.stderr)
            continue

        if not posts:
            skipped += 1
            manifest.append(
                {
                    "thread_id": tid,
                    "title": title,
                    "skip_reason": "no_posts",
                    "exported": False,
                }
            )
            continue

        starter_uid = posts[0][1]
        starter_name = posts[0][2]
        op_raw = [p for p in posts if p[1] == starter_uid]
        op_slices: list[PostSlice] = []
        for post_id, _uid, _un, date_s, num, html in op_raw:
            text = html_to_text(html)
            op_slices.append(
                PostSlice(
                    post_id=post_id,
                    post_number=num,
                    date=date_s,
                    html=html,
                    text=text,
                    word_count=word_count(text),
                )
            )

        op_words = sum(p.word_count for p in op_slices)
        ok, reason = is_story_thread(title, page_count, op_slices, stories_only=stories_only)

        rec = ThreadRecord(
            thread_id=tid,
            title=title,
            url=urljoin(BASE, href.split("&")[0]),
            starter_userid=starter_uid,
            starter_username=starter_name,
            page_count=page_count,
            op_posts=op_slices,
            op_word_count=op_words,
            exported=ok,
            skip_reason=reason,
        )

        if ok:
            path = write_story_md(out_dir, rec)
            print(f"Wrote {path.name} ({op_words} words, {len(op_slices)} OP posts)")
            exported += 1
        else:
            print(f"Skip {tid} — {reason} ({title[:50]}…)")
            skipped += 1

        manifest.append(
            {
                **{k: v for k, v in asdict(rec).items() if k != "op_posts"},
                "op_post_count": len(op_slices),
                "op_posts_meta": [
                    {"post_id": p.post_id, "post_number": p.post_number, "word_count": p.word_count}
                    for p in op_slices
                ],
            }
        )

    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "forum": FORUM_URL,
                "archived_at": datetime.now(timezone.utc).isoformat(),
                "exported_count": exported,
                "skipped_count": skipped,
                "threads": manifest,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\nDone: {exported} stories → {out_dir}")
    print(f"Manifest: {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())