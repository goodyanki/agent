#!/usr/bin/env python3

import asyncio
import json
import platform
from typing import Any, List
import os
import fire

from metagpt.actions import Action
from metagpt.schema import Message
from metagpt.roles import Role
from metagpt.team import Team

# ===================================================================
# 1. 动作 (Actions)
# ===================================================================

class HandOut(Action):
    """Action: The Coordinator issues the initial instruction."""
    PROMPT_TEMPLATE: str = """
You are the Hacker Director, coordinating a multi-agent vulnerability detection workflow.
Issue a clear requirement to the TreeAnalyzer based on the provided repository path: '{repo_path}'.
Your instruction should be: "The repository for analysis is located at '{repo_path}'. Please identify the key files that require security scrutiny."
"""
    name: str = "HandOut"

    async def run(self, repo_path: str) -> str:
        prompt = self.PROMPT_TEMPLATE.format(repo_path=repo_path)
        response = await self._aask(prompt)
        return response

class ExtractKeyFiles(Action):
    """Action: Simulate a human-hacker's intuition to identify high-risk files from a directory tree."""
    PROMPT_TEMPLATE: str = """
You are a senior security researcher simulating a human hacker. Your task is to review a project's directory tree to identify the most critical files for a security audit.

Directory Tree:
{tree}

Based on your intuition and experience with threat modeling, list the files that likely contain the most critical logic and therefore require deep security analysis first. Consider files related to authentication, payments, data handling, and core business logic.
Provide only the relative file paths, one per line.
"""
    name: str = "ExtractKeyFiles"

    async def run(self, tree: str) -> str:
        prompt = self.PROMPT_TEMPLATE.format(tree=tree)
        result = await self._aask(prompt)
        return result

class AnalyzeSourceAndAST(Action):
    """Action: Analyze a single source file with its Abstract Syntax Tree (AST) for security vulnerabilities."""
    PROMPT_TEMPLATE: str = """
You are a world-class security expert specializing in code auditing. Your task is to conduct a detailed security analysis of a specific source file, using its source code and the project-wide Abstract Syntax Tree (AST) for context.

**File Under Analysis**: `{file_path}`

**Full Source Code of the File**:
{source_code}

**Project-wide Abstract Syntax Tree (AST)**:
(This provides context on how this file interacts with the rest of the project, including function calls and data flow.)

**Your Mission**:
Analyze the provided **Source Code** for the file `{file_path}`. Identify potential security vulnerabilities, paying close attention to:
- Input validation flaws (e.g., SQL injection, command injection).
- Integer overflow/underflow, especially in financial or state-management logic.
- Unsafe memory operations or race conditions.
- Hardcoded secrets or insecure secret management.
- Authorization and authentication bypasses.
- Logical flaws that could be exploited.

Present your findings as a concise, actionable report for this specific file in markdown format. Start your report with `### Analysis for {file_path}`.
"""
    name: str = "AnalyzeSourceAndAST"

    async def run(self, file_path: str, source_code: str, ast_output: str) -> str:
        prompt = self.PROMPT_TEMPLATE.format(
            file_path=file_path,
            source_code=source_code,
            ast_output=ast_output
        )
        result = await self._aask(prompt)
        return result

class CombineReports(Action):
    """Action: Combine individual file analysis reports into a single, final report."""
    PROMPT_TEMPLATE: str = """
You are the Hacker Director. You have received a series of security analysis reports for individual files. Your task is to combine them into a single, comprehensive final report.

**Individual Analysis Reports**:
---
{reports}
---

**Your Mission**:
Synthesize all the provided reports into a single, well-structured markdown document. Create a brief executive summary at the beginning, then list the detailed findings for each file.
"""
    name: str = "CombineReports"

    async def run(self, reports: List[str]) -> str:
        report_str = "\n\n---\n\n".join(reports)
        prompt = self.PROMPT_TEMPLATE.format(reports=report_str)
        final_report = await self._aask(prompt)
        return final_report

# ===================================================================
# 2. 角色 (Roles)
# ===================================================================

class Coordinator(Role):
    """Coordinator: Accepts the initial repo_path and triggers the TreeAnalyzer."""
    def __init__(self, **data: Any):
        super().__init__(**data)
        self.set_actions([HandOut])
        self._watch([HandOut]) # Watches for its own action to start the process.

    async def _act(self) -> Message:
        # This role is triggered by the initial `team.run_project(repo_path)` call.
        repo_path = self.rc.news[-1].content
        print(f"🎬 [Coordinator] Received repository path: {repo_path}. Issuing initial instruction.")
        
        # Use the HandOut action to format the instruction.
        instruction = await self.rc.todo.run(repo_path=repo_path)
        
        # Create a message to kick off the TreeAnalyzer.
        msg = Message(
            content=instruction,
            role="instruction",
            sent_from=self.name,
            send_to="TreeAnalyzer"
        )
        # We don't need to add it to memory here, as it will be processed by the team.
        return msg

class TreeAnalyzer(Role):
    """TreeAnalyzer: Receives the directory tree, extracts key files, and triggers the SecurityExpert."""
    def __init__(self, **data: Any):
        super().__init__(**data)
        self.set_actions([ExtractKeyFiles])
        self._watch([HandOut]) # Watches for the instruction from the Coordinator.

    async def _act(self) -> Message:
        # This role is triggered by the message from the Coordinator.
        # For this script, we assume the tree content is passed in the next `run_project` call.
        tree_text = self.rc.news[-1].content
        print("🌳 [TreeAnalyzer] Received directory tree. Extracting key files...")
        
        key_files_str = await self.rc.todo.run(tree=tree_text)
        files = [line.strip() for line in key_files_str.splitlines() if line.strip()]

        # Save the list of key files for the SecurityExpert to use.
        os.makedirs("memory", exist_ok=True)
        with open("memory/risk.json", "w", encoding="utf-8") as f:
            json.dump(files, f, indent=2)
        
        print(f"✅ [TreeAnalyzer] Identified {len(files)} key files. Saved to memory/risk.json.")
        
        # Create a message to activate the SecurityExpert.
        msg = Message(
            content="Analysis required for files listed in memory/risk.json",
            role="analysis_request",
            sent_from=self.name,
            send_to="SecurityExpert"
        )
        return msg

class SecurityExpert(Role):
    """SecurityExpert: Analyzes a list of files one by one, using a stateful, round-by-round approach."""
    def __init__(self, repo_path: str, **data: Any):
        super().__init__(**data)
        self.repo_path = repo_path
        self.set_actions([AnalyzeSourceAndAST, CombineReports])
        self._watch([TreeAnalyzer]) # Watches for the trigger from the TreeAnalyzer.
        
        # State: These variables will persist across multiple activations of this role.
        self.files_to_analyze: List[str] = []
        self.analyses_results: List[str] = []
        self._is_initialized = False

    async def _act(self) -> Message:
        # This role will be activated repeatedly by the team's main loop until its work is done.
        
        # --- Initialization (runs only once on the first activation) ---
        if not self._is_initialized:
            print("🤖 [SecurityExpert] Activated. Initializing task list...")
            try:
                with open('memory/risk.json', 'r', encoding='utf-8') as f:
                    self.files_to_analyze = json.load(f)
                self._is_initialized = True
                print(f"🤖 [SecurityExpert] Successfully loaded {len(self.files_to_analyze)} files to analyze.")
            except FileNotFoundError:
                print("❌ [SecurityExpert] ERROR: memory/risk.json not found. Cannot proceed.")
                return Message(content="Error: risk.json not found.", role="error", send_to="Director")

        # --- Per-Round Processing ---
        if self.files_to_analyze:
            # Take ONE file from the list for this round.
            file_path = self.files_to_analyze.pop(0)
            total_files = len(self.analyses_results) + len(self.files_to_analyze) + 1
            current_file_num = len(self.analyses_results) + 1
            
            print(f"-> [SecurityExpert] Analyzing file {current_file_num}/{total_files}: {file_path}...")
            
            try:
                # For simplicity, we read the AST file every time. In a real-world scenario,
                # this could be loaded once during initialization if memory allows.
                with open('ast/ast_output.txt', 'r', encoding='utf-8') as f:
                    ast_output = f.read()
                
                full_path = os.path.join(self.repo_path, file_path)
                with open(full_path, 'r', encoding='utf-8', errors='ignore') as f:
                    source_code = f.read()

                # Execute the analysis for the single file.
                # Note: We are not awaiting `self.rc.todo.run()` here because this `_act` method
                # is not subscribed to an action. We directly call the action.
                single_analysis_result = await AnalyzeSourceAndAST().run(
                    file_path=file_path, source_code=source_code, ast_output=ast_output
                )
                self.analyses_results.append(single_analysis_result)
            
            except Exception as e:
                error_report = f"### Analysis for {file_path}\n\n[ERROR] Failed to analyze file: {e}"
                self.analyses_results.append(error_report)
            
            # The role's action for this round is complete. MetaGPT will run the next round.
            # No message needs to be returned here, as the role will just run again if its
            # work queue (`self.files_to_analyze`) is not empty.
            return

        # --- Completion (runs only once, after all files are analyzed) ---
        else:
            print("🤖 [SecurityExpert] All files analyzed. Compiling final report.")
            
            # Combine all individual reports into a single final report.
            final_report_text = await CombineReports().run(reports=self.analyses_results)
            
            # Create the final message for the Director.
            final_msg = Message(
                content=final_report_text,
                role="final_report",
                sent_from=self.name,
                send_to="Director"
            )
            return final_msg

class Director(Role):
    """Director: Listens for the final report from the SecurityExpert and presents it."""
    def __init__(self, **data: Any):
        super().__init__(**data)
        # This role does not perform actions, it only listens and concludes the process.
        self.set_actions([]) 
        self._watch([SecurityExpert]) # Watches for the final message from the SecurityExpert.

    async def _act(self) -> Message:
        msg = self.rc.news[-1]
        
        # Check if the message is the final report we are waiting for.
        if msg.role == "final_report":
            print("🎬 [Director] Received the final report from SecurityExpert.")
            report_content = msg.content
            
            print("\n======================= FINAL SECURITY REPORT =======================")
            print(report_content)
            print("=====================================================================")
            
            # Final message to signify the end of the process.
            final_msg = Message(content=report_content, role="conclusion", sent_from=self.name)
            return final_msg
        
        print(f"🎬 [Director] Received an intermediate message. Waiting for the final report.")
        return None # Do nothing and wait for the correct message.

# ===================================================================
# 3. 主流程 (Pipeline)
# ===================================================================

async def pipeline(repo_path: str, tree_file: str, n_round: int = 10):
    """
    Main pipeline to run the multi-agent security analysis.

    Args:
        repo_path: The local path to the code repository to be analyzed.
        tree_file: A text file containing the directory structure of the repository.
        n_round: The maximum number of rounds for the team to run. 
                 **This MUST be greater than the number of files to analyze** plus a few extra rounds for coordination.
    """
    # Create the roles
    coord = Coordinator(name="Coordinator")
    tree_agent = TreeAnalyzer(name="TreeAnalyzer")
    expert = SecurityExpert(name="SecurityExpert", repo_path=repo_path)
    direc = Director(name="Director")
    
    # Create the team and hire the roles
    team = Team(use_mgx=False)
    team.hire([coord, tree_agent, expert, direc])

    print("--- Starting Security Analysis Pipeline ---")
    
    # 1. Trigger the Coordinator with the repository path.
    # This starts the chain of events.
    team.run_project(repo_path, send_to="Coordinator")

    # 2. Provide the TreeAnalyzer with the directory tree it needs.
    with open(tree_file, 'r', encoding='utf-8') as f:
        tree_text = f.read()
    team.run_project(tree_text, send_to="TreeAnalyzer")

    # 3. Run the main event loop for a specified number of rounds.
    await team.run(n_round=n_round)
    
    print("--- Security Analysis Pipeline Finished ---")

def main(repo_path: str, tree_file: str, n_round: int = 10):
    """
    CLI entry point for the security analysis pipeline.

    Example:
    python your_script_name.py --repo_path="/path/to/my/project" --tree_file="tree.txt" --n_round=20
    """
    # Necessary for asyncio on Windows
    if platform.system() == "Windows":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    
    # Ensure the required input files exist before starting.
    if not os.path.isdir(repo_path):
        print(f"Error: Repository path not found at '{repo_path}'")
        return
    if not os.path.isfile(tree_file):
        print(f"Error: Tree file not found at '{tree_file}'")
        return

    asyncio.run(pipeline(repo_path, tree_file, n_round))

if __name__ == "__main__":
    fire.Fire(main)









