
# Steam Discussions Metrics Prototype

A highly optimized, polite web scraper designed to track unanswered threads, post-to-reply ratios, and community activity within specific EA game discussion boards on Steam. 

## 🛠️ Architecture & Server Courtesy
This tool is built as a **Proof of Concept (PoC)**. Because it interacts with public web elements, it has been engineered with strict guardrails to ensure it operates responsibly and minimizes server footprint:
* **Rate Limiting:** Enforces a hard `REQUEST_DELAY` (2.0s) between requests to prevent server strain.
* **Capped Scope:** Limits crawls to 2 forum pages and 15 fresh threads per app per run.
* **Smart Backoff:** Includes exponential backoff retry logic for `429` and `5xx` status codes.
* **Circuit Breaker:** Automatically halts execution after 5 consecutive throttling failures to prevent endless loops.

## ⚠️ Operational Risks & Constraints (Read Before Production)
Before migrating this prototype into a permanent production workflow, please note the following technical and partner considerations:

1. **Automation & Terms of Service (ToS):** Valve’s Subscriber Agreement prohibits automated data harvesting. While this script only reads public data and does not utilize a logged-in account, running automated scripts against Steam infrastructure technically breaches their standard ToS.
2. **IP Rate-Limiting & Blocking:** Since this runs via GitHub Actions, it utilizes shared cloud data center IP blocks. Steam employs aggressive automated defenses (Cloudflare). If these defenses flag the runner's IP, the script will experience `403 Forbidden` or `429 Too Many Requests` errors and fail to collect data.
3. **HTML Structural Fragility:** This script relies on frontend CSS/HTML selectors. If Valve updates the visual design or backend layout of Steam Discussions, the scraper will break and require manual selector updates.

## 🚀 Future Recommendations
To transition these insights into a robust, permanent corporate tool, the next team should explore:
* Requesting direct backend data feeds or compliant API access through EA's official Valve partner channels.
* Integrating residential proxy rotation if continued cloud-based execution is required.
