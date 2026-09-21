"""旧入口兼容：工作区变更追踪已移至 Runtime。"""

from app.runtime.workspace_changes import AppliedChangeTracker

__all__ = ["AppliedChangeTracker"]
