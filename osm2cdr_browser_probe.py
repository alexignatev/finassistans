from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from playwright.async_api import Response, async_playwright

URL = "https://osm2cdr.ru/kadastr-v-geojson/"
CAD = "22:43:010001:1229"
OUT = Path("osm2cdr_result")
BODIES = OUT / "bodies"
SCRIPTS = OUT / "scripts"
for folder in (OUT, BODIES, SCRIPTS):
    folder.mkdir(parents=True, exist_ok=True)

network: list[dict] = []
body_number = 0


def filename(value: str, suffix: str) -> str:
    clean = re.sub(r"[^a-zA-Z0-9._-]+", "_", value).strip("_")[-150:]
    return (clean or "resource") + suffix


async def main() -> None:
    global body_number
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="chrome", headless=True)
        context = await browser.new_context(
            viewport={"width": 1600, "height": 1100},
            locale="ru-RU",
            record_har_path=str(OUT / "network.har"),
            record_har_content="embed",
            accept_downloads=True,
        )
        page = await context.new_page()
        console: list[dict] = []
        downloads: list[dict] = []
        page.on("console", lambda message: console.append({"type": message.type, "text": message.text}))
        page.on("pageerror", lambda error: console.append({"type": "pageerror", "text": str(error)}))
        page.on("request", lambda request: network.append({"kind": "request", "method": request.method, "url": request.url, "resource_type": request.resource_type, "post_data": request.post_data}))

        async def response_handler(response: Response) -> None:
            global body_number
            content_type = response.headers.get("content-type", "")
            network.append({"kind": "response", "status": response.status, "url": response.url, "content_type": content_type})
            if not (any(token in response.url.lower() for token in ("api", "cadast", "kadastr", "geojson", "rosreestr", "nspd", "download")) or any(token in content_type.lower() for token in ("json", "javascript", "text/plain"))):
                return
            try:
                body = await response.body()
            except Exception:
                return
            if not body or len(body) > 8_000_000:
                return
            body_number += 1
            suffix = ".json" if "json" in content_type.lower() else ".js" if "javascript" in content_type.lower() else ".txt"
            (BODIES / f"{body_number:03d}_{filename(response.url, suffix)}").write_bytes(body)

        page.on("response", response_handler)
        navigation_error = None
        try:
            await page.goto(URL, wait_until="domcontentloaded", timeout=90_000)
            try:
                await page.wait_for_load_state("networkidle", timeout=25_000)
            except Exception:
                pass
        except Exception as exc:
            navigation_error = repr(exc)

        before_html = await page.content()
        (OUT / "before.html").write_text(before_html, encoding="utf-8")
        await page.screenshot(path=str(OUT / "before.png"), full_page=True)
        controls_before = await page.evaluate("""() => [...document.querySelectorAll('input,textarea,select,button,a')].map((e,i)=>({i,tag:e.tagName,type:e.type||null,id:e.id||null,name:e.name||null,placeholder:e.placeholder||null,value:e.value||null,text:(e.innerText||e.textContent||'').trim().slice(0,500),href:e.href||null,visible:!!(e.offsetWidth||e.offsetHeight||e.getClientRects().length)}))""")

        action = {"input": None, "button": None, "error": None}
        inputs = page.locator("input:not([type=hidden]), textarea")
        chosen = None
        for i in range(await inputs.count()):
            item = inputs.nth(i)
            try:
                if not await item.is_visible():
                    continue
                meta = await item.evaluate("e=>({type:e.type||'',id:e.id||'',name:e.name||'',placeholder:e.placeholder||'',aria:e.getAttribute('aria-label')||''})")
                text = " ".join(str(value) for value in meta.values()).lower()
                if chosen is None and meta.get("type") not in {"checkbox", "radio", "file", "button", "submit"}:
                    chosen = item
                    action["input"] = meta
                if any(token in text for token in ("кадастр", "cadastr", "номер", "number", "участ")):
                    chosen = item
                    action["input"] = meta
                    break
            except Exception:
                continue

        if chosen is not None:
            await chosen.fill(CAD)
            buttons = page.locator("button, input[type=submit], input[type=button], a")
            clicked = False
            for i in range(await buttons.count()):
                button = buttons.nth(i)
                try:
                    if not await button.is_visible():
                        continue
                    label = await button.evaluate("e=>((e.innerText||e.textContent||e.value||e.title||e.getAttribute('aria-label')||'')).trim()")
                    if re.search(r"получ|скача|найт|показ|сформ|конверт|выгруз|отправ", label, re.I):
                        action["button"] = label
                        try:
                            async with page.expect_download(timeout=15_000) as info:
                                await button.click(timeout=15_000)
                            download = await info.value
                            path = OUT / (download.suggested_filename or "download.bin")
                            await download.save_as(path)
                            downloads.append({"filename": path.name, "url": download.url})
                        except Exception as exc:
                            action.setdefault("download_wait_errors", []).append(repr(exc))
                        clicked = True
                        break
                except Exception as exc:
                    action.setdefault("button_errors", []).append(repr(exc))
            if not clicked:
                await chosen.press("Enter")
        else:
            action["error"] = "No visible text input found"

        await page.wait_for_timeout(25_000)
        after_html = await page.content()
        (OUT / "after.html").write_text(after_html, encoding="utf-8")
        await page.screenshot(path=str(OUT / "after.png"), full_page=True)
        controls_after = await page.evaluate("""() => [...document.querySelectorAll('input,textarea,select,button,a')].map((e,i)=>({i,tag:e.tagName,type:e.type||null,id:e.id||null,name:e.name||null,placeholder:e.placeholder||null,value:e.value||null,text:(e.innerText||e.textContent||'').trim().slice(0,500),href:e.href||null,download:e.download||null,visible:!!(e.offsetWidth||e.offsetHeight||e.getClientRects().length)}))""")
        (OUT / "page_report.json").write_text(json.dumps({"navigation_error": navigation_error, "action": action, "downloads": downloads, "before": controls_before, "after": controls_after, "console": console}, ensure_ascii=False, indent=2), encoding="utf-8")
        (OUT / "network.json").write_text(json.dumps(network, ensure_ascii=False, indent=2), encoding="utf-8")
        await context.close()
        await browser.close()

    soup = BeautifulSoup((OUT / "after.html").read_text(encoding="utf-8"), "html.parser")
    session = requests.Session()
    session.headers["User-Agent"] = "Mozilla/5.0 Chrome/131 Safari/537.36"
    searchable: list[tuple[str, str]] = [("before.html", before_html), ("after.html", after_html)]
    script_log = []
    for tag in soup.find_all("script"):
        src = tag.get("src")
        if not src:
            continue
        url = urljoin(URL, src)
        try:
            response = session.get(url, timeout=50)
            script_log.append({"url": url, "status": response.status_code, "size": len(response.content)})
            if response.status_code == 200:
                path = SCRIPTS / filename(url, ".js")
                path.write_bytes(response.content)
                searchable.append((url, response.text))
        except Exception as exc:
            script_log.append({"url": url, "error": repr(exc)})

    patterns = [
        r"https?://[^\"'`\\\s]+",
        r"/[a-zA-Z0-9_./{}:-]*(?:api|cadast|kadastr|nspd|rosreestr|geojson|download)[a-zA-Z0-9_?&=./{}:%-]*",
        r"(?:fetch|axios\.(?:get|post)|XMLHttpRequest)[^;]{0,1200}",
        r"(?:cadastral|cadastre|cadast|kadastr|кадастр|nspd|rosreestr|geojson)[^\n;]{0,1500}",
    ]
    findings = []
    seen = set()
    for source, text in searchable:
        for pattern in patterns:
            for match in re.finditer(pattern, text, re.I):
                value = match.group(0)[:2500]
                key = (source, value)
                if key not in seen:
                    seen.add(key)
                    findings.append({"source": source, "match": value})
    (OUT / "code_findings.json").write_text(json.dumps({"scripts": script_log, "findings": findings}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"network_events": len(network), "saved_bodies": body_number, "scripts": len(script_log), "findings": len(findings), "downloads": downloads}, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
