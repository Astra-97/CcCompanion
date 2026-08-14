const IMAGE_MIME = /^image\//i;
const IMAGE_EXTENSIONS = new Set(['avif', 'bmp', 'gif', 'heic', 'heif', 'ico', 'jfi', 'jfif', 'jif', 'jpeg', 'jpg', 'png', 'svg', 'tif', 'tiff', 'webp', 'jxl']);
const GENERIC_IMAGE_STEMS = new Set(['blob', 'clipboard', 'file', 'image', 'photo', 'picture', 'pasted', 'pasted-image', 'screenshot', 'thumbnail', 'unknown', 'untitled']);
const MIME_IMAGE_EXTENSIONS = new Map([
  ['image/avif', 'avif'], ['image/bmp', 'bmp'], ['image/gif', 'gif'], ['image/heic', 'heic'],
  ['image/heif', 'heif'], ['image/jpeg', 'jpg'], ['image/jxl', 'jxl'], ['image/png', 'png'],
  ['image/svg+xml', 'svg'], ['image/tiff', 'tif'], ['image/webp', 'webp'], ['image/x-icon', 'ico'],
]);

export function isImageFile(file) {
  return Boolean(file && typeof file === 'object' && IMAGE_MIME.test(String(file.type || '')));
}

function imageExtension(file) {
  const type = String(file?.type || '').toLowerCase().split(';', 1)[0].trim();
  return MIME_IMAGE_EXTENSIONS.get(type) || 'img';
}

/**
 * Resolve the name shown to the user and sent in the upload query without
 * changing the File itself. Non-image files retain their existing name;
 * image Files with no meaningful image name receive a safe MIME-derived one.
 */
export function resolveAttachmentFilename(file, index = 0) {
  const name = String(file?.name || '');
  if (!isImageFile(file)) return name;
  const basename = name.split(/[\\/]/).pop() || '';
  const match = /^(.+)\.([A-Za-z0-9]{1,8})$/.exec(basename);
  const extension = match?.[2]?.toLowerCase() || '';
  const stem = match?.[1]?.trim().toLowerCase() || '';
  if (basename && IMAGE_EXTENSIONS.has(extension) && !GENERIC_IMAGE_STEMS.has(stem)) return name;
  return `pasted-image-${Math.max(1, Number(index) + 1)}.${imageExtension(file)}`;
}

/**
 * Extract image Files from a clipboard. When valid image Files are available
 * through DataTransfer.items, that collection is canonical: DataTransfer.files
 * is only a fallback for clipboard implementations that expose no item image.
 * Files are returned untouched, including when two item images share metadata.
 */
export function extractClipboardImageFiles(clipboardData) {
  if (!clipboardData) return [];
  const itemImages = [];
  for (const item of Array.from(clipboardData.items || [])) {
    if (item?.kind !== 'file') continue;
    let file = null;
    try { file = item.getAsFile?.() || null; } catch { /* clipboard providers may reject access */ }
    if (isImageFile(file)) itemImages.push(file);
  }
  if (itemImages.length) return itemImages;
  return Array.from(clipboardData.files || []).filter(isImageFile);
}
