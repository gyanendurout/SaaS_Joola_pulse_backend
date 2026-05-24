# Seed data

After applying all `.sql` migrations, the database needs the following one-time seed data before any code can run end-to-end.

---

## 1. `brands` (11 rows, JOOLA + 10 competitors)

JOOLA's brand_id is hard-coded across the frontend (every social-media `page.tsx` defines `const JOOLA = '04db8591-37a3-4634-9d11-536975fa6935'`). The other 10 brand names are enumerated in `backend/app/agents/news_scraper.py` line 50-53 as `COMPETITOR_BRANDS`.

```sql
insert into brands (id, name, is_joola) values
  ('04db8591-37a3-4634-9d11-536975fa6935', 'JOOLA',     true),
  (gen_random_uuid(),                        'Selkirk',   false),
  (gen_random_uuid(),                        'Paddletek', false),
  (gen_random_uuid(),                        'Franklin',  false),
  (gen_random_uuid(),                        'CRBN',      false),
  (gen_random_uuid(),                        'Engage',    false),
  (gen_random_uuid(),                        'Onix',      false),
  (gen_random_uuid(),                        'Six Zero',  false),
  (gen_random_uuid(),                        'Proton',    false),
  (gen_random_uuid(),                        'Head',      false),
  (gen_random_uuid(),                        'Wilson',    false)
on conflict (name) do nothing;
```

> **CRITICAL:** Keep the JOOLA `id` exactly `04db8591-37a3-4634-9d11-536975fa6935`. Changing it requires updating 5 frontend pages (`youtube`, `tiktok`, `twitter`, `reddit`, `influencers`) where this UUID is a literal.

---

## 2. `influencers` — JOOLA roster

CLAUDE.md audit (May-19): "JOOLA athlete cross-platform coverage: IG 6/6, YouTube 1/6 (only Ben Johns has channel URL), TikTok 0/6 handles in DB, X has no `x_handle` field on `influencers` schema."

The full pickleball player list defined in `backend/app/agents/news_scraper.py:38-48` as `JOOLA_PLAYERS` has 35 names. **Only 6 are seeded in the live `influencers` table** — these are the top-tier sponsored athletes whose posts get scraped:

| Name | IG handle (verify) | YouTube |
|---|---|---|
| Ben Johns | `benjohnspb` | `https://www.youtube.com/@BenJohns` |
| Collin Johns | `collinjohns` | — |
| Anna Bright | `anna.bright` | — |
| Tyson McGuffin | `tysonmcguffin` | — |
| Federico Staksrud | `fedestaksrud` | — |
| Simone Jardim | `simonejardim10` | — |

```sql
insert into influencers (brand_id, name, type, instagram_handle, youtube_channel_url, is_active)
values
  ('04db8591-37a3-4634-9d11-536975fa6935', 'Ben Johns',         'athlete', 'benjohnspb',     'https://www.youtube.com/@BenJohns', true),
  ('04db8591-37a3-4634-9d11-536975fa6935', 'Collin Johns',      'athlete', 'collinjohns',    null, true),
  ('04db8591-37a3-4634-9d11-536975fa6935', 'Anna Bright',       'athlete', 'anna.bright',    null, true),
  ('04db8591-37a3-4634-9d11-536975fa6935', 'Tyson McGuffin',    'athlete', 'tysonmcguffin',  null, true),
  ('04db8591-37a3-4634-9d11-536975fa6935', 'Federico Staksrud', 'athlete', 'fedestaksrud',   null, true),
  ('04db8591-37a3-4634-9d11-536975fa6935', 'Simone Jardim',     'athlete', 'simonejardim10', null, true);
```

**Verification step:** before scraping, visit each Instagram handle in a browser to confirm the username is current. Pickleball athletes change handles frequently.

### Optional: full 35-player roster

If you want to expand coverage, the full list from `news_scraper.py` is:

> Ben Johns, Collin Johns, Anna Bright, Tyson McGuffin, Federico Staksrud, Simone Jardim, Lea Jansen, Lacy Schneemann, Brooke Buckner, Kate Fahey, Milan Rane, John Lucian Goins, Patrick Smith, Noe Khlif, Alec LaMacchio, Aanik Lohani, Alka Strippoli, Bobbi Oshiro, Boone Casady, Chuck Taylor, Dayne Gingrich, Jake Kusmider, Johnny Goldberg, Jonathan Truong, Len Yang, Luke Geiser, Mota Alhouni, Rachel Rettger, Regina Franco Goldberg, Ryder Brown, Sammy Lee, Scott Crandall, Tam Trinh, Wil Shaffer, Zack Taylor

Handles for these are not in any source file — would need manual research per athlete.

---

## 3. Accounts for social platforms

Before scrapers can run for JOOLA, seed one account row per platform pointing at the official handle. These are referenced by the platform `page.tsx` files via `brand_id`.

```sql
-- TikTok (CLAUDE.md says @joolapickleball, 110 videos, 776K views)
insert into tiktok_accounts (brand_id, handle, profile_url) values
  ('04db8591-37a3-4634-9d11-536975fa6935',
   'joolapickleball',
   'https://www.tiktok.com/@joolapickleball');

-- X / Twitter (CLAUDE.md says @joolausa, 12 posts — mostly RTs of Tyson McGuffin)
insert into x_accounts (brand_id, handle, profile_url) values
  ('04db8591-37a3-4634-9d11-536975fa6935',
   'joolausa',
   'https://x.com/joolausa');

-- YouTube — JOOLA channel
-- (Per audit, scraper data quality is broken: 0 yt_comments rows for JOOLA brand_id)
insert into yt_channels (brand_id, channel_name, channel_url, is_primary, is_active) values
  ('04db8591-37a3-4634-9d11-536975fa6935',
   'JOOLA Pickleball',
   'https://www.youtube.com/@JoolaPickleball',
   true, true);

-- Instagram: no `ig_accounts` table — the IG scraper hard-codes
-- JOOLA_HANDLE = "joolapickleball" in frontend/scripts/scrape_joola_ig.py
-- and writes directly to joola_ig_posts / joola_ig_comments.
```

---

## 4. `news_sources` — already seeded by migration 006

20 pickleball media sites are upserted by `006_news_tables.sql`. No additional action needed. List for reference:

`pickleball.com, thedinkpickleball.com, pickleballunion.com, pickleballmagazine.com, usapickleball.org, ppatour.com, majorleaguepickleball.co, theapp.global, dupr.com, pickleheads.com, pickleballcentral.com, justpaddles.com, thekitchenpickle.com, pickleballportal.com, pickleballstudio.com, pickleballeffect.com, selkirk.com, worldpickleballmagazine.com, pickleballnewsasia.com, pickleballtoday.co`

---

## 5. JOOLA product keyword list

Used by AI enrichment (sentiment detection). From `backend/app/agents/news_scraper.py:55-59`:

> perseus, scorpeus, hyperion, agassi, magnus, joola paddle, joola ball, joola shoe, joola bag, joola apparel, joola grip

If you build a `joola_products` lookup table in the future, seed it from this list.
