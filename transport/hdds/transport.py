"""
AIRCP Transport over HDDS (DDS middleware).

Wrapper simplifié autour du SDK Python HDDS.
Cache la complexité DDS derrière une API simple.

v3.0: Added publish_event() / receive_topic() for dashboard bridge topics.

Usage:
    transport = AIRCPTransport("@my_agent")
    transport.join_room("#general")
    transport.send_chat("#general", "Hello!")
    messages = transport.receive_new("#general")

    # v3.0: Event topics (presence, tasks, mode, workflows)
    transport.publish_event("aircp/presence", {"agent_id": "@alpha", "health": "online"})
    msgs = transport.receive_topic("aircp/commands")
"""

import json
import uuid
import time
import sys
from pathlib import Path
from dataclasses import dataclass
from typing import Optional

# Add HDDS SDK to path
_aircp_home = Path(__file__).resolve().parent.parent.parent
HDDS_SDK_PATH = Path(os.environ.get("HDDS_SDK_PATH", _aircp_home / "lib" / "hdds_sdk" / "python"))
if HDDS_SDK_PATH.is_dir() and str(HDDS_SDK_PATH) not in sys.path:
    sys.path.insert(0, str(HDDS_SDK_PATH))

# Import HDDS
import hdds

# Import generated types
from .generated.aircp_types import Message, SenderType, MessageKind


@dataclass
class AIRCPMessage:
    """High-level message representation."""
    id: str
    room: str
    from_id: str
    from_type: SenderType
    kind: MessageKind
    payload: dict
    timestamp_ns: int
    room_seq: int
    to_agent_id: str = ""
    broadcast: bool = True
    project: str = ""

    @classmethod
    def from_raw(cls, msg: Message) -> "AIRCPMessage":
        """Convert from raw CDR2 Message to high-level AIRCPMessage."""
        try:
            payload = json.loads(msg.payload_json) if msg.payload_json else {}
        except json.JSONDecodeError:
            payload = {"raw": msg.payload_json}

        return cls(
            id=msg.id,
            room=msg.room,
            from_id=msg.from_id,
            from_type=msg.from_type,
            kind=msg.kind,
            payload=payload,
            timestamp_ns=msg.timestamp_ns,
            room_seq=msg.room_seq,
            to_agent_id=msg.to_agent_id,
            broadcast=msg.broadcast,
            project=getattr(msg, 'project', ''),
        )

    def to_raw(self) -> Message:
        """Convert to raw CDR2 Message for serialization."""
        return Message(
            id=self.id,
            room=self.room,
            from_id=self.from_id,
            from_type=self.from_type,
            to_agent_id=self.to_agent_id,
            broadcast=self.broadcast,
            kind=self.kind,
            payload_json=json.dumps(self.payload),
            timestamp_ns=self.timestamp_ns,
            protocol_version="0.3.0",
            room_seq=self.room_seq,
            project=self.project,
        )


class AIRCPTransport:
    """Transport HDDS pour AIRCP."""

    def __init__(self, agent_id: str, domain_id: int = 219):
        if not agent_id.startswith("@"):
            agent_id = f"@{agent_id}"

        self.agent_id = agent_id
        self.domain_id = domain_id
        self._room_seq: dict[str, int] = {}

        self.participant = hdds.Participant(
            f"aircp_{agent_id.lstrip('@')}",
            domain_id=domain_id
        )

        # QoS for chat: reliable, transient_local, keep 1000 messages
        self.chat_qos = (
            hdds.QoS.reliable()
            .transient_local()
            .history_depth(1000)
        )

        # QoS for events: reliable, transient_local, keep 10 (last state)
        self.event_qos = (
            hdds.QoS.reliable()
            .transient_local()
            .history_depth(10)
        )

        # Writers and readers per room (chat)
        self.writers: dict[str, hdds.DataWriter] = {}
        self.readers: dict[str, hdds.DataReader] = {}

        # Writers and readers for raw topics (events, commands)
        self._topic_writers: dict[str, hdds.DataWriter] = {}
        self._topic_readers: dict[str, hdds.DataReader] = {}

        # Deduplication cache
        self._seen_ids: set[str] = set()
        self._max_seen_ids = 10000

    # =====================================================================
    # Topic management (v3.0)
    # =====================================================================

    def _ensure_topic_writer(self, topic: str) -> Optional[hdds.DataWriter]:
        """Get or create a writer for a raw topic name."""
        if topic in self._topic_writers:
            return self._topic_writers[topic]
        try:
            writer = self.participant.create_writer(topic, qos=self.event_qos)
            self._topic_writers[topic] = writer
            return writer
        except hdds.HddsException as e:
            print(f"[HDDS] Failed to create writer for {topic}: {e}")
            return None

    def _ensure_topic_reader(self, topic: str) -> Optional[hdds.DataReader]:
        """Get or create a reader for a raw topic name."""
        if topic in self._topic_readers:
            return self._topic_readers[topic]
        try:
            reader = self.participant.create_reader(topic, qos=self.event_qos)
            self._topic_readers[topic] = reader
            return reader
        except hdds.HddsException as e:
            print(f"[HDDS] Failed to create reader for {topic}: {e}")
            return None

    # =====================================================================
    # Chat (room-based)
    # =====================================================================

    def join_room(self, room: str) -> bool:
        """Join a chat room (creates topic + writer + reader)."""
        if room in self.writers:
            return True

        topic_name = f"aircp/{room.lstrip('#')}"
        try:
            self.writers[room] = self.participant.create_writer(topic_name, qos=self.chat_qos)
            self.readers[room] = self.participant.create_reader(topic_name, qos=self.chat_qos)
            self._room_seq[room] = 0
            return True
        except hdds.HddsException as e:
            print(f"Failed to join room {room}: {e}")
            return False

    def leave_room(self, room: str):
        """Leave a room."""
        self.writers.pop(room, None)
        self.readers.pop(room, None)
        self._room_seq.pop(room, None)

    def send_chat(self, room: str, content: str, payload_extra: dict = None, from_id: str = None, project: str = "") -> Optional[str]:
        """Send a chat message to a room."""
        if room not in self.writers:
            print(f"Not joined to room {room}")
            return None

        payload = {"role": "assistant", "content": content}
        if payload_extra:
            payload.update(payload_extra)

        self._room_seq[room] += 1

        msg = AIRCPMessage(
            id=str(uuid.uuid4()),
            room=room,
            from_id=from_id or self.agent_id,
            from_type=SenderType.AGENT,
            kind=MessageKind.CHAT,
            payload=payload,
            timestamp_ns=time.time_ns(),
            room_seq=self._room_seq[room],
            broadcast=True,
            project=project,
        )

        try:
            raw = msg.to_raw()
            encoded = raw.encode_cdr2_le()
            self.writers[room].write(encoded)
            return msg.id
        except Exception as e:
            print(f"Failed to send message: {e}")
            return None

    def receive_new(self, room: str) -> list[AIRCPMessage]:
        """Receive new messages from a room (non-blocking, deduplicating)."""
        if room not in self.readers:
            return []

        messages = []
        while True:
            data = self.readers[room].take()
            if data is None:
                break

            try:
                raw, _ = Message.decode_cdr2_le(data)
                msg = AIRCPMessage.from_raw(raw)

                if msg.from_id == self.agent_id:
                    continue
                if msg.id in self._seen_ids:
                    continue
                self._seen_ids.add(msg.id)

                if len(self._seen_ids) > self._max_seen_ids:
                    self._seen_ids = set(list(self._seen_ids)[-5000:])

                messages.append(msg)
            except Exception as e:
                print(f"Failed to decode message: {e}")

        return messages

    def get_history(self, room: str, limit: int = 100) -> list[AIRCPMessage]:
        """Get message history (via TRANSIENT_LOCAL QoS)."""
        return self.receive_new(room)[:limit]

    # =====================================================================
    # Events — v3.0 (presence, tasks, mode, workflows, commands)
    # =====================================================================

    def publish_event(self, topic: str, data: dict, from_id: str = None, project: str = "") -> Optional[str]:
        """
        Publish an event to a DDS topic.

        Uses the same Message IDL struct (kind=EVENT, data in payload_json)
        so hdds-ws decodes it to JSON identically to chat messages.
        The dashboard unwraps payload_json to get the actual data.

        Args:
            topic: DDS topic name (e.g., "aircp/presence")
            data: Payload dict
            from_id: Sender ID (default: self.agent_id)

        Returns:
            Message ID if sent, None if failed
        """
        writer = self._ensure_topic_writer(topic)
        if not writer:
            return None

        msg = AIRCPMessage(
            id=str(uuid.uuid4()),
            room=topic,
            from_id=from_id or self.agent_id,
            from_type=SenderType.SYSTEM,
            kind=MessageKind.EVENT,
            payload=data,
            timestamp_ns=time.time_ns(),
            room_seq=0,
            broadcast=True,
            project=project,
        )

        try:
            raw = msg.to_raw()
            encoded = raw.encode_cdr2_le()
            writer.write(encoded)
            return msg.id
        except Exception as e:
            print(f"[HDDS] Failed to publish event to {topic}: {e}")
            return None

    def receive_topic(self, topic: str) -> list[AIRCPMessage]:
        """
        Receive new messages from a raw topic (non-blocking).

        Args:
            topic: DDS topic name

        Returns:
            List of new messages
        """
        reader = self._ensure_topic_reader(topic)
        if not reader:
            return []

        messages = []
        while True:
            data = reader.take()
            if data is None:
                break
            try:
                raw, _ = Message.decode_cdr2_le(data)
                msg = AIRCPMessage.from_raw(raw)
                messages.append(msg)
            except Exception as e:
                print(f"[HDDS] Failed to decode topic {topic}: {e}")

        return messages

    # =====================================================================
    # Lifecycle
    # =====================================================================

    def close(self):
        """Close the transport and cleanup resources."""
        self.writers.clear()
        self.readers.clear()
        self._topic_writers.clear()
        self._topic_readers.clear()
        self.participant.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False
