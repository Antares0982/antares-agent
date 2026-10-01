import asyncio
import json

import pytest

from antares_agent.auth import TokenClient
from antares_agent.config import Settings
from antares_agent.runtime import Runtime


async def test_external_auth(tmp_path):
    path = tmp_path / "auth.sock"
    requests = []
    response = {"accessToken": "fixture", "chatgptAccountId": "account", "generation": "one"}

    async def serve(reader, writer):
        requests.append(json.loads(await reader.readline()))
        writer.write(json.dumps(response).encode() + b"\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_unix_server(serve, path=path)
    try:
        client = TokenClient(path)
        assert "generation" not in await asyncio.to_thread(client.fetch)
        runtime = Runtime(Settings(auth_socket=path))
        runtime.tokens = client
        await asyncio.to_thread(
            runtime.handle_request,
            "account/chatgptAuthTokens/refresh",
            {"previousAccountId": "account"},
        )
        assert requests[-1] == {"refresh": True, "generation": "one", "account": "account"}
        response["chatgptAccountId"] = "other"
        with pytest.raises(RuntimeError, match="账户已变更"):
            await asyncio.to_thread(client.fetch)
    finally:
        server.close()
        await server.wait_closed()
