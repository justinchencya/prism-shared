#!/usr/bin/env python3
"""
Fetch raw discovery signals for the Prism scout from free, no-auth sources.

Usage:
  fetch_signals.py <run-dir> <queries-json>

Reads:
  <queries-json>                        (per-run query spec the scout agent writes from the
                                         user's focus: gdelt_queries, hn_keywords,
                                         edgar_fts_terms, edgar_tickers, + optional
                                         hn_min_points / edgar_forms / lookback_days / focus)

Writes:
  <run-dir>/signals.json                (normalized signal records + run metadata)

Sources:
  - GDELT DOC 2.0   coverage-volume spike detection + representative articles (free, no key)
  - Hacker News     Algolia search: points/comment velocity for tech themes (free, no key)
  - SEC EDGAR       full-text search on focus terms + recent watchlist filings (free, no key)
  - X               recent posts from a curated follow list (needs X_BEARER_TOKEN, read
                    from the environment or <repo-root>/.env; if the token is unset the
                    source is simply skipped with a warning)

Design:
  - stdlib only (urllib). No third-party packages. Python 3.8+.
  - This is the *standardized data layer*. No LLM, no judgement — just normalized
    records the scout agent reasons over. Every source is wrapped so one dead or
    rate-limited endpoint degrades to a warning, never a crash.

Normalized record shape:
  {
    "source":        "gdelt" | "hn" | "edgar" | "x",
    "cluster_query": <the query/keyword/ticker/@handle that produced it>,
    "title":         str,
    "url":           str,
    "published":     ISO-8601 str or "",
    "metric_type":   "spike_ratio" | "velocity_pph" | "filing_recency_days" | "engagement_velocity",
    "metric_value":  float,
    "snippet":       str
  }
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

# SEC EDGAR requires a contact email in the User-Agent. Read it from the env so
# the engine carries no personal address; falls back to a generic placeholder.
UA = f"Prism scout ({os.environ.get('EDGAR_CONTACT_EMAIL', 'prism-user@example.com')})"
GDELT_DOC = "https://api.gdeltproject.org/api/v2/doc/doc"
HN_SEARCH = "https://hn.algolia.com/api/v1/search_by_date"
HN_FRONT = "https://hn.algolia.com/api/v1/search"
EDGAR_FTS = "https://efts.sec.gov/LATEST/search-index"
EDGAR_TICKERS = "https://www.sec.gov/files/company_tickers.json"
EDGAR_SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik}.json"
X_USER_BY = "https://api.x.com/2/users/by/username/{handle}"
X_USER_POSTS = "https://api.x.com/2/users/{uid}/tweets"


def log(msg: str) -> None:
    print(f"fetch_signals: {msg}", file=sys.stderr)


def die(msg: str, code: int = 1) -> None:
    log(msg)
    sys.exit(code)


def load_dotenv(repo_root: Path) -> None:
    """Load KEY=VALUE pairs from <repo-root>/.env into os.environ if not already set."""
    env_path = repo_root / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


def get_json(url: str, timeout: int = 15, retries: int = 0, backoff: float = 3.0,
             headers: dict = None) -> dict:
    """GET a URL and parse JSON. Raises on HTTP / parse error; caller decides fatality.

    retries>0 adds linear backoff between attempts — used for GDELT, which rate-limits
    aggressively (HTTP 429 / connection reset) and sometimes returns a non-JSON body.
    Timeouts and retry counts are kept small on purpose: when a source is throttling, we
    want to degrade to a warning in seconds, not hang for minutes.

    headers merges extra request headers over the defaults (e.g. an X API bearer token)."""
    hdrs = {"User-Agent": UA, "Accept": "application/json"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, headers=hdrs)
    last_err = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw)
        except urllib.error.HTTPError as e:
            # Non-transient codes won't resolve on retry: 429 (throttled this window),
            # 401/403 (bad/insufficient auth), 402 (no API credits). Bail fast.
            if e.code in (401, 402, 403, 429):
                raise
            last_err = e
            if attempt < retries:
                time.sleep(backoff * (attempt + 1))
        except Exception as e:  # noqa: BLE001 — retry on any other transient failure
            last_err = e
            if attempt < retries:
                time.sleep(backoff * (attempt + 1))
    raise last_err


# Defaults applied when the per-run query spec omits a field. The scout agent builds the
# query lists from the user's focus; these knobs rarely need per-run tuning.
DEFAULT_LOOKBACK = 7
DEFAULT_MIN_POINTS = 100
DEFAULT_EDGAR_FORMS = ["8-K", "S-1", "424B4", "10-K", "10-Q"]


def load_queries(path: Path) -> dict:
    if not path.exists():
        die(f"query spec not found at {path} — the scout agent writes this before fetching")
    with open(path) as f:
        return json.load(f)


# --------------------------------------------------------------------------- #
# GDELT — coverage-volume spike detection + representative articles
# --------------------------------------------------------------------------- #

GDELT_BUDGET_S = 90  # hard wall-clock cap on the whole GDELT phase


def gdelt_signals(queries: list, warnings: list) -> list:
    """For each query: a volume spike-ratio summary record + a few example articles.

    The phase is bounded by GDELT_BUDGET_S: if GDELT is throttling us into slow failures,
    we stop after the budget and emit a warning rather than hanging the whole fetch."""
    records: list = []
    deadline = time.monotonic() + GDELT_BUDGET_S
    for q in queries:
        if not q or not str(q).strip():
            continue
        if time.monotonic() > deadline:
            warnings.append("gdelt phase budget exceeded — skipped remaining queries "
                            "(likely rate-limited; try again later)")
            break
        q = str(q).strip()
        try:
            ratio, recent_n, base_n = gdelt_spike(q)
        except Exception as e:  # noqa: BLE001 — one bad query must not kill the rest
            warnings.append(f"gdelt volume query failed ({q!r}): {e}")
            continue

        records.append({
            "source": "gdelt",
            "cluster_query": q,
            "title": f"Coverage-volume signal: {q}",
            "url": gdelt_ui_url(q),
            "published": now_iso(),
            "metric_type": "spike_ratio",
            "metric_value": round(ratio, 2),
            "snippet": (f"Recent 3d avg article count {recent_n:.0f} vs trailing baseline "
                        f"{base_n:.0f} over 14d (ratio {ratio:.2f}x)."),
        })

        try:
            for art in gdelt_articles(q):
                art["metric_value"] = round(ratio, 2)  # articles carry their query's spike
                records.append(art)
        except Exception as e:  # noqa: BLE001
            warnings.append(f"gdelt artlist failed ({q!r}): {e}")
        time.sleep(2.5)  # be polite to GDELT (it rate-limits hard)
    return records


def gdelt_spike(q: str) -> tuple:
    """Return (spike_ratio, recent_avg, baseline_avg) from a 14d daily volume timeline."""
    params = {
        "query": q,
        "mode": "timelinevolraw",
        "timespan": "14d",
        "format": "json",
    }
    data = get_json(f"{GDELT_DOC}?{urllib.parse.urlencode(params)}", retries=1)
    series = data.get("timeline") or []
    if not series:
        raise ValueError("empty timeline")
    points = series[0].get("data") or []
    values = [float(p.get("value", 0)) for p in points]
    if len(values) < 4:
        raise ValueError(f"too few timeline points ({len(values)})")
    recent = values[-3:]
    baseline = values[:-3]
    recent_avg = sum(recent) / len(recent)
    base_avg = sum(baseline) / len(baseline)
    ratio = recent_avg / base_avg if base_avg > 0 else (recent_avg if recent_avg else 0.0)
    return ratio, recent_avg, base_avg


def gdelt_articles(q: str, maxrecords: int = 8) -> list:
    params = {
        "query": q,
        "mode": "artlist",
        "maxrecords": str(maxrecords),
        "timespan": "3d",
        "sort": "hybridrel",
        "format": "json",
    }
    data = get_json(f"{GDELT_DOC}?{urllib.parse.urlencode(params)}", retries=1)
    out = []
    for a in data.get("articles", []):
        out.append({
            "source": "gdelt",
            "cluster_query": q,
            "title": a.get("title", "").strip(),
            "url": a.get("url", ""),
            "published": gdelt_date(a.get("seendate", "")),
            "metric_type": "spike_ratio",
            "metric_value": 0.0,  # filled in by caller with the query's ratio
            "snippet": f"{a.get('domain', '')} · {a.get('sourcecountry', '')}".strip(" ·"),
        })
    return out


def gdelt_date(s: str) -> str:
    # GDELT seendate: 20260528T143000Z
    try:
        return datetime.strptime(s, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).isoformat()
    except (ValueError, TypeError):
        return ""


def gdelt_ui_url(q: str) -> str:
    params = {"query": q, "mode": "artlist", "timespan": "3d", "format": "html"}
    return f"{GDELT_DOC}?{urllib.parse.urlencode(params)}"


# --------------------------------------------------------------------------- #
# Hacker News — points/comment velocity (Algolia, no auth)
# --------------------------------------------------------------------------- #

def hn_signals(keywords: list, min_points: int, lookback_days: int, warnings: list) -> list:
    records: list = []
    cutoff = int(time.time()) - lookback_days * 86400

    # Keyword searches, sorted by recency, filtered to a points floor.
    for kw in keywords:
        if not kw or not str(kw).strip():
            continue
        kw = str(kw).strip()
        params = {
            "query": kw,
            "tags": "story",
            "numericFilters": f"points>={min_points},created_at_i>{cutoff}",
            "hitsPerPage": "20",
        }
        try:
            data = get_json(f"{HN_SEARCH}?{urllib.parse.urlencode(params)}")
        except Exception as e:  # noqa: BLE001
            warnings.append(f"hn search failed ({kw!r}): {e}")
            continue
        records.extend(hn_hits_to_records(data.get("hits", []), kw))
        time.sleep(0.3)

    # Current front page — broad tech signal, independent of keywords.
    try:
        data = get_json(f"{HN_FRONT}?{urllib.parse.urlencode({'tags': 'front_page', 'hitsPerPage': '30'})}")
        records.extend(hn_hits_to_records(data.get("hits", []), "front_page"))
    except Exception as e:  # noqa: BLE001
        warnings.append(f"hn front_page failed: {e}")

    return records


def hn_hits_to_records(hits: list, cluster: str) -> list:
    out = []
    now = time.time()
    for h in hits:
        created = h.get("created_at_i")
        points = h.get("points") or 0
        comments = h.get("num_comments") or 0
        age_h = max((now - created) / 3600.0, 0.5) if created else 1.0
        velocity = points / age_h
        object_id = h.get("objectID", "")
        url = h.get("url") or f"https://news.ycombinator.com/item?id={object_id}"
        out.append({
            "source": "hn",
            "cluster_query": cluster,
            "title": (h.get("title") or "").strip(),
            "url": url,
            "published": iso_from_epoch(created),
            "metric_type": "velocity_pph",
            "metric_value": round(velocity, 1),
            "snippet": f"{points} points, {comments} comments in {age_h:.0f}h "
                       f"(discussion: https://news.ycombinator.com/item?id={object_id})",
        })
    return out


# --------------------------------------------------------------------------- #
# SEC EDGAR — full-text search on focus terms + recent watchlist filings
# --------------------------------------------------------------------------- #

def edgar_signals(edgar_cfg: dict, fts_terms: list, lookback_days: int, warnings: list) -> list:
    records: list = []
    forms = edgar_cfg.get("forms") or ["8-K", "S-1"]
    today = datetime.now(timezone.utc).date()
    startdt = (today.toordinal() - lookback_days)
    startdt = datetime.fromordinal(startdt).date().isoformat()
    enddt = today.isoformat()

    # Full-text search on focus terms (catches themes regardless of who filed).
    for term in fts_terms:
        if not term or not str(term).strip():
            continue
        term = str(term).strip()
        params = {
            "q": f'"{term}"',
            "forms": ",".join(forms),
            "startdt": startdt,
            "enddt": enddt,
        }
        try:
            data = get_json(f"{EDGAR_FTS}?{urllib.parse.urlencode(params)}")
        except Exception as e:  # noqa: BLE001
            warnings.append(f"edgar FTS failed ({term!r}): {e}")
            continue
        records.extend(edgar_fts_to_records(data, term))
        time.sleep(0.3)

    # Recent filings for watchlist tickers (name-level catalysts).
    tickers = edgar_cfg.get("tickers") or []
    if tickers:
        try:
            cik_map = load_cik_map()
        except Exception as e:  # noqa: BLE001
            warnings.append(f"edgar ticker→CIK map failed: {e}")
            cik_map = {}
        for tk in tickers:
            tk = str(tk).strip().upper()
            cik = cik_map.get(tk)
            if not cik:
                warnings.append(f"edgar: no CIK for ticker {tk}")
                continue
            try:
                records.extend(edgar_recent_filings(tk, cik, forms, lookback_days))
            except Exception as e:  # noqa: BLE001
                warnings.append(f"edgar submissions failed ({tk}): {e}")
            time.sleep(0.3)

    return records


def edgar_fts_to_records(data: dict, term: str) -> list:
    out = []
    hits = (data.get("hits") or {}).get("hits") or []
    for h in hits[:10]:
        src = h.get("_source", {})
        names = src.get("display_names") or []
        # `form` is the filing form (e.g. "8-K"); `file_type` is the document/exhibit
        # type (e.g. "EX-99.1"), which is not what we want to surface.
        form = src.get("form") or (src.get("root_forms") or [""])[0] or ""
        filed = src.get("file_date", "")
        recency = days_since(filed)
        out.append({
            "source": "edgar",
            "cluster_query": term,
            "title": f"{form} mentioning '{term}': {'; '.join(names) if names else 'filer unknown'}",
            "url": edgar_doc_url(h.get("_id", "")),
            "published": filed,
            "metric_type": "filing_recency_days",
            "metric_value": float(recency),
            "snippet": f"EDGAR full-text hit ({form}) filed {filed}.",
        })
    return out


def edgar_recent_filings(ticker: str, cik: str, forms: list, lookback_days: int) -> list:
    data = get_json(EDGAR_SUBMISSIONS.format(cik=cik))
    recent = (data.get("filings") or {}).get("recent") or {}
    form_list = recent.get("form") or []
    dates = recent.get("filingDate") or []
    accns = recent.get("accessionNumber") or []
    docs = recent.get("primaryDocument") or []
    forms_set = {f.upper() for f in forms}
    out = []
    for i, form in enumerate(form_list):
        if form.upper() not in forms_set:
            continue
        filed = dates[i] if i < len(dates) else ""
        recency = days_since(filed)
        if recency > lookback_days:
            continue
        accn = accns[i] if i < len(accns) else ""
        doc = docs[i] if i < len(docs) else ""
        out.append({
            "source": "edgar",
            "cluster_query": ticker,
            "title": f"{ticker} filed {form}",
            "url": edgar_archive_url(cik, accn, doc),
            "published": filed,
            "metric_type": "filing_recency_days",
            "metric_value": float(recency),
            "snippet": f"Watchlist filing: {ticker} {form} on {filed}.",
        })
    return out


_CIK_CACHE: dict = {}


def load_cik_map() -> dict:
    if _CIK_CACHE:
        return _CIK_CACHE
    data = get_json(EDGAR_TICKERS)
    for entry in data.values():
        tk = str(entry.get("ticker", "")).upper()
        cik = str(entry.get("cik_str", "")).zfill(10)
        if tk:
            _CIK_CACHE[tk] = cik
    return _CIK_CACHE


def edgar_doc_url(_id: str) -> str:
    # _id like "0000320193-26-000123:aapl-20260101.htm"
    if not _id:
        return "https://efts.sec.gov/LATEST/search-index"
    accn = _id.split(":")[0]
    doc = _id.split(":")[1] if ":" in _id else ""
    cik_guess = accn.split("-")[0].lstrip("0") or "0"
    nodash = accn.replace("-", "")
    return f"https://www.sec.gov/Archives/edgar/data/{cik_guess}/{nodash}/{doc}"


def edgar_archive_url(cik: str, accn: str, doc: str) -> str:
    cik_int = str(int(cik)) if cik.isdigit() else cik
    nodash = accn.replace("-", "")
    return f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{nodash}/{doc}"


# --------------------------------------------------------------------------- #
# X — recent posts from a curated follow list (X API v2, bearer auth)
# --------------------------------------------------------------------------- #

def x_fatal_code(e: Exception):
    """HTTP status that dooms the whole X source this run — bad/insufficient auth (401/403),
    no credits (402), or rate-limited for the window (429). None for per-handle errors."""
    if isinstance(e, urllib.error.HTTPError) and e.code in (401, 402, 403, 429):
        return e.code
    return None


def x_signals(handles: list, lookback_days: int, warnings: list) -> list:
    """Recent original posts (no reposts/replies) from each followed account.

    Unlike the other sources this one needs auth: a bearer token in X_BEARER_TOKEN.
    If the token is unset the source degrades to a single warning and returns nothing,
    so a scout run without X configured behaves exactly as before. Each handle is wrapped
    so one bad account never kills the rest — but an auth/credit/rate-limit status
    (x_fatal_code) would fail every remaining handle the same way, so it aborts the
    source with a single warning instead of one per account."""
    if not handles:
        return []
    token = os.environ.get("X_BEARER_TOKEN", "").strip()
    if not token:
        warnings.append("x: X_BEARER_TOKEN not set (env or .env) — skipped X source")
        return []

    auth = {"Authorization": f"Bearer {token}"}
    start_time = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    records: list = []
    for h in handles:
        handle = str(h).strip().lstrip("@")
        if not handle:
            continue
        try:
            u = get_json(X_USER_BY.format(handle=urllib.parse.quote(handle)),
                         retries=1, headers=auth)
            uid = (u.get("data") or {}).get("id")
        except Exception as e:  # noqa: BLE001 — one bad handle must not kill the rest
            code = x_fatal_code(e)
            if code:
                warnings.append(f"x: HTTP {code} on @{handle} — aborting X source "
                                "(bad token, insufficient API tier, or rate-limited)")
                break
            warnings.append(f"x: user lookup failed (@{handle}): {e}")
            continue
        if not uid:
            warnings.append(f"x: no user id for @{handle}")
            continue

        params = {
            "max_results": "50",
            "start_time": start_time,
            "exclude": "retweets,replies",
            "tweet.fields": "created_at,public_metrics,entities",
        }
        try:
            data = get_json(f"{X_USER_POSTS.format(uid=uid)}?{urllib.parse.urlencode(params)}",
                            retries=1, headers=auth)
        except Exception as e:  # noqa: BLE001
            code = x_fatal_code(e)
            if code:
                warnings.append(f"x: HTTP {code} on @{handle} — aborting X source "
                                "(bad token, insufficient API tier, or rate-limited)")
                break
            warnings.append(f"x: timeline failed (@{handle}): {e}")
            continue
        records.extend(x_posts_to_records(data.get("data", []), handle))
        time.sleep(1.0)  # be polite between accounts
    return records


def x_posts_to_records(posts: list, handle: str) -> list:
    out = []
    now = time.time()
    for t in posts:
        text = (t.get("text") or "").strip()
        pm = t.get("public_metrics") or {}
        likes = pm.get("like_count", 0)
        reposts = pm.get("retweet_count", 0)
        quotes = pm.get("quote_count", 0)
        created = t.get("created_at", "")
        age_h = x_age_hours(created, now)
        velocity = (likes + reposts + quotes) / age_h
        tid = t.get("id", "")
        # External links the post points at — usually the real research seed (a filing,
        # transcript, dataset), not the post itself.
        links = []
        for ent in ((t.get("entities") or {}).get("urls") or []):
            exp = ent.get("expanded_url") or ""
            if exp and "x.com" not in exp and "twitter.com" not in exp:
                links.append(exp)
        link_str = (" · links: " + ", ".join(dict.fromkeys(links))) if links else ""
        out.append({
            "source": "x",
            "cluster_query": f"@{handle}",
            "title": x_first_line(text),
            "url": f"https://x.com/{handle}/status/{tid}",
            "published": created,
            "metric_type": "engagement_velocity",
            "metric_value": round(velocity, 1),
            "snippet": f"{text}  [likes={likes} reposts={reposts} quotes={quotes}]{link_str}",
        })
    return out


def x_first_line(text: str) -> str:
    line = text.splitlines()[0] if text else ""
    return (line[:117] + "…") if len(line) > 118 else line


def x_age_hours(created: str, now: float) -> float:
    try:
        dt = datetime.strptime(created[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        return max((now - dt.timestamp()) / 3600.0, 0.5)
    except (ValueError, TypeError):
        return 1.0


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def iso_from_epoch(epoch) -> str:
    try:
        return datetime.fromtimestamp(int(epoch), tz=timezone.utc).isoformat()
    except (ValueError, TypeError):
        return ""


def days_since(date_str: str) -> int:
    try:
        d = datetime.strptime(date_str[:10], "%Y-%m-%d").date()
        return max((datetime.now(timezone.utc).date() - d).days, 0)
    except (ValueError, TypeError):
        return 9999


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def main() -> None:
    if len(sys.argv) != 3:
        die("usage: fetch_signals.py <run-dir> <queries-json>")
    load_dotenv(Path(__file__).resolve().parent.parent)
    run_dir = Path(sys.argv[1]).resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    spec = load_queries(Path(sys.argv[2]).resolve())

    focus = spec.get("focus", "")
    lookback = int(spec.get("lookback_days", DEFAULT_LOOKBACK))
    warnings: list = []
    records: list = []

    log("fetching GDELT volume signals…")
    records += gdelt_signals(spec.get("gdelt_queries", []), warnings)

    log("fetching Hacker News signals…")
    records += hn_signals(
        spec.get("hn_keywords", []),
        int(spec.get("hn_min_points", DEFAULT_MIN_POINTS)),
        lookback,
        warnings,
    )

    log("fetching SEC EDGAR signals…")
    edgar_cfg = {
        "forms": spec.get("edgar_forms") or DEFAULT_EDGAR_FORMS,
        "tickers": spec.get("edgar_tickers", []),
    }
    records += edgar_signals(
        edgar_cfg,
        spec.get("edgar_fts_terms", []),
        lookback,
        warnings,
    )

    log("fetching X signals…")
    records += x_signals(spec.get("x_handles", []), lookback, warnings)

    out = {
        "generated_at": now_iso(),
        "focus": focus,
        "lookback_days": lookback,
        "counts": {
            "gdelt": sum(1 for r in records if r["source"] == "gdelt"),
            "hn": sum(1 for r in records if r["source"] == "hn"),
            "edgar": sum(1 for r in records if r["source"] == "edgar"),
            "x": sum(1 for r in records if r["source"] == "x"),
            "total": len(records),
        },
        "warnings": warnings,
        "signals": records,
    }
    out_path = run_dir / "signals.json"
    out_path.write_text(json.dumps(out, indent=2))

    for w in warnings:
        log(f"WARN {w}")
    log(f"wrote {len(records)} signals "
        f"(gdelt={out['counts']['gdelt']} hn={out['counts']['hn']} "
        f"edgar={out['counts']['edgar']} x={out['counts']['x']}) "
        f"to {out_path}")
    print(str(out_path))


if __name__ == "__main__":
    main()
