# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Karin Szilágyi
"""Automated accessibility audit of one Power BI report, used by the public check in app.py.

audit_report() opens every visible page in a browser, presses Tab through it and returns
scores, issues, a visual inventory and one tab-order screenshot per page.

Tests (scores are passed checks / all checks, summed over all pages):
    alt_text         visible, non-decorative visuals with author alt text (text boxes excluded)
    text_contrast    text items with 4.5:1 (3:1 for large text) against what is behind them
    visual_contrast  chart colours with 3:1 against the background and against touching colours
    keyboard_access  visible data/interactive visuals reachable with Tab
    tab_order        pairs of tab stops in left-to-right, top-to-bottom (Z) reading order
Decorative = an image, shape or text box hidden from the tab order in Power BI.

Visual inventory (not scored), per visible visual:
    custom       runs in Power BI's custom-visual sandbox (AppSource, organizational or .pbiviz)
    core data    built-in visual that shows data: charts, cards, tables, slicers, maps, ...
    core layout  built-in text box, image, shape, button or navigator
A built-in visual is recognised by how it renders, not by a list of names, so changes to the
core list (e.g. the built-in Gantt preview replacing the AppSource Gantt) need no update.
visuals_core_share = core data / (core data + custom).
report_area (JSON) shows how much report a viewer can reach: every page of the report definition
(visible, hidden, tooltip or drillthrough), which hidden pages a button opens, and the share of the page
each bookmark view covers with visuals it adds (0.6 = 60% of the page). report_area_summary is the same in
one line, e.g. "1 visible page + 3 hidden pages reached + bookmark overlays 0.8 = 4.8 pages".
visuals_items is the same inventory one visual at a time (JSON, see describe()): its category
(core, custom, text, shape, image, button), a sub-kind (shape outline, image format, text role,
button action), its visible text or the words inside an SVG image, and its alt text. Core and custom
visuals add their size and title; core visuals also their legend entries, axis titles, table columns,
number of data points, or (cards, slicers) the text they show. stop is the visual's number on the
tab-order screenshot (none: Tab skips it); view is set when the visual first appeared in a button view.
report_area also lists each page's size in Power BI (e.g. 1280 x 720) and its page view: fit to page,
fit to width (a tall page then scrolls) or actual size. A long page still counts as one page.
Automated results are evidence; they do not replace a manual check.
"""

import base64
import collections
import datetime
import io
import json
import re
import time
import unicodedata
from pathlib import Path
from urllib.parse import urlparse

import numpy as np
from PIL import Image, ImageDraw, ImageFont

POWERBI_HOSTS = ("app.powerbi.com", "app.fabric.microsoft.com")


def slugify(text, limit=60):
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:limit].strip("-")


TESTS = ["alt_text", "text_contrast", "visual_contrast", "keyboard_access", "tab_order"]
FIELDS = (["report_id", "audit_status", "audited_at", "pages_audited", "states_audited"]
          + [f"{test}_{part}" for test in TESTS for part in ("score", "checked", "issues")]
          + ["visuals_core_data", "visuals_core_layout", "visuals_custom", "visuals_core_share",
             "visuals_core_data_types", "visuals_custom_types", "visuals_by_page", "visuals_items",
             "report_area_summary", "report_area"]
          + ["tab_order_images", "tab_order_titles", "audit_notes"])
VIEWPORT = {"width": 1600, "height": 1100}
DECORATIVE_TYPES = {"img", "image", "shape", "text box", "textbox"}
TEXT_TYPES = {"text box", "textbox"}
DEFAULT_LABELS = {"", "image", "img", "shape", "text box", "textbox"}
MAX_ISSUES = 5
MAX_COLOURS = 12
# Bookmark states: buttons whose screen-reader label starts with their action type are clicked.
ACTION = re.compile(r"^(Bookmark|Page navigation)\b\s*", re.I)
MAX_STATES = 10     # extra states (after a button click) checked per page
MAX_CLICKS = 40     # button clicks per page
MAX_HOVERS = 12     # tooltip hovers per state
MAX_WINDOW = {"width": 4000, "height": 8000}  # a taller or wider report page grows the window up to this


def park(page):
    """Rests the mouse on the viewer's bottom bar, wherever the (possibly enlarged) window ends."""
    size = page.viewport_size
    page.mouse.move(size["width"] // 2, size["height"] - 6)


TIP_SCOPE = "[data-a11y-tip]"
# Built-in layout elements, by aria-roledescription with spaces removed, lower case.
LAYOUT_TYPES = {"textbox", "img", "image", "shape", "button", "actionbutton", "pagenavigator", "bookmarknavigator", "navigator"}
# Visuals of the page (or of one open tooltip, scope '[data-a11y-tip]').
# covered: an opaque part of another visual (image, filled shape, solid background) lies on top at
#   every sample point, e.g. a full-page welcome picture that a bookmark button removes.
# hit: a point where a click reaches this visual (it is the top element there), or null.
VISUALS_JS = """scope => {
    const root = scope ? document.querySelector(scope) : document;
    if (!root) return [];
    const alpha = s => { const m = s && s.match(/rgba?\\(([^)]+)\\)/); if (!m) return 0;
        const p = m[1].split(/[ ,\\/]+/).filter(Boolean); return p.length > 3 ? parseFloat(p[3]) : 1; };
    const opaque = e => {
        const tag = e.tagName.toLowerCase();
        if (tag === 'img' || tag === 'image' || tag === 'canvas') return true;
        const cs = getComputedStyle(e);
        if (parseFloat(cs.opacity) < 0.9) return false;
        if (alpha(cs.backgroundColor) >= 0.9 || (cs.backgroundImage && cs.backgroundImage !== 'none')) return true;
        return e instanceof SVGGeometryElement && cs.fill !== 'none' && alpha(cs.fill) * parseFloat(cs.fillOpacity || 1) >= 0.9;
    };
    return Array.from(root.querySelectorAll('.visualContainer')).map((v, i) => {
        const r = v.getBoundingClientRect(), cs = getComputedStyle(v);
        const points = [[.5, .5], [.2, .2], [.8, .2], [.2, .8], [.8, .8]].map(([fx, fy]) => [r.x + r.width * fx, r.y + r.height * fy])
            .filter(([x, y]) => x >= 0 && y >= 0 && x < innerWidth && y < innerHeight);
        let hit = null, covered = points.length > 0;
        for (const [x, y] of points) {
            const stack = document.elementsFromPoint(x, y);
            if (hit === null && stack.length && v.contains(stack[0])) hit = [x, y];
            const own = stack.findIndex(e => v.contains(e));
            if (own < 0 || !stack.slice(0, own).some(e => e.closest('.visualContainer') && opaque(e))) covered = false;
        }
        return {i, type: (v.getAttribute('aria-roledescription') || '').trim(), tab: v.getAttribute('tabindex'),
            label: (v.getAttribute('aria-label') || '').trim(), text: (v.innerText || '').trim().slice(0, 40),
            rect: [r.x, r.y, r.width, r.height], canvas: v.querySelectorAll('canvas, iframe').length,
            sandbox: !!v.querySelector('iframe[name="visual-sandbox"], iframe[src*="cvSandbox"]'), hit, covered,
            shown: cs.visibility === 'visible' && r.width >= 2 && r.height >= 2 && parseFloat(cs.opacity) > 0.05};
    });
}"""

# Details of the layout elements, for the visual inventory: which built-in element it is (from the
# classes Power BI renders), the outline and fill of a shape, the format of an image and its SVG source.
DETAIL_JS = """indices => {
    const all = document.querySelectorAll('.visualContainer');
    return indices.map(i => {
        const v = all[i];
        if (!v) return null;
        const has = sel => !!v.querySelector(sel);
        // SVG labels repeat their text in a <title> child (the hover text): leave that out.
        const clean = e => { const t = e.querySelector('title'), all = e.innerText || e.textContent || '';
            return (t ? all.replace(t.textContent, '') : all).trim().replace(/\\s+/g, ' '); };
        const texts = (sel, n, len = 60) => [...new Set(Array.from(v.querySelectorAll(sel)).map(clean).filter(Boolean))].slice(0, n).map(t => t.slice(0, len));
        const first = sel => texts(sel, 1, 120)[0] || '';
        const kind = has('.visual-shape, .shapeVisual') ? 'shape' : has('.visual-image, .imageContainer') ? 'image'
            : has('.visual-textbox') ? 'text' : has('.visual-actionButton, .bookmarkNavigator, .pageNavigator') ? 'button' : '';
        const path = v.querySelector('svg path');
        const shapeEl = v.querySelector('svg path, svg rect, svg ellipse');
        const img = v.querySelector('img, image');
        const src = img ? (img.getAttribute('src') || img.getAttribute('href') || '') : '';
        return {kind, d: path ? (path.getAttribute('d') || '').slice(0, 200) : '',
            fill: shapeEl ? getComputedStyle(shapeEl).fill : '', stroke: shapeEl ? getComputedStyle(shapeEl).stroke : '',
            img: src.startsWith('data:image/svg') ? 'svg' : src ? 'picture' : '',
            svg: src.startsWith('data:image/svg') ? src.slice(0, 200000) : '',
            text: (v.innerText || '').trim().replace(/\\s+/g, ' ').slice(0, 400),
            // Core and custom visuals: the title area (outside a custom visual's sandbox) and what a chart shows.
            title: first('.visualTitle'), subtitle: first('.visualSubTitle, [class*="subTitle"], [class*="SubTitle"]'),
            legend: texts('.legendText, .legend-item-text, [class*="legendText"]', 8), axes: texts('.xAxisLabel, .yAxisLabel', 4),
            columns: texts('[role="columnheader"]', 10), points: v.querySelectorAll('[role="option"]').length};
    });
}"""

# The tooltip on screen, if any. A report-page tooltip holds visuals; it is marked so VISUALS_JS and
# PAINT_JS can be scoped to it. A default tooltip returns its text and background colours.
TIP_JS = """() => {
    document.querySelectorAll('[data-a11y-tip]').forEach(e => e.removeAttribute('data-a11y-tip'));
    const tip = Array.from(document.querySelectorAll('.tooltip-container')).find(e => {
        const r = e.getBoundingClientRect(), cs = getComputedStyle(e);
        return r.width > 4 && r.height > 4 && cs.visibility === 'visible' && cs.display !== 'none' && parseFloat(cs.opacity) > 0.5; });
    if (!tip) return null;
    const vis = Array.from(tip.querySelectorAll('.visualContainer'));
    if (vis.length) {
        tip.setAttribute('data-a11y-tip', '1');
        const sig = vis.map(v => { const r = v.getBoundingClientRect();
            return (v.getAttribute('aria-roledescription') || '') + ':' + Math.round(r.width / 10) + 'x' + Math.round(r.height / 10); }).join('/');
        const title = vis.map(v => (v.getAttribute('aria-label') || v.innerText || '').trim()).find(t => t) || '';
        return {page: true, sig, title: title.split('\\n')[0].slice(0, 40)};
    }
    const bgOf = el => { for (let e = el; e && e.nodeType === 1; e = e.parentElement) {
        const c = getComputedStyle(e).backgroundColor, m = c.match(/rgba?\\(([^)]+)\\)/);
        if (m) { const p = m[1].split(/[ ,\\/]+/).filter(Boolean).map(parseFloat); if (p.length < 4 || p[3] > 0.5) return p.slice(0, 3); } }
        return null; };
    const rows = [];
    const walker = document.createTreeWalker(tip, NodeFilter.SHOW_TEXT);
    while (walker.nextNode() && rows.length < 12) {
        const el = walker.currentNode.parentElement, text = walker.currentNode.textContent.trim();
        if (!text || !el) continue;
        const cs = getComputedStyle(el), m = cs.color.match(/rgba?\\(([^)]+)\\)/);
        if (!m) continue;
        rows.push({text: text.slice(0, 25), fg: m[1].split(/[ ,\\/]+/).filter(Boolean).map(parseFloat).slice(0, 3), bg: bgOf(el),
                   size: parseFloat(cs.fontSize), weight: parseInt(cs.fontWeight) || 400});
    }
    return {page: false, rows};
}"""

# Collects visible text and chart marks of one frame (or of one open tooltip); marks are tagged so
# they can be hidden.
PAINT_JS = """cfg => {
    const root = cfg.scope ? document.querySelector(cfg.scope) : document.body;
    if (!root) return {texts: [], marks: [], width: innerWidth};
    const vis = Array.from(root.querySelectorAll('.visualContainer'));
    const rgba = s => { const m = s && s.match(/rgba?\\(([^)]+)\\)/); if (!m) return null;
        const p = m[1].split(/[ ,\\/]+/).filter(Boolean).map(parseFloat); return [p[0], p[1], p[2], p.length > 3 ? p[3] : 1]; };
    const opacity = el => { let o = 1; for (let e = el; e && e.nodeType === 1; e = e.parentElement) o *= parseFloat(getComputedStyle(e).opacity); return o; };
    const visualOf = el => { const v = el.closest('.visualContainer'); return v ? vis.indexOf(v) : -1; };
    const box = r => [r.x, r.y, r.width, r.height];
    const texts = [], marks = [], seen = new Set();
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    while (walker.nextNode() && texts.length < cfg.maxText) {
        const node = walker.currentNode, el = node.parentElement;
        if (!node.textContent.trim() || !el || seen.has(el)) continue;
        seen.add(el);
        if (el.closest('script,style,title,desc,defs,clipPath,mask,[aria-hidden="true"]')) continue;
        const cs = getComputedStyle(el);
        if (cs.visibility !== 'visible' || cs.display === 'none') continue;
        // Screen-reader-only helpers, e.g. a table's "Select Row", are clipped to a 1 px box.
        if (el.closest('.visually-hidden, .sr-only')) continue;
        const own = el.getBoundingClientRect();
        if (own.width < 2 || own.height < 2) continue;
        const range = document.createRange(); range.selectNodeContents(node);
        const r = range.getBoundingClientRect();
        if (r.width < 2 || r.height < 4) continue;
        const svg = el instanceof SVGElement, colour = rgba(svg ? cs.fill : cs.color);
        if (!colour) continue;
        const alpha = colour[3] * opacity(el) * (svg ? parseFloat(cs.fillOpacity || 1) : 1);
        if (alpha < 0.05) continue;
        texts.push({visual: visualOf(el), rect: box(r), colour: colour.slice(0, 3), alpha,
            size: parseFloat(cs.fontSize), weight: parseInt(cs.fontWeight) || 400, text: node.textContent.trim().slice(0, 40)});
    }
    const skip = /axis|grid|tick|legend|label|background|selection|tooltip|scroll|clip|overlay|interactiv|hit/i;
    for (const el of root.querySelectorAll('svg rect, svg path, svg circle, svg ellipse, svg polygon')) {
        if (marks.length >= cfg.maxMarks) break;
        if (el.closest('defs,clipPath,mask,pattern,marker')) continue;
        let bad = false;
        for (let e = el; e && !(e instanceof SVGSVGElement); e = e.parentElement)
            if (skip.test(e.getAttribute('class') || '')) { bad = true; break; }
        if (bad) continue;
        const cs = getComputedStyle(el);
        if (cs.visibility !== 'visible' || cs.display === 'none') continue;
        const r = el.getBoundingClientRect();
        if (r.width < 1 || r.height < 1) continue;
        const op = opacity(el), fill = rgba(cs.fill), stroke = rgba(cs.stroke);
        const fillAlpha = fill ? fill[3] * parseFloat(cs.fillOpacity || 1) * op : 0;
        const strokeAlpha = stroke ? stroke[3] * parseFloat(cs.strokeOpacity || 1) * op : 0;
        let mark = null;
        if (fill && fillAlpha >= 0.2 && r.width * r.height >= 9)
            mark = {kind: 'fill', colour: fill.slice(0, 3), alpha: fillAlpha};
        else if (stroke && strokeAlpha >= 0.2 && parseFloat(cs.strokeWidth) >= 1.5 && el.tagName.toLowerCase() === 'path')
            mark = {kind: 'line', colour: stroke.slice(0, 3), alpha: strokeAlpha};
        if (!mark) continue;
        el.setAttribute('data-a11y-mark', '1');
        marks.push({...mark, visual: visualOf(el), rect: box(r)});
    }
    return {texts, marks, width: innerWidth};
}"""

HIDE_TEXT_CSS = ("*{color:transparent!important;-webkit-text-fill-color:transparent!important;text-shadow:none!important;"
                 "text-decoration-color:transparent!important}svg text,svg tspan{fill:transparent!important;stroke:transparent!important}")
HIDE_MARKS_CSS = HIDE_TEXT_CSS + "[data-a11y-mark]{visibility:hidden!important}"
STYLE_JS = """css => { let s = document.getElementById('a11y-audit-style');
    if (css === null) { if (s) s.remove(); return; }
    if (!s) { s = document.createElement('style'); s.id = 'a11y-audit-style'; document.head.appendChild(s); }
    s.textContent = css; }"""
# The focused tab stop: "v<n>" for a visual, "g<n>" for a group; null outside the given group.
STOP_JS = """group => { const e = document.activeElement; if (!e) return null;
    const groups = document.querySelectorAll('.visualContainerGroup');
    if (group !== null && !groups[group].contains(e)) return null;
    const v = e.closest('.visualContainer');
    if (v) return 'v' + Array.from(document.querySelectorAll('.visualContainer')).indexOf(v);
    const g = e.closest('.visualContainerGroup');
    return g ? 'g' + Array.from(groups).indexOf(g) : null; }"""


# ---------- colour maths (WCAG 2.x) ----------

def luminance(rgb):
    c = np.asarray(rgb, float) / 255
    c = np.where(c <= 0.03928, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def contrast(a, b):
    high, low = sorted((luminance(a), luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def blend(fg, alpha, bg):
    return tuple(round(f * alpha + b * (1 - alpha)) for f, b in zip(fg, bg))


def hexcolour(rgb):
    return "#%02x%02x%02x" % tuple(int(v) for v in rgb)


def pixels(img, rect):
    x, y, w, h = rect
    x0, y0 = max(int(x), 0), max(int(y), 0)
    x1, y1 = min(int(x + w), img.shape[1]), min(int(y + h), img.shape[0])
    if x1 <= x0 or y1 <= y0:
        return None
    return img[y0:y1, x0:x1]


def background(img, rect):
    area = pixels(img, rect)
    return None if area is None else tuple(int(v) for v in np.median(area.reshape(-1, 3), axis=0))


def painted(with_text, without_text, rect):
    """True if the text shows on screen: hiding it changes some pixels. Screen-reader-only helpers
    (e.g. a table's "Select Row") and text under another visual change nothing."""
    a, b = pixels(with_text, rect), pixels(without_text, rect)
    if a is None or b is None:
        return False
    return int((np.abs(a.astype(int) - b.astype(int)).max(axis=2) > 6).sum()) >= 3


def short_label(text, limit=40):
    """A button's label up to its first sentence: "Why did profit fall? Opens a panel" -> "Why did profit fall?"."""
    text = re.sub(r"\s+", " ", text).strip()
    match = re.match(r"(.+?[?!]|.+?(?=[.:] ))", text)
    text = match.group(1).strip() if match else text.rstrip(".")
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] + "…"
    return text.replace(";", ",").replace("]", ")")


def screenshot(page):
    return np.array(Image.open(io.BytesIO(page.screenshot())).convert("RGB"))


# ---------- page handling ----------

def wait_rendered(page, frame, settle_ms=4000):
    frame.locator(".visualContainer").first.wait_for(state="attached", timeout=60000)
    previous, stable = -1, 0
    for _ in range(60):
        page.wait_for_timeout(500)
        count = frame.locator(".visualContainer").count()
        stable = stable + 1 if count == previous else 0
        previous = count
        if stable >= 3:
            break
    page.wait_for_timeout(settle_ms)  # custom visuals render after their containers


# How far the report page reaches past what the report frame shows: (right, bottom) in pixels.
# Covers a scrolling canvas (fit to width, actual size) and a page cut off by the frame's edge.
OVERFLOW_JS = """() => {
    const host = document.querySelector('.visualContainerHost');
    if (!host) return [0, 0];
    const r = host.getBoundingClientRect();
    let x = Math.ceil(r.right - innerWidth), y = Math.ceil(r.bottom - innerHeight);
    for (let c = host.parentElement; c && c !== document.documentElement; c = c.parentElement) {
        const s = getComputedStyle(c);
        if (/(auto|scroll)/.test(s.overflowY)) y = Math.max(y, c.scrollHeight - c.clientHeight);
        if (/(auto|scroll)/.test(s.overflowX)) x = Math.max(x, c.scrollWidth - c.clientWidth);
    }
    return [x, y];
}"""


def fit_window(page, frame):
    """Sizes the browser window so the whole report page shows without scrolling. Reports that are not
    16:9 (tall pages shown fit to width, or actual size) otherwise scroll, and everything outside the
    window is left out of the screenshot and the checks. Returns a note when the page still does not fit."""
    if page.viewport_size != VIEWPORT:
        page.set_viewport_size(VIEWPORT)  # each page starts from the normal size: pages can differ
        wait_rendered(page, frame, settle_ms=1500)
    for _ in range(5):  # growing can change the layout again (e.g. a scrollbar disappears)
        right, bottom = frame.evaluate(OVERFLOW_JS)
        if right <= 2 and bottom <= 2:
            return ""
        size = page.viewport_size
        # Grow only the side that overflows: on a fit-to-width page a wider window also makes the page taller.
        grow = lambda current, extra, limit: min(current + extra + 20, limit) if extra > 2 else current
        grown = {"width": grow(size["width"], right, MAX_WINDOW["width"]),
                 "height": grow(size["height"], bottom, MAX_WINDOW["height"])}
        if grown == size:
            break
        page.set_viewport_size(grown)
        wait_rendered(page, frame, settle_ms=1500)
    right, bottom = frame.evaluate(OVERFLOW_JS)
    if right <= 2 and bottom <= 2:
        return ""
    return (f"page is larger than the audit window ({max(right, 0)} px wider, {max(bottom, 0)} px taller), "
            "the part outside was not checked")


def report_frame(page):
    for _ in range(120):
        for frame in page.frames:
            try:
                if urlparse(frame.url).hostname in POWERBI_HOSTS and frame.locator(".visualContainerHost").count():
                    return frame
            except Exception:
                pass
        page.wait_for_timeout(500)
    raise RuntimeError("Power BI report did not load")


PAGE_TABS = ".section.dynamic.thumbnail-container"


def pager_position(frame):
    """(current, total) from the "1 of 2" page counter; None on a hidden page, where it only says "Pages"."""
    pager = frame.locator(".middleText")
    if not pager.count():
        return (1, 1)
    match = re.search(r"(\d+)\s*of\s*(\d+)", pager.first.inner_text())
    return (int(match.group(1)), int(match.group(2))) if match else None


def list_pages(frame):
    """Pages a viewer can open. Pages hidden in Power BI keep a tab element of zero size.
    A report can open on a hidden page (e.g. a cover reached through a pageName link): that
    page is audited first as "start", then every visible page."""
    if frame.locator(PAGE_TABS).count():
        tabs = frame.evaluate("""sel => Array.from(document.querySelectorAll(sel)).map((e, i) =>
            ({i, name: e.innerText.trim(), shown: e.getBoundingClientRect().width > 0,
              selected: e.classList.contains('selected')}))""", PAGE_TABS)
        shown = [tab for tab in tabs if tab["shown"]]
        start = [] if any(tab["selected"] for tab in shown) else [("start", -1, "start-page")]
        return start + [("pane", tab["i"], tab["name"]) for tab in shown]
    # The page counter's flyout lists the visible pages by name, also while a hidden page is open.
    names = frame.evaluate("() => Array.from(document.querySelectorAll('.navFlyout li')).map(l => l.innerText.trim())")
    position = pager_position(frame)
    if position is None:
        return [("start", -1, "start-page")] + [("pager", i, name) for i, name in enumerate(names or [""])]
    return [("pager", i, names[i] if len(names) == position[1] else "") for i in range(position[1])]


def open_page(page, frame, kind, index):
    """Opens the page explicitly: a report can open on any page, not only the first."""
    if kind == "start":
        return  # the page the report opened on; audited before any navigation
    if kind == "pane":
        tab = frame.locator(PAGE_TABS).nth(index)
        if "selected" in (tab.get_attribute("class") or ""):
            return
        try:
            tab.click(timeout=5000)
        except Exception:
            tab.dispatch_event("click")  # tab scrolled out of the page-tab carousel
    else:
        position = pager_position(frame)
        if position is None:  # on a hidden page: Next Page goes to the first visible page
            frame.get_by_role("button", name="Next Page").click()
            wait_rendered(page, frame)
            position = pager_position(frame)
            if position is None:
                raise RuntimeError("could not leave the hidden start page")
        current = position[0]
        if current == index + 1:
            return
        button = "Next Page" if current < index + 1 else "Previous Page"
        for _ in range(abs(index + 1 - current)):
            frame.get_by_role("button", name=button).click()
            page.wait_for_timeout(500)
    wait_rendered(page, frame)


def frame_transform(frame, inner_width):
    """Offset and scale that map a frame's own coordinates to page screenshot pixels."""
    if frame.parent_frame is None:
        return 0, 0, 1
    box = frame.frame_element().bounding_box()
    if not box or not inner_width:
        return None
    return box["x"], box["y"], box["width"] / inner_width


def to_page(rect, transform):
    x, y, scale = transform
    return [x + rect[0] * scale, y + rect[1] * scale, rect[2] * scale, rect[3] * scale]


def inside(rect, outer):
    cx, cy = rect[0] + rect[2] / 2, rect[1] + rect[3] / 2
    return outer[0] <= cx <= outer[0] + outer[2] and outer[1] <= cy <= outer[1] + outer[3]


def tab_stops(page, frame, first_key, group, max_steps):
    """Presses first_key, then Tab until focus wraps to the first stop or leaves the group."""
    stops = []
    page.keyboard.press(first_key)
    for _ in range(max_steps):
        page.wait_for_timeout(120)
        stop = frame.evaluate(STOP_JS, group)
        if stop is None or (stops and stop == stops[0]):
            break
        if stop not in stops:
            stops.append(stop)
        page.keyboard.press("Tab")
    return stops


def tab_sequence(page, frame, max_steps, group=None, depth=0):
    """Visuals in keyboard order. Tab moves between groups and ungrouped visuals;
    Enter opens a group, whose members are expanded in place."""
    if group is None:
        frame.evaluate("() => document.querySelector('.visualContainerHost').focus()")
        stops = tab_stops(page, frame, "Tab", None, max_steps)
    else:
        frame.evaluate("i => document.querySelectorAll('.visualContainerGroup')[i].focus()", group)
        stops = tab_stops(page, frame, "Enter", group, max_steps)
    sequence = []
    for stop in stops:
        index = int(stop[1:])
        if stop[0] == "v":
            sequence.append(index)
        elif index != group and depth < 5:
            sequence += [i for i in tab_sequence(page, frame, max_steps, index, depth + 1) if i not in sequence]
    return sequence


def reading_order(rects):
    """Z order: rows top to bottom (a row is the band of its smallest member), left to right within a row."""
    rows = []
    for index, (x, y, w, h) in sorted(rects.items(), key=lambda item: (item[1][1], item[1][0])):
        centre = y + h / 2
        for row in rows:
            if row["top"] <= centre <= row["top"] + row["height"]:
                row["items"].append((x, index))
                if h < row["height"]:
                    # Move the whole band, not just its height: a tall visual that starts higher (a map or
                    # background image behind the header) must not shift the row's top edge.
                    row["top"], row["height"] = y, h
                break
        else:
            rows.append({"top": y, "height": h, "items": [(x, index)]})
    order = [index for row in sorted(rows, key=lambda r: r["top"]) for _, index in sorted(row["items"])]
    return {index: rank for rank, index in enumerate(order)}


# ---------- one page ----------

def name_of(visual):
    text = visual["label"] or visual["text"] or visual["type"] or "visual"
    return '"%s"' % re.sub(r"\s+", " ", text)[:30]


def inventory(shown):
    """Counts the visible visuals of a page as core data, core layout or custom, by type name."""
    counts = {"data": collections.Counter(), "layout": collections.Counter(), "custom": collections.Counter()}
    for v in shown:
        kind = v["type"] or "visual without a type name"
        if v["sandbox"]:
            counts["custom"][kind] += 1
            continue
        counts["layout" if re.sub(r"\s+", "", kind.lower()) in LAYOUT_TYPES else "data"][kind] += 1
    return counts


# ---------- visual inventory, one visual at a time ----------

STOP_WORDS = set("""a an the and or of to in on for with by at from as is are was were be been this that these those it its
our we you your their they them he she his her not no but if so than then there here what which who whom how why when
up down out over under into onto about after before again more most less very can will just also each every one two
same both only own such too do does did has have had""".split())
BUTTON_TYPES = {"button", "actionbutton", "bookmarknavigator", "pagenavigator", "navigator"}


def keywords(text, limit=4):
    """The first few content words of a text box."""
    found, seen = [], set()
    for word in re.findall(r"[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ'-]{2,}", text):
        key = word.lower().strip("'-")
        if len(key) < 4 or key in STOP_WORDS or key in seen:
            continue
        seen.add(key)
        found.append(key)
        if len(found) == limit:
            break
    return found


def text_role(text):
    """A guess at what a text box is for, from its wording."""
    t = text.strip()
    if not t:
        return "Empty"
    if re.match(r"^(source|data|by)\b", t, re.I):
        return "Footnote"
    if re.match(r"^\d{2}\s", t) or (t.isupper() and len(t.split()) <= 8):
        return "Section heading"
    if len(t.split()) <= 9 and not t.endswith("."):
        return "Heading"
    return "Body text"


def shape_kind(d, width, height):
    """The outline of a shape, from its SVG path."""
    d = d.strip()
    points = re.findall(r"[ML]\s*(-?[\d.e-]+)[\s,]+(-?[\d.e-]+)", d)
    if re.search(r"[Aa]", d) and len(points) <= 5:
        return "Rounded rectangle"
    if d.upper().endswith("Z") and len(points) == 4 and not re.search(r"[AaCcQq]", d):
        return "Rectangle"
    if len(points) == 2 and not d.upper().endswith("Z"):
        (x1, y1), (x2, y2) = [(float(a), float(b)) for a, b in points]
        if abs(y1 - y2) < 0.5:
            return "Horizontal line"
        if abs(x1 - x2) < 0.5:
            return "Vertical line"
    if max(width, height) <= 40:
        return "Small shape (icon size)"
    return "Custom shape"


def hex_colour(css):
    parts = re.findall(r"[\d.]+", css or "")
    if len(parts) < 3 or (len(parts) > 3 and float(parts[3]) < 0.1):
        return ""
    return "#" + "".join(f"{round(float(n)):02X}" for n in parts[:3])


def svg_words(src, limit=12):
    """Words written inside an SVG image (its <text> elements), or [] if there are none."""
    try:
        head, body = src.split(",", 1)
        from urllib.parse import unquote
        svg = base64.b64decode(body).decode("utf-8", "ignore") if ";base64" in head else unquote(body)
    except Exception:
        return []
    words = []
    for match in re.finditer(r"<text\b[^>]*>(.*?)</text>", svg, re.S | re.I):
        text = re.sub(r"<[^>]+>", " ", match.group(1))
        text = re.sub(r"\s+", " ", text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")).strip()
        if text and text not in words:
            words.append(text[:40])
        if len(words) == limit:
            break
    return words


def describe(visual, detail, page_number):
    """One visual of the inventory: category, sub-kind, visible text and alt text, for the results page."""
    detail = detail or {}
    kind = re.sub(r"\s+", "", (visual["type"] or "").lower())
    label = re.sub(r"\s+", " ", visual["label"]).strip()
    alt = "" if label.lower() in DEFAULT_LABELS or label.lower() == (visual["type"] or "").lower() else label[:160]
    text = detail.get("text", "")
    x, y, width, height = (round(n) for n in visual["rect"])
    item = {"p": page_number, "type": visual["type"] or "Visual", "alt": alt, "hidden": visual["tab"] == "-1",
            "pos": [x, y]}
    if visual.get("stop"):
        item["stop"] = visual["stop"]
    if visual["sandbox"] or kind not in LAYOUT_TYPES:
        core = not visual["sandbox"]
        item.update(cat="core" if core else "custom", kind=visual["type"] or ("Visual" if core else "Custom visual"),
                    size=[width, height], title=detail.get("title", "")[:120], subtitle=detail.get("subtitle", "")[:120])
        if core:  # a custom visual's content is in its sandbox and can't be read
            item.update({k: detail[k] for k in ("legend", "axes", "columns") if detail.get(k)})
            if detail.get("points"):
                item["points"] = detail["points"]
            if not item["title"] and not any(detail.get(k) for k in ("legend", "axes", "columns")):
                item["text"] = text[:120]  # cards, KPIs, slicers: what the visual shows
    elif kind in TEXT_TYPES or detail.get("kind") == "text":
        item.update(cat="text", kind=text_role(text), text=text[:160], words=keywords(text))
    elif kind in BUTTON_TYPES or detail.get("kind") == "button":
        action = ACTION.match(label)
        item.update(cat="button", kind=(action.group(1).capitalize() if action else "Button"),
                    text=text[:80], alt=ACTION.sub("", label).strip(" .")[:160] or alt)
    elif detail.get("kind") == "shape" or (kind == "shape" and detail.get("kind") != "image"):
        outline = shape_kind(detail.get("d", ""), width, height)
        colour = detail.get("stroke") if outline.endswith(" line") else detail.get("fill")
        item.update(cat="shape", kind=outline, size=[width, height], fill=hex_colour(colour), text=text[:80])
    else:
        svg = detail.get("img") == "svg"
        item.update(cat="image", kind="SVG graphic" if svg else "Picture", size=[width, height],
                    words=svg_words(detail.get("svg", "")) if svg else [])
    return item


def visual_details(frame, visuals):
    """DETAIL_JS for these visuals of the page, in the same order; empty dicts if it fails."""
    try:
        return frame.evaluate(DETAIL_JS, [v["i"] for v in visuals]) or [{} for _ in visuals]
    except Exception:
        return [{} for _ in visuals]


def listing(counter):
    return ", ".join(f"{kind} x{n}" if n > 1 else kind for kind, n in counter.most_common())


def visual_key(v):
    """Identifies a visual across bookmark states: same type, label and place."""
    return f'{v["type"]}|{v["label"][:60]}|' + ",".join(str(round(n / 8)) for n in v["rect"])


def collect(page, frame, scope=None):
    """Visuals of the page (or of the open tooltip) in page coordinates, and the report canvas."""
    host = frame.locator(".visualContainerHost").first.bounding_box()
    transform = frame_transform(frame, frame.evaluate("() => innerWidth"))
    canvas = [host["x"], host["y"], host["width"], host["height"]]
    visuals = frame.evaluate(VISUALS_JS, scope)
    for visual in visuals:
        visual["rect"] = to_page(visual["rect"], transform)
        if visual["hit"]:
            visual["hit"] = to_page(visual["hit"] + [0, 0], transform)[:2]
        visual["shown"] = (visual["shown"] and not visual["covered"]
                           and (scope is not None or inside(visual["rect"], canvas)))
        kind = visual["type"].lower()
        visual["decorative"] = visual["tab"] == "-1" and kind in DECORATIVE_TYPES
        visual["data"] = kind not in DECORATIVE_TYPES
        visual["key"] = visual_key(visual)
    return visuals, canvas


def paint(page, frame, shown, scope=None):
    """Text items and chart marks of the shown visuals, from the report frame and custom-visual sandboxes."""
    texts, marks = [], []
    frames = [frame] + [f for f in page.frames if f.parent_frame == frame]
    for current in frames:
        try:
            found = current.evaluate(PAINT_JS, {"maxText": 800, "maxMarks": 4000,
                                                "scope": scope if current is frame else None})
            transform = frame_transform(current, found["width"])
        except Exception:
            continue
        if transform is None:
            continue
        owner = None
        if current is not frame:
            box = current.frame_element().bounding_box()
            hits = [v for v in shown if box and inside([box["x"], box["y"], box["width"], box["height"]], v["rect"])]
            if not hits:
                continue
            owner = min(hits, key=lambda v: v["rect"][2] * v["rect"][3])["i"]
        for item in found["texts"] + found["marks"]:
            item["rect"] = to_page(item["rect"], transform)
            if owner is not None:
                item["visual"] = owner
        texts += found["texts"]
        marks += found["marks"]
    return texts, marks, frames


def contrast_checks(page, frames, visuals, fresh, texts, marks, prefix, record, checks, notes):
    """text_contrast and visual_contrast for the fresh visuals; returns the plain screenshot."""
    def screenshot_with(css):
        for current in frames:
            try:
                current.evaluate(STYLE_JS, css)
            except Exception:
                pass
        page.wait_for_timeout(400)
        return screenshot(page)

    normal = screenshot(page)
    behind_text = screenshot_with(HIDE_TEXT_CSS)  # bars stay: a label inside a bar is read against the bar
    behind_marks = screenshot_with(HIDE_MARKS_CSS)
    screenshot_with(None)
    viewport = [0, 0, page.viewport_size["width"], page.viewport_size["height"]]
    by_index = {v["i"]: v for v in visuals}
    fresh_ids = {v["i"] for v in fresh}

    # text_contrast
    outside = 0
    failures = []
    for t in texts:
        if t["visual"] not in fresh_ids:
            continue
        if not inside(t["rect"], viewport):
            outside += 1
            continue
        bg = background(behind_text, t["rect"])
        if bg is None or not painted(normal, behind_text, t["rect"]):
            continue
        fg = blend(t["colour"], min(t["alpha"], 1), bg)
        ratio = contrast(fg, bg)
        large = t["size"] >= 24 or (t["size"] >= 18.66 and t["weight"] >= 700)
        need = 3 if large else 4.5
        checks["text_contrast"][1] += 1
        if ratio >= need:
            checks["text_contrast"][0] += 1
        else:
            failures.append((ratio, f'{prefix} "{t["text"][:25]}" {ratio:.2f}:1 ({hexcolour(fg)} on {hexcolour(bg)}, needs {need:g}:1)'))
    checks["text_contrast"][2] += [issue for _, issue in sorted(failures)]
    if outside:
        notes.append(f"{prefix}: {outside} text items outside the screen, not checked")

    # visual_contrast
    for v in fresh:
        if not v["data"]:
            continue
        own = [m for m in marks if m["visual"] == v["i"] and inside(m["rect"], v["rect"])]
        if not own:
            if v["canvas"]:
                notes.append(f"{prefix}: {name_of(v)} is a canvas or custom visual without readable colours, not checked")
            continue
        bg = background(behind_marks, v["rect"])
        if bg is None:
            continue
        colours = {}
        for m in own:
            colour = blend(m["colour"], min(m["alpha"], 1), bg)
            entry = colours.setdefault(colour, {"area": 0, "kind": m["kind"]})
            entry["area"] += m["rect"][2] * m["rect"][3]
            if m["kind"] == "fill":
                entry["kind"] = "fill"
        # Marks that look like the background are plot areas or hit targets, not data.
        colours = {c: e for c, e in colours.items() if contrast(c, bg) >= 1.25 and (e["kind"] == "line" or e["area"] >= 40)}
        ranked = sorted(colours, key=lambda c: -colours[c]["area"])
        if len(ranked) > MAX_COLOURS:
            notes.append(f"{prefix}: {name_of(v)} uses {len(ranked)} colours, checked the {MAX_COLOURS} largest")
            ranked = ranked[:MAX_COLOURS]
        for c in ranked:
            ratio = contrast(c, bg)
            record("visual_contrast", ratio >= 3, f"{name_of(v)} {hexcolour(c)} on background {hexcolour(bg)} {ratio:.1f}:1")
        area = pixels(normal, v["rect"]).astype(int)
        masks = {}
        for c in ranked:
            if colours[c]["kind"] == "fill":
                masks[c] = (np.abs(area - np.array(c)).max(axis=2) <= 12)
        fills = list(masks)
        for a_i, a in enumerate(fills):
            grown = masks[a].copy()
            for dy in range(-2, 3):
                for dx in range(-2, 3):
                    grown |= np.roll(np.roll(masks[a], dy, axis=0), dx, axis=1)
            for b in fills[a_i + 1:]:
                if (grown & masks[b]).sum() >= 12:
                    ratio = contrast(a, b)
                    record("visual_contrast", ratio >= 3, f"{name_of(v)} touching {hexcolour(a)}/{hexcolour(b)} {ratio:.1f}:1")
    return normal


def new_checks():
    checks = {test: [0, 0, []] for test in TESTS}  # passed, checked, issues
    checks["tab_order"].append(0)  # scored over pairs of stops, reported as the number of stops
    return checks


def recorder(checks, prefix):
    def record(test, ok, issue):
        checks[test][1] += 1
        if ok:
            checks[test][0] += 1
        else:
            checks[test][2].append(f"{prefix} {issue}")
    return record


def audit_page(page, frame, prefix, seen=None, tips=None):
    """Checks the page as it is on screen. seen: keys of visuals already checked in an earlier bookmark
    state of this page (only new visuals are scored again; the tab order is scored for every state).
    tips: signatures of tooltips already checked; None skips the tooltip check."""
    checks, notes = new_checks(), []
    record = recorder(checks, prefix)
    seen = set() if seen is None else seen
    visuals, canvas = collect(page, frame)
    shown = [v for v in visuals if v["shown"]]
    fresh = [v for v in shown if v["key"] not in seen]
    texts, marks, frames = paint(page, frame, shown)
    normal = contrast_checks(page, frames, visuals, fresh, texts, marks, prefix, record, checks, notes)

    # alt_text
    for v in fresh:
        if v["decorative"] or v["type"].lower() in TEXT_TYPES:
            continue
        has_alt = v["label"].lower() not in DEFAULT_LABELS and v["label"].lower() != v["type"].lower()
        record("alt_text", has_alt, f'{v["type"] or "visual"} {name_of(v)} has no alt text')

    # keyboard_access and tab_order
    by_index = {v["i"]: v for v in visuals}
    reachable = [v for v in visuals if v["tab"] == "0" and v["shown"]]
    sequence = [i for i in tab_sequence(page, frame, len(visuals) + 10) if by_index.get(i, {}).get("shown")]
    if reachable and not sequence:
        notes.append(f"{prefix}: the tab sequence could not be recorded")
    else:
        for v in fresh:
            if v["data"]:
                record("keyboard_access", v["i"] in sequence, f"{name_of(v)} cannot be reached with Tab")
    backwards = set()
    if len(sequence) >= 2:
        rank = reading_order({i: by_index[i]["rect"] for i in sequence})
        pairs = [(a, b) for n, a in enumerate(sequence) for b in sequence[n + 1:]]
        good = sum(rank[a] < rank[b] for a, b in pairs)
        checks["tab_order"][0] += good
        checks["tab_order"][1] += len(pairs)
        checks["tab_order"][3] += len(sequence)
        for step, (a, b) in enumerate(zip(sequence, sequence[1:]), 1):
            if rank[b] < rank[a]:
                backwards.add(step)
                checks["tab_order"][2].append(f"{prefix} step {step}->{step + 1} goes back: {name_of(by_index[a])} -> {name_of(by_index[b])}")

    image = draw_tab_order(normal, canvas, shown, sequence, by_index, backwards)
    for v in fresh:  # the number drawn on this page's (or view's) tab-order screenshot
        v["stop"] = sequence.index(v["i"]) + 1 if v["i"] in sequence else None
    if tips is not None:
        check_tooltips(page, frame, prefix, fresh, texts, marks, tips, checks, notes)
    seen.update(v["key"] for v in shown)
    return checks, notes, image, inventory(fresh), list(zip(fresh, visual_details(frame, fresh)))


# ---------- tooltips ----------

def hover_points(v, texts, marks):
    """Up to two places on a data visual where a data point sits: its largest mark and a middle text item."""
    own = sorted((m for m in marks if m["visual"] == v["i"] and inside(m["rect"], v["rect"])),
                 key=lambda m: -m["rect"][2] * m["rect"][3])
    words = [t for t in texts if t["visual"] == v["i"] and inside(t["rect"], v["rect"])]
    points = [m["rect"] for m in own[:1]] + ([words[len(words) // 2]["rect"]] if len(words) > 2 else [])
    return [(r[0] + r[2] / 2, r[1] + r[3] / 2) for r in points]


def check_tooltips(page, frame, prefix, fresh, texts, marks, tips, checks, notes):
    """Hovers data points of the fresh data visuals. A report-page tooltip (made of visuals) gets the text
    and chart colour contrast checks once per tooltip design; a default tooltip gets a text contrast check
    once per colour pair."""
    targets = [v for v in fresh if v["data"] and re.sub(r"\s+", "", v["type"].lower()) not in LAYOUT_TYPES]
    hovers = 0
    for v in targets:
        for x, y in hover_points(v, texts, marks):
            if hovers >= MAX_HOVERS:
                notes.append(f"{prefix}: tooltips checked on the first {MAX_HOVERS} data points only")
                park(page)
                return
            hovers += 1
            page.mouse.move(x, y)
            page.wait_for_timeout(900)
            try:
                tip = frame.evaluate(TIP_JS)
            except Exception:
                tip = None
            if not tip:
                continue
            if tip["page"]:
                if tip["sig"] in tips:
                    continue
                tips.add(tip["sig"])
                tip_prefix = f'{prefix} [tooltip: {tip["title"].replace("]", ")") or name_of(v)}]'
                record = recorder(checks, tip_prefix)
                visuals, _ = collect(page, frame, TIP_SCOPE)
                shown = [t for t in visuals if t["shown"]]
                tip_texts, tip_marks, frames = paint(page, frame, shown, TIP_SCOPE)
                contrast_checks(page, frames, visuals, shown, tip_texts, tip_marks, tip_prefix, record, checks, notes)
                continue
            for row in tip["rows"]:
                if not row["bg"]:
                    continue
                key = "default|" + hexcolour(row["fg"]) + "|" + hexcolour(row["bg"])
                if key in tips:
                    continue
                tips.add(key)
                ratio = contrast(row["fg"], row["bg"])
                large = row["size"] >= 24 or (row["size"] >= 18.66 and row["weight"] >= 700)
                need = 3 if large else 4.5
                recorder(checks, f"{prefix} [tooltip]")("text_contrast", ratio >= need,
                    f'"{row["text"]}" {ratio:.2f}:1 ({hexcolour(row["fg"])} on {hexcolour(row["bg"])}, needs {need:g}:1)')
    park(page)


# ---------- bookmark states ----------

def where(frame):
    """The open page: its page tab or pager number, or "hidden" on a hidden page."""
    if frame.locator(PAGE_TABS).count():
        names = frame.evaluate("""sel => Array.from(document.querySelectorAll(sel))
            .filter(e => e.classList.contains('selected') && e.getBoundingClientRect().width > 0).map(e => e.innerText.trim())""", PAGE_TABS)
        return "tab:" + names[0] if names else "hidden"
    position = pager_position(frame)
    return f"pager:{position[0]}" if position else "hidden"


def settle(page, frame):
    """Waits after a click until the number of visuals stops changing."""
    park(page)
    page.wait_for_timeout(1200)
    previous, stable = -1, 0
    for _ in range(20):
        count = frame.locator(".visualContainer").count()
        stable = stable + 1 if count == previous else 0
        previous = count
        if stable >= 2:
            break
        page.wait_for_timeout(300)
    page.wait_for_timeout(800)


def snapshot(page, frame):
    """(state, buttons): the state is the open page plus the set of visuals on screen; buttons are
    (key, label, click point) of the bookmark and page-navigation buttons a click can reach."""
    visuals, _ = collect(page, frame)
    shown = [v for v in visuals if v["shown"]]
    state = (where(frame), frozenset(v["key"] for v in shown))
    buttons = []
    for v in shown:
        if v["hit"] and ACTION.match(v["label"]):
            label = ACTION.sub("", v["label"]).strip() or v["text"] or v["label"].strip()
            buttons.append((v["key"], short_label(label), v["hit"]))
    return state, buttons


def explore(page, frame, open_fresh, prefix, seen, tips, add, max_states, deadline, known, on_state=None):
    """Clicks every bookmark and page-navigation button the page offers, in every state reached, and checks
    each new state (page plus visuals on screen). A button is clicked once: a bookmark applies the same
    saved view wherever it is clicked. open_fresh(path) reloads the report on this page and replays the
    clicks in path; it returns the new frame. known: visual sets of the pages checked so far, so a button
    that opens one of them (a logo going to the cover page) adds nothing. Returns (frame, clicked, notes)."""
    notes = []
    state, buttons = snapshot(page, frame)
    _, canvas = collect(page, frame)
    base = layout(state, canvas)
    states = {state: {"path": [], "buttons": buttons}}
    known.add(state[1])
    home = state[0]
    tried = set()
    current, clicks, extra = state, 0, 0
    while clicks < MAX_CLICKS:
        if deadline and time.time() > deadline:
            notes.append(f"{prefix}: time limit reached, some buttons were not tried")
            break
        todo = [b for b in states[current]["buttons"] if b[0] not in tried] if current else []
        if not todo:
            target = next((s for s, info in states.items() if any(b[0] not in tried for b in info["buttons"])), None)
            if target is None:
                break
            frame = open_fresh(states[target]["path"])
            state, _ = snapshot(page, frame)
            if state != target:  # the replay ended somewhere else: give up on that state's buttons
                tried.update(b[0] for b in states[target]["buttons"])
                current = state if state in states else None
                continue
            current = target
            continue
        key, label, point = todo[0]
        tried.add(key)
        clicks += 1
        page.mouse.click(*point)
        settle(page, frame)
        state, buttons = snapshot(page, frame)
        if state == current:
            continue
        if (state[0] != home and state[0] != "hidden") or (state not in states and state[1] in known):
            current = None  # went to another page, which is checked on its own
            continue
        if state in states:
            current = state
            continue
        if extra >= max_states:
            notes.append(f"{prefix}: more than {max_states} button states, checked the first {max_states}")
            break
        extra += 1
        origin = layout(current, canvas)  # where the clicked button is
        states[state] = {"path": states[current]["path"] + [key], "buttons": buttons}
        current = state
        if on_state:
            on_state(extra)
        add(audit_page(page, frame, f"{prefix} [view {extra}: {label}]", seen, tips), label, extra,
            {"hidden": state[0] == "hidden", "rects": layout(state, canvas), "base": base, "origin": origin})
        park(page)
    return frame, clicks > 0, notes


# ---------- report area: hidden pages and bookmark overlays ----------

# Page view setting in Power BI (View > Page view): numbers in the older format, names in PBIR.
FIT_MODES = {"0": "page", "fittopage": "page", "1": "width", "fittowidth": "width", "2": "actual", "actualsize": "actual"}


def report_definition(data):
    """Pages of the report as Power BI sends them to the viewer (also hidden ones): name, kind (visible,
    hidden, tooltip, drillthrough), size, page view (fit: page, width or actual) and visual rectangles. [] if the data has no report definition."""
    exploration = (data or {}).get("exploration") or {}
    sections = exploration.get("sections") or []
    flags = {}
    content = exploration.get("explorationContent") or {}
    try:  # report definition in the page-by-page (PBIR) format
        document = json.loads(content.get("explorationDocument") or "{}")
        for entry in (document.get("pages") or {}).get("pages", []):
            page = entry.get("content") or {}
            binding = ((page.get("pageBinding") or {}).get("type") or page.get("type") or "").lower()
            kind = ("tooltip" if binding == "tooltip" else "drillthrough" if binding == "drillthrough"
                    else "hidden" if page.get("visibility") == "HiddenInViewMode" else "visible")
            positions = [(v.get("content") or {}).get("position") or {} for v in entry.get("visualContainers") or []]
            rects = [[q.get("x", 0), q.get("y", 0), q.get("width", 0), q.get("height", 0)] for q in positions if q]
            flags[page.get("name")] = (kind, page.get("width"), page.get("height"), rects,
                                       FIT_MODES.get(str(page.get("displayOption", "")).lower(), "page"))
    except (ValueError, AttributeError):
        pass
    pages = []
    for section in sections:
        kind, width, height, pbir_rects, fit = flags.get(section.get("objectName") or section.get("name"),
                                                         (None, None, None, [], None))
        if kind is None:  # older format: flags in the section's config
            config = section.get("config") or "{}"
            try:
                parsed = json.loads(config)
            except ValueError:
                parsed = {}
            kind = ("tooltip" if '"Tooltip"' in config or parsed.get("type") == 2
                    else "drillthrough" if "Drillthrough" in config
                    else "hidden" if parsed.get("visibility") == 1 else "visible")
            width, height = section.get("width"), section.get("height")
            fit = FIT_MODES.get(str(section.get("displayOption", "")).lower(), "page")
        rects = [[v.get("x", 0), v.get("y", 0), v.get("width", 0), v.get("height", 0)]
                 for v in section.get("visualContainers") or []]
        if pbir_rects or not any(r[2] for r in rects):  # the PBIR format keeps positions in the page files
            rects = pbir_rects
        pages.append({"name": section.get("displayName") or "", "kind": kind, "size": [width or 1280, height or 720],
                      "fit": fit or "page", "rects": rects})
    return pages


def key_rect(key):
    """The page rectangle stored in a visual key (see visual_key)."""
    try:
        return [8 * int(n) for n in key.rsplit("|", 1)[1].split(",")]
    except (ValueError, IndexError):
        return None


def layout(state, canvas):
    """Rectangles of the visuals of a snapshot state, as shares of the report canvas (0-1)."""
    x0, y0, width, height = canvas
    rects = []
    for key in state[1]:
        r = key_rect(key)
        if r and width and height:
            rects.append(((r[0] - x0) / width, (r[1] - y0) / height, r[2] / width, r[3] / height))
    return rects


def same_rect(a, b, tolerance=0.025):
    return all(abs(p - q) <= tolerance for p, q in zip(a, b))


def covered(rects, cells=120):
    """Share of the canvas (0-1) the union of these rectangles covers."""
    grid = np.zeros((cells, cells), dtype=bool)
    for x, y, w, h in rects:
        left, top = max(0, int(x * cells)), max(0, int(y * cells))
        right, bottom = min(cells, int(np.ceil((x + w) * cells))), min(cells, int(np.ceil((y + h) * cells)))
        if right > left and bottom > top:
            grid[top:bottom, left:right] = True
    return float(grid.mean())


def match_page(definition, rects):
    """The report page whose visual positions fit what is on screen best, or None."""
    best, score = None, 0.0
    for page in definition:
        width, height = page["size"]
        expected = [(x / width, y / height, w / width, h / height) for x, y, w, h in page["rects"]]
        if not rects or not expected:
            continue
        hits = sum(1 for r in rects if any(same_rect(r, e) for e in expected))
        fit = hits / len(rects)
        if fit > score:
            best, score = page, fit
    return best if score >= 0.5 else None


def screen_state(page, frame):
    """(state, canvas): the snapshot state and the report canvas in page coordinates."""
    visuals, canvas = collect(page, frame)
    shown = [v for v in visuals if v["shown"]]
    return (where(frame), frozenset(v["key"] for v in shown)), canvas


# ---------- screenshot overlay ----------

def font(size):
    for name in ("arialbd.ttf", "DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            pass
    return ImageFont.load_default(size)


def dashed_rect(draw, box, colour, width, dash=8):
    x0, y0, x1, y1 = box
    for start, end, fixed, horizontal in ((x0, x1, y0, True), (x0, x1, y1, True), (y0, y1, x0, False), (y0, y1, x1, False)):
        for p in range(int(start), int(end), dash * 2):
            q = min(p + dash, end)
            draw.line([(p, fixed), (q, fixed)] if horizontal else [(fixed, p), (fixed, q)], fill=colour, width=width)


def arrow(draw, a, b, colour, width=4):
    draw.line([a, b], fill=colour, width=width)
    v = np.subtract(b, a)
    length = float(np.hypot(*v))
    if length < 1:
        return
    u = v / length
    tip = np.subtract(b, u * 16)  # stop before the badge
    left = tip - u * 12 + np.array([-u[1], u[0]]) * 7
    right = tip - u * 12 - np.array([-u[1], u[0]]) * 7
    draw.polygon([tuple(tip), tuple(left), tuple(right)], fill=colour)


def draw_tab_order(img, canvas, shown, sequence, by_index, backwards):
    x0, y0 = max(int(canvas[0]), 0), max(int(canvas[1]), 0)
    x1, y1 = min(int(canvas[0] + canvas[2]), img.shape[1]), min(int(canvas[1] + canvas[3]), img.shape[0])
    base = Image.fromarray(img[y0:y1, x0:x1]).convert("RGBA")
    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    local = lambda r: (r[0] - x0, r[1] - y0, r[0] + r[2] - x0, r[1] + r[3] - y0)
    centre = lambda r: (r[0] + r[2] / 2 - x0, r[1] + r[3] / 2 - y0)
    for v in shown:
        if v["i"] in sequence:
            continue
        if v["decorative"]:
            dashed_rect(draw, local(v["rect"]), (150, 150, 150, 220), 2, dash=4)
        elif v["data"]:
            dashed_rect(draw, local(v["rect"]), (220, 38, 38, 255), 4)
    points = [centre(by_index[i]["rect"]) for i in sequence]
    for step, (a, b) in enumerate(zip(points, points[1:]), 1):
        arrow(draw, a, b, (220, 38, 38, 230) if step in backwards else (37, 99, 235, 200))
    label_font = font(15)
    for number, (cx, cy) in enumerate(points, 1):
        draw.ellipse([cx - 14, cy - 14, cx + 14, cy + 14], fill=(17, 24, 39, 235), outline=(255, 255, 255, 255), width=2)
        draw.text((cx, cy), str(number), fill="white", font=label_font, anchor="mm")
    result = Image.alpha_composite(base, layer).convert("RGB")
    # The legend wraps so it stays readable on narrow (portrait) reports.
    legend_font = font(13)
    parts = ["1,2,3 = tab stops", "blue arrow = next stop", "red arrow = against reading order",
             "red dashes = not reachable", "grey dots = hidden (decorative)"]
    measure = ImageDraw.Draw(result)
    lines, line = [], ""
    for part in parts:
        candidate = f"{line}    {part}" if line else part
        if line and measure.textlength(candidate, font=legend_font) > result.width - 16:
            lines.append(line)
            candidate = part
        line = candidate
    lines.append(line)
    final = Image.new("RGB", (result.width, result.height + 12 + 18 * len(lines)), "white")
    final.paste(result, (0, 0))
    legend = ImageDraw.Draw(final)
    for number, text in enumerate(lines):
        legend.text((8, result.height + 7 + 18 * number), text, fill=(17, 24, 39), font=legend_font)
    return final


# ---------- one report ----------

def audit_report(browser, row, tab_dir, max_pages=None, on_page=None, max_states=MAX_STATES,
                 explore_seconds=None, tooltips=True):
    """Audits one report. Every page is checked as it opens, then in each state its bookmark buttons
    lead to (max_states per page, 0 = off), with tooltips hovered in every state.
    max_pages caps the pages audited and explore_seconds the time spent on button states (public tool);
    on_page(number, total, state) is called before each page and each state, for progress display."""
    result = {key: "" for key in FIELDS}
    result.update(report_id=row["report_id"],
                  audited_at=datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%MZ"))
    url = row.get("url")
    if not url:
        result["audit_status"] = "not audited: no report URL"
        return result
    totals = {test: [0, 0, []] for test in TESTS}
    notes, images, failed = [], [], []
    tab_stops = 0
    report_counts = collections.defaultdict(collections.Counter)
    by_page, items = [], []
    definition, reached, overlays = [], {}, []  # report pages; hidden pages reached; bookmark overlays
    deadline = time.time() + explore_seconds if explore_seconds else None
    known = set()  # visual sets of the pages checked so far
    context = browser.new_context(viewport=VIEWPORT, locale="en-US")
    try:
        page = context.new_page()
        page.set_default_timeout(30000)

        def on_response(response):
            if definition or "modelsandexploration" not in response.url.lower():
                return
            try:
                definition.extend(report_definition(response.json()))
            except Exception:
                pass

        page.on("response", on_response)

        def load():
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            loaded = report_frame(page)
            wait_rendered(page, loaded, settle_ms=6000)
            park(page)
            return loaded

        frame = load()
        pages = list_pages(frame)
        if max_pages and len(pages) > max_pages:
            notes.append(f"report has {len(pages)} pages, audited the first {max_pages}")
            pages = pages[:max_pages]
        changed = False  # a button was clicked: other pages may show a bookmark's state, so reload first
        for number, (kind, index, name) in enumerate(pages, 1):
            prefix = f"p{number}"
            if on_page:
                on_page(number, len(pages), 0)
            page_counts = {"data": collections.Counter(), "layout": collections.Counter(), "custom": collections.Counter()}
            seen, tips = set(), (set() if tooltips else None)
            page_slug = slugify(name, 40)

            def add(audited, label=None, state=0, area=None):
                nonlocal tab_stops
                checks, state_notes, image, counts, described = audited
                filename = f'{row["report_id"]}_p{number:02d}' + (f"s{state:02d}" if state else "") + ("_" + page_slug if page_slug else "") + ".png"
                title = (name or f"Page {number}") + (f" · after {label}" if label else "")
                if area:
                    found = match_page(definition, area["rects"]) if area["hidden"] else None
                    page_name = found["name"] if found else ("hidden page" if area["hidden"] else name)
                    first = reached.get(page_name) if area["hidden"] else None
                    source = match_page(definition, area.get("origin") or []) if area.get("origin") else None
                    origin_name = source["name"] if source else (name or f"Page {number}")
                    if area["hidden"] and first is None:
                        reached[page_name] = {"rects": area["rects"], "via": label or "", "from": origin_name,
                                              "image": filename, "matched": bool(found)}
                        title = f"{page_name} (hidden page, via {label})"
                    else:
                        before = first["rects"] if first else area["base"]
                        new = [r for r in area["rects"] if not any(same_rect(r, b, 0.01) for b in before)]
                        share = covered(new)
                        if share >= 0.02:
                            overlays.append({"page": page_name or f"Page {number}", "label": label or "",
                                             "area": round(share, 2), "image": filename})
                        if first:
                            title = f"{page_name} (hidden page) · after {label}"
                for v, d in described:
                    item = describe(v, d, number)
                    if state:  # first seen in a button view: its tab stop is on that view's screenshot
                        item["view"] = state
                    items.append(item)
                for group, counter in counts.items():
                    page_counts[group].update(counter)
                for test in TESTS:
                    for part in range(2):
                        totals[test][part] += checks[test][part]
                    totals[test][2] += checks[test][2]
                tab_stops += checks["tab_order"][3]
                notes.extend(state_notes)
                images.append((filename, image, title.replace(";", ","), state))

            fit_note = [""]

            def open_fresh(path):
                fresh = load()
                open_page(page, fresh, kind, index)
                fit_note[0] = fit_window(page, fresh)  # before any click: button positions depend on the size
                for key in path:
                    _, buttons = snapshot(page, fresh)
                    button = next((b for b in buttons if b[0] == key), None)
                    if button is None:
                        break
                    page.mouse.click(*button[2])
                    settle(page, fresh)
                return fresh

            try:
                if changed:
                    frame = open_fresh([])
                    changed = False
                else:
                    open_page(page, frame, kind, index)
                    fit_note[0] = fit_window(page, frame)
                if fit_note[0]:
                    notes.append(f"{prefix}: {fit_note[0]}")
                park(page)
                start_area = None
                if kind == "start":
                    start, start_canvas = screen_state(page, frame)
                    start_area = {"hidden": True, "rects": layout(start, start_canvas), "base": []}
                add(audit_page(page, frame, prefix, seen, tips), area=start_area)
                if max_states and deadline and time.time() > deadline:
                    notes.append(f"{prefix}: time limit reached, buttons not tried")
                elif max_states:
                    progress = (lambda state: on_page(number, len(pages), state)) if on_page else None
                    frame, changed, explore_notes = explore(page, frame, open_fresh, prefix, seen, tips, add,
                                                            max_states, deadline, known, progress)
                    notes.extend(explore_notes)
            except Exception as exc:
                failed.append(f"{prefix}: {str(exc).splitlines()[0][:120]}")
                changed = True
            for group, counter in page_counts.items():
                report_counts[group].update(counter)
            custom = sum(page_counts["custom"].values())
            if any(page_counts.values()):
                by_page.append(f'{prefix}: core {sum(page_counts["data"].values())} data + '
                               f'{sum(page_counts["layout"].values())} layout, custom {custom}'
                               + (f' ({listing(page_counts["custom"])})' if custom else ""))
        result["pages_audited"] = str(sum(1 for image in images if not image[3]))
        result["states_audited"] = str(sum(1 for image in images if image[3]))
        result["audit_status"] = "audited" if not failed else ("partial: " if images else "error: ") + "; ".join(failed)
    except Exception as exc:
        result["audit_status"] = "error: " + str(exc).splitlines()[0][:200]
        return result
    finally:
        context.close()
    for old in tab_dir.glob(f'{row["report_id"]}_p*.png'):
        old.unlink()
    for filename, image, _, _ in images:
        image.save(tab_dir / filename, optimize=True)
    result["tab_order_images"] = "; ".join(image[0] for image in images)
    result["tab_order_titles"] = "; ".join(image[2] for image in images)
    data, layout_count, custom = (sum(report_counts[g].values()) for g in ("data", "layout", "custom"))
    result.update(visuals_core_data=str(data), visuals_core_layout=str(layout_count), visuals_custom=str(custom),
                  visuals_core_share=str(round(100 * data / (data + custom))) if data + custom else "",
                  visuals_core_data_types=listing(report_counts["data"]),
                  visuals_custom_types=listing(report_counts["custom"]), visuals_by_page="; ".join(by_page),
                  visuals_items=json.dumps(items, ensure_ascii=False, separators=(",", ":")))
    visible = sum(1 for kind, _, _ in pages if kind != "start") if "pages" in locals() else 0
    area_pages = []
    for page_def in definition:
        hit = reached.get(page_def["name"])
        area_pages.append({"name": page_def["name"], "kind": page_def["kind"], "reached": bool(hit),
                           "via": hit["via"] if hit else "", "from": hit["from"] if hit else None,
                           "image": hit["image"] if hit else "", "size": page_def["size"], "fit": page_def["fit"]})
    for page_name, hit in reached.items():  # hidden pages reached but not matched to the definition
        if not any(p["name"] == page_name for p in area_pages):
            area_pages.append({"name": page_name, "kind": "hidden", "reached": True, "via": hit["via"],
                               "from": hit["from"], "image": hit["image"]})
    hidden_reached = sum(1 for p in area_pages if p["kind"] in ("hidden", "drillthrough") and p["reached"])
    overlay_total = round(sum(o["area"] for o in overlays), 1)
    total = round(visible + hidden_reached + overlay_total, 1)
    if definition or reached or overlays:
        hidden_all = sum(1 for p in area_pages if p["kind"] == "hidden")
        result["report_area_summary"] = (
            f"{visible} visible page{'s' if visible != 1 else ''}"
            + (f" + {hidden_reached} hidden page{'s' if hidden_reached != 1 else ''} reached" if hidden_reached else "")
            + (f" + bookmark overlays {overlay_total}" if overlays else "") + f" = {total} pages"
            + (f" ({hidden_all} hidden in total)" if hidden_all else "")
            + "".join(f"; {count} {label} page{'s' if count != 1 else ''} (not counted)" for label, count in
                      (("tooltip", sum(1 for p in area_pages if p["kind"] == "tooltip")),
                       ("drillthrough", sum(1 for p in area_pages if p["kind"] == "drillthrough"))) if count))
        result["report_area"] = json.dumps({"defined": bool(definition), "visible": visible, "total": total,
                                            "pages": area_pages, "overlays": overlays},
                                           ensure_ascii=False, separators=(",", ":"))
    for test, (passed, checked, issues) in totals.items():
        result[f"{test}_checked"] = str(checked)
        result[f"{test}_score"] = str(round(100 * passed / checked)) if checked else ""
        shown = issues[:MAX_ISSUES]
        if len(issues) > MAX_ISSUES:
            shown.append(f"...and {len(issues) - MAX_ISSUES} more")
        result[f"{test}_issues"] = "; ".join(shown)
    if totals["tab_order"][1]:
        result["tab_order_checked"] = str(tab_stops)
    result["audit_notes"] = "; ".join(notes)
    return result
