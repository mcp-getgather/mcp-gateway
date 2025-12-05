import asyncio
import sys
from typing import TypedDict

from fastmcp import Client
from rich import print

TestConfig = TypedDict("TestConfig", {"mcp": str, "tool": str})

configs: dict[str, TestConfig] = {
    "user": {"mcp": "/mcp-media", "tool": "get_user_info"},
    "npr": {"mcp": "/mcp-media", "tool": "npr_get_headlines"},
    "goodreads": {"mcp": "/mcp-books", "tool": "goodreads_get_book_list"},
}


async def call_tool(base_url: str, config: TestConfig):
    url = f"{base_url}{config['mcp']}"
    async with Client(url, auth="oauth") as client:
        result = await client.call_tool_mcp(config["tool"], {})

    print(f"Tool call:\n  mcp server: {url}\n  tool: {config['tool']}")
    print("Result:")
    print(result)


if __name__ == "__main__":
    match len(sys.argv):
        case 2:
            test = sys.argv[1]
            url = "http://localhost:9000"
        case 3:
            test = sys.argv[1]
            url = sys.argv[2]
        case _:
            print("Usage: python main.py <test_name> [<url>]")
            print(f"  possible test names: {', '.join(configs.keys())}")
            sys.exit(1)

    asyncio.run(call_tool(url, configs[test]))
