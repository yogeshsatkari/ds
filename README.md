## Health Monitoring
* Github action is not working as expected, it is pinging the health endpoint very late so we are also using cron-job.org for the same purpose.
* Automated availability checks hit `GET /health` during active hours (02:00–16:59 UTC) across two staggered schedules:

* **Monitor 1:** `3,28,54 2-16 * * *`
* **Monitor 2:** `16,41 2-16 * * *`




## Cloudflare R2 Storage Pricing

This project uses **S3-compatible object storage** with **zero egress fees** via [Cloudflare R2]

### Free Tier (Per Month)
* **Storage:** 10 GB
* **Class A Ops (Writes):** 1 Million
* **Class B Ops (Reads):** 10 Million

## Backlog
* **Dashboard Database (When scaling past 50 doctors):** Move dashboard patient listing from Cloudflare R2 to a simple database (like Supabase or Cloudflare D1) so fetching all discharges remains instant without slowing down the free Render server.

