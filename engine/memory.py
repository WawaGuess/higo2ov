from abc import ABC, abstractmethod


class MemoryEngine(ABC):
    """记忆引擎抽象接口，后续替换为真实实现。"""

    @abstractmethod
    async def generate_memory(self, session_id: str, messages: list[dict]) -> str:
        """根据会话历史生成记忆摘要。"""
        ...


class PlaceholderMemoryEngine(MemoryEngine):
    """占位符实现，返回固定格式的记忆摘要。"""

    async def generate_memory(self, session_id: str, messages: list[dict]) -> str:
        return f"[memory] session={session_id} placeholder summary"
