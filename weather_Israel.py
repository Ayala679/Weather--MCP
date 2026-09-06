from mcp.server.fastmcp import FastMCP
from playwright.async_api import TimeoutError as PlaywrightTimeout
from playwright.async_api import async_playwright, Browser, Page, Playwright

mcp = FastMCP("weather-Israel")

FORECAST_URL = "https://www.weather2day.co.il/forecast"

_playwright: Playwright | None = None
_browser: Browser | None = None
_page: Page | None = None


async def _get_page() -> Page:
    """Return the shared page, launching Chrome if not already open."""
    global _playwright, _browser, _page
    if _browser is None or not _browser.is_connected():
        _playwright = await async_playwright().start()
        _browser = await _playwright.chromium.launch(channel="chrome", headless=False)
        _page = await _browser.new_page()
    elif _page is None:
        _page = await _browser.new_page()
    return _page


@mcp.tool()
async def open_weather_forecast_israel() -> str:
    """Open Chrome and navigate to the Israel weather forecast page."""
    page = await _get_page()
    await page.goto(FORECAST_URL)
    await page.wait_for_load_state("domcontentloaded")
    return f"Opened Chrome at {FORECAST_URL}"


@mcp.tool()
async def enter_weather_forecast_city_israel(city: str) -> str:
    """Types the specific city name into the search bar. Requires 'city' argument. Must be called right after opening the browser."""
    page = await _get_page()
    search_input = page.locator("#city_search_forecast")
    await search_input.wait_for(state="visible", timeout=5000)
    await search_input.click()
    await search_input.fill(city)
    await page.wait_for_timeout(1500)
    return f"Typed city '{city}' into search bar on {FORECAST_URL}"


@mcp.tool()
async def select_weather_forecast_city_israel() -> str:
    """Clicks the first suggestion in the search dropdown menu. Call this ONLY after enter_weather_forecast_city_israel has finished executing. Takes no arguments."""
    page = await _get_page()
    search_input = page.locator("#city_search_forecast")
    await search_input.click()
    await search_input.press("ArrowDown")
    await search_input.press("Enter")
    await page.wait_for_timeout(3000)
    return "Selected first suggestion from the dropdown"


@mcp.tool()
async def get_weather_page_content_israel() -> str:
    """Extracts and returns the visible text content of the currently open weather page. Call this after navigating to a city forecast to give the LLM the actual weather data."""
    page = await _get_page()
    await page.wait_for_load_state("domcontentloaded")

    # Extract all visible text from the page body
    raw_text: str = await page.evaluate("""() => {
        const remove = ['script', 'style', 'noscript', 'nav', 'footer', 'iframe', 'img'];
        remove.forEach(tag => document.querySelectorAll(tag).forEach(el => el.remove()));
        return document.body.innerText;
    }""")

    # Clean up the extracted text
    lines = raw_text.splitlines()
    seen: set[str] = set()
    cleaned_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped in seen:
            continue
        seen.add(stripped)
        cleaned_lines.append(stripped)

    content = "\n".join(cleaned_lines)
    # Truncate to avoid overwhelming the LLM context
    if len(content) > 8000:
        content = content[:8000] + "\n...[truncated]"

    return f"=== Weather Page Content ===\nURL: {page.url}\n\n{content}"


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
