"""Network tools: HTTP fetch and browser automation.

``http.fetch`` is plain HTTPS/HTTP with a response-size cap. Browser tools are
backed by Playwright when the optional ``browser`` extra is installed; when it
is not, the tools still exist but return an actionable failure — the model (and
operator) learn exactly what to enable rather than hitting a missing tool.
"""

from __future__ import annotations

import httpx
from pydantic import BaseModel, Field

from hoursx.tools.base import FunctionTool, ToolContext, ToolOutcome, ToolSpec
from hoursx.tools.registry import ToolRegistry

_BODY_CAP = 80_000


class FetchArgs(BaseModel):
    url: str = Field(description="HTTP or HTTPS URL to fetch")
    method: str = Field(default="GET", pattern="^(GET|HEAD)$")


class BrowseArgs(BaseModel):
    url: str = Field(description="Page URL to open in the headless browser")


async def _fetch(args: FetchArgs, ctx: ToolContext) -> ToolOutcome:
    if not args.url.startswith(("http://", "https://")):
        return ToolOutcome.failure("Only http(s) URLs are allowed.")
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
            response = await client.request(args.method, args.url)
    except httpx.HTTPError as exc:
        return ToolOutcome.failure(f"Fetch failed: {exc}. Check the URL and retry.")
    body = response.text[:_BODY_CAP]
    return ToolOutcome.success(
        f"HTTP {response.status_code} from {args.url} ({len(body)} chars)",
        status=response.status_code,
        content_type=response.headers.get("content-type", ""),
        body=body,
        truncated=len(response.text) > _BODY_CAP,
    )


async def _browse(args: BrowseArgs, ctx: ToolContext) -> ToolOutcome:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return ToolOutcome.failure(
            "Browser automation is not enabled on this deployment "
            "(install the 'browser' extra). Use http.fetch for plain pages."
        )
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        try:
            page = await browser.new_page()
            await page.goto(args.url, wait_until="domcontentloaded", timeout=30_000)
            title = await page.title()
            text = (await page.inner_text("body"))[:_BODY_CAP]
        finally:
            await browser.close()
    return ToolOutcome.success(f"Rendered {args.url} — {title!r}", title=title, text=text)


def register_web_tools(registry: ToolRegistry) -> None:
    registry.register(
        FunctionTool(
            ToolSpec(
                name="http.fetch",
                description="Fetch a URL over HTTP(S) and return status and body text.",
                params_model=FetchArgs,
            ),
            _fetch,
        )
    )
    registry.register(
        FunctionTool(
            ToolSpec(
                name="browser.open",
                description=(
                    "Open a page in a headless browser (JavaScript rendered) and "
                    "return its title and visible text."
                ),
                params_model=BrowseArgs,
                timeout_seconds=90,
            ),
            _browse,
        )
    )
