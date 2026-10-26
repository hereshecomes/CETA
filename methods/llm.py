"""Minimal OpenAI-compatible chat client for the local vLLM server, with usage accounting."""
import json
import time

import requests


class InfraError(RuntimeError):
    pass


class LLM:
    def __init__(self, base_url, model, temperature=0.6, top_p=0.95, max_tokens=8192, thinking=True,
                 timeout=600, retries=2):
        self.base_url, self.model = base_url.rstrip("/"), model
        self.temperature, self.top_p, self.max_tokens = temperature, top_p, max_tokens
        self.thinking, self.timeout, self.retries = thinking, timeout, retries
        self.calls, self.prompt_tokens, self.completion_tokens, self.retried = 0, 0, 0, 0

    def chat(self, messages, schema=None):
        body = {"model": self.model, "messages": messages, "temperature": self.temperature,
                "top_p": self.top_p, "max_tokens": self.max_tokens,
                "chat_template_kwargs": {"enable_thinking": self.thinking}}
        if schema is not None:
            body["response_format"] = {"type": "json_schema", "json_schema": {"name": "action", "schema": schema}}
        last = None
        for attempt in range(self.retries + 1):
            try:
                resp = requests.post(f"{self.base_url}/v1/chat/completions", json=body, timeout=self.timeout)
                if resp.status_code != 200:
                    raise InfraError(f"http {resp.status_code}: {resp.text[:200]}")
                data = resp.json()
                break
            except (requests.RequestException, InfraError, ValueError) as exc:
                last = exc
                if attempt < self.retries:
                    self.retried += 1
                    time.sleep(5 * (attempt + 1))
        else:
            raise InfraError(str(last)[:300])
        self.calls += 1
        usage = data.get("usage") or {}
        self.prompt_tokens += usage.get("prompt_tokens", 0)
        self.completion_tokens += usage.get("completion_tokens", 0)
        choice = data["choices"][0]
        msg = choice["message"]
        # Recent vLLM servers return the reasoning as `reasoning`; older ones used `reasoning_content`.
        return {"content": msg.get("content") or "", "reasoning": msg.get("reasoning_content") or msg.get("reasoning") or "",
                "finish_reason": choice.get("finish_reason")}

    def cost(self):
        return {"llm_calls": self.calls, "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens, "infra_retries": self.retried}


def parse_json(text):
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{"):]
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("no JSON object")
    return json.loads(text[start:end + 1])
