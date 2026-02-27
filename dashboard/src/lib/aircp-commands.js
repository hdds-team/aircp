/**
 * AIRCP DDS Helpers — CDR2 encode/decode for dashboard ↔ daemon communication
 *
 * publishCommand() — CDR2-encode and publish commands to aircp/commands
 * unwrapPayload()  — CDR2-decode DDS samples and extract payload_json
 *
 * The daemon uses CDR2-encoded AIRCP Messages for ALL DDS topics.
 * hdds-ws sends { _raw: "base64_cdr2", data: "first_cdr_string" } where
 * data is just the message UUID (not valid JSON). So CDR2 decode of _raw
 * is required to get the actual data from payload_json.
 */
import { hdds } from './hdds-client.js';
import { TOPIC_COMMANDS } from './topics.js';
import { Cdr2Buffer, aircp } from './aircp_generated.ts';
import { settingsStore } from '../stores/settings.svelte.js';

/**
 * Decode a DDS sample and extract the payload data.
 *
 * For event topics (presence, tasks, workflows, mode), the daemon publishes
 * CDR2-encoded AIRCP Messages where payload_json contains the actual data dict.
 * This function CDR2-decodes _raw and parses payload_json.
 *
 * @param {object} sample - Raw sample from hdds-ws (may have _raw field)
 * @returns {object} Parsed payload data, or sample as-is if not CDR2-encoded
 */
export function unwrapPayload(sample) {
  if (sample && sample._raw && typeof sample._raw === 'string') {
    try {
      const binary = atob(sample._raw);
      const bytes = new Uint8Array(binary.length);
      for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
      const buf = new Cdr2Buffer(bytes);
      const msg = aircp.decodeMessage(buf);
      if (msg.payload_json) {
        return JSON.parse(msg.payload_json);
      }
    } catch { /* fall through */ }
  }
  return sample;
}

/**
 * Publish a command to the daemon via DDS (CDR2-encoded AIRCP Message).
 *
 * @param {string} command - Command name (e.g., "stop", "stfu", "mode/set")
 * @param {object} params - Additional command parameters
 * @returns {boolean} true if published successfully
 */
export function publishCommand(command, params = {}) {
  const msg = {
    id: crypto.randomUUID(),
    room: 'commands',
    from_id: settingsStore.operatorId,
    from_type: aircp.SenderType.USER,
    kind: aircp.MessageKind.CONTROL,
    payload_json: JSON.stringify({ command, ...params }),
    timestamp_ns: BigInt(Date.now()) * 1000000n,
    protocol_version: '0.2.0',
    broadcast: true,
    to_agent_id: '',
    room_seq: 0n,
  };

  const buf = new Cdr2Buffer(new ArrayBuffer(8192));
  aircp.encodeMessage(msg, buf);
  const bytes = buf.toBytes();

  let binary = '';
  for (let i = 0; i < bytes.length; i++) {
    binary += String.fromCharCode(bytes[i]);
  }

  return hdds.publish(TOPIC_COMMANDS, { _raw: btoa(binary) });
}
