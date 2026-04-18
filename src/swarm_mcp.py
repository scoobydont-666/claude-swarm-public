"""
Swarm MCP Server — Expose swarm operations as MCP tools.

Any Claude Code instance with this MCP server configured can:
- Create and manage tasks
- Dispatch work to fleet members
- Check fleet and GPU status
- Run pipelines

Runs as a stdio MCP server (for .mcp.json command-based config).

Usage in .mcp.json:
  "swarm": {
    "command": "python3",
    "args": ["/opt/claude-swarm/src/swarm_mcp.py"]
  }
"""

import json
import logging
import sys
from pathlib import Path

# Add swarm source to path
sys.path.insert(0, str(Path(__file__).parent))

logger = logging.getLogger(__name__)

# ── MCP Protocol Helpers ──────────────────────────────────────────────────────


def send_response(id, result=None, error=None):
    """Send a JSON-RPC response to stdout."""
    resp = {"jsonrpc": "2.0", "id": id}
    if error:
        resp["error"] = error
    else:
        resp["result"] = result
    sys.stdout.write(json.dumps(resp) + "\n")
    sys.stdout.flush()


# ── Tool Definitions ──────────────────────────────────────────────────────────

TOOLS = [
    {
        "name": "swarm_task_create",
        "description": "Create a new task in the swarm queue.",
        "inputSchema": {
            "type": "object",
            "required": ["title"],
            "properties": {
                "title": {"type": "string"},
                "description": {"type": "string"},
                "priority": {"type": "number", "default": 3},
                "project": {"type": "string"},
                "requires": {"type": "array", "items": {"type": "string"}},
            },
        },
    },
    {
        "name": "swarm_task_list",
        "description": "List tasks in the swarm queue.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "state": {"type": "string", "enum": ["pending", "claimed", "completed", "all"]},
                "limit": {"type": "number", "default": 20},
            },
        },
    },
    {
        "name": "swarm_dispatch",
        "description": "Dispatch a task to a fleet member.",
        "inputSchema": {
            "type": "object",
            "required": ["task"],
            "properties": {
                "task": {"type": "string", "description": "Task description/prompt"},
                "host": {"type": "string"},
                "model": {"type": "string"},
                "project_dir": {"type": "string"},
            },
        },
    },
    {
        "name": "swarm_status",
        "description": "Get fleet status — all nodes with heartbeat, capabilities, GPU info.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "swarm_gpu_status",
        "description": "Get GPU allocation status across the fleet.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "swarm_pipeline_run",
        "description": "Run a named pipeline (gen_verify_loop, test_fix_loop, hierarchical, etc.)",
        "inputSchema": {
            "type": "object",
            "required": ["pipeline", "input"],
            "properties": {
                "pipeline": {"type": "string", "description": "Pipeline name"},
                "input": {"type": "string", "description": "Input/goal for the pipeline"},
                "project_dir": {"type": "string"},
            },
        },
    },
    {
        "name": "swarm_route_model",
        "description": "Classify a task and get recommended model tier.",
        "inputSchema": {
            "type": "object",
            "required": ["task_description"],
            "properties": {
                "task_description": {"type": "string"},
            },
        },
    },
]


# ── Tool Implementations ──────────────────────────────────────────────────────


def handle_tool_call(name, arguments):
    """Handle a tool call and return the result."""
    try:
        if name == "swarm_task_create":
            return _task_create(arguments)
        elif name == "swarm_task_list":
            return _task_list(arguments)
        elif name == "swarm_dispatch":
            return _dispatch(arguments)
        elif name == "swarm_status":
            return _status()
        elif name == "swarm_gpu_status":
            return _gpu_status()
        elif name == "swarm_pipeline_run":
            return _pipeline_run(arguments)
        elif name == "swarm_route_model":
            return _route_model(arguments)
        else:
            return {
                "content": [
                    {"type": "text", "text": json.dumps({"error": f"Unknown tool: {name}"})}
                ]
            }
    except Exception as e:
        return {
            "content": [{"type": "text", "text": json.dumps({"error": str(e)})}],
            "isError": True,
        }


def _text_result(data):
    return {"content": [{"type": "text", "text": json.dumps(data, default=str)}]}


def _task_create(args):
    try:
        from backend import lib as swarm

        task_id = swarm.create_task(
            title=args.get("title", ""),
            description=args.get("description", ""),
            priority=args.get("priority", 3),
            project=args.get("project", ""),
            requires=args.get("requires", []),
        )
        return _text_result({"created": True, "task_id": task_id})
    except Exception as e:
        return _text_result({"error": f"Task creation failed: {e}"})


def _task_list(args):
    try:
        from backend import lib as swarm

        state = args.get("state", "pending")
        limit = args.get("limit", 20)
        tasks = swarm.list_tasks(state=state if state != "all" else None)
        return _text_result({"tasks": tasks[:limit], "total": len(tasks)})
    except Exception as e:
        return _text_result({"error": str(e)})


def _dispatch(args):
    try:
        from hydra_dispatch import dispatch

        result = dispatch(
            host=args.get("host", "node_reserve1"),
            task=args["task"],
            model=args.get("model"),
            project_dir=args.get("project_dir"),
        )
        return _text_result(
            {
                "dispatch_id": result.dispatch_id,
                "host": result.host,
                "model": result.model,
                "status": result.status,
            }
        )
    except Exception as e:
        return _text_result({"error": str(e)})


def _status():
    try:
        from backend import lib as swarm

        nodes = swarm.get_all_statuses()
        return _text_result({"nodes": nodes})
    except Exception as e:
        return _text_result({"error": str(e)})


def _gpu_status():
    try:
        from gpu_scheduler_v2 import GpuScheduler

        scheduler = GpuScheduler()
        return _text_result(scheduler.get_status())
    except Exception as e:
        return _text_result({"error": str(e)})


def _pipeline_run(args):
    try:
        pipeline_name = args["pipeline"]
        input_text = args["input"]
        project_dir = args.get("project_dir", "<hydra-project-path>")

        # Dynamic pipeline import
        import importlib

        mod = importlib.import_module(f"pipelines.{pipeline_name}")
        pipeline = mod.create(project_dir=project_dir)

        from pipeline import PipelineExecutor

        executor = PipelineExecutor()
        result = executor.execute(pipeline, {"input": input_text, "project_dir": project_dir})

        return _text_result(
            {
                "pipeline": pipeline_name,
                "pipeline_id": result.pipeline_id,
                "status": result.status,
                "stages_completed": len(result.stage_results),
            }
        )
    except Exception as e:
        return _text_result({"error": str(e)})


def _route_model(args):
    try:
        from model_router import route_task

        decision = route_task(args["task_description"])
        return _text_result(
            {
                "rule": decision.rule_name,
                "tier": decision.tier,
                "model": decision.model,
                "fallback": decision.fallback_model,
                "reason": decision.reason,
            }
        )
    except Exception as e:
        return _text_result({"error": str(e)})


# ── MCP Server Main Loop ─────────────────────────────────────────────────────


def main():
    """stdio MCP server main loop."""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue

        method = request.get("method", "")
        req_id = request.get("id")
        params = request.get("params", {})

        if method == "initialize":
            send_response(
                req_id,
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "swarm-mcp", "version": "3.0.0"},
                },
            )
        elif method == "initialized":
            pass  # notification, no response
        elif method == "tools/list":
            send_response(req_id, {"tools": TOOLS})
        elif method == "tools/call":
            tool_name = params.get("name", "")
            arguments = params.get("arguments", {})
            result = handle_tool_call(tool_name, arguments)
            send_response(req_id, result)
        elif method == "ping":
            send_response(req_id, {})
        else:
            send_response(req_id, error={"code": -32601, "message": f"Unknown method: {method}"})


if __name__ == "__main__":
    main()
