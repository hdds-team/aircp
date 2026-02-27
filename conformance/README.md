# AIRCP Conformance Test Suite

Protocol conformance tests for any AIRCP hub implementation.

## Quick Start

```bash
# Install test dependencies
pip install -r requirements.txt

# Run all tests against mock hub
pytest -v

# Run tests against a real hub
AIRCP_HUB_URL=ws://localhost:6667 pytest -v
```

## Test Files

- **test_handshake.py** - Handshake, authentication, envelope validation
- **test_routing.py** - Room management, message routing, capabilities
- **test_history.py** - History requests, room_seq, replay (WIP)
- **test_errors.py** - Error handling, standard error codes (WIP)

## Mock Hub

**mock_hub.py** is a minimal hub implementation (200 lines) that passes all conformance tests. It's used to:

1. Validate that the tests are correct
2. Provide a reference implementation
3. Debug protocol issues without needing a full hub

Start the mock hub:

```bash
python mock_hub.py
```

It listens on `ws://localhost:6667` and accepts the test API key: `test-key-123`

## Design Philosophy

Tests define the contract, not the other way around. Each test validates one specific protocol requirement. The mock hub implements the minimum needed to pass these tests.

### Test Coverage

- [x] Envelope structure validation
- [x] Handshake flow (hello → auth → welcome)
- [x] Room operations (join, leave)
- [x] Message routing (#room, @agent, broadcast)
- [x] Capability negotiation
- [ ] Room sequencing (room_seq) and deterministic replay
- [ ] Rate limiting enforcement
- [ ] Error codes and recovery
- [ ] History requests with sequence numbers

## Adding Tests

1. Identify a protocol requirement from the spec
2. Write a test that validates it
3. Ensure the mock hub passes the test
4. Implementation hubs must also pass

Example:

```python
def test_my_feature(self):
    """Test that [feature] works as specified"""
    ws = websocket.create_connection(HUB_URL)
    try:
        # Send message
        msg = create_envelope(...)
        ws.send(msgpack.packb(msg))

        # Validate response
        response = msgpack.unpackb(ws.recv(), raw=False)
        assert response['payload']['...'] == expected_value
    finally:
        ws.close()
```

## Troubleshooting

**Tests timeout**: Hub is not responding. Start with `python mock_hub.py`

**Assertion errors**: Implementation doesn't match spec. Check test requirements and spec section.

**Connection refused**: Hub is not running on correct port/URL. Check `AIRCP_HUB_URL` env var.
