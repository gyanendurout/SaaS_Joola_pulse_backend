-- Migration 002 — add 'single_url' as a valid crawl_mode value.
alter table runs drop constraint if exists runs_crawl_mode_check;
alter table runs add constraint runs_crawl_mode_check
  check (crawl_mode in (
    'full_site', 'sitemap_only', 'selected_urls',
    'product_category_only', 'single_url'
  ));
