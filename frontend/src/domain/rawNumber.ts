export interface RawNumberConstraints {
  required?: boolean;
  integer?: boolean;
  min?: number;
  max?: number;
}

export function parseRawNumber(raw: string, constraints: RawNumberConstraints = {}) {
  if (raw === "") return constraints.required === false
    ? { valid: true, value: null, error: "" }
    : { valid: false, value: null, error: "请输入数值" };
  if (!/^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$/.test(raw))
    return { valid: false, value: null, error: "数值尚未填写完整" };
  const value = Number(raw);
  if (!Number.isFinite(value)) return { valid: false, value: null, error: "请输入有限数值" };
  if (constraints.integer && !Number.isInteger(value))
    return { valid: false, value: null, error: "请输入整数" };
  if (constraints.min !== undefined && value < constraints.min)
    return { valid: false, value: null, error: `数值不能小于 ${constraints.min}` };
  if (constraints.max !== undefined && value > constraints.max)
    return { valid: false, value: null, error: `数值不能大于 ${constraints.max}` };
  return { valid: true, value, error: "" };
}
