"""
WebReconstruct v3 - AI-Optimized Website Extractor

Produces structured, semantic data that AI agents can use to understand,
analyze, and rebuild websites from scratch.

Output:
  site_blueprint.json  - Complete site structure, design system, and content map
  pages/*.json         - Detailed per-page semantic data (sections, forms, links)
  markdown/*.md        - AI-friendly structured markdown with section annotations
  website/             - Static HTML mirror with local assets
"""

import asyncio
from curl_cffi.requests import AsyncSession
import os
import json
import re
import time
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup
from rich.console import Console
from rich.progress import (
    Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn,
)
import argparse
import html2text
from collections import Counter

console = Console()

# ── Section-type inference keywords ────────────────────────────────
SECTION_TYPE_HINTS = {
    "hero":         ["hero", "banner", "jumbotron", "splash", "intro", "landing", "masthead"],
    "features":     ["features", "benefits", "services", "capabilities", "offerings", "why-us", "advantages"],
    "testimonials": ["testimonial", "review", "quote", "feedback", "client-say", "customer"],
    "pricing":      ["pricing", "plans", "packages", "tiers", "subscription"],
    "team":         ["team", "people", "staff", "members", "founders"],
    "contact":      ["contact", "get-in-touch", "reach-us", "enquiry"],
    "cta":          ["cta", "call-to-action", "signup", "subscribe", "newsletter", "join"],
    "gallery":      ["gallery", "portfolio", "showcase", "work", "projects", "case-stud"],
    "faq":          ["faq", "questions", "accordion", "help"],
    "stats":        ["stats", "numbers", "counter", "metrics", "achievements", "figures"],
    "blog":         ["blog", "posts", "articles", "news", "updates", "journal"],
    "partners":     ["partners", "clients", "logos", "trusted", "brands", "sponsors"],
    "video":        ["video", "embed", "media", "watch"],
    "map":          ["map", "location", "directions", "find-us"],
    "about":        ["about", "story", "mission", "vision", "who-we-are"],
}

SOCIAL_PLATFORMS = [
    "facebook.com", "twitter.com", "x.com", "instagram.com", "linkedin.com",
    "youtube.com", "github.com", "tiktok.com", "pinterest.com", "reddit.com",
    "discord.gg", "discord.com", "mastodon", "threads.net",
]


class WebsiteExtractor:
    def __init__(self, base_url, output_dir=None, max_depth=None, concurrency=10):
        self.base_url = base_url.rstrip("/")
        parsed_base = urlparse(base_url)
        self.domain = parsed_base.netloc
        self.output_dir = output_dir or f"{self.domain.replace('.', '_')}_extracted"
        self.max_depth = max_depth
        self.concurrency = concurrency
        self.semaphore = asyncio.Semaphore(concurrency * 2)  # HTTP-level throttle

        # Directory layout
        self.website_dir  = os.path.join(self.output_dir, "website")
        self.pages_dir    = os.path.join(self.output_dir, "pages")
        self.markdown_dir = os.path.join(self.output_dir, "markdown")
        self.assets_dir   = os.path.join(self.website_dir, "assets")
        self.images_dir   = os.path.join(self.assets_dir, "images")
        self.css_dir      = os.path.join(self.assets_dir, "css")
        self.js_dir       = os.path.join(self.assets_dir, "js")

        # Crawl state
        self.visited_urls = {}          # url -> depth
        self.url_to_path  = {}
        self.pages        = []          # rich per-page data
        self.css_sources  = []          # raw CSS text for design-token extraction
        self.google_font_urls = []
        self.asset_manifest = {"images": [], "stylesheets": [], "scripts": []}

        # Extracted once from the first page that has them
        self.site_header     = None
        self.site_footer     = None
        self.site_navigation = []

        self.stats = {
            "total_pages": 0,
            "total_images": 0,
            "total_css": 0,
            "total_js": 0,
            "start_time": time.time(),
        }
        self.headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        }

        self.md_converter = html2text.HTML2Text()
        self.md_converter.ignore_links = False
        self.md_converter.bypass_tables = False
        self.md_converter.body_width = 0

        self._setup_directories()

    # ── Setup ──────────────────────────────────────────────────────

    def _setup_directories(self):
        for d in [
            self.output_dir, self.website_dir, self.pages_dir,
            self.markdown_dir, self.assets_dir, self.images_dir,
            self.css_dir, self.js_dir,
        ]:
            os.makedirs(d, exist_ok=True)

    # ── Image helpers ─────────────────────────────────────────────

    def _resolve_img_src(self, img):
        """Get the real image URL, handling lazy-loading patterns."""
        src = (
            img.get("data-lazy-src")
            or img.get("data-src")
            or img.get("data-original")
            or img.get("src")
        )
        if not src or src.startswith("data:"):
            return ""
        return src

    # ── URL utilities ──────────────────────────────────────────────

    def is_internal(self, url):
        parsed = urlparse(url)
        if parsed.netloc == "" or parsed.netloc == self.domain:
            return True
        if self.domain.startswith("www."):
            return parsed.netloc == self.domain[4:]
        return parsed.netloc == f"www.{self.domain}"

    def clean_filename(self, filename, default="index"):
        if not filename or filename == "/":
            return default
        clean = re.sub(r'[^\w\-_\.]', '_', filename)
        return clean or default

    def get_local_path_for_url(self, url):
        parsed = urlparse(url)
        path = parsed.path
        if not path or path == "/":
            return "index.html"
        if path.endswith("/"):
            path = path[:-1]
        clean_name = self.clean_filename(path.lstrip("/"))
        if not clean_name.endswith(".html"):
            clean_name += ".html"
        return clean_name

    def _slug_for_url(self, url):
        path = urlparse(url).path.strip("/")
        if not path:
            return "index"
        return re.sub(r'[^\w\-]', '_', path)

    # ── HTTP with retry ────────────────────────────────────────────

    async def _fetch(self, client, url, max_retries=3):
        for attempt in range(max_retries):
            try:
                async with self.semaphore:
                    resp = await client.get(
                        url, allow_redirects=True,
                        headers=self.headers, impersonate="chrome",
                    )
                if resp.status_code == 200:
                    return resp
                if resp.status_code in (429, 503) and attempt < max_retries - 1:
                    await asyncio.sleep(2 ** attempt)
                    continue
                return resp  # non-retryable status code
            except Exception:
                if attempt < max_retries - 1:
                    await asyncio.sleep(2 ** attempt)
                    continue
                return None
        return None

    # ── Asset download ─────────────────────────────────────────────

    async def download_asset(self, client, url, target_dir, stat_key=None):
        try:
            parsed = urlparse(url)
            filename = os.path.basename(parsed.path)
            if not filename:
                return None
            filename = self.clean_filename(filename)
            abs_local_path = os.path.join(target_dir, filename)

            if os.path.exists(abs_local_path):
                return filename

            resp = await self._fetch(client, url)
            if resp and resp.status_code == 200:
                with open(abs_local_path, "wb") as f:
                    f.write(resp.content)
                if stat_key:
                    self.stats[stat_key] += 1
                # Collect CSS text for design-token extraction
                if target_dir == self.css_dir:
                    try:
                        self.css_sources.append(
                            resp.content.decode("utf-8", errors="ignore")
                        )
                    except Exception:
                        pass
                return filename
        except Exception:
            pass
        return None

    # ── Metadata extraction ────────────────────────────────────────

    def _extract_meta(self, soup, url):
        meta = {
            "title": "",
            "description": "",
            "keywords": "",
            "canonical": "",
            "language": "",
            "charset": "utf-8",
            "viewport": "",
            "og": {},
            "twitter": {},
            "json_ld": [],
            "favicon": "",
        }

        if soup.title and soup.title.string:
            meta["title"] = soup.title.string.strip()

        for tag in soup.find_all("meta"):
            name = (tag.get("name") or "").lower()
            prop = (tag.get("property") or "").lower()
            content = tag.get("content", "")
            if name == "description":
                meta["description"] = content
            elif name == "keywords":
                meta["keywords"] = content
            elif name == "viewport":
                meta["viewport"] = content
            elif prop.startswith("og:"):
                meta["og"][prop[3:]] = content
            elif name.startswith("twitter:") or prop.startswith("twitter:"):
                key = (name or prop).split(":", 1)[1]
                meta["twitter"][key] = content
            if tag.get("charset"):
                meta["charset"] = tag["charset"]

        canonical = soup.find("link", rel="canonical")
        if canonical:
            meta["canonical"] = canonical.get("href", "")

        html_tag = soup.find("html")
        if html_tag:
            meta["language"] = html_tag.get("lang", "")

        favicon = soup.find(
            "link",
            rel=lambda x: x and any(
                "icon" in v for v in (x if isinstance(x, list) else [x])
            ),
        )
        if favicon:
            meta["favicon"] = urljoin(url, favicon.get("href", ""))

        for script in soup.find_all("script", type="application/ld+json"):
            try:
                if script.string:
                    meta["json_ld"].append(json.loads(script.string))
            except (json.JSONDecodeError, Exception):
                pass

        return meta

    # ── Navigation extraction ──────────────────────────────────────

    def _extract_navigation(self, root):
        """Extract structured nav items from a root element (soup or tag)."""
        navs = []
        for nav in root.find_all("nav"):
            nav_data = {
                "id": nav.get("id", ""),
                "class": " ".join(nav.get("class", [])),
                "aria_label": nav.get("aria-label", ""),
                "items": [],
            }
            top_ul = nav.find("ul")
            if top_ul:
                for li in top_ul.find_all("li", recursive=False):
                    a = li.find("a", href=True)
                    if not a:
                        continue
                    item = {
                        "label": a.get_text(strip=True),
                        "url": a.get("href", ""),
                        "children": [],
                    }
                    sub_ul = li.find("ul")
                    if sub_ul:
                        for sub_li in sub_ul.find_all("li", recursive=False):
                            sub_a = sub_li.find("a", href=True)
                            if sub_a:
                                item["children"].append({
                                    "label": sub_a.get_text(strip=True),
                                    "url": sub_a.get("href", ""),
                                })
                    nav_data["items"].append(item)
            else:
                for a in nav.find_all("a", href=True):
                    nav_data["items"].append({
                        "label": a.get_text(strip=True),
                        "url": a.get("href", ""),
                        "children": [],
                    })
            if nav_data["items"]:
                navs.append(nav_data)
        return navs

    # ── Section extraction ─────────────────────────────────────────

    def _infer_section_type(self, element):
        classes = " ".join(element.get("class", [])).lower()
        el_id  = (element.get("id") or "").lower()
        identifier = f"{classes} {el_id}"

        for stype, keywords in SECTION_TYPE_HINTS.items():
            for kw in keywords:
                if kw in identifier:
                    return stype

        headings = element.find_all(["h1", "h2", "h3"], limit=3)
        heading_text = " ".join(h.get_text(strip=True).lower() for h in headings)
        for stype, keywords in SECTION_TYPE_HINTS.items():
            for kw in keywords:
                if kw in heading_text:
                    return stype

        if element.find("form"):
            return "form"
        if len(element.find_all("img")) > 3:
            return "gallery"
        if element.find("h1") and not element.find_previous_sibling(
            ["section", "div"]
        ):
            return "hero"

        return "content"

    def _extract_repeating_items(self, element):
        """Detect card / grid patterns (repeated child elements)."""
        items = []
        candidates = [element] + element.find_all("div", recursive=False)[:3]
        for container in candidates:
            children = [
                c for c in container.find_all("div", recursive=False)
                if c.get("class")
            ]
            if len(children) < 2:
                continue

            class_counts = Counter()
            for child in children:
                class_key = tuple(sorted(child.get("class", [])))
                class_counts[class_key] += 1

            if not class_counts:
                continue
            most_common_class, count = class_counts.most_common(1)[0]
            if count < 2:
                continue

            for child in children:
                if tuple(sorted(child.get("class", []))) != most_common_class:
                    continue
                item = {"heading": "", "text": "", "image": None, "link": None}
                h = child.find(["h2", "h3", "h4", "h5", "h6"])
                if h:
                    item["heading"] = h.get_text(strip=True)
                paragraphs = child.find_all("p")
                item["text"] = "\n".join(
                    p.get_text(strip=True) for p in paragraphs if p.get_text(strip=True)
                )
                img = child.find("img")
                if img:
                    img_src = self._resolve_img_src(img)
                    if img_src:
                        item["image"] = {
                            "src": img_src,
                            "alt": img.get("alt", ""),
                        }
                a = child.find("a", href=True)
                if a:
                    item["link"] = {
                        "text": a.get_text(strip=True),
                        "url": a["href"],
                    }
                items.append(item)
            if items:
                break
        return items

    def _parse_section_element(self, el):
        section = {
            "tag": el.name,
            "id": el.get("id", ""),
            "class": " ".join(el.get("class", [])),
            "inferred_type": self._infer_section_type(el),
            "heading": "",
            "subheading": "",
            "text_content": "",
            "images": [],
            "links": [],
            "buttons": [],
            "lists": [],
            "repeating_items": [],
        }

        for level in ("h1", "h2", "h3", "h4"):
            h = el.find(level)
            if h:
                section["heading"] = h.get_text(strip=True)
                for sub_level in ("h2", "h3", "h4", "h5"):
                    if sub_level <= level:
                        continue
                    sub = el.find(sub_level)
                    if sub and sub != h:
                        section["subheading"] = sub.get_text(strip=True)
                        break
                break

        texts = []
        for p in el.find_all("p"):
            txt = p.get_text(strip=True)
            if txt:
                texts.append(txt)
        section["text_content"] = "\n\n".join(texts)

        for img in el.find_all("img"):
            src = self._resolve_img_src(img)
            if not src:
                continue
            section["images"].append({
                "src": src,
                "alt": img.get("alt", ""),
                "width": img.get("width", ""),
                "height": img.get("height", ""),
            })

        for a in el.find_all("a", href=True):
            classes = " ".join(a.get("class", [])).lower()
            section["links"].append({
                "text": a.get_text(strip=True),
                "url": a["href"],
                "is_cta": any(
                    kw in classes for kw in ("btn", "button", "cta", "action")
                ),
            })

        for btn in el.find_all("button"):
            section["buttons"].append({
                "text": btn.get_text(strip=True),
                "type": btn.get("type", ""),
                "class": " ".join(btn.get("class", [])),
            })

        for ul in el.find_all(["ul", "ol"]):
            if ul.find_parent("nav"):
                continue
            items = [
                li.get_text(strip=True)
                for li in ul.find_all("li", recursive=False)
            ]
            if items:
                section["lists"].append({"type": ul.name, "items": items})

        section["repeating_items"] = self._extract_repeating_items(el)
        return section

    def _extract_sections(self, soup):
        sections = []
        section_els = self._find_section_elements(soup)

        for el in section_els:
            section = self._parse_section_element(el)
            if (
                section.get("text_content")
                or section.get("heading")
                or section.get("images")
                or section.get("repeating_items")
            ):
                sections.append(section)
        return sections

    def _find_section_elements(self, soup):
        """Find section boundaries using multiple strategies (page builders, semantic HTML, fallback)."""
        # 1. WPBakery / Visual Composer rows
        vc_rows = soup.find_all("div", class_="vc_row")
        if vc_rows:
            return vc_rows

        # 2. Elementor sections
        el_sections = soup.find_all("section", class_="elementor-section")
        if el_sections:
            return el_sections

        # 3. Divi sections
        divi = soup.find_all("div", class_="et_pb_section")
        if divi:
            return divi

        # 4. Semantic HTML sections inside main/article
        main = soup.find("main") or soup.find("article")
        if main:
            section_tags = main.find_all("section", recursive=False)
            if section_tags:
                return section_tags
            divs = [
                d for d in main.find_all("div", recursive=False)
                if d.get("id") or d.get("class") or d.find(["h1", "h2", "h3"])
            ]
            if divs:
                return divs

        # 5. Generic: top-level divs inside body with meaningful content
        body = soup.find("body")
        if body:
            divs = [
                d for d in body.find_all("div", recursive=False)
                if d.get("id") or d.get("class")
            ]
            if len(divs) >= 2:
                return divs

        # 6. Last resort: treat body as one section
        return [soup.find("body")] if soup.find("body") else []

    # ── Form extraction ────────────────────────────────────────────

    def _extract_forms(self, soup):
        forms = []
        for form in soup.find_all("form"):
            form_data = {
                "action": form.get("action", ""),
                "method": (form.get("method") or "GET").upper(),
                "id": form.get("id", ""),
                "class": " ".join(form.get("class", [])),
                "fields": [],
                "submit_text": "Submit",
            }
            for inp in form.find_all(["input", "textarea", "select"]):
                ftype = (
                    inp.get("type", "text") if inp.name == "input" else inp.name
                )
                if ftype in ("hidden", "submit"):
                    continue
                field = {
                    "tag": inp.name,
                    "type": ftype,
                    "name": inp.get("name", ""),
                    "placeholder": inp.get("placeholder", ""),
                    "required": inp.has_attr("required"),
                    "label": "",
                }
                if inp.get("id"):
                    label = form.find("label", attrs={"for": inp["id"]})
                    if label:
                        field["label"] = label.get_text(strip=True)
                if inp.name == "select":
                    field["options"] = [
                        {
                            "value": opt.get("value", ""),
                            "text": opt.get_text(strip=True),
                        }
                        for opt in inp.find_all("option")
                    ]
                form_data["fields"].append(field)

            submit = (
                form.find("button", type="submit")
                or form.find("input", type="submit")
            )
            if submit:
                form_data["submit_text"] = (
                    submit.get_text(strip=True)
                    or submit.get("value", "Submit")
                )
            if form_data["fields"]:
                forms.append(form_data)
        return forms

    # ── Link classification ────────────────────────────────────────

    def _classify_links(self, soup, page_url):
        internal, external = [], []
        seen = set()
        for a in soup.find_all("a", href=True):
            href = a["href"]
            full_url = urljoin(page_url, href).split("#")[0].rstrip("/")
            if full_url in seen:
                continue
            seen.add(full_url)
            classes = " ".join(a.get("class", [])).lower()
            link = {
                "text": a.get_text(strip=True),
                "url": full_url,
                "is_cta": any(
                    kw in classes for kw in ("btn", "button", "cta", "action")
                ),
            }
            if self.is_internal(full_url):
                internal.append(link)
            else:
                external.append(link)
        return {"internal": internal, "external": external}

    # ── Header structure ───────────────────────────────────────────

    def _extract_header_structure(self, soup, page_url):
        header = soup.find("header")
        if not header:
            return None

        structure = {
            "logo": None,
            "navigation": [],
            "cta_buttons": [],
            "social_links": [],
        }

        # Logo detection
        logo = (
            header.find(
                "img",
                class_=lambda x: x and "logo" in " ".join(x).lower(),
            )
            or header.find(
                "a",
                class_=lambda x: x and "logo" in " ".join(x).lower(),
            )
        )
        if not logo:
            logo = header.find("img")

        if logo:
            if logo.name == "img":
                structure["logo"] = {
                    "type": "image",
                    "src": logo.get("src", ""),
                    "alt": logo.get("alt", ""),
                }
            elif logo.name == "a":
                inner = logo.find("img")
                if inner:
                    structure["logo"] = {
                        "type": "image",
                        "src": inner.get("src", ""),
                        "alt": inner.get("alt", ""),
                        "link": logo.get("href", "/"),
                    }
                else:
                    structure["logo"] = {
                        "type": "text",
                        "text": logo.get_text(strip=True),
                        "link": logo.get("href", "/"),
                    }

        structure["navigation"] = self._extract_navigation(header)

        for a in header.find_all("a", href=True):
            classes = " ".join(a.get("class", [])).lower()
            if any(kw in classes for kw in ("btn", "button", "cta")):
                structure["cta_buttons"].append({
                    "text": a.get_text(strip=True),
                    "url": urljoin(page_url, a["href"]),
                })

        return structure

    # ── Footer structure ───────────────────────────────────────────

    def _extract_footer_structure(self, soup, page_url):
        footer = soup.find("footer")
        if not footer:
            return None

        structure = {
            "columns": [],
            "copyright": "",
            "social_links": [],
            "contact_info": {},
        }

        containers = footer.find_all(["div", "section"], recursive=False)
        if not containers:
            containers = footer.find_all(["div", "section"])

        for col in containers:
            heading = col.find(["h2", "h3", "h4", "h5", "h6"])
            links = col.find_all("a", href=True)
            if heading or len(links) >= 2:
                col_data = {
                    "heading": heading.get_text(strip=True) if heading else "",
                    "links": [
                        {
                            "text": a.get_text(strip=True),
                            "url": urljoin(page_url, a["href"]),
                        }
                        for a in links
                    ],
                }
                if col_data["links"]:
                    structure["columns"].append(col_data)

        copyright_el = footer.find(
            string=re.compile(r'\u00a9|copyright|all rights reserved', re.I)
        )
        if copyright_el:
            parent = getattr(copyright_el, "parent", None)
            structure["copyright"] = (
                parent.get_text(strip=True) if parent else str(copyright_el).strip()
            )

        for a in footer.find_all("a", href=True):
            href = a["href"].lower()
            for platform in SOCIAL_PLATFORMS:
                if platform in href:
                    structure["social_links"].append({
                        "platform": platform.split(".")[0],
                        "url": a["href"],
                    })
                    break

        footer_text = footer.get_text()
        email = re.search(r'[\w.+-]+@[\w-]+\.[\w.]+', footer_text)
        if email:
            structure["contact_info"]["email"] = email.group()
        phone = re.search(r'[\+\(]?[\d\s\-\(\)]{7,15}', footer_text)
        if phone and len(re.sub(r'\D', '', phone.group())) >= 7:
            structure["contact_info"]["phone"] = phone.group().strip()

        return structure

    # ── Design-token extraction (CSS) ──────────────────────────────

    def _extract_design_tokens(self):
        all_css = "\n".join(self.css_sources)
        if not all_css:
            return {}

        tokens = {
            "colors": [],
            "css_variables": {},
            "fonts": {
                "families": [],
                "google_fonts": list(set(self.google_font_urls)),
            },
            "border_radius": [],
            "box_shadows": [],
            "breakpoints": [],
            "transitions": [],
        }

        # Colors by frequency
        hex_colors = re.findall(r'#(?:[0-9a-fA-F]{3,4}){1,2}\b', all_css)
        color_freq = Counter(c.lower() for c in hex_colors)
        tokens["colors"] = [
            {"value": color, "frequency": count}
            for color, count in color_freq.most_common(30)
        ]

        # CSS custom properties (variables)
        for name, value in re.findall(r'(--[\w-]+)\s*:\s*([^;]+)', all_css):
            tokens["css_variables"][name] = value.strip()

        # Font families
        all_families = set()
        for ff in re.findall(r'font-family\s*:\s*([^;{}]+)', all_css):
            for f in ff.split(","):
                clean = f.strip().strip("'\"")
                # Skip CSS keywords, empty, or values that leaked past a rule boundary
                if (
                    not clean
                    or clean.lower() in ("inherit", "initial", "unset", "revert")
                    or len(clean) > 40
                    or "{" in clean or "}" in clean or ":" in clean
                ):
                    continue
                all_families.add(clean)
        tokens["fonts"]["families"] = sorted(all_families)

        # Border radius values
        tokens["border_radius"] = sorted(
            set(r.strip() for r in re.findall(r'border-radius\s*:\s*([^;]+)', all_css))
        )

        # Box shadows
        tokens["box_shadows"] = list(set(
            s.strip()
            for s in re.findall(r'box-shadow\s*:\s*([^;]+)', all_css)
            if s.strip() != "none"
        ))[:10]

        # Media-query breakpoints
        tokens["breakpoints"] = sorted(
            set(int(bp) for bp in re.findall(r'@media[^{]*?(\d+)px', all_css))
        )

        # Transitions
        tokens["transitions"] = list(set(
            t.strip()
            for t in re.findall(r'transition\s*:\s*([^;]+)', all_css)
            if t.strip() != "none"
        ))[:10]

        return tokens

    # ── Main page extraction ───────────────────────────────────────

    async def extract_page(self, client, url, depth):
        if url in self.visited_urls:
            return []

        resp = await self._fetch(client, url)
        if not resp or resp.status_code != 200:
            return []

        try:
            # Sync domain on redirect (first request)
            if url == self.base_url:
                new_domain = urlparse(str(resp.url)).netloc
                if new_domain != self.domain:
                    self.domain = new_domain

            final_url = str(resp.url).rstrip("/")
            if final_url in self.visited_urls and final_url != url:
                return []

            self.visited_urls[url] = depth
            self.visited_urls[final_url] = depth

            local_html_path = self.get_local_path_for_url(final_url)
            self.url_to_path[final_url] = local_html_path

            soup = BeautifulSoup(resp.text, "html.parser")

            # 1. Metadata
            meta = self._extract_meta(soup, final_url)

            # 2. Google Fonts
            for link in soup.find_all("link", href=True):
                href = link["href"]
                if "fonts.googleapis.com" in href or "fonts.gstatic.com" in href:
                    self.google_font_urls.append(href)

            # 3. Inline CSS
            for style in soup.find_all("style"):
                if style.string:
                    self.css_sources.append(style.string)

            # 4. Download assets + rewrite paths (lazy-load aware)
            page_images = []
            for img in soup.find_all("img"):
                src = self._resolve_img_src(img)
                if not src:
                    continue
                abs_src = urljoin(final_url, src)
                filename = await self.download_asset(
                    client, abs_src, self.images_dir, "total_images",
                )
                if filename:
                    rel_path = f"assets/images/{filename}"
                    img["src"] = rel_path
                    # Clear lazy-load attrs so static mirror works
                    for attr in ("data-lazy-src", "data-src", "data-original"):
                        img.attrs.pop(attr, None)
                    page_images.append({
                        "original_url": abs_src,
                        "local_path": rel_path,
                        "alt": img.get("alt", ""),
                    })

            for link_tag in soup.find_all("link", rel="stylesheet"):
                href = link_tag.get("href")
                if not href:
                    continue
                abs_href = urljoin(final_url, href)
                filename = await self.download_asset(
                    client, abs_href, self.css_dir, "total_css",
                )
                if filename:
                    link_tag["href"] = f"assets/css/{filename}"
                    self.asset_manifest["stylesheets"].append({
                        "original_url": abs_href,
                        "local_path": f"assets/css/{filename}",
                    })

            for script in soup.find_all("script", src=True):
                src = script.get("src")
                if not src:
                    continue
                abs_src = urljoin(final_url, src)
                filename = await self.download_asset(
                    client, abs_src, self.js_dir, "total_js",
                )
                if filename:
                    script["src"] = f"assets/js/{filename}"
                    self.asset_manifest["scripts"].append({
                        "original_url": abs_src,
                        "local_path": f"assets/js/{filename}",
                    })

            # 5. Navigation (store from first page that has it)
            nav = self._extract_navigation(soup)
            if nav and not self.site_navigation:
                self.site_navigation = nav

            # 6. Header / Footer (extract once)
            if self.site_header is None:
                self.site_header = self._extract_header_structure(soup, final_url)
            if self.site_footer is None:
                self.site_footer = self._extract_footer_structure(soup, final_url)

            # 7. Semantic sections
            sections = self._extract_sections(soup)

            # 8. Forms
            forms = self._extract_forms(soup)

            # 9. Links
            links = self._classify_links(soup, final_url)

            # 10. Raw markdown fallback
            content_tag = (
                soup.find("main") or soup.find("article") or soup.find("body")
            )
            raw_markdown = (
                self.md_converter.handle(str(content_tag)) if content_tag else ""
            )

            # 11. Store rich page data
            self.pages.append({
                "url": final_url,
                "slug": urlparse(final_url).path or "/",
                "depth": depth,
                "meta": meta,
                "sections": sections,
                "forms": forms,
                "links": links,
                "images": page_images,
                "raw_markdown": raw_markdown,
            })

            # 12. Asset manifest
            self.asset_manifest["images"].extend(page_images)

            # 13. Discover links to crawl
            links_to_crawl = []
            for a in soup.find_all("a", href=True):
                href = a["href"]
                full_url = urljoin(final_url, href).split("#")[0].rstrip("/")
                parsed_href = urlparse(full_url)
                if parsed_href.scheme not in ("http", "https", ""):
                    continue
                if self.is_internal(full_url):
                    if self.max_depth is None or depth < self.max_depth:
                        links_to_crawl.append((full_url, depth + 1))
                    a["href"] = self.get_local_path_for_url(full_url)

            # 14. Save static HTML
            html_out = os.path.join(self.website_dir, local_html_path)
            with open(html_out, "w", encoding="utf-8") as f:
                f.write(soup.prettify())

            self.stats["total_pages"] += 1
            return links_to_crawl

        except Exception as e:
            console.print(f"[red]Error extracting {url}: {e}[/red]")
            return []

    # ── Output generation ──────────────────────────────────────────

    def _build_sitemap(self):
        pages_sorted = sorted(self.pages, key=lambda p: (p["depth"], p["slug"]))
        return [
            {
                "url": p["url"],
                "slug": p["slug"],
                "title": p["meta"]["title"],
                "depth": p["depth"],
                "section_types": [s["inferred_type"] for s in p["sections"]],
                "has_forms": len(p["forms"]) > 0,
            }
            for p in pages_sorted
        ]

    def _generate_structured_markdown(self, page):
        """Produce AI-optimised structured markdown for one page."""
        lines = []
        meta = page["meta"]

        # YAML front-matter
        lines.append("---")
        lines.append(f"url: {page['url']}")
        lines.append(f"slug: {page['slug']}")
        # Escape quotes in title/description for valid YAML
        safe_title = meta["title"].replace('"', '\\"')
        safe_desc  = meta["description"].replace('"', '\\"')
        lines.append(f'title: "{safe_title}"')
        lines.append(f'description: "{safe_desc}"')
        if meta.get("language"):
            lines.append(f"language: {meta['language']}")
        if meta.get("og", {}).get("image"):
            lines.append(f"og_image: {meta['og']['image']}")
        if meta.get("og", {}).get("type"):
            lines.append(f"og_type: {meta['og']['type']}")
        lines.append(f"sections_count: {len(page['sections'])}")
        lines.append(f"has_forms: {'true' if page['forms'] else 'false'}")
        lines.append("---")
        lines.append("")

        lines.append(f"# {meta['title']}")
        if meta["description"]:
            lines.append(f"\n> {meta['description']}")
        lines.append("")

        # ── Sections ──
        for i, section in enumerate(page["sections"]):
            lines.append("---")
            lines.append("")
            label = section["inferred_type"].replace("_", " ").title()
            sid   = section["id"] or f"section-{i}"
            lines.append(f"## [{label} Section]")
            lines.append(
                f'<!-- type: {section["inferred_type"]} '
                f'| id: {sid} '
                f'| class: {section["class"]} -->'
            )
            lines.append("")

            if section["heading"]:
                lines.append(f"### {section['heading']}")
                if section["subheading"]:
                    lines.append(f"#### {section['subheading']}")
                lines.append("")

            if section["text_content"]:
                lines.append(section["text_content"])
                lines.append("")

            # Repeating items (cards / grids)
            if section["repeating_items"]:
                lines.append("**Items:**")
                lines.append("")
                for j, item in enumerate(section["repeating_items"], 1):
                    head = f"**{item['heading']}**" if item["heading"] else f"**Item {j}**"
                    lines.append(f"{j}. {head}")
                    if item["text"]:
                        for tl in item["text"].split("\n"):
                            lines.append(f"   {tl}")
                    if item["image"]:
                        lines.append(
                            f"   ![{item['image']['alt']}]({item['image']['src']})"
                        )
                    if item["link"]:
                        lines.append(
                            f"   [{item['link']['text']}]({item['link']['url']})"
                        )
                    lines.append("")

            for img in section["images"]:
                if img["src"]:
                    lines.append(f"![{img['alt']}]({img['src']})")

            for lst in section["lists"]:
                for item in lst["items"]:
                    prefix = "-" if lst["type"] == "ul" else "1."
                    lines.append(f"{prefix} {item}")
                lines.append("")

            ctas = [l for l in section["links"] if l["is_cta"]]
            for cta in ctas:
                lines.append(f"**CTA: [{cta['text']}]({cta['url']})**")
            for btn in section["buttons"]:
                if btn["text"]:
                    lines.append(f"**Button: {btn['text']}**")
            lines.append("")

        # ── Forms ──
        if page["forms"]:
            lines.append("---")
            lines.append("")
            lines.append("## Forms")
            for form in page["forms"]:
                flabel = form["id"] or "Form"
                target = form["action"] or "self"
                lines.append(f"\n### {flabel} ({form['method']} -> {target})")
                for field in form["fields"]:
                    req   = " *required*" if field["required"] else ""
                    label = (
                        field["label"] or field["name"]
                        or field["placeholder"] or "unnamed"
                    )
                    lines.append(f"- **{label}** ({field['type']}){req}")
                lines.append(f"- Submit: **{form['submit_text']}**")
            lines.append("")

        return "\n".join(lines)

    def _generate_site_blueprint(self):
        """Produce the master site_blueprint.json."""
        homepage = next(
            (p for p in self.pages if p["slug"] in ("/", "")),
            self.pages[0] if self.pages else None,
        )
        site_title = site_desc = site_lang = site_favicon = ""
        if homepage:
            site_title   = homepage["meta"]["title"]
            site_desc    = homepage["meta"]["description"]
            site_lang    = homepage["meta"].get("language") or "en"
            site_favicon = homepage["meta"].get("favicon", "")

        return {
            "$schema": "website_blueprint_v1",
            "extracted_at": datetime.now(timezone.utc).isoformat(),
            "site": {
                "url": self.base_url,
                "domain": self.domain,
                "title": site_title,
                "description": site_desc,
                "language": site_lang,
                "favicon": site_favicon,
            },
            "design_system": self._extract_design_tokens(),
            "layout": {
                "header": self.site_header,
                "footer": self.site_footer,
                "navigation": self.site_navigation,
            },
            "pages": [
                {
                    "url": p["url"],
                    "slug": p["slug"],
                    "title": p["meta"]["title"],
                    "description": p["meta"]["description"],
                    "depth": p["depth"],
                    "section_types": [s["inferred_type"] for s in p["sections"]],
                    "has_forms": len(p["forms"]) > 0,
                    "internal_links_count": len(p["links"]["internal"]),
                    "external_links_count": len(p["links"]["external"]),
                    "images_count": len(p["images"]),
                }
                for p in self.pages
            ],
            "sitemap": self._build_sitemap(),
            "assets": {
                "images": list(
                    {i["local_path"]: i for i in self.asset_manifest["images"]}.values()
                ),
                "stylesheets": list(
                    {s["local_path"]: s for s in self.asset_manifest["stylesheets"]}.values()
                ),
                "scripts": list(
                    {s["local_path"]: s for s in self.asset_manifest["scripts"]}.values()
                ),
                "google_fonts": list(set(self.google_font_urls)),
            },
        }

    def _write_page_files(self):
        for page in self.pages:
            slug = self._slug_for_url(page["url"])

            # Per-page JSON (full detail)
            page_json = {
                "url": page["url"],
                "slug": page["slug"],
                "depth": page["depth"],
                "meta": page["meta"],
                "sections": page["sections"],
                "forms": page["forms"],
                "links": page["links"],
                "images": page["images"],
            }
            with open(
                os.path.join(self.pages_dir, f"{slug}.json"), "w", encoding="utf-8",
            ) as f:
                json.dump(page_json, f, indent=2, ensure_ascii=False)

            # Structured markdown
            md = self._generate_structured_markdown(page)
            with open(
                os.path.join(self.markdown_dir, f"{slug}.md"), "w", encoding="utf-8",
            ) as f:
                f.write(md)

    # ── Run ────────────────────────────────────────────────────────

    async def run(self):
        async with AsyncSession(timeout=30.0) as client:
            queue = [(self.base_url, 0)]
            pending = set()                        # in-flight asyncio tasks
            queued_urls = {self.base_url}           # all URLs ever queued

            with Progress(
                SpinnerColumn(),
                TextColumn("[bold blue]{task.description}"),
                BarColumn(),
                TaskProgressColumn(),
                console=console,
            ) as progress:
                ptask = progress.add_task("Extracting...", total=None)

                while queue or pending:
                    # Fill up to concurrency limit
                    while queue and len(pending) < self.concurrency:
                        url, depth = queue.pop(0)
                        t = asyncio.create_task(
                            self.extract_page(client, url, depth),
                        )
                        pending.add(t)

                    if not pending:
                        break

                    done, pending = await asyncio.wait(
                        pending, return_when=asyncio.FIRST_COMPLETED,
                    )

                    for completed in done:
                        try:
                            new_links = completed.result()
                        except Exception:
                            continue
                        for link, next_depth in (new_links or []):
                            if link not in self.visited_urls and link not in queued_urls:
                                queue.append((link, next_depth))
                                queued_urls.add(link)
                        progress.update(ptask, advance=1)

                    desc = (
                        f"Pages: {self.stats['total_pages']} | "
                        f"Queue: {len(queue)} | Active: {len(pending)}"
                    )
                    total = len(self.visited_urls) + len(queue) + len(pending)
                    progress.update(ptask, description=desc, total=total)

        duration = round(time.time() - self.stats["start_time"], 2)

        # ── Write outputs ──
        blueprint = self._generate_site_blueprint()
        with open(
            os.path.join(self.output_dir, "site_blueprint.json"), "w", encoding="utf-8",
        ) as f:
            json.dump(blueprint, f, indent=2, ensure_ascii=False)

        self._write_page_files()

        summary = {
            "domain": self.domain,
            "duration_seconds": duration,
            "pages_extracted": self.stats["total_pages"],
            "images_downloaded": self.stats["total_images"],
            "css_files": self.stats["total_css"],
            "js_files": self.stats["total_js"],
        }
        with open(
            os.path.join(self.output_dir, "summary.json"), "w", encoding="utf-8",
        ) as f:
            json.dump(summary, f, indent=2)

        console.print(f"\n[bold green]WebReconstruct v3 Complete[/bold green]")
        console.print(f"  Time: [yellow]{duration}s[/yellow]  |  "
                       f"Pages: [cyan]{self.stats['total_pages']}[/cyan]  |  "
                       f"Images: [cyan]{self.stats['total_images']}[/cyan]")
        console.print(f"\n[bold]Output:[/bold]")
        console.print(f"  [cyan]{self.output_dir}/site_blueprint.json[/cyan]  — master blueprint for AI agents")
        console.print(f"  [cyan]{self.output_dir}/pages/*.json[/cyan]          — per-page semantic data")
        console.print(f"  [cyan]{self.output_dir}/markdown/*.md[/cyan]         — structured markdown")
        console.print(f"  [cyan]{self.output_dir}/website/[/cyan]              — static HTML mirror")


# ── CLI ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="WebReconstruct v3 — AI-Optimized Website Extractor",
    )
    parser.add_argument("url", help="The URL to extract")
    parser.add_argument(
        "--depth", type=int, default=None,
        help="Max crawl depth (default: unlimited)",
    )
    parser.add_argument(
        "--concurrency", type=int, default=10,
        help="Max concurrent downloads (default: 10)",
    )
    args = parser.parse_args()

    url = args.url
    if not url.startswith("http"):
        url = "https://" + url

    extractor = WebsiteExtractor(
        url, max_depth=args.depth, concurrency=args.concurrency,
    )
    asyncio.run(extractor.run())
