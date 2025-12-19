import traceback
from typing import Annotated

from cyclopts import App, Parameter
from fastmcp import Client
from rich import print

app = App(help="End-to-end testing of the mcp-gateway server.")


@app.command(
    help="""Call an mcp tool. Examples:\n
    * python tests/testing
    * python tests/testing --tool npr_get_headlines
    * python tests/testing --mcp books --tool goodreads_get_book_list
    * python tests/testing --token TOKEN
    """
)
async def call_tool(
    server_url: Annotated[
        str, Parameter(help="URL of the mcp-gateway server")
    ] = "http://localhost:9000",
    mcp: Annotated[str, Parameter(help="name of the mcp server")] = "media",
    tool: Annotated[str, Parameter(help="name of the tool")] = "get_user_info",
    token: Annotated[str, Parameter(help="OAuth token to skip full auth flow")] | None = None,
):
    url = f"{server_url}/mcp-{mcp}"
    result = None
    error = None
    try:
        async with Client(url, auth=(token or "oauth"), timeout=60) as client:
            result = await client.call_tool_mcp(tool, {})
    except Exception:
        error = traceback.format_exc()

    msg = f"Tool call:\n  mcp server: {url}\n  tool: {tool}"
    if token:
        msg = f"{msg}\n  token: {token}"

    print(msg)
    if error:
        print("Error:")
        print(error)
    else:
        print("Result:")
        print(result)


if __name__ == "__main__":
    app()
