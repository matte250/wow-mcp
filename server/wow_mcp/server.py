"""MCP server that lets AI agents show things to the player inside World of Warcraft and read
their game. The tools live in wow_mcp/tools/, one module per feature."""

from mcp.server.mcpserver import MCPServer

from wow_mcp import app, tools  # noqa: F401 (importing tools registers them)

INTRO = "Tools for showing things to a player who is playing World of Warcraft right now, and for reading their game."


def create_server() -> MCPServer:
    paragraphs = [INTRO, app.game_note(), *(text for _, text in sorted(app.GUIDANCE, key=lambda g: g[0]))]
    server = MCPServer("wow", instructions="\n\n".join(paragraphs))
    for fn, options in app.TOOLS:
        server.add_tool(fn, **options)
    return server


def main() -> None:
    create_server().run()


if __name__ == "__main__":
    main()
