"""Voice providers share an asynchronous transport contract; room policy lives in the runtime."""
from typing import AsyncIterator, Protocol


class Provider(Protocol):
    async def connect(self) -> None: ...
    async def send_audio(self, pcm24k: bytes) -> None: ...
    async def request_reply(self, note: str | None = None) -> None: ...
    async def cancel(self) -> None: ...
    def events(self) -> AsyncIterator[dict]: ...
    async def close(self) -> None: ...


class UnavailableProvider:
    """Interface-conforming placeholder: never silently routes audio to a different provider."""
    def __init__(self, provider, *args, **kwargs):
        self.provider = provider

    async def connect(self):
        raise NotImplementedError(f"{self.provider} is a stub; use openai_realtime for a live run")

    async def send_audio(self, pcm24k):
        raise NotImplementedError(self.provider)

    async def request_reply(self, note=None):
        raise NotImplementedError(self.provider)

    async def cancel(self):
        pass

    async def events(self):
        if False:
            yield {}
        raise NotImplementedError(self.provider)

    async def close(self):
        pass


def make_provider(config, **kwargs):
    if config.provider == "openai_realtime":
        from providers.openai_realtime import OpenAIRealtime
        return OpenAIRealtime(config, **kwargs)
    return UnavailableProvider(config.provider)
