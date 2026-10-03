# Domain / DNS

## Status as of 2026-10-03

**No custom domain configured.** Hosting moved from Railway to Vercel; the app is reachable only via Vercel-generated subdomains. Production URLs are not recorded yet (the old Railway URL is retired).

| Service | URL (Vercel-generated, placeholder) |
|---|---|
| Frontend  | `https://<frontend>.vercel.app` |
| Backend   | `https://<backend>.vercel.app` |
| Analytics | `https://<analytics>.vercel.app` |

(Actual URLs are visible in Vercel → Project → Settings → Domains after first deploy.)

## How to add a custom domain (when ready)

1. Buy a domain (Cloudflare Registrar / Namecheap / Vercel domains).
2. In Vercel: Project → Settings → Domains → Add domain `app.joola-pulse.com` (or whatever).
3. Vercel shows the DNS record to create (CNAME target). Add that record at your DNS host.
4. Wait 5-30 min for DNS propagation + cert issuance.
5. Repeat for the backend project: `api.joola-pulse.com` (and the analytics project if it needs a public name).
6. Update the frontend's `SEO_API_URL` env var to `https://api.joola-pulse.com` (and `ANALYTICS_API_URL` if changed), update `CORS_ORIGINS` / `GOOGLE_REDIRECT_BASE_URL` on the backends, then redeploy.

## DNS records to plan for

| Record | Type | Target | TTL |
|---|---|---|---|
| `app.joola-pulse.com` | CNAME | target shown by Vercel for the frontend project | 300 |
| `api.joola-pulse.com` | CNAME | target shown by Vercel for the backend project | 300 |

If you want apex (`joola-pulse.com`) → frontend, you need either:
- A registrar that supports CNAME flattening (Cloudflare, Vercel), OR
- The A record Vercel shows for apex domains in Project → Settings → Domains.

## SSL / TLS

Vercel provisions certs automatically once the domain is verified. No manual cert management.

## CORS implications

If you switch to custom domains, the backend's CORS policy may need updating. Allowed origins come from the `CORS_ORIGINS` env var (comma-separated) on the backend and analytics projects — add the custom frontend domain there.

## Email / SMTP

Not configured. The Phase 2 "AI weekly briefing email digest" feature is unbuilt. When that feature lands you'll need:

- An SMTP provider (Resend, Postmark, SES)
- DNS records: SPF (TXT), DKIM (TXT), DMARC (TXT)
- The email sender domain configured at the SMTP provider

None of that exists today.
