#!/usr/bin/env python3
"""
Agente de ofertas post-MBA.
Greenhouse / Lever / Ashby -> filtro de reglas -> filtro de visa -> Claude -> Telegram.

Uso:
  python job_agent.py --seed                 # primera vez: marca todo como visto, sin avisar
  python job_agent.py --tier priority        # tu tracker (correr cada 5 min)
  python job_agent.py --tier normal          # el resto (correr cada 30 min)
  python job_agent.py --tier all --dry       # prueba: imprime en vez de enviar a Telegram
  python job_agent.py --discover nombres.txt # encuentra slugs de empresas y los imprime como CSV
"""
import argparse
import csv
import html
import json
import os
import re
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

import requests

# ----------------------------- CONFIG -----------------------------
MODEL = "claude-haiku-5-5"
SEEN_PATH = "seen.txt"      # ofertas ya vistas (una clave por línea)
AUDIT_LOG = "excluded_visa.log"   # revisa este archivo de vez en cuando
MIN_SCORE = 6
MAX_EVAL_PER_RUN = 150            # tope de ofertas que se mandan a Claude por corrida
UA = {"User-Agent": "personal-job-agent/1.0"}

TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID")

# ----------------------------- REGLAS -----------------------------
# Capa 1a: no es full-time
EXCLUDE_TITLE = re.compile(
    r"\b(intern|internship|co-?op|student|part[- ]time|contractor|contract|temporary|apprentice)\b", re.I)

# Capa 1b: debe haber alguna señal de que apunta a MBAs / programas de entrada
INCLUDE = re.compile(
    r"\bMBA\b|post-?mba|leadership development|rotational|rotation program|class of 2027|"
    r"graduating (?:between|in|by)[^.]{0,60}2027|early[- ]career|associate program", re.I)

# Capa 2: exclusión DURA de visa (solo si lo dicen explícitamente)
VISA_BLOCK = re.compile("|".join([
    r"(?:will|can|do|does|is|are)\s*(?:not|n't)\s+(?:be\s+able\s+to\s+|able\s+to\s+)?"
    r"(?:provide\s+|offer\s+)?(?:\w+\s+){0,2}sponsor",
    r"(?:unable|not\s+able)\s+to\s+(?:provide\s+)?(?:\w+\s+){0,2}sponsor",
    r"(?:not|n't)\s+eligible\s+for\s+(?:\w+\s+)?sponsorship",
    r"without\s+(?:the\s+need\s+for\s+)?(?:current\s+or\s+future\s+|now\s+or\s+in\s+the\s+future\s+)?"
    r"(?:visa\s+)?sponsorship",
    r"\bno\s+(?:visa\s+)?sponsorship",
    r"sponsorship\s+(?:is\s+)?not\s+(?:available|offered|provided)",
    r"permanent\s+work\s+authorization",
    r"must\s+be\s+(?:a\s+)?(?:u\.?s\.?|united\s+states)\s+(?:citizen|person)",
    r"(?:u\.?s\.?|united\s+states)\s+citizens?\s+(?:only|or\s+(?:lawful\s+)?permanent)",
]), re.I)

SYSTEM = """You screen job postings for an MBA candidate graduating from Chicago Booth in June 2027
who wants a FULL-TIME role starting around summer 2027. Reply ONLY with a JSON object, no other text:
{
 "full_time": true/false,            // false for internships, co-ops, part-time, contract, even if "Summer 2027"
 "mba_fit": true/false,              // explicitly targets MBAs / post-MBA / LDP / rotational, or accepts MBA grads
 "us_based": true/false,             // role located in the United States (remote-US counts)
 "start_date": "string",             // inferred start date or "no indica"
 "starts_summer_2027": "si" | "no" | "desconocido",   // start between ~May and Sept 2027
 "visa": "excluye" | "patrocina" | "no_indica",
     // "excluye" ONLY if the posting EXPLICITLY says it won't sponsor, requires permanent work authorization,
     // or requires US citizenship/residency. A plain "must be authorized to work in the US" is "no_indica".
 "industry": "consumo" | "tech" | "otra",
 "score": 1-10,                      // fit for a post-MBA corporate strategy / leadership program candidate
 "reason": "one short line in Spanish"
}"""


# --------------------------- FETCHERS ----------------------------
def clean(s):
    s = html.unescape(s or "")
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def get_json(url):
    r = requests.get(url, headers=UA, timeout=20)
    r.raise_for_status()
    return r.json()


def fetch_greenhouse(slug):
    data = get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true")
    return [dict(id=str(j["id"]), title=j["title"], location=(j.get("location") or {}).get("name", ""),
                 url=j["absolute_url"], text=clean(j.get("content")), commitment="")
            for j in data.get("jobs", [])]


def fetch_lever(slug):
    out = []
    for j in get_json(f"https://api.lever.co/v0/postings/{slug}?mode=json"):
        cat = j.get("categories") or {}
        lists = " ".join(l.get("text", "") + " " + l.get("content", "") for l in j.get("lists", []))
        text = " ".join([j.get("descriptionPlain", ""), lists, j.get("additionalPlain", "")])
        out.append(dict(id=j["id"], title=j["text"], location=cat.get("location", ""),
                        url=j["hostedUrl"], text=clean(text), commitment=cat.get("commitment", "")))
    return out


def fetch_ashby(slug):
    data = get_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    return [dict(id=j["id"], title=j["title"], location=j.get("location", ""), url=j["jobUrl"],
                 text=clean(j.get("descriptionPlain") or j.get("descriptionHtml")),
                 commitment=j.get("employmentType", ""))
            for j in data.get("jobs", []) if j.get("isListed", True)]


FETCHERS = {"greenhouse": fetch_greenhouse, "lever": fetch_lever, "ashby": fetch_ashby}


def safe_fetch(row):
    try:
        return row, FETCHERS[row["ats"].strip().lower()](row["slug"].strip())
    except Exception as e:
        print(f"[warn] {row['name']} ({row['ats']}/{row['slug']}): {e}")
        return row, []


# ----------------------------- LÓGICA ----------------------------
def layer1(j):
    """Devuelve None si pasa, o el motivo del rechazo."""
    if EXCLUDE_TITLE.search(j["title"]) or EXCLUDE_TITLE.search(j["commitment"]):
        return "no full-time"
    if not INCLUDE.search(j["title"] + " " + j["text"]):
        return "sin señal MBA"
    return None


def visa_block(j):
    """Devuelve el fragmento que excluye por visa, o None."""
    m = VISA_BLOCK.search(j["text"])
    if not m:
        return None
    a, b = max(0, m.start() - 80), min(len(j["text"]), m.end() + 80)
    return j["text"][a:b]


def evaluate(client, row, j):
    text = j["text"]
    if len(text) > 6000:  # las frases de visa suelen estar al final, así que se conserva el final
        text = text[:3500] + " [...] " + text[-2500:]
    prompt = (f"Company: {row['name']}\nTitle: {j['title']}\nLocation: {j['location']}\n"
              f"Employment type: {j['commitment'] or 'n/a'}\n\nDescription:\n{text}")
    try:
        msg = client.messages.create(model=MODEL, max_tokens=400, system=SYSTEM,
                                     messages=[{"role": "user", "content": prompt}])
        raw = msg.content[0].text
        return json.loads(raw[raw.index("{"): raw.rindex("}") + 1])
    except Exception as e:
        print(f"[warn] Claude falló en {row['name']} / {j['title']}: {e}")
        return None


def passes(v):
    return (v.get("full_time") and v.get("mba_fit") and v.get("us_based")
            and v.get("visa") != "excluye"
            and v.get("starts_summer_2027") in ("si", "desconocido")
            and int(v.get("score", 0)) >= MIN_SCORE)


def notify(row, j, v, dry):
    aviso = "" if v.get("starts_summer_2027") == "si" else "⚠️ Fecha de inicio no indicada\n"
    text = (f"🆕 {row['name']} — {j['title']}\n📍 {j['location']}\n"
            f"⭐ {v['score']}/10 · Visa: {v['visa']} · Inicio: {v['start_date']}\n"
            f"{aviso}{v['reason']}\n{j['url']}")
    if dry or not (TG_TOKEN and TG_CHAT):
        print("\n" + text + "\n")
        return
    requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                  json={"chat_id": TG_CHAT, "text": text, "disable_web_page_preview": True}, timeout=20)


def log_visa(row, j, snippet):
    with open(AUDIT_LOG, "a", encoding="utf-8") as f:
        f.write(f"{datetime.now():%Y-%m-%d %H:%M} | {row['name']} | {j['title']} | {j['url']}\n    ...{snippet}...\n")


def load_seen():
    if not os.path.exists(SEEN_PATH):
        return set()
    with open(SEEN_PATH, encoding="utf-8") as f:
        return {line.strip() for line in f if line.strip()}


def flush(new_keys):
    if new_keys:
        with open(SEEN_PATH, "a", encoding="utf-8") as f:
            f.write("\n".join(new_keys) + "\n")
        new_keys.clear()


def run(tier, seed, dry):
    import anthropic
    client = anthropic.Anthropic()  # lee ANTHROPIC_API_KEY
    with open("companies.csv", encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f)
                if tier == "all" or r["priority"].strip().lower() == tier]
    seen = load_seen()
    new_keys = []

    with ThreadPoolExecutor(8) as ex:
        results = list(ex.map(safe_fetch, rows))

    evaluated = sent = 0
    for row, jobs in results:
        for j in jobs:
            key = f"{row['ats']}:{row['slug']}:{j['id']}"
            if key in seen:
                continue
            mark = lambda: (seen.add(key), new_keys.append(key))
            if seed or layer1(j):
                mark()
                continue
            snippet = visa_block(j)
            if snippet:
                log_visa(row, j, snippet)
                mark()
                continue
            if evaluated >= MAX_EVAL_PER_RUN:
                continue  # sin marcar: se evalúa en la próxima corrida
            evaluated += 1
            v = evaluate(client, row, j)
            if v is None:
                continue  # sin marcar: reintenta después
            mark()
            if passes(v):
                notify(row, j, v, dry)
                sent += 1
        flush(new_keys)
    print(f"[{datetime.now():%H:%M}] empresas={len(rows)} evaluadas={evaluated} avisos={sent}")


def discover(path):
    probes = {"greenhouse": "https://boards-api.greenhouse.io/v1/boards/{s}/jobs",
              "lever": "https://api.lever.co/v0/postings/{s}?mode=json",
              "ashby": "https://api.ashbyhq.com/posting-api/job-board/{s}"}
    print("ats,slug,name,priority")
    for name in open(path, encoding="utf-8").read().splitlines():
        name = name.strip()
        if not name:
            continue
        base = re.sub(r"[^a-z0-9 ]", "", name.lower())
        for slug in {base.replace(" ", ""), base.replace(" ", "-")}:
            for ats, url in probes.items():
                try:
                    if requests.get(url.format(s=slug), headers=UA, timeout=15).status_code == 200:
                        print(f"{ats},{slug},{name},normal")
                except requests.RequestException:
                    pass


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--tier", default="priority", choices=["priority", "normal", "all"])
    ap.add_argument("--seed", action="store_true", help="marcar todo como visto sin evaluar ni avisar")
    ap.add_argument("--dry", action="store_true", help="imprimir en vez de enviar a Telegram")
    ap.add_argument("--discover", metavar="ARCHIVO", help="archivo con un nombre de empresa por línea")
    a = ap.parse_args()
    if a.discover:
        discover(a.discover)
    else:
        run(a.tier, a.seed, a.dry)
