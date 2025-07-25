# new_milvus.py

import asyncio
import json
import os
import uuid
from typing import List
from enum import Enum

from dotenv import load_dotenv
from metagpt.actions import Action
from metagpt.environment import Environment
from metagpt.llm import LLM
from metagpt.roles import Role
from metagpt.schema import Message
from metagpt.team import Team

from pymilvus import connections, Collection, utility
from sentence_transformers import SentenceTransformer

# --- 0. Load environment variables ---
load_dotenv()

# --- 1. RAG Manager using Milvus ---
class RAGManager:
    def __init__(self):
        host = os.getenv("VECTOR_DB_PATH")
        collection_name = os.getenv("VECTOR_DB_COLLECTION_NAME")

        if not host or not collection_name:
            raise ValueError("VECTOR_DB_PATH and VECTOR_DB_COLLECTION_NAME must be set in .env")

        connections.connect(alias="default", host=host, port="19530")
        if not utility.has_collection(collection_name):
            raise ValueError(f"Milvus collection '{collection_name}' does not exist.")

        self.collection = Collection(collection_name)
        self.model = SentenceTransformer("all-MiniLM-L6-v2")

        print(f"RAGManager initialized. Connected to Milvus collection '{collection_name}'.")

    def query(self, query_text: str, n_results: int = 5) -> List[str]:
        try:
            vector = self.model.encode([query_text])
            search_params = {"metric_type": "L2", "params": {"nprobe": 10}}

            results = self.collection.search(
                data=vector,
                anns_field="embedding",
                param=search_params,
                limit=n_results,
                output_fields=["document"]
            )

            documents = []
            for hits in results:
                for hit in hits:
                    documents.append(hit.entity.get("document", ""))

            return documents
        except Exception as e:
            print(f"[RAGManager] Error querying Milvus: {e}")
            return []

# --- 2. Custom message types ---
class PotentialVulnerability(Message):
    vulnerability_type: str
    mir_evidence: List[str]
    proposer_reasoning: str
    severity_guess: str = "Medium"

class ValidationStatus(str, Enum):
    CONFIRMED = "CONFIRMED"
    REFUTED = "REFUTED"
    NEEDS_INFO = "NEEDS_INFO"

class ValidationResult(Message):
    status: ValidationStatus
    validator_reasoning: str
    counter_evidence: List[str] = []
    original_vulnerability_type: str

# --- 3. Agent actions ---
class ProposeVulnerability(Action):
    PROMPT_TEMPLATE = """
    You are an expert offensive security researcher specializing in Solana smart contracts written in Rust.
    Your current task is to analyze a Rust project's MIR (Mid-level Intermediate Representation) to find vulnerabilities.
    You will be given a specific vulnerability class to focus on.
    Formulate a search query describing low-level MIR patterns to look for.
    Vulnerability Class: "{vuln_class}"
    """

    ANALYSIS_PROMPT_TEMPLATE = """
    You are an expert offensive security researcher. Analyze the following MIR snippets related to: "{vuln_class}".

    Evidence:
    ---
    {evidence}
    ---

    Decide if the evidence shows a vulnerability. If so, return JSON:
    {{
        "vulnerability_type": "{vuln_class}",
        "proposer_reasoning": "...",
        "severity_guess": "High/Medium/Low"
    }}
    Else return {{}}.
    """

    async def run(self, vuln_class: str) -> Message:
        prompt = self.PROMPT_TEMPLATE.format(vuln_class=vuln_class)
        rag_query = await self._aask(prompt)
        print(f"Proposer generated RAG query: {rag_query}")

        rag_manager: RAGManager = self.environment.get_profile(RAGManager)
        evidence = rag_manager.query(rag_query)

        if not evidence:
            return Message(content=f"No evidence for {vuln_class}")

        evidence_str = "\n---\n".join(evidence)
        analysis_prompt = self.ANALYSIS_PROMPT_TEMPLATE.format(vuln_class=vuln_class, evidence=evidence_str)
        response_text = await self._aask(analysis_prompt)

        try:
            json_part = response_text[response_text.find('{'):response_text.rfind('}')+1]
            hypothesis_data = json.loads(json_part)
            if not hypothesis_data:
                return Message(content="No vulnerability detected.")

            return PotentialVulnerability(
                vulnerability_type=hypothesis_data["vulnerability_type"],
                mir_evidence=evidence,
                proposer_reasoning=hypothesis_data["proposer_reasoning"],
                severity_guess=hypothesis_data.get("severity_guess", "Medium")
            )
        except Exception as e:
            return Message(content=f"Parsing error: {e}\nRaw: {response_text}")

class ValidateProposal(Action):
    PROMPT_TEMPLATE = """
    You are a defensive auditor. Evaluate the following hypothesis:

    Vulnerability: {vulnerability_type}
    Reasoning: {proposer_reasoning}
    Evidence:
    ---
    {evidence}
    ---

    Determine whether it is CONFIRMED or REFUTED. Return JSON:
    {{
        "status": "CONFIRMED" or "REFUTED",
        "validator_reasoning": "..."
    }}
    """

    async def run(self, message: PotentialVulnerability) -> ValidationResult:
        evidence_str = "\n---\n".join(message.mir_evidence)
        prompt = self.PROMPT_TEMPLATE.format(
            vulnerability_type=message.vulnerability_type,
            proposer_reasoning=message.proposer_reasoning,
            evidence=evidence_str
        )
        response_text = await self._aask(prompt)
        try:
            json_part = response_text[response_text.find('{'):response_text.rfind('}')+1]
            validation_data = json.loads(json_part)
            return ValidationResult(
                status=ValidationStatus(validation_data["status"]),
                validator_reasoning=validation_data["validator_reasoning"],
                original_vulnerability_type=message.vulnerability_type
            )
        except Exception as e:
            return ValidationResult(
                status=ValidationStatus.REFUTED,
                validator_reasoning=f"Parsing failed: {e}\nRaw: {response_text}",
                original_vulnerability_type=message.vulnerability_type
            )

# --- 4. Roles ---
class SolanaVulnerabilityProposer(Role):
    def __init__(self, name="Alice", profile="Offensive Security Researcher", **kwargs):
        super().__init__(name=name, profile=profile, **kwargs)
        self.set_actions([ProposeVulnerability])
        self.vulnerability_checklist = ["Missing Signature Check", "Unchecked Arithmetic"]
        self.investigation_idx = 0

    async def _act(self) -> Message:
        if self.investigation_idx >= len(self.vulnerability_checklist):
            return Message(content="FINISH")
        vuln_class = self.vulnerability_checklist[self.investigation_idx]
        self.investigation_idx += 1
        result = await self.actions[0].run(vuln_class=vuln_class)
        self.rc.memory.add(result)
        return result

class ExploitabilityValidator(Role):
    def __init__(self, name="Bob", profile="Defensive Code Auditor", **kwargs):
        super().__init__(name=name, profile=profile, **kwargs)
        self.set_actions([ValidateProposal])
        self.watch({PotentialVulnerability})

    async def _act(self) -> Message:
        todo = self.rc.todo
        if not todo:
            return await super()._act()
        message = self.rc.memory.get(k=1)[0]
        if not isinstance(message, PotentialVulnerability):
            return await super()._act()
        result = await self.actions[0].run(message=message)
        self.rc.memory.add(result)
        return result

# --- 5. Report Generator ---
class GenerateReport(Action):
    async def run(self, messages: List[Message]) -> str:
        confirmed = []
        proposals = {m.vulnerability_type: m for m in messages if isinstance(m, PotentialVulnerability)}
        validations = {m.original_vulnerability_type: m for m in messages if isinstance(m, ValidationResult)}

        for k, v in validations.items():
            if v.status == ValidationStatus.CONFIRMED and k in proposals:
                proposal = proposals[k]
                confirmed.append({
                    "vulnerability_id": str(uuid.uuid4()),
                    "title": k,
                    "status": "CONFIRMED",
                    "severity": proposal.severity_guess,
                    "evidence": proposal.mir_evidence,
                    "reasoning": {
                        "proposer": proposal.proposer_reasoning,
                        "validator": v.validator_reasoning
                    }
                })

        with open("risk.json", "w") as f:
            json.dump({"findings": confirmed}, f, indent=2)

        return json.dumps({"findings": confirmed}, indent=2)

# --- 6. Main entry ---
async def main():
    env = Environment()
    env.add_profile(profile=RAGManager())

    team = Team(
        investment=10000,
        environment=env,
        roles=[SolanaVulnerabilityProposer(), ExploitabilityValidator()]
    )

    await team.run("Start vulnerability investigation")

    report = await GenerateReport().run(messages=env.memory.get())
    print("\n--- Final Report ---\n")
    print(report)

if __name__ == "__main__":
    asyncio.run(main())
