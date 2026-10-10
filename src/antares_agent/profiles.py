from dataclasses import dataclass
from typing import Literal

PermissionMode = Literal["plan", "auto"]

ORCHESTRATION = """你是多仓库工作区的编排者。
先读取工作区索引及相关仓库的 AGENTS.md。
复杂任务先调研、明确契约，再修改和验证；小任务直接完成。
可以派遣子 Agent；汇总前必须等待子任务完成。
需要用户决定时用普通中文消息提问并结束本轮，等用户回复后继续。
新技能写到工作区或仓库的 .agents/skills/<name>/SKILL.md。
"""


@dataclass(frozen=True)
class Profile:
    name: str
    description: str = ""
    permission_mode: PermissionMode = "auto"


PROFILES = {
    "auto": Profile("auto", "直接执行。", "auto"),
    "plan": Profile("plan", "先制定计划。", "plan"),
}
