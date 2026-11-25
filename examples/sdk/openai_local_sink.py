"""Capture OpenAI calls to local files. No control plane is needed.

Run: OPENAI_API_KEY=... python openai_local_sink.py
"""

import os

import roadbed_sdk
from openai import OpenAI

# needs roadbed-sdk >= 0.1
roadbed_sdk.init_local(
    "./roadbed-events",
    tenant_key=os.environ["ROADBED_TENANT_KEY"],
    profile_id="demo-default@v1",
    model="gpt-4o-mini",
)

client = roadbed_sdk.wrap_openai(OpenAI())

with roadbed_sdk.capture(subject="customer-42", slices={"channel": "web"}):
    reply = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": "Summarise this claim in one line."}],
    )
    print(reply.choices[0].message.content)
    print("interaction id:", roadbed_sdk.last_interaction_id())

roadbed_sdk.shutdown()
