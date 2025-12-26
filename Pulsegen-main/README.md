# SaaS Review Scraper (G2, Capterra, TrustRadius)

A Python-based scraper to collect SaaS product reviews from major platforms over a specified time range.  
The script supports multiple sources, normalizes outputs into JSON, and includes safeguards for JavaScript-heavy and bot-protected websites.

---

## 📌 Supported Sources

- **G2**
- **Capterra**
- **TrustRadius** *(Bonus source)*

---

## 🚀 Features

- Unified CLI interface for all sources
- Date range filtering (`start_date` → `end_date`)
- JavaScript-rendered page support using Playwright
- Headless and headful (visible browser) modes
- Pagination with safe termination
- Graceful handling of browser crashes and site blocking
- Normalized JSON output
- Debug HTML capture for troubleshooting

---

## 📁 Project Structure

