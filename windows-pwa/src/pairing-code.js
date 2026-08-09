export const PAIRING_ALPHABET = '23456789ABCDEFGHJKLMNPQRSTUVWXYZ';
const validCode = new RegExp(`^[${PAIRING_ALPHABET}]{8}$`);
const validPrefix = new RegExp(`^[${PAIRING_ALPHABET}]*$`);

function compact(value) {
  return String(value || '').toUpperCase().replace(/[\s-]/g, '');
}

/** Returns a code only when it is exactly eight allowed pairing characters. */
export function normalizePairingCode(value) {
  const code = compact(value);
  return validCode.test(code) ? code : '';
}

/** Keeps invalid characters visible so they cannot be silently accepted on paste. */
export function formatPairingCode(value) {
  const code = compact(value);
  if (!validPrefix.test(code) || code.length > 8) return code;
  return code.length > 4 ? `${code.slice(0, 4)} ${code.slice(4)}` : code;
}
