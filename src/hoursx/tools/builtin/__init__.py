"""Built-in tools shipped with the platform."""

from hoursx.tools.builtin.code import register_code_tools
from hoursx.tools.builtin.delegate import register_delegate_tool
from hoursx.tools.builtin.fs import register_fs_tools
from hoursx.tools.builtin.git import register_git_tools
from hoursx.tools.builtin.recall import register_recall_tools
from hoursx.tools.builtin.shell import register_shell_tools
from hoursx.tools.builtin.web import register_web_tools
from hoursx.tools.registry import ToolRegistry


def register_builtin_tools(registry: ToolRegistry) -> None:
    """Register every built-in tool. Visibility per agent is decided later by
    profile grants; registration itself is unconditional and deterministic."""
    register_fs_tools(registry)
    register_shell_tools(registry)
    register_git_tools(registry)
    register_web_tools(registry)
    register_recall_tools(registry)
    register_code_tools(registry)
    register_delegate_tool(registry)
