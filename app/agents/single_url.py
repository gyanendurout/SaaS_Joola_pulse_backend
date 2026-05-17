"""Single-URL analysis pipeline — 10 steps + optional gap analysis.

Steps:
  1. fetch            — HTTP GET the URL
  2. parse            — extract title, meta, headings, links, schema, images
  3. issues           — rule-based SEO issue detection
  4. entities         — LLM identifies products / categories / topics
  5. keywords         — DataForSEO keyword ideas seeded from entities
  6. serp             — SERP top results for top keywords
  7. recommendations  — LLM generates actionable recommendations
  8. ranked_keywords  — what the domain already ranks for (DataForSEO)
  9. competitors      — organic competitor domains (DataForSEO)
 10. backlinks        — domain backlink profile (DataForSEO)
 11. gap_analysis     — delta vs previous run (only on re-analyses)

All steps persist to Supabase for the dashboard view.
"""
from __future__ import annotations

import json
import time
from typing import Any
from urllib.parse import urlparse
from uuid import UUID, uuid4

import structlog
import tldextract

from app.config import get_settings
from app.db import service_client
from app.services import event_bus, storage
from app.services.crawler import fetch, make_client
from app.services.dataforseo import (
    backlinks_overview,
    competitors_domain,
    keyword_ideas,
    ranked_keywords,
    serp_organic,
)
from app.services.llm import chat_json
from app.services.page_classifier import classify
from app.services.parser import parse_html

log = structlog.get_logger()

# ---------------- Step IDs ----------------
STEP_FETCH = "fetch"
STEP_PARSE = "parse"
STEP_ISSUES = "issues"
STEP_ENTITIES = "entities"
STEP_KEYWORDS = "keywords"
STEP_SERP = "serp"
STEP_RECOMMENDATIONS = "recommendations"
STEP_RANKED_KW = "ranked_keywords"
STEP_COMPETITORS = "competitors"
STEP_BACKLINKS = "backlinks"
STEP_GAP = "gap_analysis"

STEP_DEFINITIONS_BASE: list[dict[str, str]] = [
    {"id": STEP_FETCH,           "title": "Fetch page",              "description": "Download the URL"},
    {"id": STEP_PARSE,           "title": "Parse content",           "description": "Extract title, meta, headings, links, schema"},
    {"id": STEP_ISSUES,          "title": "Detect SEO issues",       "description": "Run rule-based checks"},
    {"id": STEP_ENTITIES,        "title": "Identify products & topics", "description": "AI reads the page and identifies what's being sold"},
    {"id": STEP_KEYWORDS,        "title": "Keyword opportunities",   "description": "Pull keyword ideas from Google data"},
    {"id": STEP_SERP,            "title": "SERP comparison",         "description": "Who's ranking and what you're missing"},
    {"id": STEP_RECOMMENDATIONS, "title": "AI recommendations",      "description": "Concrete fixes prioritized by impact"},
    {"id": STEP_RANKED_KW,       "title": "Domain rankings",         "description": "Keywords your domain currently ranks for"},
    {"id": STEP_COMPETITORS,     "title": "Competitor domains",      "description": "Organic competitors with keyword overlap"},
    {"id": STEP_BACKLINKS,       "title": "Backlink profile",        "description": "Referring domains, domain rank, backlink count"},
]

STEP_DEF_GAP = {"id": STEP_GAP, "title": "Gap analysis", "description": "What changed since the previous analysis"}


# ---------------- Single-page issue rules ----------------
def _detect_issues_single_page(parsed, page_type: str) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    title = parsed.title or ""
    desc = parsed.meta_description or ""
    h1 = parsed.h1 or []
    word_count = parsed.word_count or 0

    if not title:
        issues.append({"code": "MISSING_TITLE", "severity": "critical",
                       "details": {}, "recommendation": "Add a <title> tag, 30-60 chars, with the primary keyword."})
    elif len(title) < 30:
        issues.append({"code": "TITLE_TOO_SHORT", "severity": "medium",
                       "details": {"length": len(title), "min": 30},
                       "recommendation": "Lengthen title to 30-60 chars; add the primary keyword."})
    elif len(title) > 65:
        issues.append({"code": "TITLE_TOO_LONG", "severity": "low",
                       "details": {"length": len(title), "max": 65},
                       "recommendation": "Shorten title to under 65 chars to avoid Google truncation."})

    if not desc:
        issues.append({"code": "MISSING_META_DESC", "severity": "high",
                       "details": {}, "recommendation": "Add a meta description, 120-160 chars, summarizing the page."})
    elif len(desc) > 160:
        issues.append({"code": "META_DESC_TOO_LONG", "severity": "low",
                       "details": {"length": len(desc), "max": 160},
                       "recommendation": "Trim meta description to under 160 chars."})

    if not h1:
        issues.append({"code": "MISSING_H1", "severity": "high",
                       "details": {}, "recommendation": "Add a single <h1> with the primary keyword."})
    elif len(h1) > 1:
        issues.append({"code": "MULTIPLE_H1", "severity": "medium",
                       "details": {"count": len(h1)},
                       "recommendation": "Use only one <h1> per page; demote the rest to <h2>."})

    thin_threshold = 200 if page_type in ("product", "category", "home") else 300
    if word_count < thin_threshold:
        issues.append({"code": "THIN_CONTENT", "severity": "high",
                       "details": {"word_count": word_count, "min": thin_threshold},
                       "recommendation": f"Expand content to at least {thin_threshold} words. Add product details, FAQs, buying guide."})

    if not parsed.canonical:
        issues.append({"code": "MISSING_CANONICAL", "severity": "medium",
                       "details": {}, "recommendation": "Add <link rel='canonical'> to declare the preferred URL."})

    if parsed.robots_meta and "noindex" in parsed.robots_meta.lower():
        issues.append({"code": "NOINDEX_ON_IMPORTANT_PAGE", "severity": "critical",
                       "details": {"robots": parsed.robots_meta},
                       "recommendation": "Remove 'noindex' so search engines can index this page."})

    total_imgs = len(parsed.image_urls or [])
    if total_imgs > 0 and parsed.images_missing_alt / total_imgs > 0.3:
        issues.append({"code": "IMAGES_MISSING_ALT", "severity": "medium",
                       "details": {"missing": parsed.images_missing_alt, "total": total_imgs},
                       "recommendation": "Add descriptive alt text to all images."})

    expected_schema = {
        "product": "Product",
        "category": "ItemList",
        "blog": "BlogPosting",
        "article": "Article",
        "faq": "FAQPage",
    }
    expected = expected_schema.get(page_type)
    if expected and expected not in (parsed.schema_types or []):
        issues.append({"code": "MISSING_SCHEMA_FOR_PAGE_TYPE", "severity": "medium",
                       "details": {"expected": expected, "found": parsed.schema_types or []},
                       "recommendation": f"Add {expected} JSON-LD schema."})

    return issues


# ---------------- Entry point ----------------
async def run_single_url_analysis(run_id: UUID) -> None:
    rid = str(run_id)
    db = service_client()
    settings = get_settings()

    try:
        run_res = db.table("runs").select("*").eq("id", rid).single().execute()
        if not run_res.data:
            event_bus.publish(rid, "error", message=f"Run {rid} not found")
            return
        run = run_res.data

        url: str = run["website_url"]
        market: str = run["market"]
        language: str = run["language"]
        previous_run_id: str | None = run.get("previous_run_id")

        # Build step list — gap_analysis only for re-analyses
        step_defs = list(STEP_DEFINITIONS_BASE)
        if previous_run_id:
            step_defs.append(STEP_DEF_GAP)

        db.table("runs").update({"status": "running", "started_at": _now_iso()}).eq("id", rid).execute()
        event_bus.publish(rid, "started", url=url, market=market, language=language, steps=step_defs)

        # ------ STEP 1: FETCH ------
        event_bus.publish(rid, "step.start", step=STEP_FETCH, message=f"Fetching {url}")
        t0 = time.time()
        async with make_client(settings.user_agent, settings.crawl_request_timeout_sec) as client:
            event_bus.publish(rid, "step.progress", step=STEP_FETCH, message="Connecting...")
            fetched = await fetch(url, client)
        elapsed_ms = int((time.time() - t0) * 1000)

        if not fetched.ok:
            event_bus.publish(rid, "step.error", step=STEP_FETCH,
                              error=fetched.error or f"HTTP {fetched.status}")
            await _fail_run(db, rid, f"Fetch failed: {fetched.error or fetched.status}")
            return

        event_bus.publish(rid, "step.progress", step=STEP_FETCH,
                          message=f"Got HTTP {fetched.status} in {elapsed_ms}ms ({len(fetched.text)} bytes)")
        event_bus.publish(rid, "step.complete", step=STEP_FETCH, result={
            "url": fetched.url,
            "final_url": fetched.final_url,
            "status": fetched.status,
            "redirect_chain": fetched.redirect_chain,
            "elapsed_ms": elapsed_ms,
            "size_bytes": len(fetched.text),
            "fetcher": fetched.fetcher,
        })

        # ------ STEP 2: PARSE ------
        event_bus.publish(rid, "step.start", step=STEP_PARSE, message="Parsing HTML...")
        base_domain = _base_domain(fetched.final_url)
        parsed = parse_html(fetched.text, fetched.final_url, base_domain)
        page_type = classify(fetched.final_url, parsed)
        event_bus.publish(rid, "step.progress", step=STEP_PARSE, message=f"Page type: {page_type}")
        event_bus.publish(rid, "step.progress", step=STEP_PARSE,
                          message=f"Title: {(parsed.title or '(missing)')[:80]}")
        event_bus.publish(rid, "step.progress", step=STEP_PARSE,
                          message=f"Headings: {len(parsed.h1)} H1, {len(parsed.h2)} H2, {len(parsed.h3)} H3")
        event_bus.publish(rid, "step.progress", step=STEP_PARSE,
                          message=f"Links: {len(parsed.internal_links)} internal, {len(parsed.external_links)} external")
        event_bus.publish(rid, "step.progress", step=STEP_PARSE,
                          message=f"Schema types: {', '.join(parsed.schema_types) or '(none)'}")
        page_id = _persist_page(db, rid, fetched, parsed, page_type)
        if page_id:
            try:
                storage.write_html(rid, page_id, fetched.text)
            except Exception as e:
                log.warning("html_storage_failed", error=str(e))
        event_bus.publish(rid, "step.complete", step=STEP_PARSE, result={
            "page_id": page_id,
            "page_type": page_type,
            "title": parsed.title,
            "meta_description": parsed.meta_description,
            "h1": parsed.h1,
            "h2": parsed.h2[:10],
            "h3": parsed.h3[:10],
            "word_count": parsed.word_count,
            "canonical": parsed.canonical,
            "is_indexable": parsed.is_indexable,
            "schema_types": parsed.schema_types,
            "open_graph": parsed.open_graph,
            "image_count": len(parsed.image_urls),
            "images_missing_alt": parsed.images_missing_alt,
            "internal_link_count": len(parsed.internal_links),
            "external_link_count": len(parsed.external_links),
        })

        # ------ STEP 3: ISSUES ------
        event_bus.publish(rid, "step.start", step=STEP_ISSUES, message="Running SEO rules...")
        issue_records = _detect_issues_single_page(parsed, page_type)
        for issue in issue_records:
            event_bus.publish(rid, "step.progress", step=STEP_ISSUES,
                              message=f"[{issue['severity'].upper()}] {issue['code']}",
                              detail=issue.get("recommendation"))
        _persist_issues(db, rid, page_id, issue_records)
        event_bus.publish(rid, "step.complete", step=STEP_ISSUES, result={
            "count": len(issue_records),
            "by_severity": _count_by(issue_records, "severity"),
            "issues": issue_records,
        })

        # ------ STEP 4: ENTITIES (LLM) ------
        event_bus.publish(rid, "step.start", step=STEP_ENTITIES,
                          message="Asking AI to identify products and topics...")
        entities_data = await _extract_entities_llm(parsed, fetched.final_url, market, language)
        for ent in entities_data.get("entities", []):
            event_bus.publish(rid, "step.progress", step=STEP_ENTITIES,
                              message=f"[{ent['entity_type']}] {ent['name']}",
                              detail=f"confidence {ent.get('confidence', 0):.2f}")
        entity_ids = _persist_entities(db, rid, page_id, entities_data.get("entities", []))
        event_bus.publish(rid, "step.complete", step=STEP_ENTITIES, result={
            "business_summary": entities_data.get("business_summary"),
            "business_type": entities_data.get("business_type"),
            "entities": entities_data.get("entities", []),
            "buyer_personas": entities_data.get("buyer_personas", []),
            "seed_keywords": entities_data.get("seed_keywords", []),
            "topic_clusters": entities_data.get("topic_clusters", []),
        })

        # ------ STEP 5: KEYWORDS (DataForSEO) ------
        event_bus.publish(rid, "step.start", step=STEP_KEYWORDS,
                          message="Fetching keyword opportunities from Google...")

        # Priority: user-curated seeds (saved from dashboard) > LLM-generated > entity names.
        user_seeds = run.get("seed_keywords") or []
        if user_seeds:
            seeds = [s for s in user_seeds if s and s.strip()][:15]
            event_bus.publish(rid, "step.progress", step=STEP_KEYWORDS,
                              message=f"Using {len(seeds)} user-curated seed keyword(s) from the dashboard")
        else:
            seeds = (entities_data.get("seed_keywords") or [])[:10]
            if not seeds:
                seeds = [e["name"] for e in (entities_data.get("entities") or [])[:10]]
        event_bus.publish(rid, "step.progress", step=STEP_KEYWORDS,
                          message=f"Using {len(seeds)} seeds: {', '.join(seeds[:5])}{'...' if len(seeds) > 5 else ''}")

        kw_items: list[dict] = []
        try:
            kw_items = await keyword_ideas(seeds, market=market, language=language, limit=80)
        except Exception as e:
            event_bus.publish(rid, "step.progress", step=STEP_KEYWORDS,
                              message=f"DataForSEO error: {e}")
            log.warning("dataforseo_failed", error=str(e))

        normalized_keywords = _normalize_keywords(kw_items, market, language, entity_ids)

        # Relevance filter: drop keywords that don't contain any seed token.
        # Each seed contributes its multi-word tokens; we keep a keyword if at
        # least one such token is a substring of the keyword (case-insensitive).
        seed_tokens: set[str] = set()
        for s in seeds:
            sl = s.lower().strip()
            if not sl:
                continue
            seed_tokens.add(sl)
            # also add the most distinctive single word (longest non-stopword)
            stop = {"the", "a", "an", "for", "and", "with", "in", "on", "of", "to"}
            words = [w for w in sl.split() if len(w) > 3 and w not in stop]
            if words:
                seed_tokens.add(max(words, key=len))

        filtered_keywords: list[dict] = []
        dropped = 0
        for k in normalized_keywords:
            kn = k["keyword_normalized"].lower()
            if not seed_tokens or any(tok in kn for tok in seed_tokens):
                filtered_keywords.append(k)
            else:
                dropped += 1
        if dropped:
            event_bus.publish(rid, "step.progress", step=STEP_KEYWORDS,
                              message=f"Filtered out {dropped} off-topic keyword(s) (no seed match)")

        seen_kw: set[str] = set()
        deduped: list[dict] = []
        for k in filtered_keywords:
            if k["keyword_normalized"] in seen_kw:
                continue
            seen_kw.add(k["keyword_normalized"])
            deduped.append(k)
        deduped = deduped[: settings.max_keywords_per_project]
        event_bus.publish(rid, "step.progress", step=STEP_KEYWORDS,
                          message=f"Got {len(deduped)} unique on-topic keywords")
        _persist_keywords(db, rid, deduped)
        top_keywords = sorted(deduped, key=lambda k: (k.get("search_volume") or 0), reverse=True)[:20]
        event_bus.publish(rid, "step.complete", step=STEP_KEYWORDS, result={
            "count": len(deduped),
            "top": top_keywords,
            "seeds_used": seeds,
            "user_curated": bool(user_seeds),
        })

        # ------ STEP 6: SERP comparison ------
        event_bus.publish(rid, "step.start", step=STEP_SERP,
                          message="Looking at top Google results for your top keywords...")
        serp_targets = top_keywords[:5]
        serp_results_list: list[dict[str, Any]] = []
        for kw in serp_targets:
            event_bus.publish(rid, "step.progress", step=STEP_SERP,
                              message=f"Searching: {kw['keyword']}")
            try:
                serp = await serp_organic(kw["keyword"], market=market, language=language, depth=10)
                our_domain = base_domain
                we_rank = next(
                    (r for r in serp.get("organic", []) if (r.get("domain") or "").endswith(our_domain)),
                    None,
                )
                top_domains = [r.get("domain") for r in serp.get("organic", [])[:5]]
                event_bus.publish(rid, "step.progress", step=STEP_SERP,
                                  message=f"Top domains: {', '.join(d for d in top_domains if d)}",
                                  detail=("You rank #" + str(we_rank["rank"]) if we_rank else "You don't rank in top 10"))
                serp_row = {
                    "keyword": kw["keyword"],
                    "search_volume": kw.get("search_volume"),
                    "we_rank": we_rank,
                    "top_domains": top_domains,
                    "organic": serp.get("organic", [])[:10],
                    "people_also_ask": serp.get("people_also_ask", []),
                    "related_searches": serp.get("related_searches", []),
                }
                serp_results_list.append(serp_row)
                _persist_serp_result(db, rid, serp_row, we_rank)
            except Exception as e:
                event_bus.publish(rid, "step.progress", step=STEP_SERP,
                                  message=f"SERP error for '{kw['keyword']}': {e}")
                log.warning("serp_failed", keyword=kw["keyword"], error=str(e))
        event_bus.publish(rid, "step.complete", step=STEP_SERP, result={"results": serp_results_list})

        # ------ STEP 7: RECOMMENDATIONS (LLM) ------
        event_bus.publish(rid, "step.start", step=STEP_RECOMMENDATIONS,
                          message="Generating recommendations...")
        recs = await _generate_recommendations_llm(
            url=fetched.final_url,
            page_type=page_type,
            parsed=parsed,
            issues=issue_records,
            entities_data=entities_data,
            top_keywords=top_keywords,
            serp_results=serp_results_list,
        )
        for rec in (recs.get("recommendations") or [])[:8]:
            event_bus.publish(rid, "step.progress", step=STEP_RECOMMENDATIONS,
                              message=f"[{rec.get('priority', 'medium').upper()}] {rec.get('title', '')}",
                              detail=rec.get("change"))
        # Persist recommendations on the run row
        try:
            db.table("runs").update({"recommendations": json.dumps(recs)}).eq("id", rid).execute()
        except Exception as e:
            log.warning("recs_persist_failed", error=str(e))
        event_bus.publish(rid, "step.complete", step=STEP_RECOMMENDATIONS, result=recs)

        # ------ STEP 8: RANKED KEYWORDS ------
        event_bus.publish(rid, "step.start", step=STEP_RANKED_KW,
                          message=f"Fetching keywords {base_domain} currently ranks for...")
        ranked_kw_rows: list[dict] = []
        try:
            rk_items = await ranked_keywords(base_domain, market=market, language=language, limit=100)
            for item in rk_items:
                kw_data = item.get("keyword_data") or {}
                info = kw_data.get("keyword_info") or {}
                ranked_elem = (item.get("ranked_serp_element") or {})
                serp_elem = (ranked_elem.get("serp_item") or {})
                kw = (kw_data.get("keyword") or "").strip()
                if not kw:
                    continue
                pos = serp_elem.get("rank_absolute")
                row = {
                    "keyword": kw,
                    "keyword_normalized": kw.lower(),
                    "position": pos,
                    "url": serp_elem.get("url"),
                    "search_volume": info.get("search_volume"),
                    "cpc": info.get("cpc"),
                    "traffic": serp_elem.get("etv"),
                }
                ranked_kw_rows.append(row)
                event_bus.publish(rid, "step.progress", step=STEP_RANKED_KW,
                                  message=f"#{pos} '{kw}' — vol {info.get('search_volume', '?'):,}" if isinstance(info.get("search_volume"), int) else f"#{pos} '{kw}'")
            _persist_ranked_keywords(db, rid, ranked_kw_rows)
        except Exception as e:
            event_bus.publish(rid, "step.progress", step=STEP_RANKED_KW,
                              message=f"DataForSEO error: {e}")
            log.warning("ranked_keywords_failed", error=str(e))

        event_bus.publish(rid, "step.complete", step=STEP_RANKED_KW, result={
            "count": len(ranked_kw_rows),
            "keywords": sorted(ranked_kw_rows, key=lambda r: r.get("position") or 9999)[:30],
        })

        # ------ STEP 9: COMPETITORS ------
        event_bus.publish(rid, "step.start", step=STEP_COMPETITORS,
                          message=f"Finding organic competitors of {base_domain}...")
        competitor_rows: list[dict] = []
        try:
            comp_items = await competitors_domain(base_domain, market=market, language=language, limit=15)
            for item in comp_items:
                domain_name = item.get("domain") or ""
                if not domain_name:
                    continue
                row = {
                    "domain": domain_name,
                    "avg_position": item.get("avg_position"),
                    "sum_position": item.get("sum_position"),
                    "intersections": item.get("intersections"),
                    "full_domain_metrics": item,
                }
                competitor_rows.append(row)
                event_bus.publish(rid, "step.progress", step=STEP_COMPETITORS,
                                  message=f"{domain_name} — {item.get('intersections', 0)} shared keywords, avg rank #{item.get('avg_position', '?')}")
            _persist_competitors(db, rid, competitor_rows)
        except Exception as e:
            event_bus.publish(rid, "step.progress", step=STEP_COMPETITORS,
                              message=f"DataForSEO error: {e}")
            log.warning("competitors_failed", error=str(e))

        event_bus.publish(rid, "step.complete", step=STEP_COMPETITORS, result={
            "count": len(competitor_rows),
            "competitors": competitor_rows,
        })

        # ------ STEP 10: BACKLINKS ------
        event_bus.publish(rid, "step.start", step=STEP_BACKLINKS,
                          message=f"Fetching backlink profile for {base_domain}...")
        backlink_result: dict[str, Any] = {}
        try:
            bl_data = await backlinks_overview(base_domain)
            if bl_data:
                total_bl = bl_data.get("total_backlinks") or 0
                total_rd = bl_data.get("total_referring_domains") or 0
                dr = bl_data.get("rank") or 0
                event_bus.publish(rid, "step.progress", step=STEP_BACKLINKS,
                                  message=f"Total backlinks: {total_bl:,}")
                event_bus.publish(rid, "step.progress", step=STEP_BACKLINKS,
                                  message=f"Referring domains: {total_rd:,}")
                event_bus.publish(rid, "step.progress", step=STEP_BACKLINKS,
                                  message=f"Domain rank: {dr}")
                _persist_backlinks(db, rid, base_domain, bl_data)
                backlink_result = {
                    "domain": base_domain,
                    "total_backlinks": total_bl,
                    "total_referring_domains": total_rd,
                    "total_referring_ips": bl_data.get("total_referring_ips"),
                    "domain_rank": dr,
                    "broken_backlinks": bl_data.get("broken_backlinks"),
                    "referring_domains_nofollow": bl_data.get("referring_domains_nofollow"),
                }
        except Exception as e:
            err_text = str(e)
            if "401" in err_text or "403" in err_text:
                event_bus.publish(rid, "step.progress", step=STEP_BACKLINKS,
                                  message="Backlinks API not active for this DataForSEO account — skipping (enable via 'Gain Access' on the dashboard).")
            else:
                event_bus.publish(rid, "step.progress", step=STEP_BACKLINKS,
                                  message=f"Backlinks fetch failed: {err_text}")
            log.warning("backlinks_failed", error=err_text)

        event_bus.publish(rid, "step.complete", step=STEP_BACKLINKS, result=backlink_result)

        # ------ STEP 11: GAP ANALYSIS (re-analyses only) ------
        if previous_run_id:
            event_bus.publish(rid, "step.start", step=STEP_GAP,
                              message="Comparing with previous analysis...")
            gap = _compute_gap(db, rid, previous_run_id, issue_records, ranked_kw_rows)
            event_bus.publish(rid, "step.progress", step=STEP_GAP,
                              message=f"{len(gap['new_issues'])} new issues, {len(gap['fixed_issues'])} resolved")
            event_bus.publish(rid, "step.progress", step=STEP_GAP,
                              message=f"{len(gap['new_ranked_keywords'])} new rankings, {len(gap['rank_improvements'])} rank improvements")
            _persist_gap(db, rid, previous_run_id, gap)
            event_bus.publish(rid, "step.complete", step=STEP_GAP, result=gap)

        # ------ DONE ------
        db.table("runs").update({
            "status": "done",
            "finished_at": _now_iso(),
            "pages_crawled": 1,
        }).eq("id", rid).execute()

        event_bus.publish(rid, "done", final_summary={
            "url": fetched.final_url,
            "page_type": page_type,
            "issues_count": len(issue_records),
            "entities_count": len(entities_data.get("entities", [])),
            "keywords_count": len(deduped),
            "ranked_keywords_count": len(ranked_kw_rows),
            "competitors_count": len(competitor_rows),
            "recommendations_count": len(recs.get("recommendations") or []),
            "is_re_analysis": bool(previous_run_id),
        })

    except Exception as e:
        log.exception("single_url_pipeline_failed")
        event_bus.publish(rid, "error", message=str(e))
        try:
            db.table("runs").update({
                "status": "failed",
                "finished_at": _now_iso(),
                "error_message": str(e),
            }).eq("id", rid).execute()
        except Exception:
            pass


# ---------------- Helpers ----------------
def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _base_domain(url: str) -> str:
    e = tldextract.extract(url)
    return ".".join(p for p in [e.domain, e.suffix] if p)


def _count_by(rows: list[dict], key: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in rows:
        v = r.get(key)
        if v is None:
            continue
        out[v] = out.get(v, 0) + 1
    return out


async def _fail_run(db, run_id: str, msg: str) -> None:
    db.table("runs").update({
        "status": "failed",
        "finished_at": _now_iso(),
        "error_message": msg,
    }).eq("id", run_id).execute()
    event_bus.publish(run_id, "done", final_summary={"error": msg})


def _persist_page(db, run_id: str, fetched, parsed, page_type: str) -> str | None:
    payload = {
        "run_id": run_id,
        "url": fetched.url,
        "final_url": fetched.final_url,
        "http_status": fetched.status,
        "redirect_chain": fetched.redirect_chain,
        "fetcher": fetched.fetcher,
        "title": parsed.title,
        "meta_description": parsed.meta_description,
        "h1": parsed.h1,
        "h2": parsed.h2,
        "h3": parsed.h3,
        "canonical": parsed.canonical,
        "robots_meta": parsed.robots_meta,
        "is_indexable": parsed.is_indexable,
        "word_count": parsed.word_count,
        "text_content": parsed.text_content,
        "internal_links": parsed.internal_links,
        "external_links": parsed.external_links,
        "image_urls": parsed.image_urls,
        "images_missing_alt": parsed.images_missing_alt,
        "schema_types": parsed.schema_types,
        "schema_raw": parsed.schema_raw,
        "open_graph": parsed.open_graph,
        "hreflang": parsed.hreflang,
        "page_type": page_type,
        "page_type_source": "rule",
        "content_hash": parsed.content_hash,
        "template_hint": parsed.template_hint,
    }
    res = db.table("pages").upsert(payload, on_conflict="run_id,url").execute()
    return res.data[0]["id"] if res.data else None


def _persist_issues(db, run_id: str, page_id: str | None, issues: list[dict[str, Any]]) -> None:
    if not issues:
        return
    rows = [
        {
            "run_id": run_id,
            "page_id": page_id,
            "issue_code": i["code"],
            "severity": i["severity"],
            "source": "rule",
            "details": i.get("details", {}),
            "recommendation": i.get("recommendation"),
        }
        for i in issues
    ]
    db.table("issues").insert(rows).execute()


def _persist_entities(db, run_id: str, page_id: str | None, entities: list[dict[str, Any]]) -> dict[str, str]:
    out: dict[str, str] = {}
    for ent in entities:
        canonical = (ent.get("canonical_name") or ent.get("name") or "").strip().lower()
        if not canonical:
            continue
        payload = {
            "run_id": run_id,
            "entity_type": ent.get("entity_type", "topic"),
            "name": ent.get("name", canonical),
            "canonical_name": canonical,
            "confidence": float(ent.get("confidence", 0.7)),
            "attributes": ent.get("attributes") or {},
            "source_page_ids": [page_id] if page_id else [],
            "source": "ai",
        }
        try:
            res = db.table("entities").upsert(
                payload, on_conflict="run_id,entity_type,canonical_name"
            ).execute()
            if res.data:
                out[canonical] = res.data[0]["id"]
        except Exception as e:
            log.warning("entity_persist_failed", entity=canonical, error=str(e))
    return out


def _normalize_keywords(items: list[dict], market: str, language: str, entity_ids: dict[str, str]) -> list[dict]:
    norm: list[dict] = []
    for item in items:
        kw_data = item.get("keyword_data") or item
        kw = (kw_data.get("keyword") or item.get("keyword") or "").strip()
        if not kw:
            continue
        info = (kw_data.get("keyword_info") or {})
        norm.append({
            "keyword": kw,
            "keyword_normalized": kw.lower(),
            "market": market,
            "language": language,
            "search_volume": info.get("search_volume"),
            "cpc": info.get("cpc"),
            "competition": info.get("competition"),
            "keyword_difficulty": (kw_data.get("keyword_properties") or {}).get("keyword_difficulty"),
            "intent": _guess_intent(kw),
            "keyword_type": _guess_type(kw),
            "source": "dfs_keyword_ideas",
            "raw_payload": item,
        })
    return norm


def _guess_intent(kw: str) -> str:
    k = kw.lower()
    if any(t in k for t in ["how to", "what is", "guide", "tutorial", "tips"]):
        return "informational"
    if any(t in k for t in ["buy", "shop", "price", "cheap", "discount", "deal", "for sale"]):
        return "transactional"
    if any(t in k for t in ["best", "top", "review", "vs", "compare", "comparison"]):
        return "commercial"
    return "commercial"


def _guess_type(kw: str) -> str:
    if "?" in kw or kw.lower().startswith(("how ", "what ", "why ", "when ", "where ", "who ")):
        return "question"
    if len(kw.split()) >= 4:
        return "long_tail"
    return "head"


def _persist_keywords(db, run_id: str, keywords: list[dict]) -> None:
    if not keywords:
        return
    rows = [{**k, "run_id": run_id} for k in keywords]
    chunk_size = 100
    for i in range(0, len(rows), chunk_size):
        chunk = rows[i: i + chunk_size]
        try:
            db.table("keywords").upsert(chunk, on_conflict="run_id,keyword_normalized,market,language").execute()
        except Exception as e:
            log.warning("keyword_persist_failed", error=str(e), batch=i)


def _persist_serp_result(db, run_id: str, serp_row: dict, we_rank: dict | None) -> None:
    try:
        db.table("serp_results").insert({
            "run_id": run_id,
            "keyword": serp_row["keyword"],
            "search_volume": serp_row.get("search_volume"),
            "our_rank": (we_rank or {}).get("rank"),
            "our_url": (we_rank or {}).get("url"),
            "organic": json.dumps(serp_row.get("organic", [])),
            "people_also_ask": serp_row.get("people_also_ask", []),
            "related_searches": serp_row.get("related_searches", []),
        }).execute()
    except Exception as e:
        log.warning("serp_persist_failed", error=str(e))


def _persist_ranked_keywords(db, run_id: str, rows: list[dict]) -> None:
    if not rows:
        return
    db_rows = [{**r, "run_id": run_id} for r in rows]
    chunk_size = 100
    for i in range(0, len(db_rows), chunk_size):
        try:
            db.table("domain_ranked_keywords").insert(db_rows[i: i + chunk_size]).execute()
        except Exception as e:
            log.warning("ranked_kw_persist_failed", error=str(e))


def _persist_competitors(db, run_id: str, rows: list[dict]) -> None:
    if not rows:
        return
    try:
        db.table("competitor_domains").insert([{**r, "run_id": run_id} for r in rows]).execute()
    except Exception as e:
        log.warning("competitor_persist_failed", error=str(e))


def _persist_backlinks(db, run_id: str, domain: str, bl: dict) -> None:
    try:
        db.table("backlinks_summary").insert({
            "run_id": run_id,
            "domain": domain,
            "total_backlinks": bl.get("total_backlinks"),
            "total_referring_domains": bl.get("total_referring_domains"),
            "total_referring_ips": bl.get("total_referring_ips"),
            "domain_rank": bl.get("rank"),
            "broken_backlinks": bl.get("broken_backlinks"),
            "referring_domains_nofollow": bl.get("referring_domains_nofollow"),
            "raw_data": json.dumps(bl),
        }).execute()
    except Exception as e:
        log.warning("backlinks_persist_failed", error=str(e))


def _compute_gap(
    db,
    run_id: str,
    prev_run_id: str,
    current_issues: list[dict],
    current_ranked: list[dict],
) -> dict[str, Any]:
    """Compare current run vs previous run and return gap dict."""
    # Previous issues
    prev_issues_res = db.table("issues").select("issue_code,severity").eq("run_id", prev_run_id).execute()
    prev_issue_codes = {r["issue_code"] for r in (prev_issues_res.data or [])}
    cur_issue_codes = {i["code"] for i in current_issues}

    new_issues = [i for i in current_issues if i["code"] not in prev_issue_codes]
    fixed_codes = prev_issue_codes - cur_issue_codes
    fixed_issues = [{"code": c} for c in fixed_codes]

    # Previous ranked keywords
    prev_rk_res = db.table("domain_ranked_keywords").select("keyword_normalized,position").eq("run_id", prev_run_id).execute()
    prev_rk = {r["keyword_normalized"]: r["position"] for r in (prev_rk_res.data or [])}
    cur_rk = {r["keyword_normalized"]: r.get("position") for r in current_ranked}

    new_ranked = [r for r in current_ranked if r["keyword_normalized"] not in prev_rk]
    lost_ranked = [{"keyword": k} for k in prev_rk if k not in cur_rk]

    rank_improvements: list[dict] = []
    rank_declines: list[dict] = []
    for kw_norm, cur_pos in cur_rk.items():
        if kw_norm in prev_rk and cur_pos is not None and prev_rk[kw_norm] is not None:
            delta = prev_rk[kw_norm] - cur_pos  # positive = improved
            if delta >= 3:
                rank_improvements.append({"keyword": kw_norm, "from": prev_rk[kw_norm], "to": cur_pos, "delta": delta})
            elif delta <= -3:
                rank_declines.append({"keyword": kw_norm, "from": prev_rk[kw_norm], "to": cur_pos, "delta": delta})

    vol_gained = sum(r.get("search_volume") or 0 for r in new_ranked)

    summary_parts = []
    if fixed_issues:
        summary_parts.append(f"{len(fixed_issues)} issue(s) resolved")
    if new_issues:
        summary_parts.append(f"{len(new_issues)} new issue(s) found")
    if new_ranked:
        summary_parts.append(f"{len(new_ranked)} new keyword ranking(s)")
    if rank_improvements:
        summary_parts.append(f"{len(rank_improvements)} rank improvement(s)")
    if rank_declines:
        summary_parts.append(f"{len(rank_declines)} rank decline(s)")
    summary = "; ".join(summary_parts) if summary_parts else "No significant changes detected"

    return {
        "summary": summary,
        "new_issues": new_issues,
        "fixed_issues": fixed_issues,
        "new_ranked_keywords": new_ranked[:20],
        "lost_ranked_keywords": lost_ranked[:20],
        "rank_improvements": sorted(rank_improvements, key=lambda x: x["delta"], reverse=True)[:20],
        "rank_declines": sorted(rank_declines, key=lambda x: x["delta"])[:20],
        "keyword_volume_gained": vol_gained,
    }


def _persist_gap(db, run_id: str, previous_run_id: str, gap: dict) -> None:
    try:
        db.table("gap_analyses").insert({
            "run_id": run_id,
            "previous_run_id": previous_run_id,
            "summary": gap.get("summary"),
            "new_issues": json.dumps(gap.get("new_issues", [])),
            "fixed_issues": json.dumps(gap.get("fixed_issues", [])),
            "new_ranked_keywords": json.dumps(gap.get("new_ranked_keywords", [])),
            "lost_ranked_keywords": json.dumps(gap.get("lost_ranked_keywords", [])),
            "rank_improvements": json.dumps(gap.get("rank_improvements", [])),
            "rank_declines": json.dumps(gap.get("rank_declines", [])),
            "keyword_volume_gained": gap.get("keyword_volume_gained", 0),
        }).execute()
    except Exception as e:
        log.warning("gap_persist_failed", error=str(e))


# ---------------- LLM prompts ----------------
async def _extract_entities_llm(parsed, url: str, market: str, language: str) -> dict[str, Any]:
    body = (parsed.text_content or "")[:6000]
    page_dump = (
        f"URL: {url}\n"
        f"Market: {market}   Language: {language}\n"
        f"Title: {parsed.title or '(missing)'}\n"
        f"Meta description: {parsed.meta_description or '(missing)'}\n"
        f"H1: {' | '.join(parsed.h1) or '(missing)'}\n"
        f"H2: {' | '.join((parsed.h2 or [])[:15])}\n"
        f"Schema types: {', '.join(parsed.schema_types) or '(none)'}\n"
        f"Open Graph: {json.dumps(parsed.open_graph or {})[:300]}\n\n"
        f"Body text:\n{body}\n"
    )
    system = (
        "You are an SEO and ecommerce intelligence analyst. You read a single web page "
        "and identify what business is being run, what is being sold, and what topics "
        "are covered. Output STRICT JSON only. No prose. No markdown. Schema:\n"
        "{\n"
        '  "business_summary": "1-3 sentence plain-English summary",\n'
        '  "business_type": "ecommerce | services | content | saas | marketplace | other",\n'
        '  "entities": [\n'
        '    { "entity_type": "product | category | service | brand | topic | persona",\n'
        '      "name": "string", "canonical_name": "lowercase string",\n'
        '      "confidence": 0.0, "attributes": {} }\n'
        "  ],\n"
        '  "buyer_personas": ["string"],\n'
        '  "seed_keywords": ["string"],\n'
        '  "topic_clusters": ["string"]\n'
        "}\n"
        "Rules:\n"
        "1. Only include entities grounded in the page content. canonical_name must be "
        "lowercase, singular, no brand prefix unless entity_type is 'brand'. Max 20 entities.\n"
        "2. SEED KEYWORDS MUST BE PRODUCT-SPECIFIC, NOT GENERIC. Each seed must be a "
        "compound phrase (2-4 words) that names the actual product/category sold here. "
        "Bad seeds (TOO BROAD — never output these): 'ball', 'shoes', 'tennis', 'racket', "
        "'court', 'paddle', 'apparel', 'sport'. Good seeds (specific): 'pickleball paddle', "
        "'pickleball racket', 'table tennis blade', 'joola pickleball', 'graphite paddle', "
        "'pickleball shoes for women'.\n"
        "3. If the brand sells multiple sport categories (e.g. table tennis AND pickleball), "
        "prefer seeds for the category most prominent on THIS page based on H1 / title / "
        "first 500 chars of body. Do not mix categories unless both are equally featured.\n"
        "4. Always include the brand name combined with the primary category as one of the "
        "first seeds (e.g. 'joola pickleball', 'joola pickleball paddle').\n"
        "Max 30 seed_keywords. Order by relevance (most specific first)."
    )
    return await chat_json(system=system, user=page_dump, temperature=0.2)


async def _generate_recommendations_llm(
    url: str,
    page_type: str,
    parsed,
    issues: list[dict[str, Any]],
    entities_data: dict[str, Any],
    top_keywords: list[dict[str, Any]],
    serp_results: list[dict[str, Any]],
) -> dict[str, Any]:
    payload = {
        "url": url,
        "page_type": page_type,
        "current": {
            "title": parsed.title,
            "meta_description": parsed.meta_description,
            "h1": parsed.h1,
            "h2": parsed.h2[:20],
            "schema_types": parsed.schema_types,
            "word_count": parsed.word_count,
        },
        "issues": [{"code": i["code"], "severity": i["severity"], "details": i.get("details")} for i in issues],
        "business_summary": entities_data.get("business_summary"),
        "entities": entities_data.get("entities", [])[:15],
        "topic_clusters": entities_data.get("topic_clusters", []),
        "top_keywords": [
            {"keyword": k["keyword"], "search_volume": k.get("search_volume"), "intent": k.get("intent")}
            for k in top_keywords[:15]
        ],
        "serp": [
            {"keyword": s["keyword"],
             "we_rank": (s.get("we_rank") or {}).get("rank"),
             "top_domains": s["top_domains"],
             "people_also_ask": s.get("people_also_ask", [])[:5]}
            for s in serp_results
        ],
    }
    system = (
        "You are an SEO consultant. Given a single page's data plus keyword + SERP context, "
        "produce concrete, prioritized recommendations. Output STRICT JSON only. Schema:\n"
        "{\n"
        '  "executive_summary": "2-4 sentence plain-English summary of the page\'s SEO state",\n'
        '  "rewrites": {\n'
        '    "title": "<<recommended title, 30-60 chars>>",\n'
        '    "meta_description": "<<recommended meta description, 120-160 chars>>",\n'
        '    "h1": "<<recommended H1>>"\n'
        "  },\n"
        '  "h2_sections_to_add": ["<<section name>>"],\n'
        '  "schema_to_add": ["<<schema type>>"],\n'
        '  "faq_to_add": ["<<question to answer>>"],\n'
        '  "missing_keywords": ["<<keyword not yet targeted>>"],\n'
        '  "content_gaps": ["<<topic the SERP covers that this page does not>>"],\n'
        '  "recommendations": [\n'
        '    { "priority": "critical|high|medium|low",\n'
        '      "where": "title|meta|h1|body|schema|technical",\n'
        '      "title": "short label",\n'
        '      "current": "what the page has right now (verbatim if possible) — use \\"(missing)\\" if absent",\n'
        '      "expected": "the exact replacement / fix the developer should implement",\n'
        '      "seo_rule": "the SEO rule or best-practice being broken (e.g. MISSING_META_DESC, TITLE_TOO_LONG, THIN_CONTENT, MISSING_SCHEMA, KEYWORD_GAP, NO_INTERNAL_LINKING, etc.)",\n'
        '      "benefit": "concrete user / SEO benefit of fixing this — mention CTR, ranking, rich-result eligibility, indexation, etc.",\n'
        '      "change": "imperative one-liner restating the action (kept for backwards compat)",\n'
        '      "why": "short rationale tying to issue/keyword/SERP (kept for backwards compat)" }\n'
        "  ]\n"
        "}\n"
        "Tie every recommendation to either an issue, a keyword, or a SERP gap. "
        "Be specific (no vague 'improve content'). Always populate current, expected, seo_rule and benefit. "
        "Max 10 recommendations, ranked."
    )
    return await chat_json(
        system=system,
        user=json.dumps(payload, default=str),
        temperature=0.3,
    )
