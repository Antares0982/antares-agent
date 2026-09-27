import asyncio
import hashlib
import resource

import httpx

from antares_agent.artifacts import CHUNK, MAX_BYTES
from antares_agent.transfers import checked_stream

resource.setrlimit(resource.RLIMIT_AS, (256 * 1024**2, 256 * 1024**2))
block = bytes(CHUNK)
digest = hashlib.sha256()
for offset in range(0, MAX_BYTES, CHUNK):
    digest.update(block[: min(CHUNK, MAX_BYTES - offset)])


class Stream(httpx.AsyncByteStream):
    async def __aiter__(self):
        for offset in range(0, MAX_BYTES, CHUNK):
            yield block[: min(CHUNK, MAX_BYTES - offset)]


async def main():
    count = 0
    response = httpx.Response(200, stream=Stream())
    async for chunk in checked_stream(response, {"size": MAX_BYTES, "sha256": digest.hexdigest()}):
        count += len(chunk)
    assert count == MAX_BYTES
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    print(f"{count} bytes streamed; peak RSS={peak} KiB; address-space limit=256 MiB")


asyncio.run(main())
