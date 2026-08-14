import assert from 'node:assert/strict';
import test from 'node:test';
import { extractClipboardImageFiles, resolveAttachmentFilename } from '../src/clipboard-images.js';

function image(name = 'pasted.png', overrides = {}) {
  return { name, type: 'image/png', size: 12, lastModified: 42, ...overrides };
}

test('extracts image files from clipboard items and ignores non-file items', () => {
  const pasted = image(); let getAsFileCalls = 0;
  const result = extractClipboardImageFiles({
    items: [
      { kind: 'string', type: 'text/plain', getAsFile() { getAsFileCalls += 1; return pasted; } },
      { kind: 'file', type: 'image/png', getAsFile() { getAsFileCalls += 1; return pasted; } },
      { kind: 'file', type: 'application/pdf', getAsFile() { getAsFileCalls += 1; return { type: 'application/pdf' }; } },
    ],
  });
  assert.deepEqual(result, [pasted]);
  assert.equal(getAsFileCalls, 2, 'string clipboard items are not treated as files');
});

test('treats item-derived images as canonical, retaining identical metadata files', () => {
  const first = image('clipboard.png');
  const second = image('clipboard.png');
  const result = extractClipboardImageFiles({
    items: [
      { kind: 'file', type: 'image/png', getAsFile: () => first },
      { kind: 'file', type: 'image/png', getAsFile: () => second },
    ],
    files: [image('different-wrapper.png', { size: 999 })],
  });
  assert.deepEqual(result, [first, second]);
  assert.equal(result[0], first, 'item Files are returned untouched');
  assert.equal(result[1], second, 'identical-metadata item Files are not collapsed');
});

test('does not merge mismatched item/file wrappers and uses files only as a fallback', () => {
  const itemFile = image('item.png', { size: 12 });
  const mismatchedFileView = image('wrapper.png', { size: 99 });
  assert.deepEqual(extractClipboardImageFiles({
    items: [{ kind: 'file', type: 'image/png', getAsFile: () => itemFile }],
    files: [mismatchedFileView],
  }), [itemFile]);
  const filesOnly = [image('only.png'), image('only-2.webp', { type: 'image/webp' })];
  assert.deepEqual(extractClipboardImageFiles({ items: [{ kind: 'file', type: 'text/plain', getAsFile: () => null }], files: filesOnly }), filesOnly);
});

test('returns no files for text or HTML-only clipboard data so native paste remains available', () => {
  const called = [];
  const result = extractClipboardImageFiles({
    items: [
      { kind: 'string', type: 'text/plain', getAsString: (callback) => callback('hello') },
      { kind: 'string', type: 'text/html', getAsString: (callback) => callback('<b>hello</b>') },
    ],
    files: [],
  });
  called.push(result.length);
  assert.deepEqual(called, [0]);
});

test('keeps clipboard File metadata, including missing or generic names, unchanged', () => {
  const unnamed = image('', { type: 'image/webp', size: 8, lastModified: 0 });
  const result = extractClipboardImageFiles({ files: [unnamed] });
  assert.equal(result[0], unnamed);
  assert.equal(result[0].name, '');
  assert.equal(result[0].type, 'image/webp');
  assert.equal(result[0].size, 8);
});

test('resolves pasted image names without changing ordinary names or File metadata', () => {
  assert.equal(resolveAttachmentFilename(image('', { type: 'image/jpeg' }), 0), 'pasted-image-1.jpg');
  assert.equal(resolveAttachmentFilename(image('blob', { type: 'image/webp' }), 1), 'pasted-image-2.webp');
  assert.equal(resolveAttachmentFilename(image('image.png', { type: 'image/png' }), 2), 'pasted-image-3.png');
  assert.equal(resolveAttachmentFilename(image('holiday.PNG'), 3), 'holiday.PNG');
  assert.equal(resolveAttachmentFilename({ name: 'notes.txt', type: 'text/plain' }, 4), 'notes.txt');
  assert.equal(resolveAttachmentFilename({ type: 'application/pdf' }, 4), '');
});
