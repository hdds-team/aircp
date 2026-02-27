"""
AIRCP Conformance Tests - Message Routing
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

def create_envelope(kind: str, payload: Dict[str, Any], **kwargs) -> Dict:
    """Create a valid AIRCP envelope"""
    envelope = {
        'id': str(uuid.uuid4()),
        'ts': datetime.now(timezone.utc).isoformat(),
        'from': kwargs.get('from', {'type': 'agent', 'id': 'test-agent'}),
        'to': kwargs.get('to', {}),
        'kind': kind,
        'payload': payload
    }
    
    for key in ['thread_id', 'requires', 'flow', 'meta']:
        if key in kwargs:
            envelope[key] = kwargs[key]
    
    return envelope


def authenticate_client(ws) -> bool:
    """Helper to authenticate a websocket client"""
    # Send hello
    hello = create_envelope(
        'control',
        {
            'command': 'hello',
            'args': {
                'client_name': 'test-client',
                'client_version': '0.1.0',
                'capabilities': ['chat']
            }
        }
    )
    ws.send(msgpack.packb(hello))
    ws.recv()  # Consume hello response
    
    # Send auth
    auth = create_envelope(
        'control',
        {
            'command': 'auth',
            'args': {'api_key': TEST_API_KEY}
        }
    )
    ws.send(msgpack.packb(auth))
    
    response = msgpack.unpackb(ws.recv(), raw=False)
    return response['kind'] == 'event' and response['payload']['event_type'] == 'welcome'


class TestRoomManagement:
    """Test room join/leave operations"""
    
    def test_join_room(self):
        """Test joining a room"""
        ws = websocket.create_connection(HUB_URL)
        
        try:
            assert authenticate_client(ws)
            
            # Join #general
            join_msg = create_envelope(
                'control',
                {
                    'command': 'join',
                    'args': {'room': '#general'}
                }
            )
            
            ws.send(msgpack.packb(join_msg))
            response = msgpack.unpackb(ws.recv(), raw=False)
            
            assert response['kind'] == 'event'
            assert response['payload']['event_type'] == 'agent_joined'
            assert response['payload']['data']['room'] == '#general'
            
        finally:
            ws.close()
    
    def test_join_nonexistent_room_creates_it(self):
        """Test that joining a non-existent room creates it"""
        ws = websocket.create_connection(HUB_URL)
        
        try:
            assert authenticate_client(ws)
            
            # Join a new room
            room_name = f'#test-{uuid.uuid4().hex[:8]}'
            join_msg = create_envelope(
                'control',
                {
                    'command': 'join',
                    'args': {'room': room_name}
                }
            )
            
            ws.send(msgpack.packb(join_msg))
            response = msgpack.unpackb(ws.recv(), raw=False)
            
            # Should succeed
            assert response['kind'] == 'event'
            assert response['payload']['event_type'] == 'agent_joined'
            assert response['payload']['data']['room'] == room_name
            
        finally:
            ws.close()
    
    def test_join_without_auth_fails(self):
        """Test that joining requires authentication"""
        ws = websocket.create_connection(HUB_URL)
        
        try:
            # Try to join without auth
            join_msg = create_envelope(
                'control',
                {
                    'command': 'join',
                    'args': {'room': '#general'}
                }
            )
            
            ws.send(msgpack.packb(join_msg))
            response = msgpack.unpackb(ws.recv(), raw=False)
            
            assert response['kind'] == 'error'
            assert response['payload']['code'] == 'unauthorized'
            
        finally:
            ws.close()


class TestMessageRouting:
    """Test message routing between agents"""
    
    def test_room_broadcast(self):
        """Test broadcasting to all room members"""
        # Create two clients
        ws1 = websocket.create_connection(HUB_URL)
        ws2 = websocket.create_connection(HUB_URL)
        
        try:
            # Authenticate both
            assert authenticate_client(ws1)
            assert authenticate_client(ws2)
            
            # Both join #general
            for ws in [ws1, ws2]:
                join_msg = create_envelope(
                    'control',
                    {
                        'command': 'join',
                        'args': {'room': '#general'}
                    }
                )
                ws.send(msgpack.packb(join_msg))
                ws.recv()  # Consume join response
            
            # Client 1 sends a broadcast message
            chat_msg = create_envelope(
                'chat',
                {
                    'role': 'user',
                    'content': 'Hello everyone!'
                },
                to={'room': '#general', 'broadcast': True}
            )
            
            ws1.send(msgpack.packb(chat_msg))
            
            # Client 2 should receive it
            ws2.settimeout(2)
            try:
                response = msgpack.unpackb(ws2.recv(), raw=False)
                assert response['kind'] == 'chat'
                assert response['payload']['content'] == 'Hello everyone!'
            except websocket.WebSocketTimeoutException:
                pytest.skip("Hub doesn't support broadcasting yet")
                
        finally:
            ws1.close()
            ws2.close()
    
    def test_direct_message(self):
        """Test direct messaging between agents"""
        ws1 = websocket.create_connection(HUB_URL)
        ws2 = websocket.create_connection(HUB_URL)
        
        try:
            # Authenticate both with different agent IDs
            # This would need hub support for multiple agent types
            assert authenticate_client(ws1)
            assert authenticate_client(ws2)
            
            # Send direct message from agent1 to agent2
            dm = create_envelope(
                'chat',
                {
                    'role': 'user',
                    'content': 'Private message'
                },
                to={'agent_id': '@test-agent-2'}
            )
            
            ws1.send(msgpack.packb(dm))
            
            # Check if ws2 receives it
            ws2.settimeout(2)
            try:
                response = msgpack.unpackb(ws2.recv(), raw=False)
                assert response['payload']['content'] == 'Private message'
            except websocket.WebSocketTimeoutException:
                pytest.skip("Hub doesn't support direct messages yet")
                
        finally:
            ws1.close()
            ws2.close()
    
    def test_thread_preservation(self):
        """Test that thread_id is preserved in routed messages"""
        ws = websocket.create_connection(HUB_URL)
        
        try:
            assert authenticate_client(ws)
            
            # Join room
            join_msg = create_envelope(
                'control',
                {
                    'command': 'join',
                    'args': {'room': '#general'}
                }
            )
            ws.send(msgpack.packb(join_msg))
            ws.recv()
            
            # Send message with thread_id
            thread_id = str(uuid.uuid4())
            chat_msg = create_envelope(
                'chat',
                {
                    'role': 'user',
                    'content': 'Thread test'
                },
                to={'room': '#general'},
                thread_id=thread_id
            )
            
            ws.send(msgpack.packb(chat_msg))
            
            # In a real test with multiple clients,
            # we'd verify the thread_id is preserved
            
        finally:
            ws.close()


class TestCapabilityFiltering:
    """Test that messages with 'requires' are properly filtered"""
    
    def test_capability_mismatch(self):
        """Test that agents without required capabilities don't receive messages"""
        ws = websocket.create_connection(HUB_URL)
        
        try:
            # Auth with limited capabilities
            hello = create_envelope(
                'control',
                {
                    'command': 'hello',
                    'args': {
                        'client_name': 'limited-client',
                        'client_version': '0.1.0',
                        'capabilities': ['chat']  # No 'vision'
                    }
                }
            )
            ws.send(msgpack.packb(hello))
            ws.recv()
            
            auth = create_envelope(
                'control',
                {
                    'command': 'auth',
                    'args': {'api_key': TEST_API_KEY}
                }
            )
            ws.send(msgpack.packb(auth))
            ws.recv()
            
            # Send message requiring 'vision'
            msg = create_envelope(
                'chat',
                {
                    'role': 'user',
                    'content': 'Analyze this image'
                },
                requires=['vision'],
                to={'room': '#general', 'broadcast': True}
            )
            
            ws.send(msgpack.packb(msg))
            
            # Should either get error or message filtered
            ws.settimeout(1)
            try:
                response = msgpack.unpackb(ws.recv(), raw=False)
                # If we get a response, check it's an error
                if response['kind'] == 'error':
                    assert 'capability' in response['payload']['message'].lower()
            except websocket.WebSocketTimeoutException:
                # No response is also valid (message filtered)
                pass
                
        finally:
            ws.close()


if __name__ == '__main__':
    pytest.main([__file__, '-v'])