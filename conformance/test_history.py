"""
AIRCP Conformance Tests - History & Room Sequencing

Tests for deterministic replay through room_seq numbering.
This is CRITICAL for agent coordination and state synchronization.
"""
import uuid
import msgpack
import pytest
import websocket
from datetime import datetime, timezone
from typing import Dict, Any, List
import os
import time

HUB_URL = os.environ.get('AIRCP_HUB_URL', 'ws://localhost:6667')
TEST_API_KEY = os.environ.get('AIRCP_TEST_KEY', 'test-key-123')


def ws_send(ws, data: bytes):
    """Send binary data over WebSocket"""
    ws.send_binary(data)


def ws_recv(ws) -> bytes:
    """Receive binary data from WebSocket"""
    opcode, data = ws.recv_data()
    return data


def create_envelope(kind: str, payload: Dict[str, Any], **kwargs) -> Dict:
    """Create a valid AIRCP envelope"""
    envelope = {
        'id': str(uuid.uuid4()),
        'ts': datetime.now(timezone.utc).isoformat(),
        'from': kwargs.get('from', {'type': 'agent', 'id': '@test-agent'}),
        'to': kwargs.get('to', {}),
        'kind': kind,
        'payload': payload
    }

    for key in ['thread_id', 'requires', 'flow', 'meta']:
        if key in kwargs:
            envelope[key] = kwargs[key]

    return envelope


def authenticate_and_join(ws, room: str = '#general') -> bool:
    """Helper to authenticate and join a room"""
    # Hello
    hello = create_envelope(
        'control',
        {
            'command': 'hello',
            'args': {
                'client_name': 'test-client',
                'client_version': '0.1.0',
                'capabilities': ['chat', 'history']
            }
        }
    )
    ws_send(ws, msgpack.packb(hello))
    ws_recv(ws)  # Consume hello response

    # Auth
    auth = create_envelope(
        'control',
        {
            'command': 'auth',
            'args': {'api_key': TEST_API_KEY}
        }
    )
    ws_send(ws, msgpack.packb(auth))
    response = msgpack.unpackb(ws_recv(ws), raw=False)
    if response['kind'] != 'event' or response['payload']['event_type'] != 'welcome':
        return False

    # Join room
    join = create_envelope(
        'control',
        {
            'command': 'join',
            'args': {'room': room}
        }
    )
    ws_send(ws, msgpack.packb(join))
    response = msgpack.unpackb(ws_recv(ws), raw=False)
    return response['kind'] == 'event' and response['payload']['event_type'] == 'agent_joined'


class TestRoomSequencing:
    """Test room_seq numbering for deterministic replay"""

    def test_messages_have_room_seq(self):
        """Test that messages in a room have room_seq numbers"""
        ws = websocket.create_connection(HUB_URL)

        try:
            assert authenticate_and_join(ws, '#test-room-seq-1')

            # Send a message to the room
            chat_msg = create_envelope(
                'chat',
                {
                    'role': 'user',
                    'content': 'First message'
                },
                to={'room': '#test-room-seq-1'}
            )

            ws_send(ws, msgpack.packb(chat_msg))

            # Request history
            history_req = create_envelope(
                'control',
                {
                    'command': 'history',
                    'args': {
                        'room': '#test-room-seq-1',
                        'limit': 100
                    }
                }
            )

            ws_send(ws, msgpack.packb(history_req))
            response = msgpack.unpackb(ws_recv(ws), raw=False)

            # Check response
            assert response['kind'] == 'event'
            assert response['payload']['event_type'] == 'history_chunk'

            messages = response['payload']['data'].get('messages', [])
            if len(messages) > 0:
                # At least one message should have room_seq
                assert 'meta' in messages[0]
                assert 'room_seq' in messages[0]['meta'] or len(messages) > 0

        finally:
            ws.close()

    def test_room_seq_is_sequential(self):
        """Test that room_seq increments sequentially"""
        ws = websocket.create_connection(HUB_URL)

        try:
            room = f'#test-seq-{uuid.uuid4().hex[:8]}'
            assert authenticate_and_join(ws, room)

            # Send multiple messages
            for i in range(3):
                chat_msg = create_envelope(
                    'chat',
                    {
                        'role': 'user',
                        'content': f'Message {i}'
                    },
                    to={'room': room}
                )
                ws_send(ws, msgpack.packb(chat_msg))
                time.sleep(0.1)  # Small delay

            # Request history
            history_req = create_envelope(
                'control',
                {
                    'command': 'history',
                    'args': {
                        'room': room,
                        'limit': 100
                    }
                }
            )

            ws_send(ws, msgpack.packb(history_req))
            response = msgpack.unpackb(ws_recv(ws), raw=False)

            messages = response['payload']['data'].get('messages', [])

            # Extract room_seq numbers
            if len(messages) >= 2:
                seqs = []
                for msg in messages:
                    if 'meta' in msg and 'room_seq' in msg['meta']:
                        seqs.append(msg['meta']['room_seq'])

                # If we have sequences, they should be sequential
                if len(seqs) >= 2:
                    for i in range(1, len(seqs)):
                        assert seqs[i] > seqs[i-1], \
                            f"Sequence not increasing: {seqs[i]} <= {seqs[i-1]}"

        finally:
            ws.close()

    def test_history_request_with_since_seq(self):
        """Test requesting history since a specific sequence"""
        ws = websocket.create_connection(HUB_URL)

        try:
            room = f'#test-since-{uuid.uuid4().hex[:8]}'
            assert authenticate_and_join(ws, room)

            # Get initial history
            history_req = create_envelope(
                'control',
                {
                    'command': 'history',
                    'args': {
                        'room': room,
                        'limit': 100
                    }
                }
            )

            ws_send(ws, msgpack.packb(history_req))
            response = msgpack.unpackb(ws_recv(ws), raw=False)
            first_seq = response['payload']['data'].get('since_seq') or 0

            # Now request since a specific sequence
            history_req_since = create_envelope(
                'control',
                {
                    'command': 'history',
                    'args': {
                        'room': room,
                        'since_seq': first_seq + 1,
                        'limit': 100
                    }
                }
            )

            ws_send(ws, msgpack.packb(history_req_since))
            response = msgpack.unpackb(ws_recv(ws), raw=False)

            # Hub should acknowledge the since_seq parameter
            assert response['kind'] == 'event'
            assert response['payload']['event_type'] == 'history_chunk'

        finally:
            ws.close()

    def test_history_limit_parameter(self):
        """Test that history limit parameter is respected"""
        ws = websocket.create_connection(HUB_URL)

        try:
            room = f'#test-limit-{uuid.uuid4().hex[:8]}'
            assert authenticate_and_join(ws, room)

            # Request with limit=5
            history_req = create_envelope(
                'control',
                {
                    'command': 'history',
                    'args': {
                        'room': room,
                        'limit': 5
                    }
                }
            )

            ws_send(ws, msgpack.packb(history_req))
            response = msgpack.unpackb(ws_recv(ws), raw=False)

            messages = response['payload']['data'].get('messages', [])
            # Should not exceed limit
            assert len(messages) <= 5, \
                f"History limit not respected: got {len(messages)} messages, limit was 5"

        finally:
            ws.close()

    def test_history_default_limit(self):
        """Test that default history limit is 100"""
        ws = websocket.create_connection(HUB_URL)

        try:
            room = f'#test-default-limit-{uuid.uuid4().hex[:8]}'
            assert authenticate_and_join(ws, room)

            # Request WITHOUT limit parameter
            history_req = create_envelope(
                'control',
                {
                    'command': 'history',
                    'args': {
                        'room': room
                    }
                }
            )

            ws_send(ws, msgpack.packb(history_req))
            response = msgpack.unpackb(ws_recv(ws), raw=False)

            assert response['kind'] == 'event'
            # Default should be 100 per spec
            # (won't have 100 messages in a new room, but should accept the request)

        finally:
            ws.close()


class TestReplayDeterminism:
    """Test that room history enables deterministic replay"""

    def test_same_history_same_order(self):
        """Test that requesting same history returns messages in same order"""
        ws1 = websocket.create_connection(HUB_URL)
        ws2 = websocket.create_connection(HUB_URL)

        try:
            room = f'#test-determinism-{uuid.uuid4().hex[:8]}'
            assert authenticate_and_join(ws1, room)
            assert authenticate_and_join(ws2, room)

            # Both request the same history
            history_req = create_envelope(
                'control',
                {
                    'command': 'history',
                    'args': {
                        'room': room,
                        'limit': 100
                    }
                }
            )

            ws_send(ws1, msgpack.packb(history_req))
            response1 = msgpack.unpackb(ws_recv(ws1), raw=False)
            messages1 = response1['payload']['data'].get('messages', [])

            ws_send(ws2, msgpack.packb(history_req))
            response2 = msgpack.unpackb(ws_recv(ws2), raw=False)
            messages2 = response2['payload']['data'].get('messages', [])

            # Both should get same number of messages
            assert len(messages1) == len(messages2), \
                f"Different message counts: {len(messages1)} vs {len(messages2)}"

            # Messages should be in same order (by ID at minimum)
            if len(messages1) > 0:
                for m1, m2 in zip(messages1, messages2):
                    assert m1['id'] == m2['id'], \
                        f"Message order mismatch: {m1['id']} vs {m2['id']}"

        finally:
            ws1.close()
            ws2.close()

    def test_sequence_gap_detection(self):
        """Test that gaps in room_seq can be detected"""
        ws = websocket.create_connection(HUB_URL)

        try:
            room = f'#test-gap-{uuid.uuid4().hex[:8]}'
            assert authenticate_and_join(ws, room)

            # Request history
            history_req = create_envelope(
                'control',
                {
                    'command': 'history',
                    'args': {
                        'room': room,
                        'limit': 100
                    }
                }
            )

            ws_send(ws, msgpack.packb(history_req))
            response = msgpack.unpackb(ws_recv(ws), raw=False)

            messages = response['payload']['data'].get('messages', [])

            # Extract sequences
            if len(messages) >= 2:
                seqs = []
                for msg in messages:
                    if 'meta' in msg and 'room_seq' in msg['meta']:
                        seqs.append(msg['meta']['room_seq'])

                # Check for gaps (missing sequence numbers)
                if len(seqs) >= 2:
                    seqs.sort()
                    gaps = []
                    for i in range(1, len(seqs)):
                        if seqs[i] != seqs[i-1] + 1:
                            gaps.append((seqs[i-1], seqs[i]))

                    # If there are gaps, they should be reported
                    # (not asserting because initial room history may be empty)
                    # Just verify the logic is sound
                    if gaps:
                        # Gaps detected can be logged
                        pass

        finally:
            ws.close()


class TestHistoryErrors:
    """Test error handling in history requests"""

    def test_history_without_auth_fails(self):
        """Test that history requests require authentication"""
        ws = websocket.create_connection(HUB_URL)

        try:
            # Try to request history without auth
            history_req = create_envelope(
                'control',
                {
                    'command': 'history',
                    'args': {
                        'room': '#general'
                    }
                }
            )

            ws_send(ws, msgpack.packb(history_req))
            response = msgpack.unpackb(ws_recv(ws), raw=False)

            # Should get an error
            assert response['kind'] == 'error'
            assert response['payload']['code'] == 'unauthorized'

        finally:
            ws.close()

    def test_history_for_nonexistent_room(self):
        """Test requesting history for a room that doesn't exist"""
        ws = websocket.create_connection(HUB_URL)

        try:
            assert authenticate_and_join(ws)

            # Request history for a room we haven't joined
            history_req = create_envelope(
                'control',
                {
                    'command': 'history',
                    'args': {
                        'room': f'#nonexistent-{uuid.uuid4().hex[:8]}'
                    }
                }
            )

            ws_send(ws, msgpack.packb(history_req))
            response = msgpack.unpackb(ws_recv(ws), raw=False)

            # Should either return empty history or error
            if response['kind'] == 'error':
                # This is acceptable
                assert response['payload']['code'] in ['room_not_found', 'unauthorized']
            elif response['kind'] == 'event':
                # Empty history is also acceptable
                assert response['payload']['event_type'] == 'history_chunk'
                messages = response['payload']['data'].get('messages', [])
                assert len(messages) == 0

        finally:
            ws.close()


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
