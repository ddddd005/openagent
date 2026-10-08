import { graphUuid, type GraphEntry, type GraphNodeType, type GraphSession } from "../../domain/workflowGraph";

export interface TavernLaunch {
  href: string | null;
  diagnosis: string;
}
const exactUuid = (value: unknown): value is string => graphUuid(value) && value === value.toLowerCase();
export function tavernChatLaunch(base: string, entry: GraphEntry | undefined, catalog: GraphNodeType[],
  availablePackages: readonly { package_id: string; version: string }[], session: GraphSession | null): TavernLaunch {
  const unavailable = (diagnosis: string): TavernLaunch => ({ href: null, diagnosis });
  if (!entry || entry.saved_revision < 1) return unavailable("保存酒馆工作流后打开");
  if (entry.pending) return unavailable("核实原请求后打开酒馆");
  const doc = entry.saved_document;
  if (!doc || doc.revision !== entry.saved_revision || doc.workflow_definition_id !== entry.document.workflow_definition_id
    || !exactUuid(doc.workflow_definition_id)) return unavailable("缺少精确的已保存定义");
  if (!doc.package_lock?.some(row => row.package_id === "workflow.tavern" && row.version === "1.2.0")
    || !availablePackages.some(row => row.package_id === "workflow.tavern" && row.version === "1.2.0"))
    return unavailable("酒馆需要 workflow.tavern@1.2.0，原定义不会自动升级");
  const inputs = doc.nodes.filter(row => row.component_id === "tools.current-input");
  if (inputs.length !== 1 || inputs[0]!.component_version !== "1"
    || typeof inputs[0]!.config.input_name !== "string" || !inputs[0]!.config.input_name)
    return unavailable("酒馆首版需要单个 TEXT 用户输入");
  if (doc.nodes.some(node => {
    const type = catalog.find(row => row.component_id === node.component_id && row.component_version === node.component_version);
    return !type?.executable || type.capabilities?.includes("external:read") && node !== inputs[0];
  })) return unavailable("工作流包含缺失节点或未适配的外部输入声明");
  const displays = doc.nodes.flatMap(row => (row.public_outputs ?? []).filter(portId =>
    catalog.some(type => type.component_id === row.component_id && type.component_version === row.component_version
      && type.executable && type.outputs.some(port => port.port_id === portId && port.data_type === "TAVERN_CHAT_DISPLAY"
        && (port.data_schema_version ?? 1) === 1))));
  if (displays.length !== 1) return unavailable("酒馆需要唯一显式公开的 TAVERN_CHAT_DISPLAY@1 展示输出");
  if (entry.session_id !== null && (!exactUuid(entry.session_id) || !session || session.workflow_session_id !== entry.session_id
    || session.workflow_definition_id !== doc.workflow_definition_id || session.definition_revision !== doc.revision))
    return unavailable("所选会话与已保存定义不一致，刷新或明确改绑后打开");
  try {
    const url = new URL(base);
    if (!["http:", "https:"].includes(url.protocol) || !["127.0.0.1", "localhost", "[::1]"].includes(url.hostname)
      || url.username || url.password || !["/", "/static/index.html"].includes(url.pathname))
      return unavailable("酒馆入口需要本机宿主地址");
    url.pathname = "/tavern/"; url.search = ""; url.hash = "";
    url.searchParams.set("graph_workflow", doc.workflow_definition_id);
    if (entry.session_id !== null) url.searchParams.set("graph_session", entry.session_id);
    return { href: url.href, diagnosis: "打开酒馆" };
  } catch { return unavailable("酒馆入口地址无效"); }
}
