import tempfile, asyncio, json, time
from app.canonical_conversation_agent import AgentContextBuilder, CanonicalAgentToolBackend, CanonicalConversationAgent, OpenAICompatibleAgentModel


class RecognitionFixture:
    async def recognize(self, *_args, **_kwargs):
        return recognition()


def image_event():
    return {"envelope": {"id": "synthetic-image", "tenantId": "isolated", "event": "im.message.received",
        "payload": {"accountUnb": "test-shop", "peerUnb": "test-buyer", "chatId": "test-chat",
                    "itemId": "synthetic-purchase", "imageUrls": ["https://fixture.invalid/image"],
                    "messageType": 2}},
        "session": {"accountUnb": "test-shop", "peerUnb": "test-buyer", "chatId": "test-chat"}}


async def execute(client):
    with tempfile.TemporaryDirectory(prefix="v4-agent-isolated-") as directory:
        r = runtime(Path(directory))
        r._recognition = RecognitionFixture()
        first = await r.process_image_event(image_event())
        print("IMAGE", json.dumps(first, ensure_ascii=False), flush=True)
        assert first["status"] == "QUOTED"
        event = image_event()
        event["envelope"]["id"] = "synthetic-two"
        event["envelope"]["payload"].update(content="2张", messageType=1, imageUrls=[])
        store = r._quotes.store
        agent = CanonicalConversationAgent(AgentContextBuilder(quote_store=store), client,
            tool_backend=CanonicalAgentToolBackend(quote_runtime=r, quote_store=store), max_tool_rounds=3)
        result = await asyncio.wait_for(agent.process(event), 90)
        print("AGENT", json.dumps({k:result.get(k) for k in ["status","reason","reply","tool_trace","model_diagnostic","actions"]},ensure_ascii=False),flush=True)
        print("QUOTES",json.dumps(store.list("isolated"),ensure_ascii=False),flush=True)
        return result
