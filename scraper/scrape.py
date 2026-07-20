#!/usr/bin/env python3
"""
BuildXpress Deal Finder - daily multi-source scraper
Sources: Realtor.com (via HomeHarvest, primary), Redfin (Stingray API),
Zillow (best-effort; often bot-blocked from cloud IPs - degrades gracefully).
Dedupes across sources, detects fixer signals, scores deals, writes docs/data.json.

Run:  python scraper/scrape.py
"""

import json
import math
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests
import yaml

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config.yaml"
OUT_PATH = ROOT / "docs" / "data.json"
HISTORY_PATH = ROOT / "docs" / "history.json"

BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "application/json, text/plain, */*",
}


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def safe_num(v):
    try:
        if v is None:
            return None
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return None
        return f
    except (TypeError, ValueError):
        return None


def first_col(row, *names):
    for n in names:
        if n in row and row[n] is not None and str(row[n]) not in ("", "nan", "None"):
            return row[n]
    return None


def keyword_hits(text, cfg):
    if not text:
        return [], []
    t = str(text).lower()
    strong = [k for k in cfg["keywords"]["strong"] if k.lower() in t]
    weak = [k for k in cfg["keywords"]["weak"] if k.lower() in t]
    return strong, weak


def norm_addr(addr):
    """Normalize an address for cross-source dedupe."""
    a = re.sub(r"[^a-z0-9 ]", "", str(addr).lower())
    a = re.sub(r"\b(street|st|avenue|ave|drive|dr|road|rd|boulevard|blvd|lane|ln|place|pl|court|ct|way)\b", "", a)
    return re.sub(r"\s+", " ", a).strip()


# Address tokens that mean an attached / individually-deeded unit (condo, etc.)
UNIT_RE = re.compile(r"(?:\bunit\b|\bapt\b|\bste\b|\bspc\b|\bspace\b|#\s*\w)", re.I)


def is_attached(rec, cfg):
    """True if this listing is a condo/co-op/townhome/mobile — not a flip."""
    excl = [x.upper() for x in cfg.get("exclude_types", [])]
    pt = str(rec.get("ptype", "")).upper()
    if any(x in pt for x in excl):
        return True
    if cfg.get("exclude_units", True) and UNIT_RE.search(rec.get("address", "")):
        return True
    hoa = rec.get("hoa")
    hoa_max = cfg.get("hoa_max", 0)
    if hoa and hoa_max and hoa > hoa_max:
        return True
    return False


# ----------------------------------------------------------------------
# Source 1: Realtor.com via HomeHarvest (primary - has descriptions)
# ----------------------------------------------------------------------
def fetch_realtor(area, cfg):
    from homeharvest import scrape_property
    out = []
    try:
        df = scrape_property(
            location=area["location"],
            listing_type="for_sale",
            past_days=cfg.get("past_days", 120),
        )
    except Exception as e:
        print(f"  [{area['name']}] realtor FAILED: {e}", file=sys.stderr)
        return out

    for r in df.to_dict(orient="records"):
        price = safe_num(first_col(r, "list_price", "price"))
        if not price or price < 100000:
            continue
        style = str(first_col(r, "style", "property_type") or "").upper()
        allowed = cfg.get("property_types", [])
        if allowed and style and not any(a in style for a in allowed):
            continue
        hoa = safe_num(first_col(r, "hoa_fee", "hoa"))
        sqft = safe_num(first_col(r, "sqft", "square_feet"))
        ppsf = safe_num(first_col(r, "price_per_sqft"))
        if ppsf is None and sqft:
            ppsf = round(price / sqft)
        addr = first_col(r, "full_street_line", "street", "address") or ""
        city = first_col(r, "city") or ""
        zipc = str(first_col(r, "zip_code", "zip") or "")
        dom = safe_num(first_col(r, "days_on_mls", "days_on_market", "dom"))
        out.append({
            "source": "realtor",
            "address": f"{addr}, {city} {zipc}".strip(", "),
            "street": str(addr),
            "zip": zipc,
            "price": int(price),
            "beds": safe_num(first_col(r, "beds", "bedrooms")),
            "baths": safe_num(first_col(r, "full_baths", "baths", "bathrooms")),
            "sqft": int(sqft) if sqft else None,
            "lot_sqft": safe_num(first_col(r, "lot_sqft", "lot_size")),
            "year_built": safe_num(first_col(r, "year_built")),
            "ppsf": int(ppsf) if ppsf else None,
            "dom": int(dom) if dom is not None else None,
            "list_date": str(first_col(r, "list_date") or ""),
            "url": first_col(r, "property_url", "url") or "",
            "photo": first_col(r, "primary_photo", "photo") or "",
            "mls": str(first_col(r, "mls_id", "mls") or ""),
            "status": str(first_col(r, "status") or ""),
            "ptype": style,
            "hoa": hoa,
            "description": str(first_col(r, "text", "description") or "")[:600],
        })
    print(f"  [{area['name']}] realtor: {len(out)}")
    return out


# ----------------------------------------------------------------------
# Source 2: Redfin (unofficial Stingray API - no descriptions, good DOM)
# ----------------------------------------------------------------------
def _redfin_json(url, params):
    r = requests.get(url, params=params, headers=BROWSER_HEADERS, timeout=30)
    r.raise_for_status()
    return json.loads(r.text.replace("{}&&", ""))


def fetch_redfin(area, cfg):
    out = []
    try:
        data = _redfin_json(
            "https://www.redfin.com/stingray/do/location-autocomplete",
            {"location": area["location"], "v": 2},
        )
        rows = data["payload"]["sections"][0]["rows"]
        rid_raw = rows[0]["id"]                 # e.g. "6_25417"
        rtype, rid = rid_raw.split("_", 1)

        data = _redfin_json(
            "https://www.redfin.com/stingray/api/gis",
            {
                # uipt 1=house 2=condo 3=townhouse 4=multi 5=land 6=other
                # -> keep houses, multi and land only (no condo/townhouse)
                "al": 1, "region_id": rid, "region_type": rtype,
                "status": 9, "uipt": "1,4,5", "sf": "1,2,3,5,6,7",
                "num_homes": 400, "v": 8,
            },
        )
        homes = data.get("payload", {}).get("homes", [])
    except Exception as e:
        print(f"  [{area['name']}] redfin FAILED: {e}", file=sys.stderr)
        return out

    def val(h, key):
        v = h.get(key)
        if isinstance(v, dict):
            return v.get("value")
        return v

    RF_TYPES = {1: "SINGLE_FAMILY", 2: "CONDO", 3: "TOWNHOUSE",
                4: "MULTI_FAMILY", 5: "LAND", 6: "OTHER", 13: "CO-OP"}
    for h in homes:
        price = safe_num(val(h, "price"))
        if not price or price < 100000:
            continue
        try:
            rf_ptype = RF_TYPES.get(int(val(h, "propertyType") or 0), "")
        except (TypeError, ValueError):
            rf_ptype = ""
        sqft = safe_num(val(h, "sqFt"))
        street = str(val(h, "streetLine") or "")
        city = h.get("city") or ""
        zipc = str(h.get("zip") or "")
        rel_url = h.get("url") or ""
        out.append({
            "source": "redfin",
            "address": f"{street}, {city} {zipc}".strip(", "),
            "street": street,
            "zip": zipc,
            "price": int(price),
            "beds": safe_num(h.get("beds")),
            "baths": safe_num(h.get("baths")),
            "sqft": int(sqft) if sqft else None,
            "lot_sqft": safe_num(val(h, "lotSize")),
            "year_built": safe_num(val(h, "yearBuilt")),
            "ppsf": int(price / sqft) if sqft else None,
            "dom": int(safe_num(val(h, "dom")) or 0) or None,
            "list_date": "",
            "url": ("https://www.redfin.com" + rel_url) if rel_url.startswith("/") else rel_url,
            "photo": "",
            "mls": str(val(h, "mlsId") or ""),
            "status": "FOR_SALE",
            "ptype": rf_ptype,
            "hoa": safe_num(val(h, "hoa")),
            "description": "",
        })
    print(f"  [{area['name']}] redfin: {len(out)}")
    return out


# ----------------------------------------------------------------------
# Source 3: Zillow (best effort - frequently bot-blocked from cloud IPs)
# ----------------------------------------------------------------------
def fetch_zillow(area, cfg):
    out = []
    body = {
        "searchQueryState": {
            "usersSearchTerm": area["location"],
            "isMapVisible": False,
            "isListVisible": True,
            "filterState": {"sortSelection": {"value": "days"}},
        },
        "wants": {"cat1": ["listResults"]},
        "requestId": 2,
    }
    headers = dict(BROWSER_HEADERS)
    headers["Content-Type"] = "application/json"
    headers["Referer"] = "https://www.zillow.com/"
    try:
        r = requests.put(
            "https://www.zillow.com/async-create-search-page-state",
            json=body, headers=headers, timeout=30,
        )
        r.raise_for_status()
        results = r.json().get("cat1", {}).get("searchResults", {}).get("listResults", [])
    except Exception as e:
        print(f"  [{area['name']}] zillow blocked/failed (expected sometimes): {e}", file=sys.stderr)
        return out

    for h in results:
        price = safe_num(h.get("unformattedPrice"))
        if not price or price < 100000:
            continue
        sqft = safe_num(h.get("area"))
        info = (h.get("hdpData") or {}).get("homeInfo", {})
        street = str(h.get("addressStreet") or "")
        durl = h.get("detailUrl") or ""
        out.append({
            "source": "zillow",
            "address": f"{street}, {h.get('addressCity','')} {h.get('addressZipcode','')}".strip(", "),
            "street": street,
            "zip": str(h.get("addressZipcode") or ""),
            "price": int(price),
            "beds": safe_num(h.get("beds")),
            "baths": safe_num(h.get("baths")),
            "sqft": int(sqft) if sqft else None,
            "lot_sqft": safe_num(info.get("lotAreaValue")),
            "year_built": safe_num(info.get("yearBuilt")),
            "ppsf": int(price / sqft) if sqft else None,
            "dom": int(safe_num(info.get("daysOnZillow")) or 0) or None,
            "list_date": "",
            "url": ("https://www.zillow.com" + durl) if durl.startswith("/") else durl,
            "photo": h.get("imgSrc") or "",
            "mls": "",
            "status": "FOR_SALE",
            "ptype": str(info.get("homeType") or "").upper(),
            "hoa": safe_num(info.get("hoa")),
            "description": str(h.get("flexFieldText") or ""),
        })
    print(f"  [{area['name']}] zillow: {len(out)}")
    return out


# ----------------------------------------------------------------------
# Merge, score, publish
# ----------------------------------------------------------------------
def merge_sources(listings):
    """Dedupe by normalized street address; prefer the record with a
    description (realtor), but keep the union of source names."""
    merged = {}
    for l in listings:
        key = norm_addr(l["street"]) + "|" + l["zip"]
        if key in merged:
            keep = merged[key]
            keep["sources"] = sorted(set(keep["sources"] + [l["source"]]))
            # fill gaps
            for f in ("description", "photo", "dom", "sqft", "ppsf", "year_built", "lot_sqft"):
                if not keep.get(f) and l.get(f):
                    keep[f] = l[f]
            # keep the LOWEST asking price seen across sources
            if l["price"] < keep["price"]:
                keep["price"] = l["price"]
                keep["url"] = l["url"]
        else:
            l["sources"] = [l["source"]]
            merged[key] = l
    return list(merged.values())


def score_listings(listings, cfg):
    w = cfg["scoring"]
    budget = cfg["budget"]
    neg = cfg["negotiation"]

    by_area = {}
    for l in listings:
        if l["ppsf"]:
            by_area.setdefault(l["area"], []).append(l["ppsf"])
    medians = {a: sorted(v)[len(v) // 2] for a, v in by_area.items() if v}

    for l in listings:
        strong, weak = keyword_hits(l.get("description", ""), cfg)
        l["kw_strong"], l["kw_weak"] = strong, weak

        med = medians.get(l["area"])
        l["area_median_ppsf"] = med

        ppsf_score = 0.0
        if med and l["ppsf"]:
            disc = max(0.0, (med - l["ppsf"]) / med)
            l["ppsf_discount_pct"] = round(disc * 100, 1)
            ppsf_score = min(disc / 0.40, 1.0)
        else:
            l["ppsf_discount_pct"] = None

        dom = l["dom"] or 0
        dom_score = min(dom / 90.0, 1.0)
        kw = min((len(strong) * 2 + len(weak)) / 6.0, 1.0)

        if l["price"] <= budget["max_purchase"]:
            budget_score, l["budget_fit"] = 1.0, "fits"
        elif l["price"] <= budget["stretch_purchase"]:
            budget_score, l["budget_fit"] = 0.5, "stretch"
        else:
            budget_score, l["budget_fit"] = 0.0, "over"

        l["score"] = round(
            ppsf_score * w["w_ppsf"] + dom_score * w["w_dom"]
            + kw * w["w_keywords"] + budget_score * w["w_budget"]
        )

        l["fixer"] = bool(
            strong
            or (l["ppsf_discount_pct"] and l["ppsf_discount_pct"] >= 25)
            or (len(weak) >= 2 and dom >= neg["dom_negotiable"])
        )

        if dom >= neg["dom_negotiable"]:
            disc = min(neg["max_discount_abs"], l["price"] * neg["max_discount_pct"])
            factor = min(0.6 + 0.4 * (dom - neg["dom_negotiable"]) / 45.0, 1.0)
            l["suggested_offer"] = int(round((l["price"] - disc * factor) / 1000) * 1000)
            l["negotiable"] = True
        else:
            l["suggested_offer"] = None
            l["negotiable"] = False

        basis = l["suggested_offer"] or l["price"]
        l["down_needed"] = int(basis * budget["down_pct"])
        l.pop("street", None)
        l.pop("source", None)

    listings.sort(key=lambda x: x["score"], reverse=True)
    return listings


def track_history(listings):
    hist = {}
    if HISTORY_PATH.exists():
        try:
            hist = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            hist = {}
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for l in listings:
        key = l["url"] or l["address"]
        entry = hist.setdefault(key, {"prices": []})
        if not entry["prices"] or entry["prices"][-1]["p"] != l["price"]:
            entry["prices"].append({"d": today, "p": l["price"]})
        first = entry["prices"][0]["p"]
        l["orig_price"] = first
        l["price_cut"] = first - l["price"] if first > l["price"] else 0
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    HISTORY_PATH.write_text(json.dumps(hist), encoding="utf-8")
    return listings


def main():
    cfg = load_config()
    raw = []
    src_counts = {"realtor": 0, "redfin": 0, "zillow": 0}

    print("Scraping areas (realtor + redfin + zillow)...")
    for area in cfg["areas"]:
        for fetch in (fetch_realtor, fetch_redfin, fetch_zillow):
            rows = fetch(area, cfg)
            for r in rows:
                r["area"] = area["name"]
                r["tier"] = area.get("tier", "value")
                src_counts[r["source"]] += 1
            raw.extend(rows)
            time.sleep(2)

    if not raw:
        print("No listings from any source - keeping previous data.json", file=sys.stderr)
        sys.exit(1)

    # Drop condos / co-ops / townhomes / mobile / any attached unit
    before = len(raw)
    raw = [r for r in raw if not is_attached(r, cfg)]
    print(f"Filtered out {before - len(raw)} attached/condo listings")

    listings = merge_sources(raw)
    listings = score_listings(listings, cfg)
    listings = track_history(listings)

    payload = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "count": len(listings),
        "fixer_count": sum(1 for l in listings if l["fixer"]),
        "source_counts": src_counts,
        "budget": cfg["budget"],
        "listings": listings,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    print(f"Wrote {len(listings)} unique listings "
          f"(realtor {src_counts['realtor']} / redfin {src_counts['redfin']} / zillow {src_counts['zillow']}, "
          f"{payload['fixer_count']} fixer signals)")


if __name__ == "__main__":
    main()
