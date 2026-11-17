#!/usr/bin/env node
/**
 * Archive opening posts from KITG "BB Stories (Fiction)" forum threads to archives/kitg/*.md
 *
 * Usage:
 *   KITG_EMAIL=... KITG_PASSWORD=... node scrapers/kitg/scrape-kitg-bb-stories.mjs
 *   node tools/scrapers/kitg/scrape-kitg-bb-stories.mjs --limit=5   # dry run sample
 */

import { mkdir, writeFile, readFile, access } from "node:fs/promises";
import { constants } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import * as cheerio from "cheerio";
import TurndownService from "turndown";
import { chromium } from "playwright";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(__dirname, "../..");
const OUT_DIR = path.join(ROOT, "archives", "kitg");
const MANIFEST_PATH = path.join(OUT_DIR, "manifest.json");
const ERRORS_PATH = path.join(OUT_DIR, "errors.log");

const BASE = "https://kickedinthegroin.ning.com";
const GROUP_URL = "bb-storytellers";
const FORUM_INDEX = `${BASE}/group/${GROUP_URL}/forum`;
const FORUM_INDEX_PAGED = `${BASE}/groups/group/forum?groupUrl=${GROUP_URL}`;

const DELAY_MS = 1200;

function parseArgs(argv) {
  let limit = Infinity;
  let force = false;
  for (const arg of argv) {
    if (arg.startsWith("--limit=")) limit = Number(arg.slice(8)) || Infinity;
    if (arg === "--force") force = true;
  }
  return { limit, force };
}

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

function makeTurndown() {
  const td = new TurndownService({
    headingStyle: "atx",
    bulletListMarker: "-",
    emDelimiter: "*",
    strongDelimiter: "**",
    hr: "---",
    codeBlockStyle: "fenced",
    linkStyle: "inlined",
  });
  td.addRule("images", {
    filter: "img",
    replacement(_content, node) {
      const src = node.getAttribute("src") || "";
      const alt = (node.getAttribute("alt") || "").replace(/\]/g, "");
      if (!src) return "";
      return `\n\n![${alt}](${src})\n\n`;
    },
  });
  td.escape = (s) => s;
  return td;
}

const turndown = makeTurndown();

function slugFromTopicUrl(href) {
  try {
    const u = new URL(href, BASE);
    const m = u.pathname.match(/\/forum\/topics\/([^/?#]+)/);
    return m ? m[1] : null;
  } catch {
    return null;
  }
}

function yamlEscape(value) {
  const s = String(value ?? "");
  if (/[:#\n"'\\]/.test(s) || s.startsWith(" ") || s.endsWith(" ")) {
    return `"${s.replace(/\\/g, "\\\\").replace(/"/g, '\\"')}"`;
  }
  return s;
}

function htmlPostToMarkdown(html) {
  const $ = cheerio.load(html, { decodeEntities: false });
  $("script, style, noscript").remove();
  return turndown.turndown($.html()).replace(/\n{3,}/g, "\n\n").trim();
}

const SITE_H1_RE = /^Kicked in the Groin\b/i;

function topicTitleFromPage($) {
  for (const el of $("h1").toArray()) {
    const t = $(el).text().trim();
    if (t && !SITE_H1_RE.test(t)) return t;
  }
  return $("title")
    .text()
    .replace(/\s*-\s*Kicked in the Groin.*$/i, "")
    .trim();
}

function extractTopicMeta($) {
  const title = topicTitleFromPage($);

  let author = "";
  let posted = "";
  const byline = $("h1")
    .first()
    .parent()
    .find("li, p, .tb")
    .text();
  const postedMatch = byline.match(
    /Posted by\s+(.+?)\s+on\s+(.+?)(?:\s+in\s+|$)/i,
  );
  if (postedMatch) {
    author = postedMatch[1].trim();
    posted = postedMatch[2].trim();
  } else {
    const alt = $("body")
      .text()
      .match(/Posted by\s+(.+?)\s+on\s+(.+?)\s+in\s+BB Stories/i);
    if (alt) {
      author = alt[1].trim();
      posted = alt[2].trim();
    }
  }

  return { title, author, posted };
}

function extractOpeningPostHtml($) {
  const canvas = $("#xg_canvas");
  if (!canvas.length) return null;

  const candidates = canvas.find(".description").toArray();
  for (const el of candidates) {
    const $el = $(el);
    if ($el.closest("[id*='Comment']").length) continue;
    const html = $el.html();
    if (html && $el.text().trim().length > 0) return html;
  }

  const fallback = canvas.find(".xg_user_generated").first();
  if (fallback.length && !fallback.closest("[id*='Comment']").length) {
    return fallback.html();
  }
  return null;
}

async function fileExists(p) {
  try {
    await access(p, constants.F_OK);
    return true;
  } catch {
    return false;
  }
}

async function loadManifest() {
  try {
    const raw = await readFile(MANIFEST_PATH, "utf8");
    return JSON.parse(raw);
  } catch {
    return { threads: [], scrapedAt: null };
  }
}

async function saveManifest(manifest) {
  manifest.scrapedAt = new Date().toISOString();
  await writeFile(MANIFEST_PATH, JSON.stringify(manifest, null, 2) + "\n");
}

async function login(page, email, password) {
  const signIn = `${BASE}/main/authorization/signIn?target=${encodeURIComponent(
    `${BASE}/groups/group/show?groupUrl=${GROUP_URL}`,
  )}`;
  await page.goto(signIn, { waitUntil: "domcontentloaded", timeout: 120_000 });
  await page.getByRole("textbox", { name: "Email Address" }).fill(email);
  await page.getByRole("textbox", { name: "Password" }).fill(password);
  await page.getByRole("button", { name: "Sign In" }).click();
  await page.waitForURL(/kickedinthegroin\.ning\.com\/(groups|group)/, {
    timeout: 60_000,
  });
}

async function waitForForumIndex(page) {
  await page.waitForFunction(
    () => {
      const h1s = [...document.querySelectorAll("h1")].map(
        (h) => h.textContent || "",
      );
      return h1s.some((t) => /BB Stories.*Discussions\s*\(\d+\)/i.test(t));
    },
    { timeout: 60_000 },
  );
  await page
    .waitForSelector('a[href*="groupUrl=bb-storytellers"][href*="page="]', {
      timeout: 15_000,
    })
    .catch(() => {});
  await sleep(800);
}

async function getMaxForumPage(page) {
  const stats = await page.evaluate(() => {
    const h1s = [...document.querySelectorAll("h1")].map((h) => h.textContent || "");
    const countLine = h1s.find((t) => /Discussions\s*\(\d+\)/i.test(t)) || "";
    const countMatch = countLine.match(/Discussions\s*\((\d+)\)/i);
    let max = 1;
    for (const a of document.querySelectorAll('a[href*="page="]')) {
      const m = String(a.href).match(/[?&]page=(\d+)/);
      if (m) max = Math.max(max, Number(m[1]));
    }
    for (const a of document.querySelectorAll("a")) {
      const t = (a.textContent || "").trim();
      if (/^\d+$/.test(t)) max = Math.max(max, Number(t));
    }
    return {
      maxPage: max,
      discussionCount: countMatch ? Number(countMatch[1]) : null,
    };
  });
  return stats;
}

async function collectIndexThreads(page) {
  await page.goto(FORUM_INDEX, {
    waitUntil: "domcontentloaded",
    timeout: 120_000,
  });
  await waitForForumIndex(page);
  const { maxPage, discussionCount } = await getMaxForumPage(page);
  console.log(`Forum index: ~${discussionCount ?? "?"} threads, ${maxPage} pages`);

  const byHref = new Map();

  for (let pageNum = 1; pageNum <= maxPage; pageNum++) {
    const url =
      pageNum === 1 ? FORUM_INDEX : `${FORUM_INDEX_PAGED}&page=${pageNum}`;
    if (pageNum > 1) {
      await page.goto(url, { waitUntil: "domcontentloaded", timeout: 120_000 });
      await waitForForumIndex(page);
    }
    await sleep(400);

    const html = await page.content();
    const $ = cheerio.load(html);
    $('h3 a[href*="forum/topics/"]').each((_i, el) => {
      const href = $(el).attr("href");
      if (!href) return;
      const full = new URL(href, BASE).href.split("#")[0];
      const slug = slugFromTopicUrl(full);
      if (!slug) return;
      const title = $(el).text().trim();
      if (!byHref.has(full)) {
        byHref.set(full, { title, url: full, slug });
      }
    });
    console.log(`  index page ${pageNum}/${maxPage} → ${byHref.size} unique threads`);
  }

  return { threads: [...byHref.values()], discussionCount };
}

async function scrapeTopic(page, thread, { force }) {
  const outPath = path.join(OUT_DIR, `${thread.slug}.md`);
  if (!force && (await fileExists(outPath))) {
    const existing = await readFile(outPath, "utf8");
    if (existing.length > 200) {
      return { status: "skipped", path: outPath };
    }
  }

  await page.goto(thread.url, {
    waitUntil: "domcontentloaded",
    timeout: 120_000,
  });
  await sleep(DELAY_MS);

  const html = await page.content();
  const $ = cheerio.load(html);
  const meta = extractTopicMeta($);
  const title = meta.title || thread.title;
  const postHtml = extractOpeningPostHtml($);

  if (!postHtml) {
    return { status: "error", error: "no opening post body found" };
  }

  const body = htmlPostToMarkdown(postHtml);
  const frontmatter = [
    "---",
    `title: ${yamlEscape(title)}`,
    `author: ${yamlEscape(meta.author)}`,
    `posted: ${yamlEscape(meta.posted)}`,
    `source: ${yamlEscape(thread.url)}`,
    `slug: ${yamlEscape(thread.slug)}`,
    "---",
    "",
  ].join("\n");

  const file = frontmatter + body + "\n";
  await writeFile(outPath, file, "utf8");
  return { status: "ok", path: outPath, bytes: file.length };
}

async function main() {
  const { limit, force } = parseArgs(process.argv.slice(2));
  const email = process.env.KITG_EMAIL;
  const password = process.env.KITG_PASSWORD;

  if (!email || !password) {
    console.error("Set KITG_EMAIL and KITG_PASSWORD in the environment.");
    process.exit(1);
  }

  await mkdir(OUT_DIR, { recursive: true });
  const manifest = await loadManifest();

  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({
    userAgent:
      "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
  });
  const page = await context.newPage();

  try {
    console.log("Signing in…");
    await login(page, email, password);

    console.log("Collecting thread index…");
    const { threads, discussionCount } = await collectIndexThreads(page);
    const slugUses = new Map();
    for (const t of threads) {
      const n = (slugUses.get(t.slug) || 0) + 1;
      slugUses.set(t.slug, n);
      if (n > 1) t.slug = `${t.slug}__${n}`;
    }

    manifest.group = GROUP_URL;
    manifest.expectedCount = discussionCount;
    manifest.indexCount = threads.length;
    manifest.threads = threads.map((t) => ({
      slug: t.slug,
      title: t.title,
      url: t.url,
    }));

    const toScrape = threads.slice(0, limit);
    console.log(`Scraping ${toScrape.length} threads…`);

    let ok = 0;
    let skipped = 0;
    let err = 0;

    for (let i = 0; i < toScrape.length; i++) {
      const thread = toScrape[i];
      process.stdout.write(
        `[${i + 1}/${toScrape.length}] ${thread.slug} … `,
      );
      try {
        const result = await scrapeTopic(page, thread, { force });
        if (result.status === "ok") {
          ok++;
          console.log(`ok (${result.bytes} bytes)`);
        } else if (result.status === "skipped") {
          skipped++;
          console.log("skipped (exists)");
        } else {
          err++;
          console.log(`error: ${result.error}`);
          await writeFile(
            ERRORS_PATH,
            `${new Date().toISOString()}\t${thread.url}\t${result.error}\n`,
            { flag: "a" },
          );
        }
        thread.scrapeStatus = result.status;
      } catch (e) {
        err++;
        const msg = e instanceof Error ? e.message : String(e);
        console.log(`error: ${msg}`);
        await writeFile(
          ERRORS_PATH,
          `${new Date().toISOString()}\t${thread.url}\t${msg}\n`,
          { flag: "a" },
        );
        thread.scrapeStatus = "error";
      }
    }

    manifest.summary = { ok, skipped, err, attempted: toScrape.length };
    await saveManifest(manifest);

    console.log(
      `\nDone. ok=${ok} skipped=${skipped} err=${err} → ${OUT_DIR}`,
    );
  } finally {
    await browser.close();
  }
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});