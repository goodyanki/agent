// main.rs

use clap::Parser as ClapParser;
use serde::Serialize;
use std::error::Error;
use std::fs;
use std::path::{Path, PathBuf};
use tree_sitter::{Node, Parser as TreeSitterParser, Tree};
use walkdir::WalkDir;

/// 定义命令行参数结构
/// 使用 clap 库来轻松创建专业的命令行界面
#[derive(ClapParser, Debug)]
#[command(author, version, about, long_about = None)]
struct Args {
    /// 要分析的Solana项目的输入目录路径
    #[arg(short, long)]
    input: PathBuf,

    /// 用于存储生成的AST文件的输出目录路径
    #[arg(short, long)]
    output: PathBuf,
}

/// 自定义的、可序列化为JSON的AST节点结构
/// 我们将tree-sitter的节点递归地转换为这个结构，以便使用serde进行序列化
#[derive(Serialize, Debug)]
struct SerializableNode {
    kind: String,       // 节点的类型，例如 "function_item", "identifier"
    text: String,       // 该节点覆盖的源代码文本片段
    start_byte: usize,  // 在源文件中的起始字节位置
    end_byte: usize,    // 在源文件中的结束字节位置
    children: Vec<SerializableNode>, // 该节点的子节点列表
}

/// 递归函数，将tree-sitter的Node转换为我们的SerializableNode
/// 这是一个深度优先的遍历过程
fn node_to_serializable(node: Node, source_code: &str) -> SerializableNode {
    // 递归地为所有子节点调用此函数
    let children: Vec<SerializableNode> = node
        .children(&mut node.walk())
        .map(|child| node_to_serializable(child, source_code))
        .collect();

    SerializableNode {
        kind: node.kind().to_string(),
        text: node
            .utf8_text(source_code.as_bytes())
            .unwrap_or("") // 如果文本不是有效的UTF-8，则返回空字符串
            .to_string(),
        start_byte: node.start_byte(),
        end_byte: node.end_byte(),
        children,
    }
}

/// 核心处理函数：解析单个文件并保存其AST
fn process_file(
    source_path: &Path,
    input_dir: &Path,
    output_dir: &Path,
    parser: &mut TreeSitterParser,
) -> Result<(), Box<dyn Error>> {
    println!("正在处理: {}", source_path.display());

    // 步骤 1: 读取源代码文件内容
    let source_code = fs::read_to_string(source_path)?;

    // 步骤 2: 根据文件扩展名选择正确的语言语法
    // **FIXED**: 使用每个crate提供的安全的、公共的language()函数，
    // 而不是使用 extern "C" 块。
    // 注意 tree-sitter-typescript 的函数名是 language_typescript()。
    let language = match source_path.extension().and_then(|s| s.to_str()) {
        Some("rs") => tree_sitter_rust::language(),
        Some("ts") => tree_sitter_typescript::language_typescript(),
        Some("js") => tree_sitter_javascript::language(),
        _ => return Ok(()), // 安全地忽略不支持的文件类型
    };

    parser.set_language(&language)?;

    // 步骤 3: 解析源代码生成AST (Tree)
    let tree: Tree = match parser.parse(&source_code, None) {
        Some(tree) => tree,
        None => {
            // 如果tree-sitter无法解析文件，则打印警告并跳过
            eprintln!("警告: 解析文件失败 {}", source_path.display());
            return Ok(());
        }
    };
    
    // 步骤 4: 将整个AST转换为我们定义的可序列化结构
    let serializable_root = node_to_serializable(tree.root_node(), &source_code);
    // 使用serde_json将其转换为格式优美的JSON字符串
    let json_output = serde_json::to_string_pretty(&serializable_root)?;

    // 步骤 5: 计算并创建输出路径，以保持原始的目录结构
    let relative_path = source_path.strip_prefix(input_dir)?;
    let mut output_path = output_dir.join(relative_path);
    
    // 为输出文件添加新的后缀，例如 "lib.rs" -> "lib.rs.ast.json"
    let new_extension = match output_path.extension() {
        Some(ext) => format!("{}.ast.json", ext.to_str().unwrap_or("")),
        None => "ast.json".to_string(),
    };
    output_path.set_extension(new_extension);

    // 确保输出路径的父目录存在，如果不存在则创建
    if let Some(parent) = output_path.parent() {
        fs::create_dir_all(parent)?;
    }

    // 步骤 6: 将JSON字符串写入文件
    fs::write(&output_path, json_output)?;
    println!("  -> AST已保存至 {}", output_path.display());

    Ok(())
}

fn main() -> Result<(), Box<dyn Error>> {
    // 解析命令行传入的参数
    let args = Args::parse();

    // 验证输入路径是否存在且为一个目录
    if !args.input.is_dir() {
        return Err(format!("输入路径 '{}' 不是一个有效的目录。", args.input.display()).into());
    }

    println!("开始分析...");
    println!("输入项目路径: {}", args.input.display());
    println!("输出目录路径: {}", args.output.display());

    // 如果输出目录不存在，则递归创建它
    fs::create_dir_all(&args.output)?;
    
    // 初始化tree-sitter解析器。它将在所有文件的处理过程中被重用，以提高效率。
    let mut parser = TreeSitterParser::new();

    // (阶段1) 使用 walkdir 查找所有相关的源文件
    for entry in WalkDir::new(&args.input)
        .into_iter()
        .filter_map(|e| e.ok()) // 过滤掉无效的目录条目
        .filter(|e| e.path().is_file()) // 只关心文件
    {
        let path = entry.path();
        // 根据文件扩展名进行最终过滤
        if let Some(ext) = path.extension().and_then(|s| s.to_str()) {
            if ["rs", "ts", "js"].contains(&ext) {
                // (阶段2 & 3) 对找到的每个文件进行处理
                if let Err(e) = process_file(path, &args.input, &args.output, &mut parser) {
                    eprintln!(
                        "处理文件 {} 时发生错误: {}",
                        path.display(),
                        e
                    );
                }
            }
        }
    }

    println!("\n分析完成。所有AST文件已生成在 '{}' 目录中。", args.output.display());
    Ok(())
}