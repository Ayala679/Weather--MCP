"""Gradio chat interface for the weather MCP host.

Run with:  uv run python app.py
Then open the local URL it prints (default http://127.0.0.1:7860).
"""

import asyncio

import gradio as gr

from host import ChatHost

# Human-readable Hebrew labels for the tool-call progress log.
TOOL_LABELS = {
    "weather_Israel__get_israel_forecast": "פותח דפדפן ושולף את התחזית",
    "weather_Israel__open_weather_forecast_israel": "פותח את אתר התחזית",
    "weather_Israel__enter_weather_forecast_city_israel": "מקליד את שם העיר",
    "weather_Israel__select_weather_forecast_city_israel": "בוחר עיר מהרשימה",
    "weather_Israel__get_weather_page_content_israel": "קורא את נתוני התחזית מהדף",
    "weather_USA__get_forecast_in_USA": "שולף תחזית (ארה״ב)",
    "weather_USA__get_alerts_in_USA": "שולף אזהרות מזג אוויר (ארה״ב)",
}

QUOTA_MESSAGE = (
    "❌ נגמרה מכסת הבקשות היומית החינמית של Gemini למודל הזה. "
    "המכסה מתאפסת אוטומטית אחרי 24 שעות. "
    "אפשר גם לשנות `MODEL` ב-host.py למודל אחר, או להוסיף חיוב ב-Google AI Studio."
)

_host: ChatHost | None = None
_host_lock = asyncio.Lock()


async def _get_host() -> ChatHost:
    """Create and connect the host once, then reuse it for every message."""
    global _host
    if _host is None:
        host = ChatHost()
        await host.connect_mcp_clients()
        _host = host
    return _host


def _render(steps: list[str], answer: str, done: bool) -> str:
    """Render the progress steps (collapsible) followed by the answer."""
    block = ""
    if steps:
        state = "מה שקרה מאחורי הקלעים" if done else "עובד…"
        open_attr = "" if done else " open"
        bullets = "\n".join(f"- {step}" for step in steps)
        block = f"<details{open_attr}><summary>\U0001f527 {state}</summary>\n\n{bullets}\n\n</details>\n\n"
    return block + answer


async def chat(message: str, history: list):
    """Stream the assistant's answer, updating a live progress log as it works."""
    message = (message or "").strip()
    if not message:
        yield "כתוב/י שאלה על מזג האוויר בעיר כלשהי בישראל."
        return

    async with _host_lock:
        try:
            host = await _get_host()
        except Exception as exc:
            yield f"❌ לא הצלחתי להתחיל את השרת: {exc}"
            return

        steps: list[str] = []
        answer = ""
        try:
            async for event in host.stream_query(message):
                if event[0] == "tool":
                    _, name, args = event
                    label = TOOL_LABELS.get(name, name)
                    city = args.get("city")
                    steps.append(label + (f" — “{city}”" if city else ""))
                else:
                    answer += event[1]
                yield _render(steps, answer, done=False)
            yield _render(steps, answer, done=True)
        except Exception as exc:
            is_quota = getattr(exc, "code", None) == 429 or "RESOURCE_EXHAUSTED" in str(exc)
            note = QUOTA_MESSAGE if is_quota else f"❌ שגיאה: {exc}"
            yield _render(steps, answer, done=True) + "\n\n" + note


CSS = """
.gradio-container {
    direction: rtl;
    max-width: 880px !important;
    margin-left: auto !important;
    margin-right: auto !important;
}
.gradio-container h1 { letter-spacing: -0.5px; }
details > summary { cursor: pointer; color: var(--body-text-color-subdued); font-size: 0.9em; }
footer { display: none !important; }
"""

with gr.Blocks(title="מזג האוויר בצ׳אט") as demo:
    gr.ChatInterface(
        fn=chat,
        title="⛅️  מזג האוויר בצ׳אט",
        description=(
            "שאל/י על מזג האוויר בכל עיר בישראל. "
            "המערכת פותחת דפדפן אמיתי, מחפשת את העיר באתר התחזית, "
            "קוראת את הנתונים ומנסחת תשובה."
        ),
        examples=[
            "מה מזג האוויר עכשיו בתל אביב?",
            "האם צפוי גשם מחר בירושלים?",
            "כמה חם היום בחיפה ומה הלחות?",
            "מה התחזית לסוף השבוע בבאר שבע?",
        ],
    )


if __name__ == "__main__":
    demo.launch(
        theme=gr.themes.Soft(primary_hue="sky", neutral_hue="slate"),
        css=CSS,
    )
