# mia-sdk (Python)

Thin async wrapper for apps that sign in with Mia (M50). Operation paths are generated from
`contracts/mvp/v1/openapi.yaml` (`scripts/generate_operations.py`); full docs are M43.

```python
from mia_sdk import MiaClient, MiaOAuth

oauth = MiaOAuth(issuer="https://mia.example", client_id="https://app.example/client.json",
                 redirect_uri="https://app.example/callback")
start = oauth.start(scope="openid llm.generate memory.context")   # redirect to start.url
tokens = await oauth.finish(callback_url, state=start.state, verifier=start.verifier)

async with MiaClient(base_url="https://mia.example", access_token=tokens.access_token) as mia:
    answer = await mia.chat_completions(
        {"model": "mia:default", "messages": [{"role": "user", "content": "Привет"}],
         "mia": {"context": True}}
    )
```

Any OpenAI-compatible library works too: `base_url="https://mia.example/v1"`,
`api_key=<access token>`, `model="mia:default"` (LiteLLM: `model="openai/mia:default"`).
Refresh tokens rotate: store the new one from every `oauth.refresh(...)`.
