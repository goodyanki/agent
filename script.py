# -*- coding: utf-8 -*-
"""
End-to-end pipeline:
1. Parse a Rust MIR (*.mir) text file.
2. Build a Code Property Graph (CFG + DFG) with rustworkx.
3. Vectorize each function graph with KarateClub's FeatherGraph.
4. Store vectors + metadata into Milvus Lite (local file DB) via pymilvus MilvusClient.

Tested with:
- rustworkx >= 0.17
- networkx >= 3.2
- karateclub >= 1.3
- pymilvus >= 2.4 (Milvus Lite)
- tqdm >= 4.0

Run:
    python rust_mir_cpg_pipeline_fixed.py --mir spl_single_pool.mir --db ./rust_cpg.db \
        --collection rust_cpg_collection --dim 128
"""

import re
import os
import argparse
from typing import Dict, List, Tuple, Optional, Any

import rustworkx as rx
import networkx as nx
from karateclub import FeatherGraph
from pymilvus import MilvusClient, DataType
import numpy as np
from tqdm import tqdm

# ----------------------------- 1. Configuration ----------------------------- #
class Config:
    def __init__(self,
                 mir_file_path: str,
                 milvus_db_path: str = "./rust_cpg.db",
                 milvus_collection_name: str = "rust_cpg_collection",
                 embedding_dim: int = 128):
        self.MIR_FILE_PATH = mir_file_path
        self.MILVUS_DB_PATH = milvus_db_path
        self.MILVUS_COLLECTION_NAME = milvus_collection_name
        self.EMBEDDING_DIMENSION = embedding_dim


# ------------------------------ 2. MIR Parser ------------------------------ #
class MIRParser:
    """Parse Rust MIR text into a nested dict of functions, blocks, statements.

    Expected (simplified) MIR structure:
        fn <name>(...) -> ... {
            let mut _0: ...;
            bb0: {
                _1 = ...;
                goto -> bb1;
            }
            bb1: {
                return;
            }
        }
    """

    _FUNC_START = re.compile(r"^\s*fn\s+([^\s(]+)")
    _DECL_RE = re.compile(r"^\s*let\s+(?:mut\s+)?(_\d+(?:\[.*?\]|\..*?)?):\s*(.*?);\s*$")
    _BLOCK_RE = re.compile(r"^\s*(bb\d+):")

    def __init__(self, mir_filepath: str):
        if not os.path.exists(mir_filepath):
            raise FileNotFoundError(f"MIR file not found: {mir_filepath}")
        with open(mir_filepath, "r", encoding="utf-8") as f:
            self.lines = f.readlines()
        self.functions: Dict[str, Dict[str, Any]] = {}

    def parse(self) -> Dict[str, Dict[str, Any]]:
        current: List[str] = []
        in_fn = False
        for line in self.lines:
            if self._FUNC_START.match(line):
                if in_fn and current:
                    self._parse_function_block(current)
                current = [line]
                in_fn = True
            elif in_fn:
                current.append(line)
        if in_fn and current:
            self._parse_function_block(current)
        return self.functions

    def _parse_function_block(self, lines: List[str]) -> None:
        m = self._FUNC_START.match(lines[0])
        if not m:
            return
        fn_name = m.group(1)

        declarations: List[Dict[str, str]] = []
        basic_blocks: Dict[str, Dict[str, Any]] = {}
        current_block: Optional[str] = None
        in_body = False

        for raw in lines:
            s = raw.strip()
            if not in_body and "{" in s:
                in_body = True
                continue
            if not in_body:
                continue
            if s == "}":
                break

            # Declarations
            dm = self._DECL_RE.match(s)
            if dm:
                declarations.append({
                    "local": dm.group(1),
                    "type": dm.group(2),
                    "raw": s
                })
                continue

            # Basic block start
            bm = self._BLOCK_RE.match(s)
            if bm:
                current_block = bm.group(1)
                basic_blocks[current_block] = {"statements": [], "terminator": None}
                continue

            if current_block is None or not s or s.startswith("//"):
                continue

            # Heuristic: consider the last line in a block that contains control keywords as terminator
            if any(tok in s for tok in ("->", "return;", "unreachable;", "resume;", "abort;")):
                basic_blocks[current_block]["terminator"] = {"raw": s}
            else:
                basic_blocks[current_block]["statements"].append({"raw": s})

        self.functions[fn_name] = {
            "declarations": declarations,
            "basic_blocks": basic_blocks
        }


# ----------------------------- 3. CPG Builder ------------------------------ #
class CPGBuilder:
    """Build a Code Property Graph with CFG + DFG edges from parsed MIR."""

    _ASSIGN_RE = re.compile(r"^\s*(_\d+(?:\[.*?\]|\..*?)?)\s*=\s*(.*);\s*$")
    _LOCAL_RE = re.compile(r"(_\d+)")

    # CFG terminator regexes
    _GOTO_RE = re.compile(r"goto\s*->\s*(bb\d+);")
    _SWITCH_RE = re.compile(r"switchInt.*?->\s*\[(.*?)\]", re.S)
    _CALL_RE = re.compile(r"->\s*\[return:\s*(bb\d+),\s*unwind:\s*(bb\d+)\];")

    def __init__(self, parsed_function_mir: Dict[str, Any]):
        self.parsed_mir = parsed_function_mir
        self.graph = rx.PyDiGraph(multigraph=True)
        self.node_map: Dict[Tuple[str, Any], int] = {}
        self.definitions: Dict[str, int] = {}

    def build_graph(self) -> rx.PyDiGraph:
        self._create_nodes()
        self._create_cfg_edges()
        self._create_dfg_edges()
        return self.graph

    def _create_nodes(self) -> None:
        for block, content in self.parsed_mir["basic_blocks"].items():
            for i, stmt in enumerate(content["statements"]):
                idx = self.graph.add_node({"type": "statement", "raw": stmt["raw"], "block": block})
                self.node_map[(block, i)] = idx
            if content["terminator"] is not None:
                idx = self.graph.add_node({"type": "terminator", "raw": content["terminator"]["raw"], "block": block})
                self.node_map[(block, "term")] = idx

    def _create_cfg_edges(self) -> None:
        for block, content in self.parsed_mir["basic_blocks"].items():
            term = content["terminator"]
            if term is None:
                continue
            src = self.node_map.get((block, "term"))
            if src is None:
                continue
            raw = term["raw"]

            # goto -> bbX
            mg = self._GOTO_RE.search(raw)
            if mg:
                self._add_cfg(src, mg.group(1), label="goto")

            # switchInt ... -> [0: bb1, otherwise: bb2]
            ms = self._SWITCH_RE.search(raw)
            if ms:
                # extract all bbNN tokens
                targets = re.findall(r"bb\d+", ms.group(1))
                for t in targets:
                    self._add_cfg(src, t, label="switch")

            # call -> [return: bbX, unwind: bbY]
            mc = self._CALL_RE.search(raw)
            if mc:
                for lab, bb in zip(["return", "unwind"], mc.groups()):
                    self._add_cfg(src, bb, label=lab)

    def _add_cfg(self, src_node: int, target_block: str, label: str) -> None:
        # Connect to first stmt of block, else to its terminator
        tgt = self.node_map.get((target_block, 0))
        if tgt is None:
            tgt = self.node_map.get((target_block, "term"))
        if tgt is not None:
            self.graph.add_edge(src_node, tgt, {"type": "CFG", "label": label})

    def _create_dfg_edges(self) -> None:
        # iterate blocks in numeric order
        sorted_blocks = sorted(self.parsed_mir["basic_blocks"].items(), key=lambda x: int(x[0][2:]))
        for block, content in sorted_blocks:
            # statements
            for i, stmt in enumerate(content["statements"]):
                node_id = self.node_map[(block, i)]
                self._process_dfg(stmt["raw"], node_id)
            # terminator
            if content["terminator"] is not None:
                node_id = self.node_map[(block, "term")]
                self._process_dfg(content["terminator"]["raw"], node_id)

    def _process_dfg(self, raw: str, node_id: int) -> None:
        m = self._ASSIGN_RE.match(raw)
        if m:
            target = m.group(1)
            base = self._LOCAL_RE.match(target).group(1) if self._LOCAL_RE.match(target) else None
            rhs = m.group(2)
            # uses
            for src_var in set(self._LOCAL_RE.findall(rhs)):
                if src_var in self.definitions:
                    self.graph.add_edge(self.definitions[src_var], node_id, {"type": "DFG"})
            # def
            if base:
                self.definitions[base] = node_id
        else:
            # non-assignment uses
            for src_var in set(self._LOCAL_RE.findall(raw)):
                if src_var in self.definitions:
                    self.graph.add_edge(self.definitions[src_var], node_id, {"type": "DFG"})


# ---------------------- 4. Vectorizer & Milvus Storer ----------------------- #
class VectorizerAndStorer:
    def __init__(self, config: Config):
        self.config = config
        # Milvus Lite: pass local file name as uri
        self.client = MilvusClient(uri=self.config.MILVUS_DB_PATH)

    # ---- Milvus helpers ---- #
    def setup_milvus_collection(self) -> None:
        name = self.config.MILVUS_COLLECTION_NAME
        if self.client.has_collection(name):
            return
        self.client.create_collection(
            collection_name=name,
            dimension=self.config.EMBEDDING_DIMENSION,
            primary_field_name="id",
            id_type=DataType.INT64,
            vector_field_name="vector",
            metric_type="L2",
            auto_id=True,
            enable_dynamic_field=True,
        )

    def insert_vector(self, vec: np.ndarray, metadata: Dict[str, Any]) -> Any:
        data = [{"vector": vec.tolist(), **metadata}]
        return self.client.insert(collection_name=self.config.MILVUS_COLLECTION_NAME, data=data)

    # ---- Embedding ---- #
    def vectorize_graph(self, graph: rx.PyDiGraph) -> Optional[np.ndarray]:
        if graph.num_nodes() == 0:
            return None
        # Convert to networkx DiGraph then undirected (FeatherGraph expects Graph)
        nx_g = nx.DiGraph()
        nx_g.add_nodes_from(graph.node_indices())
        for u, v, payload in graph.weighted_edge_list():  # payload is edge data dict
            nx_g.add_edge(u, v, **(payload if isinstance(payload, dict) else {"weight": payload}))
        # Ensure contiguous labels from 0
        if list(nx_g.nodes()) != list(range(nx_g.number_of_nodes())):
            nx_g = nx.convert_node_labels_to_integers(nx_g, first_label=0)
        undirected = nx_g.to_undirected()

        # --- FeatherGraph constructor compatibility layer ---
        try:
            model = FeatherGraph(dimensions=self.config.EMBEDDING_DIMENSION)  # 新 API
        except TypeError:
            try:
                model = FeatherGraph(dims=self.config.EMBEDDING_DIMENSION)    # 一些版本用 dims
            except TypeError:
                model = FeatherGraph()                                        # 老版本无参
        model.fit([undirected])
        emb = model.get_embedding()
        # emb 可能是 (1, d) 也可能是 (d,)
        emb = np.asarray(emb[0] if getattr(emb, "ndim", 1) == 2 else emb)
        target = self.config.EMBEDDING_DIMENSION
        if emb.shape[0] != target:
            emb = emb[:target] if emb.shape[0] > target else np.pad(emb, (0, target - emb.shape[0]))
        return emb



# ------------------------------ 5. Orchestration --------------------------- #

def process_functions(parsed: Dict[str, Dict[str, Any]], vs: VectorizerAndStorer) -> None:
    vs.setup_milvus_collection()
    for fn, fn_mir in tqdm(parsed.items(), desc="Processing Functions"):
        builder = CPGBuilder(fn_mir)
        graph = builder.build_graph()
        emb = vs.vectorize_graph(graph)
        if emb is None:
            tqdm.write(f"[skip] Empty graph for {fn}")
            continue
        meta = {
            "function_name": fn,
            "source_file": vs.config.MIR_FILE_PATH,
            "node_count": graph.num_nodes(),
            "edge_count": graph.num_edges(),
        }
        vs.insert_vector(emb, meta)


# --------------------------------- main ------------------------------------ #

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Rust MIR -> CPG -> Embedding -> Milvus")
    p.add_argument("--mir", required=True, help="Path to *.mir file")
    p.add_argument("--db", default="./rust_cpg.db", help="Milvus Lite DB file path")
    p.add_argument("--collection", default="rust_cpg_collection", help="Milvus collection name")
    p.add_argument("--dim", type=int, default=128, help="Embedding dimension")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    cfg = Config(args.mir, args.db, args.collection, args.dim)

    print(f"[1/4] Parsing MIR: {cfg.MIR_FILE_PATH}")
    parser = MIRParser(cfg.MIR_FILE_PATH)
    functions = parser.parse()
    print(f"    -> {len(functions)} functions found")

    print("[2/4] Connecting Milvus Lite & creating collection if needed")
    vs = VectorizerAndStorer(cfg)
    vs.setup_milvus_collection()

    print("[3/4] Building graphs, embedding & inserting into Milvus")
    process_functions(functions, vs)

    print("[4/4] Done.")


if __name__ == "__main__":
    main()
