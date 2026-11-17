# forum-archives

Mirrors and scrapers for ballbusting forum fiction (preservation / offline reading).

## Layout

- **`archives/`** — scraped output (markdown, JSON manifests, raw dumps)
- **`scrapers/`** — tools that produced each archive

| Archive | Scraper |
|---------|---------|
| `archives/femaledom-stories/` | `scrapers/femaledom-stories/` (Python) |
| `archives/furry-femaledom/` | `scrapers/furry-femaledom/` |
| `archives/kitg/` | `scrapers/kitg/` (Node + Playwright) |

## Quick start

**Python** (femaledom / furry):

```bash
python3 -m venv scrapers/femaledom-stories/.venv
scrapers/femaledom-stories/.venv/bin/pip install -r scrapers/femaledom-stories/requirements.txt
export FEMALEDOM_USER=… FEMALEDOM_PASS=…
scrapers/femaledom-stories/.venv/bin/python scrapers/femaledom-stories/scrape.py
```

**KITG:**

```bash
npm install
export KITG_EMAIL=… KITG_PASSWORD=…
npm run scrape:kitg
```

Credentials stay in the environment — never commit them.