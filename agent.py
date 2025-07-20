#!/usr/bin/env python3

import asyncio
import json
import platform
import os
import fire
from typing import Any

from metagpt.actions import Action
from metagpt.roles import Role
from metagpt.team import Team
from metagpt.schema import Message
from metagpt.const import USER_REQUIREMENT  # 导入标准的 UserRequirement

# ===================================================================
# 1. 动作 (Actions) - 按执行顺序列出
# ===================================================================

class PrepareTree(Action):
    """Action: 读取树文件内容，为下一步做准备。这是一个非 LLM 的工具性 Action。"""
    name: str = "PrepareTree"

    async def run(self, tree_file_path: str) -> str:
        print(f"🔩 Action: Reading tree file from '{tree_file_path}'...")
        try:
            with open(tree_file_path, 'r', encoding='utf-8') as f:
                return f.read()
        except FileNotFoundError:
            print(f"❌ ERROR: Tree file not found at {tree_file_path}")
            return ""

class ParseTreeToFilePaths(Action):
    """Action: 解析树状文本，提取所有文件路径。这是一个非 LLM 的工具性 Action。"""
    name: str = "ParseTreeToFilePaths"

    async def run(self, tree_text: str) -> list[str]:
        print("🌳 Action: Parsing tree text to file paths...")
        files = []
        path_stack = []
        # 假设第一行是根目录，我们从第二行开始解析
        lines = tree_text.strip().splitlines()[1:]

        for line in lines:
            depth = line.count('│   ') + line.count('    ')
            
            if "├── " in line:
                name = line.split("├── ")[-1]
            elif "└── " in line:
                name = line.split("└── ")[-1]
            else:
                continue

            while len(path_stack) > depth:
                path_stack.pop()

            # 简单的文件判断逻辑
            is_file = '.' in name and not name.startswith('.') and not line.strip().endswith('/')

            if is_file:
                full_path = os.path.join(*path_stack, name)
                files.append(full_path.replace('\\', '/'))
            else:
                path_stack.append(name)
        
        print(f"✅ Action: Found {len(files)} files.")
        return files

class AnalyzeSourceCode(Action):
    """Action: (LLM-based) 分析单个源文件以查找安全漏洞。"""
    PROMPT_TEMPLATE: str = """
You are a top-tier security expert analyzing the file `{file_path}`.

**Full Source Code**:
{source_code}

**Project Context**: 
The full project tree has been provided in earlier steps. This file is one of several key files selected for analysis. Use your expertise to identify potential security vulnerabilities within THIS file's source code. Pay special attention to:
- Input validation, SQL injection, XSS.
- Integer overflow/underflow, unsafe memory operations, race conditions.
- Hardcoded secrets, authorization issues, logical flaws.

Provide a concise and actionable report for `{file_path}` in markdown format. If no significant vulnerabilities are found, state that clearly.
"""
    name: str = "AnalyzeSourceCode"

    async def run(self, file_path: str, source_code: str) -> str:
        prompt = self.PROMPT_TEMPLATE.format(file_path=file_path, source_code=source_code)
        return await self._aask(prompt)

# ===================================================================
# 2. 角色 (Roles) - 按执行顺序列出
# ===================================================================

class Coordinator(Role):
    """角色1: 启动器。接收用户需求，并启动树文件准备工作。"""
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.set_actions([PrepareTree])
        self._watch([USER_REQUIREMENT]) # **关键**: 监听框架的初始消息

    async def _act(self) -> Message:
        print("--- Role: Coordinator starting ---")
        # 获取由 team.run(idea=...) 产生的初始消息
        idea = self.rc.news[0].content
        # 解析初始 idea (我们将其设计为 JSON 字符串)
        try:
            context = json.loads(idea)
            tree_file = context.get("tree_file")
        except (json.JSONDecodeError, AttributeError):
            raise ValueError("The initial idea for the Coordinator must be a valid JSON string with a 'tree_file' key.")
        
        # 运行自己的动作
        tree_text = await self.rc.todo.run(tree_file_path=tree_file)
        
        # **关键**: 发布新消息，触发下一个角色
        return Message(content=tree_text, cause_by=PrepareTree)


class TreeAnalyzer(Role):
    """角色2: 解析器。接收树文件内容，解析出文件列表。"""
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.set_actions([ParseTreeToFilePaths])
        self._watch([PrepareTree]) # **关键**: 监听上一个角色的动作

    async def _act(self) -> Message:
        print("--- Role: TreeAnalyzer starting ---")
        tree_text = self.rc.news[0].content
        file_list = await self.rc.todo.run(tree_text=tree_text)

        # 保存文件列表以供下一个角色使用
        os.makedirs("memory", exist_ok=True)
        with open("memory/risk.json", "w", encoding="utf-8") as f:
            json.dump(file_list, f, indent=2)

        # **关键**: 发布新消息，触发下一个角色
        return Message(content=json.dumps(file_list), cause_by=ParseTreeToFilePaths)

class SecurityExpert(Role):
    """角色3: 安全专家。接收文件列表并逐一分析。"""
    def __init__(self, repo_path: str, **kwargs):
        super().__init__(**kwargs)
        self.repo_path = repo_path
        self.set_actions([AnalyzeSourceCode])
        self._watch([ParseTreeToFilePaths]) # **关键**: 监听上一个角色的动作
        self.files_to_analyze = []
        self.analysis_reports = []

    async def _act(self) -> Message:
        print("--- Role: SecurityExpert starting ---")
        
        # 首次被激活时，从消息中加载待办列表
        if not self.files_to_analyze:
            file_list_json = self.rc.news[0].content
            self.files_to_analyze = json.loads(file_list_json)

        # 如果还有文件待分析
        if self.files_to_analyze:
            file_path = self.files_to_analyze.pop(0)
            print(f"🕵️  Analyzing file: {file_path} ({len(self.files_to_analyze)} remaining)")
            
            full_path = os.path.join(self.repo_path, file_path)
            try:
                with open(full_path, 'r', encoding='utf-8', errors='ignore') as f:
                    source_code = f.read()
                
                # 运行分析动作
                report = await self.rc.todo.run(file_path=file_path, source_code=source_code)
                self.analysis_reports.append(report)
            except Exception as e:
                error_report = f"### Analysis for {file_path}\n\n[ERROR] Failed to analyze file: {e}"
                self.analysis_reports.append(error_report)

            # **关键**: 给自己发消息以继续处理下一个文件
            # 如果还有文件，继续触发自己；否则，流程会自然结束
            if self.files_to_analyze:
                 return Message(content=json.dumps(self.files_to_analyze), cause_by=ParseTreeToFilePaths, send_to=self.name)

        # 所有文件分析完毕，发布最终报告
        print("✅ All files analyzed. Compiling final report.")
        final_report = "\n\n---\n\n".join(self.analysis_reports)
        return Message(content=final_report, cause_by=AnalyzeSourceCode, send_to="Director") # 假设有 Director 接收

class Director(Role):
    """角色4: 主管。接收并打印最终报告。"""
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._watch([AnalyzeSourceCode]) # **关键**: 监听 SecurityExpert 的最终动作

    async def _act(self) -> Message:
        print("--- Role: Director starting ---")
        final_report = self.rc.news[0].content
        print("\n======================= FINAL SECURITY REPORT =======================")
        print(final_report)
        print("=====================================================================")
        # 流程结束
        return Message(content="Analysis complete.", cause_by=self.rc.todo)


# ===================================================================
# 3. 主流程 (Pipeline)
# ===================================================================

async def pipeline(repo_path: str, tree_file: str, n_round: int = 20):
    """(重构版) 仅负责组建团队和启动流程。"""
    team = Team(
        roles=[
            Coordinator(name="Coordinator"),
            TreeAnalyzer(name="TreeAnalyzer"),
            SecurityExpert(name="SecurityExpert", repo_path=repo_path),
            Director(name="Director"),
        ]
    )

    # 将所有初始信息打包成一个 JSON 字符串作为 "idea"
    initial_context = {
        "repo_path": repo_path,
        "tree_file": tree_file
    }

    print("--- Starting Security Analysis Pipeline (v5 - Refactored) ---")
    
    await team.run(idea=json.dumps(initial_context), n_round=n_round)
    
    print("--- Security Analysis Pipeline Finished ---")

def main(repo_path: str, tree_file: str, n_round: int = 20):
    """CLI entry point."""
    if platform.system() == "Windows":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(pipeline(repo_path, tree_file, n_round))

if __name__ == "__main__":
    fire.Fire(main)
