#!/usr/bin/env python3
"""Scrape a single femaledom.com megathread: raw dump, then one file per author."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BASE = "http://femaledom.com/forum/"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

DEFAULT_THREAD_ID = 4946
DEFAULT_THREAD_SLUG = "4946-Furry-Ballbusting"
POST_DELIM = "\n" + ("=" * 72) + "\n"


@dataclass
class ScrapedPost:
    post_id: int
    post_number: int
    thread_id: int
    page: int
    userid: int
    username: str
    date: str
    text: str
    word_count: int


class FemaledomSession:
    def __init__(self, delay: float = 0.85):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": UA})
        self.delay = delay
        self._securitytoken = "guest"

    def get(self, path: str) -> requests.Response:
        time.sleep(self.delay)
        url = urljoin(BASE, path)
        r = self.session.get(url, timeout=120)
        r.encoding = r.apparent_encoding or "iso-8859-1"
        r.raise_for_status()
        return r

    def login(self, username: str, password: str) -> None:
        r = self.get("index.php")
        soup = BeautifulSoup(r.text, "html.parser")
        session_s = soup.find("input", {"name": "s"})["value"]
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
        time.sleep(self.delay)
        r = self.session.post(
            urljoin(BASE, "login.php?do=login"),
            data=data,
            timeout=120,
            headers={"Referer": urljoin(BASE, "index.php")},
        )
        r.encoding = r.apparent_encoding or "iso-8859-1"
        if "Thank you for logging in" not in r.text:
            r2 = self.get("index.php")
            if "Thank you for logging in" not in r2.text and "Welcome back, <strong>" not in r2.text:
                raise RuntimeError("Login failed")

    @staticmethod
    def page_count_from_soup(soup: BeautifulSoup) -> int:
        label = soup.select_one(".pagination span a.popupctrl")
        if not label:
            return 1
        m = re.search(r"Page\s+1\s+of\s+(\d+)", label.get_text(" ", strip=True))
        return int(m.group(1)) if m else 1


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


def parse_posts(soup: BeautifulSoup, thread_id: int, page: int) -> list[ScrapedPost]:
    out: list[ScrapedPost] = []
    for li in soup.select("li[id^=post_]"):
        m = re.search(r"post_(\d+)", li.get("id", ""))
        if not m:
            continue
        post_id = int(m.group(1))
        user_a = li.select_one(".username_container a.username")
        if not user_a:
            continue
        username = user_a.get_text(strip=True)
        href = user_a.get("href", "")
        uid_m = re.search(r"member\.php\?(\d+)", href)
        userid = int(uid_m.group(1)) if uid_m else -1
        postdate = li.select_one(".postdate")
        date_s = postdate.get_text(" ", strip=True) if postdate else ""
        num = 1
        counter = li.select_one("a.postcounter")
        if counter:
            nm = re.search(r"#(\d+)", counter.get_text())
            if nm:
                num = int(nm.group(1))
        msg = li.select_one(f"div#post_message_{post_id}")
        inner = msg.decode_contents() if msg else ""
        text = html_to_text(inner)
        out.append(
            ScrapedPost(
                post_id=post_id,
                post_number=num,
                thread_id=thread_id,
                page=page,
                userid=userid,
                username=username,
                date=date_s,
                text=text,
                word_count=word_count(text),
            )
        )
    return out


def thread_title_from_soup(soup: BeautifulSoup) -> str:
    t = soup.select_one("span.threadtitle, h1, .pagetitle h2")
    if t:
        return t.get_text(strip=True)
    title_tag = soup.find("title")
    return title_tag.get_text(strip=True) if title_tag else "Unknown thread"


def safe_username_filename(username: str, userid: int) -> str:
    """Unique filesystem name; collisions get userid suffix."""
    base = re.sub(r"[^\w.-]+", "_", username, flags=re.UNICODE).strip("_")
    if not base:
        base = f"user_{userid}"
    return f"{base}__uid{userid}.md"


def scrape_raw(
    client: FemaledomSession,
    thread_slug: str,
    thread_id: int,
    out_raw: Path,
    *,
    start_page: int = 1,
) -> tuple[str, int, list[ScrapedPost]]:
    path_base = thread_slug
    first = client.get(f"showthread.php?{path_base}")
    soup = BeautifulSoup(first.text, "html.parser")
    title = thread_title_from_soup(soup)
    total_pages = client.page_count_from_soup(soup)

    all_posts: list[ScrapedPost] = []
    seen_ids: set[int] = set()

    # Resume: load existing post ids if mid-run restart
    jsonl_path = out_raw / "posts.jsonl"
    if start_page > 1 and jsonl_path.is_file():
        with jsonl_path.open(encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    row = json.loads(line)
                    seen_ids.add(row["post_id"])

    for page in range(start_page, total_pages + 1):
        if page == 1:
            psoup, rtext = soup, first.text
        else:
            r = client.get(f"showthread.php?{path_base}/page{page}")
            psoup = BeautifulSoup(r.text, "html.parser")
        batch = parse_posts(psoup, thread_id, page)
        new = [p for p in batch if p.post_id not in seen_ids]
        for p in new:
            seen_ids.add(p.post_id)
        all_posts.extend(new)
        with jsonl_path.open("a", encoding="utf-8") as f:
            for p in new:
                f.write(json.dumps(asdict(p), ensure_ascii=False) + "\n")
        meta = {
            "thread_id": thread_id,
            "title": title,
            "slug": thread_slug,
            "total_pages": total_pages,
            "last_completed_page": page,
            "posts_collected": len(seen_ids),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        (out_raw / "checkpoint.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
        print(f"Page {page}/{total_pages}: +{len(new)} posts (total {len(seen_ids)})", flush=True)

    meta_final = {
        "thread_id": thread_id,
        "title": title,
        "slug": thread_slug,
        "url": urljoin(BASE, f"showthread.php?{thread_slug}"),
        "total_pages": total_pages,
        "post_count": len(all_posts),
        "scraped_at": datetime.now(timezone.utc).isoformat(),
    }
    (out_raw / "thread_meta.json").write_text(json.dumps(meta_final, indent=2), encoding="utf-8")

    # Reload full set from jsonl for dedupe-safe collate
    posts: list[ScrapedPost] = []
    with jsonl_path.open(encoding="utf-8") as f:
        for line in f:
            if line.strip():
                posts.append(ScrapedPost(**json.loads(line)))
    posts.sort(key=lambda p: (p.post_number, p.post_id))
    return title, total_pages, posts


def collate_by_user(posts: list[ScrapedPost], out_users: Path, thread_title: str) -> int:
    out_users.mkdir(parents=True, exist_ok=True)
    by_key: dict[tuple[int, str], list[ScrapedPost]] = defaultdict(list)
    for p in posts:
        by_key[(p.userid, p.username)].append(p)

    for (userid, username), user_posts in sorted(by_key.items(), key=lambda x: x[0][1].lower()):
        user_posts.sort(key=lambda p: (p.post_number, p.post_id))
        path = out_users / safe_username_filename(username, userid)
        lines = [
            f"# {username}",
            "",
            f"Thread: {thread_title}",
            f"Posts: {len(user_posts)}",
            f"Exported: {datetime.now(timezone.utc).isoformat()}",
            "",
        ]
        for i, p in enumerate(user_posts):
            if i > 0:
                lines.append(POST_DELIM)
            lines.append(f"Post #{p.post_number} · {p.date} · post_id {p.post_id}")
            lines.append("")
            lines.append(p.text)
        path.write_text("\n".join(lines), encoding="utf-8")
    return len(by_key)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--thread-id", type=int, default=DEFAULT_THREAD_ID)
    parser.add_argument("--thread-slug", default=DEFAULT_THREAD_SLUG)
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(__file__).resolve().parents[2] / "archives" / "furry-femaledom",
    )
    parser.add_argument("--delay", type=float, default=0.85)
    parser.add_argument(
        "--collate-only",
        action="store_true",
        help="Skip crawl; rebuild by-user from raw/posts.jsonl",
    )
    parser.add_argument("--start-page", type=int, default=1)
    args = parser.parse_args()

    user = os.environ.get("FEMALEDOM_USER")
    password = os.environ.get("FEMALEDOM_PASS")
    out_raw = args.out / "raw"
    out_users = args.out / "by-user"
    args.out.mkdir(parents=True, exist_ok=True)
    out_raw.mkdir(parents=True, exist_ok=True)

    jsonl_path = out_raw / "posts.jsonl"
    if args.collate_only:
        if not jsonl_path.is_file():
            print("No raw/posts.jsonl", file=sys.stderr)
            return 1
        posts = [ScrapedPost(**json.loads(line)) for line in jsonl_path.open() if line.strip()]
        meta = json.loads((out_raw / "thread_meta.json").read_text(encoding="utf-8"))
        n = collate_by_user(posts, out_users, meta["title"])
        print(f"Collated {n} users → {out_users}")
        return 0

    if not user or not password:
        print("Set FEMALEDOM_USER and FEMALEDOM_PASS", file=sys.stderr)
        return 1

    if args.start_page == 1 and jsonl_path.is_file():
        jsonl_path.unlink()

    client = FemaledomSession(delay=args.delay)
    print("Logging in…", flush=True)
    client.login(user, password)
    print(f"Scraping thread {args.thread_id} ({args.thread_slug})…", flush=True)
    title, pages, posts = scrape_raw(
        client,
        args.thread_slug,
        args.thread_id,
        out_raw,
        start_page=args.start_page,
    )
    n_users = collate_by_user(posts, out_users, title)
    print(f"Done: {len(posts)} posts, {pages} pages, {n_users} users")
    print(f"Raw: {out_raw}")
    print(f"By user: {out_users}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())