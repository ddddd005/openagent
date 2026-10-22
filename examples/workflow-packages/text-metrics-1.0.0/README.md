# Text Metrics Local Package

Place this entire directory under the workflow package root:
`<main SQLite directory>/packages/workflow/text-metrics-1.0.0/`.
An explicit `--workflow-package-root` overrides that parent directory.
The package is discovered but is not enabled automatically.

Use the existing package selection command to add
`"example.text-metrics": "1.0.0"` while retaining the project's other exact
selections. No additional Python dependencies are required.

In a workflow with an Agent and native context assembly, add the
`example.text-metrics-tools@1` node, set its `agent_node_id` to that Agent,
and connect `materials` to the context assembly's `materials` input.
The Agent receives `text_metrics(text, include_spaces)` and its matching
parameter description. The result is a JSON object with `characters` and
`lines`. No variable registration or object binding is required for this tool.

The target `example.text-metrics@1` is also available as an ordinary node
with a `TEXT@2` input and `JSON@2` output.

This is trusted local Python code. Pure computation is an interface contract;
the host does not sandbox Python imports or arbitrary filesystem/network calls.
