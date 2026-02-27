"""
Mock AIRCP Hub - Minimal implementation for testing
"""
import asyncio
import websockets
import msgpack
import uuid
import json
from datetime import datetime, timezone
from typing import Dict, Set, Any, Optional, List
from collections import deque
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class RoomHistory:
    """Manages room message history with sequence numbering"""

    def __init__(self, max_size: int = 1000):
        self.messages: deque = deque(maxlen=max_size)
        self.seq_counter = 0

    def add_message(self, envelope: Dict) -> Dict:
        """Add a message to history, returns the envelope with room_seq"""
        self.seq_counter += 1

        # Add room_seq to message
        if 'meta' not in envelope:
            envelope['meta'] = {}
        envelope['meta']['room_seq'] = self.seq_counter

        self.messages.append(envelope)
        return envelope

    def get_history(self, since_seq: Optional[int] = None, limit: int = 100) -> Dict:
        """Get history, optionally starting from since_seq"""
        messages = list(self.messages)

        # Filter by since_seq if provided
        if since_seq is not None:
            messages = [m for m in messages if m.get('meta', {}).get('room_seq', 0) > since_seq]

        # Limit results
        messages = messages[-limit:] if limit else messages

        return {
            'messages': messages,
            'since_seq': since_seq,
            'total': self.seq_counter
        }


class MockHub:
    """Minimal AIRCP hub for conformance testing"""

    def __init__(self):
        self.sessions: Dict[str, Dict] = {}
        # Track agent IDs in each room (for routing)
        self.room_members: Dict[str, Set[str]] = {
            '#general': set(),
            '#system': set(),
            '#agents': set()
        }
        # Track session WebSocket objects by agent_id in room
        # room -> agent_id -> [websocket, ...]
        self.room_websockets: Dict[str, Dict[str, list]] = {}
        # Room history with room_seq numbering
        self.room_history: Dict[str, RoomHistory] = {}
        self.api_keys = {
            'test-key-123': {
                'agent_id': '@test-agent',
                'allowed_rooms': ['#general', '#agents'],
                'capabilities': ['chat', 'history']
            }
        }

        # Initialize history for default rooms
        for room in ['#general', '#system', '#agents']:
            self.room_history[room] = RoomHistory()
            self.room_websockets[room] = {}
        
    async def handle_connection(self, websocket, path):
        """Handle a new WebSocket connection"""
        session_id = str(uuid.uuid4())
        session = {
            'id': session_id,
            'websocket': websocket,
            'authenticated': False,
            'hello_received': False,
            'agent_id': None,
            'capabilities': [],
            'rooms': set()  # Track which rooms this session is in
        }

        self.sessions[session_id] = session
        logger.info(f"New connection: {session_id}")

        try:
            async for message in websocket:
                try:
                    envelope = msgpack.unpackb(message, raw=False)
                    response = await self.handle_message(session, envelope)

                    if response:
                        await websocket.send(msgpack.packb(response))

                except msgpack.exceptions.ExtraData:
                    await self.send_error(websocket, 'invalid_format', 'Invalid MessagePack')
                except Exception as e:
                    logger.error(f"Error handling message: {e}")
                    await self.send_error(websocket, 'internal_error', str(e))

        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            # Cleanup session
            agent_id = session.get('agent_id')
            if agent_id:
                # Remove from all rooms
                for room in list(session.get('rooms', [])):
                    if room in self.room_members:
                        self.room_members[room].discard(agent_id)
                    if room in self.room_websockets and agent_id in self.room_websockets[room]:
                        self.room_websockets[room][agent_id].remove(websocket)
                        if not self.room_websockets[room][agent_id]:
                            del self.room_websockets[room][agent_id]

            del self.sessions[session_id]
            logger.info(f"Connection closed: {session_id}")
    
    async def handle_message(self, session: Dict, envelope: Dict) -> Optional[Dict]:
        """Process an incoming message"""
        
        # Validate envelope structure
        required_fields = ['id', 'ts', 'from', 'to', 'kind', 'payload']
        for field in required_fields:
            if field not in envelope:
                return self.create_error('invalid_format', f'Missing required field: {field}')
        
        # Validate kind
        if envelope['kind'] not in ['chat', 'control', 'event', 'error']:
            return self.create_error('invalid_format', f'Invalid kind: {envelope["kind"]}')
        
        # Handle based on kind
        if envelope['kind'] == 'control':
            return await self.handle_control(session, envelope)
        elif envelope['kind'] == 'chat':
            return await self.handle_chat(session, envelope)
        
        return None
    
    async def handle_control(self, session: Dict, envelope: Dict) -> Optional[Dict]:
        """Handle control messages"""
        payload = envelope.get('payload', {})
        command = payload.get('command')
        args = payload.get('args', {})
        
        if command == 'hello':
            # Process hello
            session['hello_received'] = True
            session['capabilities'] = args.get('capabilities', [])
            
            # Send hello response
            return self.create_envelope(
                'control',
                {
                    'command': 'hello',
                    'args': {
                        'capabilities': ['chat', 'streaming', 'history', 'events'],
                        'protocol_version': '0.1.0'
                    }
                }
            )
            
        elif command == 'auth':
            # Check hello was received
            if not session['hello_received']:
                return self.create_error('unauthorized', 'Must send hello before auth')
            
            # Validate API key
            api_key = args.get('api_key')
            if api_key in self.api_keys:
                session['authenticated'] = True
                session['agent_id'] = self.api_keys[api_key]['agent_id']
                
                # Send welcome event
                return self.create_envelope(
                    'event',
                    {
                        'event_type': 'welcome',
                        'data': {
                            'agent_id': session['agent_id'],
                            'allowed_rooms': self.api_keys[api_key]['allowed_rooms']
                        }
                    }
                )
            else:
                return self.create_error('unauthorized', 'Invalid API key')
                
        elif command == 'join':
            # Check authenticated
            if not session['authenticated']:
                return self.create_error('unauthorized', 'Must authenticate first')

            room = args.get('room')
            agent_id = session['agent_id']

            # Initialize room if needed
            if room not in self.room_members:
                self.room_members[room] = set()
                self.room_history[room] = RoomHistory()
                self.room_websockets[room] = {}

            # Add agent to room
            self.room_members[room].add(agent_id)
            session['rooms'].add(room)

            # Track WebSocket for this agent in this room
            if agent_id not in self.room_websockets[room]:
                self.room_websockets[room][agent_id] = []
            self.room_websockets[room][agent_id].append(session['websocket'])

            logger.info(f"Agent {agent_id} joined {room}")

            return self.create_envelope(
                'event',
                {
                    'event_type': 'agent_joined',
                    'data': {
                        'room': room,
                        'agent_id': agent_id
                    }
                }
            )
            
        elif command == 'history':
            # Check authenticated
            if not session['authenticated']:
                return self.create_error('unauthorized', 'Must authenticate first')

            room = args.get('room', '#general')
            since_seq = args.get('since_seq')
            limit = args.get('limit', 100)

            # Initialize room history if it doesn't exist
            if room not in self.room_history:
                self.room_history[room] = RoomHistory()

            # Get history
            history_data = self.room_history[room].get_history(since_seq, limit)

            return self.create_envelope(
                'event',
                {
                    'event_type': 'history_chunk',
                    'data': {
                        'room': room,
                        'messages': history_data['messages'],
                        'since_seq': since_seq,
                        'total': history_data['total']
                    }
                }
            )
            
        return self.create_error('invalid_command', f'Unknown command: {command}')
    
    async def handle_chat(self, session: Dict, envelope: Dict) -> Optional[Dict]:
        """Handle chat messages - route to room or direct"""
        if not session['authenticated']:
            return self.create_error('unauthorized', 'Must authenticate first')

        to = envelope.get('to', {})
        room = to.get('room')
        agent_id = to.get('agent_id')
        broadcast = to.get('broadcast', False)

        # Validate that routing is specified
        if not room and not agent_id:
            return self.create_error('invalid_command', 'Must specify room or agent_id in to')

        # Add room_seq if routing to a room
        if room:
            if room not in self.room_history:
                self.room_history[room] = RoomHistory()

            # Add room_seq to envelope
            envelope = self.room_history[room].add_message(envelope)

            # Route to room members if broadcast
            if broadcast:
                await self.broadcast_to_room(room, envelope)

        # TODO: Handle direct messages (agent_id routing)
        # TODO: Check capability requirements

        # No immediate response for chat messages
        return None

    async def broadcast_to_room(self, room: str, envelope: Dict) -> None:
        """Broadcast a message to all members of a room"""
        if room not in self.room_websockets:
            return

        # Send to all websockets in the room except sender
        sender_id = envelope.get('from', {}).get('id')
        tasks = []

        for agent_id, websockets in self.room_websockets[room].items():
            if agent_id != sender_id:  # Don't send back to sender
                for ws in websockets:
                    try:
                        tasks.append(ws.send(msgpack.packb(envelope)))
                    except Exception as e:
                        logger.warning(f"Failed to broadcast to {agent_id}: {e}")

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def send_error(self, websocket, code: str, message: str):
        """Send an error message"""
        error = self.create_error(code, message)
        await websocket.send(msgpack.packb(error))
    
    def create_envelope(self, kind: str, payload: Dict) -> Dict:
        """Create a response envelope"""
        return {
            'id': str(uuid.uuid4()),
            'ts': datetime.now(timezone.utc).isoformat(),
            'from': {'type': 'system', 'id': 'hub'},
            'to': {},
            'kind': kind,
            'payload': payload,
            'meta': {'protocol_version': '0.1.0'}
        }
    
    def create_error(self, code: str, message: str, details: Any = None) -> Dict:
        """Create an error envelope"""
        payload = {
            'code': code,
            'message': message
        }
        if details:
            payload['details'] = details
        
        return self.create_envelope('error', payload)


async def main():
    """Run the mock hub"""
    hub = MockHub()
    
    async with websockets.serve(hub.handle_connection, 'localhost', 6667):
        logger.info("Mock AIRCP Hub running on ws://localhost:6667")
        await asyncio.Future()  # Run forever


if __name__ == '__main__':
    asyncio.run(main())