import asyncio
import httpx
import os
import json
import re
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn
import sys
import shutil

console = Console()

class WebsiteExtractor:
    def __init__(self, base_url, output_dir=None):
        self.base_url = base_url.rstrip("/")
        parsed_base = urlparse(base_url)
        self.domain = parsed_base.netloc
        self.output_dir = output_dir or f"{self.domain.replace('.', '_')}_extracted"
        
        # New structure for the "Website" product
        self.website_dir = os.path.join(self.output_dir, "website")
        self.assets_dir = os.path.join(self.website_dir, "assets")
        self.images_dir = os.path.join(self.assets_dir, "images")
        self.css_dir = os.path.join(self.assets_dir, "css")
        self.js_dir = os.path.join(self.assets_dir, "js")
        
        self.visited_urls = set()
        self.url_to_path = {} # Map URLs to local .html paths
        self.data = []
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        
        self._setup_directories()

    def _setup_directories(self):
        for d in [self.output_dir, self.website_dir, self.assets_dir, self.images_dir, self.css_dir, self.js_dir]:
            if not os.path.exists(d):
                os.makedirs(d)

    def is_internal(self, url):
        parsed = urlparse(url)
        if parsed.netloc == '' or parsed.netloc == self.domain:
            return True
        if self.domain.startswith("www."):
            return parsed.netloc == self.domain[4:]
        if parsed.netloc == f"www.{self.domain}":
            return True
        return False

    def clean_filename(self, filename, default="index"):
        if not filename or filename == "/":
            return default
        clean = re.sub(r'[^\w\-_\.]', '_', filename)
        return clean if clean else default

    async def download_asset(self, client, url, target_dir):
        try:
            parsed = urlparse(url)
            filename = os.path.basename(parsed.path)
            if not filename:
                return None
            
            # Keep original extension
            filename = self.clean_filename(filename)
            abs_local_path = os.path.join(target_dir, filename)
            
            if os.path.exists(abs_local_path):
                return filename

            resp = await client.get(url, follow_redirects=True, headers=self.headers)
            if resp.status_code == 200:
                with open(abs_local_path, "wb") as f:
                    f.write(resp.content)
                return filename
        except Exception:
            pass
        return None

    def get_local_path_for_url(self, url):
        parsed = urlparse(url)
        path = parsed.path
        if not path or path == "/":
            return "index.html"
        
        # Remove trailing slash for naming
        if path.endswith("/"):
            path = path[:-1]
            
        clean_name = self.clean_filename(path.lstrip("/"))
        if not clean_name.endswith(".html"):
            clean_name += ".html"
        return clean_name

    async def extract_page(self, client, url):
        if url in self.visited_urls:
            return []
        
        try:
            response = await client.get(url, follow_redirects=True, headers=self.headers)
            if response.status_code != 200:
                return []

            if url == self.base_url:
                new_domain = urlparse(str(response.url)).netloc
                if new_domain != self.domain:
                    self.domain = new_domain

            final_url = str(response.url).rstrip("/")
            if final_url in self.visited_urls and final_url != url:
                return []
            
            self.visited_urls.add(url)
            self.visited_urls.add(final_url)
            
            local_html_path = self.get_local_path_for_url(final_url)
            self.url_to_path[final_url] = local_html_path

            soup = BeautifulSoup(response.text, 'html.parser')
            
            # --- Metadata ---
            title = soup.title.string.strip() if soup.title else ""
            meta_desc = ""
            desc_tag = soup.find("meta", attrs={"name": "description"})
            if desc_tag:
                meta_desc = desc_tag.get("content", "").strip()
            
            # --- Header/Footer ---
            header = soup.find("header")
            header_html = str(header) if header else ""
            footer = soup.find("footer")
            footer_html = str(footer) if footer else ""

            # --- Assets: Images ---
            page_images = []
            for img in soup.find_all("img"):
                src = img.get("src")
                if src:
                    abs_src = urljoin(final_url, src)
                    filename = await self.download_asset(client, abs_src, self.images_dir)
                    if filename:
                        rel_path = f"assets/images/{filename}"
                        img["src"] = rel_path
                        page_images.append({"url": abs_src, "local": rel_path})

            # --- Assets: CSS ---
            for link in soup.find_all("link", rel="stylesheet"):
                href = link.get("href")
                if href:
                    abs_href = urljoin(final_url, href)
                    filename = await self.download_asset(client, abs_href, self.css_dir)
                    if filename:
                        link["href"] = f"assets/css/{filename}"

            # --- Assets: JS ---
            for script in soup.find_all("script", src=True):
                src = script.get("src")
                if src:
                    abs_src = urljoin(final_url, src)
                    filename = await self.download_asset(client, abs_src, self.js_dir)
                    if filename:
                        script["src"] = f"assets/js/{filename}"

            # --- internal link rewriting ---
            links_to_crawl = []
            for a in soup.find_all("a", href=True):
                href = a["href"]
                full_url = urljoin(final_url, href).split("#")[0].rstrip("/")
                parsed_href = urlparse(full_url)
                
                if parsed_href.scheme not in ["http", "https", ""]:
                    continue
                
                if self.is_internal(full_url):
                    links_to_crawl.append(full_url)
                    # Rewrite link to local .html path
                    a["href"] = self.get_local_path_for_url(full_url)

            # Store data
            content_tag = soup.find("main") or soup.find("article") or soup.find("body")
            content_html = str(content_tag) if content_tag else ""

            self.data.append({
                "slug": urlparse(final_url).path or "/",
                "title": title,
                "meta_description": meta_desc,
                "header": header_html,
                "footer": footer_html,
                "content": content_html,
                "images": page_images
            })

            # Save the reconstructed HTML file
            with open(os.path.join(self.website_dir, local_html_path), "w") as f:
                f.write(soup.prettify())

            return links_to_crawl

        except Exception as e:
            console.print(f"[red]Error extracting {url}: {e}[/red]")
            return []

    async def run(self):
        async with httpx.AsyncClient(timeout=30.0) as client:
            queue = [self.base_url]
            
            with Progress(
                SpinnerColumn(),
                TextColumn("[bold blue]{task.description}"),
                BarColumn(),
                TaskProgressColumn(),
                console=console
            ) as progress:
                crawl_task = progress.add_task("Processing Domain...", total=None)
                
                while queue:
                    current_url = queue.pop(0)
                    new_links = await self.extract_page(client, current_url)
                    for link in new_links:
                        if link not in self.visited_urls and link not in queue:
                            queue.append(link)
                    
                    progress.update(crawl_task, advance=1, description=f"Extracted: {current_url}")
                    progress.update(crawl_task, total=len(self.visited_urls) + len(queue))

        # Save data.json
        with open(os.path.join(self.output_dir, "data.json"), "w") as f:
            json.dump(self.data, f, indent=2)
        
        console.print(f"\n[bold green]Success![/bold green]")
        console.print(f"📦 JSON Packaged: [cyan]{self.output_dir}/data.json[/cyan]")
        console.print(f"🏠 Static Site: [cyan]{self.output_dir}/website/index.html[/cyan]")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        console.print("[bold red]Usage: python extractor.py <url>[/bold red]")
        sys.exit(1)
    
    url = sys.argv[1]
    if not url.startswith("http"):
        url = "https://" + url
        
    extractor = WebsiteExtractor(url)
    asyncio.run(extractor.run())
