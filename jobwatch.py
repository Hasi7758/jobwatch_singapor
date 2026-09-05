#!/usr/bin/env python3
"""
munich-jobwatch
================
每天抓一次慕尼黑相关职位,只报告"今天第一次见到"的那些。

核心思路:不相信任何平台标注的发布日期(StepStone 之类经常滞后半个月),
而是自己建库记录每个职位 ID 第一次出现的时间。首次出现 = 新职位。

用法:
    python jobwatch.py run                # 抓取 + 差分 + 生成报告
    python jobwatch.py discover <slug>... # 探测某公司用的是哪套招聘系统
    python jobwatch.py stats              # 看数据库里有多少条
    python jobwatch.py reset              # 清空数据库重新开始
"""

import argparse
import html
import json
import os
import re
import sqlite3
import sys
import time
import webbrowser
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

try:
    import requests
except ImportError:
    sys.exit("缺少依赖,请先运行:  pip install requests pyyaml")

try:
    import yaml
except ImportError:
    sys.exit("缺少依赖,请先运行:  pip install requests pyyaml")



def _html_unescape(s):
    return re.sub(r'\s+', ' ', html.unescape(s or '')).strip()


BASE = Path(__file__).resolve().parent
DB_PATH = BASE / "jobs.db"
CONFIG_PATH = BASE / "config.yaml"
COMPANIES_PATH = BASE / "companies.yaml"
OUT_HTML = BASE / "digest.html"
DOCS_HTML = BASE / "docs" / "index.html"   # GitHub Pages 用
IN_CI = bool(os.environ.get("GITHUB_ACTIONS") or os.environ.get("CI"))

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 " \
     "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"

session = requests.Session()
session.headers.update({"User-Agent": UA, "Accept": "application/json, text/xml, */*"})


# ----------------------------------------------------------------------------
# 数据结构
# ----------------------------------------------------------------------------

@dataclass
class Job:
    uid: str            # 全局唯一 key,用于差分
    source: str         # 来源标识
    company: str
    title: str
    location: str
    url: str
    posted: str = ""    # 来源自己声称的发布日期(仅供参考,不作为判断依据)
    extra: dict = field(default_factory=dict)


# ----------------------------------------------------------------------------
# 配置
# ----------------------------------------------------------------------------

def load_yaml(path, default=None):
    if not path.exists():
        return default if default is not None else {}
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or (default if default is not None else {})


def load_config():
    cfg = load_yaml(CONFIG_PATH)
    if not cfg:
        sys.exit(f"找不到配置文件 {CONFIG_PATH}")
    cfg.setdefault("keywords", {})
    cfg["keywords"].setdefault("include", [])
    cfg["keywords"].setdefault("exclude", [])
    cfg.setdefault("location", {})
    cfg["location"].setdefault("terms", ["münchen", "munich", "muenchen"])
    cfg["location"].setdefault("allow_remote", True)
    cfg["location"].setdefault("plz_prefixes", ["80", "81", "82", "85"])
    cfg.setdefault("mycareersfuture", {})
    cfg["mycareersfuture"].setdefault("enabled", True)
    cfg["mycareersfuture"].setdefault("pages", 5)
    cfg["mycareersfuture"].setdefault("queries", [""])
    cfg.setdefault("telegram", {})
    cfg["telegram"].setdefault("enabled", False)
    cfg.setdefault("open_browser", True)
    return cfg




# ----------------------------------------------------------------------------
# 数据库(差分的核心)
# ----------------------------------------------------------------------------

def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            uid        TEXT PRIMARY KEY,
            source     TEXT,
            company    TEXT,
            title      TEXT,
            location   TEXT,
            url        TEXT,
            posted     TEXT,
            first_seen TEXT,
            igm        TEXT DEFAULT ''
        )
    """)
    conn.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
    conn.commit()
    return conn


def is_first_run(conn):
    row = conn.execute("SELECT v FROM meta WHERE k='seeded'").fetchone()
    return row is None


def mark_seeded(conn):
    # 记下基线里最晚的 first_seen,之后凡是严格大于它的才算"新"
    row = conn.execute("SELECT COALESCE(MAX(first_seen),'') FROM jobs").fetchone()
    conn.execute("INSERT OR REPLACE INTO meta VALUES ('seeded', ?)", (row[0],))
    conn.commit()


def split_new(conn, jobs):
    """返回数据库里还没有的职位,并把全部职位写入库。"""
    known = {r[0] for r in conn.execute("SELECT uid FROM jobs")}
    now = datetime.now(timezone.utc).isoformat()
    fresh = []
    for j in jobs:
        if j.uid in known:
            continue
        known.add(j.uid)
        fresh.append(j)
        conn.execute(
            "INSERT OR IGNORE INTO jobs VALUES (?,?,?,?,?,?,?,?,?)",
            (j.uid, j.source, j.company, j.title, j.location, j.url,
             j.posted, now, ",".join(j.extra.get("cats") or [])),
        )
    conn.commit()
    return fresh



def parse_posted(s):
    """把各来源的发布日期解析成 date。解析不出返回 None。
    覆盖:ISO 日期、Workday 的 'Posted 5 Days Ago' / 'Posted Today' / 'Posted 30+ Days Ago'。"""
    if not s:
        return None
    s = str(s).strip()
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    m = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", s)   # 31.08.2026
    if m:
        try:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            return None
    low = s.lower()
    today = datetime.now(timezone.utc).date()
    if "today" in low or "heute" in low or "gerade" in low:
        return today
    if "yesterday" in low or "gestern" in low:
        return today - timedelta(days=1)
    m = re.search(r"(\d+)\s*\+?\s*(?:day|tag)", low)
    if m:
        return today - timedelta(days=int(m.group(1)))
    m = re.search(r"(\d+)\s*\+?\s*(?:week|woche)", low)
    if m:
        return today - timedelta(weeks=int(m.group(1)))
    m = re.search(r"(\d+)\s*\+?\s*(?:month|monat)", low)
    if m:
        return today - timedelta(days=30 * int(m.group(1)))
    return None


def job_age_days(j):
    """职位"有多新"。优先用来源标注的发布日期,没有就用我们首次见到的日期。"""
    today = datetime.now(timezone.utc).date()
    d = parse_posted(j.posted)
    if d:
        return (today - d).days, "posted"
    fs = j.extra.get("first_seen") or ""
    if fs:
        try:
            return (today - datetime.fromisoformat(fs).date()).days, "seen"
        except ValueError:
            pass
    return None, ""


def age_label(j):
    n, how = job_age_days(j)
    if n is None:
        return ""
    src = "发布" if how == "posted" else "发现"
    if n <= 0:
        return f"{src}于今天"
    if n == 1:
        return f"{src}于昨天"
    return f"{src}于 {n} 天前"


def all_current(conn, limit=3000):
    """库中全部职位,IG Metall 优先、新入库在前。"""
    rows = conn.execute(
        "SELECT company,title,location,url,posted,source,igm,first_seen FROM jobs "
        "ORDER BY first_seen DESC LIMIT ?", (limit,)).fetchall()
    return [_row_to_job(r) for r in rows]


def _row_to_job(r):
    c, ti, l, u, po, s, igm, fs = r
    j = Job(uid="", source=s, company=c, title=ti, location=l, url=u, posted=po)
    if igm:
        j.extra["cats"] = [x for x in igm.split(",") if x]
    j.extra["first_seen"] = fs
    return j


def recent_days(conn, days=2):
    """最近 N 天内首次出现的职位,按天分组(新的在前)。
    不再排除基线:基线本身就是"那天第一次看到的",同样有参考价值。"""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = conn.execute(
        "SELECT company,title,location,url,posted,source,igm,first_seen FROM jobs "
        "WHERE first_seen >= ? ORDER BY first_seen DESC", (cutoff,)).fetchall()
    groups = {}
    for r in rows:
        groups.setdefault(r[7][:10], []).append(_row_to_job(r))
    return sorted(groups.items(), reverse=True)



# ----------------------------------------------------------------------------
# 过滤
# ----------------------------------------------------------------------------

def matches_keywords(job, kw):
    hay = f"{job.title} {job.company}".lower()
    inc, exc = kw.get("include") or [], kw.get("exclude") or []
    if any(e.lower() in hay for e in exc):
        return False
    if not inc:
        return True
    return any(i.lower() in hay for i in inc)


def matches_location(job, loc):
    text = f"{job.location}".lower()
    if not text.strip():
        return True  # 位置信息缺失时不误杀,交给关键词过滤
    if any(t.lower() in text for t in loc.get("terms", [])):
        return True
    if loc.get("allow_remote") and re.search(r"remote|home\s?office|ortsunabh", text):
        return True
    for p in loc.get("plz_prefixes", []):
        if re.search(rf"\b{p}\d{{3}}\b", text):
            return True
    return False


# ----------------------------------------------------------------------------

# ----------------------------------------------------------------------------
# 来源 1:MyCareersFuture(新加坡政府官方求职门户,公开接口)
# 返回真实发布日期 newPostingDate,且支持关键词搜索。
# ----------------------------------------------------------------------------

MCF_URL = "https://api.mycareersfuture.gov.sg/v2/search"


def fetch_mcf(cfg):
    mc = cfg.get("mycareersfuture") or {}
    if not mc.get("enabled", True):
        return []
    out, seen = [], set()
    for term in (mc.get("queries") or [""]):
        for page in range(int(mc.get("pages", 5))):
            try:
                r = session.post(
                    MCF_URL + "?limit=100&page=" + str(page),
                    json={"search": term, "sessionId": "", "categories": []},
                    headers={"Content-Type": "application/json"},
                    timeout=30)
                if r.status_code != 200:
                    print("  [MCF] " + term + " HTTP " + str(r.status_code))
                    break
                data = r.json()
            except Exception as e:
                print("  [MCF] " + term + " 失败: " + str(e))
                break
            items = data.get("results") or []
            for it in items:
                meta = it.get("metadata") or {}
                jid = meta.get("jobPostId") or it.get("uuid")
                if not jid or jid in seen:
                    continue
                seen.add(jid)
                comp = it.get("postedCompany") or it.get("hiringCompany") or {}
                addr = it.get("address") or {}
                loc = " ".join(str(x) for x in
                               [addr.get("block"), addr.get("street")] if x) or "Singapore"
                skills = " ".join(s.get("skill", "") for s in (it.get("skills") or []))
                levels = " ".join(l.get("position", "") if isinstance(l, dict) else str(l)
                                  for l in (it.get("positionLevels") or []))
                cats_txt = " ".join(c.get("category", "") for c in (it.get("categories") or []))
                _j = Job(
                    uid="mcf:" + str(jid),
                    source="MyCareersFuture",
                    company=comp.get("name") or "-",
                    title=it.get("title") or "-",
                    location=loc,
                    url="https://www.mycareersfuture.gov.sg/job/" + str(jid),
                    posted=meta.get("newPostingDate") or str(meta.get("updatedAt", ""))[:10],
                )
                _j.extra["desc"] = f"{skills} {levels} {cats_txt}"
                out.append(_j)
            if len(items) < 100:
                break
            time.sleep(0.4)
    return out
# 来源 2:公司自己的 ATS 接口(比任何聚合平台都早)
# ----------------------------------------------------------------------------

def _get(url, **kw):
    kw.setdefault("timeout", 20)
    return session.get(url, **kw)


def ats_personio(slug, name):
    import xml.etree.ElementTree as ET
    r = _get(f"https://{slug}.jobs.personio.de/xml")
    r.raise_for_status()
    root = ET.fromstring(r.content)
    jobs = []
    for p in root.iter("position"):
        def t(tag):
            el = p.find(tag)
            return (el.text or "").strip() if el is not None and el.text else ""
        jid = t("id")
        if not jid:
            continue
        jobs.append(Job(
            uid=f"personio:{slug}:{jid}",
            source="Personio",
            company=name,
            title=t("name"),
            location=" ".join(x for x in [t("office"), t("department")] if x),
            url=f"https://{slug}.jobs.personio.de/job/{jid}",
            posted=t("createdAt"),
        ))
    return jobs


def ats_greenhouse(slug, name):
    r = _get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true")
    r.raise_for_status()
    out = []
    for j in r.json().get("jobs", []):
        jb = Job(
            uid=f"gh:{slug}:{j['id']}",
            source="Greenhouse",
            company=name,
            title=j.get("title", ""),
            location=(j.get("location") or {}).get("name", ""),
            url=j.get("absolute_url", ""),
            posted=j.get("first_published") or j.get("updated_at", ""),
        )
        jb.extra["desc"] = re.sub(r"<[^>]+>", " ", str(j.get("content") or ""))[:4000]
        out.append(jb)
    return out


def _ats_greenhouse_old(slug, name):
    r = _get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
    r.raise_for_status()
    return [Job(
        uid=f"gh:{slug}:{j['id']}",
        source="Greenhouse",
        company=name,
        title=j.get("title", ""),
        location=(j.get("location") or {}).get("name", ""),
        url=j.get("absolute_url", ""),
        posted=j.get("updated_at", ""),
    ) for j in r.json().get("jobs", [])]


def ats_lever(slug, name):
    r = _get(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    r.raise_for_status()
    jobs = []
    for j in r.json():
        cat = j.get("categories") or {}
        ts = j.get("createdAt")
        jobs.append(Job(
            uid=f"lever:{slug}:{j.get('id')}",
            source="Lever",
            company=name,
            title=j.get("text", ""),
            location=cat.get("location", "") or "",
            url=j.get("hostedUrl", ""),
            posted=datetime.fromtimestamp(ts / 1000, timezone.utc).date().isoformat() if ts else "",
        ))
    return jobs


def ats_smartrecruiters(slug, name):
    jobs, offset = [], 0
    while offset < 400:
        r = _get(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings",
                 params={"limit": 100, "offset": offset})
        r.raise_for_status()
        data = r.json()
        items = data.get("content") or []
        for j in items:
            loc = j.get("location") or {}
            jobs.append(Job(
                uid=f"sr:{slug}:{j.get('id')}",
                source="SmartRecruiters",
                company=name,
                title=j.get("name", ""),
                location=" ".join(str(x) for x in [loc.get("city"), loc.get("country")] if x),
                url=f"https://jobs.smartrecruiters.com/{slug}/{j.get('id')}",
                posted=(j.get("releasedDate") or "")[:10],
            ))
        if len(items) < 100:
            break
        offset += 100
    return jobs


def ats_recruitee(slug, name):
    r = _get(f"https://{slug}.recruitee.com/api/offers/")
    r.raise_for_status()
    return [Job(
        uid=f"rc:{slug}:{j.get('id')}",
        source="Recruitee",
        company=name,
        title=j.get("title", ""),
        location=" ".join(str(x) for x in [j.get("city"), j.get("country")] if x),
        url=j.get("careers_url") or j.get("careers_apply_url", ""),
        posted=(j.get("published_at") or "")[:10],
    ) for j in r.json().get("offers", [])]


def ats_ashby(slug, name):
    r = _get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    r.raise_for_status()
    return [Job(
        uid=f"ashby:{slug}:{j.get('id')}",
        source="Ashby",
        company=name,
        title=j.get("title", ""),
        location=j.get("location", "") or "",
        url=j.get("jobUrl", ""),
        posted=(j.get("publishedAt") or "")[:10],
    ) for j in r.json().get("jobs", [])]


def ats_workable(slug, name):
    r = _get(f"https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true")
    r.raise_for_status()
    return [Job(
        uid=f"wk:{slug}:{j.get('shortcode')}",
        source="Workable",
        company=name,
        title=j.get("title", ""),
        location=" ".join(str(x) for x in [j.get("city"), j.get("country")] if x),
        url=j.get("url") or j.get("application_url", ""),
        posted=(j.get("published_on") or "")[:10],
    ) for j in r.json().get("jobs", [])]


def ats_join(slug, name):
    r = _get(f"https://api.join.com/api/v1/companies/{slug}/jobs")
    r.raise_for_status()
    data = r.json()
    items = data if isinstance(data, list) else data.get("data") or data.get("jobs") or []
    return [Job(
        uid=f"join:{slug}:{j.get('id')}",
        source="JOIN",
        company=name,
        title=j.get("title", ""),
        location=str(j.get("location") or j.get("city") or ""),
        url=j.get("url", ""),
        posted=(str(j.get("publishedAt") or ""))[:10],
    ) for j in items]



def ats_successfactors(cfg_entry, name):
    """SAP SuccessFactors 的求职门户(HTML)。解析职位列表页。"""
    base = cfg_entry["url"].rstrip("/")
    jobs, seen = [], set()
    for start in (0, 25, 50, 75):
        url = f"{base}/search/?q=&locationsearch={quote(cfg_entry.get('location','München'))}&startrow={start}"
        try:
            r = session.get(url, timeout=25)
            if r.status_code != 200:
                break
            page = r.text
        except Exception:
            break
        blocks = re.findall(
            r'href="(/job/[^"]+)"[^>]*>\s*([^<]{4,120}?)\s*</a>(.{0,900}?)(?=href="/job/|</tbody>)',
            page, re.S)
        if not blocks:
            break
        n_before = len(jobs)
        for href, title, tail in blocks:
            jid = href.rstrip("/").split("/")[-1]
            if jid in seen:
                continue
            seen.add(jid)
            loc = ""
            m = re.search(r'jobLocation[^>]*>\s*([^<]{2,60})', tail)
            if m:
                loc = m.group(1).strip()
            jobs.append(Job(
                uid=f"sf:{name}:{jid}",
                source="SuccessFactors",
                company=name,
                title=_html_unescape(title),
                location=_html_unescape(loc),
                url=base.split("/search")[0] + href,
                posted="",
            ))
        if len(jobs) == n_before:
            break
        time.sleep(0.4)
    return jobs


def ats_workday(cfg_entry, name):
    """Workday 需要 POST,且每家公司的 tenant/site 不同。"""
    url = cfg_entry["url"]          # 例:https://x.wd3.myworkdayjobs.com/wday/cxs/x/Careers/jobs
    base = url.split("/wday/")[0]
    jobs, offset = [], 0
    while offset < 400:
        r = session.post(url, json={"appliedFacets": {}, "limit": 20,
                                    "offset": offset,
                                    "searchText": cfg_entry.get("search", "")},
                         headers={"Content-Type": "application/json",
                                  "Accept": "application/json"},
                         timeout=25)
        r.raise_for_status()
        data = r.json()
        items = data.get("jobPostings") or []
        for j in items:
            path = j.get("externalPath", "")
            jobs.append(Job(
                uid=f"wd:{name}:{path}",
                source="Workday",
                company=name,
                title=j.get("title", ""),
                location=j.get("locationsText", "") or "",
                url=base + path,
                posted=j.get("postedOn", ""),
            ))
        if len(items) < 20:
            break
        offset += 20
    return jobs




def ats_hr4you(cfg_entry, name):
    """
    HR4YOU 没有列表接口,职位页是 generator.php?id=N。

    为了不给对方服务器造成不必要的负担:首次全量扫一遍并记住命中的 ID,
    之后每天只做两件事——复查已知 ID 是否还在(职位下架就消失),
    再在已知最大 ID 往上探一个小窗口(新职位的 ID 总是递增的)。
    请求量从每天 2000 次降到几十次。
    """
    import concurrent.futures as cf
    import json as _json

    host = cfg_entry["url"].rstrip("/")
    state_file = BASE / f"hr4you_{re.sub(r'[^a-z0-9]+', '', name.lower())}.json"
    known = []
    if state_file.exists():
        try:
            known = _json.loads(state_file.read_text())
        except Exception:
            known = []

    if known:
        window = int(cfg_entry.get("scan_ahead", 40))
        hi = max(known)
        ids = sorted(set(known) | set(range(hi + 1, hi + window + 1)))
        mode = f"增量({len(known)}个已知 + 前探{window})"
    else:
        ids = list(range(int(cfg_entry.get("id_from", 1000)),
                         int(cfg_entry.get("id_to", 3000)) + 1))
        mode = f"首次全量扫描({len(ids)})"
    print(f"  [HR4YOU:{name}] {mode},共 {len(ids)} 次请求")

    def probe(i):
        try:
            r = session.get(f"{host}/generator.php?id={i}&changelanguage=de", timeout=12)
        except Exception:
            return None
        if r.status_code != 200 or len(r.content) < 3000:
            return None
        r.encoding = "iso-8859-1"
        page = r.text
        m = re.search(r"<title>(.*?)</title>", page, re.S)
        if not m:
            return None
        title = _html_unescape(m.group(1))
        if len(title) < 6 or "fehler" in title.lower() or "error" in title.lower():
            return None
        loc = ""
        mk = re.search(r'Keywords"\s+CONTENT="[^"]*?,\s*[^,"]*,\s*([^,"]{3,40}?)\s*,', page, re.I)
        if mk:
            loc = _html_unescape(mk.group(1))
        return i, Job(uid=f"hr4you:{name}:{i}", source="HR4YOU", company=name,
                      title=title, location=loc or "München",
                      url=f"{host}/generator.php?id={i}&changelanguage=de", posted="")

    jobs, hits = [], []
    # 并发降到 4,并分批加间隔,避免给对方造成突发压力
    with cf.ThreadPoolExecutor(max_workers=4) as ex:
        for n, res in enumerate(ex.map(probe, ids)):
            if res:
                hits.append(res[0])
                jobs.append(res[1])
            if n and n % 200 == 0:
                time.sleep(1.0)
    if hits:
        try:
            state_file.write_text(_json.dumps(sorted(hits)))
        except Exception:
            pass
    return jobs





def ats_oracle(cfg_entry, name):
    """
    Oracle Cloud HCM (Recruiting Cloud)。很多跨国企业在用。
    公开 REST 接口,返回真实 PostedDate,可按国家过滤。
    cfg 需要 url(租户域名)、site(如 CX_1001),可选 location_id。
    """
    host = cfg_entry["url"].rstrip("/")
    site = cfg_entry.get("site", "CX_1001")
    api = f"{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
    jobs, offset = [], 0
    while offset < 600:
        finder = f"findReqs;siteNumber={site},limit=100,sortBy=POSTING_DATES_DESC"
        if cfg_entry.get("keyword"):
            finder += f",keyword={cfg_entry['keyword']}"
        try:
            params = {"onlyData": "true", "finder": finder}
            if offset:
                params["offset"] = offset
            r = session.get(api, params=params,
                            headers={"Accept": "application/json"}, timeout=30)
            if r.status_code != 200:
                print(f"  [Oracle:{name}] HTTP {r.status_code}")
                break
            blocks = r.json().get("items") or []
            items = []
            for b in blocks:
                items += b.get("requisitionList") or []
        except Exception as e:
            print(f"  [Oracle:{name}] {type(e).__name__}")
            break
        if not items:
            break
        for j in items:
            jid = j.get("Id")
            if not jid:
                continue
            loc = j.get("PrimaryLocation") or j.get("PrimaryLocationCountry") or ""
            jobs.append(Job(
                uid=f"oracle:{name}:{jid}",
                source="Oracle HCM",
                company=name,
                title=j.get("Title") or "-",
                location=str(loc),
                url=f"{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{jid}",
                posted=str(j.get("PostedDate") or "")[:10],
            ))
        if len(items) < 100:
            break
        offset += 100
        time.sleep(0.3)
    return jobs


def ats_phenom(cfg_entry, name):
    """
    Phenom People 平台(AMD / Keysight / 多数大型制造企业用)。
    /api/jobs 是公开 JSON,支持 location 过滤,返回精确到分钟的发布时间。
    """
    host = cfg_entry["url"].rstrip("/")
    loc = cfg_entry.get("location", "Singapore")
    jobs, page = [], 1
    while page <= 10:
        try:
            r = session.get(host + "/api/jobs",
                            params={"location": loc, "limit": 100, "page": page},
                            headers={"Accept": "application/json"}, timeout=25)
            if r.status_code != 200:
                break
            items = (r.json() or {}).get("jobs") or []
        except Exception as e:
            print(f"  [Phenom:{name}] {type(e).__name__}")
            break
        if not items:
            break
        for it in items:
            d = it.get("data") or it
            jid = d.get("slug") or d.get("req_id")
            if not jid:
                continue
            city = d.get("city") or d.get("location_name") or d.get("state") or ""
            country = d.get("country") or ""
            jobs.append(Job(
                uid=f"phenom:{name}:{jid}",
                source="Phenom",
                company=name,
                title=d.get("title") or "-",
                location=" ".join(str(x) for x in [city, country] if x) or loc,
                url=d.get("apply_url") or f"{host}/careers/job/{jid}",
                posted=str(d.get("posted_date") or d.get("create_date") or "")[:10],
            ))
        if len(items) < 100:
            break
        page += 1
        time.sleep(0.3)
    return jobs


def ats_softgarden(slug, name):
    """softgarden:德国中小企业常用。"""
    r = _get(f"https://{slug}.softgarden.io/api/rest/frontend/v3/job-postings",
             params={"limit": 200})
    r.raise_for_status()
    d = r.json()
    items = d.get("content") or d.get("jobPostings") or d.get("data") or []
    jobs = []
    for j in items:
        jid = j.get("id") or j.get("jobPostingId")
        if not jid:
            continue
        loc = j.get("jobLocation") or j.get("location") or {}
        if isinstance(loc, dict):
            loc = " ".join(str(x) for x in [loc.get("postalCode"), loc.get("city"),
                                            loc.get("name")] if x)
        jobs.append(Job(
            uid=f"sg:{slug}:{jid}",
            source="softgarden",
            company=name,
            title=j.get("jobTitle") or j.get("name") or j.get("title", ""),
            location=str(loc),
            url=j.get("jobPostingUrl") or j.get("url")
                or f"https://{slug}.softgarden.io/job/{jid}",
            posted=str(j.get("onlineDate") or j.get("createdDate") or "")[:10],
        ))
    return jobs


def ats_teamtailor(slug, name):
    r = _get(f"https://{slug}.teamtailor.com/jobs.json")
    r.raise_for_status()
    d = r.json()
    items = d if isinstance(d, list) else (d.get("jobs") or d.get("data") or [])
    return [Job(
        uid=f"tt:{slug}:{j.get('id')}",
        source="Teamtailor",
        company=name,
        title=j.get("title", ""),
        location=str(j.get("location") or ""),
        url=j.get("url", ""),
        posted=str(j.get("created_at") or "")[:10],
    ) for j in items]


ATS_FETCHERS = {
    "personio": ats_personio,
    "greenhouse": ats_greenhouse,
    "lever": ats_lever,
    "smartrecruiters": ats_smartrecruiters,
    "recruitee": ats_recruitee,
    "ashby": ats_ashby,
    "workable": ats_workable,
    "join": ats_join,
    "softgarden": ats_softgarden,
    "teamtailor": ats_teamtailor,
}


def fetch_companies():
    conf = load_yaml(COMPANIES_PATH, default={"companies": []})
    out = []
    for c in conf.get("companies") or []:
        _n_before = 0
        name = c.get("name") or c.get("slug", "?")
        ats = (c.get("ats") or "").lower()
        try:
            _n_before = len(out)
            if ats == "workday":
                out += ats_workday(c, name)
            elif ats == "successfactors":
                out += ats_successfactors(c, name)
            elif ats == "bmw":
                out += ats_bmw(c, name)
            elif ats == "oracle":
                out += ats_oracle(c, name)
            elif ats == "phenom":
                out += ats_phenom(c, name)
            elif ats == "hr4you":
                out += ats_hr4you(c, name)
            elif ats in ATS_FETCHERS:
                out += ATS_FETCHERS[ats](c["slug"], name)
            else:
                print(f"  [跳过] {name}: 未知 ats '{ats}'")
                continue
            for _j in out[_n_before:]:
                _j.extra["cats"] = classify(_j, c.get("tags"))
            print(f"  [OK] {name} ({ats})")
        except Exception as e:
            print(f"  [失败] {name} ({ats}): {type(e).__name__} {e}")
        time.sleep(0.3)
    return out


# ----------------------------------------------------------------------------
# ATS 自动探测:给公司名,猜它用的哪套系统
# ----------------------------------------------------------------------------

PROBES = [
    ("personio",       lambda s: f"https://{s}.jobs.personio.de/xml"),
    ("greenhouse",     lambda s: f"https://boards-api.greenhouse.io/v1/boards/{s}/jobs"),
    ("lever",          lambda s: f"https://api.lever.co/v0/postings/{s}?mode=json"),
    ("smartrecruiters", lambda s: f"https://api.smartrecruiters.com/v1/companies/{s}/postings?limit=1"),
    ("recruitee",      lambda s: f"https://{s}.recruitee.com/api/offers/"),
    ("ashby",          lambda s: f"https://api.ashbyhq.com/posting-api/job-board/{s}"),
    ("workable",       lambda s: f"https://apply.workable.com/api/v1/widget/accounts/{s}?details=true"),
]


def cmd_discover(slugs):
    print("探测中(slug 通常是公司名小写去空格,例:BMW Group -> bmwgroup)\n")
    found = []
    for slug in slugs:
        hits = []
        for ats, mk in PROBES:
            url = mk(slug)
            try:
                r = _get(url, timeout=10)
                if r.status_code != 200 or len(r.content) < 40:
                    continue
                # 粗略数一下有多少职位,避免把空壳页面当成命中
                n = len(re.findall(r'"id"\s*:|<position>', r.text))
                hits.append((ats, n))
            except Exception:
                pass
        if hits:
            for ats, n in hits:
                print(f"  ✓ {slug:<24} {ats:<16} 约 {n} 个职位")
                found.append({"name": slug, "ats": ats, "slug": slug})
        else:
            print(f"  ✗ {slug:<24} 没探到 — 手动打开它的招聘页,看 URL 跳到哪个域名")
    if found:
        print("\n把下面这段贴进 companies.yaml 的 companies: 下面(注意缩进):\n")
        print(yaml.safe_dump(found, allow_unicode=True, sort_keys=False))


# ----------------------------------------------------------------------------
# 输出
# ----------------------------------------------------------------------------

CSS = """
:root{--bg:#faf9f7;--card:#fff;--tx:#1a1a18;--mut:#6b6862;--line:#e6e3dd;--acc:#b8562f}
*{box-sizing:border-box}
body{margin:0;padding:32px 20px;background:var(--bg);color:var(--tx);
     font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",sans-serif}
.wrap{max-width:860px;margin:0 auto}
h1{font-size:23px;margin:0 0 4px;letter-spacing:-.01em}
.sub{color:var(--mut);font-size:13px;margin-bottom:26px}
.grp{font-size:12px;text-transform:uppercase;letter-spacing:.08em;color:var(--mut);
     margin:28px 0 10px;padding-bottom:6px;border-bottom:1px solid var(--line)}
.j{background:var(--card);border:1px solid var(--line);border-radius:9px;
   padding:13px 15px;margin-bottom:8px}
.j a{color:var(--tx);text-decoration:none;font-weight:600}
.j a:hover{color:var(--acc)}
.meta{color:var(--mut);font-size:12.5px;margin-top:4px}
.chips{margin:6px 0 8px;line-height:2.3}
.chip{display:inline-block;color:#fff;border:none;border-radius:5px;padding:4px 10px;
      font-size:12px;font-weight:600;margin:0 6px 6px 0;cursor:pointer;opacity:.55;
      background:#6b7280;font-family:inherit}
.chip:hover{opacity:.85}
.chip.active{opacity:1;box-shadow:0 0 0 2px var(--tx)}
.filterinfo{font-size:12.5px;color:var(--mut);min-height:18px;margin-bottom:14px}
.chips .cat{margin:0 6px 0 0}
.cat{display:inline-block;color:#fff;border-radius:4px;padding:2px 7px;font-size:11px;
     font-weight:600;margin-left:6px;letter-spacing:.02em}
.tag{display:inline-block;background:#f0ede7;border-radius:4px;padding:1px 6px;
     font-size:11px;color:var(--mut);margin-left:6px}
.tag.igm{background:#2f7d32;color:#fff;font-weight:700;cursor:help;letter-spacing:.04em;
         padding:2px 7px}
.empty{color:var(--mut);padding:40px 0;text-align:center}
.sec2{font-size:14px;font-weight:600;margin:26px 0 6px;color:var(--mut);
      padding-bottom:6px;border-bottom:1px solid var(--line)}
.hint{font-weight:400;font-size:12px;margin-left:6px}
details summary{cursor:pointer;color:var(--acc);font-size:13px;margin-bottom:10px}
.sec{font-size:15px;font-weight:700;margin:30px 0 6px;padding-bottom:8px;
     border-bottom:2px solid var(--tx)}
.note{background:#fff6e8;border:1px solid #f0dcc0;border-radius:8px;padding:10px 13px;
      font-size:13px;color:#6b5533;margin-bottom:18px}
"""



# ----------------------------------------------------------------------------
# 岗位分类:按公司名 / 行业关键词打标签,一个岗位可有多个标签
# ----------------------------------------------------------------------------

CATEGORY_RULES = [
    ("半导体与精密制造", [
        "micron", "globalfoundries", "global foundries", "applied materials", "kla",
        "lam research", "asml", "amd", "intel", "tsmc", "umc", "infineon", "stmicro",
        "western digital", "seagate", "siltronic", "soitec", "amkor", "kulicke",
        "ultra clean", "entegris", "onto innovation", "nanofilm", "semiconductor",
        "wafer", "photonics", "precision engineering", "advanced micro", "murata",
        "tdk", "jabil", "venture corp", "advanced packaging", "hbm", "foundry",
    ]),
    ("航空与MRO", [
        "st engineering", "st engg", "singapore technologies", "rolls-royce", "rolls royce",
        "pratt & whitney", "pratt and whitney", "collins aerospace", "raytheon", "rtx",
        "safran", "thales", "honeywell aerospace", "gkn aerospace", "sia engineering",
        "singapore airlines", "airbus", "boeing", "eagle services", "jamco",
        "aerospace", "aviation", "mro", "aircraft", "airframe", "turbine", "engine overhaul",
    ]),
    ("医疗器械与光学", [
        "medtronic", "essilor", "luxottica", "alcon", "hoya", "zeiss", "carl zeiss",
        "becton", "baxter", "abbott", "johnson & johnson", "j&j", "stryker",
        "boston scientific", "siemens healthineers", "healthineers", "philips",
        "b. braun", "fresenius", "dentsply", "coloplast", "smith & nephew",
        "medical device", "medtech", "ophthalmic", "optical", "lens",
        "diagnostics", "biosensor", "thermo fisher", "agilent", "roche",
    ]),
    ("德企(非汽车)", [
        "siemens", "zeiss", "tuv sud", "tüv süd", "tuv rheinland", "rohde", "festo",
        "sick ag", "beckhoff", "trumpf", "sap ", "basf", "bayer", "merck kgaa",
        "henkel", "linde", "evonik", "covestro", "wacker", "lufthansa", "dhl",
        "siemens energy", "knorr-bremse",
    ]),
    ("⚠德国汽车供应链", [
        "bosch", "continental", "zf friedrichshafen", "zf group", "schaeffler",
        "webasto", "mahle", "hella", "brose", "vitesco", "thyssenkrupp automotive",
        "daimler", "mercedes", "bmw", "volkswagen", "audi", "porsche", "aumovio",
    ]),
    ("能源与可持续", [
        "shell", "exxonmobil", "keppel", "sembcorp", "vestas", "schneider electric",
        "abb ", "sunseap", "decarbon", "renewable", "hydrogen", "energy storage",
    ]),
    ("中资出海", [
        "bytedance", "tiktok", "shein", "temu", "alibaba", "lazada", "ant group",
        "tencent", "huawei", "xiaomi", "byd", "nio", "xpeng", "catl",
        "haier", "midea", "goertek", "luxshare", "shopee", "sea limited",
    ]),
]


# 只在公司名里匹配的词(品牌名);行业通名才允许在职位名里匹配
GENERIC_OK = {
    "semiconductor", "wafer", "photonics", "advanced packaging", "foundry",
    "aerospace", "aviation", "mro", "aircraft", "airframe", "turbine",
    "medical device", "medtech", "ophthalmic", "diagnostics",
    "renewable", "hydrogen", "decarbon",
}




# 德国本部企业(总标签,含汽车供应链;与"德企(非汽车)"并存,便于分别筛选)
GERMAN_HQ = [
    "siemens", "bosch", "robert bosch", "continental", "zf ", "zf friedrichshafen",
    "schaeffler", "thyssenkrupp", "zeiss", "carl zeiss", "tuv sud", "tüv süd",
    "tuv rheinland", "tuv nord", "dekra", "rohde", "festo", "sick ag", "beckhoff",
    "trumpf", "sap ", "sap se", "basf", "bayer", "merck kgaa", "henkel", "linde",
    "evonik", "covestro", "wacker", "lufthansa", "dhl", "deutsche post",
    "deutsche bank", "allianz", "munich re", "knorr-bremse", "man se", "man truck",
    "daimler", "mercedes", "bmw", "volkswagen", "audi", "porsche", "webasto",
    "mahle", "hella", "brose", "vitesco", "infineon", "siltronic", "aixtron",
    "jenoptik", "heraeus", "freudenberg", "kion", "dürr", "duerr", "gea group",
    "krones", "kuka", "leoni", "osram", "phoenix contact", "rational ag",
    "sartorius", "schott", "stihl", "voith", "wago", "wuerth", "würth",
    "b. braun", "boehringer", "fresenius", "siemens healthineers", "siemens energy",
    "rodenstock", "leica", "bosch rexroth", "harting", "hbm", "kärcher", "karcher",
    "miele", "liebherr", "claas", "fendt", "deutz", "rheinmetall", "hensoldt",
    "diehl", "eberspächer", "eberspaecher", "elringklinger", "norma group",
]


def is_german_hq(job):
    comp = f" {(job.company or '').lower()} "
    for n in GERMAN_HQ:
        n = n.strip()
        if len(n) <= 4:
            if f" {n} " in comp or comp.strip() == n:
                return True
        elif n in comp:
            return True
    return False


# ---- 三语/跨区域优势识别 ----
# 你的中文母语+德语+欧洲工程背景在这类岗位上价值最大
TRILINGUAL_HINTS = [
    "mandarin", "chinese", "putonghua", "greater china", "china market",
    "german", "germany", "deutsch", "europe", "european", "emea",
    "apac", "asia pacific", "regional", "cross-region", "cross region",
    "global headquarters", "hq ", "multinational", "japan", "korea",
]


def is_trilingual_fit(job):
    hay = " ".join([job.title or "", job.company or "",
                    job.extra.get("desc", "")]).lower()
    return any(h in hay for h in TRILINGUAL_HINTS)


def classify(job, company_tags=None):
    """公司在直连清单里的用预设标签;其余按公司名(品牌词)+职位名(行业通名)判断。"""
    tags = list(company_tags or [])
    comp = f" {job.company.lower()} "
    title = f" {job.title.lower()} "
    for cat, needles in CATEGORY_RULES:
        if cat in tags:
            continue
        for n in needles:
            n = n.strip()
            if len(n) < 3:
                continue
            # 短品牌名要求词边界,避免 "amd" 命中 "amdocs"
            hit_comp = (f" {n} " in comp or comp.strip() == n) if len(n) <= 4 else (n in comp)
            if hit_comp or (n in GENERIC_OK and n in title):
                tags.append(cat)
                break
    if is_german_hq(job) and "🇩🇪德国企业" not in tags:
        tags.append("🇩🇪德国企业")
    if is_trilingual_fit(job):
        tags.append("🌏三语/跨区域")
    return tags


TAG_COLORS = {
    "半导体与精密制造": "#1d4ed8",
    "航空与MRO": "#4338ca",
    "医疗器械与光学": "#047857",
    "德企(非汽车)": "#b45309",
    "能源与可持续": "#0f766e",
    "科技与平台": "#6b7280",
    "中资出海": "#9333ea",
    "⚠德国汽车供应链": "#9ca3af",
    "🌏三语/跨区域": "#7c3aed",
    "🇩🇪德国企业": "#a16207",
}


def _job_card(j):
    lbl = age_label(j)
    posted = f'<span class=tag>{html.escape(lbl)}</span>' if lbl else ""
    igm = "".join(
        f'<span class=cat style="background:{TAG_COLORS.get(c, "#6b7280")}">{html.escape(c)}</span>'
        for c in (j.extra.get("cats") or []))
    return (f'<div class=j data-cats="{html.escape("|".join(j.extra.get("cats") or []))}">'
            f'<a href="{html.escape(j.url)}" target=_blank rel=noopener>'
            f'{html.escape(j.title)}</a>{igm}{posted}'
            f'<div class=meta>{html.escape(j.company)} · '
            f'{html.escape(j.source)} · '
            f'{html.escape(j.location) or "地点未标注"}</div></div>')


def render_html(day_groups, new_today, total_seen, first_run, cfg, fallback=None):
    """只显示 max_age_days 天内的职位;若为空,退回显示最新的若干条并说明。"""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    disp = cfg.get("display") or {}
    maxage = int(disp.get("max_age_days", 5))
    allj = fallback or []

    PRIORITY = {"半导体与精密制造", "航空与MRO", "医疗器械与光学", "德企(非汽车)"}
    fresh, older, prio_all = [], [], []
    for j in allj:
        n, _ = job_age_days(j)
        (fresh if (n is not None and n <= maxage) else older).append(j)
        if PRIORITY & set(j.extra.get("cats") or []):
            prio_all.append(j)

    def by_age(js):
        return sorted(js, key=lambda x: (0 if x.extra.get("igm") else 1,
                                         job_age_days(x)[0] if job_age_days(x)[0] is not None else 999))

    def cards(js):
        return [_job_card(j) for j in by_age(js)]

    parts = ["<!doctype html><meta charset=utf-8>",
             '<meta name=viewport content="width=device-width,initial-scale=1">',
             "<title>新加坡新职位</title>", f"<style>{CSS}</style><div class=wrap>",
             "<h1>新加坡 · 新职位</h1>",
             f"<div class=sub>更新于 {ts} UTC · 最近 {maxage} 天 <b>{len(fresh)}</b> 条"
             f"· 库中累计 {total_seen} 条</div>"]

    cnt = {}
    for j in fresh:
        for c in (j.extra.get("cats") or []):
            cnt[c] = cnt.get(c, 0) + 1
    # 统计全部(不只 fresh),让筛选覆盖整页
    cnt_all = {}
    for j in (fresh + prio_all + older):
        for c in (j.extra.get("cats") or []):
            cnt_all[c] = cnt_all.get(c, 0) + 1
    if cnt_all:
        chips = ('<button class="chip active" data-f="__all__">全部</button>' +
                 "".join(
                     f'<button class=chip data-f="{html.escape(c)}" '
                     f'style="background:{TAG_COLORS.get(c, "#6b7280")}">'
                     f'{html.escape(c)} {n}</button>'
                     for c, n in sorted(cnt_all.items(), key=lambda kv: -kv[1])))
        parts.append(f'<div class=chips>{chips}</div>'
                     f'<div class=filterinfo id=finfo></div>')
    parts.append(f"<div class=sec>最近 {maxage} 天内的职位 · {len(fresh)} 条</div>")
    if fresh:
        parts += cards(fresh)
    else:
        parts.append(f'<div class=note>最近 {maxage} 天没有新职位(周末常见)。'
                     f'下面列出最新的一批供参考。</div>')
        parts += cards(older[:25])

    prio_all.sort(key=lambda j: (job_age_days(j)[0] if job_age_days(j)[0] is not None else 500))
    if prio_all:
        parts.append(f"<div class=sec>重点方向全部在招 · {len(prio_all)} 条"
                     f"<span class=hint>(半导体 / 航空MRO / 医疗光学 / 德企非汽车,不限日期)</span></div>")
        parts += cards(prio_all[:200])

    if fresh and older:
        parts.append(f'<div class=sec2>更早的职位 · {len(older)} 条'
                     f'<span class=hint>(超过 {maxage} 天,默认折叠)</span></div>')
        parts.append("<details><summary>展开查看</summary>")
        parts += cards(older[:150])
        parts.append("</details>")

    parts.append("</div>")
    parts.append("""
<script>
(function(){
  var chips=document.querySelectorAll('.chip');
  var cards=document.querySelectorAll('.j');
  var info=document.getElementById('finfo');
  function apply(f){
    var shown=0;
    cards.forEach(function(c){
      var cats=(c.getAttribute('data-cats')||'').split('|');
      var ok=(f==='__all__')||cats.indexOf(f)>=0;
      c.style.display=ok?'':'none';
      if(ok)shown++;
    });
    document.querySelectorAll('.sec,.sec2,.grp,.note,details').forEach(function(s){
      var n=s.nextElementSibling,any=false;
      while(n&&!n.classList.contains('sec')&&!n.classList.contains('sec2')&&!n.classList.contains('grp')){
        if(n.classList.contains('j')&&n.style.display!=='none'){any=true;break;}
        n=n.nextElementSibling;
      }
      s.style.display=(f==='__all__'||any)?'':'none';
    });
    info.textContent=(f==='__all__')?'':('已筛选:'+f+' — 显示 '+shown+' 条,再次点击「全部」恢复');
  }
  chips.forEach(function(b){
    b.addEventListener('click',function(){
      chips.forEach(function(x){x.classList.remove('active')});
      b.classList.add('active');
      apply(b.getAttribute('data-f'));
    });
  });
})();
</script>""")
    doc = "".join(parts)
    OUT_HTML.write_text(doc, encoding="utf-8")
    DOCS_HTML.parent.mkdir(exist_ok=True)
    DOCS_HTML.write_text(doc, encoding="utf-8")
    (DOCS_HTML.parent / ".nojekyll").touch()



def push_telegram(new_jobs, cfg):
    tg = cfg.get("telegram", {})
    # GitHub Actions 的 Secrets 通过环境变量进来,优先于 config
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or tg.get("bot_token")
    chat = os.environ.get("TELEGRAM_CHAT_ID") or tg.get("chat_id")
    env_enabled = bool(os.environ.get("TELEGRAM_BOT_TOKEN"))
    if not (tg.get("enabled") or env_enabled) or not new_jobs:
        return
    if not token or not chat:
        print("  [Telegram] 缺 bot_token 或 chat_id,跳过")
        return
    lines = [f"<b>新加坡新职位 {len(new_jobs)} 条</b>"]
    for j in new_jobs[:30]:
        lines.append(f'· <a href="{html.escape(j.url)}">{html.escape(j.title)}</a> — '
                     f'{html.escape(j.company)}')
    if len(new_jobs) > 30:
        lines.append(f"…另有 {len(new_jobs)-30} 条,见 digest.html")
    try:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat, "text": "\n".join(lines),
                            "parse_mode": "HTML",
                            "disable_web_page_preview": True}, timeout=20)
    except Exception as e:
        print(f"  [Telegram] 推送失败: {e}")


# ----------------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------------

def cmd_run(cfg):
    print("抓取 MyCareersFuture…")
    raw = fetch_mcf(cfg)
    print(f"  拿到 {len(raw)} 条")

    print("抓取公司 ATS…")
    company_jobs = fetch_companies()
    print(f"  拿到 {len(company_jobs)} 条")
    raw += company_jobs

    max_age = int((cfg.get("filters") or {}).get("max_posted_age_days", 365))
    for j in raw:
        if "cats" not in j.extra:
            j.extra["cats"] = classify(j)
    kept, stale = [], 0
    for j in raw:
        if not (matches_keywords(j, cfg["keywords"]) and matches_location(j, cfg["location"])):
            continue
        d = parse_posted(j.posted)
        if d and (datetime.now(timezone.utc).date() - d).days > max_age:
            stale += 1
            continue
        kept.append(j)
    if stale:
        print(f"丢弃过期职位 {stale} 条(发布超过 {max_age} 天)")
    print(f"\n过滤后 {len(kept)} / {len(raw)} 条符合关键词+地点")


    conn = db_connect()
    win = int((cfg.get("display") or {}).get("recent_days", 2))
    first = is_first_run(conn)
    new = split_new(conn, kept)
    total = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]

    if first:
        mark_seeded(conn)
        print(f"\n首次运行:已把现有 {len(new)} 条收作基线,不算新职位。")
        print("从下一次运行起,只会显示真正新增的。")
        render_html(recent_days(conn, win), 0, total, True, cfg,
                    fallback=all_current(conn))
    else:
        print(f"\n★ 新职位 {len(new)} 条")
        for j in new[:15]:
            print(f"  · {j.title[:60]} — {j.company[:30]}")
        if len(new) > 15:
            print(f"  …另有 {len(new)-15} 条")
        render_html(recent_days(conn, win), len(new), total, False, cfg,
                    fallback=all_current(conn))
        push_telegram(new, cfg)

    conn.close()
    print(f"\n报告已生成: {OUT_HTML}")
    if cfg.get("open_browser") and not IN_CI and (first or new):
        try:
            webbrowser.open(OUT_HTML.as_uri())
        except Exception:
            pass


def main():
    ap = argparse.ArgumentParser(description="慕尼黑职位每日差分监控")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("run", help="抓取并生成今日报告")
    d = sub.add_parser("discover", help="探测公司用的哪套 ATS")
    d.add_argument("slugs", nargs="+")
    sub.add_parser("stats")
    sub.add_parser("reset")
    args = ap.parse_args()

    if args.cmd == "discover":
        cmd_discover(args.slugs)
    elif args.cmd == "stats":
        conn = db_connect()
        n = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        print(f"数据库共 {n} 条职位")
        for r in conn.execute("SELECT source, COUNT(*) FROM jobs GROUP BY source ORDER BY 2 DESC"):
            print(f"  {r[0]:<20} {r[1]}")
    elif args.cmd == "reset":
        if DB_PATH.exists():
            DB_PATH.unlink()
        print("数据库已清空,下次 run 会重新建立基线。")
    else:
        cmd_run(load_config())


if __name__ == "__main__":
    main()
