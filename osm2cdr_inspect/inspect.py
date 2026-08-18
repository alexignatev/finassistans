from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from playwright.async_api import BrowserContext, Page, Response, async_playwright

URL = "https://osm2cdr.ru/kadastr-v-geojson/"
TEST_CAD = "22:43:010001:1229"
OUT = Path("osm2cdr_result")
BODIES = OUT / "response_bodies"
SCRIPTS = OUT / "scripts"
for folder in (OUT, BODIES, SCRIPTS):
    folder.mkdir(parents=True, exist_ok=True)

network: list[dict[str, Any]] = []
body_counter = 0


def safe_name(value: str, suffix: str = "") -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "_", value).strip("_")
    return (cleaned[-160:] or "resource") + suffix


async def save_interesting_body(response: Response) -> None:
    global body_counter
    url = response.url
    content_type = (response.headers.get("content-type") or "").lower()
    interesting = any(token in url.lower() for token in ("api", "cadast", "kadastr", "geojson", "rosreestr", "nspd", "download"))
    interesting = interesting or any(token in content_type for token in ("json", "geo+json", "javascript", "text/plain"))
    if not interesting:
        return
    try:
        body = await response.body()
    except Exception:
        return
    if not body or len(body) > 8_000_000:
        return
    body_counter += 1
    extension = ".bin"
    if "json" in content_type:
        extension = ".json"
    elif "javascript" in content_type:
        extension = ".js"
    elif "text" in content_type or "html" in content_type:
        extension = ".txt"
    path = BODIES / f"{body_counter:03d}_{safe_name(url, extension)}"
    path.write_bytes(body)


async def describe_page(page: Page, label: str) -> dict[str, Any]:
    return await page.evaluate(
        """(label) => ({
            label,
            url: location.href,
            title: document.title,
            forms: [...document.forms].map((form, fi) => ({
                index: fi,
                action: form.action,
                method: form.method,
                enctype: form.enctype,
                text: form.innerText.slice(0, 1000),
                fields: [...form.elements].map((el, ei) => ({
                    index: ei,
                    tag: el.tagName,
                    type: el.type || null,
                    name: el.name || null,
                    id: el.id || null,
                    placeholder: el.placeholder || null,
                    value: el.value || null,
                    text: (el.innerText || el.textContent || '').trim().slice(0, 300),
                })),
            })),
            inputs: [...document.querySelectorAll('input,textarea,select,button,a')].map((el, i) => ({
                index: i,
                tag: el.tagName,
                type: el.type || null,
                name: el.name || null,
                id: el.id || null,
                placeholder: el.placeholder || null,
                value: el.value || null,
                text: (el.innerText || el.textContent || '').trim().slice(0, 400),
                href: el.href || null,
                visible: !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length),
            })),
            scripts: [...document.scripts].map(s => ({src: s.src || null, type: s.type || null, text: s.src ? null : s.textContent.slice(0, 3000)})),
        })""",
        label,
    )


async def choose_and_submit(page: Page) -> dict[str, Any]:
    candidates = page.locator("input:not([type=hidden]), textarea")
    count = await candidates.count()
    chosen_index: int | None = None
    chosen_meta: dict[str, Any] | None = None
    for index in range(count):
        locator = candidates.nth(index)
        try:
            if not await locator.is_visible():
                continue
            meta = await locator.evaluate(
                "el => ({type: el.type || '', name: el.name || '', id: el.id || '', placeholder: el.placeholder || '', aria: el.getAttribute('aria-label') || ''})"
            )
            haystack = " ".join(str(value) for value in meta.values()).lower()
            if any(token in haystack for token in ("кадастр", "cadastr", "номер", "number", "участ")):
                chosen_index, chosen_meta = index, meta
                break
            if chosen_index is None and meta.get("type") not in {"checkbox", "radio", "file", "submit", "button"}:
                chosen_index, chosen_meta = index, meta
        except Exception:
            continue
    result: dict[str, Any] = {"chosen_input_index": chosen_index, "chosen_input": chosen_meta, "actions": []}
    if chosen_index is None:
        result["error"] = "No visible text input found"
        return result
    field = candidates.nth(chosen_index)
    await field.fill(TEST_CAD)
    result["actions"].append("filled cadastral number")

    button_patterns = [
        re.compile(pattern, re.I)
        for pattern in ("получ", "скача", "найт", "показ", "сформ", "конверт", "выгруз", "отправ")
    ]
    buttons = page.locator("button, input[type=submit], input[type=button], a")
    button_count = await buttons.count()
    for index in range(button_count):
        button = buttons.nth(index)
        try:
            if not await button.is_visible():
                continue
            label = await button.evaluate(
                "el => ((el.innerText || el.textContent || el.value || el.getAttribute('aria-label') || el.title || '')).trim()"
            )
            if any(pattern.search(label) for pattern in button_patterns):
                result["chosen_button_index"] = index
                result["chosen_button_label"] = label
                await button.click(timeout=15_000)
                result["actions"].append(f"clicked: {label}")
                return result
        except Exception as exc:
            result.setdefault("button_errors", []).append(repr(exc))
    await field.press("Enter")
    result["actions"].append("pressed Enter")
    return result


async def run() -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context: BrowserContext = await browser.new_context(
            viewport={"width": 1600, "height": 1100},
            locale="ru-RU",
            record_har_path=str(OUT / "network.har"),
            record_har_content="embed",
        )
        page = await context.new_page()

        page.on(
            "request",
            lambda request: network.append(
                {
                    "kind": "request",
                    "method": request.method,
                    "url": request.url,
                    "resource_type": request.resource_type,
                    "post_data": request.post_data,
                }
            ),
        )

        async def on_response(response: Response) -> None:
            network.append(
                {
                    "kind": "response",
                    "status": response.status,
                    "url": response.url,
                    "content_type": response.headers.get("content-type"),
                }
            )
            await save_interesting_body(response)

        page.on("response", on_response)
        console_messages: list[dict[str, str]] = []
        page.on("console", lambda message: console_messages.append({"type": message.type, "text": message.text}))
        page.on("pageerror", lambda error: console_messages.append({"type": "pageerror", "text": str(error)}))

        navigation_error = None
        try:
            await page.goto(URL, wait_until="domcontentloaded", timeout=90_000)
            try:
                await page.wait_for_load_state("networkidle", timeout=30_000)
            except Exception:
                pass
        except Exception as exc:
            navigation_error = repr(exc)

        (OUT / "before.html").write_text(await page.content(), encoding="utf-8")
        await page.screenshot(path=str(OUT / "before.png"), full_page=True)
        before = await describe_page(page, "before")
        submit = await choose_and_submit(page)

        try:
            await page.wait_for_timeout(25_000)
            try:
                await page.wait_for_load_state("networkidle", timeout=20_000)
            except Exception:
                pass
        except Exception:
            pass

        (OUT / "after.html").write_text(await page.content(), encoding="utf-8")
        await page.screenshot(path=str(OUT / "after.png"), full_page=True)
        after = await describe_page(page, "after")
        downloads = await page.evaluate(
            """() => [...document.querySelectorAll('a')].map(a => ({text:(a.innerText||a.textContent||'').trim(), href:a.href, download:a.download})).filter(x => x.download || /geojson|kml|shp|download|скача/i.test(x.href+' '+x.text))"""
        )

        (OUT / "page_report.json").write_text(
            json.dumps(
                {
                    "navigation_error": navigation_error,
                    "before": before,
                    "submit": submit,
                    "after": after,
                    "downloads": downloads,
                    "console": console_messages,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        (OUT / "network.json").write_text(json.dumps(network, ensure_ascii=False, indent=2), encoding="utf-8")
        await context.close()
        await browser.close()

    # Fetch all same-origin script files and search them outside the browser.
    soup = BeautifulSoup((OUT / "after.html").read_text(encoding="utf-8"), "html.parser")
    session = requests.Session()
    session.headers["User-Agent"] = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131 Safari/537.36"
    script_records: list[dict[str, Any]] = []
    searchable: list[tuple[str, str]] = [("before.html", (OUT / "before.html").read_text(encoding="utf-8")), ("after.html", (OUT / "after.html").read_text(encoding="utf-8"))]
    for tag in soup.find_all("script"):
        src = tag.get("src")
        if not src:
            continue
        script_url = urljoin(URL, src)
        try:
            response = session.get(script_url, timeout=50)
            script_records.append({"url": script_url, "status": response.status_code, "size": len(response.content), "content_type": response.headers.get("content-type")})
            if response.status_code == 200:
                filename = safe_name(script_url, ".js")
                path = SCRIPTS / filename
                path.write_bytes(response.content)
                searchable.append((script_url, response.text))
        except Exception as exc:
            script_records.append({"url": script_url, "error": repr(exc)})

    patterns = [
        r"https?://[^\"'`\\\s]+",
        r"/[a-zA-Z0-9_./{}:-]*(?:api|cadast|kadastr|nspd|rosreestr|geojson|download)[a-zA-Z0-9_?&=./{}:%-]*",
        r"(?:fetch|axios\.(?:get|post)|XMLHttpRequest)[^;]{0,1000}",
        r"(?:cadastral|cadastre|cadast|kadastr|кадастр|nspd|rosreestr|geojson)[^\n;]{0,1200}",
    ]
    findings: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for source, text in searchable:
        for pattern in patterns:
            for match in re.finditer(pattern, text, flags=re.IGNORECASE):
                snippet = match.group(0)[:2000]
                key = (source, snippet)
                if key in seen:
                    continue
                seen.add(key)
                findings.append({"source": source, "match": snippet})
    (OUT / "code_findings.json").write_text(json.dumps({"scripts": script_records, "findings": findings}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"network_events": len(network), "bodies": body_counter, "scripts": len(script_records), "findings": len(findings)}, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(run())
