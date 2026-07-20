# BuildXpress Deal Finder

Private daily fixer-upper deal scanner for the SFV + LA. Scrapes active listings every morning, flags fixer signals (keywords, $/sqft below area median, long days-on-market), scores every deal 0–100, suggests below-asking cash offers on stale listings, and serves it all on a password-protected dashboard at **deals.buildxpressdepot.us**.

Tuned for your financing: **$150–180K down @ 20% hard money → ~$750–900K purchase power** (stretch to $1.05M on negotiable listings).

---

## How it works

```
GitHub Actions (daily 7AM PT cron)
   └─ scraper/scrape.py  →  pulls listings via HomeHarvest (Realtor.com data)
        └─ filters + scores + suggests offers
             └─ writes docs/data.json  →  auto-commit
                  └─ GitHub Pages serves docs/ at deals.buildxpressdepot.us
```

No server to maintain. GitHub runs the scraper and hosts the site — free.

## Setup (one time, ~15 minutes)

### 1. Create the GitHub repo
1. Go to github.com → **New repository** → name it `deal-finder`, set it **Private**... 
   ⚠️ **Important:** GitHub Pages on private repos requires GitHub Pro ($4/mo). On a free account, make the repo **Public** — the dashboard is still password-gated and `noindex`ed, and the code contains nothing sensitive.
2. On your PC (Git installed), from this folder:
   ```
   git init
   git add .
   git commit -m "initial"
   git branch -M main
   git remote add origin https://github.com/YOUR_USERNAME/deal-finder.git
   git push -u origin main
   ```

### 2. Enable the daily scraper
- Repo → **Actions** tab → enable workflows if prompted.
- Click **Daily Deal Scan** → **Run workflow** to do your first scan now (takes ~3–5 min).
- It then runs automatically every day at 7 AM Pacific.

### 3. Enable the website
- Repo → **Settings → Pages** → Source: **Deploy from a branch** → Branch: `main`, folder: `/docs` → Save.
- Under **Custom domain**, enter `deals.buildxpressdepot.us` → Save → tick **Enforce HTTPS** (after DNS below propagates).

### 4. Point your domain (DreamHost)
- DreamHost panel → **Domains → Manage Domains → buildxpressdepot.us → DNS**.
- Add record: Type **CNAME**, Name **deals**, Value **YOUR_USERNAME.github.io** (no https://).
- Wait 10–60 min for DNS, then visit https://deals.buildxpressdepot.us

### 5. Set your password
- Default password is `password`. Change it: open `docs/hash.html` in a browser, type your new password, copy the hash, paste it into `docs/index.html` replacing the `PASSWORD_HASH` value. Commit + push.

## Daily use
- Open the dashboard → sort by **Deal score**.
- 🔨 FIXER badge = fixer language / deep $/sqft discount detected.
- 💰 45+ DOM = seller fatigue → dashboard shows a **suggested cash offer** (up to $100K / 10% below asking, scaling with DOM) and the 20% down needed at that price.
- **Price cut** tag = tracked drop since the scraper first saw the listing.

## Tuning
Everything lives in `config.yaml` — add/remove areas (each needs `name`, `location`, `tier`), edit keywords, change budget or discount rules. Push the change; next run uses it.

## Honest notes
- Data comes via [HomeHarvest](https://github.com/Bunsly/HomeHarvest), an open-source library reading public Realtor.com search endpoints. Running once daily at this volume is low-risk, but it's not an official API — if it ever breaks, the fix is usually `pip install -U homeharvest` (bump the version in `requirements.txt`).
- The password gate is client-side — good enough to keep the public and search engines out, not bank-grade security. Don't store anything sensitive in the repo.
- Scores/suggested offers are heuristics from listing data only. Always run your own comps, ARV, and rehab estimate before offering. Not financial advice.
