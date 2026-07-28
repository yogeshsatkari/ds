
* Github action is not working as expected, it is pinging the health endpoint very late so we are also using cron-job.org for the same purpose.
* Automated background keep-awake pings via cron-job.org scheduled every 12 minutes during India daytime hours (7:00 AM - 2:00 AM IST).


## Cloudflare R2 Storage Pricing

This project uses **S3-compatible object storage** with **zero egress fees** via [Cloudflare R2]

### Free Tier (Per Month)
* **Storage:** 10 GB
* **Class A Ops (Writes):** 1 Million
* **Class B Ops (Reads):** 10 Million
