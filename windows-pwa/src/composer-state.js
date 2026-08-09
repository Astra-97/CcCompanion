/** In-memory, contact-scoped composer and upload-operation ownership. */
export function createComposerState() {
  const drafts = new Map(); const operations = new Map(); let sequence = 0;
  const get = (contactId) => {
    if (!drafts.has(contactId)) drafts.set(contactId, { text: '', attachments: [] });
    return drafts.get(contactId);
  };
  const isCurrent = (contactId, operation) => operations.get(contactId) === operation;
  return {
    get,
    begin(contactId) {
      if (operations.has(contactId)) throw new Error('This contact already has an active send operation');
      const operation = { id: ++sequence, controller: new AbortController(), progress: '', uploading: false };
      operations.set(contactId, operation); return operation;
    },
    current: (contactId) => operations.get(contactId) || null,
    isCurrent,
    setProgress(contactId, operation, progress) { if (isCurrent(contactId, operation)) operation.progress = progress; },
    finish(contactId, operation) { if (isCurrent(contactId, operation)) operations.delete(contactId); },
    cancel(contactId) { operations.get(contactId)?.controller.abort(); },
    cancelAll() { operations.forEach(({ controller }) => controller.abort()); },
  };
}
