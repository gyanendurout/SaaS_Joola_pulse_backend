"""Content generation routes.

Two feature groups share the same `/api/content` prefix:

1. **Legacy run-scoped content** (blog ideas/outlines/drafts/emails/social/calendar)
   — keyed by `{run_id}`, used by the SEO Intel POC.
2. **Content Studio (Text v1)** — the JOOLA Pulse Content Generation feature
   (spec 2026-05-19). New endpoints: /generate, /generate/stream, /drafts/*,
   /signals/preview, /templates, /usage.
"""
from __future__ import annotations

import json
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.content.generator import generate_once, sse_iterator
from app.content.rate_limiter import get_limiter
from app.content.signal_collectors import preview_signals
from app.content.types import (
    Draft,
    DraftListResponse,
    DraftStatus,
    DraftUpdate,
    GenerateRequest,
    GenerateResponse,
    RegenerateRequest,
    SignalsPreview,
    Template,
)
from app.db import service_client
from app.services.llm import chat_json

router = APIRouter(prefix="/api/content", tags=["content"])


# ─── Run context builder ──────────────────────────────────────────────────────

def _get_run_context(run_id: str) -> dict:
    db = service_client()
    run_res = db.table("runs").select("*").eq("id", run_id).single().execute()
    if not run_res.data:
        raise HTTPException(404, "Run not found")
    run = run_res.data

    entities = (
        db.table("entities")
        .select("entity_type,name,attributes")
        .eq("run_id", run_id)
        .execute().data or []
    )
    keywords = (
        db.table("keywords")
        .select("keyword,search_volume,cpc,intent,keyword_type")
        .eq("run_id", run_id)
        .order("search_volume", desc=True)
        .limit(30)
        .execute().data or []
    )
    pages = (
        db.table("pages")
        .select("title,meta_description,h1,h2,word_count,text_content,page_type")
        .eq("run_id", run_id)
        .execute().data or []
    )
    page = pages[0] if pages else {}

    recs_raw = run.get("recommendations")
    recs: dict = {}
    if recs_raw:
        try:
            recs = json.loads(recs_raw) if isinstance(recs_raw, str) else recs_raw
        except Exception:
            pass

    return {
        "run": run,
        "entities": entities,
        "keywords": keywords,
        "page": page,
        "recommendations": recs,
        "domain": run.get("canonical_domain", ""),
        "market": run.get("market", "US"),
        "language": run.get("language", "en"),
    }


def _business_ctx(ctx: dict) -> dict:
    ents = ctx["entities"]
    return {
        "domain": ctx["domain"],
        "page_title": ctx["page"].get("title", ""),
        "page_summary": (ctx["page"].get("text_content") or "")[:600],
        "products": [e["name"] for e in ents if e.get("entity_type") == "product"][:8],
        "categories": [e["name"] for e in ents if e.get("entity_type") == "category"][:5],
        "brands": [e["name"] for e in ents if e.get("entity_type") == "brand"][:3],
        "market": ctx["market"],
    }


def _persist(run_id: str, content_type: str, title: str | None, payload: dict, platform: str | None = None) -> None:
    try:
        db = service_client()
        row: dict = {
            "run_id": run_id,
            "content_type": content_type,
            "title": title,
            "payload": payload,
        }
        if platform:
            row["platform"] = platform
        db.table("generated_content").insert(row).execute()
    except Exception:
        pass


# ─── Blog Ideas ──────────────────────────────────────────────────────────────

class BlogIdeasRequest(BaseModel):
    count: int = 8
    tone: str = "professional and helpful"


@router.post("/{run_id}/blog-ideas")
async def generate_blog_ideas(run_id: UUID, req: BlogIdeasRequest):
    ctx = _get_run_context(str(run_id))
    system = (
        "You are an SEO content strategist. Generate blog ideas that will rank on Google and drive qualified traffic. "
        "Output STRICT JSON only. Schema: "
        '{"ideas":[{"title":string,"target_keyword":string,"search_volume":number|null,'
        '"intent":"informational"|"commercial"|"transactional","blog_type":string,'
        '"why":string,"estimated_word_count":number}]}'
    )
    user = json.dumps({
        "business": _business_ctx(ctx),
        "top_keywords": [
            {"keyword": k["keyword"], "volume": k.get("search_volume"), "intent": k.get("intent")}
            for k in ctx["keywords"][:20]
        ],
        "content_gaps": ctx["recommendations"].get("content_gaps", []),
        "faq_ideas": ctx["recommendations"].get("faq_to_add", []),
        "h2_ideas": ctx["recommendations"].get("h2_sections_to_add", []),
        "tone": req.tone,
        "count": req.count,
    }, default=str)
    result = await chat_json(system=system, user=user, temperature=0.65)
    for idea in (result.get("ideas") or []):
        _persist(str(run_id), "blog_idea", idea.get("title"), idea)
    return result


# ─── Blog Outline ─────────────────────────────────────────────────────────────

class BlogOutlineRequest(BaseModel):
    title: str
    target_keyword: str
    tone: str = "professional and helpful"
    word_count: int = 1500


@router.post("/{run_id}/blog-outline")
async def generate_blog_outline(run_id: UUID, req: BlogOutlineRequest):
    ctx = _get_run_context(str(run_id))
    secondary = [
        k["keyword"] for k in ctx["keywords"][:15]
        if k["keyword"].lower() != req.target_keyword.lower()
    ]
    system = (
        "You are an SEO content strategist. Create a detailed blog outline optimized for search and reader value. "
        "Output STRICT JSON only. Schema: "
        '{"seo_title":string,"meta_description":string,"slug":string,"h1":string,'
        '"outline":[{"heading":string,"type":"h2"|"h3","talking_points":[string],"word_count":number}],'
        '"faq":[{"question":string,"answer":string}],'
        '"internal_links":[string],"cta":string,"estimated_word_count":number}'
    )
    user = json.dumps({
        "business": _business_ctx(ctx),
        "title": req.title,
        "target_keyword": req.target_keyword,
        "secondary_keywords": secondary[:8],
        "tone": req.tone,
        "target_word_count": req.word_count,
    }, default=str)
    result = await chat_json(system=system, user=user, temperature=0.4)
    _persist(str(run_id), "blog_outline", req.title, {**result, "target_keyword": req.target_keyword})
    return result


# ─── Blog Draft ───────────────────────────────────────────────────────────────

class BlogDraftRequest(BaseModel):
    title: str
    target_keyword: str
    outline: list[dict] = []
    tone: str = "professional and helpful"
    word_count: int = 1200
    include_products: bool = True


@router.post("/{run_id}/blog-draft")
async def generate_blog_draft(run_id: UUID, req: BlogDraftRequest):
    ctx = _get_run_context(str(run_id))
    products = [
        e["name"] for e in ctx["entities"]
        if e.get("entity_type") in ("product", "category")
    ][:6]
    system = (
        "You are an expert SEO content writer. Write a complete, publish-ready blog post. "
        "Output STRICT JSON only. Schema: "
        '{"seo_title":string,"meta_description":string,"slug":string,'
        '"article_html":string,"word_count":number,"reading_time_minutes":number,'
        '"keywords_used":[string],"image_suggestions":[string],"cta":string}'
        "\narticle_html must be valid HTML with h2/h3/p/ul/ol tags. "
        "Include target keyword naturally. Write for humans first, SEO second. No keyword stuffing."
    )
    user = json.dumps({
        "business": _business_ctx(ctx),
        "title": req.title,
        "target_keyword": req.target_keyword,
        "outline": req.outline,
        "products_to_mention": products if req.include_products else [],
        "tone": req.tone,
        "target_word_count": req.word_count,
    }, default=str)
    result = await chat_json(system=system, user=user, temperature=0.5)
    _persist(str(run_id), "blog_draft", req.title, {**result, "target_keyword": req.target_keyword})
    return result


# ─── Email ────────────────────────────────────────────────────────────────────

class EmailRequest(BaseModel):
    email_type: str = "newsletter"   # newsletter | promotional | product_launch | blog_promotion | re_engagement
    topic: str = ""
    tone: str = "professional and friendly"


@router.post("/{run_id}/email")
async def generate_email(run_id: UUID, req: EmailRequest):
    ctx = _get_run_context(str(run_id))
    system = (
        "You are an email marketing expert. Generate a complete, high-converting email. "
        "Output STRICT JSON only. Schema: "
        '{"subject_lines":[string],"preview_text":string,'
        '"email_body_html":string,"cta_text":string,"cta_url_suggestion":string,'
        '"segmentation_suggestion":string,"send_time_recommendation":string,'
        '"estimated_read_time":"~X min"}'
        "\nemail_body_html: valid HTML, short paragraphs, scannable, strong single CTA."
    )
    user = json.dumps({
        "business": _business_ctx(ctx),
        "email_type": req.email_type,
        "topic": req.topic or f"Latest {ctx['domain']} updates and tips",
        "tone": req.tone,
        "top_keywords": [k["keyword"] for k in ctx["keywords"][:6]],
    }, default=str)
    result = await chat_json(system=system, user=user, temperature=0.6)
    _persist(str(run_id), "email", f"{req.email_type}: {req.topic or 'Campaign'}", {**result, "email_type": req.email_type})
    return result


# ─── Social ───────────────────────────────────────────────────────────────────

class SocialRequest(BaseModel):
    platform: str = "instagram"   # instagram | facebook | linkedin
    post_type: str = "product"    # product | educational | promotional | blog_promo
    topic: str = ""
    tone: str = "engaging and friendly"


@router.post("/{run_id}/social")
async def generate_social(run_id: UUID, req: SocialRequest):
    ctx = _get_run_context(str(run_id))
    hints = {
        "instagram": "Strong hook on line 1. 150-220 words. 15-25 relevant hashtags. Emojis sparingly.",
        "facebook": "Conversational tone. Ask a question to drive comments. 80-150 words. 3-5 hashtags.",
        "linkedin": "Professional and insight-driven. Share expertise. 150-250 words. 3-5 hashtags. No emojis.",
    }
    system = (
        f"You are a {req.platform.title()} content expert for brands. "
        f"Platform guidance: {hints.get(req.platform, '')} "
        "Output STRICT JSON only. Schema: "
        '{"caption":string,"hook":string,'
        '"carousel_slides":[{"slide_number":number,"headline":string,"body":string}],'
        '"hashtags":[string],"cta":string,"image_video_idea":string}'
        "\ncarousel_slides: 5-7 slides for educational posts, empty array for single-image posts."
    )
    user = json.dumps({
        "business": _business_ctx(ctx),
        "platform": req.platform,
        "post_type": req.post_type,
        "topic": req.topic or f"Discover {ctx['domain']}",
        "tone": req.tone,
    }, default=str)
    result = await chat_json(system=system, user=user, temperature=0.7)
    _persist(
        str(run_id), "social",
        f"{req.platform}: {req.topic or req.post_type}",
        {**result, "platform": req.platform, "post_type": req.post_type},
        platform=req.platform,
    )
    return result


# ─── Content Calendar ─────────────────────────────────────────────────────────

class CalendarRequest(BaseModel):
    weeks: int = 4
    channels: list[str] = ["blog", "email", "instagram"]


@router.post("/{run_id}/calendar")
async def generate_calendar(run_id: UUID, req: CalendarRequest):
    ctx = _get_run_context(str(run_id))
    system = (
        "You are a content strategist. Create a practical, SEO-aligned content calendar. "
        "Output STRICT JSON only. Schema: "
        '{"calendar":[{"week":number,"date":string,"channel":string,'
        '"title":string,"description":string,"keyword":string,'
        '"content_type":string,"status":"planned"}]}'
        "\nDistribute content evenly across channels. Align blogs with high-volume keywords. "
        "Use YYYY-MM-DD format for dates starting from today. Include 2-3 items per week."
    )
    user = json.dumps({
        "business": _business_ctx(ctx),
        "weeks": req.weeks,
        "channels": req.channels,
        "top_keywords": [
            {"keyword": k["keyword"], "volume": k.get("search_volume"), "intent": k.get("intent")}
            for k in ctx["keywords"][:15]
        ],
        "content_gaps": ctx["recommendations"].get("content_gaps", []),
        "faq_ideas": ctx["recommendations"].get("faq_to_add", []),
    }, default=str)
    result = await chat_json(system=system, user=user, temperature=0.5)
    db = service_client()
    for item in (result.get("calendar") or []):
        try:
            db.table("content_calendar").insert({
                "run_id": str(run_id),
                "channel": item.get("channel", "blog"),
                "title": item.get("title", ""),
                "description": item.get("description"),
                "keyword": item.get("keyword"),
                "status": "planned",
                "scheduled_date": item.get("date"),
            }).execute()
        except Exception:
            pass
    return result


# ─── List / Get ───────────────────────────────────────────────────────────────

@router.get("/{run_id}/list")
async def list_content(run_id: UUID, content_type: str | None = Query(None)):
    db = service_client()
    q = (
        db.table("generated_content")
        .select("id,content_type,platform,title,created_at")
        .eq("run_id", str(run_id))
        .order("created_at", desc=True)
    )
    if content_type:
        q = q.eq("content_type", content_type)
    res = q.execute()
    return {"items": res.data or []}


@router.get("/{run_id}/list/{content_id}")
async def get_content_item(run_id: UUID, content_id: UUID):
    db = service_client()
    res = (
        db.table("generated_content")
        .select("*")
        .eq("id", str(content_id))
        .eq("run_id", str(run_id))
        .single()
        .execute()
    )
    if not res.data:
        raise HTTPException(404, "Content not found")
    return res.data


@router.get("/{run_id}/calendar")
async def get_calendar(run_id: UUID):
    db = service_client()
    res = (
        db.table("content_calendar")
        .select("*")
        .eq("run_id", str(run_id))
        .order("scheduled_date")
        .execute()
    )
    return {"items": res.data or []}


@router.delete("/{run_id}/list/{content_id}")
async def delete_content(run_id: UUID, content_id: UUID):
    db = service_client()
    db.table("generated_content").delete().eq("id", str(content_id)).eq("run_id", str(run_id)).execute()
    return {"ok": True}


# ============================================================================
# Content Studio (Text v1) — see backend/app/content/*
# ============================================================================

# ─── Generate (streaming + one-shot) ──────────────────────────────────────────

@router.post("/generate/stream")
async def content_generate_stream(req: GenerateRequest):
    """Server-Sent Events stream of tokens + meta + done."""
    return StreamingResponse(
        sse_iterator(req),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/generate", response_model=GenerateResponse)
async def content_generate(req: GenerateRequest):
    """Non-streaming wrapper — runs the full pipeline and returns the final draft."""
    result = await generate_once(req)
    if not result.get("ok"):
        msg = result.get("error") or "generation failed"
        # Map known errors to HTTP codes
        lower = msg.lower()
        if "rate" in lower and "limit" in lower:
            raise HTTPException(429, detail=msg)
        if "monthly_cap" in lower or "402" in lower:
            raise HTTPException(402, detail=msg)
        if "openai" in lower:
            raise HTTPException(502, detail=msg)
        raise HTTPException(422, detail=msg)
    return GenerateResponse(
        draft_id=result["draft_id"],
        body=result.get("body") or "",
        title=result.get("title"),
        hashtags=result.get("hashtags") or [],
        run_id=result["run_id"],
        cost_usd=float(result.get("cost_usd") or 0.0),
    )


# ─── Signals preview ─────────────────────────────────────────────────────────

@router.get("/signals/preview", response_model=SignalsPreview)
async def content_signals_preview(
    content_type: str | None = Query(None),
    source_article_id: str | None = Query(None),
):
    """Returns the cross-channel signal preview for the composer."""
    _ = content_type  # accepted for forward compat; preview is same shape today
    return await preview_signals(source_article_id)


# ─── Drafts CRUD ─────────────────────────────────────────────────────────────

@router.get("/drafts", response_model=DraftListResponse)
async def content_drafts_list(
    content_type: str | None = Query(None),
    status: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    db = service_client()
    q = (
        db.table("content_drafts")
        .select("*", count="exact")
        .order("created_at", desc=True)
        .range(offset, offset + limit - 1)
    )
    if content_type:
        q = q.eq("content_type", content_type)
    if status:
        q = q.eq("status", status)
    res = q.execute()
    rows = res.data or []
    total = getattr(res, "count", None) or len(rows)
    drafts = [Draft(**_normalize_draft_row(r)) for r in rows]
    return DraftListResponse(drafts=drafts, total=int(total))


@router.get("/drafts/{draft_id}", response_model=Draft)
async def content_draft_get(draft_id: UUID):
    db = service_client()
    res = db.table("content_drafts").select("*").eq("id", str(draft_id)).single().execute()
    if not res.data:
        raise HTTPException(404, "Draft not found")
    return Draft(**_normalize_draft_row(res.data))


@router.patch("/drafts/{draft_id}", response_model=Draft)
async def content_draft_update(draft_id: UUID, patch: DraftUpdate):
    db = service_client()
    existing = (
        db.table("content_drafts").select("*").eq("id", str(draft_id)).single().execute()
    )
    if not existing.data:
        raise HTTPException(404, "Draft not found")

    payload: dict = {}
    if patch.body is not None:
        payload["body"] = patch.body
    if patch.title is not None:
        payload["title"] = patch.title
    if patch.hashtags is not None:
        payload["hashtags"] = patch.hashtags
    if patch.status is not None:
        payload["status"] = patch.status.value if hasattr(patch.status, "value") else str(patch.status)
    if not payload:
        return Draft(**_normalize_draft_row(existing.data))

    upd = (
        db.table("content_drafts")
        .update(payload)
        .eq("id", str(draft_id))
        .execute()
    )
    row = (upd.data or [existing.data])[0]
    return Draft(**_normalize_draft_row(row))


@router.post("/drafts/{draft_id}/regenerate", response_model=GenerateResponse)
async def content_draft_regenerate(draft_id: UUID, body: RegenerateRequest | None = None):
    """Regenerate from a saved draft's source_signal_snapshot.

    A new draft row is inserted with parent_draft_id set to the source.
    """
    db = service_client()
    res = db.table("content_drafts").select("*").eq("id", str(draft_id)).single().execute()
    if not res.data:
        raise HTTPException(404, "Draft not found")
    src = res.data

    md = src.get("metadata") or {}
    req = GenerateRequest(
        content_type=src["content_type"],
        signals_config={},  # re-use everything from snapshot; collectors will refetch
        source_article_id=src.get("source_article_id"),
        instructions=(body.instructions if body else None) or "Regenerate with the same intent.",
        tone=md.get("tone") or "informative",
        length=md.get("length") or "medium",
        audience=md.get("audience") or "general_fans",
        cta_goal=md.get("cta_goal") or "none",
        created_by=src.get("created_by") or "anon@joola.com",
    )
    result = await generate_once(req)
    if not result.get("ok"):
        raise HTTPException(502, detail=result.get("error") or "regenerate failed")

    # Patch the new draft to record the parent link + bump version
    new_id = result["draft_id"]
    try:
        db.table("content_drafts").update({
            "parent_draft_id": str(draft_id),
            "version": int(src.get("version") or 1) + 1,
        }).eq("id", new_id).execute()
    except Exception:
        pass

    return GenerateResponse(
        draft_id=new_id,
        body=result.get("body") or "",
        title=result.get("title"),
        hashtags=result.get("hashtags") or [],
        run_id=result["run_id"],
        cost_usd=float(result.get("cost_usd") or 0.0),
    )


@router.delete("/drafts/{draft_id}", status_code=204)
async def content_draft_delete(draft_id: UUID):
    """Soft-delete: set status='archived'."""
    db = service_client()
    res = db.table("content_drafts").select("id").eq("id", str(draft_id)).single().execute()
    if not res.data:
        raise HTTPException(404, "Draft not found")
    db.table("content_drafts").update({"status": DraftStatus.ARCHIVED.value}).eq("id", str(draft_id)).execute()
    return None


# ─── Templates ───────────────────────────────────────────────────────────────

@router.get("/templates", response_model=list[Template])
async def content_templates_list(content_type: str | None = Query(None)):
    db = service_client()
    q = db.table("content_templates").select("*").eq("is_active", True).order("name")
    if content_type:
        q = q.eq("content_type", content_type)
    res = q.execute()
    return [Template(**r) for r in (res.data or [])]


# ─── Usage / rate-limit stats ────────────────────────────────────────────────

@router.get("/usage")
async def content_usage(user: str = Query("anon@joola.com")):
    return await get_limiter().stats(user)


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _normalize_draft_row(r: dict) -> dict:
    """Coerce raw Supabase row to Draft-model-friendly shape."""
    out = dict(r)
    if out.get("hashtags") is None:
        out["hashtags"] = []
    if out.get("metadata") is None:
        out["metadata"] = {}
    if out.get("source_signal_snapshot") is None:
        out["source_signal_snapshot"] = {}
    return out
