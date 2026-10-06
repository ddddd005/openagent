import type { BackendPreparationProgram } from "./preparation";
import { isDataDefinition, isGlobalContent } from "./workbenchResources";

const object = (value: unknown): value is Record<string, unknown> =>
  !!value && typeof value === "object" && !Array.isArray(value);
const exact = (value: unknown, required: string[], optional: string[] = []): value is Record<string, unknown> =>
  object(value) && required.every((key) => Object.hasOwn(value, key))
  && Object.keys(value).every((key) => required.includes(key) || optional.includes(key));
const uuidPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const uuid = (value: unknown): value is string => typeof value === "string" && uuidPattern.test(value);
const nodeId = (value: unknown): value is string => typeof value === "string"
  && (uuidPattern.test(value) || value.endsWith(":materials") && uuidPattern.test(value.slice(0, -10)));
const text = (value: unknown): value is string => typeof value === "string" && value.length <= 1_000_000;
const name = (value: unknown) => typeof value === "string" && value.length <= 128
  && /^[\p{L}_][\p{L}\p{N}_]*$/u.test(value) && !["workflow_session_id", "node_binding_id"].includes(value);
const typed = (value: unknown, type: unknown) =>
  type === "string" ? text(value) : type === "boolean" ? typeof value === "boolean"
    : typeof value === "number" && Number.isFinite(value) && (type !== "integer" || Number.isSafeInteger(value));
const scalar = (value: unknown) => text(value) || typeof value === "boolean"
  || typeof value === "number" && Number.isFinite(value);

export function isBackendPreparationProgram(value: unknown): value is BackendPreparationProgram {
  if (!exact(value, ["schema_version", "kind", "nodes", "outputs"])
    || value.schema_version !== 1 || value.kind !== "prompt_preparation_program"
    || !Array.isArray(value.nodes) || value.nodes.length > 128
    || !exact(value.outputs, ["prompt", "context"])) return false;
  const visited = new Set<string>();
  let chars = 0;
  for (const node of value.nodes) {
    if (!exact(node, ["node_id", "kind", "inputs", "config"], ["public_outputs", "output_ports"])
      || !nodeId(node.node_id) || visited.has(node.node_id) || !object(node.inputs) || !object(node.config)
      || Object.hasOwn(node, "public_outputs") && (!Array.isArray(node.public_outputs)
        || node.public_outputs.length > 4 || !node.public_outputs.every(id => typeof id === "string" && id.length <= 64))
      || Object.keys(node.inputs).length > 128
      || !Object.entries(node.inputs).every(([key, id]) =>
        /^[A-Za-z][A-Za-z0-9_-]{0,127}$/.test(key) && nodeId(id) && visited.has(id))) return false;
    const config = node.config;
    const inputs = node.inputs;
    const inputKeys = Object.keys(inputs);
    const input = () => inputKeys.length === 1 && inputKeys[0] === "input";
    const optionalText = () => inputKeys.length === 0 || inputKeys.length === 1 && inputKeys[0] === "text";
    const supported = node.kind === "context-source" ? ["context"]
      : ["session-data-read", "session-data-write"].includes(node.kind as string) ? ["json"]
        : ["regex", "variable-replace"].includes(node.kind as string) ? [config.mode]
          : ["prompt-source", "prompt-collector"].includes(node.kind as string)
            ? ["prompt", "tool-descriptions", "tool-schemas"]
            : ["global-source", "text-to-prompt"].includes(node.kind as string) ? ["prompt"]
              : node.kind === "text" && config.source === "root_input" ? ["current-input"] : ["text"];
    if (Object.hasOwn(node, "output_ports") && (!Array.isArray(node.output_ports)
      || !node.output_ports.length || node.output_ports.length > 4
      || new Set(node.output_ports).size !== node.output_ports.length
      || !node.output_ports.every(id => supported.includes(id)))) return false;
    if (Object.hasOwn(node, "public_outputs") && (new Set(node.public_outputs as string[]).size !== (node.public_outputs as string[]).length
      || !(node.public_outputs as string[]).every(id => (node.output_ports as unknown[] ?? supported).includes(id)))) return false;
    switch (node.kind) {
      case "text":
        if (inputKeys.length || !(exact(config, ["text"]) && text(config.text)
          || exact(config, ["source"]) && config.source === "root_input")) return false;
        break;
      case "prompt-source": {
        if (inputKeys.length || !exact(config, ["instances"]) || !Array.isArray(config.instances)
          || config.instances.length > 512) return false;
        const instances = new Set<string>();
        for (const instance of config.instances) {
          if (!exact(instance, ["group_instance_id", "item_instance_id"])
            || !(instance.group_instance_id === null || uuid(instance.group_instance_id))
            || !uuid(instance.item_instance_id)) return false;
          const key = `${instance.group_instance_id}:${instance.item_instance_id}`;
          if (instances.has(key)) return false;
          instances.add(key);
        }
        break;
      }
      case "context-source":
        if (inputKeys.length || !exact(config, [], ["binding_id"])
          || Object.hasOwn(config, "binding_id") && !uuid(config.binding_id)) return false;
        break;
      case "global-source":
        if (inputKeys.length || !exact(config, ["resource_id"], ["record"]) || !uuid(config.resource_id)
          || Object.hasOwn(config, "record") && (!isGlobalContent(config.record)
            || config.record.resource_id !== config.resource_id || !config.record.enabled)) return false;
        break;
      case "session-data-read":
      case "session-data-write":
        if (!exact(config, node.kind === "session-data-read" ? ["definition"] : ["definition", "value"])
          || !isDataDefinition(config.definition)
          || (node.kind === "session-data-read" ? inputKeys.length !== 0 : !optionalText())) return false;
        break;
      case "json-to-text":
        if (!input() || !exact(config, [])) return false;
        break;
      case "prompt-collector":
        if (!exact(config, ["input_order"]) || !Array.isArray(config.input_order)
          || config.input_order.length !== inputKeys.length
          || new Set(config.input_order).size !== inputKeys.length
          || !config.input_order.every((key) => typeof key === "string" && Object.hasOwn(inputs, key))) return false;
        break;
      case "regex":
        if (!input() || !exact(config, ["mode", "rule"]) || !["text", "prompt"].includes(config.mode as string)
          || !exact(config.rule, ["pattern", "replacement", "flags", "mode"])
          || !text(config.rule.pattern) || !text(config.rule.replacement)
          || config.rule.pattern.length + config.rule.replacement.length > 16_384
          || typeof config.rule.flags !== "string" || !/^[imsxa]*$/.test(config.rule.flags)
          || new Set(config.rule.flags).size !== config.rule.flags.length
          || !["first", "all"].includes(config.rule.mode as string)) return false;
        break;
      case "variable-register":
        if (!optionalText() || !exact(config, ["name", "type"], ["initial"]) || !name(config.name)
          || !["string", "integer", "number", "boolean"].includes(config.type as string)
          || Object.hasOwn(config, "initial") && !typed(config.initial, config.type)) return false;
        break;
      case "variable-assign":
        if (!optionalText() || !exact(config, ["name", "operation"], ["value"]) || !name(config.name)
          || !["set", "add", "subtract"].includes(config.operation as string)
          || !inputKeys.length && !Object.hasOwn(config, "value")
          || Object.hasOwn(config, "value") && !scalar(config.value)) return false;
        break;
      case "variable-replace":
        if (!input() || !exact(config, ["mode"]) || !["text", "prompt"].includes(config.mode as string)) return false;
        break;
      case "text-to-prompt":
        if (!input() || !exact(config, ["item_instance_id", "role", "placement", "depth", "order"], ["enabled"])
          || !uuid(config.item_instance_id) || !["system", "user", "assistant"].includes(config.role as string)
          || !["before", "middle", "after"].includes(config.placement as string)
          || !Number.isSafeInteger(config.order)
          || Object.hasOwn(config, "enabled") && typeof config.enabled !== "boolean"
          || !(config.placement === "middle" ? Number.isSafeInteger(config.depth) && (config.depth as number) >= 0
            : config.depth === null)) return false;
        break;
      case "prompt-to-text":
        if (!input() || !exact(config, ["separator"]) || !text(config.separator)) return false;
        break;
      default:
        return false;
    }
    chars += JSON.stringify(config).length;
    if (chars > 1_000_000) return false;
    visited.add(node.node_id);
  }
  return [value.outputs.prompt, value.outputs.context].every((id) => id === null || nodeId(id) && visited.has(id));
}
