# Power BI Report Accessibility Check

Paste a link to a public Power BI report and get an automated accessibility audit in a few minutes:
alt text, text contrast, chart colour contrast, keyboard access and tab order (with screenshots),
plus a list of the core and custom visuals used. It also clicks bookmark buttons and hovers chart
tooltips, so reports that hide content behind buttons are checked too.

**Try it:** https://www.dataschnitzel.at/a11y-check/

## What it checks

| Test | Passes when |
|---|---|
| Alt text | every visible, non-decorative visual has alt text written by the author |
| Text contrast | text reaches 4.5:1 against what is behind it (3:1 for large text), WCAG 2.2 |
| Chart colour contrast | chart colours reach 3:1 against the background and against touching colours |
| Keyboard access | every data or interactive visual can be reached with Tab |
| Tab order | tab stops follow the reading order (left to right, top to bottom) |

Scores are passed checks out of all checks, summed over all pages and button views. Every failed
check is listed with its page and visual. The results are automated evidence, not a WCAG
certification: screen reader output, meaningful alt text and colour-only meaning still need a person.

## How it works

```
static/index.html              the page people see: a single HTML file, no build step
app.py                         the API: queues checks, runs them, returns results and screenshots
audit/audit_accessibility.py   the audit itself: audit_report() and its checks
Dockerfile                     slim container image (Python + Chromium headless shell)
```

The API opens the report in headless Chromium with [Playwright](https://playwright.dev/python/),
reads every visible page from the browser, presses Tab through it and measures colours from the
rendered pixels. Then it clicks every button whose screen-reader label starts with "Bookmark" or
"Page navigation" (Power BI puts the action type there). Each new set of visuals on screen is
checked as a "view"; only visuals not seen before are scored again, while tab order is scored per
view. In every view it hovers two data points per new chart: report-page tooltips get the text and
chart colour checks, default tooltips a text contrast check.

### Which links work

| Link | Result |
|---|---|
| Publish to web: `https://app.powerbi.com/view?r=…` or `https://app.fabric.microsoft.com/view?r=…` | checked |
| Fabric community report viewer (`…/iframeEmbed/…`) | checked |
| Fabric community post (`https://community.fabric.microsoft.com/…`) | refused: the community site shows automated browsers a bot check. The page offers a bookmarklet that reads the report link from the post in the visitor's own browser. |
| Anything else, including reports that need a sign-in | refused with an explanation |

## Run it locally

Python 3.11 or newer (on Windows use `.venv\Scripts\` instead of `.venv/bin/`):

```bash
python -m venv .venv
```
```bash
.venv/bin/pip install -r requirements.txt
```
```bash
.venv/bin/python -m playwright install chromium
```
```bash
.venv/bin/python -m uvicorn app:app --port 8000
```

Open http://localhost:8000. On localhost the page calls the API on the same address.
To use an installed Google Chrome instead of Playwright's Chromium, set `BROWSER_CHANNEL=chrome`.

Or with Docker:

```bash
docker build -t a11y-check . && docker run -p 8000:8000 a11y-check
```

## Settings (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `ALLOWED_ORIGINS` | `http://localhost:8000` | comma-separated sites allowed to call the API, e.g. `https://example.com,https://www.example.com` for a page hosted on example.com |
| `BROWSER_CHANNEL` | (empty: Playwright Chromium) | `chrome` for an installed Google Chrome |
| `MAX_PAGES` | 12 | pages audited per report |
| `MAX_STATES` | 8 | button views checked per page (0 = don't click buttons) |
| `EXPLORE_SECONDS` | 300 | time per report for clicking buttons; pages are always checked |
| `MAX_QUEUE` | 20 | waiting checks before new ones are refused |
| `RATE_LIMIT` | 6 | checks one visitor can start per hour |
| `RESULT_MINUTES` | 60 | how long results and screenshots are kept |
| `VIEWER_HOSTS` | the community viewer's host | allowed report viewer hosts |

Results are kept in memory, so run **one** instance with one worker. A restart clears them.

## Host your own copy

1. **Image.** Every push to `main` runs `.github/workflows/build-image.yml`, which builds the image
   and pushes it to `ghcr.io/<your-github-name>/a11y-check` (lowercase). In GitHub › Packages ›
   a11y-check › Package settings, make it public so your host can pull it without a password.
2. **Container host.** Any host that runs a container with about 1 vCPU and 2 GB memory works. A
   host that scales to zero when idle keeps the cost low; the first check after a quiet period then
   waits while the container starts. Example with Azure Container Apps (logs off to avoid
   Log Analytics charges):
   ```bash
   az group create -n <resource-group> -l <region>
   az containerapp env create -n <environment> -g <resource-group> -l <region> --logs-destination none
   az containerapp create -n <app-name> -g <resource-group> --environment <environment> --image ghcr.io/<your-github-name>/a11y-check:latest --target-port 8000 --ingress external --cpu 1 --memory 2Gi --min-replicas 0 --max-replicas 1 --env-vars "ALLOWED_ORIGINS=https://example.com,https://www.example.com"
   ```
   Open `/api/health` on the address it prints to check that it runs, and set a budget alert
   with your cloud provider.
3. **Page.** In `static/index.html`, set your service address on the `<html>` tag
   (`data-api="https://a11y-api.example.com"`) and upload the file to your website.
   The bookmarklet on the page points to wherever the page is hosted, so it needs no change.

## Limits

- Custom visuals (Deneb, HTML Content…) are counted and their text is checked for contrast, but
  their chart colours often can't be read; the results list them under "What couldn't be checked".
- Button views and tooltips are sampled: up to `MAX_STATES` views per page, `EXPLORE_SECONDS` per
  report, two hovered data points per chart.
- Visuals covered by an opaque visual (a welcome picture over the dashboard) don't count as visible.

## Contributing

Issues and pull requests are welcome, especially reports the check gets wrong (please include a
Publish to web link) and ideas for new checks. By contributing you agree that your contribution is
licensed under the AGPL-3.0.

## License

[GNU Affero General Public License v3.0](LICENSE) (`AGPL-3.0-only`). You may use, change and share
this code, also commercially. If you distribute a changed version or offer it as a service to
others, you must publish your source code under the same license.

## Author

Karin Szilágyi · [dataschnitzel.at](https://www.dataschnitzel.at/en/) ·
[LinkedIn](https://www.linkedin.com/in/karinszilagyi/)
