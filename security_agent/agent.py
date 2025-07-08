#!/usr/bin/env python3
"""
Filename: security_agent.py
Created Date: Tuesday, July 8th 2025
Author: ChatGPT
@Modified By: ChatGPT, 2025-07-08. Added TreeAnalyzer agent and ExtractKeyFiles action.
"""

import asyncio
import platform
from typing import Any

import fire
from metagpt.actions import Action
from metagpt.schema import Message
from metagpt.roles import Role
from metagpt.team import Team


class HandOut(Action):
    """Action: read user repo_path and compose initial instruction for Director."""
    PROMPT_TEMPLATE: str = """
Based on the provided repository path: {repo_path},
please generate an instruction for the Hacker Director:
1. You are the Hacker Director, coordinating the multi-agent vulnerability detection workflow.
2. Issue a clear requirement to the Analyser in the format:
   "The requirement is in directory '{repo_path}'. You are a professional Rust analyser;
    please analyze the code for potential security vulnerabilities and highlight key files
    needing further inspection."
"""
    name: str = "HandOut"

    async def run(self, context: str, repo_path: str) -> str:
        prompt = self.PROMPT_TEMPLATE.format(repo_path=repo_path)
        response = await self._aask(prompt)
        return response


class SpeakAloud(Action):
    """Action: speak out loud the given context from one role to another."""
    PROMPT_TEMPLATE: str = """
## CONTEXT
{context}

## YOUR TURN
You are {name}. Speak this instruction clearly to {opponent_name}:
"""
    name: str = "SpeakAloud"

    async def run(self, context: str, name: str, opponent_name: str) -> str:
        prompt = self.PROMPT_TEMPLATE.format(
            context=context, name=name, opponent_name=opponent_name
        )
        response = await self._aask(prompt)
        return f"[{name}] {response}"


class AnalyseTree(Action):
    """Action: scan the project tree for vulnerabilities and key files."""
    PROMPT_TEMPLATE: str = """
You are a professional Rust security analyser. Here is the project tree to review:

{tree}

Please:
1. Identify potential security issues (e.g. unsafe block misuse, concurrency flaws, panic propagation, dependency CVEs).
2. List the key files requiring deeper inspection, with a brief rationale for each.
3. Summarize your findings in concise bullet points.
"""
    name: str = "AnalyseTree"

    async def run(self, context: str, tree: str) -> str:
        prompt = self.PROMPT_TEMPLATE.format(tree=tree)
        result = await self._aask(prompt)
        return result


class ExtractKeyFiles(Action):
    """Action: simulate human-hacker reasoning over the tree to pick key files."""
    PROMPT_TEMPLATE: str = """
You are simulating a human hacker reviewing a project's directory tree. Here is the tree:

{tree}

Based on hacker intuition and threat modeling, list the files that require deep security analysis first.
Provide only the relative file paths, one per line.
"""
    name: str = "ExtractKeyFiles"

    async def run(self, context: str, tree: str) -> str:
        prompt = self.PROMPT_TEMPLATE.format(tree=tree)
        result = await self._aask(prompt)
        return result


class Coordinator(Role):
    """Coordinator: accepts repo_path and dispatches HandOut."""
    def __init__(self, **data: Any):
        super().__init__(**data)
        self.set_actions([HandOut])
        self._watch([HandOut])

    async def _observe(self) -> int:
        await super()._observe()
        return len(self.rc.news)

    async def _act(self):
        todo = self.rc.todo               # HandOut Action instance
        repo_path = self.rc.news[-1].content
        instruction = await todo.run(context="", repo_path=repo_path)
        msg = Message(
            content=instruction,
            role="coordination",
            sent_from=self.name,
            send_to="Director"
        )
        self.rc.memory.add(msg)
        return msg


class TreeAnalyzer(Role):
    """TreeAnalyzer: reads the tree text, extracts key files, then speaks aloud."""
    def __init__(self, **data: Any):
        super().__init__(**data)
        self.set_actions([ExtractKeyFiles, SpeakAloud])
        self._watch([ExtractKeyFiles, SpeakAloud])

    async def _observe(self) -> int:
        await super()._observe()
        return len(self.rc.news)

    async def _act(self):
        todo = self.rc.todo
        tree_text = self.rc.news[-1].content

        if isinstance(todo, ExtractKeyFiles):
            key_list = await todo.run(context="", tree=tree_text)
            # 先把关键文件列表发给 Director
            msg = Message(
                content=key_list,
                role="keyfiles",
                sent_from=self.name,
                send_to="Director"
            )
            self.rc.memory.add(msg)
            # 紧接着朗读一遍
            speaker = SpeakAloud()
            spoken = await speaker.run(
                context=key_list, name=self.name, opponent_name="Director"
            )
            msg2 = Message(
                content=spoken,
                role="keyfiles-spoken",
                sent_from=self.name,
                send_to="Director"
            )
            self.rc.memory.add(msg2)
            return msg2
        return None


class Director(Role):
    """Hacker Director: instructs Analyser and finally reports to user."""
    def __init__(self, **data: Any):
        super().__init__(**data)
        self.set_actions([SpeakAloud])
        self._watch([HandOut, SpeakAloud])

    async def _observe(self) -> int:
        await super()._observe()
        return len(self.rc.news)

    async def _act(self):
        todo = self.rc.todo            # SpeakAloud Action instance
        context = self.rc.news[-1].content

        # Coordinator → Director 转述给 Analyser
        if self.rc.news[-1].sent_from == "Coordinator" or self.rc.news[-1].sent_from == "TreeAnalyzer":
            spoken = await todo.run(
                context=context, name=self.name, opponent_name="Analyser"
            )
            msg = Message(
                content=spoken,
                role="direction",
                sent_from=self.name,
                send_to="Analyser"
            )
            self.rc.memory.add(msg)
            return msg

        # Analyser → Director 最终报告给 User
        elif self.rc.news[-1].sent_from == "Analyser":
            report = await todo.run(
                context=context, name=self.name, opponent_name="User"
            )
            print("\nSecurity Analysis Report:\n", report)
            msg = Message(
                content=report,
                role="report",
                sent_from=self.name,
                send_to="User"
            )
            self.rc.memory.add(msg)
            return msg


class Analyser(Role):
    """Rust Analyser: runs AnalyseTree then SpeakAloud."""
    def __init__(self, **data: Any):
        super().__init__(**data)
        self.set_actions([AnalyseTree, SpeakAloud])
        self._watch([AnalyseTree, SpeakAloud])

    async def _observe(self) -> int:
        await super()._observe()
        return len(self.rc.news)

    async def _act(self):
        todo = self.rc.todo
        last_content = self.rc.news[-1].content

        # AnalyseTree → 生成分析结果并发给 Director
        if isinstance(todo, AnalyseTree):
            analysis = await todo.run(context="", tree=last_content)
            msg = Message(
                content=analysis,
                role="analysis",
                sent_from=self.name,
                send_to="Director"
            )
            self.rc.memory.add(msg)
            # SpeakAloud
            speaker = SpeakAloud()
            spoken = await speaker.run(
                context=analysis, name=self.name, opponent_name="Director"
            )
            msg2 = Message(
                content=spoken,
                role="analysis-spoken",
                sent_from=self.name,
                send_to="Director"
            )
            self.rc.memory.add(msg2)
            return msg2
        return None


async def pipeline(repo_path: str, tree_file: str, n_round: int = 8):
    coord = Coordinator(name="Coordinator")
    tree_agent = TreeAnalyzer(name="TreeAnalyzer")
    direc = Director(name="Director")
    analy = Analyser(name="Analyser")
    team = Team(use_mgx=False)
    team.hire([coord, tree_agent, direc, analy])

    # 1) 发 repo_path 给 Coordinator
    team.run_project(repo_path, send_to="Coordinator")

    # 2) 读取 tree_file，并发给 TreeAnalyzer
    with open(tree_file, 'r', encoding='utf-8') as f:
        tree_text = f.read()
    team.run_project(tree_text, send_to="TreeAnalyzer")

    # 3) 异步多轮，完成 HandOut→Director→AnalyseTree→TreeAnalyzer→Director 全链路
    await team.run(n_round=n_round)


def main(repo_path: str, tree_file: str, n_round: int = 8):
    """CLI entry: specify repo_path, tree_file, and optional n_round."""
    if platform.system() == "Windows":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(pipeline(repo_path, tree_file, n_round))


if __name__ == "__main__":
    fire.Fire(main)
