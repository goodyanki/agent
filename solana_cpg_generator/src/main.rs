#![feature(rustc_private)]

extern crate rustc_driver;

// 导入必要的模块
use clap::Parser as ClapParser;
use petgraph::dot::{Config, Dot};
use petgraph::graph::{DiGraph, NodeIndex};
use rustc_driver::{Callbacks, Compilation};
use rustc_interface::{interface, Queries};
use rustc_middle::mir::{self, Rvalue, StatementKind, TerminatorKind};
use rustc_middle::ty::TyCtxt;
use std::collections::HashMap;
use std::fmt::{self, Display, Formatter};
use std::process::Command;

/// 定义我们工具的命令行参数
#[derive(ClapParser, Debug)]
#[command(author, version, about, long_about = None)]
struct Args {
    /// 要分析的Solana项目crate的路径 (例如 ./single-pool/program)
    #[arg(value_name = "CRATE_PATH")]
    crate_path: String,
}

// --- CPG 数据结构定义 ---

/// CPG中的节点，代表一条MIR指令或终结符
#[derive(Debug, Clone)]
struct CpgNode {
    // MIR指令的文本表示，用于可视化
    label: String,
    // 指令在MIR中的位置 (哪个基本块, 第几条语句)
    location: mir::Location,
}

/// CPG中的边，区分为控制流或数据流
#[derive(Debug, Clone, Copy)]
enum EdgeType {
    ControlFlow,
    DataFlow,
}

// 为EdgeType实现Display trait，以便在.dot文件中显示为标签
impl Display for EdgeType {
    fn fmt(&self, f: &mut Formatter<'_>) -> fmt::Result {
        match self {
            EdgeType::ControlFlow => write!(f, "CFG"),
            EdgeType::DataFlow => write!(f, "DFG"),
        }
    }
}

// --- 编译器回调与分析逻辑 ---

struct CpgCallback;

impl Callbacks for CpgCallback {
    fn after_analysis<'tcx>(
        &mut self,
        _compiler: &interface::Compiler,
        queries: &'tcx Queries<'tcx>,
    ) -> Compilation {
        queries.global_ctxt().unwrap().enter(|tcx| {
            println!("\n✅ 成功进入编译器上下文，开始分析...");
            analyze_crate(tcx);
        });
        Compilation::Continue
    }
}

/// 主分析函数，遍历Crate中的所有函数
fn analyze_crate(tcx: TyCtxt<'_>) {
    for item_def_id in tcx.hir().body_owners() {
        let function_path = tcx.def_path_str(item_def_id);
        println!("\n--- 正在分析函数: {} ---", function_path);

        let mir_body = tcx.optimized_mir(item_def_id);
        let cpg = build_cpg_for_function(mir_body);

        // 为生成的图生成DOT文件用于可视化
        let dot_content = format!(
            "{:?}",
            Dot::with_config(&cpg, &[Config::EdgeNoLabel])
        );
        
        // 此处可以添加保存 .dot 和 .json 文件的逻辑
        // 为了简化，我们直接打印DOT内容
        println!("--- DOT Representation for {} ---", function_path);
        println!("{}", dot_content);
        println!("--- End of DOT ---");
    }
}

/// 为单个函数构建CPG（包含CFG和DFG）
fn build_cpg_for_function(mir: &mir::Body<'_>) -> DiGraph<CpgNode, EdgeType> {
    let mut cpg = DiGraph::<CpgNode, EdgeType>::new();
    // 映射: MIR位置 -> CPG节点索引
    let mut node_map: HashMap<mir::Location, NodeIndex> = HashMap::new();

    // --- 阶段 A: 创建节点 ---
    // 遍历所有基本块和其中的语句，为每个MIR指令创建一个CPG节点
    for (block_id, block_data) in mir.basic_blocks.iter_enumerated() {
        for (statement_index, statement) in block_data.statements.iter().enumerate() {
            let location = mir::Location {
                block: block_id,
                statement_index,
            };
            let node = CpgNode {
                label: format!("{:?}", statement),
                location,
            };
            let node_index = cpg.add_node(node);
            node_map.insert(location, node_index);
        }
        // 也为终结符创建节点
        let location = mir::Location {
            block: block_id,
            statement_index: block_data.statements.len(),
        };
        let node = CpgNode {
            label: format!("{:?}", block_data.terminator()),
            location,
        };
        let node_index = cpg.add_node(node);
        node_map.insert(location, node_index);
    }

    // --- 阶段 B: 构建CFG和DFG边 ---
    // `last_def` 追踪每个变量（mir::Local）最后被定义的位置
    let mut last_def: HashMap<mir::Local, NodeIndex> = HashMap::new();

    for (block_id, block_data) in mir.basic_blocks.iter_enumerated() {
        // --- 构建DFG ---
        for (statement_index, statement) in block_data.statements.iter().enumerate() {
            let location = mir::Location { block: block_id, statement_index };
            let current_node_index = node_map[&location];

            if let StatementKind::Assign(assign) = &statement.kind {
                let (place, rvalue) = &**assign;

                // 1. 处理右值 (Rvalue) - 变量的“使用”
                visit_rvalue(rvalue, &last_def, current_node_index, &mut cpg);

                // 2. 处理左值 (Place) - 变量的“定义”
                // 更新这个变量的最新定义位置
                last_def.insert(place.local, current_node_index);
            }
        }

        // --- 构建CFG ---
        let terminator = block_data.terminator();
        let terminator_loc = mir::Location { block: block_id, statement_index: block_data.statements.len() };
        let terminator_node_index = node_map[&terminator_loc];

        // 也为终结符中的 "use" 添加DFG边
        visit_terminator(terminator, &last_def, terminator_node_index, &mut cpg);

        // 根据终结符的类型连接控制流
        for successor_block in terminator.successors() {
            let successor_loc = mir::Location { block: successor_block, statement_index: 0 };
            if let Some(&successor_node_index) = node_map.get(&successor_loc) {
                cpg.add_edge(terminator_node_index, successor_node_index, EdgeType::ControlFlow);
            }
        }
    }

    cpg
}

/// 辅助函数：遍历Rvalue，为所有“使用”的变量添加DFG边
fn visit_rvalue(rvalue: &Rvalue, last_def: &HashMap<mir::Local, NodeIndex>, use_node: NodeIndex, cpg: &mut DiGraph<CpgNode, EdgeType>) {
    match rvalue {
        Rvalue::Use(operand) | Rvalue::CopyForDeref(operand) => {
            visit_operand(operand, last_def, use_node, cpg);
        }
        Rvalue::BinaryOp(_, box (left, right)) | Rvalue::CheckedBinaryOp(_, box (left, right)) => {
            visit_operand(left, last_def, use_node, cpg);
            visit_operand(right, last_def, use_node, cpg);
        }
        Rvalue::UnaryOp(_, operand) => {
            visit_operand(operand, last_def, use_node, cpg);
        }
        // 递归处理更复杂的结构
        Rvalue::Aggregate(_, operands) => {
            for op in operands {
                visit_operand(op, last_def, use_node, cpg);
            }
        }
        _ => {} // 其他Rvalue类型暂不处理
    }
}

/// 辅助函数：遍历Terminator，为所有“使用”的变量添加DFG边
fn visit_terminator(terminator: &mir::Terminator, last_def: &HashMap<mir::Local, NodeIndex>, use_node: NodeIndex, cpg: &mut DiGraph<CpgNode, EdgeType>) {
    match &terminator.kind {
        TerminatorKind::Call { args, .. } => {
            for arg in args {
                visit_operand(arg, last_def, use_node, cpg);
            }
        }
        TerminatorKind::SwitchInt { discr, .. } => {
            visit_operand(discr, last_def, use_node, cpg);
        }
        _ => {}
    }
}

/// 辅助函数：处理单个操作数（Operand），添加DFG边
fn visit_operand(operand: &mir::Operand, last_def: &HashMap<mir::Local, NodeIndex>, use_node: NodeIndex, cpg: &mut DiGraph<CpgNode, EdgeType>) {
    if let mir::Operand::Move(place) | mir::Operand::Copy(place) = operand {
        // 如果这个变量之前被定义过
        if let Some(&def_node) = last_def.get(&place.local) {
            // 添加一条从“定义”节点到“使用”节点的数据流边
            cpg.add_edge(def_node, use_node, EdgeType::DataFlow);
        }
    }
}


fn main() {
    let args = Args::parse();
    println!("🎯 目标Crate路径: {}", args.crate_path);

    let output = Command::new("rustc")
        .arg("--print")
        .arg("sysroot")
        .output()
        .expect("无法执行 `rustc --print sysroot`");
    let sysroot = String::from_utf8(output.stdout).unwrap().trim().to_string();
    println!("📚 使用Sysroot: {}", sysroot);

    let mut compiler_args = vec![
        "solana_cpg_generator".to_string(),
        "--crate-type".to_string(),
        "lib".to_string(),
        format!("--sysroot={}", sysroot),
        // Solana/Anchor项目通常需要特定的cfg标志才能正确编译
        "--cfg".to_string(),
        "feature=\"no-entrypoint\"".to_string(),
        args.crate_path,
    ];

    // 确保我们为Solana BPF目标进行编译
    compiler_args.push("--target=bpfel-unknown-unknown".to_string());

    println!("⚙️ 编译器参数: {:?}", compiler_args);

    let mut callbacks = CpgCallback;
    let compiler = rustc_driver::RunCompiler::new(&compiler_args, &mut callbacks);
    compiler.run().expect("编译和分析失败！");

    println!("\n🎉 分析流程成功完成！");
}
