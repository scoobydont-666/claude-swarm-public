"""Living Specification — Shared context via Context Bridge MCP.

Replaces NFS-based collaborative context files with Context Bridge v2
MCP tools for bidirectional spec documents. Agents read via cb_peek/cb_search,
architect stages update via cb_ingest.

Pattern from Augment Code Intent's bidirectional living specs.
"""

import json
import logging
import os
import subprocess

logger = logging.getLogger(__name__)

CB_MCP_URL = "http://127.0.0.1:8520/mcp"

_CB_TOKEN_PATH = os.environ.get("CB_TOKEN_FILE", "/opt/ai-shared/secrets/cb-mcp-token")
try:
    with open(_CB_TOKEN_PATH) as _tf:
        _CB_TOKEN = _tf.read().strip()
except OSError:
    _CB_TOKEN = ""


def _mcp_call(tool_name: str, arguments: dict) -> dict | None:
    """Call a Context Bridge MCP tool via HTTP."""
    payload = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": arguments,
            },
        }
    )
    try:
        curl_cmd = [
            "curl",
            "-sf",
            "-X",
            "POST",
            CB_MCP_URL,
            "-H",
            "Content-Type: application/json",
        ]
        if _CB_TOKEN:
            curl_cmd.extend(["-H", f"Authorization: Bearer {_CB_TOKEN}"])
        curl_cmd.extend(["-d", payload])
        result = subprocess.run(
            curl_cmd,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0 and result.stdout:
            resp = json.loads(result.stdout)
            content = resp.get("result", {}).get("content", [])
            if content:
                return json.loads(content[0].get("text", "{}"))
        return None
    except Exception as e:
        logger.warning(f"CB MCP call failed ({tool_name}): {e}")
        return None


def create_spec(pipeline_id: str, spec_content: str, title: str = "") -> bool:
    """Create or update a living specification in Context Bridge.

    Args:
        pipeline_id: Pipeline identifier (used as alias)
        spec_content: Markdown specification content
        title: Optional title for the spec

    Returns:
        True if successfully stored
    """
    alias = f"spec:{pipeline_id}"
    result = _mcp_call(
        "cb_ingest",
        {
            "source_type": "raw",
            "content": spec_content,
            "alias": alias,
            "content_type": "markdown",
            "namespace": "pipeline_specs",
        },
    )
    if result and result.get("status") in ("cached", "deduped"):
        logger.info(f"Living spec created: {alias} ({result.get('chunk_count', 0)} chunks)")
        return True
    return False


def read_spec(pipeline_id: str) -> str | None:
    """Read a living specification from Context Bridge.

    Args:
        pipeline_id: Pipeline identifier

    Returns:
        Spec content or None if not found
    """
    alias = f"spec:{pipeline_id}"
    result = _mcp_call(
        "cb_peek",
        {
            "alias": alias,
            "namespace": "pipeline_specs",
        },
    )
    if result and result.get("results"):
        # Concatenate all chunks
        return "\n".join(chunk.get("content", "") for chunk in result["results"])
    return None


def search_spec(pipeline_id: str, query: str) -> list[dict]:
    """Search within a living specification.

    Args:
        pipeline_id: Pipeline identifier
        query: Search query

    Returns:
        List of matching chunks
    """
    alias = f"spec:{pipeline_id}"
    result = _mcp_call(
        "cb_search",
        {
            "query": query,
            "alias": alias,
            "namespace": "pipeline_specs",
            "include_chunk_content": True,
        },
    )
    if result:
        return result.get("results", result.get("grouped_results", []))
    return []


def update_spec(pipeline_id: str, new_content: str) -> bool:
    """Update a living specification (re-ingest with same alias)."""
    return create_spec(pipeline_id, new_content)


def delete_spec(pipeline_id: str) -> bool:
    """Delete a living specification."""
    alias = f"spec:{pipeline_id}"
    result = _mcp_call(
        "cb_manage",
        {
            "action": "evict_alias",
            "alias": alias,
        },
    )
    return result is not None and result.get("evicted", False)
