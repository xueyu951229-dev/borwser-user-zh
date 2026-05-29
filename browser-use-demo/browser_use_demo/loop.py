"""
Sampling loop for browser automation with Claude.
Supports both Anthropic native API and OpenAI-compatible proxies (e.g., PackyAPI).
"""

import json
import os
from collections.abc import Callable
from datetime import datetime
from enum import StrEnum
from typing import Any, Optional

import httpx
from openai import (
    AsyncOpenAI)

from anthropic import (
    AsyncAnthropic,
    AsyncAnthropicBedrock,
    AsyncAnthropicVertex,
)
from anthropic.types.beta import (
    BetaCacheControlEphemeralParam,
    BetaContentBlockParam,
    BetaMessageParam,
    BetaTextBlockParam,
)

from .message_handler import MessageBuilder, ProcessedResponse, ResponseProcessor
from .tools import BrowserTool, ToolCollection, ToolResult

PROMPT_CACHING_BETA_FLAG = "prompt-caching-2024-07-31"


class APIProvider(StrEnum):
    ANTHROPIC = "anthropic"
    BEDROCK = "bedrock"
    VERTEX = "vertex"


today = datetime.today()
date_str = f"{today:%A}, {today:%B} {today.day}, {today:%Y}"
# Browser-specific system prompt
BROWSER_SYSTEM_PROMPT = f"""<SYSTEM_CAPABILITY>
* You control a Chromium browser via Playwright automation.
* The current date is {date_str}.
</SYSTEM_CAPABILITY>

<TOOL_GUIDANCE>
You receive a screenshot at the start of each turn. Look at it to see the current page - if you're already where you need to be, don't re-navigate.

After navigating to a new page, always call read_page to get element references (ref_1, ref_2, etc.) before interacting with the page. Use these refs with your interaction tools (click, type, hover, form_input, etc.). Refs are more reliable than coordinates.

When you need to extract or read text content from a page, always use get_page_text - don't try to read text from screenshots.

If DOM-based actions (refs) aren't working, fall back to screenshot + coordinate-based actions.
</TOOL_GUIDANCE>

<TIPS>
* Prefer get_page_text over scrolling when looking for information - it's faster and more reliable
* Use execute_js to extract data from JavaScript variables, localStorage, or trigger behaviors not accessible through clicks
* Use full URLs with https://
* Use wait for slow-loading pages
* Use scroll_to with a ref to reveal elements
* Use form_input with refs for form fields
* Use key for shortcuts (e.g., "ctrl+a")
* Close popups when they appear
* Verify actions succeeded before moving on
</TIPS>"""


def _strip_images(messages: list[BetaMessageParam]) -> list[dict[str, Any]]:
    """Return a copy of messages with base64 image blocks removed.

    Keeps the original messages intact for DB/SSE; returns a lightweight
    copy suitable for API calls (prevents 403 due to oversized requests).

    Images may appear at two levels:
      - Top-level content blocks (e.g. user messages with screenshots)
      - Nested inside tool_result content arrays (browser tool responses)
    """
    def _without_images(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        clean: list[dict[str, Any]] = []
        for b in blocks:
            if isinstance(b, dict) and b.get("type") == "image":
                continue
            # Recurse into tool_result blocks which have their own content list
            if isinstance(b, dict) and b.get("type") == "tool_result" and isinstance(b.get("content"), list):
                b = {**b, "content": _without_images(b["content"])}
            clean.append(b)
        return clean

    stripped: list[dict[str, Any]] = []
    for msg in messages:
        msg_copy: dict[str, Any] = {"role": msg["role"]}
        content = msg.get("content", [])
        if isinstance(content, list):
            msg_copy["content"] = _without_images(content)
        else:
            msg_copy["content"] = content
        stripped.append(msg_copy)
    return stripped


def _anthropic_tools_to_openai(tools: list[dict]) -> list[dict]:
    """Convert Anthropic-format tool definitions to OpenAI format."""
    openai_tools = []
    for tool in tools:
        openai_tools.append({
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": tool.get("input_schema", {}),
            },
        })
    return openai_tools


def _anthropic_messages_to_openai(messages: list[BetaMessageParam]) -> list[dict]:
    """Convert Anthropic-format message history to OpenAI format."""
    openai_messages = []
    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", [])

        if role == "user":
            if isinstance(content, list) and len(content) > 0:
                # Check if these are tool_result blocks
                is_tool_result = all(
                    isinstance(block, dict) and block.get("type") == "tool_result"
                    for block in content
                )
                if is_tool_result:
                    for block in content:
                        tool_content_parts = []
                        for item in block.get("content", []):
                            if isinstance(item, dict) and item.get("type") == "text":
                                tool_content_parts.append(item.get("text", ""))
                        openai_messages.append({
                            "role": "tool",
                            "tool_call_id": block.get("tool_use_id", ""),
                            "content": "\n".join(tool_content_parts),
                        })
                    continue

            # Regular user message: extract text and images
            user_content: Any = ""
            text_parts = []
            image_parts = []
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        text_parts.append(block.get("text", ""))
                    elif block.get("type") == "image":
                        source = block.get("source", {})
                        image_parts.append({
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{source.get('media_type', 'image/png')};base64,{source.get('data', '')}"
                            },
                        })

            if image_parts:
                user_content = [{"type": "text", "text": "\n".join(text_parts)}] + image_parts
            else:
                user_content = "\n".join(text_parts)

            openai_messages.append({"role": "user", "content": user_content})

        elif role == "assistant":
            text_parts = []
            tool_calls = []
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        text_parts.append(block.get("text", ""))
                    elif block.get("type") == "tool_use":
                        tool_calls.append({
                            "id": block.get("id", ""),
                            "type": "function",
                            "function": {
                                "name": block.get("name", ""),
                                "arguments": json.dumps(block.get("input", {})),
                            },
                        })

            msg_dict: dict[str, Any] = {"role": "assistant"}
            if text_parts:
                msg_dict["content"] = "\n".join(text_parts)
            else:
                msg_dict["content"] = None
            if tool_calls:
                msg_dict["tool_calls"] = tool_calls
            openai_messages.append(msg_dict)

    return openai_messages


def _openai_response_to_processed(response) -> ProcessedResponse:
    """Convert an OpenAI-format chat completion response to a ProcessedResponse."""
    assistant_content: list[BetaContentBlockParam] = []
    tool_uses: list[dict[str, Any]] = []
    has_text = False
    has_tools = False

    choice = response.choices[0]
    message = choice.message

    if message.content:
        has_text = True
        assistant_content.append({
            "type": "text",
            "text": message.content,
        })

    if message.tool_calls:
        for tool_call in message.tool_calls:
            has_tools = True
            try:
                arguments = json.loads(tool_call.function.arguments)
            except (json.JSONDecodeError, TypeError):
                arguments = {}

            tool_use_dict: dict[str, Any] = {
                "type": "tool_use",
                "id": tool_call.id,
                "name": tool_call.function.name,
                "input": arguments,
            }
            assistant_content.append(tool_use_dict)
            tool_uses.append(tool_use_dict)

    return ProcessedResponse(
        assistant_content=assistant_content,
        tool_uses=tool_uses,
        has_text=has_text,
        has_tools=has_tools,
    )


async def sampling_loop(
    *,
    model: str,
    provider: APIProvider,
    system_prompt_suffix: str,
    messages: list[BetaMessageParam],
    output_callback: Callable[[BetaContentBlockParam], None],
    tool_output_callback: Callable[[ToolResult, str], None],
    api_response_callback: Callable[
        [httpx.Request | None, httpx.Response | object | None, Exception | None], None
    ],
    api_key: str,
    only_n_most_recent_images: int | None = None,
    max_tokens: int = 4096,
    browser_tool: Optional[BrowserTool] = None,
):
    """Sampling loop for browser automation."""
    if browser_tool is None:
        browser_tool = BrowserTool()

    tool_collection = ToolCollection(browser_tool)

    system_text = f"{BROWSER_SYSTEM_PROMPT}{' ' + system_prompt_suffix if system_prompt_suffix else ''}"
    system = BetaTextBlockParam(type="text", text=system_text)

    # Use Anthropic native API format (supports tool calling).
    # When ANTHROPIC_BASE_URL is set, route through the proxy while
    # keeping the native Anthropic protocol (the OpenAI-compatible
    # path does not reliably forward tool definitions to all proxies).
    base_url = os.getenv("ANTHROPIC_BASE_URL", None)

    while True:
        betas: list[str] = []
        enable_prompt_caching = False

        if provider == APIProvider.ANTHROPIC:
            client = AsyncAnthropic(
                api_key=api_key,
                base_url=base_url,  # route through proxy when set
                max_retries=4,
            )
            enable_prompt_caching = True
        elif provider == APIProvider.VERTEX:
            client = AsyncAnthropicVertex()
        elif provider == APIProvider.BEDROCK:
            client = AsyncAnthropicBedrock()
        else:
            raise ValueError(f"Unsupported provider: {provider}")

        if enable_prompt_caching:
            betas.append(PROMPT_CACHING_BETA_FLAG)
            system = BetaTextBlockParam(
                type="text",
                text=system["text"],
                cache_control=BetaCacheControlEphemeralParam(type="ephemeral"),
            )

        try:
            # Strip base64 images from message history before sending to API.
            # Images are kept in the original messages list for DB persistence
            # and SSE display, but must not be sent to the API (→ 403 on large payloads).
            api_messages = _strip_images(messages)
            import json as _json
            payload_size = len(_json.dumps(api_messages, default=str))
            print(f"[DEBUG] API request: {len(api_messages)} messages, payload ~{payload_size // 1024}KB", flush=True)

            api_kwargs = {
                "max_tokens": max_tokens,
                "messages": api_messages,
                "model": model,
                "system": [system],
                "tools": tool_collection.to_params(),
            }
            if betas:
                api_kwargs["betas"] = betas
                response = await client.beta.messages.create(**api_kwargs)
            else:
                response = await client.messages.create(**api_kwargs)
        except Exception as e:
            await api_response_callback(None, None, e)
            raise e

        await api_response_callback(None, response, None)

        processor = ResponseProcessor()
        processed = processor.process_response(response)

        # Output all content blocks to callbacks
        for content_block in processed.assistant_content:
            await output_callback(content_block)

        # Build and append the complete assistant message
        builder = MessageBuilder()
        builder.add_assistant_message(messages, processed.assistant_content)

        if processed.tool_uses:
            processor = ResponseProcessor()
            tool_results = await processor.execute_tools(
                processed.tool_uses,
                tool_collection,
                tool_output_callback,
            )
            builder.add_tool_results(messages, tool_results)
        else:
            return messages


def _maybe_filter_to_n_most_recent_images(
    messages: list[BetaMessageParam],
    images_to_keep: int,
    min_removal_threshold: int = 10,
):
    """Filter messages to keep only the N most recent images."""
    if images_to_keep <= 0:
        raise ValueError("images_to_keep must be > 0")

    total_images = sum(
        1
        for message in messages
        if message["role"] == "user"
        for block in message.get("content", [])
        if isinstance(block, dict) and block.get("type") == "image"
    )

    images_to_remove = total_images - images_to_keep
    if images_to_remove < min_removal_threshold:
        return

    images_removed = 0
    for message in messages:
        if message["role"] == "user" and isinstance(message.get("content"), list):
            new_content = []
            for block in message["content"]:
                if isinstance(block, dict) and block.get("type") == "image":
                    if images_removed < images_to_remove:
                        images_removed += 1
                        continue
                new_content.append(block)
            message["content"] = new_content
