# Domain / DNS

## Status as of 2026-05-19

**No custom domain configured.** The app is reachable only via Railway-generated subdomains.

| Service | URL (Railway-generated, illustrative) |
|---|---|
| Frontend | `https://<frontend-service-name>.up.railway.app` |
| Backend  | `https://<backend-service-name>.up.railway.app` |

(Actual URLs are visible in Railway dashboard → Service → Settings → Networking after first deploy.)

## How to add a custom domain (when ready)

1. Buy a domain (Cloudflare Registrar / Namecheap / Vercel domains).
2. In Railway: Service → Settings → Networking → Custom Domain → Add domain `app.joola-pulse.com` (or whatever).
3. Railway shows a CNAME target. Add that CNAME record at your DNS host.
4. Wait 5-30 min for DNS propagation + Let's Encrypt cert issuance.
5. Repeat for the backend: `api.joola-pulse.com` → backend service.
6. Update the frontend's `SEO_API_URL` env var to `https://api.joola-pulse.com`.

## DNS records to plan for

| Record | Type | Target | TTL |
|---|---|---|---|
| `app.joola-pulse.com` | CNAME | `<frontend>.up.railway.app` | 300 |
| `api.joola-pulse.com` | CNAME | `<backend>.up.railway.app` | 300 |

If you want apex (`joola-pulse.com`) → frontend, you need either:
- A registrar that supports CNAME flattening (Cloudflare, Vercel), OR
- An A record pointing at Railway's static IP (less recommended; IP can change).

## SSL / TLS

Railway provisions Let's Encrypt certs automatically once the CNAME is verified. No manual cert management.

## CORS implications

If you switch to custom domains, the backend's CORS policy may need updating. Currently `app/main.py` is wide-open in dev. Audit `app/main.py` CORS middleware before going to production with a custom domain.

## Email / SMTP

Not configured. The Phase 2 "AI weekly briefing email digest" feature is unbuilt. When that feature lands you'll need:

- An SMTP provider (Resend, Postmark, SES)
- DNS records: SPF (TXT), DKIM (TXT), DMARC (TXT)
- The email sender domain configured at the SMTP provider

None of that exists today.
