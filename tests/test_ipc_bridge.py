"""Tests for IPC bridge module."""

from unittest.mock import patch

from src.ipc_bridge import (
    DISPATCH_EVENTS,
    GPU_EVENTS,
    INFRA_EVENTS,
    ROUTING_EVENTS,
    TASK_EVENTS,
    emit_dispatch_started,
    emit_gpu_allocated,
    emit_node_health,
    emit_routing_decision,
    emit_task_completed,
    emit_task_created,
    is_available,
    publish,
)


class TestAvailability:
    def test_unavailable_when_no_redis(self):
        with patch("src.ipc_bridge.get_client") as mock:
            mock.return_value.ping.side_effect = Exception("Connection refused")
            assert is_available() is False

    def test_available_when_redis_up(self):
        with patch("src.ipc_bridge.get_client") as mock:
            mock.return_value.ping.return_value = True
            assert is_available() is True


class TestPublish:
    @patch("src.ipc_bridge.get_client")
    def test_publish_returns_id(self, mock_client):
        mock_client.return_value.xadd.return_value = "1234567890-0"
        result = publish(TASK_EVENTS, "task.created", {"task_id": "t-1"})
        assert result is not None

    @patch("src.ipc_bridge.get_client")
    def test_publish_returns_none_on_failure(self, mock_client):
        mock_client.return_value.xadd.side_effect = Exception("Redis down")
        result = publish(TASK_EVENTS, "task.created", {"task_id": "t-1"})
        assert result is None


class TestConvenienceEmitters:
    @patch("src.ipc_bridge.publish")
    def test_emit_task_created(self, mock_pub):
        emit_task_created("t-1", "Test task", priority=2)
        mock_pub.assert_called_once()
        args = mock_pub.call_args
        assert args[0][0] == TASK_EVENTS
        assert args[0][1] == "task.created"
        assert args[0][2]["task_id"] == "t-1"

    @patch("src.ipc_bridge.publish")
    def test_emit_task_completed(self, mock_pub):
        emit_task_completed("t-1", result="success", cost_usd=0.05)
        args = mock_pub.call_args
        assert args[0][1] == "task.completed"
        assert args[0][2]["cost_usd"] == 0.05

    @patch("src.ipc_bridge.publish")
    def test_emit_gpu_allocated(self, mock_pub):
        emit_gpu_allocated("node_reserve1", 0, "t-1", model="qwen3:14b", vram_mb=10000)
        args = mock_pub.call_args
        assert args[0][0] == GPU_EVENTS
        assert args[0][2]["host"] == "node_reserve1"

    @patch("src.ipc_bridge.publish")
    def test_emit_dispatch_started(self, mock_pub):
        emit_dispatch_started("d-1", "node_reserve1", "sonnet", "Fix the bug")
        args = mock_pub.call_args
        assert args[0][0] == DISPATCH_EVENTS
        assert args[0][2]["dispatch_id"] == "d-1"

    @patch("src.ipc_bridge.publish")
    def test_emit_node_health(self, mock_pub):
        emit_node_health("node_reserve1", "online", gpu_count=2)
        args = mock_pub.call_args
        assert args[0][0] == INFRA_EVENTS

    @patch("src.ipc_bridge.publish")
    def test_emit_routing_decision(self, mock_pub):
        emit_routing_decision("Fix auth bug", "sonnet", "claude-sonnet-4-6", "debug")
        args = mock_pub.call_args
        assert args[0][0] == ROUTING_EVENTS
        assert args[0][2]["tier"] == "sonnet"


class TestChannelNames:
    def test_all_channels_defined(self):
        assert TASK_EVENTS == "task-events"
        assert GPU_EVENTS == "gpu-events"
        assert DISPATCH_EVENTS == "dispatch-events"
        assert INFRA_EVENTS == "infra-events"
        assert ROUTING_EVENTS == "routing-events"
