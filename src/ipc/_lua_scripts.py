"""Lua scripts for atomic Redis IPC operations."""

# Atomic send: check recipient exists, deliver to inbox or DLQ
# KEYS: [1] ipc:agents:index, [2] ipc:inbox:{recipient}, [3] ipc:dlq,
#        [4] ipc:metrics:sent, [5] ipc:metrics:delivered, [6] ipc:metrics:dlq
# ARGV: [1] envelope JSON, [2] recipient agent_id, [3] inbox maxlen, [4] dlq maxlen
SEND_ATOMIC = """
local exists = redis.call('SISMEMBER', KEYS[1], ARGV[2])
redis.call('INCR', KEYS[4])
if exists == 1 then
    redis.call('XADD', KEYS[2], 'MAXLEN', '~', ARGV[3], '*', 'envelope', ARGV[1])
    redis.call('INCR', KEYS[5])
    return 1
else
    redis.call('XADD', KEYS[3], 'MAXLEN', '~', ARGV[4], '*',
               'envelope', ARGV[1], 'reason', 'recipient_not_found')
    redis.call('INCR', KEYS[6])
    return 0
end
"""

# Atomic RPC request: create response slot + send to target inbox + track pending
# KEYS: [1] ipc:rpc:resp:{corr_id}, [2] ipc:inbox:{target}, [3] ipc:rpc:pending
# ARGV: [1] envelope JSON, [2] TTL seconds, [3] deadline timestamp, [4] correlation_id,
#        [5] inbox maxlen
RPC_REQUEST_ATOMIC = """
redis.call('DEL', KEYS[1])
redis.call('RPUSH', KEYS[1], '__placeholder__')
redis.call('LPOP', KEYS[1])
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[2]))
redis.call('XADD', KEYS[2], 'MAXLEN', '~', ARGV[5], '*', 'envelope', ARGV[1])
redis.call('ZADD', KEYS[3], ARGV[3], ARGV[4])
return 1
"""

# Broadcast: send to all agent inboxes in the index
# KEYS: [1] ipc:agents:index, [2] ipc:metrics:sent, [3] ipc:metrics:delivered
# ARGV: [1] envelope JSON, [2] sender agent_id (skip self), [3] inbox maxlen
BROADCAST_ATOMIC = """
local agents = redis.call('SMEMBERS', KEYS[1])
local delivered = 0
for _, agent_id in ipairs(agents) do
    if agent_id ~= ARGV[2] then
        local inbox_key = 'ipc:inbox:' .. agent_id
        redis.call('XADD', inbox_key, 'MAXLEN', '~', ARGV[3], '*', 'envelope', ARGV[1])
        delivered = delivered + 1
    end
end
redis.call('INCRBY', KEYS[2], delivered)
redis.call('INCRBY', KEYS[3], delivered)
return delivered
"""

# Channel publish: send to all subscriber inboxes
# KEYS: [1] ipc:channel:subs:{name}, [2] ipc:channel:{name},
#        [3] ipc:metrics:sent, [4] ipc:metrics:delivered
# ARGV: [1] envelope JSON, [2] channel maxlen
CHANNEL_PUBLISH_ATOMIC = """
redis.call('XADD', KEYS[2], 'MAXLEN', '~', ARGV[2], '*', 'envelope', ARGV[1])
redis.call('INCR', KEYS[3])
redis.call('INCR', KEYS[4])
return 1
"""
