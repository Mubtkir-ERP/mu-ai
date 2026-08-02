# -*- coding: utf-8 -*-
"""Unified LLM gateway for the Mubtkir platform.

Every model call goes through this module (via LiteLLM), which provides:
- provider routing (Anthropic / OpenAI / Gemini) from a single settings doc,
- prompt caching (stable system prefix first, volatile content last),
- an agentic function-calling loop with permission-safe tools,
- centralized token/cost metering into `AI Usage Log`.

Configuration lives in the `Mubtkir AI Settings` single DocType.
"""

import frappe

SETTINGS_DOCTYPE = "Mubtkir AI Settings"

RAG_CONTEXT_TEMPLATE = (
	"مقاطع مرجعية من قاعدة المعرفة — اعتمد عليها في إجابتك وإن لم تكفِ فقل ذلك:\n\n"
	"{context}\n\n---\nسؤال المستخدم: {question}"
)


# ------------------------------------------------------------------- settings


def get_settings():
	return frappe.get_cached_doc(SETTINGS_DOCTYPE)


def is_configured():
	"""True when both a model and an API key are set."""
	s = get_settings()
	return bool(s.get("model")) and bool(s.get_password("api_key", raise_exception=False))


def _litellm_model(provider, model):
	"""Return the model string LiteLLM expects.

	OpenAI models pass through unchanged; other providers are prefixed
	("anthropic/claude-...") unless the caller already prefixed them.
	"""
	provider = (provider or "").strip().lower()
	model = (model or "").strip()
	if provider in ("", "openai") or "/" in model:
		return model
	return f"{provider}/{model}"


# ------------------------------------------------------------------- metering


def log_usage(feature, model, usages, session_id=None, responses=None):
	"""Persist token usage and estimated cost into `AI Usage Log`.

	`usages` may contain several LiteLLM usage objects (tool-loop rounds);
	they are summed into one row. Metering failures never fail the call —
	they are only reported to the Error Log.
	"""
	try:
		prompt = completion = cached = 0
		for u in usages or []:
			if not u:
				continue
			prompt += getattr(u, "prompt_tokens", 0) or 0
			completion += getattr(u, "completion_tokens", 0) or 0
			details = getattr(u, "prompt_tokens_details", None)
			cached += (getattr(details, "cached_tokens", 0) or 0) if details else 0

		cost = 0.0
		try:
			import litellm

			for r in responses or []:
				cost += litellm.completion_cost(completion_response=r) or 0.0
		except Exception:
			cost = 0.0

		frappe.get_doc(
			{
				"doctype": "AI Usage Log",
				"user": frappe.session.user,
				"feature": feature,
				"model": model,
				"session_id": session_id,
				"prompt_tokens": prompt,
				"completion_tokens": completion,
				"cached_tokens": cached,
				"total_tokens": prompt + completion,
				"cost_usd": cost,
			}
		).insert(ignore_permissions=True)
	except Exception:
		frappe.log_error(title="Mubtkir usage log error", message=frappe.get_traceback())


# ------------------------------------------------------------- prompt assembly


def build_messages(user_text, system_prompt=None, enable_caching=False, provider="openai", history=None):
	"""Assemble the messages list with a cache-friendly ordering.

	The stable system prompt comes first (with an explicit Anthropic
	cache_control marker when caching is enabled); per-turn content last.
	`history` is a list of prior {"role", "content"} turns.
	"""
	messages = []
	if system_prompt:
		if enable_caching and (provider or "").lower() == "anthropic":
			messages.append(
				{
					"role": "system",
					"content": [
						{
							"type": "text",
							"text": system_prompt,
							"cache_control": {"type": "ephemeral"},
						}
					],
				}
			)
		else:
			messages.append({"role": "system", "content": system_prompt})

	for turn in history or []:
		role = turn.get("role")
		content = (turn.get("content") or "").strip()
		if role in ("user", "assistant") and content:
			messages.append({"role": role, "content": content})

	messages.append({"role": "user", "content": user_text})
	return messages


def _request_params(user_text, context=None, history=None):
	"""Resolve settings into the kwargs shared by all completion styles."""
	s = get_settings()
	if context:
		user_text = RAG_CONTEXT_TEMPLATE.format(context=context, question=user_text)

	messages = build_messages(
		user_text,
		system_prompt=s.get("system_prompt"),
		enable_caching=bool(s.get("enable_prompt_caching")),
		provider=s.provider,
		history=history,
	)
	return s, {
		"model": _litellm_model(s.provider, s.model),
		"messages": messages,
		"api_key": s.get_password("api_key"),
	}


# ----------------------------------------------------------------- completions


def complete(user_text, context=None, tools=None, history=None, max_tool_rounds=4, session_id=None):
	"""Synchronous completion with an optional function-calling loop.

	Returns ``(text, usage, used_tool_names)``. When the model requests
	tools they are executed through `ai_tools.execute_tool` (which enforces
	the current user's permissions) and results are fed back until the model
	produces a final answer or `max_tool_rounds` is reached.
	"""
	import litellm

	s, params = _request_params(user_text, context=context, history=history)

	used_tools = []
	usages = []
	responses = []
	messages = params["messages"]

	def _finish(text, usage):
		log_usage("chat", s.model, usages, session_id=session_id, responses=responses)
		return text, usage, used_tools

	for round_no in range(max_tool_rounds + 1):
		response = litellm.completion(
			model=params["model"],
			messages=messages,
			api_key=params["api_key"],
			stream=False,
			**({"tools": tools} if tools else {}),
		)
		usage = getattr(response, "usage", None)
		usages.append(usage)
		responses.append(response)

		msg = response.choices[0].message
		tool_calls = getattr(msg, "tool_calls", None) or []
		if not tool_calls or round_no == max_tool_rounds:
			return _finish(msg.content or "", usage)

		from mubtkir_ai import ai_tools

		messages.append(
			{
				"role": "assistant",
				"content": msg.content or "",
				"tool_calls": [
					{
						"id": tc.id,
						"type": "function",
						"function": {"name": tc.function.name, "arguments": tc.function.arguments},
					}
					for tc in tool_calls
				],
			}
		)
		for tc in tool_calls:
			used_tools.append(tc.function.name)
			messages.append(
				{
					"role": "tool",
					"tool_call_id": tc.id,
					"content": ai_tools.execute_tool(tc.function.name, tc.function.arguments),
				}
			)

	return _finish("", usages[-1] if usages else None)


def stream_complete(user_text, on_chunk, context=None, history=None):
	"""Streaming completion (no tools). Calls ``on_chunk(text_so_far)`` per
	delta and returns the full text. Kept for the realtime delivery path."""
	import litellm

	_s, params = _request_params(user_text, context=context, history=history)

	full = ""
	for part in litellm.completion(stream=True, **params):
		try:
			delta = part.choices[0].delta.content or ""
		except (AttributeError, IndexError):
			delta = ""
		if delta:
			full += delta
			on_chunk(full)
	return full
