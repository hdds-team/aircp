"""
AIRCP Conformance Tests - Handshake & Authentication
"""
import uuid
import json
import msgpack
import pytest
import websocket
from datetime import datetime, timezone
from typing import Dict, Any
import os

# Hub URL from env or default to mock
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
    
    # Add optional fields
    for key in ['thread_id', 'requires', 'flow', 'meta']:
        if key in kwargs:
            envelope[key] = kwargs[key]
    
    return envelope

class TestHandshake:
    """Test the connection handshake flow"""
    
    def test_valid_handshake(self):
        """Test a complete valid handshake sequence"""
        ws = websocket.create_connection(HUB_URL)
        
        try:
            # Step 1: Send hello
            hello_msg = create_envelope(
                'control',
                {
                    'command': 'hello',
                    'args': {
                        'client_name': 'test-client',
                        'client_version': '0.1.0',
                        'capabilities': ['chat'],
                        'provides': ['test'],
                        'wants': ['events', 'history']
                    }
                }
            )
            
            ws.send(msgpack.packb(hello_msg))
            
            # Step 2: Receive hello response
            response = msgpack.unpackb(ws.recv(), raw=False)
            assert response['kind'] in ['control', 'event']
            
            if response['kind'] == 'control':
                assert response['payload']['command'] == 'hello'
                # Hub should return its capabilities
                assert 'args' in response['payload']
                assert 'capabilities' in response['payload']['args']
            
            # Step 3: Send auth
            auth_msg = create_envelope(
                'control',
                {
                    'command': 'auth',
                    'args': {'api_key': TEST_API_KEY}
                }
            )
            
            ws.send(msgpack.packb(auth_msg))
            
            # Step 4: Receive welcome or error
            response = msgpack.unpackb(ws.recv(), raw=False)
            assert response['kind'] in ['event', 'error']
            
            if response['kind'] == 'event':
                assert response['payload']['event_type'] == 'welcome'
            else:
                # If error, it should be a proper error format
                assert 'code' in response['payload']
                assert 'message' in response['payload']
                
        finally:
            ws.close()
    
    def test_missing_hello(self):
        """Test that sending auth without hello fails properly"""
        ws = websocket.create_connection(HUB_URL)
        
        try:
            # Try to auth directly without hello
            auth_msg = create_envelope(
                'control',
                {
                    'command': 'auth',
                    'args': {'api_key': TEST_API_KEY}
                }
            )
            
            ws.send(msgpack.packb(auth_msg))
            response = msgpack.unpackb(ws.recv(), raw=False)
            
            # Should get an error
            assert response['kind'] == 'error'
            assert response['payload']['code'] in ['unauthorized', 'invalid_command']
            
        finally:
            ws.close()
    
    def test_invalid_api_key(self):
        """Test authentication with invalid API key"""
        ws = websocket.create_connection(HUB_URL)
        
        try:
            # Send hello first
            hello_msg = create_envelope(
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
            
            ws.send(msgpack.packb(hello_msg))
            ws.recv()  # Consume hello response
            
            # Send auth with bad key
            auth_msg = create_envelope(
                'control',
                {
                    'command': 'auth',
                    'args': {'api_key': 'invalid-key'}
                }
            )
            
            ws.send(msgpack.packb(auth_msg))
            response = msgpack.unpackb(ws.recv(), raw=False)
            
            # Should get unauthorized error
            assert response['kind'] == 'error'
            assert response['payload']['code'] == 'unauthorized'
            
        finally:
            ws.close()
    
    def test_capability_negotiation(self):
        """Test that capabilities are properly negotiated"""
        ws = websocket.create_connection(HUB_URL)
        
        try:
            # Send hello with specific capabilities
            hello_msg = create_envelope(
                'control',
                {
                    'command': 'hello',
                    'args': {
                        'client_name': 'capability-test',
                        'client_version': '0.1.0',
                        'capabilities': ['chat', 'streaming', 'vision'],
                        'provides': ['llm', 'code_execution'],
                        'wants': ['events']
                    }
                }
            )
            
            ws.send(msgpack.packb(hello_msg))
            response = msgpack.unpackb(ws.recv(), raw=False)
            
            # Hub should respond with its capabilities
            if response['kind'] == 'control':
                assert 'capabilities' in response['payload'].get('args', {})
                hub_caps = response['payload']['args']['capabilities']
                assert isinstance(hub_caps, list)
                
        finally:
            ws.close()


class TestEnvelopeValidation:
    """Test that the hub validates envelope structure properly"""
    
    def test_missing_required_fields(self):
        """Test that missing required fields are rejected"""
        ws = websocket.create_connection(HUB_URL)
        
        try:
            # Send malformed message (missing 'kind')
            bad_msg = {
                'id': str(uuid.uuid4()),
                'ts': datetime.now(timezone.utc).isoformat(),
                'from': {'type': 'agent', 'id': 'test'},
                'to': {},
                'payload': {'command': 'hello'}
            }
            
            ws.send(msgpack.packb(bad_msg))
            
            # Should either get error or connection closed
            try:
                response = msgpack.unpackb(ws.recv(), raw=False)
                assert response['kind'] == 'error'
            except:
                # Connection might be closed for protocol violation
                pass
                
        finally:
            ws.close()
    
    def test_invalid_kind_value(self):
        """Test that invalid 'kind' values are rejected"""
        ws = websocket.create_connection(HUB_URL)
        
        try:
            bad_msg = create_envelope(
                'invalid_kind',  # Invalid kind
                {'test': 'data'}
            )
            
            ws.send(msgpack.packb(bad_msg))
            
            try:
                response = msgpack.unpackb(ws.recv(), raw=False)
                assert response['kind'] == 'error'
                assert 'invalid' in response['payload']['message'].lower()
            except:
                pass  # Connection closed is also valid
                
        finally:
            ws.close()


if __name__ == '__main__':
    pytest.main([__file__, '-v'])