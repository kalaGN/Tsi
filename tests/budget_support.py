"""让已有行为替身使用回放预算协议，而不是吞掉新增守卫参数。"""

from app.evaluation.replay import ReplayRequestState
from app.services.llm.budget import default_request_guard
from app.services.llm.contracts import GenerationOptions


class BudgetedTestTurn:
    def __init__(self, turn, messages, tools, *, options=GenerationOptions(), request_guard=default_request_guard):
        self._turn = turn
        self._state = ReplayRequestState(messages, tools)
        self._guard = request_guard
        self.options = options

    def estimate_pending(self, tool_results=(), *, complete=True):
        return self._state.estimate(tool_results, complete=complete)

    async def next(self, tool_results=(), *, on_text_delta=None):
        self._guard(self.estimate_pending(tool_results))
        self._state.prepare(tool_results)
        step = await self._turn.next(tool_results, on_text_delta=on_text_delta)
        self._state.accept(step)
        return step

    def replace_tools(self, tools):
        self._state.tools = tuple(tools)
        replace = getattr(self._turn, "replace_tools", None)
        if replace is not None:
            replace(tools)

    async def aclose(self):
        close = getattr(self._turn, "aclose", None)
        if close is not None:
            await close()


def estimate_test_request(messages, tools, *, options=GenerationOptions()):
    return ReplayRequestState(messages, tools).estimate()
