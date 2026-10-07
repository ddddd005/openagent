"""Application composition for the installed trusted packages."""

from .content_contracts import create_content_package
from .prompt_package import create_prompt_package
from .tool_package import create_tool_package
from .workflow_tool_catalog import builtin_prompt_tool_catalog
from .model_package import create_model_package
from .context_package import create_context_package
from .context_compression_package import create_context_compression_package
from .agent_package import create_agent_package
from .frontend_business import create_frontend_business_package
from .frontend_package import create_frontend_package


DEFAULT_PACKAGES = {
    "workflow.tools": "1.0.0", "workflow.prompts": "1.0.0",
    "workflow.models": "1.0.0", "workflow.context": "1.0.0",
    "workflow.agents": "1.0.0", "workflow.context-compression": "1.0.0",
    "workflow.frontend-business": "1.0.0",
    "workflow.frontend": "1.0.0",
}


def builtin_capability_packages(*, runtime_fact_reader=None):
    return (create_content_package(), create_tool_package(),
            create_prompt_package(tool_catalog=builtin_prompt_tool_catalog()),
            create_model_package(), create_context_package(),
            create_agent_package(information_reader=runtime_fact_reader),
            create_context_compression_package(), create_frontend_business_package(), create_frontend_package())
