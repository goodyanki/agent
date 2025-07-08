from __future__ import annotations


import asyncio
import json
import platform
import re
from pathlib import Path
from typing import Any, ClassVar, Dict, List, Type

import fire
from metagpt.actions import Action, UserRequirement
from metagpt.llm import LLM
from metagpt.logs import logger
from metagpt.roles import Role
from metagpt.schema import Message
from metagpt.team import Team

INLINE_SNIPPET = r"""
use std::ptr;

/// Copy `len` bytes from `src` into a new Vec<u8>`.
pub fn copy_bytes(src: &[u8], len: usize) -> Vec<u8> {
    let mut dst: Vec<u8> = Vec::with_capacity(len);
    unsafe {
        // BUG: unchecked copy can read OOB & leave uninitialized memory
        ptr::copy_nonoverlapping(src.as_ptr(), dst.as_mut_ptr(), len);
        dst.set_len(len);
    }
    dst
}
"""

# ---------------------------------------------------------------------------
# 1. Action
# ---------------------------------------------------------------------------
class HumanHackerInsight(Action):
    name: str = "HumanHackerInsight"

    async def run(self, context: Dict[str, Any]) -> Dict[str, Any]:
        code = context.get("code", "")
        lang = context.get("language", "unknown")
        heur = self._heuristic_scan(code)
        llm = await self._llm_attack_mindset(code, lang)
        return self._merge(heur, llm)

    # heuristic + llm same as before … (omitted for brevity)
    # --- keep existing _heuristic_scan, _llm_attack_mindset, _merge implementations ---

    def _heuristic_scan(self, code: str) -> Dict[str, Any]:
        patterns = {
            r"transfer\([^,]+,\s*[^,]+\)": "Insecure Ether transfer",
            r"unchecked\s*\{": "Unchecked Solidity block",
            r"\bunsafe\b": "Use of `unsafe` (Rust)",
        }
        vulns: List[Dict[str, Any]] = []
        for pat, desc in patterns.items():
            for m in re.finditer(pat, code, flags=re.MULTILINE):
                line_no = code[: m.start()].count("\n") + 1
                vulns.append({"line": line_no, "type": desc, "detail": code.splitlines()[line_no-1].strip()})
        return {"vulnerabilities": vulns}

    async def _llm_attack_mindset(self, code: str, lang: str) -> Dict[str, Any]:
        prompt = (
            "You are a senior Rust security auditor specializing in memory safety and unsafe usage.\n"
            "Analyse the following Rust code for real exploitable vulnerabilities … (prompt unchanged)\n\n"
            f"Language: rust\nCode:\n{code}\n"
        )
        rsp = (await LLM().aask(prompt)).strip()
        if rsp.startswith("```"):  # strip fences
            rsp = re.sub(r"^```\w*|```$", "", rsp, flags=re.DOTALL).strip()
        try:
            return json.loads(rsp)
        except json.JSONDecodeError:
            return {"error": "Non‑JSON response", "raw": rsp}

    @staticmethod
    def _merge(heur: Dict[str, Any], llm: Dict[str, Any]) -> Dict[str, Any]:
        if "error" in llm:
            return {**heur, **llm}
        return {
            "vulnerabilities": heur.get("vulnerabilities", []) + llm.get("vulnerabilities", []),
            "attack_paths": llm.get("attack_paths", []),
            "recommendations": llm.get("recommendations", []),
        }

# ---------------------------------------------------------------------------
# 2. Role (unchanged)
# ---------------------------------------------------------------------------
class HumanHackerAgent(Role):
    name: str = "HackerGPT"
    profile: str = "Offensive Security Analyst"
    goal: str = "Uncover exploitable weaknesses in code samples."
    allowed_actions: ClassVar[List[Type[Action]]] = [HumanHackerInsight]
    # observe/act logic same as before …

# ---------------------------------------------------------------------------
# 3. CLI — add --inline flag
# ---------------------------------------------------------------------------
INVESTMENT, ROUNDS = 3.0, 1

def run_team(code_str: str, lang: str):
    agent = HumanHackerAgent()
    team = Team(use_mgx=False)
    team.hire([agent])
    team.invest(INVESTMENT)
    idea = json.dumps({"code": code_str, "language": lang})
    team.run_project(idea, send_to=agent.name)
    return team

async def audit(path: str, lang: str, inline: bool):
    code_str = INLINE_SNIPPET if inline else Path(path).read_text("utf-8")
    print("[DEBUG] Using inline=" + str(inline))
    print("[DEBUG] First 120 chars:\n", code_str[:120])
    team = run_team(code_str, lang)
    await team.run(n_round=ROUNDS)


def main(path: str = "vuln.rs", lang: str = "rust", inline: bool = False):
    if not inline and not Path(path).exists():
        raise SystemExit(f"[!] File not found: {path}")
    if platform.system() == "Windows":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(audit(path, lang, inline))


if __name__ == "__main__":
    fire.Fire(main)
