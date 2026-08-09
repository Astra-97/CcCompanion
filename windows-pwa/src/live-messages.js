/**
 * Keep server history authoritative while displaying one ephemeral live row.
 * Kairos reports text through /chat/status draft snapshots; SSE remains useful
 * for contacts that emit chunks.  Neither path is allowed to append duplicate
 * rows to persisted history.
 */
function timestamp(value) {
  const parsed = Date.parse(String(value || ''));
  return Number.isFinite(parsed) ? parsed : 0;
}

function revision(value) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function isOlder(current, incomingRevision, incomingAt) {
  const currentRevision = revision(current?.revision);
  const nextRevision = revision(incomingRevision);
  if (currentRevision !== null && nextRevision !== null && nextRevision !== currentRevision) return nextRevision < currentRevision;
  return Boolean(incomingAt && timestamp(current?.updatedAt) > incomingAt);
}

function isAtLeastAsNew(first, second) {
  const a = revision(first?.revision); const b = revision(second?.revision);
  if (a !== null && b !== null && a !== b) return a > b;
  return timestamp(first?.updatedAt) >= timestamp(second?.updatedAt);
}

export function reduceStreamDraft(current, payload = {}) {
  const event = String(payload.event || '');
  if (!['chunk', 'done', 'draft', 'lifecycle'].includes(event)) return current || null;
  const streamId = String(payload.turn_id || payload.turnId || payload.stream_id || 'current');
  const incomingRevision = payload.revision ?? payload.draft_revision ?? '';
  const incomingAt = timestamp(payload.updated_at || payload.updatedAt);
  if (current?.turnId && current.turnId !== streamId && incomingAt && timestamp(current.updatedAt) > incomingAt) return current;
  if (current?.turnId === streamId && isOlder(current, incomingRevision, incomingAt)) return current;
  if (current?.terminal && current.turnId === streamId && event === 'draft') return current;
  if (event === 'lifecycle') {
    if (current?.turnId && current.turnId !== streamId) return current;
    return { streamId, turnId: streamId, revision: incomingRevision, body: '', streaming: false, terminal: true, refreshHistory: Boolean(payload.refresh_history), updatedAt: payload.updated_at || payload.updatedAt || '' };
  }
  const next = current && current.streamId === streamId
    ? { ...current }
    : { streamId, turnId: streamId, revision: incomingRevision, body: '', streaming: true, terminal: false, updatedAt: '' };
  if (event === 'draft') {
    next.body = String(payload.text || '');
    next.streaming = ['queued', 'generating'].includes(String(payload.reply_state || 'generating'));
    next.replyState = String(payload.reply_state || 'generating');
    next.revision = incomingRevision;
    next.updatedAt = payload.updated_at || payload.updatedAt || '';
    return next;
  }
  if (event === 'chunk') next.body += String(payload.text || '');
  if (event === 'done') { next.body = payload.text ? String(payload.text) : next.body; next.streaming = false; }
  return next;
}

function historyAlreadyContains(history, text) {
  if (!text) return false;
  const lastAssistant = [...history].reverse().find(({ role }) => role === 'assistant');
  const body = String(lastAssistant?.body || '');
  return body === text || body.endsWith(text);
}

/** Returns persisted history plus at most one transient assistant row. */
export function composeLiveMessages(history = [], live = {}, { contactId = '', stream = null } = {}) {
  const persisted = Array.isArray(history) ? history : [];
  // The Codex/Kairos transport writes authoritative partial text to status.
  // Prefer it over any concurrent SSE chunks so a reconnect cannot duplicate it.
  const liveActive = Boolean(live?.busy || ['queued', 'generating'].includes(live?.replyState));
  const streamActive = Boolean(stream && !stream.terminal && (stream.streaming || ['queued', 'generating'].includes(stream.replyState)));
  const terminalForLiveTurn = Boolean(stream?.terminal && (!live?.turnId || live.turnId === stream.turnId));
  const preferStream = streamActive && (!liveActive || (stream.turnId === live.turnId && isAtLeastAsNew(stream, live)) || (stream.turnId !== live.turnId && timestamp(stream.updatedAt) > timestamp(live.updatedAt)));
  const statusDraft = contactId === 'kairos' && !terminalForLiveTurn && (liveActive || streamActive) ? String(preferStream ? stream?.body : live?.draft || '') : '';
  if (statusDraft && !historyAlreadyContains(persisted, statusDraft)) {
    return [...persisted, {
      id: `live-draft-${contactId}`,
      role: 'assistant',
      body: statusDraft,
      time: '现在',
      streaming: true,
      transient: true,
    }];
  }
  // SSE is a best-effort delta channel. Once the status snapshot is idle the
  // next history snapshot is expected to contain the final persisted message.
  if (stream?.body && streamActive && !historyAlreadyContains(persisted, stream.body)) {
    return [...persisted, {
      id: `live-stream-${contactId}-${stream.streamId}`,
      role: 'assistant',
      body: stream.body,
      time: '现在',
      streaming: Boolean(stream.streaming),
      transient: true,
    }];
  }
  return persisted;
}

/** Authoritative status snapshots terminate an SSE draft even if lifecycle was lost. */
export function reconcileSnapshotStream(stream, live = {}) {
  if (!stream || stream.terminal) return stream || null;
  const active = Boolean(live.busy || ['queued', 'generating'].includes(live.replyState));
  if (active) return stream;
  const sameTurn = Boolean(live.turnId && live.turnId === stream.turnId);
  const newerTurn = Boolean(live.turnId && live.turnId !== stream.turnId && timestamp(live.updatedAt) >= timestamp(stream.updatedAt));
  if (!sameTurn && !newerTurn) return stream;
  if (sameTurn && !isAtLeastAsNew(live, stream)) return stream;
  return { ...stream, body: '', streaming: false, terminal: true, refreshHistory: false, revision: live.revision || stream.revision, updatedAt: live.updatedAt || stream.updatedAt };
}
