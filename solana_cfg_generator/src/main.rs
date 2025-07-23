// main.rs

/*
=================================================
 项目设置 (Cargo.toml) - 重要！
=================================================
请确保您的 `Cargo.toml` 文件包含了以下所有依赖项，
这是解决大部分编译错误的关键。

[dependencies]
clap = { version = "4.5.8", features = ["derive"] }
serde = { version = "1.0.203", features = ["derive"] }
serde_json = "1.0.120"
walkdir = "2.5.0"
petgraph = { version = "0.6.5", features = ["serde-1"] }

*/

use clap::Parser as ClapParser;
use petgraph::dot::{Config, Dot};
use petgraph::graph::{DiGraph, NodeIndex};
use serde::{Deserialize, Serialize};
use serde_json; // FIX: Added missing import for serde_json
use std::error::Error;
use std::fs;
use std::path::{Path, PathBuf};
use walkdir::WalkDir;

// --- 阶段 1: 数据结构定义 ---

/// 定义命令行参数
#[derive(ClapParser, Debug)]
#[command(author, version, about, long_about = None)]
struct Args {
    /// 包含AST JSON文件的输入目录
    #[arg(short, long)]
    input: PathBuf,

    /// 用于存储生成的CFG文件的输出目录
    #[arg(short, long)]
    output: PathBuf,
}

/// 从第一步复用的AST节点结构，用于反序列化
#[derive(Serialize, Deserialize, Debug, Clone)]
struct AstNode {
    kind: String,
    text: String,
    children: Vec<AstNode>,
}

/// 代表CFG中的一个基本块 (Basic Block)
#[derive(Serialize, Deserialize, Debug, Clone, Default)]
struct BasicBlock {
    statements: Vec<String>,
}

/// 用于构建CFG的状态机
struct CfgBuilder {
    graph: DiGraph<BasicBlock, ()>,
    entry_node: NodeIndex,
    exit_node: NodeIndex,
    current_block: NodeIndex,
    loop_contexts: Vec<(NodeIndex, NodeIndex)>, // (loop_start, loop_end)
}

impl CfgBuilder {
    fn new() -> Self {
        let mut graph = DiGraph::new();
        let entry_node = graph.add_node(BasicBlock {
            statements: vec!["Entry".to_string()],
        });
        let exit_node = graph.add_node(BasicBlock {
            statements: vec!["Exit".to_string()],
        });
        CfgBuilder {
            graph,
            entry_node,
            exit_node,
            current_block: entry_node,
            loop_contexts: vec![],
        }
    }

    /// 创建一个新的基本块
    fn new_block(&mut self) -> NodeIndex {
        self.graph.add_node(BasicBlock::default())
    }

    /// 在图中添加一条边
    fn add_edge(&mut self, from: NodeIndex, to: NodeIndex) {
        self.graph.add_edge(from, to, ());
    }

    /// 将一条语句添加到当前基本块
    fn add_statement_to_current_block(&mut self, statement: String) {
        if let Some(block) = self.graph.node_weight_mut(self.current_block) {
            block.statements.push(statement);
        }
    }
}

// --- 阶段 2: CFG 构建核心逻辑 ---

/// 递归地从AST节点构建CFG
fn build_cfg_from_ast(ast_node: &AstNode, builder: &mut CfgBuilder) {
    match ast_node.kind.as_str() {
        // 遇到函数体或代码块，遍历其子语句
        "statement_block" | "block" => {
            for child in &ast_node.children {
                build_cfg_from_ast(child, builder);
            }
        }

        // 处理 `if` 表达式 (if-else 和 if)
        "if_expression" => {
            let condition = ast_node
                .children
                .iter()
                .find(|c| c.kind == "condition")
                .map_or("".to_string(), |c| c.text.clone());
            builder.add_statement_to_current_block(format!("IF ({})", condition));

            let consequence = ast_node
                .children
                .iter()
                .find(|c| c.kind == "consequence");
            let alternative = ast_node
                .children
                .iter()
                .find(|c| c.kind == "alternative");

            let if_block_end = builder.current_block;
            let merge_block = builder.new_block();

            // 处理 `then` 分支
            if let Some(consequence_node) = consequence {
                let then_block_start = builder.new_block();
                builder.add_edge(if_block_end, then_block_start);
                builder.current_block = then_block_start;
                build_cfg_from_ast(consequence_node, builder);
                builder.add_edge(builder.current_block, merge_block);
            }

            // 处理 `else` 分支
            if let Some(alternative_node) = alternative {
                let else_block_start = builder.new_block();
                builder.add_edge(if_block_end, else_block_start);
                builder.current_block = else_block_start;
                build_cfg_from_ast(alternative_node, builder);
                builder.add_edge(builder.current_block, merge_block);
            } else {
                // 如果没有 `else`，`if` 块可以直接跳到合并块
                builder.add_edge(if_block_end, merge_block);
            }

            builder.current_block = merge_block;
        }

        // 处理 `return` 语句
        "return_expression" => {
            builder.add_statement_to_current_block(ast_node.text.clone());
            builder.add_edge(builder.current_block, builder.exit_node);
            // return后创建一个新块，但不再连接它，因为它代表不可达代码
            builder.current_block = builder.new_block();
        }

        // 简化的循环处理 (loop, while, for)
        "loop_expression" | "while_expression" | "for_expression" => {
            let loop_header = builder.new_block();
            builder.add_edge(builder.current_block, loop_header);
            
            let loop_body_start = builder.new_block();
            let after_loop_block = builder.new_block();

            // 循环上下文，用于 `break` 和 `continue`
            builder.loop_contexts.push((loop_header, after_loop_block));

            // 循环头连接到循环体和循环后
            builder.add_edge(loop_header, loop_body_start);
            builder.add_edge(loop_header, after_loop_block); // 循环退出的边
            
            // 构建循环体
            builder.current_block = loop_body_start;
            let body_node = ast_node.children.iter().find(|c| c.kind == "statement_block");
            if let Some(body) = body_node {
                build_cfg_from_ast(body, builder);
            }
            
            // 循环体末尾跳回循环头
            builder.add_edge(builder.current_block, loop_header);

            builder.loop_contexts.pop();
            builder.current_block = after_loop_block;
        }

        // `break` 语句
        "break_expression" => {
            builder.add_statement_to_current_block("break".to_string());
            if let Some(&(_, loop_end)) = builder.loop_contexts.last() {
                builder.add_edge(builder.current_block, loop_end);
            }
            builder.current_block = builder.new_block(); // 不可达代码块
        }

        // 对于其他普通语句，直接添加到当前块
        _ => {
            if !ast_node.kind.ends_with("_statement")
                && !ast_node.kind.ends_with("_declaration")
                && !ast_node.kind.ends_with("_item")
            {
                // 递归处理子节点以深入查找语句
                for child in &ast_node.children {
                    build_cfg_from_ast(child, builder);
                }
            } else {
                // 将语句/声明的文本简化为一行，以保持CFG节点的可读性
                let simplified_text = ast_node.text.lines().next().unwrap_or("").trim().to_string();
                if !simplified_text.is_empty() {
                    builder.add_statement_to_current_block(simplified_text);
                }
            }
        }
    }
}

// --- 阶段 3: 文件处理与主逻辑 ---

/// 处理单个AST文件，为其中的所有函数生成CFG
fn process_ast_file(
    ast_path: &Path,
    input_dir: &Path,
    output_dir: &Path,
) -> Result<(), Box<dyn Error>> {
    let content = fs::read_to_string(ast_path)?;
    let root_node: AstNode = serde_json::from_str(&content)?;

    // 查找所有函数
    let mut functions = vec![];
    find_functions(&root_node, &mut functions);

    for func_node in functions {
        let func_name = func_node
            .children
            .iter()
            .find(|c| c.kind == "identifier")
            .map_or("unknown_function".to_string(), |c| c.text.clone());

        println!(
            "  -> Found function: `{}` in {}",
            func_name,
            ast_path.file_name().unwrap().to_str().unwrap()
        );

        let mut builder = CfgBuilder::new();
        
        // 找到函数体并开始构建CFG
        if let Some(body) = func_node.children.iter().find(|c| c.kind == "statement_block") {
            build_cfg_from_ast(body, &mut builder);
        }
        
        // 将最后一个活动块连接到出口
        builder.add_edge(builder.current_block, builder.exit_node);

        // --- 序列化与保存 ---
        let relative_path = ast_path.strip_prefix(input_dir)?;
        let mut output_path_base = output_dir.join(relative_path);
        
        // **FIXED**: 改进文件命名逻辑，使其更清晰
        let original_filename = output_path_base.file_name().unwrap().to_str().unwrap();
        let new_filename_base = original_filename.replace(".ast.json", "");
        output_path_base.set_file_name(format!("{}.{}.cfg", new_filename_base, func_name));
        
        // 确保父目录存在
        if let Some(parent) = output_path_base.parent() {
            fs::create_dir_all(parent)?;
        }

        // 保存为 .dot 文件 (用于可视化)
        let mut dot_path = output_path_base.clone();
        dot_path.set_extension("dot");
        let dot_content = format!(
            "{:?}",
            Dot::with_config(&builder.graph, &[Config::EdgeNoLabel])
        );
        fs::write(&dot_path, dot_content)?;

        // 保存为 .json 文件 (用于程序化分析)
        let mut json_path = output_path_base;
        json_path.set_extension("json");
        let serializable_graph = builder.graph.map(
            |_, node_weight| node_weight.clone(),
            |_, _| (),
        );
        let json_content = serde_json::to_string_pretty(&serializable_graph)?;
        fs::write(&json_path, json_content)?;
    }

    Ok(())
}

/// 递归辅助函数，用于在AST中查找所有 `function_item`
fn find_functions<'a>(node: &'a AstNode, functions: &mut Vec<&'a AstNode>) {
    if node.kind == "function_item" {
        functions.push(node);
    }
    for child in &node.children {
        find_functions(child, functions);
    }
}

fn main() -> Result<(), Box<dyn Error>> {
    let args = Args::parse();
    if !args.input.is_dir() {
        return Err(format!("Input path '{}' is not a valid directory.", args.input.display()).into());
    }
    fs::create_dir_all(&args.output)?;

    println!("Starting CFG generation...");
    println!("Input AST directory: {}", args.input.display());
    println!("Output CFG directory: {}", args.output.display());

    // 遍历输入目录，查找所有Rust的AST文件
    for entry in WalkDir::new(&args.input)
        .into_iter()
        .filter_map(|e| e.ok())
        .filter(|e| e.path().is_file() && e.path().to_str().unwrap().ends_with(".rs.ast.json"))
    {
        let path = entry.path();
        println!("\nProcessing file: {}", path.display());
        if let Err(e) = process_ast_file(path, &args.input, &args.output) {
            eprintln!("Error processing file {}: {}", path.display(), e);
        }
    }

    println!("\nCFG generation complete.");
    Ok(())
}
