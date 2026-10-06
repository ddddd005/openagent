export const sessionUuid = (value: unknown): value is string => typeof value === "string"
  && /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(value);
export const sessionInteger = (value: unknown): value is number =>
  Number.isSafeInteger(value) && (value as number) >= 0;
export const sessionObject = (value: unknown): value is Record<string, unknown> =>
  !!value && typeof value === "object" && !Array.isArray(value);
export function exactSessionObject(value: unknown, keys: string[], optional: string[] = []): value is Record<string, unknown> {
  return sessionObject(value) && keys.every((key) => Object.hasOwn(value, key))
    && Object.keys(value).every((key) => keys.includes(key) || optional.includes(key));
}
export function safeSessionJson(value: unknown): boolean {
  let entries = 0;
  let chars = 0;
  function check(input: unknown, depth: number): boolean {
    if (++entries > 100_000 || depth > 64) return false;
    if (input === null || typeof input === "boolean") return true;
    if (typeof input === "number") return Number.isFinite(input);
    if (typeof input === "string") return (chars += input.length) <= 4_000_000;
    return (Array.isArray(input) || sessionObject(input))
      && Object.values(input).every((child) => check(child, depth + 1));
  }
  return check(value, 0);
}
