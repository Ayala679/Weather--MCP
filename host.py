import asyncio
import os
from typing import Any, AsyncIterator

from google import genai
from google.genai import types
from dotenv import load_dotenv

from client import MCPClient

load_dotenv()

# Free-tier model on Google AI Studio. "gemini-flash-latest" always points at the
# current Flash release; pin an explicit name (e.g. "gemini-3.6-flash") if you
# want reproducible behaviour.
MODEL = "gemini-flash-latest"

# JSON-schema keywords that Gemini's function-calling accepts. FastMCP emits a
# few extras ("title", "additionalProperties", "$defs"...) that trigger 400s, so
# we strip everything else before handing a schema to the model.
_ALLOWED_SCHEMA_KEYS = {
    "type", "description", "enum", "items", "properties", "required", "nullable",
}


def _clean_schema(schema: Any) -> Any:
    """Recursively drop schema keys Gemini does not understand."""
    if not isinstance(schema, dict):
        return schema

    cleaned: dict[str, Any] = {}
    for key, value in schema.items():
        if key not in _ALLOWED_SCHEMA_KEYS:
            continue
        if key == "properties" and isinstance(value, dict):
            cleaned[key] = {name: _clean_schema(sub) for name, sub in value.items()}
        elif key == "items":
            cleaned[key] = _clean_schema(value)
        else:
            cleaned[key] = value
    return cleaned


class ChatHost:
    """Bridges a Gemini chat model to one or more MCP tool servers."""

    def __init__(self):
        self.mcp_clients: list[MCPClient] = [
            MCPClient("./weather_USA.py"),
            MCPClient("./weather_Israel.py"),
        ]
        self.tool_clients: dict[str, tuple[MCPClient, str]] = {}
        self.clients_connected = False

        api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not api_key:
            raise RuntimeError(
                "Missing GEMINI_API_KEY in .env - get a free key at "
                "https://aistudio.google.com/apikey"
            )

        # client_args are forwarded to httpx; verify=False lets requests pass
        # through the Netfree TLS proxy without a certificate error.
        self.client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(
                client_args={"verify": False},
                async_client_args={"verify": False},
            ),
        )

    async def connect_mcp_clients(self):
        """Connect every configured MCP client once."""
        if self.clients_connected:
            return

        for client in self.mcp_clients:
            if client.session is None:
                await client.connect_to_server()

        if not self.mcp_clients:
            raise RuntimeError("No MCP clients are configured")

        self.clients_connected = True

    async def build_tools(self) -> list[types.Tool]:
        """Collect tools from all MCP servers as Gemini function declarations."""
        await self.connect_mcp_clients()
        self.tool_clients = {}
        declarations: list[types.FunctionDeclaration] = []

        for client in self.mcp_clients:
            if client.session is None:
                print(f"Warning: {client.client_name} is not connected, skipping")
                continue

            try:
                response = await client.session.list_tools()
            except Exception as exc:
                print(f"Warning: could not list tools from {client.client_name}: {exc}")
                continue

            for tool in response.tools:
                exposed_name = f"{client.client_name}__{tool.name}"
                if exposed_name in self.tool_clients:
                    raise RuntimeError(f"Duplicate tool name: {exposed_name}")

                self.tool_clients[exposed_name] = (client, tool.name)
                schema = _clean_schema(tool.inputSchema or {})
                parameters = schema if schema.get("properties") else None
                declarations.append(
                    types.FunctionDeclaration(
                        name=exposed_name,
                        description=f"[{client.client_name}] {tool.description}",
                        parameters=parameters,
                    )
                )

        if not declarations:
            raise RuntimeError("No tools available from any MCP server")

        return [types.Tool(function_declarations=declarations)]

    async def _generate(self, contents: list, config: "types.GenerateContentConfig"):
        """Call Gemini, retrying briefly on transient 429/503 responses."""
        delay = 2.0
        for attempt in range(4):
            try:
                return await self.client.aio.models.generate_content(
                    model=MODEL, contents=contents, config=config
                )
            except Exception as exc:
                code = getattr(exc, "code", None)
                if code not in (429, 503) or attempt == 3:
                    raise
                await asyncio.sleep(delay)
                delay *= 2

    async def _run_tool(self, name: str, args: dict[str, Any]) -> str:
        client, original_name = self.tool_clients[name]
        if client.session is None:
            raise RuntimeError(f"MCP client {client.client_name} is not connected")

        result = await client.session.call_tool(original_name, args)
        chunks = [
            part.text
            for part in result.content
            if getattr(part, "type", None) == "text"
        ]
        return "\n".join(chunks) if chunks else "(the tool returned no text)"

    async def stream_query(self, query: str) -> AsyncIterator[tuple]:
        """Run the Gemini tool-calling loop, yielding progress events.

        Emits ("tool", name, args) right before each MCP call and
        ("text", chunk) for every piece of model prose.
        """
        tools = await self.build_tools()
        config = types.GenerateContentConfig(tools=tools)
        contents: list[types.Content] = [
            types.Content(role="user", parts=[types.Part(text=query)])
        ]

        while True:
            response = await self._generate(contents, config)

            candidate = response.candidates[0]
            contents.append(candidate.content)
            parts = candidate.content.parts or []

            for part in parts:
                if part.text:
                    yield ("text", part.text)

            calls = [part.function_call for part in parts if part.function_call]
            if not calls:
                return

            tool_responses: list[types.Part] = []
            for call in calls:
                args = dict(call.args or {})
                yield ("tool", call.name, args)
                output = await self._run_tool(call.name, args)
                tool_responses.append(
                    types.Part.from_function_response(
                        name=call.name, response={"result": output}
                    )
                )

            contents.append(types.Content(role="user", parts=tool_responses))

    async def process_query(self, query: str) -> str:
        """Collect a full answer as plain text (used by the terminal client)."""
        lines: list[str] = []
        async for event in self.stream_query(query):
            if event[0] == "tool":
                _, name, args = event
                lines.append(f"[running tool {name} with {args}]")
            else:
                lines.append(event[1])
        return "\n".join(lines)

    async def chat_loop(self):
        """Run an interactive chat loop."""
        print("\nWeather chat is ready.")
        print("Ask about the weather in any city, or type 'quit' to exit.")

        while True:
            try:
                query = input("\nQuery: ").strip()
                if query.lower() == "quit":
                    break
                if not query:
                    continue
                print("\n" + await self.process_query(query))
            except Exception as exc:
                print(f"\nchat_loop error: {exc}")

    async def cleanup(self):
        """Release MCP resources."""
        for client in reversed(self.mcp_clients):
            await client.cleanup()


async def main():
    host = ChatHost()
    try:
        await host.chat_loop()
    finally:
        await host.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
