#!/usr/bin/env python3
"""Fetch CureNow Meta ads + ad-level daily insights via cmo-ai-cloud MCP.
Outputs: dashboard-mock/data.json + downloads fresh images to dashboard-mock/images/
"""
import json, os, re, sys, time, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor

MCP_URL = "https://kxmhgmeiosbkrnaygobe.supabase.co/functions/v1/mcp"
MCP_TOKEN = "1ca90e47321a0dbe12b7bec61cd83e78b6f6d3b51c41eab0657002c5bfe22100"
AD_ACCOUNT = "act_2172563619854212"
BASE = os.path.dirname(os.path.abspath(__file__))


def mcp(method, params=None):
    body = {"jsonrpc": "2.0", "id": int(time.time() * 1000), "method": method, "params": params or {}}
    req = urllib.request.Request(
        MCP_URL,
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {MCP_TOKEN}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        text = r.read().decode()
    # Parse SSE
    for line in text.split("\n"):
        if line.startswith("data:"):
            obj = json.loads(line[5:].strip())
            if "error" in obj:
                raise RuntimeError(obj["error"])
            content = obj.get("result", {}).get("content", [{}])[0].get("text", "")
            return json.loads(content) if content else obj.get("result")
    raise RuntimeError("No data in SSE response: " + text[:200])


def call_tool(name, args):
    return mcp("tools/call", {"name": name, "arguments": args})


# === step 1: fetch ads + campaigns ===
print("[1/5] Fetching ACTIVE ads...")
ads = call_tool("meta_list_ads", {"adAccountId": AD_ACCOUNT, "status": "ACTIVE", "limit": 50})
print(f"   {len(ads)} ads")

print("[2/5] Fetching ACTIVE campaigns...")
campaigns = call_tool("meta_list_campaigns", {"adAccountId": AD_ACCOUNT, "status": "ACTIVE", "limit": 25})
print(f"   {len(campaigns)} campaigns")

print("[2.5/5] Fetching ACTIVE adsets...")
adsets = call_tool("meta_list_ad_sets", {"adAccountId": AD_ACCOUNT, "status": "ACTIVE", "limit": 50})
print(f"   {len(adsets)} adsets")


# === step 3: per-ad insights ===
def fetch_ad_insights(ad):
    aid = ad["id"]
    try:
        rows = call_tool("meta_get_ad_insights", {"adId": aid, "datePreset": "last_30d", "timeIncrement": "1"})
        return aid, rows
    except Exception as e:
        print(f"  [WARN] ad {aid}: {e}")
        return aid, []


print("[3/5] Fetching per-ad daily insights (21 calls)...")
ad_insights = {}
with ThreadPoolExecutor(max_workers=4) as ex:
    for aid, rows in ex.map(fetch_ad_insights, ads):
        ad_insights[aid] = rows
        print(f"  ad {aid}: {len(rows)} daily rows")


# === step 4: campaign daily insights ===
print("[4/5] Fetching per-campaign daily insights...")
def fetch_camp(c):
    try:
        rows = call_tool("meta_get_campaign_insights", {"campaignId": c["id"], "datePreset": "last_30d", "timeIncrement": "1"})
        return c["id"], rows
    except Exception as e:
        print(f"  [WARN] camp {c['id']}: {e}")
        return c["id"], []

camp_insights = {}
with ThreadPoolExecutor(max_workers=4) as ex:
    for cid, rows in ex.map(fetch_camp, campaigns):
        camp_insights[cid] = rows
        print(f"  camp {cid}: {len(rows)} daily rows")


# === step 5: download fresh images ===
print("[5/5] Downloading images...")
imgs_dir = os.path.join(BASE, "images")
os.makedirs(imgs_dir, exist_ok=True)


def get_img_url(ad):
    c = ad.get("creative", {}) or {}
    url = c.get("image_url")
    if url:
        return url
    osp = c.get("object_story_spec", {}) or {}
    vd = osp.get("video_data", {}) or {}
    if vd.get("image_url"):
        return vd["image_url"]
    return c.get("thumbnail_url")


def download(args):
    aid, url = args
    if not url:
        return f"  [skip] {aid}: no url"
    out = os.path.join(imgs_dir, f"{aid}.jpg")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = r.read()
        with open(out, "wb") as f:
            f.write(data)
        return f"  [{len(data)//1024}KB] {aid}"
    except Exception as e:
        return f"  [FAIL] {aid}: {e}"


with ThreadPoolExecutor(max_workers=8) as ex:
    for r in ex.map(download, [(a["id"], get_img_url(a)) for a in ads]):
        print(r)


# === step 6: compact data + write ===
def actions_get(actions, key):
    if not actions: return 0
    for a in actions:
        if a.get("action_type") == key:
            return int(float(a.get("value", 0)))
    return 0


def compact_daily(rows):
    """Convert raw insight rows to [date, imp, click, ctr, spend, link_click, cv]"""
    out = []
    for r in rows:
        out.append([
            r["date_start"],
            int(r.get("impressions") or 0),
            int(r.get("clicks") or 0),
            round(float(r.get("ctr") or 0), 2),
            int(float(r.get("spend") or 0)),
            actions_get(r.get("actions") or [], "link_click"),
            actions_get(r.get("actions") or [], "offsite_conversion.fb_pixel_custom"),
        ])
    return out


def infer_dept(name, link):
    nl = (name + " " + (link or "")).lower()
    out = []
    for keys, label in [
        (("pediatric", "小児"), "小児科"),
        (("diabetes", "糖尿"), "糖尿病"),
        (("hypertension", "高血圧"), "高血圧"),
        (("gout", "痛風", "尿酸", "通風"), "痛風"),
        (("lipid", "脂質"), "脂質異常症"),
        (("hayfever", "花粉"), "花粉症"),
        (("/ed/", "ＥＤ"), "ED"),
        (("diet", "ダイエット", "甘いもの"), "医療ダイエット"),
    ]:
        if any(k.lower() in nl for k in keys):
            out.append(label)
    return out


def infer_axis(name, body):
    t = (name or "") + " " + (body or "")
    rules = [
        (["即時", "当日", "すぐ", "今すぐ"], "即時診察"),
        (["通院", "スマホ", "オンライン"], "通院ゼロ"),
        (["院内感染", "病気をもらう"], "院内感染回避"),
        (["夜間", "夜でも"], "夜間対応"),
        (["お悩み", "ありませんか"], "共感喚起"),
        (["危険", "危機感", "予備軍", "やばい", "放っておく", "甘いもの"], "危機感喚起"),
        (["薬手に入", "薬の入手", "薬が手"], "薬入手"),
    ]
    for keys, label in rules:
        if any(k in t for k in keys):
            return label
    return "汎用"


def ad_compact(ad):
    c = ad.get("creative", {}) or {}
    osp = c.get("object_story_spec", {}) or {}
    ld = osp.get("link_data", {}) or {}
    vd = osp.get("video_data", {}) or {}
    link = ld.get("link") or vd.get("call_to_action", {}).get("value", {}).get("link") or ""
    body = c.get("body") or ld.get("message") or vd.get("message") or ""
    return {
        "id": ad["id"],
        "name": ad["name"],
        "status": ad.get("effective_status", "ACTIVE"),
        "campaign_id": ad.get("campaign_id"),
        "adset_id": ad.get("adset_id"),
        "image": f"./images/{ad['id']}.jpg",
        "link": link,
        "dept": infer_dept(ad["name"], link),
        "axis": infer_axis(ad["name"], body),
        "created_time": ad.get("created_time", "")[:10],
        "daily": compact_daily(ad_insights.get(ad["id"], [])),
    }


def camp_compact(c):
    return {
        "id": c["id"],
        "name": c["name"],
        "status": c.get("effective_status", c.get("status", "ACTIVE")),
        "daily": compact_daily(camp_insights.get(c["id"], [])),
    }


def adset_compact(s):
    return {
        "id": s["id"],
        "name": s["name"],
        "campaign_id": s.get("campaign_id"),
        "status": s.get("effective_status", s.get("status", "ACTIVE")),
        "daily_budget": int(s.get("daily_budget") or 0),
        "targeting_summary": "",
    }


out = {
    "generated_at": time.strftime("%Y-%m-%d %H:%M JST", time.localtime()),
    "campaigns": [camp_compact(c) for c in campaigns],
    "adsets": [adset_compact(s) for s in adsets],
    "ads": [ad_compact(a) for a in ads],
}

with open(os.path.join(BASE, "data.json"), "w") as f:
    json.dump(out, f, ensure_ascii=False, indent=1)

print(f"\nWrote data.json: {len(out['campaigns'])} campaigns, {len(out['adsets'])} adsets, {len(out['ads'])} ads")
print(f"Total daily insight rows: {sum(len(a['daily']) for a in out['ads'])} ad-days + {sum(len(c['daily']) for c in out['campaigns'])} campaign-days")
